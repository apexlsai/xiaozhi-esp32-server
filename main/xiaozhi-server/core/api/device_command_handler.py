import json

from aiohttp import web

from config.logger import setup_logging
from core.handle.deviceEventHandle import (
    LANGUAGE_AGENT_SUFFIXES,
    apply_language_change,
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

        try:
            result = await apply_language_change(conn, language)
        except ValueError as error:
            raise web.HTTPConflict(text=str(error))
        except Exception as error:
            self.logger.bind(tag=TAG).error(f"语言切换换绑失败: {error}")
            raise web.HTTPServiceUnavailable(text=f"language change failed: {error}")

        self.logger.bind(tag=TAG).info(
            "内部接口已完成语言切换换绑: "
            f"{result['deviceId']} {result['currentAgentName']} -> {result['targetAgentName']}"
        )
        return web.json_response(result)

    def _is_authorized(self, request: web.Request) -> bool:
        secret = self.config.get("manager-api", {}).get("secret")
        return bool(secret) and request.headers.get("Authorization") == f"Bearer {secret}"
