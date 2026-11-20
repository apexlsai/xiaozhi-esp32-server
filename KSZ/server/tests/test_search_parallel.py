import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ksz_guide.search_parallel import attach_web_search
from plugins_func.register import Action, ActionResponse
from test_guide_announcement import conn as chat_conn, spoken, assistant_text


def conn(names=("web_search", "yuanbao_search")):
    return SimpleNamespace(func_handler=SimpleNamespace(has_tool=lambda name: name in names))


def test_yuanbao_also_runs_web_search(monkeypatch):
    monkeypatch.setattr("ksz_guide.search_parallel.uuid.uuid4", lambda: "web-1")
    calls = attach_web_search(conn(), [
        {"id": "yb-1", "name": "yuanbao_search", "arguments": '{"query":"中慧公司"}'},
    ], query="用元宝搜索中慧公司")
    assert [item["name"] for item in calls] == ["yuanbao_search", "web_search"]
    assert calls[1]["arguments"] == '{"query":"中慧公司"}'
    assert calls[1]["id"] == "web-1"


def test_real_search_adds_yuanbao_to_web_search(monkeypatch):
    monkeypatch.setattr("ksz_guide.search_parallel.uuid.uuid4", lambda: "yb-1")
    calls = attach_web_search(conn(), [
        {"id": "web-1", "name": "web_search", "arguments": '{"query":"APEC"}'},
    ], query="搜索一下APEC2026年在哪里举办")
    assert [item["name"] for item in calls] == ["web_search", "yuanbao_search"]
    assert calls[1]["arguments"] == '{"query":"APEC"}'


def test_forced_empty_real_search_runs_both(monkeypatch):
    ids = iter(["web-1", "yb-1"])
    monkeypatch.setattr("ksz_guide.search_parallel.uuid.uuid4", lambda: next(ids))
    calls = attach_web_search(conn(), [], query="说一下这两天发生了什么大事")
    assert [item["name"] for item in calls] == ["web_search", "yuanbao_search"]
    assert '"说一下这两天发生了什么大事"' in calls[0]["arguments"]


def test_existing_web_search_stays_parallel():
    original = [
        {"id": "yb-1", "name": "yuanbao_search", "arguments": '{"query":"APEC"}'},
        {"id": "web-1", "name": "web_search", "arguments": '{"query":"APEC"}'},
    ]
    assert attach_web_search(conn(), original, query="搜索一下APEC") == original


def test_non_search_web_only_does_not_add_yuanbao():
    original = [{"id": "web-1", "name": "web_search", "arguments": '{"query":"展厅介绍"}'}]
    assert attach_web_search(conn(), original, query="介绍一下这个展厅。") == original


def test_missing_web_search_plugin_keeps_yuanbao_only():
    original = [{"id": "yb-1", "name": "yuanbao_search", "arguments": '{"query":"APEC"}'}]
    assert attach_web_search(conn(("yuanbao_search",)), original) == original


def test_greeting_does_not_invent_search():
    assert attach_web_search(conn(), [], query="你好") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("model_calls_search", [False, True])
async def test_search_draft_is_neither_spoken_nor_added_to_history(chat_conn, monkeypatch, model_calls_search):
    chat_conn.loop = asyncio.get_running_loop()
    chat_conn.intent_type = "function_call"
    chat_conn.func_handler.has_tool = lambda name: name in {"web_search", "yuanbao_search"}
    chat_conn.func_handler.handle_llm_function_call = AsyncMock(return_value=ActionResponse(Action.REQLLM, result="检索事实"))
    monkeypatch.setattr("core.connection.enqueue_tool_report", Mock())
    draft = [("尚未核实的旧回答。", None)]
    if model_calls_search:
        draft.append((None, [SimpleNamespace(index=0, id="search", function=SimpleNamespace(
            name="web_search", arguments='{"query":"APEC峰会"}',
        ))]))
    chat_conn.llm.response_with_functions.side_effect = [iter(draft), iter([("根据检索结果确认的回答。", None)])]
    await asyncio.to_thread(chat_conn.chat, "查一下APEC领导人峰会。")
    assert spoken(chat_conn) == "根据检索结果确认的回答。"
    assert assistant_text(chat_conn) == ["根据检索结果确认的回答。"]
    assert {call.args[1]["name"] for call in chat_conn.func_handler.handle_llm_function_call.call_args_list} == {
        "web_search", "yuanbao_search",
    }


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
