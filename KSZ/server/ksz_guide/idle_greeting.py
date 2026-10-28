import asyncio
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from uuid import uuid4

from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO
from core.utils.dialogue import Message
from core.utils.tts_emotion import strip_emotion_markers

from .presence import connection_is_open, is_mqtt_bridge
from .prompt import LANGUAGE_NAMES, response_language
from .settings import _milliseconds
from .tts import tts_complete_sentence


log = logging.getLogger(__name__)


@dataclass
class GreetingTurn:
    session_id: str
    socket: object
    runtime: object = None
    sentence_id: str = field(default_factory=lambda: uuid4().hex)
    cancelled: threading.Event = field(default_factory=threading.Event)
    task: asyncio.Task | None = None

    def owns(self, conn):
        return (conn.session_id == self.session_id and conn.websocket is self.socket
                and conn.sentence_id == self.sentence_id and not conn.client_abort)

    def valid(self, conn):
        device = self.runtime.devices.get(conn.device_id.upper()) if self.runtime else None
        return (not self.cancelled.is_set() and self.owns(conn) and connection_is_open(conn)
                and (self.runtime is None or (device is not None and device.owner is conn))
                and not getattr(conn, "client_have_voice", False)
                and not getattr(conn, "guide_chat_count", 0))


class GreetingSession:
    def __init__(self):
        self.ready = False
        self.listening = False
        self.results = OrderedDict()
        self.last_accepted = float("-inf")
        self.turn = None
        self.min_interval = _milliseconds(os.environ, "KSZ_IDLE_GREETING_MIN_INTERVAL_MS", 120000) / 1000


def greeting_session(conn):
    state = getattr(conn, "ksz_idle_greeting", None)
    if state is None:
        state = conn.ksz_idle_greeting = GreetingSession()
    return state


def cancel_idle_greeting(conn):
    state = getattr(conn, "ksz_idle_greeting", None)
    if state and state.turn:
        state.turn.cancelled.set()


def close_idle_greeting(conn):
    state = getattr(conn, "ksz_idle_greeting", None)
    if state:
        state.ready = False
        cancel_idle_greeting(conn)
        if state.turn and state.turn.task:
            state.turn.task.cancel()


async def interrupt_idle_greeting(conn, message):
    if message.get("type") not in {"listen", "abort"}:
        return
    state = getattr(conn, "ksz_idle_greeting", None)
    if state and message.get("type") == "listen":
        state.listening = message.get("state") == "start"
    turn = state.turn if state else None
    if turn is None or turn.cancelled.is_set():
        return
    cancel_idle_greeting(conn)
    if message.get("type") != "abort" and turn.owns(conn) and conn.client_is_speaking:
        from core.handle.abortHandle import handleAbortMessage

        await handleAbortMessage(conn)


def rejection(conn, state, runtime):
    if not state.ready or not connection_is_open(conn) or getattr(conn, "need_bind", True):
        return "session_not_ready"
    device = runtime.devices.get(conn.device_id.upper()) if runtime else None
    if runtime and (device is None or device.owner is not conn):
        return "session_not_current"
    if is_mqtt_bridge(conn) and (device is None or not device.owner_ready
                                or not getattr(conn, "guide_connection_id", None)
                                or conn.guide_connection_id != device.connection_id):
        return "audio_not_ready"
    if getattr(conn, "tts", None) is None or getattr(conn, "llm", None) is None:
        return "session_not_ready"
    vision = getattr(getattr(conn, "vision_sessions", None), "active", None)
    if (state.listening or getattr(conn, "client_have_voice", False) or getattr(conn, "client_is_speaking", False)
            or getattr(conn, "calling", False) or getattr(conn, "guide_chat_count", 0)
            or (vision is not None and not getattr(vision, "finished", False)
                and not getattr(vision, "cancelled", False))
            or (state.turn and state.turn.task and not state.turn.task.done())
            or (device and (runtime.clock() < device.activity_until or device.pending is not None
                            or (device.announcement_task and not device.announcement_task.done())))):
        return "busy"
    return None


