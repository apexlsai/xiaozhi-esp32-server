import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiohttp import ClientSession, web

from ksz_guide.client import ManagementClient
from ksz_guide.integration import (
    create_runtime, filter_context, handle_control_message, handle_legacy_event,
    on_connect, on_disconnect, on_hello, refresh_location, register_routes, vision_context,
)
from ksz_guide.models import GuideError, Policy
from ksz_guide.presence import PresenceTracker
from ksz_guide.runtime import GuideRuntime
from test_guide import Clock, DEVICE, FakeClient, beacon, policy_data


class PresenceClient(FakeClient):
    def __init__(self, clock):
        super().__init__(clock)
        self.registered = set()
        self.registrations = []
        self.runtime_failed = False

    async def ensure_registered(self, macs):
        if self.failed:
            raise GuideError("management_unavailable")
        for mac in macs:
            if mac not in self.registered:
                self.registrations.append(mac)
                self.registered.add(mac)

    async def report(self, reports):
        if self.failed or self.runtime_failed:
            raise GuideError("management_runtime_unavailable")
        self.reports.extend(reports)


class Connection:
    def __init__(self, server, *, mqtt=False, device=DEVICE):
        self.device_id = device
        self.session_id = str(uuid4())
        self.server = server
        self.conn_from_mqtt_gateway = False
        self.beacon_location = "legacy-location"
        self.websocket = SimpleNamespace(
            state=1, request=SimpleNamespace(path="/xiaozhi/v1/?from=mqtt_gateway" if mqtt else "/xiaozhi/v1/"),
            send=AsyncMock(), close=AsyncMock(),
        )


def setup(guide_enabled=False):
    clock = Clock()
    client = PresenceClient(clock)
    server = SimpleNamespace(device_connections={})
    runtime = GuideRuntime(client, server, clock=clock, wall_clock=clock, guide_enabled=guide_enabled)
    runtime.control_token = "x" * 32
    server.guide_runtime = runtime
    return clock, client, server, runtime


def batch(clock, *, instance=None, connection=None, sequence=1, online=True, connected_at=95, device=DEVICE, observed_at=None):
    return {
        "instance_id": instance or str(uuid4()), "sequence": sequence,
        "reports": [{"device_mac": device, "connection_id": connection or str(uuid4()), "online": online,
                     "connected_at": connected_at, "observed_at": clock() if observed_at is None else observed_at}],
    }


@pytest.mark.asyncio
async def test_idle_legacy_mqtt_registers_and_reports_without_hello_or_location():
    clock, client, server, runtime = setup(True)
    state = runtime.state(DEVICE)
    state.current = beacon()
    state.last_position_at = 99
    state.ack_revision = "unchanged"
    event = batch(clock)
    assert runtime.receive_presence(event) == {"accepted": 1, "ignored": 0}
    assert state.last_position_at == 99
    assert state.ack_revision == "unchanged"
    await runtime.flush_presence()
    assert client.registrations == [DEVICE]
    report = client.reports[-1]
    assert report["online"] and report["transport"] == "mqtt"
    assert report["session_active"] is False
    assert report["last_seen_at"] == 100
    clock.now = 110
    await runtime.tick()
    assert client.registrations == [DEVICE]
    await runtime.close()


@pytest.mark.asyncio
async def test_connection_sequence_is_independent_and_old_offline_cannot_hide_reconnect():
    clock, client, server, runtime = setup()
    first = batch(clock, sequence=20)
    runtime.receive_presence(first)
    second = batch(clock, instance=first["instance_id"], sequence=10, connected_at=99)
    assert runtime.receive_presence(second)["accepted"] == 1
    offline = {**first, "sequence": 21, "reports": [{**first["reports"][0], "online": False}]}
    runtime.receive_presence(offline)
    assert runtime.receive_presence(first)["ignored"] == 1
    assert runtime.presence.fields(DEVICE)["online"] is True
    await runtime.flush_presence()
    assert client.reports[-1]["connected_at"] == 99
    restarted = batch(clock, sequence=0, connected_at=100)
    assert runtime.receive_presence(restarted)["accepted"] == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_mqtt_expiry_uses_observed_time_and_offline_is_reported():
    clock, client, server, runtime = setup()
    runtime.receive_presence(batch(clock, observed_at=80, connected_at=75))
    await runtime.flush_presence()
    assert client.reports[-1]["online"] is True
    clock.now = 110
    await runtime.tick()
    assert client.reports[-1]["online"] is False
    assert client.reports[-1]["last_seen_at"] == 80
    await runtime.close()


