import asyncio
import contextvars
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO
from core.utils.dialogue import Message
from plugins_func.register import Action, ActionResponse


VISION_TOOL_NAMES = {"self_camera_take_photo", "self.camera.take_photo"}
VISION_TIMEOUT = 120.0
VISION_UNAVAILABLE_MESSAGE = "视觉服务暂时不可用，请稍后再试。"
VISION_INTERRUPTED_MESSAGE = "识别中断了，请再拍一次。"
vision_call_scope = contextvars.ContextVar("vision_call_scope", default=None)


async def run_vision_tool(conn, call, sentence_id, deadline):
    token = vision_call_scope.set((sentence_id, deadline))
    try:
        if conn.client_abort or conn.sentence_id != sentence_id:
            return ActionResponse(Action.NONE)
        result = await asyncio.wait_for(
            conn.func_handler.handle_llm_function_call(conn, call),
            max(0, deadline - time.monotonic()),
        )
        if result is None or result.action not in (Action.NONE, Action.RESPONSE):
            return fail_vision_tool(conn, sentence_id)
        return result
    except Exception as exc:
        conn.logger.error(f"[VISION] tool_failed: {type(exc).__name__}: {exc}")
        return fail_vision_tool(conn, sentence_id)
    finally:
        vision_call_scope.reset(token)


def fail_vision_tool(conn, sentence_id):
    if conn.client_abort or conn.sentence_id != sentence_id:
        return ActionResponse(Action.NONE)
    sessions = getattr(conn, "vision_sessions", None)
    request = sessions.active if sessions is not None else None
    if request is not None and request.sentence_id == sentence_id:
        if not request.cancelled and not request.finished:
            sessions.fail(request)
        return ActionResponse(Action.NONE)
    return ActionResponse(Action.RESPONSE, response=VISION_UNAVAILABLE_MESSAGE)


class SentenceBuffer:
    def __init__(self):
        self.pending = ""

    def feed(self, text, final=False):
        self.pending += text
        parts = []
        start = 0
        for index, char in enumerate(self.pending):
            boundary = char in "。！？!?；;\n"
            if char == ".":
                following = self.pending[index + 1:index + 2]
                boundary = bool(following and not following.isalnum()) or (
                    final and not following
                )
            if boundary:
                sentence = self.pending[start:index + 1].strip()
                if sentence:
                    parts.append(sentence)
                start = index + 1
        self.pending = self.pending[start:]
        if final and self.pending.strip():
            parts.append(self.pending.strip())
            self.pending = ""
        return parts


@dataclass
class VisionRequest:
    request_id: str
    sentence_id: str
    mode: str
    deadline: float
    created_at: float = field(default_factory=time.monotonic)
    cancelled: bool = False
    finished: bool = False
    claimed: bool = False
    task: object = None
    emitted: list = field(default_factory=list)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    def cancel(self):
        self.cancelled = True
        self.cancel_event.set()
        if self.task is not None and not self.task.done():
            self.task.cancel()


