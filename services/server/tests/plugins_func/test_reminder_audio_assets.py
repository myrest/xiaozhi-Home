from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.notifications.assets import AudioAssets
from core.providers.tts.base import TTSProviderBase


class FileTTSProvider:
    to_tts = TTSProviderBase.to_tts
    audio_file_type = "wav"
    tts_timeout = 1

    def __init__(self):
        self.conn = None

    async def text_to_speak(self, _text, output_file):
        if self.conn.sample_rate != 24000:
            raise AssertionError("reminder TTS must use the configured 24 kHz device rate")
        Path(output_file).write_bytes(b"file-audio")


class StreamTTSProvider(FileTTSProvider):
    def to_tts(self, _text):
        if self.conn.sample_rate != 24000:
            raise AssertionError("reminder TTS must use the configured 24 kHz device rate")
        return [b"opus-frame"]


class AudioAssetRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.assets = AudioAssets(
            {"audio_signing_secret": "test-only-key"},
            "https://audio.example.test",
            self.root,
        )
        self.app = web.Application()
        self.app.router.add_get(
            "/reminders/audio/{reminder_id}/{revision}.ogg", self.assets.handle_get
        )
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.assets.aclose()
        self.temp.cleanup()

    async def _register(self, *, device="90:70:69:eb:11:18", expiry=None):
        reminder = "reminder-1"
        revision = 0
        expiry = int(expiry or time.time() + 60)
        path = self.assets._asset_path(reminder, revision, expiry)
        path.write_bytes(b"OggS-fixture")
        url = self.assets._asset_url(reminder, device, revision, expiry)
        return url, path

    async def _prepare_with_provider(self, provider, expected_audio):
        config = {
            "audio_signing_secret": "test-only-key",
            "selected_module": {"TTS": "fixture"},
            "TTS": {"fixture": {}},
        }
        assets = AudioAssets(config, "https://audio.example.test", self.root)
        source_audio = []

        async def transcode(source, destination):
            source_audio.append(Path(source).read_bytes())
            Path(destination).write_bytes(b"OggS-transcoded")

        assets._transcode = transcode
        try:
            with (
                patch(
                    "config.config_loader.get_private_config_from_api",
                    new=AsyncMock(return_value={}),
                ),
                patch(
                    "core.utils.modules_initialize.initialize_tts",
                    return_value=provider,
                ),
                patch(
                    "core.utils.util.opus_datas_to_wav_bytes",
                    return_value=b"decoded-wav",
                ) as decode_opus,
            ):
                await assets.prepare(
                    "reminder-1",
                    "90:70:69:eb:11:18",
                    0,
                    "測試提醒",
                    int(time.time() + 60),
                )
            self.assertEqual(source_audio, [expected_audio])
            if isinstance(provider, StreamTTSProvider):
                decode_opus.assert_called_once_with(
                    [b"opus-frame"], sample_rate=24000, channels=1
                )
        finally:
            await assets.aclose()

    async def test_file_tts_provider_writes_directly_to_private_asset_temp(self):
        await self._prepare_with_provider(FileTTSProvider(), b"file-audio")

    async def test_stream_tts_provider_opus_is_decoded_before_transcoding(self):
        await self._prepare_with_provider(StreamTTSProvider(), b"decoded-wav")

    async def test_signed_asset_survives_service_restart(self):
        url, _ = await self._register()
        parts = urlsplit(url)
        restarted = AudioAssets(
            {"audio_signing_secret": "test-only-key"},
            "https://audio.example.test",
            self.root,
        )
        app = web.Application()
        app.router.add_get(
            "/reminders/audio/{reminder_id}/{revision}.ogg", restarted.handle_get
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get(parts.path + "?" + parts.query)
            self.assertEqual(response.status, 200)
        finally:
            await client.close()
            await restarted.aclose()

    async def test_signed_get_is_bound_to_device_and_revision(self):
        url, _ = await self._register()
        parts = urlsplit(url)
        allowed = await self.client.get(parts.path + "?" + parts.query)
        self.assertEqual(allowed.status, 200)
        self.assertEqual(allowed.headers["Content-Type"], "audio/ogg")

        values = parse_qs(parts.query)
        values["device"] = ["device-b"]
        denied = await self.client.get(parts.path + "?" + "&".join(f"{k}={v[0]}" for k, v in values.items()))
        self.assertEqual(denied.status, 404)
        wrong_revision = await self.client.get(parts.path.replace("/0.ogg", "/1.ogg") + "?" + parts.query)
        self.assertEqual(wrong_revision.status, 404)

    async def test_expired_and_traversal_requests_are_rejected(self):
        url, path = await self._register(expiry=int(time.time()) - 1)
        parts = urlsplit(url)
        expired = await self.client.get(parts.path + "?" + parts.query)
        self.assertEqual(expired.status, 404)
        traversal = await self.client.get("/reminders/audio/..%2Foutside/0.ogg")
        self.assertIn(traversal.status, (400, 404))
        self.assertTrue(path.exists())

    async def test_invalidate_removes_files_and_rejects_url(self):
        url, path = await self._register()
        self.assets.invalidate("reminder-1")
        self.assertFalse(path.exists())
        parts = urlsplit(url)
        denied = await self.client.get(parts.path + "?" + parts.query)
        self.assertEqual(denied.status, 404)

    def test_public_url_requires_https_by_default(self):
        with self.assertRaises(ValueError):
            AudioAssets({"audio_signing_secret": "key"}, "http://audio.example.test", self.root)

