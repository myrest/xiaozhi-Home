"""Behavioral checks for local story streaming through XiaoZhi's audio queue."""

from __future__ import annotations

import asyncio
import json
import queue
import shutil
import tempfile
import threading
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from core.providers.tts.base import TTSProviderBase
from core.providers.tts.dto.dto import SentenceType, StoryPlaybackMessage
from plugins_func.functions.play_story import StoryPlayback, StoryPlaybackError, _resolve_media
from plugins_func.register import Action


class _Logger:
    def bind(self, **_kwargs):
        return self

    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class _WebSocket:
    def __init__(self):
        self.frames = []

    async def send(self, frame):
        self.frames.append(frame)


class _TTS:
    def __init__(self, conn):
        self.conn = conn
        self.tts_text_queue = queue.Queue()
        self.tts_audio_queue = queue.Queue()
        self.tts_audio_first_sentence = True
        self.report_on_last = False

    def audio_worker(self):
        TTSProviderBase._audio_play_priority_thread(self)


class _Connection:
    def __init__(self, loop):
        self.loop = loop
        self.logger = _Logger()
        self.websocket = _WebSocket()
        self.tts = _TTS(self)
        self.stop_event = threading.Event()
        self.client_abort = False
        self.calling = False
        self.close_after_chat = False
        self.conn_from_mqtt_gateway = False
        self.max_output_size = 0
        self.headers = {}
        self.last_activity_time = 0
        self.config = {"tts_audio_send_delay": 0, "enable_stop_tts_notify": False}
        self.session_id = "story-test-session"
        self.sentence_id = "story-test-sentence"
        self.intent_type = "function_call"
        self.mcp_endpoint_client = object()

    def clearSpeakStatus(self):
        pass