def generate(conn, turn, runtime):
    if not turn.valid(conn):
        return ""
    language = LANGUAGE_NAMES[response_language(conn)]
    rule = (
        f"This is a device-initiated idle greeting in {language}, not a reply to a new user message. "
        "Keep the configured character and speak naturally in one or two short sentences: "
        "greet the visitor or gently invite them to explore or play. Vary the wording using the conversation. "
        "Do not claim the visitor said anything, mention timers or system events, or call tools. "
        "Do not invent places, exhibits, activities, or facts. Without reliable location evidence, "
        "use a general invitation. Output only the words to speak, without emoji or stage directions."
    )
    location = runtime.location(conn.device_id) if runtime else None
    if location:
        rule += "\nVerified current location: " + location.to_prompt_context()
    messages = [dict(item) for item in conn.dialogue.get_llm_dialogue()]
    system = next((item for item in messages if item.get("role") == "system"), None)
    if system is None:
        messages.insert(0, {"role": "system", "content": rule})
    else:
        system["content"] = (system.get("content") or "") + "\n\n" + rule
    messages.append({"role": "user", "content": "[Device event: idle_greeting] Initiate the greeting now."})
    stream = conn.llm.response(conn.session_id, messages, max_tokens=160, extra_body=conn._build_llm_extra_body())
    parts = []
    try:
        for chunk in stream:
            if not turn.valid(conn):
                return ""
            if chunk:
                parts.append(chunk)
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
    return strip_emotion_markers("".join(parts)).strip()


async def speak(conn, turn, runtime):
    queued = False
    started = False
    try:
        text = await asyncio.wrap_future(conn.executor.submit(generate, conn, turn, runtime))
        if not text or not turn.valid(conn):
            return
        from core.handle.sendAudioHandle import send_tts_message

        await send_tts_message(conn, "start")
        started = True
        if not turn.valid(conn):
            return
        conn.tts.store_tts_text(turn.sentence_id, text)
        conn.tts.tts_text_queue.put(TTSMessageDTO(turn.sentence_id, SentenceType.FIRST, ContentType.ACTION))
        tts_complete_sentence(conn, text, turn.sentence_id)
        conn.tts.tts_text_queue.put(TTSMessageDTO(turn.sentence_id, SentenceType.LAST, ContentType.ACTION))
        conn.dialogue.put(Message(role="assistant", content=text))
        queued = True
    except Exception:
        log.exception("Idle greeting failed: session=%s", turn.session_id)
    finally:
        if not queued and turn.owns(conn):
            try:
                if started and not turn.cancelled.is_set() and connection_is_open(conn):
                    await send_tts_message(conn, "stop")
            except Exception:
                log.warning("Idle greeting playback cleanup failed", exc_info=True)
            finally:
                if turn.owns(conn):
                    conn.client_is_speaking = False


async def handle_idle_greeting(conn, message, runtime=None):
    if message.get("event") != "idle_greeting":
        return False
    state = greeting_session(conn)
    request_id = message.get("request_id")
    result = {"type": "device_event_result", "event": "idle_greeting", "request_id": request_id,
              "session_id": conn.session_id, "accepted": False}
    valid_id = isinstance(request_id, str) and 1 <= len(request_id) <= 128
    reason = None
    if not valid_id:
        reason = "invalid_request_id"
    elif message.get("session_id") != conn.session_id:
        reason = "session_mismatch"
    elif request_id in state.results:
        await conn.websocket.send(json.dumps(state.results[request_id], ensure_ascii=False))
        return True
    else:
        reason = rejection(conn, state, runtime)
        if reason is None and time.monotonic() - state.last_accepted < state.min_interval:
            reason = "cooldown"
    turn = None
    if reason:
        result["reason"] = reason
    else:
        state.last_accepted = time.monotonic()
        turn = state.turn = GreetingTurn(conn.session_id, conn.websocket, runtime=runtime)
        conn.sentence_id = turn.sentence_id
        conn.client_abort = False
        conn.client_is_speaking = True
        conn.last_activity_time = time.time() * 1000
        result["accepted"] = True
    if valid_id and message.get("session_id") == conn.session_id:
        state.results[request_id] = result
        while len(state.results) > 64:
            state.results.popitem(last=False)
    try:
        await conn.websocket.send(json.dumps(result, ensure_ascii=False))
    finally:
        if turn:
            if turn.valid(conn):
                turn.task = asyncio.create_task(speak(conn, turn, runtime))
            elif turn.owns(conn):
                conn.client_is_speaking = False
    return True
