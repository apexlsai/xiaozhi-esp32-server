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

    async def handle_legacy_language_change(
        self, request: web.Request
    ) -> web.Response:
        return await self._handle_language_change(request, allow_legacy_test_suffix=True)

    async def handle_language_change(self, request: web.Request) -> web.Response:
        return await self._handle_language_change(request, allow_legacy_test_suffix=False)

    async def _handle_language_change(
        self, request: web.Request, *, allow_legacy_test_suffix: bool
    ) -> web.Response:
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
        dev = data.get("dev", False)
        target_agent_name = data.get("targetAgentName")
        request_id = data.get("requestId")
        if (
            allow_legacy_test_suffix
            and "dev" not in data
            and isinstance(language, str)
            and language.endswith("-test")
            and language[: -len("-test")] in LANGUAGE_AGENT_SUFFIXES
        ):
            language = language[: -len("-test")]
            dev = True
        if not isinstance(device_id, str) or not device_id:
            raise web.HTTPBadRequest(text="deviceId is required")
        if not isinstance(language, str) or language not in LANGUAGE_AGENT_SUFFIXES:
            supported = ", ".join(LANGUAGE_AGENT_SUFFIXES)
            raise web.HTTPBadRequest(
                text=f"language must use a canonical code: {supported}"
            )
        if not isinstance(dev, bool):
            raise web.HTTPBadRequest(text="dev must be a boolean")
        if target_agent_name is not None and (
            not isinstance(target_agent_name, str) or not target_agent_name
        ):
            raise web.HTTPBadRequest(text="targetAgentName must be a non-empty string")

        conn = self.websocket_server.find_device_connection({"device_id": device_id})
        if conn is None:
            raise web.HTTPNotFound(text="device is offline")

        try:
            result = await apply_language_change(
                conn,
                language,
                target_agent_name=target_agent_name,
                request_id=request_id,
                dev=dev,
            )
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