class VisionSessions:
    def __init__(self, conn):
        self.conn = conn
        self.active = None
        self.requests = OrderedDict()
        self.lock = asyncio.Lock()

    def current(self, request):
        return (
            self.active is request
            and not request.cancelled
            and not self.conn.client_abort
            and not self.conn.stop_event.is_set()
            and self.conn.sentence_id == request.sentence_id
        )

    def cancel(self, reason="cancelled"):
        if self.active is not None and not self.active.cancelled:
            request = self.active
            request.cancel()
            self.conn.logger.info(
                f"[VISION] request_id={request.request_id} cancelled={reason}"
            )

    async def disconnect(self, request):
        from core.handle.abortHandle import handleAbortMessage

        if not request.finished and self.current(request):
            await handleAbortMessage(self.conn)

    def _register(self, request):
        if request.request_id in self.requests:
            raise ValueError("重复的视觉请求编号")
        self.cancel("superseded")
        self.active = request
        self.requests[request.request_id] = request
        while len(self.requests) > 128:
            self.requests.popitem(last=False)
        return request

    def begin_mcp(self, sentence_id, deadline):
        if (
            self.conn.client_abort or self.conn.stop_event.is_set()
            or self.conn.sentence_id != sentence_id
        ):
            raise ValueError("视觉请求已取消")
        return self._register(VisionRequest(
            uuid.uuid4().hex, sentence_id, "mcp", deadline
        ))

    def claim_mcp(self, request_id):
        request = self.requests.get(request_id)
        if request is None or request.mode != "mcp":
            raise ValueError("视觉请求未关联到当前设备的拍照工具")
        if request.cancelled or not self.current(request):
            raise ValueError("视觉请求已取消")
        if request.claimed or request.finished:
            raise ValueError("重复的视觉图片上传")
        if time.monotonic() >= request.deadline:
            raise ValueError("视觉请求已超时")
        request.claimed = True
        return request

    async def begin_push(self, request_id):
        from core.handle.abortHandle import handleAbortMessage
        from core.handle.sendAudioHandle import send_tts_message

        async with self.lock:
            if request_id in self.requests:
                raise ValueError("重复的视觉请求编号")
            if self.conn.stop_event.is_set():
                raise ValueError("设备连接已关闭")
            previous_sentence_id = self.conn.sentence_id
            self.cancel("superseded")
            await handleAbortMessage(self.conn)
            if self.conn.sentence_id != previous_sentence_id or self.conn.stop_event.is_set():
                raise ValueError("视觉请求已被新轮次替换")
            self.conn.client_abort = False
            self.conn.sentence_id = uuid.uuid4().hex
            request = self._register(VisionRequest(
                request_id, self.conn.sentence_id, "push",
                time.monotonic() + VISION_TIMEOUT, claimed=True,
            ))
            self.conn.client_is_speaking = True
            await send_tts_message(self.conn, "start")
            if self.current(request):
                self.conn.tts.tts_text_queue.put(TTSMessageDTO(
                    request.sentence_id, SentenceType.FIRST, ContentType.ACTION
                ))
            return request

    def emit(self, request, text):
        if not self.current(request) or request.finished or not text.strip():
            return False
        if not request.emitted:
            self.conn.tool_feedback.cancel_all()
            self.conn.logger.info(
                f"[VISION] request_id={request.request_id} "
                f"first_sentence_ms={(time.monotonic() - request.created_at) * 1000:.0f}"
            )
        request.emitted.append(text)
        self.conn.last_activity_time = time.time() * 1000
        self.conn.tts.store_tts_text(request.sentence_id, "".join(request.emitted))
        complete_sentence = getattr(self.conn.tts, "tts_complete_sentence", None)
        if callable(complete_sentence):
            complete_sentence(self.conn, text, request.sentence_id)
        else:
            self.conn.tts.tts_text_queue.put(TTSMessageDTO(
                request.sentence_id, SentenceType.MIDDLE, ContentType.TEXT,
                content_detail=text,
            ))
        return True

    def finish(self, request, full_text=None):
        if request.finished or not self.current(request):
            return
        request.finished = True
        if request.emitted:
            content = full_text if isinstance(full_text, str) else "".join(request.emitted)
            self.conn.dialogue.put(Message(
                role="assistant", content=content
            ))
        if request.mode == "push":
            self.conn.tts.tts_text_queue.put(TTSMessageDTO(
                request.sentence_id, SentenceType.LAST, ContentType.ACTION
            ))
        self.conn.logger.info(
            f"[VISION] request_id={request.request_id} "
            f"duration_ms={(time.monotonic() - request.created_at) * 1000:.0f} "
            f"sentences={len(request.emitted)}"
        )

    def fail(self, request):
        message = (
            VISION_INTERRUPTED_MESSAGE if request.emitted
            else VISION_UNAVAILABLE_MESSAGE
        )
        self.emit(request, message)
        self.finish(request)
        return message


def get_vision_sessions(conn):
    sessions = getattr(conn, "vision_sessions", None)
    if sessions is None:
        sessions = VisionSessions(conn)
        conn.vision_sessions = sessions
    return sessions
