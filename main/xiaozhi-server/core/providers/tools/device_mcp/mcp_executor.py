"""设备端MCP工具执行器"""

import json
import asyncio
import time
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from ..base import ToolType, ToolDefinition, ToolExecutor
from plugins_func.register import Action, ActionResponse
from .mcp_handler import call_mcp_tool
from core.utils.vision_stream import (
    VISION_TOOL_NAMES, VISION_TIMEOUT, VISION_UNAVAILABLE_MESSAGE,
    get_vision_sessions, vision_call_scope,
)


class DeviceMCPExecutor(ToolExecutor):
    """设备端MCP工具执行器"""

    def __init__(self, conn):
        self.conn = conn

    @staticmethod
    def _error_result(tool_name: str, message: str) -> ActionResponse:
        if tool_name in VISION_TOOL_NAMES:
            return ActionResponse(Action.RESPONSE, response=VISION_UNAVAILABLE_MESSAGE)
        return ActionResponse(
            action=Action.REQLLM,
            result=json.dumps(
                {"status": "error", "tool": tool_name, "message": message},
                ensure_ascii=False,
            ),
        )

    @staticmethod
    def _vision_result(result_json) -> ActionResponse:
        if isinstance(result_json, dict) and "content" in result_json:
            if result_json.get("isError") is True:
                return ActionResponse(Action.RESPONSE, response=VISION_UNAVAILABLE_MESSAGE)
            content = result_json.get("content")
            if isinstance(content, list):
                result_json = "\n".join(
                    item["text"] for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                    and isinstance(item.get("text"), str)
                )
        if isinstance(result_json, str):
            text = result_json.strip()
            if not text or text.startswith(("VLLM 请求失败", "VLLM请求失败", "Traceback", "HTTP error")):
                return ActionResponse(Action.RESPONSE, response=VISION_UNAVAILABLE_MESSAGE)
            if not text.startswith(("{", "[")):
                return ActionResponse(Action.RESPONSE, response=text)
            try:
                result_json = json.loads(text)
            except json.JSONDecodeError:
                return ActionResponse(Action.RESPONSE, response=VISION_UNAVAILABLE_MESSAGE)
        if not isinstance(result_json, dict):
            return ActionResponse(Action.RESPONSE, response=VISION_UNAVAILABLE_MESSAGE)
        if result_json.get("isError") is True:
            return ActionResponse(Action.RESPONSE, response=VISION_UNAVAILABLE_MESSAGE)
        payload = result_json.get("vision_analysis")
        if not isinstance(payload, dict):
            payload = result_json

        if result_json.get("success") is False or payload.get("success") is False:
            return ActionResponse(
                action=Action.RESPONSE,
                response=VISION_UNAVAILABLE_MESSAGE,
            )

        if payload.get("action") == Action.RESPONSE.name or payload.get("success") is True:
            response = payload.get("response")
            if isinstance(response, str) and response.strip():
                return ActionResponse(
                    action=Action.RESPONSE,
                    response=response.strip(),
                )

        return ActionResponse(
            action=Action.RESPONSE,
            response=VISION_UNAVAILABLE_MESSAGE,
        )

    async def execute(
        self, conn: "ConnectionHandler", tool_name: str, arguments: Dict[str, Any]
    ) -> ActionResponse:
        """执行设备端MCP工具"""
        if not hasattr(conn, "mcp_client") or not conn.mcp_client:
            return self._error_result(
                tool_name, "设备能力尚未初始化，请稍后重试"
            )

        if not await conn.mcp_client.is_ready():
            return self._error_result(
                tool_name, "设备能力尚未准备就绪，请稍后重试"
            )

        if tool_name in VISION_TOOL_NAMES:
            return await self._execute_camera(conn, tool_name, arguments)

        try:
            # 转换参数为JSON字符串
            args_str = json.dumps(arguments) if arguments else "{}"

            # 调用设备端MCP工具
            result = await call_mcp_tool(conn, conn.mcp_client, tool_name, args_str)

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

        except ValueError:
            return self._error_result(tool_name, "当前设备不支持这个操作")
        except TimeoutError:
            return self._error_result(tool_name, "设备响应超时，请稍后重试")
        except Exception:
            return self._error_result(tool_name, "设备操作失败，请稍后重试")

    async def _execute_camera(self, conn, tool_name, arguments):
        scope = vision_call_scope.get()
        sentence_id, deadline = scope or (
            getattr(conn, "sentence_id", None), time.monotonic() + VISION_TIMEOUT
        )
        if getattr(conn, "client_abort", False) or (
            sentence_id is not None and conn.sentence_id != sentence_id
        ):
            return ActionResponse(Action.NONE)
        sessions = None
        request = None
        rpc_task = None
        cancelled_task = None
        try:
            metadata = None
            if (getattr(conn, "features", None) or {}).get("vision_stream") is True:
                sessions = get_vision_sessions(conn)
                request = sessions.begin_mcp(sentence_id, deadline)
                metadata = {"vision_request_id": request.request_id}
            rpc_task = asyncio.create_task(call_mcp_tool(
                conn, conn.mcp_client, tool_name,
                json.dumps(arguments or {}, ensure_ascii=False),
                timeout=max(0, deadline - time.monotonic()),
                metadata=metadata, return_raw=True,
            ))
            if request is not None:
                cancelled_task = asyncio.create_task(request.cancel_event.wait())
                await asyncio.wait(
                    (rpc_task, cancelled_task), return_when=asyncio.FIRST_COMPLETED
                )
                if request.cancelled or not sessions.current(request):
                    return ActionResponse(Action.NONE)
            result = await rpc_task
            if sentence_id is not None and (
                getattr(conn, "client_abort", False) or conn.sentence_id != sentence_id
            ):
                return ActionResponse(Action.NONE)
            if request is not None and request.finished:
                return ActionResponse(Action.NONE)
            if request is not None and request.claimed:
                sessions.fail(request)
                return ActionResponse(Action.NONE)
            if request is not None:
                request.finished = True
            return self._vision_result(result)
        except asyncio.CancelledError:
            if request is not None:
                if time.monotonic() >= deadline and sessions.current(request):
                    sessions.fail(request)
                else:
                    request.cancel()
            raise
        except Exception as exc:
            logger = getattr(conn, "logger", None)
            if logger is not None:
                logger.error(f"[VISION] camera_rpc_failed: {type(exc).__name__}: {exc}")
            if request is not None:
                if not sessions.current(request) or request.finished:
                    return ActionResponse(Action.NONE)
                sessions.fail(request)
                return ActionResponse(Action.NONE)
            return self._error_result(tool_name, VISION_UNAVAILABLE_MESSAGE)
        finally:
            for task in (rpc_task, cancelled_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (rpc_task, cancelled_task) if task is not None),
                return_exceptions=True,
            )
            if request is not None and request.task is not None and not request.task.done():
                request.task.cancel()

    def get_tools(self) -> Dict[str, ToolDefinition]:
        """获取所有设备端MCP工具"""
        if not hasattr(self.conn, "mcp_client") or not self.conn.mcp_client:
            return {}

        tools = {}
        mcp_tools = self.conn.mcp_client.get_available_tools()

        for tool in mcp_tools:
            func_def = tool.get("function", {})
            tool_name = func_def.get("name", "")

            if tool_name:
                tools[tool_name] = ToolDefinition(
                    name=tool_name, description=tool, tool_type=ToolType.DEVICE_MCP
                )

        return tools

    def has_tool(self, tool_name: str) -> bool:
        """检查是否有指定的设备端MCP工具"""
        if not hasattr(self.conn, "mcp_client") or not self.conn.mcp_client:
            return False

        return self.conn.mcp_client.has_tool(tool_name)
