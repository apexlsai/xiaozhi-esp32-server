from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from ksz_guide.client import ManagementClient
from ksz_guide.integration import create_runtime
from ksz_guide.metadata import fetch_device_metadata
from ksz_guide.models import GuideError
from test_guide import DEVICE, policy_data
from test_presence import batch, setup


def metadata_response(monkeypatch, data, *, status=200):
    response = httpx.Response(status, json=data, request=httpx.Request("POST", "http://manager/config/device-metadata"))
    post = AsyncMock(return_value=response)
    context = AsyncMock()
    context.__aenter__.return_value.post = post
    factory = Mock(return_value=context)
    monkeypatch.setattr("ksz_guide.metadata.httpx.AsyncClient", factory)
    return post


@pytest.mark.asyncio
async def test_metadata_reads_ota_fields_and_never_accepts_environment_from_device(monkeypatch):
    post = metadata_response(monkeypatch, {"code": 0, "data": [{
        "device_mac": DEVICE.lower(), "model": "p4", "firmware_version": "1.3.2", "environment": "fake",
    }]})
    result = await fetch_device_metadata({"url": "http://manager/", "secret": "server-secret"}, [DEVICE])
    assert result == {DEVICE: {"model": "p4", "firmware_version": "1.3.2"}}
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer server-secret"}
    assert post.call_args.kwargs["json"] == {"deviceIds": [DEVICE]}


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"code": 1, "data": []}, {"code": 0, "data": {}},
    {"code": 0, "data": [{"device_mac": "11:22:33:44:55:66"}]},
    {"code": 0, "data": [{"device_mac": DEVICE, "firmware_version": 123}]},
])
async def test_metadata_rejects_invalid_or_unrequested_devices(monkeypatch, payload):
    metadata_response(monkeypatch, payload)
    with pytest.raises(GuideError):
        await fetch_device_metadata({"url": "http://manager", "secret": "s"}, [DEVICE])


@pytest.mark.asyncio
async def test_registered_device_versions_keep_syncing_and_retry_after_failure(monkeypatch):
    client = ManagementClient("http://unused", "test", environment="showroom",
                              metadata_config={"url": "http://manager", "secret": "s"})
    client.registered.add(DEVICE)
    fetch = AsyncMock(side_effect=[GuideError("temporary"), {DEVICE: {"firmware_version": "1.0"}},
                                   {DEVICE: {"firmware_version": "1.1"}}, {DEVICE: {}}])
    monkeypatch.setattr("ksz_guide.client.fetch_device_metadata", fetch)
    client._request = AsyncMock(return_value={"policies": [policy_data()], "conflicts": []})
    await client.sync_metadata([DEVICE])
    client._request.assert_not_awaited()
    for _ in range(3):
        await client.sync_metadata([DEVICE])
    sent = [call.kwargs["json"]["devices"][0] for call in client._request.call_args_list]
    assert [item.get("firmware_version") for item in sent] == ["1.0", "1.1", None]
    assert all(item["environment"] == "showroom" for item in sent)
    await client.close()


@pytest.mark.asyncio
async def test_metadata_batches_and_conflicts_do_not_block_other_batches(monkeypatch):
    macs = [f"AA:BB:CC:DD:{index >> 8:02X}:{index & 255:02X}" for index in range(205)]
    client = ManagementClient("http://unused", "test", environment="museum", metadata_config={"url": "x"})
    async def fetch(config, batch):
        return {mac: {"firmware_version": "1.0"} for mac in batch}
    monkeypatch.setattr("ksz_guide.client.fetch_device_metadata", fetch)
    client._request = AsyncMock(return_value={"conflicts": [{"device_mac": macs[0]}]})
    await client.sync_metadata(macs)
    assert [len(call.kwargs["json"]["devices"]) for call in client._request.call_args_list] == [100, 100, 5]
    await client.close()


@pytest.mark.asyncio
async def test_mqtt_idle_metadata_syncs_immediately_then_at_periodic_refresh():
    clock, client, server, runtime = setup(guide_enabled=False)
    client.metadata_config = {"url": "manager"}
    client.sync_metadata = AsyncMock()
    runtime.receive_presence(batch(clock))
    await runtime.flush_presence()
    client.sync_metadata.assert_awaited_once_with({DEVICE})
    clock.now += 10
    await runtime.tick()
    assert client.sync_metadata.await_count == 1
    clock.now += 20
    runtime.receive_presence(batch(clock))
    await runtime.tick(refresh=True)
    assert client.sync_metadata.await_count >= 2
    await runtime.close()


@pytest.mark.asyncio
async def test_server_environment_overrides_runtime_payload():
    client = ManagementClient("http://unused", "test", environment="showroom")
    client._request = AsyncMock(return_value={"runtime_available": True})
    await client.report([{"device_mac": DEVICE, "environment": "museum"}])
    assert client._request.call_args.kwargs["json"]["reports"][0]["environment"] == "showroom"
    await client.close()


@pytest.mark.asyncio
async def test_enabled_runtime_requires_explicit_valid_environment(monkeypatch):
    monkeypatch.setenv("KSZ_GUIDE_ENABLED", "1")
    monkeypatch.setenv("KSZ_MANAGEMENT_API_URL", "http://unused/api/v1/integration")
    monkeypatch.setenv("KSZ_MANAGEMENT_API_TOKEN", "test")
    monkeypatch.setenv("KSZ_GUIDE_CONTROL_TOKEN", "x" * 32)
    monkeypatch.delenv("KSZ_DEVICE_ENVIRONMENT", raising=False)
    with pytest.raises(ValueError, match="KSZ_DEVICE_ENVIRONMENT"):
        create_runtime(SimpleNamespace())
    monkeypatch.setenv("KSZ_DEVICE_ENVIRONMENT", "showroom")
    runtime = create_runtime(SimpleNamespace())
    assert runtime.client.environment == "showroom"
    await runtime.close()
