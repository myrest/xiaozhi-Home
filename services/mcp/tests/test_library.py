from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import io
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from bedtime_mcp.library import Library, LibraryError


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "card"
        self.root.mkdir()
        self.database = self.base / "state" / "library.sqlite3"
        self.library = Library(self.root, self.database, require_mount=False)
        self.addCleanup(self.library.close)

    def media(self, path):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fixture media")
        return target

    def stories(self):
        for name in ("10 finale.mp3", "2 second.mp3", "1 first.mp3", "11 ending.mp3"):
            self.media("Stories/" + name)

    def state(self):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute('SELECT path, played, played_at, play_count, "exists" FROM files ORDER BY path').fetchall()

    def records(self, series="Stories"):
        return {item["episode"]: item for item in self.library.catalog(series)["files"]}

    def test_manual_range_survives_reopen_without_fabricating_completions(self):
        self.stories()
        self.media("Other/2 unrelated.mp3")
        result = self.library.mark_played("stories", 2, 10)
        self.assertEqual(result["updated"], 2)
        self.library.close()
        self.library = Library(self.root, self.database, require_mount=False)
        self.addCleanup(self.library.close)
        records = self.records()
        self.assertFalse(records[1]["played"])
        self.assertTrue(records[2]["played"])
        self.assertTrue(records[10]["played"])
        self.assertFalse(records[11]["played"])
        self.assertTrue(records[2]["played_at"].endswith("+00:00"))
        self.assertEqual(records[2]["play_count"], 0)
        self.assertFalse(self.records("Other")[2]["played"])
        self.library.mark_played("Stories", 2, 10, played=False)
        records = self.records()
        self.assertFalse(records[2]["played"])
        self.assertIsNone(records[2]["played_at"])
        self.assertEqual(records[2]["play_count"], 0)

    def test_range_must_match_selected_series_endpoints(self):
        self.stories()
        self.library.scan()
        before = self.state()
        for arguments in (("Stories", 3), ("Stories", 1, 9), ("Stories", 10, 2), ("Stories", -1)):
            with self.subTest(arguments=arguments), self.assertRaises(LibraryError):
                self.library.mark_played(*arguments)
        self.assertEqual(self.state(), before)

    def test_from_episode_replays_and_never_marks_earlier_files(self):
        self.stories()
        self.library.mark_played("Stories", 2, 10)
        before = self.state()
        with patch.object(self.library, "duration", return_value=120):
            plan = self.library.plan("Stories", start_episode=2, count=2)
        self.assertEqual([item["episode"] for item in plan["files"]], [2, 10])
        self.assertEqual(plan["story_seconds"], 240)
        self.assertTrue(plan["count_satisfied"])
        self.assertFalse(plan["requires_confirmation"])
        self.assertEqual(self.state(), before)
        self.assertFalse(self.records()[1]["played"])

    def test_plan_is_read_only_even_before_first_scan(self):
        self.stories()
        before = self.database.read_bytes()
        with patch.object(self.library, "duration", return_value=60):
            plan = self.library.plan("Stories", count=1)
        self.assertEqual(plan["files"][0]["episode"], 1)
        self.assertEqual(self.state(), [])
        self.assertEqual(self.database.read_bytes(), before)
        self.library.complete(plan["files"][0]["path"])
        self.assertEqual(self.records()[1]["play_count"], 1)

    def test_numeric_and_natural_order(self):
        for name in ("10 ten.mp3", "2 take10.mp3", "2 take2.mp3", "1 one.mp3", "extra10.mp3", "extra2.mp3"):
            self.media("Stories/" + name)
        with patch.object(self.library, "duration", return_value=60):
            plan = self.library.plan("Stories")
        self.assertEqual([Path(item["path"]).name for item in plan["files"]],
                         ["1 one.mp3", "2 take2.mp3", "2 take10.mp3", "10 ten.mp3", "extra2.mp3", "extra10.mp3"])
        with self.assertRaises(LibraryError):
            self.library.plan("Stories", start_episode=2)

    def test_whole_file_budget_stops_at_first_over_budget_without_skipping(self):
        self.stories()
        seconds = {1: 60, 2: 121, 10: 10, 11: 10}
        with patch.object(self.library, "duration", side_effect=lambda path: seconds[int(Path(path).name.split()[0])]):
            plan = self.library.plan("Stories", minutes=3, count=3)
        self.assertEqual([item["episode"] for item in plan["files"]], [1])
        self.assertEqual(plan["blocked_next"]["episode"], 2)
        self.assertEqual(plan["story_seconds"], 60)
        self.assertFalse(plan["count_satisfied"])
        self.assertTrue(plan["requires_confirmation"])

    def test_exact_budget_and_count_limit(self):
        self.stories()
        with patch.object(self.library, "duration", return_value=60) as probe:
            plan = self.library.plan("Stories", minutes=2, count=2)
        self.assertEqual(len(plan["files"]), 2)
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(plan["story_seconds"], 120)
        self.assertIsNone(plan["blocked_next"])
        self.assertFalse(plan["requires_confirmation"])

    def test_availability_count_conflict_requires_confirmation(self):
        self.media("Stories/1 only.mp3")
        with patch.object(self.library, "duration", return_value=60):
            plan = self.library.plan("Stories", count=2)
        self.assertFalse(plan["count_satisfied"])
        self.assertTrue(plan["requires_confirmation"])
        self.assertIsNone(plan["blocked_next"])

    def test_no_whole_file_or_no_unplayed_files_is_explicit_error(self):
        self.media("Stories/1 only.mp3")
        with patch.object(self.library, "duration", return_value=61):
            with self.assertRaises(LibraryError):
                self.library.plan("Stories", minutes=1)
        self.library.mark_played("Stories", 1)
        with self.assertRaises(LibraryError):
            self.library.plan("Stories")

    def test_append_excludes_current_and_queued_but_never_changes_explicit_start(self):
        self.stories()
        excluded = ("Stories/1 first.mp3", "Stories/2 second.mp3")
        with patch.object(self.library, "duration", return_value=60):
            plan = self.library.plan("Stories", exclude_paths=excluded)
            self.assertEqual([item["episode"] for item in plan["files"]], [10, 11])
            with self.assertRaises(LibraryError):
                self.library.plan("Stories", start_episode=2, exclude_paths=excluded)
            with self.assertRaises(LibraryError):
                self.library.plan("Stories", start_episode=3)

    def test_parameters_are_strict(self):
        self.stories()
        for minutes in (0, -1, float("nan"), float("inf"), True, "30"):
            with self.subTest(minutes=minutes), self.assertRaises(LibraryError):
                self.library.plan("Stories", minutes=minutes)
        for count in (0, -1, 1.5, True):
            with self.subTest(count=count), self.assertRaises(LibraryError):
                self.library.plan("Stories", count=count)
        for episode in (-1, 1.5, True):
            with self.subTest(episode=episode), self.assertRaises(LibraryError):
                self.library.plan("Stories", start_episode=episode)

    def test_exact_series_precedes_unique_substring_and_ambiguity_is_explicit(self):
        self.media("Story/1 first.mp3")
        self.media("Story Extended/1 first.mp3")
        self.assertEqual(self.library.catalog("sToRy")["series"], "Story")
        self.assertEqual(self.library.catalog("extended")["series"], "Story Extended")
        for series in ("stor", "absent"):
            with self.subTest(series=series), self.assertRaises(LibraryError):
                self.library.catalog(series)
        with self.assertRaisesRegex(LibraryError, "Choose a series"):
            self.library.plan()

    def test_catalog_search_unplayed_and_frequent_series_first(self):
        self.media("A/1 first.mp3")
        self.media("Z/1 first.mp3")
        self.media("Z/2 second.mp3")
        self.library.complete("Z/1 first.mp3")
        summary = self.library.catalog()["series"]
        self.assertEqual([item["name"] for item in summary], ["Z", "A"])
        self.assertEqual(summary[0]["play_count"], 1)
        result = self.library.catalog(query="FIRST", unplayed_only=True)
        self.assertEqual([item["path"] for item in result["files"]], ["A/1 first.mp3"])
        self.assertTrue(all("exists" in item for item in result["files"]))

    def test_discovery_excludes_hidden_special_directories_and_symlinks(self):
        self.media("Stories/1 first.MP3")
        for path in (".hidden/2 secret.mp3", "Stories/.secret.mp3", "System/睡覺音樂.m4a", "Gillian Aduio/3 excluded.mp3"):
            self.media(path)
        outside = self.base / "outside.mp3"
        outside.write_bytes(b"not a story")
        (self.root / "Stories/4 linked.mp3").symlink_to(outside)
        (self.root / "linked").symlink_to(self.root / "Stories", target_is_directory=True)
        self.assertEqual(self.library.scan()["total"], 1)
        self.assertEqual(self.library.sleep_file(), self.root / "System/睡覺音樂.m4a")
        for path in ("../outside.mp3", str(outside), "Stories/4 linked.mp3", "linked/1 first.MP3",
                     "Gillian Aduio/3 excluded.mp3", "Stories/.secret.mp3", "Stories//1 first.MP3"):
            with self.subTest(path=path), self.assertRaises(LibraryError):
                self.library.resolve(path)
        with self.assertRaises(LibraryError):
            self.library.complete("System/睡覺音樂.m4a")

    def test_empty_and_missing_source_preserve_existing_state(self):
        path = self.media("Stories/1 first.mp3")
        self.library.complete("Stories/1 first.mp3")
        before = self.state()
        path.unlink()
        with self.assertRaises(LibraryError):
            self.library.scan()
        self.assertEqual(self.state(), before)
        renamed = self.base / "removed-card"
        self.root.rename(renamed)
        with self.assertRaises(LibraryError):
            self.library.scan()
        self.assertEqual(self.state(), before)

    def test_missing_files_keep_history_when_they_return(self):
        first = self.media("Stories/1 first.mp3")
        self.media("Stories/2 second.mp3")
        self.library.complete("Stories/1 first.mp3")
        first.unlink()
        self.assertEqual(self.library.scan()["missing"], 1)
        missing = next(row for row in self.state() if row[0] == "Stories/1 first.mp3")
        self.assertEqual((missing[1], missing[3], missing[4]), (1, 1, 0))
        self.media("Stories/1 first.mp3")
        self.library.scan()
        returned = self.records()[1]
        self.assertTrue(returned["played"])
        self.assertEqual(returned["play_count"], 1)
        self.assertTrue(returned["exists"])

    def test_mount_failure_preserves_state_and_parent_mount_is_not_card_mount(self):
        self.media("Stories/1 first.mp3")
        self.library.complete("Stories/1 first.mp3")
        before = self.state()
        self.library.require_mount = True
        parent_mount = f"10 1 0:1 / {self.root.parent} ro - tmpfs tmpfs ro\n"
        with patch("builtins.open", return_value=io.StringIO(parent_mount)):
            with self.assertRaises(LibraryError):
                self.library.scan()
        self.assertEqual(self.state(), before)
        child_mount = f"11 10 8:3 / {self.root} ro - vfat /dev/card ro\n"
        with patch("builtins.open", side_effect=lambda *args, **kwargs: io.StringIO(parent_mount + child_mount)):
            self.assertEqual(self.library.scan()["total"], 1)
        self.assertEqual(self.state(), before)

    def test_ffprobe_uses_argument_list_timeout_and_validates_duration(self):
        self.media("Stories/1 first.mp3")
        response = subprocess.CompletedProcess([], 0, stdout="61.25\n", stderr="")
        with patch("bedtime_mcp.library.subprocess.run", return_value=response) as run:
            self.assertEqual(self.library.duration("Stories/1 first.mp3"), 61.25)
        arguments, options = run.call_args
        self.assertIsInstance(arguments[0], list)
        self.assertEqual(arguments[0][-1], str(self.root / "Stories/1 first.mp3"))
        self.assertGreater(options["timeout"], 0)
        self.assertTrue(options["check"])
        for output in ("nan", "inf", "0", "-2", "not-a-duration"):
            with self.subTest(output=output):
                response = subprocess.CompletedProcess([], 0, stdout=output, stderr="")
                with patch("bedtime_mcp.library.subprocess.run", return_value=response), self.assertRaises(LibraryError):
                    self.library.duration("Stories/1 first.mp3")
        with patch("bedtime_mcp.library.subprocess.run", side_effect=subprocess.TimeoutExpired("ffprobe", 20)):
            with self.assertRaises(LibraryError):
                self.library.duration("Stories/1 first.mp3")

    def test_successful_completions_are_transactional_across_worker_threads(self):
        self.media("Stories/1 first.mp3")
        with ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(self.library.complete, ["Stories/1 first.mp3"] * 12))
        record = self.records()[1]
        self.assertEqual(record["play_count"], 12)
        self.assertTrue(record["played"])
        self.library.mark_played("Stories", 1, played=False)
        self.assertEqual(self.records()[1]["play_count"], 12)

    def test_corrupt_database_is_never_replaced(self):
        corrupt = self.base / "corrupt.sqlite3"
        content = b"this is not sqlite and must not be overwritten"
        corrupt.write_bytes(content)
        with self.assertRaises(LibraryError):
            Library(self.root, corrupt, require_mount=False)
        self.assertEqual(corrupt.read_bytes(), content)

    def test_subdirectories_discovery_and_catalog_listing(self):
        self.media("米小圈系列/【完结】米小圈上学记 四年级/第一期/1 first.mp3")
        self.media("米小圈系列/【完结】米小圈上学记 四年级/第一期/2 second.mp3")
        self.media("米小圈系列/【完结】米小圈上学记 四年级/第二期/1 first.mp3")
        self.media("米小圈系列/米小圈睡前故事/1 bedtime.mp3")
        self.media("Other/1 normal.mp3")

        # Top level catalog includes subdirectories count
        top = self.library.catalog()
        summaries = {s["name"]: s for s in top["series"]}
        self.assertEqual(summaries["米小圈系列"]["subdirectories"], 2)
        self.assertEqual(summaries["米小圈系列"]["total"], 4)
        self.assertEqual(summaries["Other"]["subdirectories"], 0)
        self.assertEqual(summaries["Other"]["total"], 1)

        # Query 米小圈系列: should return subdirectories and empty files
        mxq = self.library.catalog(series="米小圈系列")
        self.assertEqual(mxq["series"], "米小圈系列")
        self.assertEqual(mxq["files"], [])
        sub_names = [sub["name"] for sub in mxq["subdirectories"]]
        self.assertEqual(sub_names, ["【完结】米小圈上学记 四年级", "米小圈睡前故事"])

        # Query using directory parameter
        mxq_dir = self.library.catalog(directory="米小圈系列")
        self.assertEqual(mxq_dir["directory"], "米小圈系列")
        self.assertEqual(len(mxq_dir["subdirectories"]), 2)

        # Query nested directory
        grade4 = self.library.catalog(directory="米小圈系列/【完结】米小圈上学记 四年级")
        self.assertEqual(grade4["files"], [])
        self.assertEqual([s["name"] for s in grade4["subdirectories"]], ["第一期", "第二期"])

        # Query leaf directory
        term1 = self.library.catalog(directory="米小圈系列/【完结】米小圈上学记 四年级/第一期")
        self.assertEqual(term1["subdirectories"], [])
        self.assertEqual(len(term1["files"]), 2)
        self.assertEqual([f["episode"] for f in term1["files"]], [1, 2])

        # Subdirectories method directly
        subs = self.library.subdirectories("米小圈系列")
        self.assertEqual(len(subs["subdirectories"]), 2)

        # subdirectories_only flag
        leaf_subs = self.library.catalog(directory="米小圈系列/【完结】米小圈上学记 四年级/第一期",
                                         subdirectories_only=True)
        self.assertEqual(leaf_subs["files"], [])
        self.assertEqual(leaf_subs["subdirectories"], [])

    def test_plan_nested_directories_and_auto_descend(self):
        self.media("米小圈系列/【完结】米小圈上学记 四年级/第一期/1 first.mp3")
        self.media("米小圈系列/【完结】米小圈上学记 四年级/第二期/1 first.mp3")
        self.media("米小圈系列/单目录系列/唯一子目錄/1 single.mp3")
        self.media("米小圈系列/0 parent track.mp3")

        # Planning 米小圈系列 (has multiple subdirectories) must ask even with direct audio
        with self.assertRaisesRegex(LibraryError, "Choose a subdirectory"):
            self.library.plan("米小圈系列")
        with self.assertRaisesRegex(LibraryError, "Choose a subdirectory"):
            self.library.mark_played("米小圈系列", 1)

        # Planning single child subdirectory auto-descends
        with patch.object(self.library, "duration", return_value=60):
            plan = self.library.plan("单目录系列")
            self.assertEqual(len(plan["files"]), 1)
            self.assertEqual(plan["files"][0]["path"], "米小圈系列/单目录系列/唯一子目錄/1 single.mp3")

        # Planning explicit leaf succeeds
        with patch.object(self.library, "duration", return_value=60):
            plan_leaf = self.library.plan("第一期")
            self.assertEqual(len(plan_leaf["files"]), 1)
            self.assertEqual(plan_leaf["files"][0]["path"], "米小圈系列/【完结】米小圈上学记 四年级/第一期/1 first.mp3")


if __name__ == "__main__":
    unittest.main()
