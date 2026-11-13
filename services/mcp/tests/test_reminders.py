"""Persistence, transport-race and real MCP protocol coverage for reminders."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from websockets.asyncio.server import serve

from bedtime_mcp.bridge import EndpointBridge
from bedtime_mcp.client import smoke
from reminder_mcp.scheduler import Scheduler
from reminder_mcp.server import create_server
from reminder_mcp.store import ACK_GRACE_SECONDS, ALARM_SECONDS, ReminderError, Store


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "state" / "reminders.sqlite3"
        self.now = 1_800_000_000.0
        self.store = Store(self.database, clock=lambda: self.now)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_reopening_recovers_pending_and_every_terminal_state(self) -> None:
        records = [self.store.create("device-a", delay_seconds=10) for _ in range(5)]
        self.store.cancel("device-a", records[1]["id"])
        self.now = records[1]["due_at"]
        sent_snapshot = self.store.snapshot()
        self.store.apply_outcomes(
            [
                {"id": records[1]["id"], "status": "cancelled"},
                {"id": records[3]["id"], "status": "stopped"},
            ],
            sent_snapshot,
        )
        self.now = records[0]["expires_at"]
        self.store.apply_outcomes(
            [{"id": records[2]["id"], "status": "completed"}], sent_snapshot
        )
        self.assertEqual(
            {record["id"] for record in self.store.snapshot()["reminders"]},
            {records[0]["id"], records[4]["id"]},
        )
        self.now += ACK_GRACE_SECONDS
        self.assertEqual(
            self.store.snapshot(), {"reminders": [], "cancellations": []}
        )
        pending = self.store.create("device-a", delay_seconds=60)
        self.store.close()
        self.store = Store(self.database, clock=lambda: self.now)
        statuses = {record["id"]: record["status"] for record in self.store.list("device-a")}
        self.assertEqual([statuses[record["id"]] for record in records],
                         ["expired", "cancelled", "completed", "stopped", "expired"])
        self.assertEqual(statuses[pending["id"]], "pending")
        self.assertEqual(
            [record["id"] for record in self.store.snapshot()["reminders"]],
            [pending["id"]],
        )

    def test_existing_database_migrates_notification_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "old.sqlite3"
            reminder_id = str(uuid4())
            due_at = self.now + 10
            with sqlite3.connect(database) as old_db:
                old_db.execute(
                    "CREATE TABLE reminders (id TEXT PRIMARY KEY, device_id TEXT NOT NULL, "
                    "due_at REAL NOT NULL, expires_at REAL NOT NULL, timezone TEXT NOT NULL, "
                    "message TEXT, status TEXT NOT NULL)"
                )
                old_db.execute(
                    "INSERT INTO reminders VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        reminder_id,
                        "device-a",
                        due_at,
                        due_at + ALARM_SECONDS,
                        "Asia/Taipei",
                        None,
                        "pending",
                    ),
                )
            migrated = Store(database, clock=lambda: self.now)
            try:
                self.assertEqual(
                    migrated.cancel("device-a", reminder_id)["status"], "cancelled"
                )
                self.now = due_at
                cancellation = migrated.snapshot()["cancellations"][0]
                self.assertEqual(cancellation["id"], reminder_id)
                self.assertEqual(cancellation["revision"], int((due_at - 10) // 60))
            finally:
                migrated.close()

    def test_device_scope_and_terminal_cancel_are_explicit(self) -> None:
        first = self.store.create("device-a", delay_seconds=10, message="喝水")
        second = self.store.create("device-b", delay_seconds=20)
        self.assertEqual([row["id"] for row in self.store.list("device-a")], [first["id"]])
        with self.assertRaises(ReminderError):
            self.store.cancel("device-b", first["id"])
        self.assertEqual(self.store.cancel("device-a", first["id"])["status"], "cancelled")
        self.store.apply_outcomes(
            [{"id": second["id"], "status": "stopped"}],
            {"reminders": [{"id": second["id"]}], "cancellations": []},
        )
        self.assertEqual(self.store.cancel("device-b", second["id"])["status"], "stopped")
        for record in self.store.list("device-a"):
            self.assertNotIn("device_id", record)
            self.assertNotIn("_token", record)
            self.assertIn("+08:00", record["due_display"])

    def test_create_validation_and_default_time_message(self) -> None:
        invalid = [
            {}, {"delay_seconds": 1, "time": "2030-01-01T08:00:00+08:00"},
            {"delay_seconds": True}, {"delay_seconds": 0}, {"delay_seconds": -1},
            {"delay_seconds": float("nan")}, {"delay_seconds": float("inf")},
            {"delay_seconds": 10 ** 1000}, {"delay_seconds": "10"},
            {"time": "2030-01-01T08:00:00"}, {"time": "not a time"},
            {"time": "0"}, {"time": "-1"}, {"time": "nan"},
            {"time": "2000-01-01T08:00:00+08:00"},
            {"delay_seconds": 10, "timezone": "Not/AZone"},
            {"delay_seconds": 10, "timezone": "/etc/passwd"},
            {"delay_seconds": 10, "message": 123},
            {"delay_seconds": 10, "message": "x" * 2049},
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(ReminderError):
                self.store.create("device-a", **arguments)
        for device_id in (None, "", "  ", 1):
            with self.subTest(device=device_id), self.assertRaises(ReminderError):
                self.store.create(device_id, delay_seconds=10)
        for message in (None, "", "   "):
            record = self.store.create("device-a", delay_seconds=1.5, message=message)
            self.assertIsNone(record["message"])
            self.assertEqual(record["due_at"], self.now + 1.5)
            self.assertEqual(record["expires_at"] - record["due_at"], ALARM_SECONDS)
        absolute = self.store.create("device-a", time="2030-01-01T08:00:00+08:00", timezone="America/New_York")
        relative = self.store.create("device-a", time="180")
        self.assertEqual(relative["due_at"], self.now + 180)
        self.assertEqual(absolute["due_display"], "2029-12-31T19:00:00-05:00")

    def test_snapshot_retains_expired_rows_only_for_outcome_ack_grace(self) -> None:
        record = self.store.create("device-a", delay_seconds=10)
        self.assertEqual(self.store.snapshot()["reminders"][0]["device_id"], "device-a")
        self.now = record["due_at"] + 100
        self.assertEqual(self.store.snapshot()["reminders"][0]["expires_at"], record["expires_at"])
        self.now = record["expires_at"]
        self.assertEqual(self.store.snapshot()["reminders"][0]["id"], record["id"])
        self.assertEqual(self.store.list("device-a")[0]["status"], "expired")
        self.now += ACK_GRACE_SECONDS
        self.assertEqual(
            self.store.snapshot(), {"reminders": [], "cancellations": []}
        )

    def test_due_cancellation_retries_until_gateway_ack(self) -> None:
        record = self.store.create("device-a", delay_seconds=10)
        self.store.cancel("device-a", record["id"])
        self.assertEqual(
            self.store.snapshot(), {"reminders": [], "cancellations": []}
        )
        self.now = record["due_at"]
        snapshot = self.store.snapshot()
        self.assertEqual(
            [row["id"] for row in snapshot["cancellations"]], [record["id"]]
        )
        self.assertEqual(
            snapshot["cancellations"][0]["revision"], int(self.now // 60)
        )
        self.store.apply_outcomes(
            [{"id": record["id"], "status": "completed"}], snapshot
        )
        self.assertEqual(
            [row["id"] for row in self.store.snapshot()["cancellations"]],
            [record["id"]],
        )
        self.store.apply_outcomes(
            [{"id": record["id"], "status": "cancelled"}], snapshot
        )
        self.assertEqual(
            self.store.snapshot(), {"reminders": [], "cancellations": []}
        )


    def test_only_sent_valid_outcomes_can_transition_pending_records(self) -> None:
        first = self.store.create("device-a", delay_seconds=10)
        unsent = self.store.create("device-a", delay_seconds=20)
        sent_snapshot = {
            "reminders": [{"id": first["id"]}],
            "cancellations": [],
        }
        self.store.apply_outcomes(
            [
                None,
                {"id": first["id"], "status": []},
                {"id": first["id"], "status": "cancelled"},
                {"id": [first["id"]], "status": "completed"},
                {"id": first["id"], "status": "expired"},
                {"id": unsent["id"], "status": "completed"},
                {"id": str(uuid4()), "status": "stopped"},
            ],
            sent_snapshot,
        )
        self.assertTrue(
            all(record["status"] == "pending" for record in self.store.list("device-a"))
        )
        self.store.cancel("device-a", first["id"])
        self.store.apply_outcomes(
            [{"id": first["id"], "status": "completed"}], sent_snapshot
        )
        self.assertEqual(self.store.list("device-a")[0]["status"], "cancelled")

    def test_sent_completion_and_expiry_outcomes_precede_local_expiry(self) -> None:
        completed = self.store.create("device-a", delay_seconds=10)
        expired = self.store.create("device-a", delay_seconds=10)
        cancelled = self.store.create("device-a", delay_seconds=10)
        sent_snapshot = self.store.snapshot()
        self.store.cancel("device-a", cancelled["id"])
        self.now = completed["expires_at"]
        self.store.apply_outcomes(
            [
                {"id": completed["id"], "status": "completed"},
                {"id": expired["id"], "status": "expired"},
                {"id": cancelled["id"], "status": "stopped"},
            ],
            sent_snapshot,
        )
        states = {
            record["id"]: record["status"] for record in self.store.list("device-a")
        }
        self.assertEqual(
            states,
            {
                completed["id"]: "completed",
                expired["id"]: "expired",
                cancelled["id"]: "cancelled",
            },
        )


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.now = 1_800_000_000.0
        self.store = Store(Path(self.temporary.name) / "reminders.sqlite3", clock=lambda: self.now)

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    async def test_cancel_and_create_during_http_response_do_not_get_overwritten(self) -> None:
        first = self.store.create("device-a", delay_seconds=1)
        entered, release = asyncio.Event(), asyncio.Event()
        sent = []
        second = None

        async def handler(request: httpx.Request) -> httpx.Response:
            sent.append(json.loads(request.content))
            self.assertEqual(request.headers["authorization"], "Bearer fixture-token")
            self.assertEqual(request.extensions["timeout"]["read"], 5.0)
            entered.set()
            await release.wait()
            outcomes = [
                {"id": first["id"], "status": "completed"},
                {"id": second["id"], "status": "completed"},
            ]
            if len(sent) > 1:
                outcomes[0]["status"] = "cancelled"
            return httpx.Response(200, json={"outcomes": outcomes})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scheduler = Scheduler(self.store, "http://notification/internal/reminders/sync", "fixture-token", client=client)
            task = asyncio.create_task(scheduler.sync_once())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.store.cancel("device-a", first["id"])
                second = self.store.create("device-a", delay_seconds=60)
            finally:
                release.set()
                await task
            states = {record["id"]: record["status"] for record in self.store.list("device-a")}
            self.assertEqual(states, {first["id"]: "cancelled", second["id"]: "pending"})
            self.now = second["expires_at"]
            await scheduler.sync_once()
        self.assertEqual([record["id"] for record in sent[0]["reminders"]], [first["id"]])
        self.assertEqual(sent[0]["cancellations"], [])
        self.assertEqual([record["id"] for record in sent[1]["reminders"]], [second["id"]])
        self.assertEqual([record["id"] for record in sent[1]["cancellations"]], [first["id"]])
        self.assertEqual(self.store.list("device-a")[1]["status"], "completed")
        self.assertEqual(
            self.store.snapshot()["cancellations"], []
        )

    async def test_outage_then_recovery_preserves_window_and_expires_offline(self) -> None:
        first = self.store.create("device-a", delay_seconds=1)
        requests = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                raise httpx.ConnectError("fixture outage", request=request)
            if self.now >= first["expires_at"]:
                return httpx.Response(
                    200,
                    json={"outcomes": [{"id": first["id"], "status": "expired"}]},
                )
            return httpx.Response(200, json={"outcomes": []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scheduler = Scheduler(self.store, "http://notification/sync", "fixture-token", client=client)
            with self.assertLogs("reminder_mcp.scheduler", level="WARNING") as logs:
                await scheduler.sync_once()
            self.assertNotIn("fixture-token", " ".join(logs.output))
            self.assertEqual(self.store.list("device-a")[0]["status"], "pending")
            self.now += 100
            await scheduler.sync_once()
            self.assertEqual(requests[1]["reminders"][0]["expires_at"], first["expires_at"])
            self.now = first["expires_at"]
            await scheduler.sync_once()
        self.assertEqual(requests[2]["reminders"][0]["id"], first["id"])
        self.assertEqual(
            requests[2]["reminders"][0]["expires_at"], first["expires_at"]
        )
        self.assertEqual(self.store.list("device-a")[0]["status"], "expired")

    async def test_sync_calls_are_single_flight_and_bad_responses_are_nonterminal(self) -> None:
        self.store.create("device-a", delay_seconds=1)
        active = 0
        maximum = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0)
            active -= 1
            return httpx.Response(200, json={"outcomes": "invalid"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            scheduler = Scheduler(self.store, "http://notification/sync", "fixture-token", client=client)
            with self.assertLogs("reminder_mcp.scheduler", level="WARNING"):
                await asyncio.gather(scheduler.sync_once(), scheduler.sync_once())
        self.assertEqual(maximum, 1)
        self.assertEqual(self.store.list("device-a")[0]["status"], "pending")


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_initialize_list_call_authentication_and_device_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / "reminders.sqlite3")
            bridge = EndpointBridge(create_server(store, "fixture-token"), "ws://unused")

            async def handler(websocket: Any) -> None:
                await bridge.serve_connection(websocket)

            try:
                async with serve(handler, "127.0.0.1", 0) as endpoint:
                    uri = f"ws://127.0.0.1:{endpoint.sockets[0].getsockname()[1]}/"

                    async def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
                        return await smoke(uri, tool_name=name, arguments=args)

                    auth = {"_device_id": "device-a", "_token": "fixture-token"}
                    created = await call("reminder_create", {
                        **auth, "time": "60", "message": ""
                    })
                    self.assertFalse(created["call"].get("isError", False))
                    tools = {tool["name"]: tool for tool in created["tools"]["tools"]}
                    self.assertEqual(set(tools), {"reminder_create", "reminder_list", "reminder_cancel"})
                    self.assertIn("time", tools["reminder_create"]["inputSchema"]["required"])
                    self.assertNotIn(
                        "delay_seconds",
                        tools["reminder_create"]["inputSchema"]["properties"],
                    )
                    for tool in tools.values():
                        self.assertIn("_device_id", tool["inputSchema"]["required"])
                        self.assertIn("_token", tool["inputSchema"]["required"])
                    record = json.loads(created["call"]["content"][0]["text"])
                    self.assertIsNone(record["message"])
                    self.assertNotIn("device_id", record)
                    for args in ({}, {"_device_id": "forged"}, {**auth, "_token": "wrong"},
                                 {"_token": "fixture-token"}, {**auth, "_device_id": " "}):
                        rejected = await call("reminder_list", args)
                        self.assertTrue(rejected["call"]["isError"])
                    for args in (
                        {**auth},
                        {**auth, "delay_seconds": 1},
                        {**auth, "time": None},
                        {**auth, "time": "0"},
                        {**auth, "time": "2030-01-01T08:00:00"},
                    ):
                        rejected = await call("reminder_create", args)
                        self.assertTrue(rejected["call"]["isError"])
                    other_auth = {**auth, "_device_id": "device-b"}
                    other = await call("reminder_list", other_auth)
                    self.assertEqual(json.loads(other["call"]["content"][0]["text"]), {"reminders": []})
                    denied = await call("reminder_cancel", {**other_auth, "reminder_id": record["id"]})
                    self.assertTrue(denied["call"]["isError"])
                    cancelled = await call("reminder_cancel", {**auth, "reminder_id": record["id"]})
                    self.assertEqual(json.loads(cancelled["call"]["content"][0]["text"])["status"], "cancelled")
            finally:
                store.close()
