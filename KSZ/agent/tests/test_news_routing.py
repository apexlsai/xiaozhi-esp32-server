import json
from types import SimpleNamespace

import pytest

from src.api import server
from src.api.schemas import ChatCompletionRequest


SEARCH = {"type": "function", "function": {
    "name": "web_search", "description": "联网搜索工具。", "parameters": {"type": "object"},
}}
NEWS = {"type": "function", "function": {
    "name": "get_news_from_newsnow", "description": "随机新闻；具体话题使用web_search。",
    "parameters": {"type": "object"},
}}


@pytest.fixture(autouse=True)
def gate(monkeypatch):
    monkeypatch.setattr(server, "get_settings", lambda: SimpleNamespace(rag_gate_enabled=True))


@pytest.mark.parametrize("query", [
    "请联网核实中国航展的进展。", "2026年11月最近中国航展有什么最新进展？请联网核实。",
    "这个会议什么时候开？", "这家公司是什么时候成立的？", "帮我搜索当前的政策。",
    "Search the web for the latest policy updates.", "最新の情報を検索して。", "최신 소식을 검색해 주세요.",
])
def test_current_fact_questions_keep_search_tool(query):
    body = ChatCompletionRequest(model="museum-guide-chat", messages=[{"role": "user", "content": query}], tools=[SEARCH, NEWS])
    decision = server._route_chat_request(body)
    assert decision.route == "tool" and decision.tool_available
    assert decision.reason == "search_tool_intent"


def test_explicit_baidu_hot_search_is_news_intent():
    body = ChatCompletionRequest(model="museum-guide-chat", messages=[{"role": "user", "content": "请播报一条百度热搜，来源只要百度热搜。"}], tools=[NEWS])
    decision = server._route_chat_request(body)
    assert decision.route == "tool" and decision.tool_available
    assert decision.reason == "news_tool_intent"


@pytest.mark.parametrize("tools,choice", [([], None), ([NEWS], None), ([SEARCH], "none")])
def test_search_requires_actual_offered_and_allowed_tool(tools, choice):
    body = ChatCompletionRequest(model="museum-guide-chat", messages=[{"role": "user", "content": "请联网核实最新进展。"}], tools=tools, tool_choice=choice)
    decision = server._route_chat_request(body)
    assert not decision.tool_available


@pytest.mark.parametrize("query", ["介绍一下这个展厅。", "这里这块屏幕今天的成交底价是多少？"])
def test_venue_and_product_knowledge_keep_rag(query):
    body = ChatCompletionRequest(model="museum-guide-chat", messages=[{"role": "user", "content": query}], tools=[SEARCH, NEWS])
    assert server._route_chat_request(body).route == "rag"


def test_search_result_turn_does_not_request_another_tool():
    body = ChatCompletionRequest(model="museum-guide-chat", messages=[
        {"role": "user", "content": "请联网核实最新进展。"},
        {"role": "tool", "tool_call_id": "call_test", "content": "实际搜索结果"},
    ], tools=[SEARCH, NEWS])
    decision = server._route_chat_request(body)
    assert decision.route == "tool" and not decision.tool_available


def news_result_body(*, content=None, question="再看看娱乐圈的新闻。", tools=None, choice=None, name="get_news_from_newsnow"):
    return ChatCompletionRequest(model="museum-guide-chat", messages=[
        {"role": "user", "content": question},
        {"role": "assistant", "tool_calls": [{
            "id": "call_news", "type": "function", "function": {
                "name": name, "arguments": '{"source":"娱乐圈","lang":"zh_CN"}',
            },
        }]},
        {"role": "tool", "tool_call_id": "call_news", "content": content or "未配置新闻来源或类别：娱乐圈，不能用其他新闻代替。具体话题可使用web_search检索。"},
    ], tools=[NEWS, SEARCH] if tools is None else tools, tool_choice=choice)


def test_misrouted_news_topic_can_retry_only_search(monkeypatch):
    from config.settings import get_settings
    monkeypatch.setattr(server, "get_settings", get_settings)
    body = news_result_body()
    decision = server._route_chat_request(body)
    assert decision.reason == "news_search_retry" and decision.tool_available
    payload = server._tools_rag_payload(body, context="", tool_result_turn=True)
    assert payload["tools"] == [SEARCH]
    assert payload["tool_choice"] == "auto"
    assert "再看看娱乐圈的新闻。" in json.dumps(payload, ensure_ascii=False)
    disabled = server._tools_rag_payload(body, context="", tool_result_turn=True, disable_tools=True)
    assert "tools" not in disabled


@pytest.mark.parametrize("kwargs", [
    {"tools": [NEWS]}, {"choice": "none"}, {"choice": "required"},
    {"choice": {"type": "function", "function": {"name": "get_news_from_newsnow"}}},
    {"content": "未配置新闻来源：财联社，不能用其他来源代替。"},
    {"content": "未配置国际对应的新闻类别，不能用其他类别代替。具体话题可使用web_search检索。"},
    {"content": "抱歉，未能获取澎湃新闻的新闻信息，请稍后再试。"},
    {"content": "实际搜索结果", "name": "web_search"},
    {"question": "新闻来源只要娱乐圈。"},
    {"question": "只用这个来源播新闻。"},
    {"question": "News from the requested source only."},
])
def test_news_recovery_preserves_source_constraints_and_stops_after_search(kwargs):
    decision = server._route_chat_request(news_result_body(**kwargs))
    assert not decision.tool_available
    assert decision.reason != "news_search_retry"


@pytest.mark.parametrize("stream", [False, True])
def test_api_passes_recovery_search_call_instead_of_empty_fallback(monkeypatch, stream):
    from config.settings import get_settings
    from fastapi.testclient import TestClient

    settings = get_settings()
    for key, value in {"api_key": None, "use_mock": False, "rag_gate_enabled": True, "internal_llm_stream": stream}.items():
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(server, "get_settings", get_settings)
    monkeypatch.setattr(server, "build_tools_context", lambda *_args, **_kwargs: ("", [], "zh-CN"))
    captured = []
    call = {"id": "call_search", "type": "function", "function": {"name": "web_search", "arguments": '{"query":"娱乐圈 最新新闻"}'}}

    class Inference:
        def chat_completion(self, payload):
            captured.append(payload)
            return {"id": "reply", "created": 1, "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [call]}, "finish_reason": "tool_calls"}]}

        def chat_completion_stream(self, payload):
            captured.append(payload)
            yield {"id": "reply", "created": 1, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, **call}]}, "finish_reason": "tool_calls"}]}

    monkeypatch.setattr(server, "get_inference_client", Inference)
    body = news_result_body().model_dump(exclude_none=True)
    body["stream"] = stream
    response = TestClient(server.get_app()).post("/v1/chat/completions", json=body)
    assert response.status_code == 200
    assert len(captured) == 1 and captured[0]["tools"] == [SEARCH]
    assert "call_search" in response.text and "web_search" in response.text
    assert "模型暂时未返回可用内容" not in response.text
