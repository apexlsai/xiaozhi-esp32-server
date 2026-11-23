import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ksz_guide import integration
from ksz_guide.intent_embedding import EmbeddingIntentClassifier, external_museum_query
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


@pytest.mark.asyncio
async def test_explicit_search_for_a_museum_stays_an_online_query(monkeypatch):
    classifier = EmbeddingIntentClassifier({"base_url": "https://embedding.test"})
    embed = AsyncMock(side_effect=AssertionError("explicit search does not require embedding"))
    monkeypatch.setattr(classifier, "_embed", embed)
    result = await classifier.classify("搜索一下这个博物馆的最新消息", {"yuanbao_search"})
    assert result["function_call"] == {
        "name": "yuanbao_search", "arguments": {"query": "这个博物馆的最新消息"},
    }


@pytest.mark.parametrize(("question", "external"), [
    ("深圳博物馆里面有什么重要产品？", True),
    ("帮我查一下萍乡博物馆有哪些藏品", False),
    ("介绍这个博物馆的展品", False),
    ("本馆有哪些藏品", False),
    ("你是深圳博物馆的吗？", False),
    ("[位置变化] 用户当前位于深圳博物馆序厅", False),
    ("What are the highlights of the British Museum?", True),
    ("東京国立博物館の代表的な展示は何ですか", True),
])
def test_named_museums_respect_management_binding(question, external):
    assert external_museum_query(question, {"name": "萍乡市博物馆"}) is external


@pytest.mark.asyncio
@pytest.mark.parametrize("venue", [None, {"name": "萍乡博物馆"}])
async def test_cross_museum_question_selects_yuanbao_before_embedding(monkeypatch, venue):
    monkeypatch.setattr(integration, "_embedding_classifier", lambda conn: None)
    monkeypatch.setattr(integration, "runtime_for", lambda conn: SimpleNamespace(
        context=lambda device_id: {"venue_context": venue},
    ))
    conn = SimpleNamespace(
        device_id="device-a", logger=Mock(),
        func_handler=SimpleNamespace(get_functions=lambda: [{"function": {"name": "yuanbao_search"}}]),
    )
    question = "深圳博物馆里面有什么重要产品？"
    result = json.loads(await integration.classify_embedding_intent(conn, question))
    assert result["function_call"] == {"name": "yuanbao_search", "arguments": {"query": question}}


@pytest.mark.asyncio
async def test_general_chat_embedding_returns_direct_route(monkeypatch):
    classifier = EmbeddingIntentClassifier({"base_url": "https://embedding.test"})
    monkeypatch.setattr(classifier, "_references_ready", AsyncMock(return_value={"general_chat": [1.0, 0.0], "museum_rag": [0.0, 1.0]}))
    monkeypatch.setattr(classifier, "_embed", AsyncMock(return_value=[[1.0, 0.0]]))
    result = await classifier.classify("你好", {"web_search"})
    assert result["route"] == "direct_answer"
    assert "function_call" not in result


def test_route_hint_is_consumed_once(monkeypatch):
    from ksz_guide.integration import filter_context
    monkeypatch.setattr(integration, "runtime_for", lambda conn: SimpleNamespace(
        context=lambda device_id: {"device_mode": "consumer"},
    ))
    connection = SimpleNamespace(
        device_id="AA:BB:CC:DD:EE:FF", ksz_route="direct_answer",
        ksz_route_version=1, ksz_route_confidence=0.99, ksz_route_margin=0.2,
        ksz_geographic_location=None, beacon_location=None, prompt="",
    )
    first = filter_context(connection, {})
    second = filter_context(connection, {})
    assert first["ksz_route"] == "direct_answer"
    assert "ksz_route" not in second


@pytest.mark.asyncio
async def test_low_confidence_non_original_question_falls_back_to_yuanbao(monkeypatch):
    classifier = EmbeddingIntentClassifier({"base_url": "https://embedding.test"})
    monkeypatch.setattr(classifier, "_references_ready", AsyncMock(return_value={
        "museum_rag": [0.0, 1.0], "general_chat": [1.0, 0.0],
    }))
    monkeypatch.setattr(classifier, "_embed", AsyncMock(return_value=[[0.7, 0.7]]))
    result = await classifier.classify("帮我查一个没有示例的问题", {"yuanbao_search"})
    assert result["function_call"]["name"] == "yuanbao_search"


@pytest.mark.asyncio
async def test_embedding_tool_does_not_call_web_search(monkeypatch):
    handler = SimpleNamespace(
        has_tool=lambda name: name == "yuanbao_search",
        handle_llm_function_call=AsyncMock(return_value=ActionResponse(Action.REQLLM, result="元宝结果")),
    )
    conn = SimpleNamespace(func_handler=handler)
    result = await integration.execute_embedding_tool(conn, {
        "name": "yuanbao_search", "arguments": '{"query":"测试"}',
    })
    assert result.result == "元宝结果"
    assert [call.args[1]["name"] for call in handler.handle_llm_function_call.call_args_list] == [
        "yuanbao_search"
    ]
