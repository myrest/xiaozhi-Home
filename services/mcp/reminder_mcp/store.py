"""Transactional one-time reminders; only pending records can change state."""

from __future__ import annotations

import math
import sqlite3
import threading
import time as clock_time
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ALARM_SECONDS = 180
ACK_GRACE_SECONDS = 300
DEFAULT_TIMEZONE = "Asia/Taipei"


class ReminderError(ValueError):
    """A safe, user-facing reminder validation or lookup error."""


def validate_device(device_id: Any) -> str:
    if not isinstance(device_id, str) or not device_id.strip():
        raise ReminderError("缺少有效的裝置身分")
    return device_id


def _finite_number(value: Any) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


class Store:
    """SQLite state shared safely by scheduler and MCP worker threads.

    Public records omit device identity. ``snapshot`` alone includes device_id
    for the trusted notification transport. All times are Unix seconds.
    """

    def __init__(self, database: str | Path, *, clock: Callable[[], float] = clock_time.time):
        self._clock = clock
        self._lock = threading.RLock()
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        with self._db:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    due_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    timezone TEXT NOT NULL,
                    message TEXT,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'cancelled', 'completed', 'stopped', 'expired')
                    ),
                    cancel_synced INTEGER NOT NULL DEFAULT 0,
                    cancel_revision INTEGER NOT NULL DEFAULT 0
                )
            """)
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(reminders)")}
            if "cancel_synced" not in columns:
                self._db.execute(
                    "ALTER TABLE reminders ADD COLUMN cancel_synced INTEGER NOT NULL DEFAULT 0"
                )
            if "cancel_revision" not in columns:
                self._db.execute(
                    "ALTER TABLE reminders ADD COLUMN cancel_revision INTEGER NOT NULL DEFAULT 0"
                )
            self._db.execute("CREATE INDEX IF NOT EXISTS reminders_device_due ON reminders(device_id, due_at)")
            self._db.execute("CREATE INDEX IF NOT EXISTS reminders_status_expiry ON reminders(status, expires_at)")

    def _public(self, row: sqlite3.Row) -> dict[str, Any]:
        record = {
            key: row[key]
            for key in (
                "id",
                "due_at",
                "expires_at",
                "timezone",
                "message",
                "status",
            )
        }
        if record["status"] == "pending" and record["expires_at"] <= self._clock():
            record["status"] = "expired"
        record["due_display"] = datetime.fromtimestamp(
            row["due_at"], ZoneInfo(row["timezone"])
        ).isoformat(timespec="seconds")
        return record

    def _expire(self, now: float) -> None:
        self._db.execute(
            "UPDATE reminders SET status = 'expired' "
            "WHERE status = 'pending' AND expires_at + ? <= ?",
            (ACK_GRACE_SECONDS, now),
        )

    def create(
        self,
        device_id: str,
        *,
        delay_seconds: int | float | None = None,
        time: str | None = None,
        message: str | None = None,
        timezone: str = DEFAULT_TIMEZONE,
    ) -> dict[str, Any]:
        """Create from relative seconds or an offset-aware absolute ISO time."""
        validate_device(device_id)
        if (delay_seconds is None) == (time is None):
            raise ReminderError("請擇一提供 delay_seconds 或 time")
        if not isinstance(timezone, str) or not timezone:
            raise ReminderError("timezone 必須是有效的 IANA 時區")
        try:
            zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ReminderError("timezone 必須是有效的 IANA 時區") from None
        if message is not None and not isinstance(message, str):
            raise ReminderError("message 必須是文字或 null")
        message = message if message and message.strip() else None
        if message is not None and len(message.encode("utf-8")) > 2048:
            raise ReminderError("message 不能超過 2048 個 UTF-8 位元組")
        now = self._clock()
        if delay_seconds is not None:
            if not _finite_number(delay_seconds) or delay_seconds <= 0:
                raise ReminderError("delay_seconds 必須是有限的正數秒數")
            due_at = now + delay_seconds
        else:
            if not isinstance(time, str):
                raise ReminderError("time 必須是正數秒字串或含時區偏移的 ISO8601 時間")
            normalized_time = time.strip()
            try:
                relative_seconds = float(normalized_time)
            except ValueError:
                relative_seconds = None
            if relative_seconds is not None:
                if not _finite_number(relative_seconds) or relative_seconds <= 0:
                    raise ReminderError("相對 time 必須是有限的正數秒數")
                due_at = now + relative_seconds
            else:
                try:
                    parsed = datetime.fromisoformat(normalized_time)
                    if parsed.tzinfo is None or parsed.utcoffset() is None:
                        raise ValueError
                    due_at = parsed.timestamp()
                except (ValueError, OverflowError, OSError):
                    raise ReminderError(
                        "time 必須是正數秒字串或含時區偏移的 ISO8601 時間"
                    ) from None
        if not _finite_number(due_at) or not _finite_number(due_at + ALARM_SECONDS) or due_at <= now:
            raise ReminderError("提醒時間必須是有效的未來時間")
        try:
            datetime.fromtimestamp(due_at, zone)
            datetime.fromtimestamp(due_at + ALARM_SECONDS, zone)
        except (ValueError, OverflowError, OSError):
            raise ReminderError("提醒時間超出支援範圍") from None
        reminder_id = str(uuid4())
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO reminders (id, device_id, due_at, expires_at, timezone, message, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                (reminder_id, device_id, due_at, due_at + ALARM_SECONDS, timezone, message),
            )
            row = self._db.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
            return self._public(row)

    def list(self, device_id: str) -> list[dict[str, Any]]:
        """Return this device's reminders, including their durable terminal states."""
        validate_device(device_id)
        with self._lock, self._db:
            self._expire(self._clock())
            rows = self._db.execute("SELECT * FROM reminders WHERE device_id = ? ORDER BY due_at, id", (device_id,)).fetchall()
            return [self._public(row) for row in rows]

    def cancel(self, device_id: str, reminder_id: str) -> dict[str, Any]:
        """Cancel pending playback; already-terminal records retain their state."""
        validate_device(device_id)
        try:
            if not isinstance(reminder_id, str):
                raise ValueError
            UUID(reminder_id)
        except ValueError:
            raise ReminderError("reminder_id 必須是有效的提醒識別碼") from None
        with self._lock, self._db:
            now = self._clock()
            self._expire(now)
            row = self._db.execute(
                "SELECT * FROM reminders WHERE id = ? AND device_id = ?",
                (reminder_id, device_id),
            ).fetchone()
            if row is None:
                raise ReminderError("找不到此裝置的提醒")
            if row["status"] == "pending" and row["expires_at"] > now:
                self._db.execute(
                    "UPDATE reminders SET status = 'cancelled', cancel_revision = ? "
                    "WHERE id = ? AND device_id = ? AND status = 'pending'",
                    (int(now // 60), reminder_id, device_id),
                )
            row = self._db.execute(
                "SELECT * FROM reminders WHERE id = ? AND device_id = ?",
                (reminder_id, device_id),
            ).fetchone()
            return self._public(row)

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        """Return pending reminders and due cancellations awaiting gateway ACK."""
        now = self._clock()
        with self._lock, self._db:
            self._expire(now)
            reminders = self._db.execute(
                "SELECT id, device_id, due_at, expires_at, timezone, message "
                "FROM reminders WHERE status = 'pending' ORDER BY due_at, id"
            ).fetchall()
            cancellations = self._db.execute(
                "SELECT id, device_id, due_at, expires_at, cancel_revision AS revision "
                "FROM reminders "
                "WHERE status = 'cancelled' AND cancel_synced = 0 "
                "AND due_at <= ? AND expires_at + ? > ? ORDER BY due_at, id",
                (now, ACK_GRACE_SECONDS, now),
            ).fetchall()
            return {
                "reminders": [dict(row) for row in reminders],
                "cancellations": [dict(row) for row in cancellations],
            }

    def apply_outcomes(self, outcomes: Any, sent_snapshot: Any) -> None:
        """Apply ACKs only to reminders or cancellations included in the last snapshot."""
        if not isinstance(outcomes, list) or not isinstance(sent_snapshot, dict):
            return
        sent_reminders = {
            row.get("id")
            for row in sent_snapshot.get("reminders", [])
            if isinstance(row, dict) and isinstance(row.get("id"), str)
        }
        sent_cancellations = {
            row.get("id")
            for row in sent_snapshot.get("cancellations", [])
            if isinstance(row, dict) and isinstance(row.get("id"), str)
        }
        accepted: dict[str, str] = {}
        accepted_cancellations: set[str] = set()
        for outcome in outcomes:
            if not isinstance(outcome, dict):
                continue
            reminder_id, status = outcome.get("id"), outcome.get("status")
            if not isinstance(reminder_id, str) or not isinstance(status, str):
                continue
            if reminder_id in sent_cancellations and status == "cancelled":
                accepted_cancellations.add(reminder_id)
            elif reminder_id in sent_reminders and status in {"completed", "stopped", "expired"}:
                accepted.setdefault(reminder_id, status)
        now = self._clock()
        with self._lock, self._db:
            for reminder_id in accepted_cancellations:
                self._db.execute(
                    "UPDATE reminders SET cancel_synced = 1 "
                    "WHERE id = ? AND status = 'cancelled'",
                    (reminder_id,),
                )
            for reminder_id, status in accepted.items():
                row = self._db.execute(
                    "SELECT status, expires_at FROM reminders WHERE id = ?",
                    (reminder_id,),
                ).fetchone()
                if row is None or row["status"] != "pending":
                    continue
                if status in {"completed", "expired"} and row["expires_at"] > now:
                    continue
                self._db.execute(
                    "UPDATE reminders SET status = ? "
                    "WHERE id = ? AND status = 'pending'",
                    (status, reminder_id),
                )
            self._expire(now)

    def close(self) -> None:
        with self._lock:
            self._db.close()