@pytest.mark.asyncio
async def test_ws_hook_snapshot_recovery_disconnect_and_reconnect():
    clock, client, server, runtime = setup()
    first = Connection(server)
    server.device_connections[first] = {"connected_at": clock()}
    on_connect(first)
    await runtime.flush_presence()
    assert client.reports[-1]["transport"] == "websocket"
    assert client.reports[-1]["online"] and client.reports[-1]["session_active"]
    clock.now = 140
    await runtime.tick(refresh=True)
    assert client.reports[-1]["last_seen_at"] == 140
    second = Connection(server)
    server.device_connections[second] = {"connected_at": clock()}
    runtime.snapshot_connections()
    assert second in runtime.voice_connections
    on_disconnect(first)
    server.device_connections.pop(first)
    await runtime.flush_presence()
    assert client.reports[-1]["online"]
    second.websocket.state = 3
    runtime.snapshot_connections()
    await runtime.flush_presence()
    assert client.reports[-1]["online"] is False
    assert client.reports[-1]["session_active"] is False
    assert first.beacon_location == "legacy-location"
    await runtime.close()


@pytest.mark.asyncio
async def test_mqtt_voice_session_never_owns_network_presence():
    clock, client, server, runtime = setup()
    runtime.receive_presence(batch(clock))
    conn = Connection(server, mqtt=True)
    server.device_connections[conn] = {"connected_at": clock()}
    on_connect(conn)
    await runtime.flush_presence()
    assert len(runtime.presence.connections) == 1
    assert client.reports[-1]["session_active"] is True
    on_disconnect(conn)
    server.device_connections.pop(conn)
    await runtime.flush_presence()
    assert client.reports[-1]["online"] is True
    assert client.reports[-1]["session_active"] is False
    await runtime.close()


@pytest.mark.asyncio
async def test_presence_only_preserves_all_legacy_guide_paths():
    clock, client, server, runtime = setup()
    conn = Connection(server)
    on_connect(conn)
    assert await on_hello(conn, {})
    assert runtime.state(DEVICE).owner is None
    assert not await handle_legacy_event(conn, {"event": "beacon_change", "payload": {"beacon_id": beacon().mac}})
    assert not await handle_control_message(conn, {"type": "guide_control"})
    values = {"last_beacon_id": "legacy-beacon", "venue_id": "legacy-venue"}
    assert filter_context(conn, values) is values
    assert vision_context(server, DEVICE, values) is values
    assert refresh_location(conn) is False
    await runtime.tick(refresh=True)
    assert conn.beacon_location == "legacy-location"
    assert "current_beacon_id" not in client.reports[-1]
    assert "applied_policy_revision" not in client.reports[-1]
    await runtime.close()


@pytest.mark.asyncio
async def test_registration_and_report_failures_recover_without_new_guide_messages():
    clock, client, server, runtime = setup()
    client.failed = True
    event = batch(clock)
    runtime.receive_presence(event)
    await runtime.flush_presence()
    assert DEVICE in runtime.pending_registration
    assert DEVICE in runtime.dirty_reports
    client.failed = False
    clock.now += 5
    await runtime.tick()
    assert client.registrations == [DEVICE]
    assert client.reports[-1]["online"] is True
    client.runtime_failed = True
    runtime.receive_presence({**event, "sequence": 2, "reports": [{**event["reports"][0], "online": False}]})
    await runtime.flush_presence()
    assert DEVICE in runtime.dirty_reports
    client.runtime_failed = False
    clock.now += 5
    await runtime.tick()
    assert client.reports[-1]["online"] is False
    assert not runtime.dirty_reports
    await runtime.close()


@pytest.mark.asyncio
async def test_report_sequence_survives_device_eviction_and_reconnection():
    clock, client, server, runtime = setup()
    runtime.receive_presence(batch(clock))
    await runtime.flush_presence()
    original = client.reports[-1]["report_revision"]
    clock.now = 131
    await runtime.tick()
    clock.now = 401
    await runtime.tick()
    assert DEVICE not in runtime.devices
    runtime.receive_presence(batch(clock, connected_at=401))
    await runtime.flush_presence()
    assert client.reports[-1]["report_revision"] > original
    assert client.reports[-1]["online"]
    await runtime.close()


