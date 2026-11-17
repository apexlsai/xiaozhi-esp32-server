from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from config.config_loader import merge_configs
from core.providers.tools.server_plugins.plugin_executor import ServerPluginExecutor
from plugins_func.functions import get_news_from_newsnow as news
from plugins_func.functions import get_news_from_chinanews as rss
from plugins_func.functions import web_search as search
from plugins_func.register import Action, all_function_registry


ASYNC_CLIENT = httpx.AsyncClient
RSS = """<rss><channel><item>
<title>RSS新闻标题</title><link>https://news.test/article/rss</link>
<description>新闻摘要</description><pubDate>Tue, 10 Nov 2026 09:00:00 +0800</pubDate>
</item></channel></rss>"""


@pytest.fixture
def conn():
    return SimpleNamespace(config={
        "selected_module": {"Intent": "test"},
        "Intent": {"test": {"functions": ["get_news_from_newsnow", "web_search"]}},
        "plugins": {
            "get_news_from_newsnow": {
                "url": "https://news.test/api/s?id=",
                "news_sources": "百度热搜;澎湃新闻",
            },
            "get_news_from_chinanews": {
                "default_rss_url": "https://news.test/rss/default.xml",
                "world_rss_url": "https://news.test/rss/world.xml",
                "finance_rss_url": "https://news.test/rss/finance.xml",
            },
            "web_search": {"provider": "metaso", "api_key": "test-key"},
        },
    })


@pytest.fixture
def transport(monkeypatch):
    def install(handler):
        requests = []

        def dispatch(request):
            requests.append(request)
            return handler(request)

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: ASYNC_CLIENT(
            transport=httpx.MockTransport(dispatch), **kwargs,
        ))
        return requests

    return install


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [None, "", "  ", "百度热搜"])
async def test_healthy_primary_never_requests_rss(conn, transport, source):
    requests = transport(lambda request: httpx.Response(200, json={"items": [
        {"title": "百度新闻标题", "url": "https://news.test/article/baidu"},
    ]}))
    result = await news.get_news_from_newsnow(conn, source=source, lang="ja_JP")
    assert result.action == Action.REQLLM
    assert "百度新闻标题" in result.result and "ja_JP" in result.result
    assert conn.last_newsnow_link["source_name"] == "百度热搜"
    assert len(requests) == 1 and requests[0].url.params["id"] == "baidu"
    assert "发布时间: 未知" in result.result
    assert "不得将未确认发布时间" in result.result


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http", "empty", "format", "invalid_json", "timeout", "connect"])
async def test_default_primary_failure_uses_rss_once(conn, transport, failure):
    def handler(request):
        if request.url.path.startswith("/rss/"):
            return httpx.Response(200, text=RSS)
        if failure == "timeout":
            raise httpx.ReadTimeout("test timeout", request=request)
        if failure == "connect":
            raise httpx.ConnectError("test connection failure", request=request)
        if failure == "http":
            return httpx.Response(503)
        if failure == "invalid_json":
            return httpx.Response(200, text="invalid")
        return httpx.Response(200, json={"items": [] if failure == "empty" else {}})

    requests = transport(handler)
    result = await news.get_news_from_newsnow(conn)
    assert len(requests) == 2
    assert requests[0].url.params["id"] == "baidu"
    assert requests[1].url.path == "/rss/default.xml"
    assert "RSS新闻标题" in result.result and "新闻来源: 中新网" in result.result
    assert conn.last_newsnow_link == {
        "url": "https://news.test/article/rss", "title": "RSS新闻标题",
        "source_id": "chinanews", "source_name": "中新网",
        "pubDate": "Tue, 10 Nov 2026 09:00:00 +0800",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["<rss><channel/></rss>", "not xml", RSS.replace("RSS新闻标题", "")])
