import asyncio
import json
import queue
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from core.api.vision_handler import VisionHandler
from core.providers.vllm.openai import VLLMProvider
from core.utils.dialogue import Dialogue, Message
from core.utils.vision_stream import get_vision_sessions
from ksz_guide.integration import filter_context, on_disconnect
from ksz_guide.visual_context import (
    begin_visual_request, finish_visual_request, recent_history,
    prepare_visual_request, visual_request_payload, visual_state,
)


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("KSZ_VISION_CONTEXT_ENABLED", "1")
    monkeypatch.setenv("KSZ_VISION_CONTEXT_TTL_SECONDS", "180")
    conn = SimpleNamespace(
        device_id="2e:b8:76:52:f4:60", session_id="session-1", sentence_id="turn-1",
        client_abort=False, client_is_speaking=False, stop_event=threading.Event(),
        dialogue=Dialogue(), logger=Mock(), tool_feedback=Mock(), features={"vision_stream": True},
        device_attributes={"language": "zh-CN"}, config={}, need_bind=False,
        websocket=SimpleNamespace(send=AsyncMock()), clear_queues=Mock(), clearSpeakStatus=Mock(),
        mcp_client=SimpleNamespace(call_results={}),
        tts=SimpleNamespace(tts_text_queue=queue.Queue(), tts_complete_sentence=Mock(),
                            store_tts_text=Mock(), tts_one_sentence=Mock()),
    )
    server = SimpleNamespace(find_device_connection=Mock(return_value=conn))
    conn.server = server
    handler = object.__new__(VisionHandler)
    handler.websocket_server = server
    handler.logger = Mock()
    handler.config = {
        "read_config_from_api": False,
        "selected_module": {"VLLM": "test"},
        "VLLM": {"test": {"type": "test"}},
    }
    handler._verify_auth_token = Mock(return_value=(True, conn.device_id))
    return conn, server, handler


def begin(setup, question="这是什么？", request=None):
    conn, server, _ = setup
    capture = begin_visual_request(server, conn, conn.device_id)
    prepare_visual_request(capture, question, request)
    return capture


def chat(conn):
    return filter_context(conn, {"device_id": conn.device_id, "language": "zh-CN"})


def upload(conn, mode, request_id=None):
    fields = [("question", "这是什么展品？".encode()), ("image", b"\xff\xd8\xffimage")]
    if mode is not None:
        fields.append(("delivery_mode", mode.encode()))
    if request_id:
        fields.append(("request_id", request_id.encode()))
    reader = SimpleNamespace(next=AsyncMock(side_effect=[
        *[SimpleNamespace(name=name, read=AsyncMock(return_value=value)) for name, value in fields], None,
    ]))
    return SimpleNamespace(headers={"Device-Id": conn.device_id, "Client-Id": "test"},
                           multipart=AsyncMock(return_value=reader))


def test_text_and_photo_share_session_and_successful_image(setup):
    conn, _, _ = setup
    conn.dialogue.put(Message("user", "请介绍这幅画"))
    conn.dialogue.put(Message("assistant", "图中是兰草与奇石。"))
    conn.dialogue.put(Message("user", "这是什么？"))
    assert chat(conn)["session_id"] == "session-1"
    assert "image_id" not in chat(conn)
    capture = begin(setup)
    pending = chat(conn)
    assert pending["image_id"] != capture.image_id
    assert finish_visual_request(capture, succeeded=True)
    conn.sentence_id = "followup"
    assert chat(conn) == {"device_id": conn.device_id, "language": "zh-CN", **capture.metadata()}
    assert chat(conn) == chat(conn)


def test_provider_sends_recent_text_before_current_image(setup):
    conn, _, _ = setup
    conn.dialogue.put(Message("system", "role"))
    conn.dialogue.put(Message("user", "刚才那幅画"))
    conn.dialogue.put(Message("assistant", "兰草与奇石。"))
    conn.dialogue.put(Message("user", "这是什么？"))
    capture = begin(setup)
    provider = VLLMProvider({"model_name": "museum-guide-vision", "base_url": "http://agent.test/v1", "api_key": "test"})
    payload = provider._request_payload("这是什么？", "photo", "image/jpeg", language="zh-CN", **visual_request_payload(capture))
    assert {key: payload[key] for key in capture.metadata()} == capture.metadata()
    assert payload["messages"][:2] == [
        {"role": "user", "content": "刚才那幅画"}, {"role": "assistant", "content": "兰草与奇石。"},
    ]
    assert len(payload["messages"]) == 3
    assert payload["messages"][-1]["content"][1]["image_url"]["url"] == "data:image/jpeg;base64,photo"


def test_history_is_bounded_and_omits_tools_and_temporary_messages(setup):
    conn, _, _ = setup
    for index in range(12):
        conn.dialogue.put(Message("user" if index % 2 else "assistant", str(index)))
    conn.dialogue.put(Message("assistant", "temporary", is_temporary=True))
    conn.dialogue.put(Message("assistant", "tool", tool_calls=[{"id": "tool"}]))
    conn.dialogue.put(Message("tool", "secret tool result"))
    conn.dialogue.put(Message("user", [{"type": "image_url", "image_url": {"url": "old-image"}}]))
    assert [item["content"] for item in recent_history(conn)] == list(map(str, range(6, 12)))
    conn.dialogue.put(Message("assistant", "x" * 9000))
    conn.dialogue.put(Message("user", "y" * 9000))
    assert sum(len(item["content"]) for item in recent_history(conn)) == 4000


