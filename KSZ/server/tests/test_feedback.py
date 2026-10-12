import asyncio
import json
import queue
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.api.vision_handler import VisionHandler
from core.utils.vision_stream import get_vision_sessions
from ksz_guide.feedback import (
    CAMERA_PHRASES, DEFAULT_PHRASES, CameraFeedbackScheduler,
    camera_image_received, finish_camera_feedback,
)


class Timer:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback()


@pytest.fixture
def conn():
    connection = SimpleNamespace(
        config={}, device_attributes={"language": "zh-CN"},
        sentence_id="turn-1", session_id="session-1", client_abort=False,
        stop_event=threading.Event(), need_bind=False, client_is_speaking=False,
        logger=Mock(), dialogue=Mock(), websocket=SimpleNamespace(send=AsyncMock()),
        clear_queues=Mock(), clearSpeakStatus=Mock(),
        mcp_client=SimpleNamespace(call_results={}),
        tts=SimpleNamespace(
            tts_tool_feedback=Mock(), tts_complete_sentence=Mock(),
            tts_one_sentence=Mock(), store_tts_text=Mock(), tts_text_queue=queue.Queue(),
        ),
    )
    connection.tool_feedback = CameraFeedbackScheduler(connection, timer_factory=Timer)
    return connection


def schedule(conn, call="call-1", tool="self_camera_take_photo"):
    return conn.tool_feedback.schedule(tool, call, conn.sentence_id)


def feedback_text(conn):
    return [call.args[1] for call in conn.tts.tts_tool_feedback.call_args_list]


@pytest.mark.parametrize("language", CAMERA_PHRASES)
@pytest.mark.parametrize("received", [False, True])
def test_all_languages_rotate_without_repetition_and_respect_image_state(conn, language, received):
    conn.device_attributes = {"language": language}
    expected = {
        phrase.text for phrase in DEFAULT_PHRASES[language]
        if received or not phrase.requires_image
    }
    for turn in range(len(expected) * 2):
        conn.sentence_id = f"turn-{turn}"
        schedule(conn)
        if received:
            camera_image_received(conn, legacy_mcp=True, sentence_id=conn.sentence_id)
        assert conn.tool_feedback.camera.timer.delay == 0.5
        conn.tool_feedback.camera.timer.fire()
    texts = feedback_text(conn)
    assert set(texts[:len(expected)]) == expected
    assert set(texts[len(expected):]) == expected
    assert all(left != right for left, right in zip(texts, texts[1:]))
    conn.dialogue.put.assert_not_called()
    conn.tts.store_tts_text.assert_not_called()


@pytest.mark.parametrize("attributes,language", [
    ({"agent_name": "youth-英语", "language": "zh-CN"}, "en"),
    ({"agent_name": "youth-日语-测试", "language": "en"}, "ja"),
    ({"agent_name": "custom", "language": "ko"}, "ko"),
    ({"agent_name": "youth-粤语", "language": "zh-CN"}, "zh-CN"),
    ({"language": "fr"}, "zh-CN"), ({}, "zh-CN"), ({"language": []}, "zh-CN"),
])
def test_language_matches_current_binding_and_unknown_language_falls_back(conn, attributes, language):
    conn.device_attributes = attributes
    schedule(conn)
    conn.tool_feedback.camera.timer.fire()
    if language:
        assert feedback_text(conn)[0] in CAMERA_PHRASES[language]
    else:
        assert feedback_text(conn) == []


def test_upload_changes_candidates_without_restarting_timer(conn):
    conn.config = {"tool_feedback": {"tools": {"self_camera_take_photo": {
        "phrases": {"zh-CN": ["我看看。", {"text": "收到图片啦。", "requires_image": True}]},
    }}}}
    conn.tool_feedback.choose = lambda candidates: candidates[-1]
    schedule(conn)
    timer = conn.tool_feedback.camera.timer
    handle = camera_image_received(conn, legacy_mcp=True, sentence_id=conn.sentence_id)
    assert conn.tool_feedback.camera.timer is timer
    timer.fire()
    assert feedback_text(conn) == ["收到图片啦。"]
    camera_image_received(conn, legacy_mcp=True, sentence_id=conn.sentence_id)
    assert schedule(conn, "call-2", "self.camera.take_photo") is None
    timer.callback()
    finish_camera_feedback(handle)
    assert feedback_text(conn) == ["收到图片啦。"]


