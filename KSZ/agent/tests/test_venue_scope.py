from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from config.settings import get_settings
from src.api import chat_service
from src.api.venue_scope import request_venue_code
from src.rag import retrieve_client
from src.tools import rag


@pytest.fixture
def requests(monkeypatch):
    settings = get_settings()
    for key, value in {"rag_api_enabled": True, "rag_museum_id": "museum_001", "rag_gate_enabled": False}.items():
        monkeypatch.setattr(settings, key, value)
    client = retrieve_client.RagRetrieveClient()
    monkeypatch.setattr(rag, "get_rag_retrieve_client", lambda: client)
    captured = []

    def post(url, payload, **kwargs):
        captured.append(("POST", payload))
        return {"chunks": []}

    def get(url, **kwargs):
        from urllib.parse import parse_qs, urlsplit
        captured.append(("GET", {key: value[0] for key, value in parse_qs(urlsplit(url).query).items()}))
        return {"items": []}

    monkeypatch.setattr(retrieve_client, "_http_post_json", post)
    monkeypatch.setattr(retrieve_client, "_http_get_json", get)
    return captured


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("query", ["介绍一下本馆。", "馆里有哪些藏品？"])
def test_bound_venue_code_reaches_rag_http(requests, owned, query):
    extra = {"venue_context": {"id": "internal-id", "code": "0799001"}}
    if owned:
        extra = {"ksz_prompt_context": {"state": extra}}
    chat_service.build_tools_context(query, extra_body=extra, use_rag=True)
    assert requests
    assert {payload["museum_id"] for _, payload in requests} == {"0799001"}


def test_simultaneous_venues_do_not_change_global_default(requests):
    def retrieve(code):
        chat_service.build_tools_context(code, extra_body={"venue_context": {"code": code}})

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(retrieve, ["0799001", "showroom-02"]))
    assert {(payload["query"], payload["museum_id"]) for method, payload in requests if method == "POST"} == {
        ("0799001", "0799001"), ("showroom-02", "showroom-02"),
    }
    assert get_settings().rag_museum_id == "museum_001"


def test_legacy_request_keeps_configured_default(requests):
    chat_service.build_tools_context("介绍一下本馆。")
    assert requests[0][1]["museum_id"] == "museum_001"


@pytest.mark.parametrize("nested", [False, True])
def test_chat_endpoint_preserves_bound_venue_code(requests, monkeypatch, nested):
    from fastapi.testclient import TestClient
    from src.api import server

    settings = get_settings()
    monkeypatch.setattr(settings, "api_key", None)
    monkeypatch.setattr(settings, "use_mock", False)
    monkeypatch.setattr(settings, "internal_llm_stream", False)
    monkeypatch.setattr(server, "get_inference_client", lambda: SimpleNamespace(chat_completion=lambda payload: {
        "id": "reply", "created": 1,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "馆内回答。"}, "finish_reason": "stop"}],
    }))
    venue = {"venue_id": "internal-id", "venue_context": {"id": "internal-id", "code": "0799001"}}
    body = {"model": "museum-guide-chat", "messages": [{"role": "user", "content": "介绍一下这个展厅。"}],
            "tools": [], "tool_choice": "none", "stream": False}
    body.update({"extra_body": venue} if nested else venue)
    response = TestClient(server.get_app()).post("/v1/chat/completions", json=body)
    assert response.status_code == 200
    assert requests and {payload["museum_id"] for _, payload in requests} == {"0799001"}


@pytest.mark.parametrize("stream", [False, True])
def test_legacy_reply_paths_use_bound_venue_code(requests, monkeypatch, stream):
    monkeypatch.setattr(chat_service, "get_llm_client", lambda: SimpleNamespace(
        invoke=lambda *args, **kwargs: "馆内回答。", stream=lambda *args, **kwargs: iter(["馆内回答。"]),
    ))
    reply = chat_service.generate_reply_stream if stream else chat_service.generate_reply
    answer, _, _ = reply("介绍一下本馆。", extra_body={"venue_context": {"code": "0799001"}})
    assert ("".join(answer) if stream else answer) == "馆内回答。"
    assert requests and {payload["museum_id"] for _, payload in requests} == {"0799001"}


@pytest.mark.parametrize("venue", [{"id": "internal-id"}, {"code": 799001}, {"code": " "}])
def test_bound_venue_without_code_cannot_use_another_museum(venue):
    with pytest.raises(ValueError, match="venue_context.code"):
        request_venue_code({"venue_context": venue})
