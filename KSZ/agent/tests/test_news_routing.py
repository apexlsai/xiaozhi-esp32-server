from types import SimpleNamespace

import pytest

from src.api import server
from src.api.schemas import ChatCompletionRequest


SEARCH = {"type": "function", "function": {
    "name": "web_search", "description": "联网搜索", "parameters": {"type": "object"},
}}
YUANBAO = {"type": "function", "function": {
    "name": "yuanbao_search", "description": "元宝搜索", "parameters": {"type": "object"},
}}
WEATHER = {"type": "function", "function": {
    "name": "get_weather", "description": "天气", "parameters": {"type": "object"},
}}


@pytest.fixture(autouse=True)
def gate(monkeypatch):
    settings = SimpleNamespace(rag_gate_enabled=True)
    monkeypatch.setattr(server, "get_settings", lambda: settings)


def test_embedding_or_client_selected_tools_are_the_only_tool_route():
    body = ChatCompletionRequest(
        model="museum-guide-chat",
        messages=[{"role": "user", "content": "查一下天气"}],
        tools=[WEATHER, SEARCH, YUANBAO],
    )
    decision = server._route_chat_request(body)
    assert decision.route == "tool"
    assert decision.reason == "tools_available"
    assert decision.tool_available


def test_agent_does_not_force_search_for_ordinary_questions(monkeypatch):
    from config.settings import get_settings
    monkeypatch.setattr(server, "get_settings", get_settings)
    body = ChatCompletionRequest(
        model="museum-guide-chat",
        messages=[{"role": "user", "content": "介绍一下这个展厅"}],
        tools=[SEARCH, YUANBAO],
    )
    decision = server._route_chat_request(body)
    assert decision.route == "rag"
    payload = server._tools_rag_payload(body, context="展厅资料", tool_result_turn=False)
    assert payload.get("tool_choice", "auto") == "auto"
    assert payload["tools"] == [SEARCH, YUANBAO]


def test_tool_result_turn_never_starts_another_tool_call(monkeypatch):
    from config.settings import get_settings
    monkeypatch.setattr(server, "get_settings", get_settings)
    body = ChatCompletionRequest(
        model="museum-guide-chat",
        messages=[
            {"role": "user", "content": "搜索一下最新消息"},
            {"role": "assistant", "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "web_search", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_call_id": "call_1", "content": "搜索结果"},
        ],
        tools=[SEARCH, YUANBAO],
    )
    decision = server._route_chat_request(body)
    assert decision.route == "tool"
    assert not decision.tool_available
    payload = server._tools_rag_payload(body, context="", tool_result_turn=True)
    assert "tools" not in payload
