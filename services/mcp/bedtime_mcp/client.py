"""Bounded one-shot MCP-over-WebSocket protocol smoke client."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from websockets.asyncio.client import connect


class ProtocolError(RuntimeError):
    """The endpoint did not complete the expected MCP exchange."""


async def _receive_response(websocket: Any, request_id: int, timeout: float) -> dict[str, Any]:
    while True:
        try:
            frame = await asyncio.wait_for(websocket.recv(), timeout=timeout)
        except (TimeoutError, OSError) as exc:
            raise ProtocolError("MCP endpoint timed out or disconnected") from exc
        try:
            message = json.loads(frame)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProtocolError("MCP endpoint returned invalid JSON") from exc
        if not isinstance(message, dict):
            continue
        if message.get("id") != request_id:
            continue
        if "error" in message:
            raise ProtocolError("MCP endpoint rejected a protocol request")
        if "result" not in message or not isinstance(message["result"], dict):
            raise ProtocolError("MCP endpoint returned an invalid response")
        return message["result"]


async def smoke(uri: str, *, tool_name: str = "bedtime_library",
                arguments: dict[str, Any] | None = None, timeout: float = 5.0) -> dict[str, Any]:
    """Perform initialize, initialized, tools/list, and tools/call over websocket."""
    if not isinstance(uri, str) or not uri.startswith(("ws://", "wss://")):
        raise ValueError("A websocket URI is required")
    if timeout <= 0:
        raise ValueError("Timeout must be positive")
    async with connect(uri, open_timeout=timeout, close_timeout=1, ping_interval=None,
                       max_size=2**20) as websocket:
        await websocket.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                         "params": {"protocolVersion": "2025-06-18",
                                                    "capabilities": {},
                                                    "clientInfo": {"name": "bedtime-smoke-client", "version": "1.0"}}}))
        initialized = await _receive_response(websocket, 1, timeout)
        await websocket.send(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        await websocket.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}))
        tools = await _receive_response(websocket, 2, timeout)
        await websocket.send(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                         "params": {"name": tool_name, "arguments": arguments or {}}}))
        called = await _receive_response(websocket, 3, timeout)
    return {"initialize": initialized, "tools": tools, "call": called}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one bounded MCP websocket protocol smoke exchange")
    parser.add_argument("url", help="Injected websocket URL; not read from the endpoint secret file")
    parser.add_argument("--tool", default="bedtime_library")
    parser.add_argument("--arguments", default="{}", help="JSON object of tool arguments")
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()
    try:
        arguments = json.loads(args.arguments)
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object")
        result = asyncio.run(smoke(args.url, tool_name=args.tool,
                                   arguments=arguments, timeout=args.timeout))
    except (ValueError, ProtocolError, OSError, TimeoutError):
        parser.exit(1, "MCP protocol smoke call failed (endpoint details were withheld).\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
