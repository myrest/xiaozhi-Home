"""MCP接入点工具执行器"""

from typing import Dict, Any
from ..base import ToolType, ToolDefinition, ToolExecutor
from plugins_func.register import Action, ActionResponse
from .mcp_endpoint_handler import call_mcp_endpoint_tool


_INTERNAL_MCP_TOOLS = frozenset({"bedtime_complete"})
_REMINDER_MCP_TOOLS = frozenset(
    {"reminder_create", "reminder_list", "reminder_cancel"}
)
_REMINDER_CONTEXT_ARGUMENTS = frozenset({"_device_id", "_token"})

class MCPEndpointExecutor(ToolExecutor):
    """MCP接入点工具执行器"""

    def __init__(self, conn):
        self.conn = conn

    async def execute(
        self, conn, tool_name: str, arguments: Dict[str, Any]
    ) -> ActionResponse:
        """执行MCP接入点工具"""
        if tool_name in _INTERNAL_MCP_TOOLS:
            return ActionResponse(
                action=Action.NOTFOUND,
                response="该工具仅供故事播放服务内部记录",
            )
        if tool_name in _REMINDER_MCP_TOOLS:
            notification_service = getattr(
                getattr(conn, "server", None), "notification_service", None
            )
            token = getattr(notification_service, "token", None)
            device_id = getattr(conn, "device_id", None)
            if not isinstance(token, str) or not token or not device_id:
                return ActionResponse(
                    action=Action.ERROR,
                    response="提醒服務或裝置身分尚未就緒",
                )
            arguments = dict(arguments or {})
            arguments["_device_id"] = device_id
            arguments["_token"] = token

        if not hasattr(conn, "mcp_endpoint_client") or not conn.mcp_endpoint_client:
            return ActionResponse(
                action=Action.ERROR,
                response="MCP接入点客户端未初始化",
            )

        if not await conn.mcp_endpoint_client.is_ready():
            return ActionResponse(
                action=Action.ERROR,
                response="MCP接入点客户端未准备就绪",
            )

        try:
            # 转换参数为JSON字符串
            import json

            args_str = json.dumps(arguments) if arguments else "{}"

            # 调用MCP接入点工具
            result = await call_mcp_endpoint_tool(
                conn.mcp_endpoint_client, tool_name, args_str
            )

            resultJson = None
            if isinstance(result, str):
                try:
                    resultJson = json.loads(result)
                except Exception as e:
                    pass

            # 视觉大模型不经过二次LLM处理
            if (
                resultJson is not None
                and isinstance(resultJson, dict)
                and "action" in resultJson
            ):
                return ActionResponse(
                    action=Action[resultJson["action"]],
                    response=resultJson.get("response", ""),
                )

            return ActionResponse(action=Action.REQLLM, result=str(result))

        except ValueError as e:
            return ActionResponse(action=Action.NOTFOUND, response=str(e))
        except Exception as e:
            return ActionResponse(action=Action.ERROR, response=str(e))

    def get_tools(self) -> Dict[str, ToolDefinition]:
        """获取所有MCP接入点工具"""
        if (
            not hasattr(self.conn, "mcp_endpoint_client")
            or not self.conn.mcp_endpoint_client
        ):
            return {}

        tools = {}
        mcp_tools = self.conn.mcp_endpoint_client.get_available_tools()

        for tool in mcp_tools:
            func_def = tool.get("function", {})
            tool_name = func_def.get("name", "")
            if tool_name in _INTERNAL_MCP_TOOLS:
                continue

            description = tool
            if tool_name in _REMINDER_MCP_TOOLS:
                description = dict(tool)
                function_description = dict(func_def)
                parameters = function_description.get("parameters")
                if isinstance(parameters, dict):
                    parameters = dict(parameters)
                    properties = parameters.get("properties")
                    if isinstance(properties, dict):
                        parameters["properties"] = {
                            key: value
                            for key, value in properties.items()
                            if key not in _REMINDER_CONTEXT_ARGUMENTS
                        }
                    required = parameters.get("required")
                    if isinstance(required, list):
                        parameters["required"] = [
                            key
                            for key in required
                            if key not in _REMINDER_CONTEXT_ARGUMENTS
                        ]
                    function_description["parameters"] = parameters
                description["function"] = function_description

            tools[tool_name] = ToolDefinition(
                name=tool_name,
                description=description,
                tool_type=ToolType.MCP_ENDPOINT,
            )

        return tools

    def has_tool(self, tool_name: str) -> bool:
        """检查是否有指定的MCP接入点工具"""
        if tool_name in _INTERNAL_MCP_TOOLS:
            return False
        if (
            not hasattr(self.conn, "mcp_endpoint_client")
            or not self.conn.mcp_endpoint_client
        ):
            return False

        return self.conn.mcp_endpoint_client.has_tool(tool_name)
