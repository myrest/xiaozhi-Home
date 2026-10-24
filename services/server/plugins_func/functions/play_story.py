"""Play local bedtime stories through the connected XiaoZhi device."""

from __future__ import annotations

import asyncio
import json
import math
import os
import subprocess
import threading
from pathlib import Path, PurePosixPath
from typing import Any

from core.handle.sendAudioHandle import PRE_BUFFER_COUNT, send_tts_message
from core.providers.tools.mcp_endpoint.mcp_endpoint_handler import call_mcp_endpoint_tool
from core.utils import opus_encoder_utils
from core.providers.tts.dto.dto import (
    ContentType,
    SentenceType,
    StoryPlaybackMessage,
    TTSMessageDTO,
)
from plugins_func.register import Action, ActionResponse, ToolType, register_function

TAG = __name__
_AUDIO_EXTENSIONS = frozenset(
    {".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".opus", ".wma"}
)
_SLEEP_PATH = "System/睡覺音樂.m4a"
_FRAME_BATCH = 64

play_story_function_desc = {
    "type": "function",
    "function": {
        "name": "play_story",
        "description": (
            "播放故事資料夾中的本機音檔時呼叫；先用 bedtime_library 確認有多層目錄的系列，再選實際子目錄。"
            "不要用於歌曲（歌曲使用 play_music），也不要把「講個故事」的生成式故事誤當成播放檔案。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "series": {"type": "string", "description": "故事系列或實際子目錄名稱/相對路徑"},
                "minutes": {"type": "number", "exclusiveMinimum": 0},
                "count": {"type": "integer", "minimum": 1},
                "start_episode": {"type": "integer", "minimum": 0},
                "bedtime": {"type": "boolean", "description": "故事後播放睡覺音樂；省略時為 true"},
                "sleep_volume": {"type": "integer", "minimum": 0, "maximum": 100},
                "mode": {"type": "string", "enum": ["start", "append", "replace"]},
            },
            "additionalProperties": False,
        },
    },
}

control_story_function_desc = {
    "type": "function",
    "function": {
        "name": "control_story",
        "description": "暫停、明確繼續、停止或查詢本裝置上的故事播放；只有使用者明確要求繼續時才恢復。",
        "parameters": {
            "type": "object",
            "properties": {"action": {"type": "string", "enum": ["pause", "resume", "stop", "status"]}},
            "required": ["action"],
            "additionalProperties": False,
        },
    },
}


class StoryPlaybackError(ValueError):
    """A story request cannot be fulfilled safely."""


class _Interrupted(Exception):
    """Internal signal for a user or connection interruption."""


def _resolve_media(root: Path, relative_path: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path or "\0" in relative_path:
        raise StoryPlaybackError("故事檔案路徑無效")
    relative = PurePosixPath(relative_path)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or "\\" in relative_path
        or relative.as_posix() != relative_path
        or any(part.startswith(".") for part in relative.parts)
    ):
        raise StoryPlaybackError("故事檔案必須位於故事資料夾內")
    if relative_path != _SLEEP_PATH and any(
        part.casefold() in {"system", "gillian aduio"} for part in relative.parts
    ):
        raise StoryPlaybackError("不能播放系統排除的音檔")
    if relative.suffix.casefold() not in _AUDIO_EXTENSIONS:
        raise StoryPlaybackError("不支援這種故事音檔格式")

    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise StoryPlaybackError("故事資料夾目前無法讀取")
    resolved_root = root.resolve(strict=True)
    candidate = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise StoryPlaybackError("故事檔案不能經由符號連結存取")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise StoryPlaybackError("故事檔案不存在或超出故事資料夾") from exc
    if not resolved.is_file():
        raise StoryPlaybackError("故事檔案不存在")
    return resolved


def _positive_duration(value: Any, *, optional: bool = False) -> float | None:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StoryPlaybackError("故事播放計畫中的音檔長度無效")
    duration = float(value)
    if not math.isfinite(duration) or duration <= 0:
        raise StoryPlaybackError("故事播放計畫中的音檔長度無效")
    return duration


