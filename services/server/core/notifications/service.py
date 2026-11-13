"""Authenticated reminder snapshots and MQTT notification delivery."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import math
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import web

from config.logger import setup_logging
from core.notifications.assets import AudioAssets
from core.notifications.mqtt_gateway import MqttGatewayClient


TAG = __name__
logger = setup_logging()
OUTCOME_RETENTION_SECONDS = 300
RETRY_SECONDS = 5
PUBLISH_RETRY_SECONDS = 10
ACK_POLL_SECONDS = 1
POST_EXPIRY_ACK_SECONDS = 5


@dataclass(frozen=True)
class Reminder:
    id: str
    device_id: str
    due_at: float
    expires_at: float
    timezone: str
    message: str | None


@dataclass(frozen=True)
class Cancellation:
    id: str
    device_id: str
    due_at: float
    expires_at: float
    revision: int


@dataclass(frozen=True)
class Outcome:
    status: str
    retain_until: float


@dataclass
class Delivery:
    id: str
    device_id: str
    due_at: float
    expires_at: float
    timezone: str
    message: str | None
    revision: int = -1
    minute_key: tuple[int, ...] | None = None
    subtitle: str | None = None
    cancelled: bool = False
    task_kind: str | None = None
    task: asyncio.Task | None = None


def _finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _parse_identity(value):
    if not isinstance(value, str):
        raise ValueError("id must be a UUID string")
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("id must be a UUID string") from exc


def _parse_times(row):
    times = []
    for name in ("due_at", "expires_at"):
        value = row[name]
        if not _finite(value):
            raise ValueError(f"{name} must be a finite number")
        times.append(float(value))
    due_at, expires_at = times
    if expires_at <= due_at:
        raise ValueError("expires_at must be after due_at")
    return due_at, expires_at


def _parse_snapshot(body):
    if not isinstance(body, dict) or set(body) != {"reminders", "cancellations"}:
        raise ValueError("expected reminders and cancellations")
    reminders, cancellations = body["reminders"], body["cancellations"]
    if not isinstance(reminders, list) or not isinstance(cancellations, list):
        raise ValueError("reminders and cancellations must be lists")

    parsed_reminders = {}
    parsed_cancellations = {}
    reminder_fields = {"id", "device_id", "due_at", "expires_at", "timezone", "message"}
    cancellation_fields = {"id", "device_id", "due_at", "expires_at", "revision"}

    for row in reminders:
        if not isinstance(row, dict) or set(row) != reminder_fields:
            raise ValueError("invalid reminder fields")
        identifier = _parse_identity(row["id"])
        if identifier in parsed_reminders or identifier in parsed_cancellations:
            raise ValueError("duplicate reminder id")
        device_id = row["device_id"]
        if not isinstance(device_id, str) or not device_id.strip() or len(device_id) > 128:
            raise ValueError("device_id must be a nonempty string")
        due_at, expires_at = _parse_times(row)
        timezone = row["timezone"]
        if not isinstance(timezone, str) or not timezone:
            raise ValueError("timezone must name an IANA timezone")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must name an IANA timezone") from exc
        message = row["message"]
        if message is not None and not isinstance(message, str):
            raise ValueError("message must be null or a string")
        if isinstance(message, str):
            message = message.strip() or None
            if message is not None and len(message.encode("utf-8")) > 2048:
                raise ValueError("message exceeds the supported length")
        parsed_reminders[identifier] = Reminder(
            identifier, device_id, due_at, expires_at, timezone, message
        )

    for row in cancellations:
        if not isinstance(row, dict) or set(row) != cancellation_fields:
            raise ValueError("invalid cancellation fields")
        identifier = _parse_identity(row["id"])
        if identifier in parsed_reminders or identifier in parsed_cancellations:
            raise ValueError("duplicate reminder id")
        device_id = row["device_id"]
        if not isinstance(device_id, str) or not device_id.strip() or len(device_id) > 128:
            raise ValueError("device_id must be a nonempty string")
        due_at, expires_at = _parse_times(row)
        revision = row["revision"]
        if type(revision) is not int or revision < 0:
            raise ValueError("cancellation revision must be a nonnegative integer")
        parsed_cancellations[identifier] = Cancellation(
            identifier, device_id, due_at, expires_at, revision
        )

    return parsed_reminders, parsed_cancellations


class NotificationService:
    """Deliver idempotent MQTT events; the device owns playback and ringing."""

    def __init__(self, token, config=None, *, gateway=None, assets=None, clock=time.time):
        if not isinstance(token, str) or not token.strip():
            raise ValueError("a nonempty reminder token is required")
        self.token = token
        self._token_bytes = token.encode("utf-8")
        self._clock = clock
        self.config = config or {}
        server_config = self.config.get("server", {})
        signature_key = server_config.get("mqtt_signature_key") if isinstance(server_config, dict) else None
        gateway_address = server_config.get("mqtt_manager_api") if isinstance(server_config, dict) else None
        self.gateway = gateway
        self.assets = assets
        self._owns_gateway = False
        self._owns_assets = False
        config_errors = []

        if self.gateway is None:
            try:
                self.gateway = MqttGatewayClient(
                    str(gateway_address or ""), str(signature_key or "")
                )
                self._owns_gateway = True
            except ValueError:
                config_errors.append("MQTT Gateway management address or signature key is missing or invalid")

        if self.assets is None:
            public_url = os.environ.get("REMINDER_AUDIO_PUBLIC_URL")
            if public_url and signature_key and str(signature_key).strip().lower() != "null":
                asset_config = dict(self.config)
                asset_config["audio_signing_secret"] = hmac.new(
                    str(signature_key).encode("utf-8"),
                    b"xiaozhi-reminder-audio-v1",
                    hashlib.sha256,
                ).hexdigest()
                asset_root = os.environ.get(
                    "REMINDER_AUDIO_DIR",
                    str(Path(self.config.get("log", {}).get("data_dir", "data")) / "reminder_audio"),
                )
                try:
                    self.assets = AudioAssets(asset_config, public_url, asset_root, clock=clock)
                    self._owns_assets = True
                except ValueError:
                    config_errors.append("REMINDER_AUDIO_PUBLIC_URL settings are invalid")
            else:
                config_errors.append("REMINDER_AUDIO_PUBLIC_URL or MQTT signature key is missing")

        self._configuration_errors = tuple(config_errors)
        self._configured = self.gateway is not None and self.assets is not None
        self._reminders: dict[str, Reminder] = {}
        self._deliveries: dict[str, Delivery] = {}
        self._outcomes: dict[str, Outcome] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._started = False

    async def _sync(self, request):
        authorization = request.headers.get("Authorization", "")
        scheme, separator, supplied = authorization.partition(" ")
        if (
            not separator
            or scheme.lower() != "bearer"
            or not hmac.compare_digest(
                supplied.encode("utf-8", errors="surrogatepass"), self._token_bytes
            )
        ):
            raise web.HTTPUnauthorized(text="invalid reminder authorization")
        try:
            body = await request.json()
            incoming, cancellations = _parse_snapshot(body)
        except Exception:
            raise web.HTTPBadRequest(text="invalid reminder snapshot") from None

        active_ids = set(incoming) | set(cancellations)
        async with self._lock:
            if self._closed:
                raise web.HTTPServiceUnavailable()
            now = self._clock()
            self._outcomes = {
                identifier: outcome
                for identifier, outcome in self._outcomes.items()
                if identifier in active_ids and outcome.retain_until > now
            }
            for identifier in set(self._deliveries) - active_ids:
                delivery = self._deliveries.pop(identifier)
                if delivery.task is not None and not delivery.task.done():
                    delivery.task.cancel()
                if self.assets is not None:
                    self.assets.invalidate(identifier)

            self._reminders = incoming
            for identifier, cancellation in cancellations.items():
                delivery = self._deliveries.get(identifier)
                if delivery is None:
                    delivery = Delivery(
                        cancellation.id,
                        cancellation.device_id,
                        cancellation.due_at,
                        cancellation.expires_at,
                        "UTC",
                        None,
                        revision=cancellation.revision,
                        cancelled=True,
                    )
                    self._deliveries[identifier] = delivery
                else:
                    delivery.cancelled = True
                    delivery.expires_at = cancellation.expires_at
                    delivery.revision = max(delivery.revision, cancellation.revision)
                    if delivery.task is not None and not delivery.task.done() and delivery.task_kind != "cancel":
                        previous = delivery.task
                        previous.cancel()
                        delivery.task_kind = "cancel"
                        delivery.task = asyncio.create_task(
                            self._cancel_after(previous, delivery)
                        )
            for identifier, reminder in incoming.items():
                if identifier in self._outcomes:
                    continue
                delivery = self._deliveries.get(identifier)
                if delivery is not None and delivery.cancelled:
                    continue
                if delivery is None:
                    self._deliveries[identifier] = Delivery(
                        reminder.id,
                        reminder.device_id,
                        reminder.due_at,
                        reminder.expires_at,
                        reminder.timezone,
                        reminder.message,
                    )
                else:
                    delivery.device_id = reminder.device_id
                    delivery.due_at = reminder.due_at
                    delivery.expires_at = reminder.expires_at
                    delivery.timezone = reminder.timezone
                    delivery.message = reminder.message

            outcomes = [
                {"id": identifier, "status": outcome.status}
                for identifier, outcome in self._outcomes.items()
                if identifier in active_ids
            ]
        return web.json_response({"outcomes": outcomes})

    def _record_outcome(self, delivery: Delivery, status: str):
        self._outcomes[delivery.id] = Outcome(
            status, delivery.expires_at + OUTCOME_RETENTION_SECONDS
        )
        self._deliveries.pop(delivery.id, None)
        if (
            delivery.task is not None
            and delivery.task is not asyncio.current_task()
            and not delivery.task.done()
        ):
            delivery.task.cancel()
        if self.assets is not None:
            self.assets.invalidate(delivery.id)

    async def _finish(self, delivery: Delivery, status: str):
        async with self._lock:
            if self._deliveries.get(delivery.id) is not delivery:
                return
            self._record_outcome(delivery, status)

    async def _is_current(self, delivery: Delivery, revision: int | None = None):
        async with self._lock:
            return (
                not self._closed
                and self._deliveries.get(delivery.id) is delivery
                and (revision is None or delivery.revision == revision)
            )

    async def _receipt_status(self, delivery: Delivery, revision: int):
        try:
            receipt = await self.gateway.receipt(
                delivery.device_id, delivery.id, revision
            )
        except Exception as exc:
            logger.bind(tag=TAG).warning(
                f"Reminder receipt lookup failed id={delivery.id} error={type(exc).__name__}"
            )
            return None
        if not isinstance(receipt, dict):
            return None
        status = receipt.get("status")
        allowed = {"received", "playing", "completed", "stopped", "expired", "failed", "cancelled"}
        return status if status in allowed else None

    async def _run_notify(self, delivery: Delivery, revision: int, subtitle: str):
        audio_url = None
        next_publish = 0.0
        try:
            while await self._is_current(delivery, revision):
                now = self._clock()
                if now >= delivery.expires_at:
                    status = await self._receipt_status(delivery, revision)
                    if status in {"completed", "stopped", "expired"}:
                        await self._finish(delivery, status)
                        return
                    if now >= delivery.expires_at + POST_EXPIRY_ACK_SECONDS:
                        await self._finish(delivery, "expired")
                        return
                    await asyncio.sleep(ACK_POLL_SECONDS)
                    continue

                if audio_url is None:
                    try:
                        audio_url = await self.assets.prepare(
                            delivery.id,
                            delivery.device_id,
                            revision,
                            subtitle,
                            delivery.expires_at,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.bind(tag=TAG).warning(
                            f"Reminder audio preparation failed id={delivery.id} error={type(exc).__name__}"
                        )
                        await asyncio.sleep(min(RETRY_SECONDS, max(0, delivery.expires_at - self._clock())))
                        continue

                if not await self._is_current(delivery, revision):
                    return
                now = self._clock()
                if now >= delivery.expires_at:
                    continue
                if now >= next_publish:
                    payload = {
                        "type": "notify",
                        "reminder_id": delivery.id,
                        "revision": revision,
                        "audio_url": audio_url,
                        "subtitle": subtitle,
                        "expires_at": int(delivery.expires_at),
                    }
                    try:
                        published = await self.gateway.publish(delivery.device_id, payload)
                    except Exception as exc:
                        logger.bind(tag=TAG).warning(
                            f"Reminder MQTT publish failed id={delivery.id} error={type(exc).__name__}"
                        )
                        published = False
                    next_publish = self._clock() + (
                        PUBLISH_RETRY_SECONDS if published else RETRY_SECONDS
                    )

                status = await self._receipt_status(delivery, revision)
                if status in {"completed", "stopped", "expired"}:
                    await self._finish(delivery, status)
                    return
                await asyncio.sleep(ACK_POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.bind(tag=TAG).warning(
                f"Reminder delivery failed id={delivery.id} error={type(exc).__name__}"
            )

    async def _cancel_after(self, previous: asyncio.Task, delivery: Delivery):
        await asyncio.gather(previous, return_exceptions=True)
        if await self._is_current(delivery):
            await self._run_cancel(delivery)

    async def _run_cancel(self, delivery: Delivery):
        revision = max(delivery.revision, 0)
        payload = {
            "type": "notify_cancel",
            "reminder_id": delivery.id,
            "revision": revision,
            "expires_at": int(delivery.expires_at),
        }
        next_publish = 0.0
        try:
            while await self._is_current(delivery):
                now = self._clock()
                if now >= delivery.expires_at:
                    await self._finish(delivery, "cancelled")
                    return
                if now >= next_publish:
                    try:
                        published = await self.gateway.publish(delivery.device_id, payload)
                    except Exception as exc:
                        logger.bind(tag=TAG).warning(
                            f"Reminder cancellation publish failed id={delivery.id} error={type(exc).__name__}"
                        )
                        published = False
                    next_publish = self._clock() + (
                        PUBLISH_RETRY_SECONDS if published else RETRY_SECONDS
                    )
                status = await self._receipt_status(delivery, revision)
                if status == "cancelled":
                    await self._finish(delivery, "cancelled")
                    return
                await asyncio.sleep(ACK_POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.bind(tag=TAG).warning(
                f"Reminder cancellation failed id={delivery.id} error={type(exc).__name__}"
            )

    def _refresh_revision(self, delivery: Delivery):
        if delivery.message is not None:
            minute_key = None
            revision = 0
            subtitle = delivery.message
        else:
            due_time = datetime.fromtimestamp(
                delivery.due_at, ZoneInfo(delivery.timezone)
            )
            minute_key = (
                due_time.year,
                due_time.month,
                due_time.day,
                due_time.hour,
                due_time.minute,
            )
            revision = int(delivery.due_at // 60)
            subtitle = f"現在時間：{due_time.strftime('%H:%M')}"
        if (
            delivery.revision == revision
            and delivery.minute_key == minute_key
            and delivery.subtitle == subtitle
        ):
            return False
        delivery.revision = revision
        delivery.minute_key = minute_key
        delivery.subtitle = subtitle
        return True

    async def _reconcile(self):
        now = self._clock()
        if self.assets is not None:
            self.assets.prune(now)
        async with self._lock:
            if self._closed:
                return
            self._outcomes = {
                identifier: outcome
                for identifier, outcome in self._outcomes.items()
                if outcome.retain_until > now
            }

            for identifier, reminder in self._reminders.items():
                if identifier in self._outcomes:
                    continue
                delivery = self._deliveries.get(identifier)
                if now >= reminder.expires_at:
                    if delivery is not None and delivery.task is not None and not delivery.task.done() and delivery.task_kind == "notify":
                        continue
                    if delivery is not None:
                        self._record_outcome(delivery, "expired")
                    else:
                        self._outcomes[identifier] = Outcome(
                            "expired", reminder.expires_at + OUTCOME_RETENTION_SECONDS
                        )
                    continue
                if now < reminder.due_at or not self._configured:
                    continue
                if delivery is None or delivery.cancelled:
                    continue
                changed = self._refresh_revision(delivery)
                if changed or delivery.task is None or delivery.task.done():
                    previous = delivery.task
                    if previous is not None and not previous.done():
                        previous.cancel()
                    delivery.task_kind = "notify"
                    delivery.task = asyncio.create_task(
                        self._run_notify(delivery, delivery.revision, delivery.subtitle)
                    )

            for identifier, delivery in list(self._deliveries.items()):
                if not delivery.cancelled or identifier in self._outcomes:
                    continue
                if now >= delivery.expires_at:
                    self._record_outcome(delivery, "cancelled")
                    continue
                if self._configured and (delivery.task is None or delivery.task.done()):
                    delivery.revision = max(delivery.revision, int(now // 60))
                    delivery.task_kind = "cancel"
                    delivery.task = asyncio.create_task(self._run_cancel(delivery))

    async def _scheduler(self):
        while True:
            try:
                await self._reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.bind(tag=TAG).warning(
                    f"Reminder scheduler iteration failed error={type(exc).__name__}"
                )
            await asyncio.sleep(0.25)

    async def start(self, host="0.0.0.0", port=8006):
        """Run the authenticated internal snapshot API and MQTT retry loop."""
        if self._started or self._closed:
            raise RuntimeError("notification service cannot be started twice")
        self._started = True
        if not self._configured:
            logger.bind(tag=TAG).warning(
                "MQTT reminder delivery is not configured: "
                + "; ".join(self._configuration_errors)
            )
        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_post("/internal/reminders/sync", self._sync)
        runner = web.AppRunner(app, access_log=None)
        scheduler = None
        try:
            await runner.setup()
            await web.TCPSite(runner, host, port).start()
            scheduler = asyncio.create_task(self._scheduler())
            await scheduler
        finally:
            self._closed = True
            if scheduler is not None:
                scheduler.cancel()
                await asyncio.gather(scheduler, return_exceptions=True)
            async with self._lock:
                tasks = [
                    delivery.task
                    for delivery in self._deliveries.values()
                    if delivery.task is not None and not delivery.task.done()
                ]
                for task in tasks:
                    task.cancel()
                self._deliveries.clear()
                self._reminders.clear()
                self._outcomes.clear()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if self._owns_assets and self.assets is not None:
                await self.assets.aclose()
            if self._owns_gateway and self.gateway is not None:
                await self.gateway.aclose()
            await runner.cleanup()
