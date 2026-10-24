"""End-to-end MCP protocol checks over ephemeral local websockets."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from websockets.asyncio.server import serve

from bedtime_mcp.bridge import EndpointBridge
from bedtime_mcp.client import smoke
from bedtime_mcp.library import Library
from bedtime_mcp.server import create_server


class _Library:
    def __init__(self, root: Path):
        self.root = root
        self.calls = 0

    def catalog(self, **kwargs: Any) -> dict:
        self.calls += 1
        return {"series": []}

    def plan(self, **kwargs: Any) -> dict:
        self.calls += 1
        return {"series": kwargs.get("series", "故事"), "files": []}

    def mark_played(self, **kwargs: Any) -> dict:
        self.calls += 1
        return {"updated": 1, **kwargs}

    def complete(self, path: str) -> None:
        self.calls += 1


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_and_completion_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            library = _Library(Path(temporary))
            bridge = EndpointBridge(create_server(library), "ws://unused")

            async def handler(websocket: Any) -> None:
                await bridge.serve_connection(websocket)

            async with serve(handler, "127.0.0.1", 0) as endpoint:
                uri = f"ws://127.0.0.1:{endpoint.sockets[0].getsockname()[1]}/"
                result = await smoke(uri, tool_name="bedtime_complete", arguments={"path": "Stories/1.mp3"})
            names = {tool["name"] for tool in result["tools"]["tools"]}
            self.assertEqual(names, {"bedtime_library", "bedtime_plan", "bedtime_mark_played", "bedtime_complete"})
            self.assertEqual(json.loads(result["call"]["content"][0]["text"]), {"path": "Stories/1.mp3", "completed": True})
            self.assertEqual(library.calls, 1)

    async def test_library_tool_returns_nested_directory_listing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "media"
            for name in ("第一期", "第二期"):
                directory = root / "米小圈系列" / name
                directory.mkdir(parents=True)
                (directory / "1 first.mp3").write_bytes(b"fixture media")
            library = Library(root, base / "state" / "library.sqlite3", require_mount=False)
            try:
                bridge = EndpointBridge(create_server(library), "ws://unused")

                async def handler(websocket: Any) -> None:
                    await bridge.serve_connection(websocket)

                async with serve(handler, "127.0.0.1", 0) as endpoint:
                    uri = f"ws://127.0.0.1:{endpoint.sockets[0].getsockname()[1]}/"
                    result = await smoke(uri, tool_name="bedtime_library",
                                         arguments={"directory": "米小圈系列", "subdirectories_only": True})
            finally:
                library.close()
        self.assertFalse(result["call"].get("isError", False))
        output = json.loads(result["call"]["content"][0]["text"])
        self.assertEqual([item["name"] for item in output["subdirectories"]], ["第一期", "第二期"])

    async def test_completion_updates_history_and_rejects_unsafe_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "media"
            story = root / "Stories" / "1 first.mp3"
            story.parent.mkdir(parents=True)
            story.write_bytes(b"fixture media")
            library = Library(root, base / "state" / "library.sqlite3", require_mount=False)
            try:
                bridge = EndpointBridge(create_server(library), "ws://unused")
                async def handler(websocket: Any) -> None:
                    await bridge.serve_connection(websocket)
                async with serve(handler, "127.0.0.1", 0) as endpoint:
                    uri = f"ws://127.0.0.1:{endpoint.sockets[0].getsockname()[1]}/"
                    completed = await smoke(uri, tool_name="bedtime_complete", arguments={"path": "Stories/1 first.mp3"})
                    rejected = await smoke(uri, tool_name="bedtime_complete", arguments={"path": "../outside.mp3"})
                self.assertFalse(completed["call"].get("isError", False))
                self.assertTrue(rejected["call"].get("isError", False))
                record = library.catalog("Stories")["files"][0]
                self.assertTrue(record["played"])
                self.assertEqual(record["play_count"], 1)
            finally:
                library.close()

    async def test_invalid_arguments_are_tool_error_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bridge = EndpointBridge(create_server(_Library(Path(temporary))), "ws://unused")
            async def handler(websocket: Any) -> None:
                await bridge.serve_connection(websocket)
            async with serve(handler, "127.0.0.1", 0) as endpoint:
                result = await smoke(f"ws://127.0.0.1:{endpoint.sockets[0].getsockname()[1]}/",
                                     tool_name="bedtime_library", arguments={"unexpected": True})
            self.assertTrue(result["call"]["isError"])