async def test_both_sources_fail_without_invented_news(conn, transport, body):
    requests = transport(lambda request: httpx.Response(
        200, json={"items": []},
    ) if request.url.path == "/api/s" else httpx.Response(200, text=body))
    result = await news.get_news_from_newsnow(conn)
    assert len(requests) == 2 and "未能获取" in result.result
    assert not hasattr(conn, "last_newsnow_link")


@pytest.mark.asyncio
async def test_explicit_source_failure_does_not_change_source(conn, transport):
    requests = transport(lambda request: httpx.Response(503))
    result = await news.get_news_from_newsnow(conn, source="澎湃新闻")
    assert len(requests) == 1 and requests[0].url.params["id"] == "thepaper"
    assert "未能获取澎湃新闻" in result.result


@pytest.mark.asyncio
@pytest.mark.parametrize("category,path", [
    ("国际", "world.xml"), ("国际新闻", "world.xml"),
    ("财经新闻", "finance.xml"), ("金融", "finance.xml"),
])
async def test_category_uses_only_matching_rss(conn, transport, category, path):
    requests = transport(lambda request: httpx.Response(200, text=RSS))
    result = await news.get_news_from_newsnow(conn, source=category)
    assert len(requests) == 1 and requests[0].url.path == f"/rss/{path}"
    assert "RSS新闻标题" in result.result


@pytest.mark.asyncio
async def test_category_failure_does_not_use_default_category(conn, transport):
    requests = transport(lambda request: httpx.Response(200, text="<rss/>"))
    result = await news.get_news_from_newsnow(conn, source="国际新闻")
    assert len(requests) == 1 and requests[0].url.path == "/rss/world.xml"
    assert "未能获取国际新闻" in result.result


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["社会新闻", "科技新闻", "未配置来源", "财联社"])
async def test_unconfigured_source_or_category_never_fetches_other_news(conn, transport, source):
    requests = transport(lambda request: pytest.fail("不应请求其他新闻"))
    result = await news.get_news_from_newsnow(conn, source=source)
    assert not requests and "未配置" in result.result
    if source == "财联社":
        assert "web_search" not in result.result
        assert result.result.startswith("未配置新闻来源：")
    else:
        assert "web_search" in result.result


@pytest.mark.asyncio
async def test_rss_detail_keeps_same_article_language_and_metadata(conn, transport):
    def handler(request):
        if request.url.path.startswith("/rss/"):
            return httpx.Response(200, text=RSS)
        return httpx.Response(200, text="<html><body><article><p>这条新闻的详细内容。</p></article></body></html>")

    requests = transport(handler)
    await news.get_news_from_newsnow(conn, source="国际新闻")
    original = deepcopy(conn.last_newsnow_link)
    result = await news.get_news_from_newsnow(conn, detail="true", lang="en_US")
    assert len(requests) == 2 and str(requests[-1].url) == original["url"]
    assert conn.last_newsnow_link == original
    assert "en_US" in result.result and "这条新闻的详细内容" in result.result
    assert original["pubDate"] in result.result and "新闻来源: 中新网" in result.result


@pytest.mark.asyncio
async def test_detail_without_previous_news_does_not_fetch(conn, transport):
    requests = transport(lambda request: pytest.fail("没有上一条新闻"))
    result = await news.get_news_from_newsnow(conn, detail=True)
    assert not requests and "请先获取一条新闻" in result.result


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["社会", "科技"])
async def test_legacy_rss_tool_also_rejects_unconfigured_categories(conn, transport, category):
    requests = transport(lambda request: pytest.fail("不应请求其他类别"))
    result = await rss.get_news_from_chinanews(conn, category=category)
    assert not requests and "未配置" in result.result