@pytest.mark.parametrize("change", [
    {"online": 1}, {"connected_at": 110}, {"observed_at": 110}, {"observed_at": float("nan")},
    {"connected_at": float("inf")}, {"observed_at": -1}, {"connection_id": "not-uuid"},
    {"device_mac": "invalid"},
])
def test_presence_invalid_fields_are_atomic(change):
    clock = Clock()
    tracker = PresenceTracker(clock, clock)
    payload = batch(clock)
    payload["reports"][0].update(change)
    with pytest.raises(GuideError):
        tracker.validate(payload)
    assert tracker.connections == {}


def test_presence_bounds_and_identity_cannot_be_reassigned():
    clock = Clock()
    tracker = PresenceTracker(clock, clock)
    payload = batch(clock)
    instance, sequence, reports = tracker.validate(payload)
    tracker.update(instance, sequence, reports, "mqtt")
    for changed in ({"sequence": True}, {"sequence": 2**53}, {"reports": []}, {"reports": payload["reports"] * 101}):
        with pytest.raises(GuideError):
            tracker.validate({**payload, **changed})
    payload["reports"][0]["device_mac"] = "11:22:33:44:55:66"
    with pytest.raises(GuideError, match="presence_identity_conflict"):
        tracker.validate(payload)


@pytest.mark.asyncio
async def test_unregistered_policy_cache_does_not_prevent_registration_and_retry():
    clock = Clock()
    client = ManagementClient("http://unused", "test", clock=clock, wall_clock=clock)
    client._save_policy(policy_data(enabled=False, reason="unregistered"))
    client._request = AsyncMock(side_effect=GuideError("management_unavailable"))
    with pytest.raises(GuideError):
        await client.ensure_registered([DEVICE])
    assert DEVICE not in client.registered
    client._request = AsyncMock(return_value={"policies": [policy_data(enabled=False, mode="unconfigured")]})
    await client.ensure_registered([DEVICE])
    assert client._request.call_args.args[1] == "/devices/sync"
    assert DEVICE in client.registered
    await client.ensure_registered([DEVICE])
    assert client._request.call_count == 1
    await client.close()


@pytest.mark.asyncio
async def test_management_runtime_redis_failure_is_retriable():
    client = ManagementClient("http://unused", "test")
    client._request = AsyncMock(return_value={"accepted": 0, "runtime_available": False})
    with pytest.raises(GuideError, match="management_runtime_unavailable"):
        await client.report([{"device_mac": DEVICE}])
    client._request.return_value = {"accepted": 0, "runtime_available": True}
    await client.report([{"device_mac": DEVICE}])
    await client.close()


@pytest.mark.asyncio
async def test_presence_env_and_authenticated_http_route(monkeypatch):
    monkeypatch.setenv("KSZ_GUIDE_ENABLED", "0")
    monkeypatch.setenv("KSZ_GUIDE_PRESENCE_ENABLED", "1")
    monkeypatch.setenv("KSZ_MANAGEMENT_API_URL", "http://unused/api/v1/integration")
    monkeypatch.setenv("KSZ_MANAGEMENT_API_TOKEN", "test-token")
    monkeypatch.setenv("KSZ_GUIDE_CONTROL_TOKEN", "x" * 32)
    configured = create_runtime(SimpleNamespace())
    assert configured and not configured.guide_enabled
    await configured.close()
    clock, client, server, runtime = setup()
    app = web.Application()
    register_routes(app, server)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        async with ClientSession() as session:
            async with session.post(url + "/internal/ksz/guide/presence", json=batch(clock)) as response:
                assert response.status == 401
            headers = {"Authorization": "Bearer " + "x" * 32}
            async with session.post(url + "/internal/ksz/guide/presence", headers=headers, json=batch(clock)) as response:
                assert response.status == 200
                assert (await response.json())["accepted"] == 1
            async with session.post(url + "/internal/ksz/guide/control", headers=headers, json={}) as response:
                assert response.status == 404
            async with session.post(url + "/internal/ksz/guide/presence", headers=headers, data="x" * 65537) as response:
                assert response.status == 413
        await runtime.flush_presence()
        assert client.reports[-1]["online"]
    finally:
        await runner.cleanup()
