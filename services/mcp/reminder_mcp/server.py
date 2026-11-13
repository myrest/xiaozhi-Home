"""Authenticated MCP tools and long-lived reminder process entry point."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mcp.server.lowlevel import Server
from mcp.types import CallToolResult, TextContent, Tool

from bedtime_mcp.bridge import EndpointBridge

from .scheduler import Scheduler
from .store import DEFAULT_TIMEZONE, ReminderError, Store, validate_device


_HIDDEN_PROPERTIES = {
    "_device_id": {"type": "string", "minLength": 1, "description": "由伺服器注入的裝置身分，不可由使用者提供。"},
    "_token": {"type": "string", "minLength": 1, "description": "由伺服器注入的內部認證，不可由使用者提供。"},
}
_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "reminder_create": {
        "type": "object",
        "properties": {
            **_HIDDEN_PROPERTIES,
            "time": {
                "type": "string",
                "description": "相對時間提供正數秒數字串，例如 180（代表 3 分鐘後）；指定日曆時間時提供含明確時區偏移的 ISO8601 時間，例如 2027-01-01T08:00:00+08:00。未指定時區使用 Asia/Taipei。",
            },
            "message": {
                "type": "string",
                "description": "提醒文字；省略或空白時播報當下本地時間，每分鐘更新。",
            },
            "timezone": {
                "type": "string",
                "default": DEFAULT_TIMEZONE,
                "description": "播報及顯示使用的 IANA 時區，預設 Asia/Taipei。",
            },
        },
        "required": ["_device_id", "_token", "time"],
        "additionalProperties": False,
    },
    "reminder_list": {
        "type": "object", "properties": dict(_HIDDEN_PROPERTIES),
        "required": ["_device_id", "_token"], "additionalProperties": False,
    },
    "reminder_cancel": {
        "type": "object",
        "properties": {**_HIDDEN_PROPERTIES, "reminder_id": {"type": "string", "format": "uuid"}},
        "required": ["_device_id", "_token", "reminder_id"], "additionalProperties": False,
    },
}
_TOOL_DESCRIPTIONS = {

    "reminder_create": "建立此裝置的一次性提醒。time 必填：相對時間使用正數秒數字串（例如 180），日曆時間使用含時區偏移的未來 ISO8601 時間；未指定時區按 Asia/Taipei。到期後最多響鈴 180 秒；固定文字或空白時播報當下本地時間，可立即中止。只建立提醒，不執行物聯網控制。",
    "reminder_list": "列出此裝置的提醒、到期本地時間及狀態，包含已取消、已完成、已停止及已過期的提醒。",
    "reminder_cancel": "取消此裝置的指定提醒；響鈴中的提醒將停止，已結束的提醒保留原狀態。",
}


def create_server(store: Store, token: str) -> Server:
    """Bind tools to a durable Store and an out-of-band transport credential.

    The main server strips hidden transport arguments from LLM schemas and
    overwrites them on each call. Only its authenticated device scope is used.
    """
    if not isinstance(token, str) or not token.strip():
        raise ValueError("Reminder transport credential is required")
    token_bytes = token.encode("utf-8")
    server = Server("reminder-mcp", version="1.0.0")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [Tool(name=name, description=_TOOL_DESCRIPTIONS[name], inputSchema=schema)
                for name, schema in _TOOL_SCHEMAS.items()]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        try:
            if name not in _TOOL_SCHEMAS:
                raise ReminderError("不支援的工具")
            if not isinstance(arguments, Mapping):
                raise ReminderError("工具參數必須是 JSON 物件")
            args = dict(arguments)
            supplied_token = args.pop("_token", None)
            if not isinstance(supplied_token, str) or not hmac.compare_digest(supplied_token.encode("utf-8"), token_bytes):
                raise ReminderError("提醒工具認證失敗")
            device_id = validate_device(args.pop("_device_id", None))
            allowed = _TOOL_SCHEMAS[name]["properties"].keys() - _HIDDEN_PROPERTIES.keys()
            if args.keys() - allowed:
                raise ReminderError("包含不支援的參數")
            if name == "reminder_create":
                if "time" not in args or args["time"] is None:
                    raise ReminderError("提醒工具必須提供 time")
                result = await asyncio.to_thread(store.create, device_id, **args)
            elif name == "reminder_list":
                result = {"reminders": await asyncio.to_thread(store.list, device_id)}
            else:
                if "reminder_id" not in args:
                    raise ReminderError("缺少 reminder_id")
                result = await asyncio.to_thread(store.cancel, device_id, args["reminder_id"])
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False, separators=(",", ":")))])
        except ReminderError as exc:
            return CallToolResult(content=[TextContent(type="text", text=str(exc))], isError=True)
        except Exception:
            # Never expose credentials, endpoint URLs, database paths or SQL.
            return CallToolResult(content=[TextContent(type="text", text="提醒操作暫時無法完成")], isError=True)

    return server


async def _run(store: Store, endpoint: str, notification_url: str, token: str) -> None:
    bridge = EndpointBridge(create_server(store, token), endpoint)
    scheduler = Scheduler(store, notification_url, token)
    tasks = [asyncio.create_task(bridge.run()), asyncio.create_task(scheduler.run())]
    try:
        await asyncio.gather(*tasks)
    finally:
        bridge.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await scheduler.aclose()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    store: Store | None = None
    try:
        endpoint = Path(os.environ.get("REMINDER_ENDPOINT_FILE", "/run/secrets/mcp_endpoint")).read_text(encoding="utf-8").strip()
        token = Path(os.environ.get("REMINDER_TOKEN_FILE", "/run/secrets/reminder_token")).read_text(encoding="utf-8").strip()
        if not endpoint or not token:
            raise ValueError("Missing reminder configuration")
        store = Store(os.environ.get("REMINDER_DATABASE", "/data/reminders.sqlite3"))
        notification_url = os.environ.get("REMINDER_NOTIFICATION_URL", "http://xiaozhi-esp32-server:8006/internal/reminders/sync")
        asyncio.run(_run(store, endpoint, notification_url, token))
    except KeyboardInterrupt:
        pass
    except Exception:
        logging.getLogger(__name__).error("Reminder process could not start or continue; check configuration and storage")
        raise SystemExit(1) from None
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    main()
