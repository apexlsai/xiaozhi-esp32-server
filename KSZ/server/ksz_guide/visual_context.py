import asyncio
import math
import os
import threading
import time
from dataclasses import dataclass, field
from uuid import uuid4


def context_enabled():
    return os.environ.get("KSZ_VISION_CONTEXT_ENABLED", "1").lower() in {"1", "true", "yes"}


def context_ttl():
    value = float(os.environ.get("KSZ_VISION_CONTEXT_TTL_SECONDS", "180"))
    if not math.isfinite(value) or value <= 0:
        raise ValueError("KSZ_VISION_CONTEXT_TTL_SECONDS must be positive and finite")
    return value


def recent_history(conn):
    messages = getattr(getattr(conn, "dialogue", None), "dialogue", None)
    if not isinstance(messages, list):
        return []
    history = []
    remaining = 4000
    for message in reversed(messages):
        if (getattr(message, "role", None) not in {"user", "assistant"}
                or getattr(message, "is_temporary", False)
                or getattr(message, "tool_calls", None)):
            continue
        content = getattr(message, "content", None)
        if not isinstance(content, str) or not content.strip():
            continue
        text = content[:min(remaining, 2000)]
        history.append({"role": message.role, "content": text})
        remaining -= len(text)
        if len(history) == 6 or not remaining:
            break
    return list(reversed(history))


@dataclass
class ImageCapture:
    conn: object
    server: object
    device_id: str
    session_id: str
    sentence_id: str
    image_seq: int
    history: list
    started_aborted: bool
    request: object = None
    image_id: str = field(default_factory=lambda: uuid4().hex)
    inactive_id: str = field(default_factory=lambda: uuid4().hex)
    captured_at: float = field(default_factory=time.time)
    received_at: float = field(default_factory=time.monotonic)
    ready: bool = False
    failed: bool = False

    def metadata(self, image_id=None):
        return {
            "session_id": self.session_id,
            "image_id": image_id or self.image_id,
            "image_seq": self.image_seq,
            "captured_at": self.captured_at,
        }


class VisualContext:
    def __init__(self, session_id):
        self.session_id = session_id
        self.ttl = context_ttl()
        self.sequence = 0
        self.capture = None
        self.closed = False
        self.lock = threading.Lock()


def visual_state(conn):
    session_id = getattr(conn, "session_id", None)
    if not context_enabled() or not isinstance(session_id, str) or not session_id:
        return None
    state = getattr(conn, "ksz_visual_context", None)
    if not isinstance(state, VisualContext) or state.session_id != session_id:
        state = VisualContext(session_id)
        conn.ksz_visual_context = state
    return state


def close_visual_context(conn):
    state = getattr(conn, "ksz_visual_context", None)
    if isinstance(state, VisualContext):
        with state.lock:
            state.closed = True
            state.capture = None


def visual_chat_context(conn, values):
    state = visual_state(conn)
    if state is None:
        return values
    result = dict(values)
    with state.lock:
        if state.closed:
            return result
        result["session_id"] = state.session_id
        capture = state.capture
        if capture is not None:
            usable = capture.ready and time.monotonic() - capture.received_at < state.ttl
            result.update(capture.metadata(capture.image_id if usable else capture.inactive_id))
    return result


def begin_visual_request(server, conn, device_id):
    if conn is None:
        return None
    state = visual_state(conn)
    if state is None:
        return None
    with state.lock:
        if (state.closed or conn.stop_event.is_set()
                or server.find_device_connection({"device_id": device_id}) is not conn):
            raise asyncio.CancelledError()
        state.sequence += 1
        capture = ImageCapture(
            conn=conn, server=server, device_id=device_id, session_id=state.session_id,
            sentence_id=conn.sentence_id, image_seq=state.sequence,
            history=recent_history(conn), started_aborted=conn.client_abort,
        )
        state.capture = capture
        return capture


def ensure_visual_request(capture, check_sentence=False):
    if capture is None:
        return
    state = getattr(capture.conn, "ksz_visual_context", None)
    if (not isinstance(state, VisualContext) or state.closed or state.capture is not capture
            or capture.conn.session_id != capture.session_id
            or capture.conn.stop_event.is_set()
            or capture.server.find_device_connection({"device_id": capture.device_id}) is not capture.conn):
        raise asyncio.CancelledError()
    if check_sentence and (
        capture.conn.sentence_id != capture.sentence_id
        or (capture.conn.client_abort and not capture.started_aborted)
    ):
        raise asyncio.CancelledError()


def prepare_visual_request(capture, question, request=None):
    if capture is None:
        return
    ensure_visual_request(capture)
    if request is None and capture.conn.sentence_id != capture.sentence_id:
        raise asyncio.CancelledError()
    capture.request = request
    if request is not None:
        capture.sentence_id = request.sentence_id
    if capture.history and capture.history[-1] == {"role": "user", "content": question.strip()}:
        capture.history.pop()


def visual_request_payload(capture):
    if capture is None:
        return {}
    ensure_visual_request(capture)
    if (capture.conn.sentence_id != capture.sentence_id or capture.conn.client_abort
            or (capture.request is not None and capture.request.cancelled)):
        raise asyncio.CancelledError()
    return {**capture.metadata(), "history": list(capture.history)}


def finish_visual_request(capture, succeeded=False):
    if capture is None:
        return
    state = getattr(capture.conn, "ksz_visual_context", None)
    if not isinstance(state, VisualContext):
        return
    with state.lock:
        capture.history = []
        if capture.ready or capture.failed:
            return capture.ready
        current = (
            state.capture is capture and not state.closed
            and capture.conn.session_id == capture.session_id
            and capture.conn.sentence_id == capture.sentence_id
            and not capture.conn.stop_event.is_set()
            and not capture.conn.client_abort
            and (capture.request is None or not capture.request.cancelled)
            and capture.server.find_device_connection({"device_id": capture.device_id}) is capture.conn
        )
        capture.ready = bool(succeeded and current)
        capture.failed = not capture.ready
        return capture.ready