class StoryPlaybackTests(IsolatedAsyncioTestCase):
    def test_media_paths_reject_traversal_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "media"
            story = root / "Stories" / "1.wav"
            story.parent.mkdir(parents=True)
            story.write_bytes(b"fixture")
            outside = Path(temporary) / "outside.wav"
            outside.write_bytes(b"fixture")
            (story.parent / "linked.wav").symlink_to(outside)

            self.assertEqual(_resolve_media(root, "Stories/1.wav"), story.resolve())
            for unsafe in ("../outside.wav", "Stories/linked.wav", "/etc/passwd"):
                with self.subTest(path=unsafe), self.assertRaises(StoryPlaybackError):
                    _resolve_media(root, unsafe)

    async def test_internal_completion_tool_is_not_exposed_or_invocable(self):
        from core.providers.tools.mcp_endpoint.mcp_endpoint_executor import (
            MCPEndpointExecutor,
        )

        conn = _Connection(asyncio.get_running_loop())
        available_tools = [
            {"function": {"name": "bedtime_complete"}},
            {"function": {"name": "bedtime_library"}},
        ]
        conn.mcp_endpoint_client = SimpleNamespace(
            get_available_tools=lambda: available_tools,
            has_tool=lambda name: name in {"bedtime_complete", "bedtime_library"},
        )
        executor = MCPEndpointExecutor(conn)

        self.assertNotIn("bedtime_complete", executor.get_tools())
        self.assertIn("bedtime_library", executor.get_tools())
        self.assertFalse(executor.has_tool("bedtime_complete"))
        result = await executor.execute(
            conn, "bedtime_complete", {"path": "Stories/1.wav"}
        )
        self.assertIs(result.action, Action.NOTFOUND)

    def test_clear_queues_marks_discarded_audio_complete(self):
        from core.connection import ConnectionHandler

        conn = object.__new__(ConnectionHandler)
        conn.tts = SimpleNamespace(
            tts_text_queue=queue.Queue(),
            tts_audio_queue=queue.Queue(),
        )
        conn.report_queue = queue.Queue()
        conn.logger = _Logger()
        conn.tts.tts_audio_queue.put((SentenceType.MIDDLE, b"audio", None))

        conn.clear_queues()

        self.assertEqual(conn.tts.tts_audio_queue.unfinished_tasks, 0)

    def test_conflicting_music_and_story_tool_calls_are_rejected(self):
        from core.connection import _has_conflicting_audio_tools

        def calls(*names):
            return [{"name": name} for name in names]

        conflicts = (
            ("play_music", "play_story"),
            ("play_music", "control_story"),
            ("play_story", "control_story"),
        )
        for names in conflicts:
            with self.subTest(names=names):
                self.assertTrue(
                    _has_conflicting_audio_tools(calls(*names))
                )

        compatible = (
            ("play_music",),
            ("play_story",),
            ("play_music", "play_music"),
        )
        for names in compatible:
            with self.subTest(names=names):
                self.assertFalse(
                    _has_conflicting_audio_tools(calls(*names))
                )

    async def test_story_frames_drain_before_completion_is_recorded(self):
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg is required for the streaming integration check")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "media"
            media = root / "Stories" / "1 first.wav"
            media.parent.mkdir(parents=True)
            with wave.open(str(media), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(bytes(16000 * 2 * 24 // 100))

            conn = _Connection(asyncio.get_running_loop())
            conn.audio_format = "opus"
            conn.sample_rate = 24000
            player = StoryPlayback(conn, root)
            calls = []
            drained_packet_counts = []

            async def fake_mcp_call(_client, name, arguments):
                calls.append((name, arguments))
                if name == "bedtime_plan":
                    return json.dumps(
                        {
                            "files": [
                                {
                                    "path": "Stories/1 first.wav",
                                    "duration_seconds": 0.24,
                                }
                            ]
                        }
                    )
                if name == "bedtime_complete":
                    self.assertTrue(drained_packet_counts)
                    self.assertEqual(conn.audio_flow_control["packet_count"], 4)
                    return json.dumps({"completed": True})
                raise AssertionError(f"unexpected MCP tool: {name}")

            real_wait = player._wait_file_completion

            def tracked_wait():
                real_wait()
                drained_packet_counts.append(conn.audio_flow_control["packet_count"])

            player._wait_file_completion = tracked_wait
            audio_thread = threading.Thread(target=conn.tts.audio_worker, daemon=True)
            with patch(
                "plugins_func.functions.play_story.call_mcp_endpoint_tool",
                side_effect=fake_mcp_call,
            ), patch("core.providers.tts.base.enqueue_tts_report"):
                audio_thread.start()
                try:
                    result = await player.play(
                        series="Stories", count=1, bedtime=False
                    )
                    self.assertEqual(result.action, Action.RECORD)
                    message = conn.tts.tts_text_queue.get_nowait()
                    self.assertIsInstance(message, StoryPlaybackMessage)
                    await asyncio.to_thread(
                        player.process_tts_message,
                        conn.tts,
                        message.generation,
                        message.sentence_id,
                    )

                    binary_frames = [
                        frame for frame in conn.websocket.frames if isinstance(frame, bytes)
                    ]
                    self.assertEqual(len(binary_frames), 4)
                    self.assertEqual(
                        calls[0],
                        (
                            "bedtime_plan",
                            {
                                "series": "Stories",
                                "minutes": 30,
                                "bedtime": False,
                                "count": 1,
                            },
                        ),
                    )
                    self.assertEqual(
                        calls[1],
                        ("bedtime_complete", {"path": "Stories/1 first.wav"}),
                    )
                    self.assertEqual(drained_packet_counts, [4])
                    self.assertEqual(player.snapshot()["state"], "idle")
                finally:
                    conn.tts.tts_audio_queue.put(
                        (SentenceType.LAST, [], None, conn.sentence_id)
                    )
                    await asyncio.to_thread(audio_thread.join, 2)
                    conn.stop_event.set()
                    if getattr(conn, "audio_rate_controller", None):
                        conn.audio_rate_controller.stop_sending()
                    player.close()

    async def test_interruption_pauses_at_estimated_position_until_explicit_resume(self):
        conn = _Connection(asyncio.get_running_loop())
        conn.audio_flow_control = {"sentence_id": "old-sentence", "packet_count": 110}
        conn.audio_rate_controller = SimpleNamespace(interval_ms=60)
        conn.sentence_id = "new-sentence"
        player = StoryPlayback(conn, Path("/unused"))
        player._state = "playing"
        player._files = [
            {"path": "Stories/1.wav", "duration_seconds": 30.0, "sleep": False}
        ]
        player._current = player._files[0]
        player._cursor = 0
        player._offset = 2.0
        player._file_start_packet_count = 10
        player._sentence_id = "old-sentence"

        player.interrupt()
        self.assertEqual(player.snapshot()["state"], "paused")
        self.assertAlmostEqual(player.snapshot()["offset_seconds"], 7.6)

        paused = await player.control("pause")
        self.assertEqual(paused.action, Action.RESPONSE)
        self.assertEqual(player.snapshot()["state"], "paused")
        self.assertTrue(conn.tts.tts_text_queue.empty())

        resumed = await player.control("resume")
        self.assertEqual(resumed.action, Action.RECORD)
        self.assertEqual(player.snapshot()["state"], "playing")
        self.assertIsInstance(conn.tts.tts_text_queue.get_nowait(), StoryPlaybackMessage)
        player.close()
