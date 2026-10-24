"""MCP tool definitions and long-lived Library dispatch."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from typing import Any

from mcp.server.lowlevel import Server
from mcp.types import CallToolResult, TextContent, Tool

from .library import Library, LibraryError


_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "bedtime_library": {
        "type": "object", "properties": {
            "series": {"type": "string"}, "directory": {"type": "string"},
            "query": {"type": "string"}, "unplayed_only": {"type": "boolean"},
            "subdirectories_only": {"type": "boolean"},
        }, "additionalProperties": False,
    },
    "bedtime_plan": {
        "type": "object", "properties": {
            "series": {"type": "string"}, "minutes": {"type": "number", "exclusiveMinimum": 0},
            "count": {"type": "integer", "minimum": 1}, "start_episode": {"type": "integer", "minimum": 0},
            "bedtime": {"type": "boolean"}, "exclude_paths": {"type": "array", "items": {"type": "string"}},
        }, "additionalProperties": False,
    },
    "bedtime_mark_played": {
        "type": "object", "properties": {
            "series": {"type": "string"}, "start_episode": {"type": "integer", "minimum": 0},
            "end_episode": {"type": "integer", "minimum": 0}, "played": {"type": "boolean"},
        }, "required": ["series", "start_episode"], "additionalProperties": False,
    },
    "bedtime_complete": {
        "type": "object", "properties": {"path": {"type": "string"}},
        "required": ["path"], "additionalProperties": False,
    },
}
_TOOL_DESCRIPTIONS = {
    "bedtime_library": "列出故事系列、子目錄或符合條件的故事檔案。",
    "bedtime_plan": "預覽完整故事計畫；睡前模式會附上睡眠音樂相對路徑。",
    "bedtime_mark_played": "將指定系列集數標記為已聽或未聽。",
    "bedtime_complete": "記錄已完整傳送的故事檔案。",
}


def _validate(name: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("工具參數必須是 JSON 物件")
    args = dict(value)
    schema = _TOOL_SCHEMAS[name]
    properties = schema["properties"]
    extra = set(args) - properties.keys()
    if extra:
        raise ValueError("包含不支援的參數")
    for required in schema.get("required", ()):
        if required not in args:
            raise ValueError(f"缺少必要參數：{required}")
    for key, item in args.items():
        rule = properties[key]
        kind = rule["type"]
        if kind == "array":
            valid = isinstance(item, list) and all(isinstance(entry, rule["items"]["type"]) for entry in item)
        else:
            valid = (isinstance(item, str) if kind == "string" else
                     isinstance(item, bool) if kind == "boolean" else
                     isinstance(item, int) and not isinstance(item, bool) if kind == "integer" else
                     isinstance(item, (int, float)) and not isinstance(item, bool) if kind == "number" else False)
        if not valid:
            raise ValueError(f"參數 {key} 型別錯誤")
        if kind in {"integer", "number"}:
            if "minimum" in rule and item < rule["minimum"]:
                raise ValueError(f"參數 {key} 超出允許範圍")
            if "maximum" in rule and item > rule["maximum"]:
                raise ValueError(f"參數 {key} 超出允許範圍")
            if rule.get("exclusiveMinimum") is not None and item <= rule["exclusiveMinimum"]:
                raise ValueError(f"參數 {key} 超出允許範圍")
        if "enum" in rule and item not in rule["enum"]:
            raise ValueError(f"參數 {key} 值不支援")
    return args


def _safe_error(exc: Exception, library: Library) -> str:
    message = str(exc)
    root = str(library.root)
    if root:
        message = message.replace(root, "媒體資料夾")
    message = re.sub(r"(?:https?|wss?)://\S+", "[已隱藏]", message)
    message = re.sub(r"(?<!\w)/(?:[^\s:]+/)*[^\s:]+", "[已隱藏路徑]", message)
    return message[:500] or "操作無法完成"


def create_server(library: Library) -> Server:
    """Create a protocol server bound to a stable Library."""
    server = Server("bedtime-mcp", version="1.0.0")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [Tool(name=name, description=_TOOL_DESCRIPTIONS[name], inputSchema=schema)
                for name, schema in _TOOL_SCHEMAS.items()]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        if name not in _TOOL_SCHEMAS:
            return CallToolResult(content=[TextContent(type="text", text="不支援的工具")], isError=True)
        try:
            args = _validate(name, arguments or {})
            if name == "bedtime_library":
                result = await asyncio.to_thread(library.catalog, **args)
            elif name == "bedtime_plan":
                bedtime = args.pop("bedtime", False)
                if not isinstance(args.get("exclude_paths", []), list):
                    raise ValueError("參數 exclude_paths 型別錯誤")
                result = await asyncio.to_thread(library.plan, **args)
                if bedtime:
                    sleep = await asyncio.to_thread(library.sleep_file)
                    result["sleep_file"] = {"path": sleep.relative_to(library.root).as_posix()}
            elif name == "bedtime_mark_played":
                result = await asyncio.to_thread(library.mark_played, **args)
            else:
                await asyncio.to_thread(library.complete, args["path"])
                result = {"path": args["path"], "completed": True}
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False, separators=(",", ":")))])
        except (ValueError, LibraryError) as exc:
            return CallToolResult(content=[TextContent(type="text", text=_safe_error(exc, library))], isError=True)
        except Exception:
            return CallToolResult(content=[TextContent(type="text", text="操作暫時無法完成")], isError=True)

    return server


def main() -> None:
    from pathlib import Path
    from .bridge import EndpointBridge
    config = json.loads(Path("/app/config.json").read_text(encoding="utf-8"))
    library = Library(Path(config["source_root"]), Path(config["database"]))
    endpoint = Path(config["endpoint_file"]).read_text(encoding="utf-8").strip()
    try:
        asyncio.run(EndpointBridge(create_server(library), endpoint).run())
    finally:
        library.close()


if __name__ == "__main__":
    main()