def test_prompt_before_upload_is_not_repeated_after_upload(conn):
    schedule(conn)
    conn.tool_feedback.camera.timer.fire()
    camera_image_received(conn, legacy_mcp=True, sentence_id=conn.sentence_id)
    schedule(conn, "duplicate")
    conn.tool_feedback.camera.timer.callback()
    assert len(feedback_text(conn)) == 1


def test_camera_aliases_share_one_timer_and_feedback(conn):
    first = schedule(conn)
    assert schedule(conn, "call-2", "self.camera.take_photo") == first
    assert len(conn.tool_feedback.timers) == 1
    conn.tool_feedback.camera.timer.fire()
    assert len(feedback_text(conn)) == 1


@pytest.mark.parametrize("config", [
    {"enabled": False}, {"tools": {}}, {"tools": {"self_camera_take_photo": ""}},
    {"tools": {"self_camera_take_photo": None}},
])
def test_disabled_camera_does_not_start_timer(conn, config):
    conn.config = {"tool_feedback": config}
    assert schedule(conn) is None
    assert conn.tool_feedback.timers == {}


def test_legacy_string_and_other_tool_configuration_remain_compatible(conn):
    conn.config = {"tool_feedback": {"delay_ms": 800, "tools": {
        "self_camera_take_photo": "自定义提示。", "other_tool": "稍候。",
    }}}
    conn.device_attributes = {}
    schedule(conn)
    assert conn.tool_feedback.camera.timer.delay == 0.8
    conn.tool_feedback.camera.timer.fire()
    other = schedule(conn, "other", "other_tool")
    conn.tool_feedback.timers[other].fire()
    assert feedback_text(conn) == ["自定义提示。", "稍候。"]


def test_invalid_custom_catalog_does_not_fall_back_to_chinese(conn):
    conn.device_attributes = {"language": "en"}
    conn.config = {"tool_feedback": {"tools": {"self_camera_take_photo": {
        "phrases": {"zh-CN": ["我看看。"], "en": [{"text": 42}, None]},
    }}}}
    schedule(conn)
    conn.tool_feedback.camera.timer.fire()
    assert feedback_text(conn) == []


@pytest.mark.parametrize("cancel", ["completed", "aborted", "disconnected", "new_turn", "finished"])
def test_finished_and_stale_requests_never_play_late_feedback(conn, cancel):
    key = schedule(conn)
    state = conn.tool_feedback.camera
    if cancel == "completed":
        conn.tool_feedback.cancel(key)
    elif cancel == "aborted":
        conn.client_abort = True
    elif cancel == "disconnected":
        conn.stop_event.set()
    elif cancel == "new_turn":
        conn.sentence_id = "new-turn"
    else:
        finish_camera_feedback((conn.tool_feedback, state))
    state.timer.callback()
    assert feedback_text(conn) == []


def test_first_sentence_before_late_schedule_keeps_feedback_closed(conn):
    sessions = get_vision_sessions(conn)
    request = sessions.begin_mcp(conn.sentence_id, time.monotonic() + 120)
    sessions.emit(request, "正文。")
    assert schedule(conn) is None
    assert feedback_text(conn) == []


def test_first_sentence_cancels_timer_and_suppresses_second_schedule(conn):
    sessions = get_vision_sessions(conn)
    request = sessions.begin_mcp(conn.sentence_id, time.monotonic() + 120)
    schedule(conn)
    timer = conn.tool_feedback.camera.timer
    sessions.emit(request, "正文。")
    timer.callback()
    assert schedule(conn, "late-duplicate") is None
    assert feedback_text(conn) == []
    conn.tts.tts_complete_sentence.assert_called_once_with(conn, "正文。", conn.sentence_id)


def test_request_cancellation_stops_waiting_prompt(conn):
    sessions = get_vision_sessions(conn)
    sessions.begin_mcp(conn.sentence_id, time.monotonic() + 120)
    schedule(conn)
    timer = conn.tool_feedback.camera.timer
    sessions.cancel("timeout")
    timer.callback()
    assert feedback_text(conn) == []


