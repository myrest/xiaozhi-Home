"""Short-lived, device-scoped reminder audio assets."""

import asyncio
import hashlib
import hmac
import math
import os
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from aiohttp import web


_MAX_INPUT_BYTES = 12 * 1024 * 1024
_MAX_OUTPUT_BYTES = 4 * 1024 * 1024
_MAX_DURATION_SECONDS = 180
_MAX_ASSETS = 512


class AudioAssets:
    def __init__(self, config, public_base_url, root, *, clock=time.time):
        parsed = urlsplit(public_base_url)
        if (parsed.scheme not in ("https", "http") or not parsed.netloc or
                parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("public_base_url must be an HTTP(S) URL without credentials, query, or fragment")
        if parsed.scheme != "https" and not config.get("allow_insecure_audio_urls", False):
            raise ValueError("public_base_url must use HTTPS")
        secret = config.get("audio_signing_secret")
        if not isinstance(secret, (str, bytes)) or not secret:
            raise ValueError("audio_signing_secret is required")
        self._secret = secret.encode() if isinstance(secret, str) else secret
        self._config = config
        self._base_url = public_base_url.rstrip("/")
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._root, 0o700)
        self._clock = clock
        self._inflight = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @staticmethod
    def _safe_id(value):
        return isinstance(value, str) and 0 < len(value) <= 128 and all(c.isalnum() or c in "-_:" for c in value)

    def _signature(self, reminder_id, device_id, revision, expires_at):
        data = f"{reminder_id}\n{device_id}\n{revision}\n{expires_at}".encode()
        return hmac.new(self._secret, data, hashlib.sha256).hexdigest()

    def _asset_path(self, reminder_id, revision, expiry):
        return self._root / f"{reminder_id}-{revision}-{expiry}.ogg"

    def _valid_asset(self, path):
        try:
            if path.is_symlink() or path.resolve(strict=True).parent != self._root:
                return False
            size = path.stat().st_size
        except OSError:
            return False
        return path.is_file() and 0 < size <= _MAX_OUTPUT_BYTES

    def _asset_url(self, reminder_id, device_id, revision, expiry):
        signature = self._signature(reminder_id, device_id, revision, expiry)
        query = urlencode({"device": device_id, "expires": expiry, "signature": signature})
        return f"{self._base_url}/reminders/audio/{reminder_id}/{revision}.ogg?{query}"

    async def prepare(self, reminder_id, device_id, revision, text, expires_at):
        if self._closed:
            raise RuntimeError("audio asset service is closed")
        if not self._safe_id(reminder_id) or not self._safe_id(device_id):
            raise ValueError("invalid reminder or device id")
        if type(revision) is not int or revision < 0:
            raise ValueError("revision must be a nonnegative integer")
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            raise ValueError("text must contain 1 to 8000 characters")
        expiry = int(expires_at)
        if expiry <= self._clock():
            raise ValueError("reminder is expired")
        path = self._asset_path(reminder_id, revision, expiry)
        url = self._asset_url(reminder_id, device_id, revision, expiry)
        flight_key = (reminder_id, revision, device_id, expiry)
        async with self._lock:
            self._prune_locked(self._clock())
            if self._valid_asset(path):
                return url
            task = self._inflight.get(flight_key)
            if task is None:
                task = asyncio.create_task(
                    self._generate(reminder_id, device_id, revision, text, expiry)
                )
                self._inflight[flight_key] = task
        try:
            return await asyncio.shield(task)
        finally:
            async with self._lock:
                if self._inflight.get(flight_key) is task and task.done():
                    self._inflight.pop(flight_key, None)

    async def _generate(self, reminder_id, device_id, revision, text, expiry):
        from config.config_loader import get_private_config_from_api
        from core.providers.tts.base import TTSProviderBase
        from core.utils.modules_initialize import initialize_tts
        from core.utils.util import opus_datas_to_wav_bytes
        from types import SimpleNamespace

        raw_path = transcode_path = None
        output_path = self._asset_path(reminder_id, revision, expiry)
        installed = False
        try:
            if self._clock() >= expiry:
                raise ValueError("reminder expired before synthesis")
            private = await asyncio.wait_for(
                get_private_config_from_api(self._config, device_id, device_id),
                timeout=10,
            )
            merged = dict(self._config)
            merged.update(private)
            if not merged.get("selected_module", {}).get("TTS") or not merged.get("TTS"):
                raise RuntimeError("device TTS configuration is unavailable")
            merged["TTS"] = dict(merged["TTS"])
            module = merged["selected_module"]["TTS"]
            if module in private.get("TTS", {}):
                merged["TTS"][module] = private["TTS"][module]
            provider = initialize_tts(merged)
            provider.conn = SimpleNamespace(sample_rate=24000)
            stream_provider = type(provider).to_tts is not TTSProviderBase.to_tts
            suffix = ".wav" if stream_provider else "." + str(
                getattr(provider, "audio_file_type", "wav")
            ).lstrip(".")
            with tempfile.NamedTemporaryFile(
                dir=self._root, prefix=".tts-", suffix=suffix, delete=False
            ) as f:
                raw_path = Path(f.name)
            remaining_seconds = max(0.01, expiry - self._clock())
            if stream_provider:
                result = await asyncio.wait_for(
                    asyncio.to_thread(provider.to_tts, text),
                    timeout=min(
                        float(getattr(provider, "tts_timeout", 15)) * 5 + 5,
                        remaining_seconds,
                    ),
                )
                if isinstance(result, (list, tuple)):
                    if (
                        not result
                        or len(result) > _MAX_DURATION_SECONDS * 1000 // 60
                        or any(not isinstance(packet, (bytes, bytearray)) for packet in result)
                        or sum(map(len, result)) > _MAX_INPUT_BYTES
                    ):
                        raise RuntimeError("TTS provider produced invalid Opus audio")
                    audio = opus_datas_to_wav_bytes(result, sample_rate=24000, channels=1)
                    raw_path.write_bytes(audio)
                elif isinstance(result, (bytes, bytearray)) and result:
                    raw_path.write_bytes(result)
                elif isinstance(result, (str, os.PathLike)) and Path(result).is_file():
                    shutil.copyfile(result, raw_path)
                else:
                    raise RuntimeError("TTS provider produced no audio")
            else:
                result = await asyncio.wait_for(
                    provider.text_to_speak(text, str(raw_path)),
                    timeout=min(
                        float(getattr(provider, "tts_timeout", 15)) + 5,
                        remaining_seconds,
                    ),
                )
                if not raw_path.is_file() or raw_path.stat().st_size == 0:
                    if isinstance(result, (bytes, bytearray)) and result:
                        raw_path.write_bytes(result)
                    elif isinstance(result, (str, os.PathLike)) and Path(result).is_file():
                        shutil.copyfile(result, raw_path)
                    else:
                        raise RuntimeError("TTS provider produced no audio file or bytes")
            if self._clock() >= expiry:
                raise ValueError("reminder expired before audio transcoding")
            if raw_path.stat().st_size > _MAX_INPUT_BYTES:
                raise ValueError("TTS input exceeds audio size limit")
            transcode_path = self._root / (
                f".{reminder_id}-{revision}-{os.urandom(8).hex()}.tmp.ogg"
            )
            remaining_seconds = expiry - self._clock()
            if remaining_seconds <= 0:
                raise ValueError("reminder expired before audio transcoding")
            await asyncio.wait_for(
                self._transcode(raw_path, transcode_path),
                timeout=remaining_seconds,
            )
            if not transcode_path.is_file() or not 0 < transcode_path.stat().st_size <= _MAX_OUTPUT_BYTES:
                raise RuntimeError("transcoded audio is empty or exceeds size limit")
            os.chmod(transcode_path, 0o600)
            os.replace(transcode_path, output_path)
            transcode_path = None
            installed = True
            if self._clock() >= expiry:
                raise ValueError("reminder expired during synthesis")
            if not self._valid_asset(output_path):
                raise RuntimeError("transcoded audio is empty or exceeds size limit")
            installed = False
            return self._asset_url(reminder_id, device_id, revision, expiry)
        finally:
            if raw_path:
                raw_path.unlink(missing_ok=True)
            if transcode_path:
                transcode_path.unlink(missing_ok=True)
            if installed:
                output_path.unlink(missing_ok=True)

    async def _transcode(self, source, destination):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is required to prepare reminder audio")
        duration = await self._probe_duration(source)
        if duration > _MAX_DURATION_SECONDS:
            raise ValueError("TTS audio exceeds the reminder audio duration limit")
        proc = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "24000",
            "-c:a",
            "libopus",
            "-application",
            "voip",
            "-frame_duration",
            "60",
            "-f",
            "ogg",
            "-y",
            str(destination),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=45)
        except BaseException:
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode:
            raise RuntimeError(
                f"ffmpeg could not transcode TTS output: {stderr.decode(errors='replace')[:500]}"
            )

    async def _probe_duration(self, source):
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise RuntimeError("ffprobe is required to validate reminder audio duration")
        proc = await asyncio.create_subprocess_exec(
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(source),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        except BaseException:
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode:
            raise RuntimeError(
                f"ffprobe could not inspect TTS output: {stderr.decode(errors='replace')[:500]}"
            )
        try:
            duration = float(stdout.decode().strip())
        except (UnicodeDecodeError, ValueError):
            raise RuntimeError("ffprobe returned an invalid TTS duration") from None
        if not math.isfinite(duration) or duration <= 0:
            raise RuntimeError("TTS audio duration must be finite and positive")
        return duration

    async def handle_get(self, request):
        reminder_id = request.match_info.get("reminder_id", "")
        raw_revision = request.match_info.get("revision", "")
        device_id = request.query.get("device", "")
        try:
            revision = int(raw_revision)
            expiry = int(request.query.get("expires", ""))
        except (TypeError, ValueError):
            raise web.HTTPNotFound()
        supplied = request.query.get("signature", "")
        expected = (
            self._signature(reminder_id, device_id, revision, expiry)
            if self._safe_id(reminder_id)
            and self._safe_id(device_id)
            and revision >= 0
            else ""
        )
        if not expected or not hmac.compare_digest(supplied, expected) or self._clock() >= expiry:
            raise web.HTTPNotFound()
        path = self._asset_path(reminder_id, revision, expiry)
        if not self._valid_asset(path) or path.parent != self._root:
            raise web.HTTPNotFound()
        return web.FileResponse(
            path, headers={"Content-Type": "audio/ogg", "Cache-Control": "no-store"}
        )

    def invalidate(self, reminder_id):
        if not self._safe_id(reminder_id):
            return
        for key, task in list(self._inflight.items()):
            if key[0] == reminder_id:
                task.cancel()
        for path in self._root.glob(f"{reminder_id}-*.ogg"):
            path.unlink(missing_ok=True)

    def _prune_locked(self, now):
        assets = []
        for path in self._root.glob("*.ogg"):
            try:
                _reminder_id, _revision, raw_expiry = path.stem.rsplit("-", 2)
                expiry = int(raw_expiry)
            except ValueError:
                continue
            if expiry <= now:
                path.unlink(missing_ok=True)
            elif self._valid_asset(path):
                try:
                    assets.append((path.stat().st_mtime_ns, path))
                except OSError:
                    pass
        for _, path in sorted(assets)[:-_MAX_ASSETS]:
            path.unlink(missing_ok=True)

    def prune(self, now=None):
        self._prune_locked(self._clock() if now is None else now)

    async def aclose(self):
        self._closed = True
        tasks = tuple(self._inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()

