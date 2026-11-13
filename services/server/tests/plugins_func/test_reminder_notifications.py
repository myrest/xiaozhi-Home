from __future__ import annotations

import asyncio
import time
import unittest
from datetime import datetime
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.notifications.service import NotificationService


class FakeGateway:
    def __init__(self):
        self.published = []
        self.receipts = {}
        self.publish_results = []
        self.published_event = asyncio.Event()

    async def publish(self, device_id, payload):
        self.published.append((device_id, payload))
        self.published_event.set()
        return self.publish_results.pop(0) if self.publish_results else True

    async def receipt(self, device_id, reminder_id, revision):
        return self.receipts.get((device_id, reminder_id, revision))

    async def aclose(self):
        pass


class FakeAssets:
    def __init__(self):
        self.prepared = []
        self.invalidated = []

    async def prepare(self, reminder_id, device_id, revision, text, expires_at):
        self.prepared.append((reminder_id, device_id, revision, text, expires_at))
        return f"https://audio.example.test/reminders/audio/{reminder_id}/{revision}.ogg"

    def invalidate(self, reminder_id):
        self.invalidated.append(reminder_id)

    def prune(self, now=None):
        pass

    async def aclose(self):
        pass


class NotificationRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gateway = FakeGateway()
        self.assets = FakeAssets()
        self.now = time.time()
        self.service = NotificationService(
            "fixture-token",
            gateway=self.gateway,
            assets=self.assets,
            clock=lambda: self.now,
        )
        app = web.Application()
        app.router.add_post("/internal/reminders/sync", self.service._sync)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        for delivery in self.service._deliveries.values():
            if delivery.task is not None and not delivery.task.done():
                delivery.task.cancel()
        await asyncio.gather(
            *(delivery.task for delivery in self.service._deliveries.values() if delivery.task),
            return_exceptions=True,
        )
        await self.client.close()

    async def _post(self, snapshot, token="fixture-token"):
        return await self.client.post(
            "/internal/reminders/sync",
            json=snapshot,
            headers={"Authorization": f"Bearer {token}"},
        )

    async def test_authorization_and_invalid_snapshot_are_atomic(self):
        now = time.time()
        identifier = str(uuid4())
        row = {
            "id": identifier,
            "device_id": "90:70:69:eb:11:18",
            "due_at": now + 10,
            "expires_at": now + 190,
            "timezone": "Asia/Taipei",
            "message": "測試提醒",
        }
        snapshot = {"reminders": [row], "cancellations": []}
        denied = await self._post(snapshot, "wrong-token")
        self.assertEqual(denied.status, 401)
        self.assertEqual(self.service._reminders, {})

        accepted = await self._post(snapshot)
        self.assertEqual(accepted.status, 200)
        self.assertEqual(await accepted.json(), {"outcomes": []})
        self.assertIn(identifier, self.service._reminders)

        malformed = dict(snapshot, cancellations=[dict(row)])
        rejected = await self._post(malformed)
        self.assertEqual(rejected.status, 400)
        self.assertEqual(set(self.service._reminders), {identifier})

    async def test_due_reminder_publishes_audio_event_and_waits_for_device_ack(self):
        now = 1_800_000_060.0
        self.now = now
        identifier = str(uuid4())
        due_at = now - 1
        expires_at = due_at + 180
        row = {
            "id": identifier,
            "device_id": "90:70:69:eb:11:18",
            "due_at": due_at,
            "expires_at": expires_at,
            "timezone": "Asia/Taipei",
            "message": "五分鐘後關電視",
        }
        snapshot = {"reminders": [row], "cancellations": []}
        response = await self._post(snapshot)
        self.assertEqual(response.status, 200)

        with patch("core.notifications.service.ACK_POLL_SECONDS", 0.01):
            await self.service._reconcile()
            await asyncio.wait_for(self.gateway.published_event.wait(), timeout=1)
            device_id, payload = self.gateway.published[0]
            self.assertEqual(device_id, row["device_id"])
            self.assertEqual(payload["type"], "notify")
            self.assertEqual(payload["reminder_id"], identifier)
            self.assertEqual(payload["subtitle"], row["message"])
            self.assertEqual(payload["expires_at"], int(expires_at))
            self.assertEqual(
                self.assets.prepared,
                [(identifier, row["device_id"], 0, row["message"], expires_at)],
            )

            delivery = self.service._deliveries[identifier]
            self.gateway.receipts[(row["device_id"], identifier, 0)] = {
                "status": "completed"
            }
            await asyncio.wait_for(self._wait_for_outcome(identifier), timeout=1)
            self.assertEqual(self.service._outcomes[identifier].status, "completed")

        acknowledged = await self._post(snapshot)
        self.assertEqual(
            await acknowledged.json(),
            {"outcomes": [{"id": identifier, "status": "completed"}]},
        )
        self.assertNotIn(identifier, self.service._deliveries)
        self.assertIn(identifier, self.assets.invalidated)
        self.assertTrue(delivery.task.done())

    async def test_publish_retries_after_gateway_reports_device_offline(self):
        now = 1_800_000_060.0
        self.now = now
        identifier = str(uuid4())
        row = {
            "id": identifier,
            "device_id": "90:70:69:eb:11:18",
            "due_at": now - 1,
            "expires_at": now + 179,
            "timezone": "Asia/Taipei",
            "message": "重試通知",
        }
        snapshot = {"reminders": [row], "cancellations": []}
        response = await self._post(snapshot)
        self.assertEqual(response.status, 200)
        self.gateway.publish_results = [False, True]
        clock = [now]

        def advance_clock():
            clock[0] += 0.02
            return clock[0]

        self.service._clock = advance_clock

        with (
            patch("core.notifications.service.ACK_POLL_SECONDS", 0.01),
            patch("core.notifications.service.RETRY_SECONDS", 0.01),
            patch("core.notifications.service.PUBLISH_RETRY_SECONDS", 0.01),
        ):
            await self.service._reconcile()
            await asyncio.wait_for(self._wait_for_publish_count(2), timeout=1)
            first = self.gateway.published[0][1]
            second = self.gateway.published[1][1]
            self.assertEqual(first["type"], "notify")
            self.assertEqual(second, first)
            self.gateway.receipts[(row["device_id"], identifier, 0)] = {
                "status": "completed"
            }
            await asyncio.wait_for(self._wait_for_outcome(identifier), timeout=1)
            self.assertEqual(self.service._outcomes[identifier].status, "completed")

    async def _wait_for_publish_count(self, expected):
        while len(self.gateway.published) < expected:
            await asyncio.sleep(0.005)


    async def test_missing_message_uses_due_time_and_stable_revision(self):
        now = 1_800_000_060.0
        self.now = now
        identifier = str(uuid4())
        due_at = now - 1
        row = {
            "id": identifier,
            "device_id": "90:70:69:eb:11:18",
            "due_at": due_at,
            "expires_at": due_at + 180,
            "timezone": "Asia/Taipei",
            "message": None,
        }
        response = await self._post({"reminders": [row], "cancellations": []})
        self.assertEqual(response.status, 200)

        with patch("core.notifications.service.ACK_POLL_SECONDS", 0.01):
            await self.service._reconcile()
            await asyncio.wait_for(self.gateway.published_event.wait(), timeout=1)
            _, payload = self.gateway.published[0]
            due_time = datetime.fromtimestamp(due_at, ZoneInfo("Asia/Taipei"))
            self.assertEqual(payload["subtitle"], f"現在時間：{due_time:%H:%M}")
            self.assertEqual(payload["revision"], int(due_at // 60))
            self.assertEqual(self.assets.prepared[0][3], payload["subtitle"])
            delivery = self.service._deliveries[identifier]
            self.now = now + 65
            self.assertFalse(self.service._refresh_revision(delivery))
            self.assertEqual(delivery.revision, payload["revision"])
            self.now = now

    async def test_due_cancellation_publishes_cancel_event_until_ack(self):
        now = 1_800_000_060.0
        self.now = now
        identifier = str(uuid4())
        row = {
            "id": identifier,
            "device_id": "90:70:69:eb:11:18",
            "due_at": now - 1,
            "expires_at": now + 179,
            "revision": int((now - 20) // 60),
        }
        response = await self._post({"reminders": [], "cancellations": [row]})
        self.assertEqual(response.status, 200)

        with patch("core.notifications.service.ACK_POLL_SECONDS", 0.01):
            await self.service._reconcile()
            await asyncio.wait_for(self.gateway.published_event.wait(), timeout=1)
            device_id, payload = self.gateway.published[0]
            self.assertEqual(device_id, row["device_id"])
            self.assertEqual(payload["type"], "notify_cancel")
            self.assertEqual(payload["reminder_id"], identifier)
            self.assertEqual(
                payload["revision"], max(row["revision"], int(now // 60))
            )
            self.assertEqual(payload["expires_at"], int(row["expires_at"]))
            self.assertEqual(self.assets.prepared, [])

            self.gateway.receipts[
                (device_id, identifier, max(row["revision"], int(now // 60)))
            ] = {"status": "cancelled"}
            await asyncio.wait_for(self._wait_for_outcome(identifier), timeout=1)
            self.assertEqual(self.service._outcomes[identifier].status, "cancelled")

    async def test_expired_reminder_is_dropped_without_mqtt_publish(self):
        now = 1_800_000_060.0
        self.now = now
        identifier = str(uuid4())
        row = {
            "id": identifier,
            "device_id": "90:70:69:eb:11:18",
            "due_at": now - 181,
            "expires_at": now - 1,
            "timezone": "Asia/Taipei",
            "message": "過期提醒",
        }
        response = await self._post({"reminders": [row], "cancellations": []})
        self.assertEqual(response.status, 200)
        await self.service._reconcile()
        self.assertEqual(self.gateway.published, [])
        self.assertEqual(self.service._outcomes[identifier].status, "expired")

    async def _wait_for_outcome(self, identifier):
        while identifier not in self.service._outcomes:
            await asyncio.sleep(0.005)


if __name__ == "__main__":
    unittest.main()
