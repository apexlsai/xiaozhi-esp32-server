import json

from aiohttp import web

from config.logger import setup_logging
from core.handle.deviceEventHandle import (
    LANGUAGE_AGENT_SUFFIXES,
    resolve_language_agent_name,
)

TAG = __name__


class DeviceCommandHandler:
    def __init__(self, config: dict, websocket_server):
        self.config = config
        self.websocket_server = websocket_server
        self.logger = setup_logging(config)

    async def handle_language_change(self, request: web.Request) -> web.Response:
        if not self._is_authorized(request):
            raise web.HTTPUnauthorized(text="unauthorized")

        try:
            data = await request.json()
        except json.JSONDecodeError:
            raise web.HTTPBadRequest(text="invalid JSON")

        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="request body must be an object")

        device_id = data.get("deviceId")
        language = data.get("language")
        if not isinstance(device_id, str) or not device_id:
            raise web.HTTPBadRequest(text="deviceId is required")
        if not isinstance(language, str) or language not in LANGUAGE_AGENT_SUFFIXES:
            supported = ", ".join(LANGUAGE_AGENT_SUFFIXES)
            raise web.HTTPBadRequest(
                text=f"language must use a canonical code: {supported}"
            )

        conn = self.websocket_server.find_device_connection({"device_id": device_id})
        if conn is None:
            raise web.HTTPNotFound(text="device is offline")

        current_agent_name = (conn.device_attributes or {}).get("agent_name")
        target_agent_name = resolve_language_agent_name(current_agent_name, language)
        if target_agent_name is None:
            raise web.HTTPConflict(
                text="cannot resolve target agent from current agent name"
            )

        payload = {
            "type": "server_command",
            "event": "language_change",
            "payload": {
                "language": language,
                "current_agent_name": current_agent_name,
                "target_agent_name": target_agent_name,
            },
        }
        try:
            await conn.websocket.send(json.dumps(payload, ensure_ascii=False))
        except Exception as error:
            self.logger.bind(tag=TAG).error(f"下发语言切换命令失败: {error}")
            raise web.HTTPServiceUnavailable(text="failed to send device command")

        self.logger.bind(tag=TAG).info(
            f"已下发语言切换命令: {device_id} {current_agent_name} -> {target_agent_name}"
        )
        return web.json_response(
            {
                "deviceId": device_id,
                "language": language,
                "currentAgentName": current_agent_name,
                "targetAgentName": target_agent_name,
            }
        )

    def _is_authorized(self, request: web.Request) -> bool:
        secret = self.config.get("manager-api", {}).get("secret")
        return bool(secret) and request.headers.get("Authorization") == f"Bearer {secret}"
