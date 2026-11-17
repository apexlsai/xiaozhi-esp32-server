import asyncio
import importlib.util
import json
import queue
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO
from core.utils.dialogue import Dialogue, Message
from ksz_guide.idle_greeting import greeting_session
from ksz_guide.integration import (
    handle_control_message, handle_legacy_event, on_disconnect, on_hello_complete, track_chat,
)
from test_guide import Harness


@pytest_asyncio.fixture
async def conn(monkeypatch):
    monkeypatch.setenv("KSZ_IDLE_GREETING_MIN_INTERVAL_MS", "120000")
    monkeypatch.setitem(sys.modules, "core.utils.util", SimpleNamespace(audio_to_data=AsyncMock()))
    monkeypatch.setitem(sys.modules, "core.utils.audioRateController", SimpleNamespace(AudioRateController=Mock()))
    path = Path(__file__).resolve().parents[3] / "main/xiaozhi-server/core/handle/sendAudioHandle.py"
    spec = importlib.util.spec_from_file_location("ksz_idle_audio_transport_test", path)
    transport = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(transport)
    monkeypatch.setitem(sys.modules, "core.handle.sendAudioHandle", transport)
    h = Harness()
    instance = h.conn()
    instance.server.guide_runtime = None
    instance.device_attributes = {"agent_name": "儿童-汉语"}
    instance.config = {}
    instance.logger = Mock()
    instance.sentence_id = "previous"
    instance.client_abort = False
    instance.client_is_speaking = False
    instance.executor = ThreadPoolExecutor(max_workers=1)
    instance.dialogue = Dialogue()
    instance.dialogue.put(Message(role="system", content="Keep this character."))
    instance.dialogue.put(Message(role="user", content="刚才那个真有意思"))
    instance.dialogue.put(Message(role="assistant", content="下次我们再一起看。"))
    instance.llm = SimpleNamespace(response=Mock(return_value=iter(["你好呀，", "要一起去探索吗？"])))
    instance._build_llm_extra_body = Mock(return_value={"device_id": instance.device_id})
    instance.clear_queues = Mock()
    instance.clearSpeakStatus = lambda: setattr(instance, "client_is_speaking", False)
    instance.tts = SimpleNamespace(tts_text_queue=queue.Queue(), store_tts_text=Mock())
    instance.tts.tts_complete_sentence = lambda conn, text, sentence_id: instance.tts.tts_text_queue.put(
        TTSMessageDTO(sentence_id, SentenceType.MIDDLE, ContentType.TEXT, text)
    )
    on_hello_complete(instance)
    try:
        yield instance
    finally:
        instance.server.guide_runtime = None
        on_disconnect(instance)
        instance.executor.shutdown(wait=True)
        await h.runtime.close()


def request(conn, request_id="boot-greeting-1", **fields):
    return {"type": "device_event", "event": "idle_greeting", "request_id": request_id,
            "session_id": conn.session_id, **fields}


def replies(conn):
    return [json.loads(call.args[0]) for call in conn.websocket.send.await_args_list]


async def finish(conn):
    turn = greeting_session(conn).turn
    if turn and turn.task:
        await turn.task


def queued(conn):
    return list(conn.tts.tts_text_queue.queue)


@pytest.mark.asyncio
async def test_event_generates_one_role_reply_without_a_fake_user_turn(conn):
    before = [(m.role, m.content) for m in conn.dialogue.dialogue]
    assert await handle_legacy_event(conn, request(conn))
    assert replies(conn)[0] == {"type": "device_event_result", "event": "idle_greeting",
                               "request_id": "boot-greeting-1", "session_id": conn.session_id, "accepted": True}
    await finish(conn)
    assert [(m.role, m.content) for m in conn.dialogue.dialogue] == [*before, ("assistant", "你好呀，要一起去探索吗？")]
    assert [(m.sentence_type, m.content_type) for m in queued(conn)] == [
        (SentenceType.FIRST, ContentType.ACTION), (SentenceType.MIDDLE, ContentType.TEXT),
        (SentenceType.LAST, ContentType.ACTION),
    ]
    assert {m.sentence_id for m in queued(conn)} == {conn.sentence_id}
    assert replies(conn)[1] == {"type": "tts", "state": "start", "session_id": conn.session_id}
    args, kwargs = conn.llm.response.call_args
    assert "Keep this character." in args[1][0]["content"]
    assert "Mandarin Chinese" in args[1][0]["content"]
    assert args[1][-1]["content"].startswith("[Device event: idle_greeting]")
    assert "functions" not in kwargs and kwargs["max_tokens"] == 160
    assert conn.dialogue.dialogue[0].content == "Keep this character."


