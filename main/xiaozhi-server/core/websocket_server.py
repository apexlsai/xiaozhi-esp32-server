import asyncio
import json
import logging
import time
from urllib.parse import urlparse

import websockets
from config.logger import setup_logging


class SuppressInvalidHandshakeFilter(logging.Filter):
    """过滤掉无效握手错误日志（如HTTPS访问WS端口）"""

    def filter(self, record):
        msg = record.getMessage()
        suppress_keywords = [
            "opening handshake failed",
            "did not receive a valid HTTP request",
            "connection closed while reading HTTP request",
            "line without CRLF",
        ]
        return not any(keyword in msg for keyword in suppress_keywords)


def _setup_websockets_logger():
    """配置 websockets 相关的所有 logger，过滤无效握手错误"""
    filter_instance = SuppressInvalidHandshakeFilter()
    for logger_name in ["websockets", "websockets.server", "websockets.client"]:
        logger = logging.getLogger(logger_name)
        logger.addFilter(filter_instance)


_setup_websockets_logger()


from core.connection import ConnectionHandler
from config.config_loader import get_config_from_api_async
from core.auth import AuthManager, AuthenticationError
from core.utils.modules_initialize import initialize_modules
from core.utils.util import check_vad_update, check_asr_update

TAG = __name__


class WebSocketServer:
    def __init__(self, config: dict):
        self.config = config
        self.logger = setup_logging(config)
        self.config_lock = asyncio.Lock()
        modules = initialize_modules(
            self.logger,
            self.config,
            "VAD" in self.config["selected_module"],
            "ASR" in self.config["selected_module"],
            "LLM" in self.config["selected_module"],
            False,
            "Memory" in self.config["selected_module"],
            "Intent" in self.config["selected_module"],
        )
        self._vad = modules["vad"] if "vad" in modules else None
        self._asr = modules["asr"] if "asr" in modules else None
        self._llm = modules["llm"] if "llm" in modules else None
        self._intent = modules["intent"] if "intent" in modules else None
        self._memory = modules["memory"] if "memory" in modules else None

        auth_config = self.config["server"].get("auth", {})
        self.auth_enable = auth_config.get("enabled", False)
        # 设备白名单
        self.allowed_devices = set(auth_config.get("allowed_devices", []))
        secret_key = self.config["server"]["auth_key"]
        expire_seconds = auth_config.get("expire_seconds", None)
        self.auth = AuthManager(secret_key=secret_key, expire_seconds=expire_seconds)
        self.device_connections = {}

    async def start(self):
        server_config = self.config["server"]
        host = server_config.get("ip", "0.0.0.0")
        port = int(server_config.get("port", 8000))

        async with websockets.serve(
            self._handle_connection, host, port, process_request=self._http_response
        ):
            await asyncio.Future()

    async def _handle_connection(self, websocket: websockets.ServerConnection):
        request_path = websocket.request.path or ""
        if urlparse(request_path).path == "/dev/ws":
            await self._handle_dev_connection(websocket)
            return

        headers = dict(websocket.request.headers)
        if headers.get("device-id", None) is None:
            # 尝试从 URL 的查询参数中获取 device-id
            from urllib.parse import parse_qs

            # 从 WebSocket 请求中获取路径
            if not request_path:
                self.logger.bind(tag=TAG).error("无法获取请求路径")
                await websocket.close()
                return
            parsed_url = urlparse(request_path)
            query_params = parse_qs(parsed_url.query)
            if "device-id" not in query_params:
                await websocket.send("端口正常，如需测试连接，请启动digital-human测试")
                await websocket.close()
                return
            else:
                websocket.request.headers["device-id"] = query_params["device-id"][0]
            if "client-id" in query_params:
                websocket.request.headers["client-id"] = query_params["client-id"][0]
            if "authorization" in query_params:
                websocket.request.headers["authorization"] = query_params[
                    "authorization"
                ][0]

        """处理新连接，每次创建独立的ConnectionHandler"""
        # 先认证，后建立连接
        try:
            await self._handle_auth(websocket)
        except AuthenticationError:
            await websocket.send("认证失败")
            await websocket.close()
            return
        # 创建ConnectionHandler时传入当前server实例
        handler = ConnectionHandler(
            self.config,
            self._vad,
            self._asr,
            self._llm,
            self._memory,
            self._intent,
            self,  # 传入server实例
        )
        try:
            await handler.handle_connection(websocket)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"处理连接时出错: {e}")
        finally:
            # 强制关闭连接（如果还没有关闭的话）
            try:
                # 安全地检查WebSocket状态并关闭
                if hasattr(websocket, "closed") and not websocket.closed:
                    await websocket.close()
                elif hasattr(websocket, "state") and websocket.state.name != "CLOSED":
                    await websocket.close()
                else:
                    # 如果没有closed属性，直接尝试关闭
                    await websocket.close()
            except Exception as close_error:
                self.logger.bind(tag=TAG).error(
                    f"服务器端强制关闭连接时出错: {close_error}"
                )

    def register_device_connection(self, conn: ConnectionHandler):
        peer = conn.websocket.remote_address
        peer_address = f"{peer[0]}:{peer[1]}" if peer else ""
        self.device_connections[conn] = {
            "device_id": conn.device_id,
            "client_id": conn.headers.get("client-id", conn.device_id),
            "peer_address": peer_address,
            "connected_at": time.time(),
        }
        self.logger.bind(tag=TAG).info(
            f"设备连接已注册: device_id={conn.device_id}, peer={peer_address}"
        )

    def unregister_device_connection(self, conn: ConnectionHandler):
        connection = self.device_connections.pop(conn, None)
        if connection:
            self.logger.bind(tag=TAG).info(
                f"设备连接已移除: device_id={connection['device_id']}, "
                f"peer={connection['peer_address']}"
            )

    def list_device_connections(self):
        connections = [
            {
                "device_id": item["device_id"],
                "client_id": item["client_id"],
                "peer_address": item["peer_address"],
                "connected_at": item["connected_at"],
            }
            for item in self.device_connections.values()
        ]
        return sorted(connections, key=lambda item: item["connected_at"], reverse=True)

    def find_device_connection(self, target: dict):
        device_id = target.get("device_id")
        peer_address = target.get("peer_address")
        if not device_id and not peer_address:
            return None

        matches = [
            (conn, item)
            for conn, item in self.device_connections.items()
            if (device_id and item["device_id"] == device_id)
            or (peer_address and item["peer_address"] == peer_address)
        ]
        if not matches:
            return None
        return max(matches, key=lambda item: item[1]["connected_at"])[0]

    async def _handle_dev_connection(self, websocket: websockets.ServerConnection):
        peer = websocket.remote_address
        peer_address = f"{peer[0]}:{peer[1]}" if peer else ""
        self.logger.bind(tag=TAG).warning(f"开发控制连接已建立: peer={peer_address}")
        try:
            async for message in websocket:
                if not isinstance(message, str):
                    await websocket.send(
                        json.dumps({"ok": False, "error": "仅支持 JSON 文本命令"})
                    )
                    continue
                try:
                    command = json.loads(message)
                except json.JSONDecodeError:
                    await websocket.send(
                        json.dumps({"ok": False, "error": "命令必须是 JSON"})
                    )
                    continue
                response = await self._handle_dev_command(command)
                await websocket.send(json.dumps(response, ensure_ascii=False))
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.logger.bind(tag=TAG).warning(f"开发控制连接已断开: peer={peer_address}")

    async def _handle_dev_command(self, command: dict):
        if not isinstance(command, dict):
            return {"ok": False, "error": "命令必须是 JSON 对象"}

        action = command.get("action")
        if action == "list_connections":
            return {"ok": True, "connections": self.list_device_connections()}
        if action != "speak":
            return {"ok": False, "error": "不支持的 action"}

        text = command.get("text")
        target = command.get("target")
        if not isinstance(text, str) or not text.strip():
            return {"ok": False, "error": "text 必须是非空字符串"}
        if len(text) > 1000:
            return {"ok": False, "error": "text 长度不能超过 1000"}
        if not isinstance(target, dict):
            return {"ok": False, "error": "target 必须包含 device_id 或 peer_address"}

        conn = self.find_device_connection(target)
        if conn is None:
            return {"ok": False, "error": "未找到目标在线设备"}
        if conn.need_bind:
            return {"ok": False, "error": "目标设备尚未完成绑定"}
        if conn.tts is None:
            return {"ok": False, "error": "目标设备语音模块尚未就绪"}

        from core.handle.receiveAudioHandle import startToChat

        await startToChat(conn, text.strip())
        connection = self.device_connections[conn]
        self.logger.bind(tag=TAG).warning(
            f"开发控制已注入播报: device_id={connection['device_id']}, "
            f"peer={connection['peer_address']}"
        )
        return {
            "ok": True,
            "device_id": connection["device_id"],
            "peer_address": connection["peer_address"],
        }

    async def _http_response(self, websocket, request_headers):
        # 检查是否为 WebSocket 升级请求
        if request_headers.headers.get("connection", "").lower() == "upgrade":
            # 如果是 WebSocket 请求，返回 None 允许握手继续
            return None
        else:
            # 如果是普通 HTTP 请求，返回 "server is running"
            return websocket.respond(200, "Server is running\n")

    async def update_config(self) -> bool:
        """更新服务器配置并重新初始化组件

        Returns:
            bool: 更新是否成功
        """
        try:
            async with self.config_lock:
                # 重新获取配置（使用异步版本）
                new_config = await get_config_from_api_async(self.config)
                if new_config is None:
                    self.logger.bind(tag=TAG).error("获取新配置失败")
                    return False
                self.logger.bind(tag=TAG).info(f"获取新配置成功")
                # 检查 VAD 和 ASR 类型是否需要更新
                update_vad = check_vad_update(self.config, new_config)
                update_asr = check_asr_update(self.config, new_config)
                self.logger.bind(tag=TAG).info(
                    f"检查VAD和ASR类型是否需要更新: {update_vad} {update_asr}"
                )
                # 更新配置
                self.config = new_config
                # 重新初始化组件
                modules = initialize_modules(
                    self.logger,
                    new_config,
                    update_vad,
                    update_asr,
                    "LLM" in new_config["selected_module"],
                    False,
                    "Memory" in new_config["selected_module"],
                    "Intent" in new_config["selected_module"],
                )

                # 更新组件实例
                if "vad" in modules:
                    self._vad = modules["vad"]
                if "asr" in modules:
                    self._asr = modules["asr"]
                if "llm" in modules:
                    self._llm = modules["llm"]
                if "intent" in modules:
                    self._intent = modules["intent"]
                if "memory" in modules:
                    self._memory = modules["memory"]
                self.logger.bind(tag=TAG).info(f"更新配置任务执行完毕")
                return True
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"更新服务器配置失败: {str(e)}")
            return False

    async def _handle_auth(self, websocket: websockets.ServerConnection):
        # 先认证，后建立连接
        if self.auth_enable:
            headers = dict(websocket.request.headers)
            device_id = headers.get("device-id", None)
            client_id = headers.get("client-id", None)
            if self.allowed_devices and device_id in self.allowed_devices:
                # 如果属于白名单内的设备，不校验token，直接放行
                return
            else:
                # 否则校验token
                token = headers.get("authorization", "")
                if token.startswith("Bearer "):
                    token = token[7:]  # 移除'Bearer '前缀
                else:
                    raise AuthenticationError("Missing or invalid Authorization header")
                # 进行认证
                auth_success = self.auth.verify_token(
                    token, client_id=client_id, username=device_id
                )
                if not auth_success:
                    raise AuthenticationError("Invalid token")