class StoryPlayback:
    """One connection's bounded story stream and interrupt/resume state."""

    def __init__(self, conn, root: Path, ffmpeg: str = "ffmpeg"):
        self.conn = conn
        self.root = Path(root)
        self.ffmpeg = ffmpeg
        self._lock = threading.RLock()
        self._command_lock = asyncio.Lock()
        self._generation = 0
        self._closed = False
        self._state = "idle"
        self._files: list[dict[str, Any]] = []
        self._cursor = 0
        self._sleep_tail: dict[str, Any] | None = None
        self._sleep_finished = False
        self._sleep_volume = 50
        self._current: dict[str, Any] | None = None
        self._offset = 0.0
        self._file_start_packet_count = 0
        self._sentence_id: str | None = None
        self._process: subprocess.Popen | None = None
        self._interrupted_by_voice = False
        self._opus_encoder = None
        self._last_error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            queued = [item["path"] for item in self._files[self._cursor :]]
            if self._sleep_tail is not None and not self._sleep_finished:
                queued.append(self._sleep_tail["path"])
            return {
                "state": self._state,
                "current": self._current["path"] if self._current else None,
                "offset_seconds": round(self._offset, 1),
                "queued": queued,
                "sleep_pending": self._sleep_tail is not None and not self._sleep_finished,
                "last_error": self._last_error,
            }

    async def _call_mcp(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        client = getattr(self.conn, "mcp_endpoint_client", None)
        if client is None:
            raise StoryPlaybackError("故事資料服務尚未連線")
        try:
            result = await call_mcp_endpoint_tool(client, tool_name, arguments)
            return json.loads(result) if isinstance(result, str) else result
        except Exception as exc:
            self.conn.logger.bind(tag=TAG).warning(
                f"故事 MCP 工具 {tool_name} 呼叫失敗：{type(exc).__name__}"
            )
            raise StoryPlaybackError("無法取得故事播放資料，請稍後再試") from exc

    def _plan_items(self, plan: Any, bedtime: bool) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        if not isinstance(plan, dict):
            raise StoryPlaybackError("故事播放計畫格式無效")
        if plan.get("requires_confirmation"):
            count = len(plan.get("files", [])) if isinstance(plan.get("files"), list) else 0
            raise StoryPlaybackError(
                f"目前只有 {count} 集符合條件；請重新要求播放這 {count} 集，或增加播放時間。"
            )
        raw_files = plan.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            raise StoryPlaybackError("找不到符合條件的故事音檔")
        files = []
        for item in raw_files:
            if not isinstance(item, dict):
                raise StoryPlaybackError("故事播放計畫中的檔案格式無效")
            relative = item.get("path")
            media_path = _resolve_media(self.root, relative)
            duration = _positive_duration(item.get("duration_seconds"))
            files.append(
                {
                    "path": relative,
                    "media_path": media_path,
                    "duration_seconds": duration,
                    "episode": item.get("episode"),
                    "sleep": False,
                }
            )

        sleep_tail = None
        if bedtime:
            raw_sleep = plan.get("sleep_file")
            if isinstance(raw_sleep, str):
                raw_sleep = {"path": raw_sleep}
            if not isinstance(raw_sleep, dict):
                raise StoryPlaybackError("睡覺音樂目前無法讀取")
            relative = raw_sleep.get("path")
            sleep_path = _resolve_media(self.root, relative)
            sleep_duration = _positive_duration(raw_sleep.get("duration_seconds"), optional=True)
            sleep_tail = {
                "path": relative,
                "media_path": sleep_path,
                "duration_seconds": sleep_duration,
                "episode": None,
                "sleep": True,
            }
        return files, sleep_tail

    def _clear_output(self) -> None:
        if hasattr(self.conn, "clear_queues"):
            self.conn.clear_queues()

    def _terminate_process(self, process: subprocess.Popen | None) -> None:
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
        except OSError:
            pass

    def _stop_locked(self) -> bool:
        should_send_stop = self._state == "playing" and not self._interrupted_by_voice
        self._generation += 1
        process = self._process
        self._state = "idle"
        self._files = []
        self._cursor = 0
        self._sleep_tail = None
        self._sleep_finished = False
        self._current = None
        self._offset = 0.0
        self._process = None
        self._last_error = None
        self._interrupted_by_voice = False
        self._terminate_process(process)
        self._clear_output()
        return should_send_stop

    async def play(
        self,
        series: str | None = None,
        minutes: float = 30,
        count: int | None = None,
        start_episode: int | None = None,
        bedtime: bool = True,
        sleep_volume: int = 50,
        mode: str = "start",
    ) -> ActionResponse:
        if mode not in {"start", "append", "replace"}:
            return ActionResponse(action=Action.RESPONSE, response="播放方式必須是 start、append 或 replace。")
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not math.isfinite(minutes) or minutes <= 0:
            return ActionResponse(action=Action.RESPONSE, response="播放時間必須是正數。")
        if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count <= 0):
            return ActionResponse(action=Action.RESPONSE, response="集數必須是正整數。")
        if start_episode is not None and (isinstance(start_episode, bool) or not isinstance(start_episode, int) or start_episode < 0):
            return ActionResponse(action=Action.RESPONSE, response="起始集數無效。")
        if not isinstance(bedtime, bool):
            return ActionResponse(action=Action.RESPONSE, response="睡前模式設定無效。")
        if isinstance(sleep_volume, bool) or not isinstance(sleep_volume, int) or not 0 <= sleep_volume <= 100:
            return ActionResponse(action=Action.RESPONSE, response="睡覺音樂音量必須是 0 到 100。")
        if series is not None and not isinstance(series, str):
            return ActionResponse(action=Action.RESPONSE, response="故事系列名稱無效。")

        async with self._command_lock:
            with self._lock:
                active = self._state in {"playing", "paused"}
                state = self._state
                if active and mode == "start":
                    message = "故事正在播放；若要恢復目前故事，請說「繼續故事」，若要換故事請指定 replace。" if state == "playing" else "故事已暫停；請說「繼續故事」恢復，或指定 replace 播放新故事。"
                    return ActionResponse(action=Action.RESPONSE, response=message)
                if active and mode == "append" and self._current and self._current.get("sleep"):
                    return ActionResponse(action=Action.RESPONSE, response="睡覺音樂已開始，不能再追加故事；請先停止或更換播放計畫。")
                should_send_stop = self._stop_locked() if mode == "replace" else False
                excluded = [item["path"] for item in self._files[self._cursor :]] if active and mode == "append" else []
                sentence_id = getattr(self.conn, "sentence_id", None)

            if should_send_stop:
                await send_tts_message(self.conn, "stop")

            request: dict[str, Any] = {"series": series, "minutes": minutes, "bedtime": bedtime}
            if count is not None:
                request["count"] = count
            if start_episode is not None:
                request["start_episode"] = start_episode
            if excluded:
                request["exclude_paths"] = excluded
            try:
                plan = await self._call_mcp("bedtime_plan", request)
                files, sleep_tail = self._plan_items(plan, bedtime)
            except StoryPlaybackError as exc:
                return ActionResponse(action=Action.RESPONSE, response=str(exc))

            if getattr(self.conn, "client_abort", False) or sentence_id != getattr(self.conn, "sentence_id", None):
                return ActionResponse(action=Action.RECORD, result="故事播放請求已取消")

            with self._lock:
                if self._closed:
                    return ActionResponse(action=Action.ERROR, response="故事播放連線已關閉。")
                active_now = self._state in {"playing", "paused"}
                if mode == "append" and active_now:
                    self._files.extend(files)
                    if bedtime and self._sleep_tail is None:
                        self._sleep_tail = sleep_tail
                        self._sleep_volume = sleep_volume
                    self._last_error = None
                    return ActionResponse(action=Action.RECORD, result="故事已加入播放佇列")

                self._generation += 1
                generation = self._generation
                self._state = "playing"
                self._files = files
                self._cursor = 0
                self._sleep_tail = sleep_tail
                self._sleep_finished = False
                self._sleep_volume = sleep_volume
                self._current = None
                self._offset = 0.0
                self._file_start_packet_count = 0
                self._sentence_id = sentence_id
                self._last_error = None
                self._interrupted_by_voice = False
                self._enqueue(sentence_id, generation)
            return ActionResponse(action=Action.RECORD, result="故事播放已排入裝置音訊管線")

    def _enqueue(self, sentence_id: str, generation: int) -> None:
        self.conn.tts.tts_text_queue.put(
            StoryPlaybackMessage(sentence_id=sentence_id, playback=self, generation=generation)
        )

    async def control(self, action: str) -> ActionResponse:
        if action not in {"pause", "resume", "stop", "status"}:
            return ActionResponse(action=Action.RESPONSE, response="故事控制動作必須是 pause、resume、stop 或 status。")
        if action == "status":
            return ActionResponse(action=Action.RESPONSE, result=json.dumps(self.snapshot(), ensure_ascii=False), response=self._status_text())

        async with self._command_lock:
            if action == "pause":
                with self._lock:
                    if self._state == "paused":
                        return ActionResponse(action=Action.RESPONSE, response="故事已暫停；說「繼續故事」可從接近剛才的位置恢復。")
                    if self._state != "playing":
                        return ActionResponse(action=Action.RESPONSE, response="目前沒有正在播放的故事。")
                    self._capture_progress_locked()
                    should_send_stop = not self._interrupted_by_voice
                    self._generation += 1
                    process = self._process
                    self._process = None
                    self._state = "paused"
                    self._interrupted_by_voice = False
                    self._terminate_process(process)
                    self._clear_output()
                if should_send_stop:
                    await send_tts_message(self.conn, "stop")
                return ActionResponse(action=Action.RESPONSE, response="故事已暫停；說「繼續故事」可從接近剛才的位置恢復。")

            if action == "stop":
                with self._lock:
                    active = self._state in {"playing", "paused"}
                    if not active:
                        return ActionResponse(action=Action.RESPONSE, response="目前沒有可停止的故事。")
                    should_send_stop = self._stop_locked()
                if should_send_stop:
                    await send_tts_message(self.conn, "stop")
                return ActionResponse(action=Action.RESPONSE, response="故事播放已停止。")

            with self._lock:
                if self._state != "paused" or self._cursor >= len(self._files) and (self._sleep_tail is None or self._sleep_finished):
                    return ActionResponse(action=Action.RESPONSE, response="目前沒有可繼續的故事。")
                if self._closed:
                    return ActionResponse(action=Action.ERROR, response="故事播放連線已關閉。")
                self._generation += 1
                generation = self._generation
                sentence_id = getattr(self.conn, "sentence_id", None)
                self._sentence_id = sentence_id
                self._state = "playing"
                self._interrupted_by_voice = False
                self._enqueue(sentence_id, generation)
            return ActionResponse(action=Action.RECORD, result="依使用者要求恢復故事播放")

    def _status_text(self) -> str:
        snapshot = self.snapshot()
        if snapshot["state"] == "playing":
            current = Path(snapshot["current"]).stem if snapshot["current"] else "故事"
            return f"正在播放《{current}》。"
        if snapshot["state"] == "paused":
            return f"故事已暫停，估計位置約 {snapshot['offset_seconds']:.1f} 秒；說「繼續故事」可恢復。"
        if snapshot["state"] == "error":
            return "故事播放遇到錯誤，請停止後重新播放。"
        return "目前沒有正在播放的故事。"

    def interrupt(self) -> None:
        """Capture sent progress before ConnectionHandler.clear_queues resets it."""
        with self._lock:
            if self._state != "playing":
                return
            self._capture_progress_locked()
            self._generation += 1
            self._state = "paused"
            self._interrupted_by_voice = True
            process = self._process
            self._process = None
            self._terminate_process(process)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._generation += 1
            process = self._process
            self._process = None
            self._state = "idle"
            self._files.clear()
            self._cursor = 0
            self._sleep_tail = None
            self._current = None
            self._offset = 0.0
            self._terminate_process(process)

    def _capture_progress_locked(self) -> None:
        if self._current is None:
            return
        flow = getattr(self.conn, "audio_flow_control", {})
        if not isinstance(flow, dict) or flow.get("sentence_id") != self._sentence_id:
            return
        controller = getattr(self.conn, "audio_rate_controller", None)
        interval_ms = getattr(controller, "interval_ms", 60)
        sent_packets = max(0, int(flow.get("packet_count", 0)) - self._file_start_packet_count)
        send_delay_ms = self.conn.config.get("tts_audio_send_delay", 0)
        buffered_seconds = (
            2 * interval_ms / 1000.0
            if send_delay_ms > 0
            else (PRE_BUFFER_COUNT + 2) * interval_ms / 1000.0
        )
        progress = max(0.0, sent_packets * interval_ms / 1000.0 - buffered_seconds)
        duration = self._current.get("duration_seconds")
        if duration is not None:
            progress = min(progress, max(0.0, duration - 0.06))
        self._offset = min(float(duration) if duration is not None else math.inf, self._offset + progress)

    def _is_current(self, generation: int, sentence_id: str) -> bool:
        stop_event = getattr(self.conn, "stop_event", None)
        with self._lock:
            return (
                not self._closed
                and self._generation == generation
                and self._state == "playing"
                and not getattr(self.conn, "client_abort", False)
                and not (stop_event is not None and stop_event.is_set())
                and getattr(self.conn, "sentence_id", None) == sentence_id
            )

    def _flow_packet_count(self, sentence_id: str) -> int:
        flow = getattr(self.conn, "audio_flow_control", {})
        if isinstance(flow, dict) and flow.get("sentence_id") == sentence_id:
            return int(flow.get("packet_count", 0))
        return 0

    async def _wait_tts_audio_queue(self) -> None:
        tts = getattr(self.conn, "tts", None)
        audio_queue = getattr(tts, "tts_audio_queue", None)
        if audio_queue is None:
            return
        while audio_queue.unfinished_tasks:
            if getattr(self.conn, "client_abort", False):
                return
            await asyncio.sleep(0.01)

    async def _wait_audio_output(self, include_device_buffer: bool) -> None:
        await self._wait_tts_audio_queue()
        await self._wait_audio_queue(include_device_buffer)

    async def _wait_audio_queue(self, include_device_buffer: bool) -> None:
        controller = getattr(self.conn, "audio_rate_controller", None)
        if controller is None:
            return
        if not controller.queue_empty_event.is_set():
            pending = getattr(controller, "pending_send_task", None)
            if pending is None or pending.done():
                raise RuntimeError("device audio sender stopped before draining its queue")
            interval = max(1, int(getattr(controller, "interval_ms", 60)))
            timeout = max(5.0, (len(controller.queue) + 5) * interval / 1000.0 + 5.0)
            try:
                await asyncio.wait_for(controller.queue_empty_event.wait(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("device audio queue did not drain") from exc
        if include_device_buffer and not getattr(self.conn, "client_abort", False):
            interval = getattr(controller, "interval_ms", 60)
            if self.conn.config.get("tts_audio_send_delay", 0) > 0:
                buffer_seconds = 2 * interval / 1000.0
            else:
                buffer_seconds = (PRE_BUFFER_COUNT + 2) * interval / 1000.0
            await asyncio.sleep(buffer_seconds)

    def _queue_wait_timeout(self) -> float:
        controller = getattr(self.conn, "audio_rate_controller", None)
        interval = max(1, int(getattr(controller, "interval_ms", 60)))
        tts = getattr(self.conn, "tts", None)
        audio_queue = getattr(tts, "tts_audio_queue", None)
        queued_frames = audio_queue.qsize() if audio_queue is not None else 0
        rate_frames = len(controller.queue) if controller is not None else 0
        frames = queued_frames + rate_frames + PRE_BUFFER_COUNT + 10
        return max(30.0, frames * interval / 1000.0 + 10.0)

    def _run_on_loop(self, coroutine, timeout: float = 30.0) -> Any:
        future = asyncio.run_coroutine_threadsafe(coroutine, self.conn.loop)
        try:
            return future.result(timeout=timeout)
        except Exception:
            future.cancel()
            raise

    def _drain_rate_queue(self) -> None:
        self._run_on_loop(
            self._wait_audio_output(include_device_buffer=False),
            timeout=self._queue_wait_timeout(),
        )

    def _wait_file_completion(self) -> None:
        self._run_on_loop(
            self._wait_audio_output(include_device_buffer=True),
            timeout=self._queue_wait_timeout(),
        )

    def _send_packet(
        self,
        packet: bytes,
        generation: int,
        sentence_id: str,
        first: bool,
        title: str,
    ) -> None:
        if not self._is_current(generation, sentence_id):
            raise _Interrupted
        kind = SentenceType.FIRST if first else SentenceType.MIDDLE
        text = title if first else None
        self.conn.tts.tts_audio_queue.put(
            (kind, packet, text, sentence_id)
        )

    def _stream_file(
        self,
        item: dict[str, Any],
        offset_seconds: float,
        generation: int,
        sentence_id: str,
        first_packet: list[bool],
    ) -> int:
        args = [self.ffmpeg, "-nostdin", "-v", "error"]
        if offset_seconds > 0:
            args.extend(["-ss", f"{offset_seconds:.3f}"])
        args.extend(["-i", str(item["media_path"])])
        if item.get("sleep") and self._sleep_volume < 100:
            args.extend(["-af", f"volume={self._sleep_volume / 100:g}"])
        args.extend(
            ["-map", "0:a:0", "-vn", "-f", "s16le", "-ac", "1", "-ar", str(self.conn.sample_rate), "pipe:1"]
        )
        if self.conn.audio_format == "pcm":
            encoder = None
        else:
            if self._opus_encoder is None:
                self._opus_encoder = opus_encoder_utils.OpusEncoderUtils(
                    sample_rate=self.conn.sample_rate,
                    channels=1,
                    frame_size_ms=60,
                )
            encoder = self._opus_encoder
        if encoder is not None:
            encoder.reset_state()
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        with self._lock:
            self._process = process
            if self._generation != generation or self._state != "playing":
                self._terminate_process(process)
                return 0

        frame_bytes = self.conn.sample_rate * 60 // 1000 * 2
        buffered = bytearray()
        sent_frames = 0
        title = f"正在播放：{Path(item['path']).stem}"

        try:
            assert process.stdout is not None
            while True:
                if not self._is_current(generation, sentence_id):
                    raise _Interrupted
                chunk = process.stdout.read(frame_bytes * 8)
                if not chunk:
                    break
                buffered.extend(chunk)
                while len(buffered) >= frame_bytes:
                    pcm = bytes(buffered[:frame_bytes])
                    del buffered[:frame_bytes]
                    if not self._is_current(generation, sentence_id):
                        raise _Interrupted
                    if encoder is None:
                        packets = [pcm]
                    else:
                        packets = []
                        encoder.encode_pcm_to_opus_stream(
                            pcm, end_of_stream=False, callback=packets.append
                        )
                    for packet in packets:
                        self._send_packet(
                            packet, generation, sentence_id, first_packet[0], title
                        )
                        first_packet[0] = False
                        sent_frames += 1
                        if sent_frames % _FRAME_BATCH == 0:
                            self._drain_rate_queue()
                if not self._is_current(generation, sentence_id):
                    raise _Interrupted

            if buffered and self._is_current(generation, sentence_id):
                pcm = bytes(buffered) + bytes(frame_bytes - len(buffered))
                packets = [pcm] if encoder is None else []
                if encoder is not None:
                    encoder.encode_pcm_to_opus_stream(
                        pcm, end_of_stream=False, callback=packets.append
                    )
                for packet in packets:
                    self._send_packet(
                        packet, generation, sentence_id, first_packet[0], title
                    )
                    first_packet[0] = False
                    sent_frames += 1
            return_code = process.wait(timeout=10)
            if not self._is_current(generation, sentence_id):
                raise _Interrupted
            if return_code != 0:
                raise RuntimeError("ffmpeg could not decode the story file")
            return sent_frames
        finally:
            try:
                if process.stdout:
                    process.stdout.close()
            except OSError:
                pass
            if process.poll() is None:
                self._terminate_process(process)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            with self._lock:
                if self._process is process:
                    self._process = None

    def process_tts_message(self, tts, generation: int, sentence_id: str) -> None:
        """Called synchronously by the TTS text worker, not the event loop."""
        tts.current_sentence_id = sentence_id
        tts.tts_audio_first_sentence = True
        tts.tts_text_buff = []
        tts.processed_chars = 0
        tts.is_first_sentence = True
        first_packet = [True]
        try:
            if not self._is_current(generation, sentence_id):
                return
            self._run_on_loop(
                self._wait_audio_output(include_device_buffer=True),
                timeout=self._queue_wait_timeout(),
            )
            if not self._is_current(generation, sentence_id):
                return
            while self._is_current(generation, sentence_id):
                with self._lock:
                    if self._cursor < len(self._files):
                        item = self._files[self._cursor]
                        index = self._cursor
                    elif self._sleep_tail is not None and not self._sleep_finished:
                        item = self._sleep_tail
                        index = None
                    else:
                        self._state = "idle"
                        self._current = None
                        self._offset = 0.0
                        if (
                            getattr(self.conn, "intent_type", None) == "intent_llm"
                            and not getattr(self.conn, "calling", False)
                        ):
                            self.conn.tts.tts_audio_queue.put(
                                (SentenceType.LAST, [], None, sentence_id)
                            )
                        return
                    self._current = item
                    self._offset = max(0.0, self._offset) if index == self._cursor else 0.0
                    offset = self._offset
                    self._sentence_id = sentence_id
                    self._file_start_packet_count = self._flow_packet_count(sentence_id)
                sent_frames = self._stream_file(
                    item, offset, generation, sentence_id, first_packet
                )
                if sent_frames <= 0:
                    raise RuntimeError("ffmpeg produced no audio frames")
                self._wait_file_completion()
                if not self._is_current(generation, sentence_id):
                    raise _Interrupted
                sent_packets = (
                    self._flow_packet_count(sentence_id)
                    - self._file_start_packet_count
                )
                if sent_packets != sent_frames:
                    raise RuntimeError("device audio sender did not send every story frame")
                if not item.get("sleep"):
                    self._record_completion(item["path"])
                with self._lock:
                    if self._generation != generation or self._state != "playing":
                        raise _Interrupted
                    if index is None:
                        self._sleep_finished = True
                    else:
                        self._cursor += 1
                    self._current = None
                    self._offset = 0.0
        except _Interrupted:
            return
        except Exception as exc:
            with self._lock:
                if self._generation != generation or self._state != "playing":
                    return
                self._state = "error"
                self._last_error = "故事音檔播放失敗"
                self._current = None
                self._process = None
            self.conn.logger.bind(tag=TAG).error(
                f"故事播放失敗：{type(exc).__name__}"
            )
            self._run_on_loop(self._announce_stream_error(sentence_id), timeout=10.0)

    async def _announce_stream_error(self, sentence_id: str) -> None:
        self._clear_output()
        self.conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=sentence_id,
                sentence_type=SentenceType.FIRST,
                content_type=ContentType.ACTION,
            )
        )
        self.conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=sentence_id,
                sentence_type=SentenceType.MIDDLE,
                content_type=ContentType.TEXT,
                content_detail="故事音檔播放遇到問題。",
            )
        )
        self.conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=sentence_id,
                sentence_type=SentenceType.LAST,
                content_type=ContentType.ACTION,
            )
        )

    def _record_completion(self, relative_path: str) -> None:
        try:
            result = self._run_on_loop(
                call_mcp_endpoint_tool(
                    self.conn.mcp_endpoint_client,
                    "bedtime_complete",
                    {"path": relative_path},
                ),
                timeout=35.0,
            )
            if isinstance(result, str):
                parsed = json.loads(result)
                if isinstance(parsed, dict) and parsed.get("error"):
                    raise RuntimeError("completion rejected")
        except Exception as exc:
            self.conn.logger.bind(tag=TAG).warning(
                f"故事已送出，但播放紀錄未能更新：{type(exc).__name__}"
            )


