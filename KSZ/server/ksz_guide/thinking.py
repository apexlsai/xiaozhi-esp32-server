import asyncio
import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from .presence import connection_is_open


log = logging.getLogger(__name__)
_current = ContextVar("ksz_thinking", default=None)


@dataclass
class Display:
    visible: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class Thinking:
    socket: object
    session_id: str
    response_id: str
    pending: bool = True
    display: Display = field(default_factory=Display)


def _owns(conn, state):
    return (getattr(conn, "ksz_thinking", None) is state
            and conn.websocket is state.socket and conn.session_id == state.session_id
            and conn.sentence_id == state.response_id and connection_is_open(conn))


async def _send(conn, emoji, emotion):
    try:
        await conn.websocket.send(json.dumps({"type": "llm", "text": emoji,
                                             "emotion": emotion, "session_id": conn.session_id}))
        return True
    except Exception:
        log.debug("Thinking expression delivery failed", exc_info=True)
        return False


async def _show(conn, state):
    async with state.display.lock:
        if (state.pending and _owns(conn, state) and not conn.client_abort
                and (conn.features or {}).get("emoji", True)):
            state.display.visible = await _send(conn, "🤔", "thinking")


def _schedule(conn, coroutine):
    loop = getattr(conn, "loop", None)
    if loop is None or not loop.is_running():
        coroutine.close()
        return
    try:
        asyncio.run_coroutine_threadsafe(coroutine, loop)
    except RuntimeError:
        coroutine.close()


def start_thinking(conn, response_id):
    from .announcement import is_guide_announcement
    if (not (getattr(conn, "features", None) or {}).get("emoji", True)
            or is_guide_announcement(conn)):
        return
    state = Thinking(conn.websocket, conn.session_id, response_id)
    previous = getattr(conn, "ksz_thinking", None)
    if previous is not None and previous.socket is conn.websocket and previous.session_id == conn.session_id:
        state.display = previous.display
    conn.ksz_thinking = state
    _current.set((conn, state))
    _schedule(conn, _show(conn, state))


async def finish_thinking(conn, response_id=None, *, restore=True):
    state = getattr(conn, "ksz_thinking", None)
    if state is None or (response_id is not None and state.response_id != response_id):
        return
    async with state.display.lock:
        if not state.pending:
            return
        state.pending = False
        if not _owns(conn, state):
            return
        if (state.display.visible and restore
                and (conn.features or {}).get("emoji", True)):
            await _send(conn, "😶", "neutral")
        state.display.visible = False


def finish_thinking_later(conn, response_id):
    _schedule(conn, finish_thinking(conn, response_id))


def close_thinking(conn):
    state = getattr(conn, "ksz_thinking", None)
    if state is not None:
        state.pending = False
    conn.ksz_thinking = None


async def send_response_emotion(conn, emoji, emotion, response_id):
    from core.utils.textUtils import send_emotion
    if conn.sentence_id != response_id or conn.client_abort:
        return
    await finish_thinking(conn, response_id, restore=False)
    if conn.sentence_id == response_id and not conn.client_abort:
        await send_emotion(conn, emoji, emotion)


@contextmanager
def thinking_scope():
    token = _current.set(None)
    try:
        yield
    except BaseException:
        current = _current.get()
        if current is not None:
            conn, state = current
            finish_thinking_later(conn, state.response_id)
        raise
    finally:
        _current.reset(token)
