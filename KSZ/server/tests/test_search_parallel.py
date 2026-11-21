import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ksz_guide.search_parallel import attach_web_search
from plugins_func.register import Action, ActionResponse
from test_guide_announcement import conn as chat_conn, spoken, assistant_text


def test_search_router_does_not_invent_tools():
    original = [{"id": "weather-1", "name": "get_weather", "arguments": "{}"}]
    assert attach_web_search(SimpleNamespace(), original, query="查一下天气") == original
    assert attach_web_search(SimpleNamespace(), [], query="查一下天气") == []


def test_search_router_preserves_model_selected_calls():
    original = [{"id": "web-1", "name": "web_search", "arguments": '{"query":"APEC"}'}]
    assert attach_web_search(SimpleNamespace(), original, query="搜索一下APEC") == original


@pytest.mark.asyncio
async def test_search_tools_are_executed_only_when_model_selected(chat_conn, monkeypatch):
    chat_conn.loop = asyncio.get_running_loop()
    chat_conn.intent_type = "function_call"
    chat_conn.func_handler.handle_llm_function_call = AsyncMock(
        return_value=ActionResponse(Action.REQLLM, result="检索事实")
    )
    monkeypatch.setattr("core.connection.enqueue_tool_report", Mock())
    chat_conn.llm.response_with_functions.side_effect = [
        iter([("", [SimpleNamespace(index=0, id="search", function=SimpleNamespace(
            name="web_search", arguments='{"query":"APEC峰会"}',
        ))])]),
        iter([("根据检索结果确认的回答。", None)]),
    ]
    await asyncio.to_thread(chat_conn.chat, "搜索一下APEC领导人峰会")
    assert spoken(chat_conn) == "根据检索结果确认的回答。"
    assert assistant_text(chat_conn) == ["根据检索结果确认的回答。"]
    assert [call.args[1]["name"] for call in chat_conn.func_handler.handle_llm_function_call.call_args_list] == [
        "web_search"
    ]


def test_greeting_still_streams_without_waiting_for_the_complete_answer(chat_conn):
    chat_conn.intent_type = "function_call"
    chat_conn.func_handler.has_tool = lambda name: True

    def stream():
        yield "你好。", None
        assert spoken(chat_conn) == "你好。"
        yield "可以听到。", None

    chat_conn.llm.response_with_functions.return_value = stream()
    chat_conn.chat("你好")
    assert spoken(chat_conn) == "你好。可以听到。"