def _get_player(conn) -> StoryPlayback:
    player = getattr(conn, "story_playback", None)
    if player is None:
        player = StoryPlayback(
            conn,
            Path(os.environ.get("XIAOZHI_STORY_ROOT", "/mnt/cfcard")),
            os.environ.get("XIAOZHI_STORY_FFMPEG", "ffmpeg"),
        )
        conn.story_playback = player
    return player


@register_function("play_story", play_story_function_desc, ToolType.SYSTEM_CTL)
async def play_story(
    conn,
    series: str | None = None,
    minutes: float = 30,
    count: int | None = None,
    start_episode: int | None = None,
    bedtime: bool = True,
    sleep_volume: int = 50,
    mode: str = "start",
):
    """Find a story plan through MCP, then enqueue it on this device connection."""
    try:
        return await _get_player(conn).play(
            series=series,
            minutes=minutes,
            count=count,
            start_episode=start_episode,
            bedtime=bedtime,
            sleep_volume=sleep_volume,
            mode=mode,
        )
    except Exception as exc:
        conn.logger.bind(tag=TAG).error(f"處理故事播放請求失敗：{type(exc).__name__}")
        return ActionResponse(action=Action.ERROR, response="故事播放目前無法使用，請稍後再試。")


@register_function("control_story", control_story_function_desc, ToolType.SYSTEM_CTL)
async def control_story(conn, action: str):
    """Control only this device's story stream; resume is always explicit."""
    try:
        return await _get_player(conn).control(action)
    except Exception as exc:
        conn.logger.bind(tag=TAG).error(f"控制故事播放失敗：{type(exc).__name__}")
        return ActionResponse(action=Action.ERROR, response="無法控制目前的故事播放。")
