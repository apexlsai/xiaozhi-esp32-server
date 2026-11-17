import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.handle.abortHandle import handleAbortMessage
from core.handle.sendAudioHandle import _send_sentence_start, send_tts_message
from ksz_guide.thinking import (
    close_thinking, finish_thinking, send_response_emotion, start_thinking,
)
from test_guide_announcement import conn


@pytest.fixture
def ready(conn):
    conn.features = {"emoji": True}
    conn.websocket = SimpleNamespace(send=AsyncMock(), closed=False)
    conn.clearSpeakStatus = Mock()
    return conn


def wire(conn):
    return [json.loads(call.args[0]) for call in conn.websocket.send.await_args_list]


async def shown(conn):
    conn.loop = asyncio.get_running_loop()
    start_thinking(conn, conn.sentence_id)
    for _ in range(20):
        if conn.websocket.send.await_count:
            return
        await asyncio.sleep(0)
    raise AssertionError("thinking not delivered")


async def test_thinking_precedes_blocked_model_and_does_not_enter_history(ready):
    conn = ready
    conn.loop = asyncio.get_running_loop()
    entered, release, displayed = threading.Event(), threading.Event(), asyncio.Event()
    history = list(conn.dialogue.dialogue)

    async def send(message):
        if json.loads(message).get("emotion") == "thinking":
            displayed.set()

    conn.websocket.send.side_effect = send

    def response(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return iter(["深圳位于广东省。"])

    conn.llm.response.side_effect = response
    task = asyncio.create_task(asyncio.to_thread(conn.chat, "深圳在哪里"))
    try:
        await asyncio.wait_for(displayed.wait(), 3)
        assert entered.wait(1)
        assert not task.done()
        assert wire(conn) == [{"type": "llm", "text": "🤔", "emotion": "thinking", "session_id": conn.session_id}]
    finally:
        release.set()
        await task
    assert len(conn.dialogue.dialogue) > len(history)
    assert all("🤔" not in str(message.content) for message in conn.dialogue.dialogue)
    await _send_sentence_start(conn, "深圳位于广东省。", conn.sentence_id)
    assert [item.get("emotion", item.get("state")) for item in wire(conn)] == ["thinking", "happy", "sentence_start"]


async def test_fast_reply_cancels_pending_display(ready):
    ready.loop = asyncio.get_running_loop()
    start_thinking(ready, ready.sentence_id)
    await finish_thinking(ready, ready.sentence_id)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert wire(ready) == []


@pytest.mark.parametrize("synchronized", [False, True])
async def test_real_emotion_replaces_thinking_without_neutral(ready, synchronized):
    await shown(ready)
    if synchronized:
        await _send_sentence_start(ready, "正文", ready.sentence_id,
                                   SimpleNamespace(emoji="🙂", emotion="happy"))
    else:
        await send_response_emotion(ready, "🙂", "happy", ready.sentence_id)
        await _send_sentence_start(ready, "正文", ready.sentence_id)
    assert [item.get("emotion", item.get("state")) for item in wire(ready)] == ["thinking", "happy", "sentence_start"]


async def test_stop_without_audio_resets_expression(ready):
    await shown(ready)
    await send_tts_message(ready, "stop", sentence_id=ready.sentence_id)
    assert [item.get("emotion", item.get("state")) for item in wire(ready)] == ["thinking", "neutral", "stop"]


async def test_abort_resets_and_late_model_emotion_is_ignored(ready):
    await shown(ready)
    ready.clear_queues = Mock()
    await handleAbortMessage(ready)
    await send_response_emotion(ready, "🙂", "happy", ready.sentence_id)
    assert [item.get("emotion", item.get("state")) for item in wire(ready)] == ["thinking", "neutral", "stop"]


@pytest.mark.parametrize("change", ["response", "socket", "session", "disconnect"])
async def test_old_work_cannot_change_new_session(ready, change):
    await shown(ready)
    old_id = ready.sentence_id
    old_socket = ready.websocket
    if change == "response":
        ready.sentence_id = "new-response"
        start_thinking(ready, ready.sentence_id)
    elif change == "socket":
        ready.websocket = SimpleNamespace(send=AsyncMock(), closed=False)
    elif change == "session":
        ready.session_id = "new-session"
    else:
        ready.websocket.closed = True
        close_thinking(ready)
    await finish_thinking(ready, old_id)
    if change == "response":
        await send_response_emotion(ready, "🙂", "happy", old_id)
        await send_tts_message(ready, "stop", sentence_id=old_id)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert all(json.loads(call.args[0]).get("emotion") == "thinking" for call in old_socket.send.await_args_list)
    if change == "socket":
        assert wire(ready) == []


async def test_emoji_opt_out(ready):
    ready.loop = asyncio.get_running_loop()
    ready.features = {"emoji": False}
    await asyncio.to_thread(ready.chat, "深圳在哪里")
    await asyncio.sleep(0)
    assert wire(ready) == []


@pytest.mark.parametrize("failure", ["setup", "memory", "tools"])
async def test_model_setup_and_unhandled_failure_clear_thinking(ready, failure):
    ready.loop = asyncio.get_running_loop()
    if failure == "setup":
        ready.llm.response.side_effect = RuntimeError("unavailable")
        await asyncio.to_thread(ready.chat, "问题")
    elif failure == "memory":
        ready.memory = SimpleNamespace(query_memory=Mock(side_effect=RuntimeError("unavailable")))
        await asyncio.to_thread(ready.chat, "问题")
    else:
        ready.intent_type = "function_call"
        ready.func_handler.get_functions.side_effect = RuntimeError("unavailable")
        with pytest.raises(RuntimeError):
            await asyncio.to_thread(ready.chat, "问题")
    for _ in range(20):
        await asyncio.sleep(0)
    assert all(item.get("emotion") in {"thinking", "neutral"} for item in wire(ready))
    assert not ready.ksz_thinking.pending
    if wire(ready):
        assert wire(ready)[-1]["emotion"] == "neutral"


async def test_tool_recursion_does_not_restart_thinking(ready):
    await shown(ready)
    await asyncio.to_thread(ready.chat, None, depth=1)
    await asyncio.sleep(0)
    assert len(wire(ready)) == 1
    assert ready.ksz_thinking.pending


async def test_display_failure_does_not_block_reply(ready):
    ready.websocket.send.side_effect = RuntimeError("disconnected")
    await shown(ready)
    await finish_thinking(ready, ready.sentence_id)
    assert not ready.ksz_thinking.pending


async def test_slow_display_cannot_arrive_after_reset(ready):
    ready.loop = asyncio.get_running_loop()
    sending, release = asyncio.Event(), asyncio.Event()
    delivered = []

    async def slow_send(message):
        if json.loads(message).get("emotion") == "thinking":
            sending.set()
            await release.wait()
        delivered.append(json.loads(message))

    ready.websocket.send.side_effect = slow_send
    start_thinking(ready, ready.sentence_id)
    await asyncio.wait_for(sending.wait(), 1)
    finish = asyncio.create_task(finish_thinking(ready, ready.sentence_id))
    await asyncio.sleep(0)
    assert not finish.done()
    release.set()
    await asyncio.wait_for(finish, 1)
    assert [item["emotion"] for item in delivered] == ["thinking", "neutral"]


async def test_automatic_location_narration_does_not_show_thinking(ready):
    from test_guide_announcement import narrate
    ready.loop = asyncio.get_running_loop()
    await asyncio.to_thread(narrate, ready)
    await asyncio.sleep(0)
    assert all(item.get("emotion") != "thinking" for item in wire(ready))


async def test_new_fast_reply_resets_previous_visible_thinking(ready):
    await shown(ready)
    ready.sentence_id = "new-response"
    start_thinking(ready, ready.sentence_id)
    await finish_thinking(ready, ready.sentence_id)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert [item["emotion"] for item in wire(ready)] == ["thinking", "neutral"]


async def test_new_reply_waits_for_previous_inflight_expression(ready):
    ready.loop = asyncio.get_running_loop()
    sending, release = asyncio.Event(), asyncio.Event()
    delivered = []

    async def slow_send(message):
        if json.loads(message).get("emotion") == "thinking":
            sending.set()
            await release.wait()
        delivered.append(json.loads(message))

    ready.websocket.send.side_effect = slow_send
    start_thinking(ready, ready.sentence_id)
    await asyncio.wait_for(sending.wait(), 1)
    ready.sentence_id = "new-response"
    start_thinking(ready, ready.sentence_id)
    finish = asyncio.create_task(finish_thinking(ready, ready.sentence_id))
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(finish, 1)
    await asyncio.sleep(0)
    assert delivered[-1]["emotion"] == "neutral"


async def test_old_cleanup_waiting_on_lock_cannot_clear_new_display(ready):
    await shown(ready)
    lock = ready.ksz_thinking.display.lock
    await lock.acquire()
    old_cleanup = asyncio.create_task(finish_thinking(ready, ready.sentence_id))
    await asyncio.sleep(0)
    ready.sentence_id = "new-response"
    start_thinking(ready, ready.sentence_id)
    lock.release()
    await old_cleanup
    await finish_thinking(ready, ready.sentence_id)
    assert wire(ready)[-1]["emotion"] == "neutral"