@pytest.mark.asyncio
async def test_duplicates_cooldown_and_bounded_cache(conn):
    await handle_legacy_event(conn, request(conn))
    await handle_legacy_event(conn, request(conn))
    await finish(conn)
    assert replies(conn)[:2] == [replies(conn)[0], replies(conn)[0]]
    conn.llm.response.assert_called_once()
    conn.client_is_speaking = False
    await handle_legacy_event(conn, request(conn, "second"))
    assert replies(conn)[-1]["reason"] == "cooldown"
    state = greeting_session(conn)
    for index in range(70):
        await handle_legacy_event(conn, request(conn, str(index)))
    assert len(state.results) == 64
    state.last_accepted -= 121
    conn.llm.response.return_value = iter(["还想聊点什么吗？"])
    await handle_legacy_event(conn, request(conn, "after-interval"))
    await finish(conn)
    assert conn.llm.response.call_count == 2


@pytest.mark.parametrize("fields,reason", [
    ({"request_id": None}, "invalid_request_id"),
    ({"request_id": ""}, "invalid_request_id"),
    ({"request_id": "x" * 129}, "invalid_request_id"),
    ({"session_id": "old-session"}, "session_mismatch"),
])
@pytest.mark.asyncio
async def test_invalid_requests_do_not_generate(conn, fields, reason):
    await handle_legacy_event(conn, request(conn, **fields))
    assert replies(conn)[-1]["reason"] == reason
    conn.llm.response.assert_not_called()


@pytest.mark.parametrize("busy", ["client_have_voice", "client_is_speaking", "calling", "guide_chat_count", "vision", "listen"])
@pytest.mark.asyncio
async def test_busy_is_rejected_without_queuing(conn, busy):
    if busy == "vision":
        conn.vision_sessions = SimpleNamespace(active=SimpleNamespace(finished=False, cancelled=False))
    elif busy == "listen":
        await handle_control_message(conn, {"type": "listen", "state": "start"})
    else:
        setattr(conn, busy, True)
    await handle_legacy_event(conn, request(conn))
    assert replies(conn)[-1]["reason"] == "busy"
    conn.llm.response.assert_not_called()
    assert not queued(conn)


@pytest.mark.asyncio
async def test_session_and_mqtt_readiness_and_beacon_priority(conn):
    state = greeting_session(conn)
    state.ready = False
    await handle_legacy_event(conn, request(conn, "before-hello"))
    assert replies(conn)[-1]["reason"] == "session_not_ready"
    state.ready = True
    conn.conn_from_mqtt_gateway = True
    await handle_legacy_event(conn, request(conn, "no-udp"))
    assert replies(conn)[-1]["reason"] == "audio_not_ready"
    h = Harness()
    conn.server.guide_runtime = h.runtime
    await h.sync()
    await h.runtime.bind(conn)
    device = h.runtime.state(conn.device_id)
    await handle_legacy_event(conn, request(conn, "legacy-hello"))
    assert replies(conn)[-1]["reason"] == "audio_not_ready"
    await h.runtime.bind(conn, {"session_attempt": 2, "boot_id": "boot1", "connection_id": "connection1"})
    device.owner_ready = False
    await handle_legacy_event(conn, request(conn, "before-ready"))
    assert replies(conn)[-1]["reason"] == "audio_not_ready"
    await h.send("ready", session_id=conn.session_id, session_attempt=2, udp_ready=True)
    assert device.owner_ready
    device.pending = object()
    await handle_legacy_event(conn, request(conn, "beacon-pending"))
    assert replies(conn)[-1]["reason"] == "busy"
    device.pending = None
    await handle_legacy_event(conn, request(conn, "ready"))
    await finish(conn)
    assert conn.llm.response.call_count == 1
    await h.runtime.close()


