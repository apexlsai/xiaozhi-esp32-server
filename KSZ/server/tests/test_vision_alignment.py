import asyncio
import json
import queue
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from core.providers.tools.device_mcp.mcp_executor import DeviceMCPExecutor
from core.providers.vllm.openai import VLLMProvider
from core.utils.vision_stream import fail_vision_tool, get_vision_sessions
from ksz_guide.vision import VISION_MESSAGES, vision_language


def connection(language):
    return SimpleNamespace(
        device_attributes={"language": language}, sentence_id="turn", session_id="session",
        client_abort=False, stop_event=threading.Event(), features={}, logger=Mock(),
        dialogue=Mock(), tool_feedback=Mock(),
        mcp_client=SimpleNamespace(is_ready=AsyncMock(return_value=True)),
        tts=SimpleNamespace(
            tts_text_queue=queue.Queue(), tts_complete_sentence=Mock(), store_tts_text=Mock(),
        ),
    )


@pytest.mark.parametrize("language", VISION_MESSAGES)
@pytest.mark.parametrize("partial", [False, True])
def test_failure_or_interruption_is_spoken_once(language, partial):
    conn = connection(language)
    sessions = get_vision_sessions(conn)
    request = sessions.begin_mcp(conn.sentence_id, time.monotonic() + 120)
    if partial:
        sessions.emit(request, "first")
    kind = "interrupted" if partial else "unavailable"
    sessions.fail(request)
    sessions.fail(request)
    calls = conn.tts.tts_complete_sentence.call_args_list
    assert [call.args[1] for call in calls] == (["first"] if partial else []) + [VISION_MESSAGES[language][kind]]


@pytest.mark.parametrize("language", VISION_MESSAGES)
def test_legacy_camera_errors_and_missing_session_use_binding_language(language):
    conn = connection("zh-CN")
    conn.device_attributes = {"agent_name": f"儿童-{dict(en='英语', ja='日语', ko='韩语', **{'zh-CN': '汉语'})[language]}", "language": "zh-CN"}
    executor = DeviceMCPExecutor(conn)
    assert executor._vision_result({"success": False}).response == VISION_MESSAGES[language]["unavailable"]
    assert executor._error_result("self_camera_take_photo", "error").response == VISION_MESSAGES[language]["unavailable"]
    assert fail_vision_tool(conn, conn.sentence_id).response == VISION_MESSAGES[language]["unavailable"]


def test_cancelled_request_does_not_speak_failure():
    conn = connection("ja")
    sessions = get_vision_sessions(conn)
    request = sessions.begin_mcp(conn.sentence_id, time.monotonic() + 120)
    sessions.cancel()
    sessions.fail(request)
    conn.tts.tts_complete_sentence.assert_not_called()


def test_unknown_language_uses_chinese():
    assert vision_language(connection("unknown")) == "zh-CN"


def test_visual_http_waits_beyond_old_30_second_limit():
    async def run():
        provider = VLLMProvider({"base_url": "http://vision.test/v1", "api_key": "test", "model_name": "museum-guide-vision"})
        event = {"choices": [{"index": 0, "delta": {"content": "赤色です。"}, "finish_reason": "stop"}]}
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, text="data: " + json.dumps(event) + "\n\n",
        ))
        client = httpx.AsyncClient
        with patch("core.providers.vllm.openai.httpx.AsyncClient", side_effect=lambda **kw: client(transport=transport, **kw)) as factory:
            result = [text async for text in provider.response_stream("test", "image", deadline=time.monotonic() + 60, language="ja")]
        assert result == ["赤色です。"]
        assert 30 < factory.call_args.kwargs["timeout"].read <= 60
        assert factory.call_args.kwargs["timeout"].connect == 10

    asyncio.run(run())
