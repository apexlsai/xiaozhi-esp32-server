import json
from types import SimpleNamespace

import httpx
import pytest

from plugins_func.functions import yuanbao_search
from plugins_func.register import Action


@pytest.fixture
def conn():
    return SimpleNamespace(
        config={
            "plugins": {
                "yuanbao_search": {
                    "secret_id": "test-id",
                    "secret_key": "test-key",
                    "max_results": 1,
                }
            }
        }
    )


@pytest.mark.asyncio
async def test_search_signs_request_and_formats_pages(conn, monkeypatch):
    requests = []

    def dispatch(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "Response": {
                    "Pages": [
                        json.dumps(
                            {
                                "title": "搜索标题",
                                "content": "搜索摘要",
                                "site": "测试站点",
                                "date": "2026-11-19",
                                "url": "https://example.test/article",
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps({"title": "第二条"}, ensure_ascii=False),
                    ]
                }
            },
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(dispatch), **kwargs),
    )
    monkeypatch.setattr(yuanbao_search.time, "time", lambda: 1700000000)

    result = await yuanbao_search.yuanbao_search(conn, "深圳天气")

    assert result.action == Action.REQLLM
    assert "搜索标题" in result.result
    assert "搜索摘要" in result.result
    assert "https://example.test/article" in result.result
    assert "第二条" not in result.result
    assert len(requests) == 1
    assert requests[0].content == '{"Query":"深圳天气"}'.encode("utf-8")
    assert requests[0].headers["X-TC-Action"] == "SearchPro"
    assert "Credential=test-id/2023-11-14/wsa/tc3_request" in requests[0].headers["Authorization"]


@pytest.mark.asyncio
async def test_missing_credentials_is_reported_without_request(monkeypatch):
    conn = SimpleNamespace(config={"plugins": {"yuanbao_search": {}}})
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: pytest.fail("未配置密钥时不应请求腾讯云"),
    )

    result = await yuanbao_search.yuanbao_search(conn, "测试")

    assert result.action == Action.REQLLM
    assert "Secret ID" in result.result and "Secret Key" in result.result
