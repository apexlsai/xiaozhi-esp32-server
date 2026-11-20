from types import SimpleNamespace

from ksz_guide.search_parallel import attach_web_search


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