@pytest.mark.parametrize("change", ["listen", "abort", "disconnect", "replace", "chat", "voice"])
@pytest.mark.asyncio
async def test_interruption_discards_late_generation(conn, change):
    started = threading.Event()
    release = threading.Event()

    def stream(*args, **kwargs):
        started.set()
        assert release.wait(3)
        yield "迟到的问候"

    conn.llm.response.side_effect = stream
    await handle_legacy_event(conn, request(conn))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        if change in {"listen", "abort"}:
            await handle_control_message(conn, {"type": change, "state": "start"})
        elif change == "disconnect":
            on_disconnect(conn)
        elif change == "replace":
            conn.sentence_id = "new-owner"
        elif change == "chat":
            track_chat(lambda instance: None)(conn)
        else:
            conn.client_have_voice = True
    finally:
        release.set()
    await asyncio.gather(greeting_session(conn).turn.task, return_exceptions=True)
    assert not queued(conn)
    assert all(m.content != "迟到的问候" for m in conn.dialogue.dialogue)


@pytest.mark.asyncio
async def test_listen_interrupts_queued_playback(conn):
    await handle_legacy_event(conn, request(conn))
    await finish(conn)
    await handle_control_message(conn, {"type": "listen", "state": "start"})
    conn.clear_queues.assert_called_once()
    assert conn.client_abort and not conn.client_is_speaking
    assert replies(conn)[-1] == {"type": "tts", "state": "stop", "session_id": conn.session_id}


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.asyncio
async def test_failed_or_empty_generation_releases_busy_state(conn, empty):
    if empty:
        conn.llm.response.return_value = iter(())
    else:
        conn.llm.response.side_effect = RuntimeError("offline model")
    await handle_legacy_event(conn, request(conn))
    await finish(conn)
    assert not conn.client_is_speaking
    assert not queued(conn)
    assert len(replies(conn)) == 1


@pytest.mark.asyncio
async def test_rejected_duplicate_stays_rejected_and_wrong_session_cannot_replay(conn):
    conn.client_is_speaking = True
    await handle_legacy_event(conn, request(conn))
    conn.client_is_speaking = False
    await handle_legacy_event(conn, request(conn))
    assert replies(conn)[-1]["reason"] == "busy"
    await handle_legacy_event(conn, request(conn, session_id="old-session"))
    assert replies(conn)[-1]["reason"] == "session_mismatch"
    conn.llm.response.assert_not_called()


def test_interval_configuration(monkeypatch):
    monkeypatch.setenv("KSZ_IDLE_GREETING_MIN_INTERVAL_MS", "300000")
    assert greeting_session(SimpleNamespace()).min_interval == 300
    monkeypatch.setenv("KSZ_IDLE_GREETING_MIN_INTERVAL_MS", "-1")
    with pytest.raises(ValueError, match="KSZ_IDLE_GREETING_MIN_INTERVAL_MS"):
        greeting_session(SimpleNamespace())


@pytest.mark.parametrize("suffix,language", [("英语", "English"), ("日语", "Japanese"), ("韩语", "Korean")])
@pytest.mark.asyncio
async def test_bound_role_language_overrides_attribute_language(conn, suffix, language):
    conn.device_attributes = {"agent_name": f"儿童-{suffix}", "language": "zh-CN"}
    await handle_legacy_event(conn, request(conn))
    await finish(conn)
    assert language in conn.llm.response.call_args.args[1][0]["content"]


@pytest.mark.asyncio
async def test_verified_location_is_optional_and_old_owner_cannot_deliver(conn):
    h = Harness()
    conn.server.guide_runtime = h.runtime
    await h.runtime.bind(conn)
    h.runtime.location = Mock(return_value=SimpleNamespace(to_prompt_context=lambda: "可靠展区"))
    try:
        await handle_legacy_event(conn, request(conn))
        h.runtime.state(conn.device_id).owner = object()
        await finish(conn)
        conn.llm.response.assert_not_called()
        assert not queued(conn)
        h.runtime.state(conn.device_id).owner = conn
        greeting_session(conn).last_accepted = float("-inf")
        await handle_legacy_event(conn, request(conn, "current-owner"))
        await finish(conn)
        assert "Verified current location: 可靠展区" in conn.llm.response.call_args.args[1][0]["content"]
    finally:
        conn.server.guide_runtime = None
        await h.runtime.close()


@pytest.mark.asyncio
async def test_lost_ack_is_idempotent_without_restarting_generation(conn):
    conn.websocket.send.side_effect = [RuntimeError("lost ack"), None, None]
    with pytest.raises(RuntimeError, match="lost ack"):
        await handle_legacy_event(conn, request(conn))
    await finish(conn)
    await handle_legacy_event(conn, request(conn))
    conn.llm.response.assert_called_once()
    assert replies(conn)[-1]["accepted"] is True
