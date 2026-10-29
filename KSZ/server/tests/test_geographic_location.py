import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from core.utils.dialogue import Dialogue, Message
from plugins_func.functions import get_weather as weather
from ksz_guide.geographic_location import (
    GeographicLocation, apply_location, close_location, fetch_location,
    location_context, location_prompt, start_location, watch_location,
)
from ksz_guide.prompt import prepare_system_prompt


@pytest.fixture
def conn():
    value = SimpleNamespace(
        device_id="80:f1:b2:d4:ce:64", read_config_from_api=True,
        config={"manager-api": {"url": "http://manager.test/xiaozhi", "secret": "test-secret"}},
        device_attributes={"language": "zh-CN"}, dialogue=Dialogue(),
        prompt="Role.\nApproximate device location: 合肥市\nOther context.",
    )

    def update(prompt):
        value.prompt = prepare_system_prompt(value, prompt)
        value.dialogue.update_system_message(value.prompt)

    value.change_system_prompt = update
    value._init_prompt_enhancement = Mock()
    return value


def located(source="cell", city="深圳市"):
    return {"source": source, "city": city, "expiresAt": int(time.time() * 1000) + 60000,
            "status": "ok", "cellReported": True, "retryAt": 0}


def test_resolved_city_replaces_wrong_ip_for_entire_session_without_duplicate_system(conn):
    conn.ksz_geographic_location = GeographicLocation(value=located())
    conn.dialogue.put(Message("user", "附近有什么？"))
    apply_location(conn)
    apply_location(conn)
    messages = conn.dialogue.get_llm_dialogue()
    system = next(message["content"] for message in messages if message["role"] == "system")
    assert "深圳市" in system and "合肥" not in system
    assert system.count("<ksz_device_location>") == 1
    assert system.count("<ksz_response_policy>") == 1
    assert len([message for message in messages if message["role"] == "system"]) == 1
    assert messages[-1] == {"role": "user", "content": "附近有什么？"}


@pytest.mark.parametrize("source", ["cell", "fixed", "default", "unknown"])
def test_cell_fixed_default_and_unknown_never_use_sim_ip(conn, source):
    conn.ksz_geographic_location = GeographicLocation(value=located(source, None if source == "unknown" else "深圳"))
    handled, city = location_context(conn)
    assert handled
    assert city == ("" if source == "unknown" else "深圳")
    prompt = location_prompt(conn, conn.prompt)
    assert "合肥" not in prompt
    if source == "default":
        assert "not a measured current position" in prompt
    elif source == "unknown":
        assert "Ask which city" in prompt


def test_expired_location_is_not_claimed_as_current(conn):
    conn.ksz_geographic_location = GeographicLocation(value={**located(), "expiresAt": 1})
    assert location_context(conn) == (True, "")
    assert "深圳" not in location_prompt(conn, conn.prompt)


def test_wifi_and_standalone_keep_existing_ip_path(conn):
    conn.ksz_geographic_location = GeographicLocation(value=located("ip", None))
    assert location_context(conn) == (False, "")
    assert location_prompt(conn, conn.prompt) == conn.prompt
    conn.read_config_from_api = False
    assert location_context(conn) == (False, "")


@pytest.mark.asyncio
async def test_resolution_is_async_and_updates_live_prompt_then_closes(conn, monkeypatch):
    started, ready = asyncio.Event(), asyncio.Event()

    async def fetch(_):
        started.set()
        await ready.wait()
        return located()

    monkeypatch.setattr("ksz_guide.geographic_location.fetch_location", fetch)
    start_location(conn)
    await started.wait()
    assert location_context(conn) == (True, "")
    ready.set()
    for _ in range(10):
        await asyncio.sleep(0)
        if "深圳市" in conn.prompt:
            break
    assert "深圳市" in conn.prompt and "合肥" not in conn.prompt
    close_location(conn)
    await conn.ksz_geographic_location.task
    assert conn.ksz_geographic_location.closed


@pytest.mark.asyncio
async def test_closed_connection_discards_late_result(conn, monkeypatch):
    state = GeographicLocation()
    conn.ksz_geographic_location = state

    async def fetch(_):
        state.closed = True
        return located()

    monkeypatch.setattr("ksz_guide.geographic_location.fetch_location", fetch)
    await watch_location(conn, state)
    assert "深圳市" not in conn.prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("string_timestamps", [False, True])
async def test_manager_request_uses_server_auth_without_sending_ip(conn, monkeypatch, string_timestamps):
    original = httpx.AsyncClient
    expected = {**located(), "locatedAt": int(time.time() * 1000)}
    payload = dict(expected)
    if string_timestamps:
        for name in ("locatedAt", "expiresAt", "retryAt"):
            payload[name] = str(payload[name])

    def respond(request):
        assert request.url.path == "/xiaozhi/config/device-location"
        assert request.headers["Authorization"] == "Bearer test-secret"
        assert request.content == b'{"deviceId":"80:f1:b2:d4:ce:64"}'
        return httpx.Response(200, json={"code": 0, "data": payload})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(respond)))
    assert await fetch_location(conn) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [None, "广州"])
async def test_weather_uses_shared_city_and_honors_explicit_city(conn, monkeypatch, explicit):
    from core.utils.cache.manager import cache_manager

    conn.ksz_geographic_location = GeographicLocation(value=located())
    conn.client_ip = "36.7.99.190"
    conn.config["plugins"] = {"get_weather": {"api_key": "test", "api_host": "weather.test"}}
    city_lookup = AsyncMock(return_value={"id": "city-id", "name": explicit or "深圳市"})
    monkeypatch.setattr(weather, "fetch_city_info", city_lookup)
    monkeypatch.setattr(weather, "fetch_weather_data", AsyncMock(return_value=({"now": {"text": "晴"}}, {})))
    monkeypatch.setattr(weather, "get_ip_info", lambda *args: pytest.fail("cell device must not locate by SIM IP"))
    monkeypatch.setattr(cache_manager, "get", lambda *args: None)
    monkeypatch.setattr(cache_manager, "set", lambda *args: None)
    await weather.get_weather(conn, location=explicit)
    assert city_lookup.call_args.args[0] == (explicit or "深圳市")


@pytest.mark.asyncio
async def test_unknown_city_weather_asks_without_external_call(conn, monkeypatch):
    conn.client_ip = "36.7.99.190"
    lookup = AsyncMock()
    monkeypatch.setattr(weather, "fetch_city_info", lookup)
    response = await weather.get_weather(conn)
    lookup.assert_not_called()
    assert "城市" in response.result
