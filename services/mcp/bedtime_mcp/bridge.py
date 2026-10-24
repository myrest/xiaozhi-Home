"""WebSocket transport adapter for an in-process MCP low-level server."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import anyio
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage
from websockets.asyncio.client import connect


class EndpointBridge:
    """Reconnect to an MCP websocket endpoint without recreating app state."""

    def __init__(self, server: Any, uri: str, *, initial_backoff: float = 1.0,
                 maximum_backoff: float = 30.0, open_timeout: float = 10.0):
        self.server = server
        self.uri = uri
        self.initial_backoff = initial_backoff
        self.maximum_backoff = maximum_backoff
        self.open_timeout = open_timeout
        self._stopping = False

    async def run(self) -> None:
        """Maintain the endpoint connection, backing off after any disconnect."""
        delay = self.initial_backoff
        while not self._stopping:
            try:
                async with connect(self.uri, open_timeout=self.open_timeout,
                                   close_timeout=2, ping_interval=20, ping_timeout=20,
                                   max_size=2**20) as websocket:
                    await self.serve_connection(websocket)
                delay = self.initial_backoff
            except asyncio.CancelledError:
                raise
            except Exception:
                # Endpoint URIs commonly contain bearer tokens; never log the URI or exception.
                if self._stopping:
                    break
                await asyncio.sleep(delay)
                delay = min(self.maximum_backoff, max(self.initial_backoff, delay * 2))

    def stop(self) -> None:
        self._stopping = True

    async def serve_connection(self, websocket: Any) -> None:
        """Serve one websocket session through MCP's standard stream runtime."""
        incoming_send, incoming_receive = anyio.create_memory_object_stream[SessionMessage | Exception](32)
        outgoing_send, outgoing_receive = anyio.create_memory_object_stream[SessionMessage](32)

        async def receive_websocket() -> None:
            async with incoming_send:
                async for wire in websocket:
                    if isinstance(wire, bytes):
                        wire = wire.decode("utf-8")
                    message = JSONRPCMessage.model_validate_json(wire)
                    await incoming_send.send(SessionMessage(message=message))

        async def send_websocket() -> None:
            async with outgoing_receive:
                async for wrapped in outgoing_receive:
                    await websocket.send(wrapped.message.model_dump_json(exclude_none=True))

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(self.server.run, incoming_receive, outgoing_send,
                             self.server.create_initialization_options())
            tasks.start_soon(send_websocket)
            try:
                await receive_websocket()
            except (OSError, UnicodeDecodeError, ValueError):
                # Malformed frames end only this session; reconnect starts a fresh MCP session.
                pass
            finally:
                with contextlib.suppress(Exception):
                    await incoming_send.aclose()
                tasks.cancel_scope.cancel()