def test_new_photo_supersedes_old_even_when_old_finishes_late(setup):
    conn, _, _ = setup
    old = begin(setup)
    current = begin(setup)
    assert current.image_seq == old.image_seq + 1
    assert current.image_id != old.image_id
    assert not finish_visual_request(old, succeeded=True)
    assert chat(conn)["image_id"] not in {old.image_id, current.image_id}
    assert finish_visual_request(current, succeeded=True)
    finish_visual_request(old)
    assert chat(conn)["image_id"] == current.image_id


def test_failed_new_image_never_falls_back_to_previous_photo(setup):
    conn, _, _ = setup
    old = begin(setup)
    finish_visual_request(old, succeeded=True)
    current = begin(setup)
    finish_visual_request(current)
    assert chat(conn)["image_id"] not in {old.image_id, current.image_id}
    assert not finish_visual_request(current, succeeded=True)


@pytest.mark.parametrize("change", ["abort", "turn", "stop", "connection", "session", "request"])
def test_late_result_cannot_activate_changed_or_cancelled_connection(setup, change):
    conn, server, _ = setup
    request = SimpleNamespace(cancelled=False, sentence_id=conn.sentence_id)
    capture = begin(setup, request=request)
    if change == "abort":
        conn.client_abort = True
    elif change == "turn":
        conn.sentence_id = "new-turn"
    elif change == "stop":
        conn.stop_event.set()
    elif change == "connection":
        server.find_device_connection.return_value = SimpleNamespace(session_id="new-connection")
    elif change == "session":
        conn.session_id = "new-session"
    else:
        request.cancelled = True
    assert not finish_visual_request(capture, succeeded=True)
    assert chat(conn).get("image_id") != capture.image_id


def test_finished_image_survives_speech_abort_and_expires_from_receipt(setup):
    conn, _, _ = setup
    request = SimpleNamespace(cancelled=False, sentence_id=conn.sentence_id)
    capture = begin(setup, request=request)
    finish_visual_request(capture, succeeded=True)
    request.cancelled = True
    conn.client_abort = True
    assert chat(conn)["image_id"] == capture.image_id
    with patch("ksz_guide.visual_context.time.monotonic", return_value=capture.received_at + 180):
        assert chat(conn)["image_id"] == capture.inactive_id


def test_disconnect_clears_reference_and_reconnect_starts_new_sequence(setup):
    conn, _, _ = setup
    capture = begin(setup)
    finish_visual_request(capture, succeeded=True)
    on_disconnect(conn)
    assert "image_id" not in chat(conn)
    assert "session_id" not in chat(conn)
    assert visual_state(conn).capture is None
    conn.session_id = "session-2"
    assert chat(conn)["session_id"] == "session-2"
    assert "image_id" not in chat(conn)
    new = begin(setup)
    assert new.image_seq == 1
    assert new.image_id != capture.image_id


def test_disabled_context_preserves_legacy_payload(setup, monkeypatch):
    conn, _, _ = setup
    monkeypatch.setenv("KSZ_VISION_CONTEXT_ENABLED", "0")
    assert begin(setup) is None
    values = {"device_id": conn.device_id}
    assert filter_context(conn, values) is values


@pytest.mark.parametrize("ttl", ["0", "-1", "nan", "inf", "bad"])
def test_invalid_ttl_is_rejected(setup, monkeypatch, ttl):
    monkeypatch.setenv("KSZ_VISION_CONTEXT_TTL_SECONDS", ttl)
    with pytest.raises(ValueError):
        visual_state(setup[0])


@pytest.mark.parametrize("mode", ["mcp", "push", "return", None])
@pytest.mark.asyncio
async def test_http_upload_joins_next_chat_without_client_changes(setup, mode):
    conn, _, handler = setup
    sessions = get_vision_sessions(conn)
    request_id = sessions.begin_mcp(conn.sentence_id, time.monotonic() + 120).request_id if mode == "mcp" else "client-photo"
    received = []

    class Provider:
        supports_streaming = True

        async def response_stream(self, question, image, **kwargs):
            received.append(kwargs)
            yield "这是兰石图。"

        def response(self, question, image, **kwargs):
            received.append(kwargs)
            return "这是兰石图。"

    with patch("core.api.vision_handler.create_instance", return_value=Provider()):
        response = await handler.handle_post(upload(conn, mode, request_id))
    assert json.loads(response.text)["success"]
    assert received[0]["session_id"] == conn.session_id
    assert received[0]["image_id"] != request_id
    assert received[0]["image_seq"] == 1
    assert chat(conn)["image_id"] == received[0]["image_id"]
    assert "history" not in json.loads(response.text)