def test_old_http_completion_cannot_cancel_new_photo_feedback(conn):
    schedule(conn)
    old = camera_image_received(conn, legacy_mcp=True, sentence_id=conn.sentence_id)
    conn.sentence_id = "new-photo"
    schedule(conn, "new-photo")
    finish_camera_feedback(old)
    conn.tool_feedback.camera.timer.fire()
    assert len(feedback_text(conn)) == 1


def test_feedback_racing_first_sentence_is_queued_before_body(conn):
    sessions = get_vision_sessions(conn)
    request = sessions.begin_mcp(conn.sentence_id, time.monotonic() + 120)
    schedule(conn)
    entered = threading.Event()
    release = threading.Event()
    output = []

    def feedback(*args):
        entered.set()
        assert release.wait(2)
        output.append("feedback")

    conn.tts.tts_tool_feedback.side_effect = feedback
    conn.tts.tts_complete_sentence.side_effect = lambda *args: output.append("body")
    prompt_thread = threading.Thread(target=conn.tool_feedback.camera.timer.fire)
    body_thread = threading.Thread(target=sessions.emit, args=(request, "正文。"))
    prompt_thread.start()
    try:
        assert entered.wait(2)
        body_thread.start()
    finally:
        release.set()
        prompt_thread.join(2)
        if body_thread.ident is not None:
            body_thread.join(2)
    assert not prompt_thread.is_alive() and not body_thread.is_alive()
    assert output == ["feedback", "body"]


def http_request(mode, request_id=None, image=b"\xff\xd8\xffimage"):
    fields = [("question", "描述图片".encode()), ("file", image)]
    if mode is not None:
        fields.append(("delivery_mode", mode.encode()))
    if request_id:
        fields.append(("request_id", request_id.encode()))
    reader = SimpleNamespace(next=AsyncMock(side_effect=[
        *[SimpleNamespace(name=name, read=AsyncMock(return_value=value)) for name, value in fields], None,
    ]))
    return SimpleNamespace(
        headers={"Client-Id": "web_test_client", "Device-Id": "device-1"},
        multipart=AsyncMock(return_value=reader),
    )


@pytest.fixture
def handler(conn):
    handler = object.__new__(VisionHandler)
    handler.config = {}
    handler.logger = Mock()
    handler.websocket_server = SimpleNamespace(find_device_connection=Mock(return_value=conn))
    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["mcp", "push", "return", None])
async def test_http_feedback_is_one_per_photo_and_return_mode_is_silent(conn, handler, mode):
    request_id = "manual-photo" if mode == "push" else None
    if mode == "mcp":
        request_id = get_vision_sessions(conn).begin_mcp(
            conn.sentence_id, time.monotonic() + 120,
        ).request_id
    if mode in ("mcp", None):
        conn.mcp_client.call_results = {1: object()}
        schedule(conn)

    async def generate(*args, **kwargs):
        if mode != "return":
            assert conn.tool_feedback.camera.image_received
            conn.tool_feedback.camera.timer.fire()
            assert len(feedback_text(conn)) == 1
        else:
            assert conn.tool_feedback.camera is None
        sessions, request = args[-2:]
        if request:
            sessions.emit(request, "正文。")
        return "正文。"

    handler._generate = generate
    response = await handler.handle_post(http_request(mode, request_id))
    assert json.loads(response.text)["success"]
    if mode != "return":
        assert conn.tool_feedback.camera.closed
        conn.tool_feedback.camera.timer.callback()
        assert len(feedback_text(conn)) == 1
    else:
        assert feedback_text(conn) == []


@pytest.mark.asyncio
async def test_invalid_upload_never_starts_manual_feedback(conn, handler):
    handler._generate = AsyncMock()
    response = await handler.handle_post(http_request("push", "photo", image=b"not an image"))
    assert not json.loads(response.text)["success"]
    assert conn.tool_feedback.camera is None
    handler._generate.assert_not_called()


@pytest.mark.asyncio
async def test_http_failure_cancels_pending_feedback(conn, handler):
    handler._generate = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    response = await handler.handle_post(http_request("push", "photo"))
    assert not json.loads(response.text)["success"]
    conn.tool_feedback.camera.timer.callback()
    assert feedback_text(conn) == []
