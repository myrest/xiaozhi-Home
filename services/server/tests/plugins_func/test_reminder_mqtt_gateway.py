from __future__ import annotations

import hashlib
import json
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from core.notifications.mqtt_gateway import MqttGatewayClient


class MqttGatewayClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_publish_and_receipt_use_daily_auth_and_device_scoped_paths(self):
        signature_key = "strong-test-signature-key"
        today = datetime.now(ZoneInfo("Asia/Taipei")).date()
        token = hashlib.sha256(
            f"{today.isoformat()}{signature_key}".encode()
        ).hexdigest()
        calls = []

        def handler(request):
            calls.append(request)
            self.assertEqual(request.headers["Authorization"], f"Bearer {token}")
            if request.method == "POST":
                self.assertIn(b"90%3A70%3A69%3Aeb%3A11%3A18", request.url.raw_path)
                self.assertEqual(json.loads(request.content)["type"], "notify")
                return httpx.Response(200, json={"accepted": True})
            self.assertIn(b"90%3A70%3A69%3Aeb%3A11%3A18", request.url.raw_path)
            self.assertEqual(request.url.params["revision"], "42")
            return httpx.Response(200, json={"receipt": {"status": "playing"}})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            gateway = MqttGatewayClient(
                "http://mqtt-gateway:8007", signature_key, client=client
            )
            accepted = await gateway.publish(
                "90:70:69:eb:11:18",
                {
                    "type": "notify",
                    "reminder_id": "9ef86578-0e28-47b4-8f96-08d1f10ed943",
                    "revision": 42,
                    "audio_url": "https://audio.example.test/reminders/audio/9ef86578-0e28-47b4-8f96-08d1f10ed943/42.ogg",
                    "subtitle": "提醒內容",
                    "expires_at": 1_800_000_000,
                },
            )
            receipt = await gateway.receipt(
                "90:70:69:eb:11:18",
                "9ef86578-0e28-47b4-8f96-08d1f10ed943",
                42,
            )

        self.assertTrue(accepted)
        self.assertEqual(receipt, {"status": "playing"})
        self.assertEqual([request.method for request in calls], ["POST", "GET"])

    async def test_publish_rejects_gateway_conflict(self):
        async def handler(request):
            return httpx.Response(409, json={"error": "Conflicting notification revision"})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            gateway = MqttGatewayClient(
                "http://mqtt-gateway:8007", "strong-test-signature-key", client=client
            )
            accepted = await gateway.publish(
                "90:70:69:eb:11:18",
                {
                    "type": "notify",
                    "reminder_id": "9ef86578-0e28-47b4-8f96-08d1f10ed943",
                    "revision": 0,
                },
            )
        self.assertFalse(accepted)

    def test_gateway_origin_rejects_urls_with_paths_or_credentials(self):
        for address in (
            "https://user:pass@example.test",
            "https://example.test/api",
            "https://example.test?token=x",
        ):
            with self.subTest(address=address), self.assertRaises(ValueError):
                MqttGatewayClient(address, "strong-test-signature-key")


if __name__ == "__main__":
    unittest.main()