def test_legacy_rss_description_does_not_override_default_source_policy(conn):
    conn.config["Intent"]["test"]["functions"].append("get_news_from_chinanews")
    effective = ServerPluginExecutor(conn).get_tools()["get_news_from_chinanews"].description["function"]
    assert "优先使用get_news_from_newsnow" in effective["description"]
    parameter = effective["parameters"]["properties"]["category"]["description"]
    assert "国际、财经" in parameter and "科技" not in parameter


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("description", [None, "", "联网搜索工具。当用户明确需要联网搜索问题时使用此工具。", "只查询指定数据库公开信息。"])
def test_effective_search_description_honors_defaults_and_custom_config(conn, managed, description):
    if not managed:
        defaults = yaml.safe_load((Path(news.__file__).parents[2] / "config.yaml").read_text())
        conn.config = merge_configs(defaults, conn.config)
    conn.config["read_config_from_api"] = managed
    if description is not None:
        conn.config["plugins"]["web_search"]["description"] = description
    original = deepcopy(all_function_registry["web_search"].description)
    effective = ServerPluginExecutor(conn).get_tools()["web_search"].description["function"]["description"]
    if description == "只查询指定数据库公开信息。":
        assert effective == description
    else:
        assert "具体事件" in effective and "先调用本工具核实" in effective
    assert all_function_registry["web_search"].description == original


@pytest.mark.parametrize("sources", ["百度热搜;澎湃新闻", ["百度热搜", "澎湃新闻"]])
def test_news_schema_matches_configured_categories_and_source_list(conn, sources):
    conn.config["plugins"]["get_news_from_newsnow"]["news_sources"] = sources
    conn.config["plugins"]["get_news_from_newsnow"]["description"] = "当用户要求查看或收听新闻时调用（如'来条新闻''今天有什么新闻'）。"
    effective = ServerPluginExecutor(conn).get_tools()["get_news_from_newsnow"].description["function"]
    assert "web_search" in effective["description"]
    parameter = effective["parameters"]["properties"]["source"]["description"]
    assert "百度热搜、澎湃新闻" in parameter and "国际、财经" in parameter
    assert "社会" not in parameter and "科技" not in parameter
    assert "必须省略" in parameter
    assert effective["parameters"]["properties"]["source"]["enum"] == ["百度热搜", "澎湃新闻", "国际", "财经"]


def test_tool_descriptions_are_isolated_between_connections_and_calls(conn):
    original = deepcopy(all_function_registry["get_news_from_newsnow"].description)
    first = ServerPluginExecutor(conn).get_tools()
    second_conn = SimpleNamespace(config=deepcopy(conn.config))
    second_conn.config["plugins"]["get_news_from_newsnow"] = {"news_sources": "澎湃新闻", "description": "自定义播报规则。"}
    second_conn.config["plugins"]["get_news_from_chinanews"] = {}
    second = ServerPluginExecutor(second_conn).get_tools()
    assert "百度热搜" in first["get_news_from_newsnow"].description["function"]["parameters"]["properties"]["source"]["description"]
    assert second["get_news_from_newsnow"].description["function"]["description"] == "自定义播报规则。"
    assert second["get_news_from_newsnow"].description["function"]["parameters"]["properties"]["source"]["enum"] == ["澎湃新闻"]
    assert all_function_registry["get_news_from_newsnow"].description == original
    first["web_search"].description["function"]["parameters"]["properties"]["query"]["description"] = "修改返回值"
    fresh = ServerPluginExecutor(conn).get_tools()
    assert fresh["web_search"].description["function"]["parameters"]["properties"]["query"]["description"] == "搜索关键词或问题"


@pytest.mark.asyncio
async def test_specific_event_search_preserves_query_and_returns_evidence(conn, transport):
    requests = transport(lambda request: httpx.Response(200, json={"webpages": [{
        "title": "事件最新进展", "summary": "来自搜索的事实依据", "date": "2026-11-10",
        "link": "https://news.test/event",
    }]}))
    result = await search.web_search(conn, query="某事件 2026年11月 最新进展")
    assert len(requests) == 1 and "某事件 2026年11月 最新进展" in requests[0].content.decode()
    assert "来自搜索的事实依据" in result.result and "2026-11-10" in result.result
    assert "https://news.test/event" in result.result
