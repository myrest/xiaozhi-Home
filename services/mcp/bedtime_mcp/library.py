"""Read-only media discovery and independent completion records."""

from __future__ import annotations

import math
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
import threading
from datetime import datetime, timezone


class LibraryError(ValueError):
    """A library request cannot be safely fulfilled."""


_AUDIO = frozenset({".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".opus", ".wma"})
_SLEEP = "System/睡覺音樂.m4a"


def _natural(value: str) -> tuple:
    return tuple((1, int(part)) if part.isdigit() else (0, part.casefold())
                 for part in re.split(r"(\d+)", value))


def _episode(path: str) -> int | None:
    match = re.match(r"(\d+)", PurePosixPath(path).name)
    return int(match.group(1)) if match else None


def _order(record: dict) -> tuple:
    episode = record["episode"]
    return (episode is None, episode if episode is not None else 0,
            _natural(record["path"]), record["path"])


class Library:
    def __init__(self, root: Path, database: Path, *, require_mount: bool = True):
        self.root = Path(root).absolute()
        self.database = Path(database)
        self.require_mount = require_mount
        self._lock = threading.RLock()
        self._closed = False
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = None
        try:
            connection = sqlite3.connect(self.database, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise LibraryError("Library database is corrupt; refusing to reset it")
            with connection:
                connection.execute('''CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY,
                    played INTEGER NOT NULL DEFAULT 0,
                    played_at TEXT,
                    play_count INTEGER NOT NULL DEFAULT 0,
                    "exists" INTEGER NOT NULL DEFAULT 1
                )''')
                connection.execute('SELECT path, played, played_at, play_count, "exists" FROM files LIMIT 0')
        except (sqlite3.Error, LibraryError) as exc:
            if connection is not None:
                connection.close()
            raise LibraryError(f"Cannot open library database without resetting it: {exc}") from exc
        self._db = connection

    def _ensure_open(self) -> None:
        if self._closed:
            raise LibraryError("Library is closed")

    def _validate_root(self) -> None:
        if self.root.is_symlink() or not self.root.is_dir():
            raise LibraryError(f"Source directory is unavailable: {self.root}")
        if self.require_mount:
            try:
                # A propagated child mount is visible here even when /mnt is the
                # container bind mount. Do not mistake the /mnt bind for the card.
                mountpoints = set()
                with open("/proc/self/mountinfo", encoding="utf-8") as mounts:
                    for line in mounts:
                        field = line.split()[4]
                        mountpoints.add(re.sub(r"\\([0-7]{3})",
                                               lambda m: chr(int(m.group(1), 8)), field))
                mounted = str(self.root.resolve()) in mountpoints
            except (OSError, IndexError) as exc:
                raise LibraryError(f"Cannot verify source mount: {exc}") from exc
            if not mounted:
                raise LibraryError(f"Source is not mounted: {self.root}")

    def _discover(self) -> list[dict]:
        self._validate_root()
        found = []
        try:
            pending = [self.root]
            while pending:
                directory = pending.pop()
                for entry in directory.iterdir():
                    if (entry.name.startswith(".") or entry.is_symlink()
                            or entry.name.casefold() in {"system", "gillian aduio"}):
                        continue
                    if entry.is_dir():
                        pending.append(entry)
                    elif entry.is_file() and entry.suffix.casefold() in _AUDIO:
                        relative = entry.relative_to(self.root).as_posix()
                        parts = PurePosixPath(relative).parts
                        series = PurePosixPath(*parts[:-1]).as_posix() if len(parts) > 1 else self.root.name
                        found.append({"path": relative, "series": series,
                                      "episode": _episode(relative)})
        except OSError as exc:
            raise LibraryError(f"Cannot read source library: {exc}") from exc
        if not found:
            raise LibraryError("Source contains no ordinary audio files; existing state was preserved")
        return sorted(found, key=_order)

    def _records(self, found: list[dict]) -> list[dict]:
        with self._lock:
            self._ensure_open()
            try:
                stored = {row["path"]: dict(row) for row in self._db.execute("SELECT * FROM files")}
            except sqlite3.Error as exc:
                raise LibraryError(f"Cannot read library state: {exc}") from exc
        return [dict(record, played=bool(stored.get(record["path"], {}).get("played", False)),
                     played_at=stored.get(record["path"], {}).get("played_at"),
                     play_count=stored.get(record["path"], {}).get("play_count", 0), exists=True)
                for record in found]

    @staticmethod
    def _all_directories(records: list[dict]) -> list[str]:
        directories = set()
        for record in records:
            parts = PurePosixPath(record["path"]).parts[:-1]
            if not parts:
                directories.add(record["series"])
            else:
                for depth in range(1, len(parts) + 1):
                    directories.add(PurePosixPath(*parts[:depth]).as_posix())
        return sorted(directories, key=_natural)

    @staticmethod
    def _summaries(records: list[dict], parent: str | None = None) -> list[dict]:
        grouped = {}
        children: dict[str, set[str]] = {}
        parent_parts = PurePosixPath(parent).parts if parent else ()
        for record in records:
            dir_parts = PurePosixPath(record["path"]).parts[:-1]
            if parent:
                if len(dir_parts) <= len(parent_parts) or dir_parts[:len(parent_parts)] != parent_parts:
                    continue
                idx = len(parent_parts)
                name = dir_parts[idx]
                path = PurePosixPath(*dir_parts[:idx + 1]).as_posix()
                rest = dir_parts[idx + 1:]
            elif not dir_parts:
                name = record["series"]
                path = record["series"]
                rest = ()
            else:
                name = dir_parts[0]
                path = dir_parts[0]
                rest = dir_parts[1:]
            summary = grouped.setdefault(path, {"name": name, "path": path, "total": 0,
                                                "played": 0, "unplayed": 0, "play_count": 0})
            summary["total"] += 1
            summary["played"] += int(record["played"])
            summary["unplayed"] += int(not record["played"])
            summary["play_count"] += record["play_count"]
            if rest:
                children.setdefault(path, set()).add(rest[0])
        for path, summary in grouped.items():
            summary["subdirectories"] = len(children.get(path, ()))
        return sorted(grouped.values(), key=lambda item: (-item["play_count"], _natural(item["name"]), item["name"]))

    @classmethod
    def _select(cls, records: list[dict], series: str | None, *, parent: str | None = None) -> str:
        all_dirs = cls._all_directories(records)
        if parent is not None:
            prefix = f"{parent}/"
            names = [d for d in all_dirs if d.startswith(prefix)]
            choices = sorted((item["name"] for item in cls._summaries(records, parent=parent)), key=_natural)
        else:
            names = all_dirs
            choices = sorted((item["name"] for item in cls._summaries(records)), key=_natural)
        if series is None:
            if len(choices) == 1:
                return choices[0]
            raise LibraryError("Choose a series: " + ", ".join(choices))
        if not isinstance(series, str) or not series.strip():
            raise LibraryError("Series must be a nonempty name")
        cleaned = series.strip().strip("/")
        if not cleaned:
            raise LibraryError("Series must be a nonempty name")
        needle = cleaned.casefold()
        scoped_needle = f"{parent}/{cleaned}".casefold() if parent is not None else needle
        matches = [d for d in names if d.casefold() in {needle, scoped_needle}]
        if not matches:
            matches = [d for d in names if PurePosixPath(d).name.casefold() == needle]
        if not matches:
            matches = [d for d in names if needle in PurePosixPath(d).name.casefold()]
        if not matches:
            matches = [d for d in names if needle in d.casefold()]
        if len(matches) > 1:
            pruned = [
                m for m in matches
                if not any(other != m and m.startswith(other + "/") for other in matches)
            ]
            if pruned:
                matches = pruned
        if len(matches) != 1:
            raise LibraryError(("Ambiguous series: " if matches else "Unknown series; choose: ")
                               + ", ".join(matches or choices or names))
        return matches[0]

    @classmethod
    def _select_playable(cls, records: list[dict], series: str | None) -> str:
        selected = cls._select(records, series)
        candidates = [record for record in records if record["series"] == selected]
        subdirs = cls._summaries(records, parent=selected)
        while not candidates and len(subdirs) == 1:
            selected = subdirs[0]["path"]
            candidates = [record for record in records if record["series"] == selected]
            subdirs = cls._summaries(records, parent=selected)
        if len(subdirs) > 1:
            names = sorted((item["name"] for item in subdirs), key=_natural)
            raise LibraryError(f"Choose a subdirectory under {selected}: " + ", ".join(names))
        return selected
    def scan(self) -> dict:
        found = self._discover()  # Validate everything before changing any state.
        with self._lock:
            self._ensure_open()
            try:
                with self._db:
                    self._db.execute('UPDATE files SET "exists" = 0')
                    self._db.executemany('''INSERT INTO files(path) VALUES (?)
                        ON CONFLICT(path) DO UPDATE SET "exists" = 1''',
                                         ((record["path"],) for record in found))
                records = self._records(found)
                missing = self._db.execute('SELECT COUNT(*) FROM files WHERE "exists" = 0').fetchone()[0]
            except sqlite3.Error as exc:
                raise LibraryError(f"Cannot save library scan: {exc}") from exc
        return {"total": len(records), "played": sum(record["played"] for record in records),
                "unplayed": sum(not record["played"] for record in records), "missing": missing,
                "series": self._summaries(records)}

    def subdirectories(self, directory: str | None = None,
                       unplayed_only: bool = False) -> dict:
        if not isinstance(unplayed_only, bool):
            raise LibraryError("unplayed_only must be a boolean")
        self.scan()
        records = self._records(self._discover())
        filtered = [r for r in records if not unplayed_only or not r["played"]]
        if directory is None:
            return {"directory": None, "subdirectories": self._summaries(filtered)}
        selected = self._select(records, directory)
        return {"directory": selected, "subdirectories": self._summaries(filtered, parent=selected)}

    def catalog(self, series: str | None = None, query: str | None = None,
                unplayed_only: bool = False, directory: str | None = None,
                subdirectories_only: bool = False) -> dict:
        if not isinstance(unplayed_only, bool) or not isinstance(subdirectories_only, bool):
            raise LibraryError("Boolean flags must be booleans")
        if query is not None and not isinstance(query, str):
            raise LibraryError("Query must be a string")
        self.scan()
        records = self._records(self._discover())
        filtered = [r for r in records if not unplayed_only or not r["played"]]
        if series is not None and directory is not None:
            base = self._select(records, series)
            if directory.strip().strip("/").casefold() in {
                base.casefold(), PurePosixPath(base).name.casefold()
            }:
                selected = base
            else:
                selected = self._select(records, directory, parent=base)
        elif directory is not None:
            selected = self._select(records, directory)
        elif series is not None:
            selected = self._select(records, series)
        else:
            selected = None
        if selected is None and query is None:
            summaries = self._summaries(filtered)
            if subdirectories_only:
                return {"directory": None, "series": summaries, "subdirectories": summaries}
            return {"series": summaries}
        subdirs = self._summaries(filtered, parent=selected) if selected is not None else []
        if query is not None:
            needle = query.casefold()
            if selected is None:
                subdirs = [
                    {"name": PurePosixPath(path).name, "path": path}
                    for path in self._all_directories(filtered)
                    if needle in path.casefold()
                ]
            else:
                subdirs = [item for item in subdirs if needle in item["path"].casefold()]
        if subdirectories_only:
            files = []
        elif query is not None:
            prefix = f"{selected}/" if selected is not None else ""
            files = [
                record for record in filtered
                if (selected is None or record["series"] == selected or record["path"].startswith(prefix))
                and query.casefold() in record["path"].casefold()
            ]
        else:
            files = [record for record in filtered if record["series"] == selected]
        if selected is None:
            return {"series": None, "subdirectories": subdirs, "files": files}
        return {"series": selected, "directory": selected, "subdirectories": subdirs, "files": files}
    def plan(self, series: str | None = None, minutes: float = 30,
             count: int | None = None, start_episode: int | None = None, *,
             exclude_paths: tuple[str, ...] = ()) -> dict:
        if isinstance(minutes, bool) or not isinstance(minutes, (float, int)):
            raise LibraryError("Minutes must be finite and positive")
        try:
            minutes = float(minutes)
        except OverflowError as exc:
            raise LibraryError("Minutes must be finite and positive") from exc
        if not math.isfinite(minutes) or minutes <= 0:
            raise LibraryError("Minutes must be finite and positive")
        if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count <= 0):
            raise LibraryError("Count must be a positive integer")
        if start_episode is not None and (isinstance(start_episode, bool) or not isinstance(start_episode, int) or start_episode < 0):
            raise LibraryError("Start episode must be a nonnegative integer")
        records = self._records(self._discover())
        selected = self._select_playable(records, series)
        candidates = [record for record in records if record["series"] == selected]
        excluded = set(exclude_paths)
        if start_episode is not None:
            starts = [i for i, record in enumerate(candidates) if record["episode"] == start_episode]
            if len(starts) != 1:
                raise LibraryError("Start episode must identify exactly one existing file")
            candidates = candidates[starts[0]:]
            if candidates[0]["path"] in excluded:
                raise LibraryError("Explicit start episode is already playing or queued")
        else:
            candidates = [record for record in candidates if not record["played"]]
        candidates = [record for record in candidates if record["path"] not in excluded]
        files = []
        total = 0.0
        blocked = None
        for record in candidates:
            if count is not None and len(files) == count:
                break
            seconds = self.duration(record["path"])
            item = {"path": record["path"], "episode": record["episode"], "duration_seconds": seconds}
            if total + seconds > minutes * 60:
                blocked = item
                break
            files.append(item)
            total += seconds
        if not files:
            raise LibraryError("No whole story fits the request; choose a larger budget or another episode")
        satisfied = count is None or len(files) == count
        return {"series": selected, "files": files, "story_seconds": total, "minutes": minutes,
                "requested_count": count, "count_satisfied": satisfied,
                "requires_confirmation": not satisfied, "blocked_next": blocked}

    def mark_played(self, series: str, start_episode: int, end_episode: int | None = None,
                    played: bool = True) -> dict:
        end_episode = start_episode if end_episode is None else end_episode
        if (isinstance(start_episode, bool) or not isinstance(start_episode, int) or start_episode < 0
                or isinstance(end_episode, bool) or not isinstance(end_episode, int) or end_episode < start_episode):
            raise LibraryError("Episode range must be nonnegative and inclusive in increasing order")
        if not isinstance(played, bool):
            raise LibraryError("Played must be a boolean")
        self.scan()
        records = self._records(self._discover())
        selected = self._select_playable(records, series)
        candidates = [record for record in records if record["series"] == selected]
        episodes = {record["episode"] for record in candidates}
        if start_episode not in episodes or end_episode not in episodes:
            raise LibraryError("Both range endpoints must match existing episodes in the selected series")
        paths = [record["path"] for record in candidates if record["episode"] is not None
                 and start_episode <= record["episode"] <= end_episode]
        timestamp = datetime.now(timezone.utc).isoformat() if played else None
        with self._lock:
            self._ensure_open()
            try:
                with self._db:
                    self._db.executemany("UPDATE files SET played = ?, played_at = ? WHERE path = ?",
                                         ((int(played), timestamp, path) for path in paths))
            except sqlite3.Error as exc:
                raise LibraryError(f"Cannot mark episodes: {exc}") from exc
        return {"series": selected, "paths": paths, "updated": len(paths), "played": played}

    def complete(self, path: str) -> None:
        self.resolve(path)
        if path == _SLEEP:
            raise LibraryError("Sleep music is not tracked as an ordinary story")
        with self._lock:
            self._ensure_open()
            try:
                with self._db:
                    self._db.execute('''INSERT INTO files(path, played, played_at, play_count)
                        VALUES (?, 1, ?, 1) ON CONFLICT(path) DO UPDATE SET
                        played = 1, played_at = excluded.played_at,
                        play_count = files.play_count + 1, "exists" = 1''',
                                     (path, datetime.now(timezone.utc).isoformat()))
            except sqlite3.Error as exc:
                raise LibraryError(f"Cannot record completed story: {exc}") from exc

    def resolve(self, path: str) -> Path:
        self._validate_root()
        if not isinstance(path, str) or not path or "\0" in path:
            raise LibraryError("Media path must be a nonempty relative path")
        relative = PurePosixPath(path)
        if (relative.is_absolute() or ".." in relative.parts or "\\" in path
                or relative.as_posix() != path or any(part.startswith(".") for part in relative.parts)):
            raise LibraryError("Media path must be canonical and below the source root")
        if path != _SLEEP and any(part.casefold() in {"system", "gillian aduio"} for part in relative.parts):
            raise LibraryError("This media directory is excluded")
        candidate = self.root
        for part in relative.parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise LibraryError("Symlink media paths are not allowed")
        try:
            candidate.resolve(strict=True).relative_to(self.root.resolve(strict=True))
            if not candidate.is_file() or candidate.suffix.casefold() not in _AUDIO:
                raise LibraryError("Media must be an existing supported regular file")
        except (OSError, ValueError) as exc:
            raise LibraryError(f"Media is unavailable or outside the source: {path}") from exc
        return candidate

    def duration(self, path: str) -> float:
        media = self.resolve(path)
        try:
            result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                     "-of", "default=noprint_wrappers=1:nokey=1", str(media)],
                                    capture_output=True, text=True, timeout=20, check=True)
            seconds = float(result.stdout.strip())
            if not math.isfinite(seconds) or seconds <= 0:
                raise ValueError("nonpositive or nonfinite duration")
            return seconds
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise LibraryError(f"Cannot determine duration for {path}: {exc}") from exc

    def sleep_file(self) -> Path:
        return self.resolve(_SLEEP)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True
