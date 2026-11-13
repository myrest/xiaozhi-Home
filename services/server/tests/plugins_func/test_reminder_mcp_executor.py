"""Reminder MCP context stays private from the model and is server-injected."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from core.providers.tools.mcp_endpoint.mcp_endpoint_executor import MCPEndpointExecutor
from plugins_func.register import Action


class ReminderMcpExecutorTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.tool = {
            "type": "function",
            "function": {
                "name": "reminder_create",
                "description": "Create a spoken reminder.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "time": {"type": "string"},
                        "message": {"type": "string"},
                        "_device_id": {"type": "string"},
                        "_token": {"type": "string"},
                    },
                    "required": ["_device_id", "_token", "time"],
                    "additionalProperties": False,
                },
            },
        }
        self.client = SimpleNamespace(
            get_available_tools=lambda: [self.tool],
            has_tool=lambda name: name == "reminder_create",
            is_ready=AsyncMock(return_value=True),
        )
        self.conn = SimpleNamespace(
            server=SimpleNamespace(
                notification_service=SimpleNamespace(token="trusted-token")
            ),
            device_id="authenticated-device",
            mcp_endpoint_client=self.client,
        )
        self.executor = MCPEndpointExecutor(self.conn)

    def test_transport_identity_is_hidden_and_time_is_required(self):
        description = self.executor.get_tools()["reminder_create"].description
        parameters = description["function"]["parameters"]

        self.assertEqual(set(parameters["properties"]), {"time", "message"})
        self.assertEqual(parameters["required"], ["time"])
        self.assertFalse(parameters["additionalProperties"])
        self.assertIn("_device_id", self.tool["function"]["parameters"]["properties"])

    async def test_server_overwrites_model_supplied_transport_identity(self):
        with patch(
            "core.providers.tools.mcp_endpoint.mcp_endpoint_executor.call_mcp_endpoint_tool",
            new=AsyncMock(return_value='{"created":true}'),
        ) as call_tool:
            result = await self.executor.execute(
                self.conn,
                "reminder_create",
                {
                    "time": "30",
                    "_device_id": "attacker-device",
                    "_token": "attacker-token",
                },
            )

        self.assertIs(result.action, Action.REQLLM)
        forwarded = json.loads(call_tool.await_args.args[2])
        self.assertEqual(forwarded["_device_id"], "authenticated-device")
        self.assertEqual(forwarded["_token"], "trusted-token")
        self.assertEqual(forwarded["time"], "30")

    async def test_missing_authenticated_device_fails_without_calling_mcp(self):
        self.conn.device_id = None
        with patch(
            "core.providers.tools.mcp_endpoint.mcp_endpoint_executor.call_mcp_endpoint_tool",
            new=AsyncMock(),
        ) as call_tool:
            result = await self.executor.execute(
                self.conn, "reminder_create", {"time": "30"}
            )

        self.assertIs(result.action, Action.ERROR)
        call_tool.assert_not_awaited()

    def test_mcp_client_preserves_top_level_schema_constraints(self):
        from core.providers.tools.mcp_endpoint.mcp_endpoint_client import (
            MCPEndpointClient,
        )

        schema = dict(self.tool["function"]["parameters"])
        schema["oneOf"] = [{"required": ["time"]}]
        client = MCPEndpointClient()
        client.tools = {
            "reminder_create": {
                "description": "Create a spoken reminder.",
                "inputSchema": schema,
            }
        }
        parameters = client.get_available_tools()[0]["function"]["parameters"]

        self.assertEqual(parameters["oneOf"], [{"required": ["time"]}])
        self.assertFalse(parameters["additionalProperties"])
    def test_gemini_schema_keeps_required_time_argument(self):
        from core.providers.llm.gemini.gemini import LLMProvider

        parameters = self.executor.get_tools()["reminder_create"].description[
            "function"
        ]["parameters"]
        sanitized = LLMProvider._clean_schema(parameters)

        self.assertEqual(sanitized["required"], ["time"])
        self.assertEqual(set(sanitized["properties"]), {"time", "message"})
        self.assertNotIn("oneOf", sanitized)
