import asyncio
import base64
import copy
import json
from typing import Optional, Tuple

from aiohttp import web

from config.config_loader import get_private_config_from_api
from core.api.base_handler import BaseHandler
from core.utils.auth import AuthToken
from core.utils.language import (
    LANGUAGE_AGENT_SUFFIXES,
    resolve_language_code_from_agent_name,
)
from core.utils.util import get_image_mime_type, get_vision_url
from core.utils.vllm import create_instance
from plugins_func.register import Action


TAG = __name__
MAX_FILE_SIZE = 5 * 1024 * 1024
MAX_QUESTION_SIZE = 8 * 1024
MAX_LANGUAGE_SIZE = 64
READ_CHUNK_SIZE = 64 * 1024


class VisionRequestError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class VisionHandler(BaseHandler):
    def __init__(self, config: dict):
        super().__init__(config)
        self.auth = AuthToken(config["server"]["auth_key"])

    def _create_error_response(self, message: str) -> dict:
        return {"success": False, "message": message}

    def _json_response(self, payload: dict, status: int = 200) -> web.Response:
        response = web.Response(
            text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            content_type="application/json",
            charset="utf-8",
            status=status,
        )
        self._add_cors_headers(response)
        return response

    def _verify_auth_token(self, request) -> Tuple[bool, Optional[str]]:
        auth_header = request.headers.get("Authorization", "")
        client_id = request.headers.get("Client-Id", "")

        if client_id == "web_test_client":
            device_id = request.headers.get("Device-Id", "test_device")
            return True, device_id

        if not auth_header.startswith("Bearer "):
            return False, None

        return self.auth.verify_token(auth_header[7:])

    async def _read_part(self, field, limit: int, too_large_status: int = 400) -> bytes:
        read_chunk = getattr(field, "read_chunk", None)
        if read_chunk is None:
            value = await field.read(decode=False)
            if len(value) > limit:
                raise VisionRequestError("请求字段超过大小限制", too_large_status)
            return value

        value = bytearray()
        while True:
            chunk = await read_chunk(size=READ_CHUNK_SIZE)
            if not chunk:
                break
            if len(value) + len(chunk) > limit:
                raise VisionRequestError("请求字段超过大小限制", too_large_status)
            value.extend(chunk)
        return bytes(value)

    async def _parse_multipart(self, request):
        try:
            reader = await request.multipart()
        except Exception as error:
            raise VisionRequestError("请求必须使用multipart/form-data") from error

        question = None
        language = None
        image_data = None

        while True:
            field = await reader.next()
            if field is None:
                break

            if field.name == "question":
                raw_question = await self._read_part(field, MAX_QUESTION_SIZE)
                try:
                    question = raw_question.decode("utf-8").strip()
                except UnicodeDecodeError as error:
                    raise VisionRequestError("问题字段不是有效的UTF-8文本") from error
            elif field.name == "language":
                raw_language = await self._read_part(field, MAX_LANGUAGE_SIZE)
                try:
                    language = raw_language.decode("utf-8").strip()
                except UnicodeDecodeError as error:
                    raise VisionRequestError("语言字段不是有效的UTF-8文本") from error
            elif field.name in {"file", "image"}:
                if image_data is not None:
                    raise VisionRequestError("只能上传一个图片文件")
                try:
                    image_data = await self._read_part(field, MAX_FILE_SIZE, 413)
                except VisionRequestError as error:
                    if error.status == 413:
                        raise VisionRequestError(
                            "图片大小超过限制，最大允许5MB", 413
                        ) from error
                    raise
            else:
                await self._read_part(field, MAX_QUESTION_SIZE)

        if not question:
            raise VisionRequestError("缺少问题字段")
        if not image_data:
            raise VisionRequestError("缺少图片文件")
        return question, language, image_data

    async def handle_post(self, request):
        is_valid, token_device_id = self._verify_auth_token(request)
        if not is_valid:
            return self._json_response(
                self._create_error_response("无效的认证token或token已过期"), 401
            )

        device_id = request.headers.get("Device-Id", "")
        client_id = request.headers.get("Client-Id", "")
        if not device_id or device_id != token_device_id:
            return self._json_response(self._create_error_response("设备认证信息不匹配"), 403)

        try:
            question, request_language, image_data = await self._parse_multipart(request)
            image_mime = get_image_mime_type(image_data)
            if image_mime is None:
                raise VisionRequestError(
                    "不支持的文件格式，请上传有效的图片文件（支持JPEG、PNG、GIF、BMP、TIFF、WEBP格式）"
                )

            image_base64 = base64.b64encode(image_data).decode("ascii")
            current_config = copy.deepcopy(self.config)
            if current_config.get("read_config_from_api", False):
                try:
                    current_config = await get_private_config_from_api(
                        current_config, device_id, client_id
                    )
                except Exception as error:
                    raise VisionRequestError("视觉服务配置暂时不可用", 503) from error

            selected_vllm = current_config.get("selected_module", {}).get("VLLM")
            vllm_configs = current_config.get("VLLM", {})
            if not selected_vllm or selected_vllm not in vllm_configs:
                raise VisionRequestError("视觉分析模块尚未配置", 503)

            selected_config = vllm_configs[selected_vllm]
            vllm_type = selected_config.get("type", selected_vllm)
            if not vllm_type:
                raise VisionRequestError("视觉分析模块尚未配置", 503)

            device_attributes = current_config.get("device_attributes") or {}
            language = request_language or resolve_language_code_from_agent_name(
                device_attributes.get("agent_name")
            )
            attribute_language = device_attributes.get("language")
            if language is None and attribute_language in LANGUAGE_AGENT_SUFFIXES:
                language = attribute_language

            try:
                vllm = create_instance(vllm_type, selected_config)
                result = await asyncio.to_thread(
                    vllm.response,
                    question,
                    image_base64,
                    image_mime=image_mime,
                    device_id=device_id,
                    language=language,
                    last_beacon_id=device_attributes.get("last_beacon_id"),
                )
            except Exception as error:
                raise VisionRequestError("视觉服务暂时不可用", 503) from error

            if not isinstance(result, str) or not result.strip():
                raise VisionRequestError("视觉服务未返回有效分析结果", 502)

            answer = result.strip()
            return self._json_response(
                {
                    "success": True,
                    "answer": answer,
                    "action": Action.RESPONSE.name,
                    "response": answer,
                }
            )
        except VisionRequestError as error:
            self.logger.bind(tag=TAG).warning(f"MCP Vision请求失败: {error}")
            return self._json_response(self._create_error_response(str(error)), error.status)
        except Exception as error:
            self.logger.bind(tag=TAG).error(f"MCP Vision POST请求异常: {error}")
            return self._json_response(self._create_error_response("处理请求时发生错误"), 500)

    async def handle_get(self, request):
        try:
            vision_explain = get_vision_url(self.config)
            if vision_explain and vision_explain != "null":
                message = f"MCP Vision 接口运行正常，视觉解释接口地址是：{vision_explain}"
            else:
                message = "MCP Vision 接口运行不正常，请配置server.vision_explain"
            response = web.Response(
                text=message, content_type="text/plain", charset="utf-8"
            )
        except Exception as error:
            self.logger.bind(tag=TAG).error(f"MCP Vision GET请求异常: {error}")
            return self._json_response(self._create_error_response("服务器内部错误"), 500)

        self._add_cors_headers(response)
        return response
