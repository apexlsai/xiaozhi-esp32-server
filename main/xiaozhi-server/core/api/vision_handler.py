import asyncio
import base64
import copy
import json
import re
import time
import uuid
from typing import Optional, Tuple

from aiohttp import web

from config.config_loader import get_private_config_from_api
from config.logger import setup_logging
from core.api.base_handler import BaseHandler
from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO
from core.utils.dialogue import Message
from core.utils.language import (
    LANGUAGE_AGENT_SUFFIXES,
    resolve_language_code_from_agent_name,
)
from core.utils.auth import AuthToken
from core.utils.util import get_image_mime_type, get_vision_url
from core.utils.vllm import create_instance
from core.utils.vision_stream import (
    SentenceBuffer, VISION_TIMEOUT, VISION_UNAVAILABLE_MESSAGE, get_vision_sessions,
)
from plugins_func.register import Action

TAG = __name__

# 设置最大文件大小为5MB
MAX_FILE_SIZE = 5 * 1024 * 1024


class VisionRequestError(ValueError):
    pass


class VisionHandler(BaseHandler):
    def __init__(self, config: dict, websocket_server=None):
        super().__init__(config)
        self.websocket_server = websocket_server
        self.auth = AuthToken(config["server"]["auth_key"])

    def _push_direct_tts(self, device_id: str, text: str, expected_conn=None, expected_sentence_id=None) -> bool:
        websocket_server = getattr(self, "websocket_server", None)
        if websocket_server is None or not isinstance(text, str) or not text.strip():
            return False

        conn = websocket_server.find_device_connection({"device_id": device_id})
        if expected_conn is not None and (
            conn is not expected_conn
            or conn.sentence_id != expected_sentence_id
            or conn.client_abort
        ):
            return False
        if conn is None:
            self.logger.bind(tag=TAG).warning(
                f"MCP Vision 无法推送TTS，设备不在线: {device_id}"
            )
            return False

        mcp_client = getattr(conn, "mcp_client", None)
        if getattr(mcp_client, "call_results", None):
            self.logger.bind(tag=TAG).debug(
                f"MCP Vision 检测到待处理工具调用，跳过直推TTS: {device_id}"
            )
            return False

        if getattr(conn, "need_bind", False) or getattr(conn, "tts", None) is None:
            self.logger.bind(tag=TAG).warning(
                f"MCP Vision 无法推送TTS，设备语音链路未就绪: {device_id}"
            )
            return False

        try:
            content = text.strip()
            sentence_id = uuid.uuid4().hex
            conn.last_activity_time = time.time() * 1000
            conn.sentence_id = sentence_id
            conn.tts.store_tts_text(sentence_id, content)
            conn.tts.tts_text_queue.put(
                TTSMessageDTO(sentence_id, SentenceType.FIRST, ContentType.ACTION)
            )
            conn.tts.tts_one_sentence(
                conn,
                ContentType.TEXT,
                content_detail=content,
                sentence_id=sentence_id,
            )
            conn.tts.tts_text_queue.put(
                TTSMessageDTO(sentence_id, SentenceType.LAST, ContentType.ACTION)
            )
            dialogue = getattr(conn, "dialogue", None)
            if dialogue is not None:
                dialogue.put(Message(role="assistant", content=content))
            self.logger.bind(tag=TAG).info(
                f"MCP Vision 直连结果已推送TTS: device_id={device_id}, chars={len(content)}"
            )
            return True
        except Exception as e:
            self.logger.bind(tag=TAG).error(
                f"MCP Vision 推送TTS失败: device_id={device_id}, error={e}"
            )
            return False

    def _create_error_response(self, message: str) -> dict:
        """创建统一的错误响应格式"""
        return {
            "success": False,
            "action": Action.RESPONSE.name,
            "response": message,
            "message": message,
        }

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
        """验证认证token"""
        # 测试模式：允许特定测试令牌或跳过验证
        auth_header = request.headers.get("Authorization", "")
        client_id = request.headers.get("Client-Id", "")

        # 允许测试客户端跳过认证
        if client_id == "web_test_client":
            device_id = request.headers.get("Device-Id", "test_device")
            return True, device_id

        if not auth_header.startswith("Bearer "):
            return False, None

        token = auth_header[7:]  # 移除"Bearer "前缀
        return self.auth.verify_token(token)

    async def handle_post(self, request):
        """处理 MCP Vision POST 请求"""
        vision_request = None
        sessions = None
        disconnect_watch = None
        deadline = time.monotonic() + VISION_TIMEOUT
        try:
            # 验证token
            is_valid, token_device_id = self._verify_auth_token(request)
            if not is_valid:
                return self._json_response(
                    self._create_error_response("无效的认证token或token已过期"),
                    status=401,
                )

            # 获取请求头信息
            device_id = request.headers.get("Device-Id", "")
            client_id = request.headers.get("Client-Id", "")
            if device_id != token_device_id:
                raise VisionRequestError("设备ID与token不匹配")

            server = getattr(self, "websocket_server", None)
            original_conn = server.find_device_connection({"device_id": device_id}) if server else None
            original_sentence_id = getattr(original_conn, "sentence_id", None)
            original_mcp_pending = bool(getattr(
                getattr(original_conn, "mcp_client", None), "call_results", None
            ))

            question = None
            image_data = None
            request_id = None
            delivery_mode = None
            reader = await request.multipart()
            while True:
                field = await reader.next()
                if field is None:
                    break
                if field.name == "question":
                    raw = await field.read(decode=False)
                    question = raw.decode("utf-8").strip()
                elif field.name in ("file", "image"):
                    image_data = await field.read(decode=False)
                elif field.name in ("request_id", "delivery_mode"):
                    value = (await field.read(decode=False)).decode("utf-8").strip()
                    if field.name == "request_id":
                        request_id = value
                    else:
                        delivery_mode = value

            if not question:
                raise VisionRequestError("缺少问题字段")
            if not image_data:
                raise VisionRequestError("缺少图片文件")

            # 检查文件大小
            if len(image_data) > MAX_FILE_SIZE:
                raise VisionRequestError(
                    f"图片大小超过限制，最大允许{MAX_FILE_SIZE/1024/1024}MB"
                )

            image_mime = get_image_mime_type(image_data)
            if image_mime is None:
                raise VisionRequestError(
                    "不支持的文件格式，请上传有效的图片文件（支持JPEG、PNG、GIF、BMP、TIFF、WEBP格式）"
                )

            # 将图片转换为base64编码
            image_base64 = base64.b64encode(image_data).decode("utf-8")

            if delivery_mode not in (None, "return", "mcp", "push"):
                raise VisionRequestError("不支持的视觉结果交付方式")
            if delivery_mode in ("mcp", "push"):
                if not request_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
                    raise VisionRequestError("缺少或无效的视觉请求编号")
                server = getattr(self, "websocket_server", None)
                conn = server.find_device_connection({"device_id": device_id}) if server else None
                if conn is None or conn.tts is None or getattr(conn, "need_bind", False):
                    raise VisionRequestError("设备语音链路未就绪")
                sessions = get_vision_sessions(conn)
                try:
                    vision_request = (
                        sessions.claim_mcp(request_id) if delivery_mode == "mcp"
                        else await sessions.begin_push(request_id)
                    )
                except ValueError as exc:
                    raise VisionRequestError(str(exc)) from exc
                deadline = vision_request.deadline

            task = asyncio.create_task(self._generate(
                device_id, client_id, question, image_base64, image_mime,
                deadline, sessions, vision_request,
            ))
            if vision_request is not None:
                vision_request.task = task
                if isinstance(request, web.BaseRequest):
                    disconnect_watch = asyncio.create_task(
                        self._watch_disconnect(request, vision_request)
                    )
            result = await asyncio.wait_for(task, max(0, deadline - time.monotonic()))
            return_json = {
                "success": True,
                "action": Action.RESPONSE.name,
                "response": result,
            }
            if vision_request is not None:
                if not sessions.current(vision_request):
                    return self._cancelled_response(vision_request)
                sessions.finish(vision_request, result)
                return_json.update(request_id=request_id, delivery="streamed")
            elif delivery_mode is None and original_conn is not None and not original_mcp_pending:
                self._push_direct_tts(device_id, result, original_conn, original_sentence_id)
            return self._json_response(return_json)
        except asyncio.CancelledError:
            if vision_request is not None:
                if not vision_request.cancelled:
                    await sessions.disconnect(vision_request)
                return self._cancelled_response(vision_request)
            raise
        except VisionRequestError as e:
            self.logger.bind(tag=TAG).warning(f"MCP Vision POST请求无效: {e}")
            if vision_request is not None:
                if not sessions.current(vision_request):
                    return self._cancelled_response(vision_request)
                sessions.emit(vision_request, str(e))
                sessions.finish(vision_request)
                payload = self._create_error_response(str(e))
                payload.update(request_id=vision_request.request_id, delivery="streamed")
                return self._json_response(payload)
            return self._json_response(self._create_error_response(str(e)))
        except Exception as e:
            self.logger.bind(tag=TAG).error(
                f"MCP Vision POST请求异常: request_id={getattr(vision_request, 'request_id', None)} "
                f"{type(e).__name__}: {e}"
            )
            if vision_request is not None:
                if not sessions.current(vision_request):
                    return self._cancelled_response(vision_request)
                payload = self._create_error_response(sessions.fail(vision_request))
                payload.update(request_id=vision_request.request_id, delivery="streamed")
                return self._json_response(payload)
            return self._json_response(self._create_error_response(VISION_UNAVAILABLE_MESSAGE))
        finally:
            if disconnect_watch is not None:
                disconnect_watch.cancel()
                await asyncio.gather(disconnect_watch, return_exceptions=True)

    async def _watch_disconnect(self, http_request, vision_request):
        while not vision_request.finished and not vision_request.cancelled:
            transport = http_request.transport
            if transport is None or transport.is_closing():
                vision_request.task.cancel()
                return
            await asyncio.sleep(0.1)

    def _cancelled_response(self, request):
        return self._json_response({
            "success": False, "action": Action.NONE.name, "response": "",
            "message": "视觉请求已取消", "request_id": request.request_id,
            "delivery": "cancelled",
        })

    async def _generate(
        self, device_id, client_id, question, image_base64, image_mime,
        deadline, sessions, vision_request,
    ):
        current_config = copy.deepcopy(self.config)
        if current_config.get("read_config_from_api", False):
            current_config = await get_private_config_from_api(
                current_config, device_id, client_id,
            )

        select_vllm_module = current_config["selected_module"].get("VLLM")
        if not select_vllm_module:
            raise VisionRequestError("视觉分析服务尚未配置")

        vllm_config = current_config["VLLM"][select_vllm_module]
        vllm_type = vllm_config.get("type", select_vllm_module)
        if not vllm_type:
            raise VisionRequestError("视觉分析服务配置无效")
        vllm = create_instance(vllm_type, vllm_config)

        device_attributes = current_config.get("device_attributes") or {}
        language = resolve_language_code_from_agent_name(
            device_attributes.get("agent_name")
        )
        attribute_language = device_attributes.get("language")
        if language is None and attribute_language in LANGUAGE_AGENT_SUFFIXES:
            language = attribute_language

        kwargs = {
            "image_mime": image_mime, "device_id": device_id,
            "language": language,
            "last_beacon_id": device_attributes.get("last_beacon_id"),
        }
        buffer = SentenceBuffer()
        if vision_request is not None and getattr(vllm, "supports_streaming", False) is True:
            fragments = []
            async for text in vllm.response_stream(
                question, image_base64, deadline=deadline, **kwargs
            ):
                if not sessions.current(vision_request):
                    raise asyncio.CancelledError()
                if not fragments:
                    self.logger.bind(tag=TAG).info(
                        f"[VISION] request_id={vision_request.request_id} "
                        f"first_delta_ms={(time.monotonic() - vision_request.created_at) * 1000:.0f}"
                    )
                fragments.append(text)
                for sentence in buffer.feed(text):
                    sessions.emit(vision_request, sentence)
            result = "".join(fragments)
        else:
            result = await asyncio.to_thread(
                vllm.response, question, image_base64, **kwargs
            )
            if isinstance(result, str) and vision_request is not None:
                for sentence in buffer.feed(result):
                    sessions.emit(vision_request, sentence)

        if not isinstance(result, str) or not result.strip():
            raise RuntimeError("VLLM 返回内容为空或格式无效")

        if vision_request is not None:
            for sentence in buffer.feed("", final=True):
                sessions.emit(vision_request, sentence)
        return result

    async def handle_get(self, request):
        """处理 MCP Vision GET 请求"""
        try:
            vision_explain = get_vision_url(self.config)
            if vision_explain and len(vision_explain) > 0 and "null" != vision_explain:
                message = (
                    f"MCP Vision 接口运行正常，视觉解释接口地址是：{vision_explain}"
                )
            else:
                message = "MCP Vision 接口运行不正常，请打开data目录下的.config.yaml文件，找到【server.vision_explain】，设置好地址"

            response = web.Response(text=message, content_type="text/plain")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"MCP Vision GET请求异常: {e}")
            return_json = self._create_error_response("服务器内部错误")
            response = web.Response(
                text=json.dumps(return_json, separators=(",", ":")),
                content_type="application/json",
            )
        finally:
            self._add_cors_headers(response)
            return response