@pytest.mark.asyncio
async def test_upload_failure_does_not_attach_image(setup):
    conn, _, handler = setup
    provider = Mock()
    provider.response.side_effect = RuntimeError("upstream failed")
    with patch("core.api.vision_handler.create_instance", return_value=provider):
        response = await handler.handle_post(upload(conn, "return"))
    assert not json.loads(response.text)["success"]
    capture = visual_state(conn).capture
    assert capture.failed
    assert chat(conn)["image_id"] == capture.inactive_id


@pytest.mark.asyncio
async def test_upload_cannot_move_to_reconnected_device_during_body_read(setup):
    conn, server, handler = setup
    request = upload(conn, "push", "photo")
    async def reader():
        server.find_device_connection.return_value = SimpleNamespace(session_id="other")
        return request.multipart.return_value

    request.multipart.side_effect = reader
    with patch("core.api.vision_handler.create_instance") as factory:
        response = await handler.handle_post(request)
    assert not json.loads(response.text)["success"]
    factory.assert_not_called()
    capture = visual_state(conn).capture
    assert capture.failed
    assert chat(conn)["image_id"] == capture.inactive_id


@pytest.mark.asyncio
async def test_concurrent_legacy_upload_does_not_publish_old_result(setup):
    conn, _, handler = setup
    started, release = threading.Event(), threading.Event()

    class SlowProvider:
        def response(self, question, image, **kwargs):
            started.set()
            assert release.wait(5)
            return "旧图片。"

    with patch("core.api.vision_handler.create_instance", return_value=SlowProvider()):
        task = asyncio.create_task(handler.handle_post(upload(conn, "return")))
        assert await asyncio.to_thread(started.wait, 5)
        newer = begin(setup)
        finish_visual_request(newer, succeeded=True)
        release.set()
        response = await task
    assert json.loads(response.text)["delivery"] == "cancelled"
    assert chat(conn)["image_id"] == newer.image_id


@pytest.mark.asyncio
async def test_slow_old_upload_cannot_replace_newer_photo(setup):
    conn, _, handler = setup
    started, release = asyncio.Event(), asyncio.Event()
    old_request = upload(conn, "push", "old-upload")
    reader = old_request.multipart.return_value

    async def delay_body():
        started.set()
        await release.wait()
        return reader

    old_request.multipart.side_effect = delay_body
    provider = Mock()
    provider.response.return_value = "新的图片。"
    with patch("core.api.vision_handler.create_instance", return_value=provider):
        old_task = asyncio.create_task(handler.handle_post(old_request))
        await started.wait()
        response = await handler.handle_post(upload(conn, "return"))
        assert json.loads(response.text)["success"]
        current = chat(conn)
        assert current["image_seq"] == 2
        release.set()
        old_response = await old_task
    assert json.loads(old_response.text)["delivery"] == "cancelled"
    assert chat(conn) == current
    provider.response.assert_called_once()


@pytest.mark.asyncio
async def test_cancelled_stream_does_not_activate_partial_image(setup):
    conn, _, handler = setup
    started, release = asyncio.Event(), asyncio.Event()

    class Provider:
        supports_streaming = True

        async def response_stream(self, question, image, **kwargs):
            yield "画面中有兰草。"
            started.set()
            await release.wait()

    with patch("core.api.vision_handler.create_instance", return_value=Provider()):
        task = asyncio.create_task(handler.handle_post(upload(conn, "push", "cancelled-photo")))
        await started.wait()
        capture = visual_state(conn).capture
        get_vision_sessions(conn).cancel("aborted")
        response = await task
    assert json.loads(response.text)["delivery"] == "cancelled"
    assert capture.failed
    assert chat(conn)["image_id"] != capture.image_id


def test_same_session_text_does_not_share_images_between_connections(setup):
    conn, server, _ = setup
    first = begin(setup)
    finish_visual_request(first, succeeded=True)
    other = SimpleNamespace(**{key: value for key, value in vars(conn).items() if key != "ksz_visual_context"})
    other.device_id = "aa:bb:cc:dd:ee:ff"
    server.find_device_connection.side_effect = lambda values: conn if values["device_id"] == conn.device_id else other
    assert "image_id" not in chat(other)
    capture = begin_visual_request(server, other, other.device_id)
    prepare_visual_request(capture, "test")
    finish_visual_request(capture, succeeded=True)
    assert capture.image_seq == 1
    assert chat(other)["image_id"] == capture.image_id
    assert chat(conn)["image_id"] == first.image_id


@pytest.mark.parametrize("change", ["turn", "abort"])
@pytest.mark.asyncio
async def test_new_speech_during_upload_prevents_late_push(setup, change):
    conn, _, handler = setup
    request = upload(conn, "push", "late-upload")

    async def reader():
        if change == "turn":
            conn.sentence_id = "new-question"
        else:
            conn.client_abort = True
        return request.multipart.return_value

    request.multipart.side_effect = reader
    with patch("core.api.vision_handler.create_instance") as factory:
        response = await handler.handle_post(request)
    assert json.loads(response.text)["delivery"] == "cancelled"
    conn.websocket.send.assert_not_awaited()
    factory.assert_not_called()
