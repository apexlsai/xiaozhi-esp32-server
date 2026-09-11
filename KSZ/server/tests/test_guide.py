import asyncio
import json
import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientSession, web

from ksz_guide.client import ManagementClient
from ksz_guide.integration import (
    filter_context, handle_legacy_event, register_routes, track_chat, vision_context,
)
from ksz_guide.models import Beacon, GuideError, Policy, beacon_identity
from ksz_guide.runtime import GuideRuntime


DEVICE = "AA:BB:CC:DD:EE:FF"


class Clock:
    def __init__(self):
        self.now = 100

    def __call__(self):
        return self.now


def policy_data(**changes):
    return {
        "device_id": "device1", "device_mac": DEVICE, "mode": "venue", "status": "active",
        "enabled": True, "reason": None, "revision": "1:1", "catalog_revision": "1",
        "expires_at": 160, "auto_announce": True, "venue_ids": ["venue1"], "beacon_ids": [],
        "filter": {"min_announce_interval_ms": 15000}, **changes,
    }


def beacon(number=1, **changes):
    return Beacon.parse({
        "id": f"beacon{number}", "mac": f"DA:01:16:{number >> 16 & 255:02X}:{number >> 8 & 255:02X}:{number & 255:02X}",
        "beacon_id": f"asset-{number}", "venue_id": "venue1", "area_id": f"area{number}",
        "floor": "1F", "area": f"展区{number}", "location_description": "展柜旁", **changes,
    })


class FakeClient:
    def __init__(self, clock, items=None, **policy_changes):
        self.clock = clock
        self.data = policy_data(**policy_changes)
        self.policies = {DEVICE: Policy.parse(self.data, clock(), clock())}
        self.items = items or [beacon(1), beacon(2)]
        self.failed = False
        self.reports = []

    async def policy(self, mac, metadata=None):
        if self.failed and self.policies[mac].deadline <= self.clock():
            raise GuideError("management_unavailable")
        return self.policies[mac]

    async def refresh_policies(self, macs):
        if self.failed:
            raise GuideError("management_unavailable")
        self.policies[DEVICE] = Policy.parse({**self.data, "expires_at": self.clock() + 60}, self.clock(), self.clock())
        return {mac: self.policies[mac] for mac in macs}

    async def whitelist(self, policy, current_venue=None):
        return {item.mac: item for item in self.items if policy.allows(item)}

    async def wire_whitelist(self, policy, current_venue=None):
        return [item.wire() for item in (await self.whitelist(policy, current_venue)).values()]

    async def resolve(self, mac, policy, observed, current_venue=None):
        for item in self.items:
            if item.matches(observed) and policy.allows(item):
                return item
        raise GuideError("beacon_not_allowed")

    async def report(self, reports):
        self.reports.extend(reports)

    async def close(self):
        pass


class Harness:
    def __init__(self, **changes):
        self.clock = Clock()
        self.client = FakeClient(self.clock, **changes)
        self.announce = AsyncMock()
        self.runtime = GuideRuntime(self.client, clock=self.clock, wall_clock=self.clock, announcer=self.announce)
        self.runtime.control_token = "x" * 32
        self.seq = 0

    async def send(self, action, **fields):
        message = {"type": "guide_control", "version": 1, "action": action,
                   "boot_id": "boot1", "seq": self.seq, "request_id": str(self.seq),
                   "policy_revision": self.client.data["revision"], **fields}
        self.seq += 1
        udp_ready = message.pop("udp_ready", False)
        response = await self.runtime.process(DEVICE, "connection1", message, udp_ready=udp_ready)
        return response["messages"]

    async def sync(self):
        messages = await self.send("sync")
        part = next(item for item in messages if item["action"] == "policy")
        await self.send("policy_ack", policy_sha256=part["manifest"]["sha256"], chunk_count=part["manifest"]["chunk_count"])
        return part

    async def observe(self, number=1, **fields):
        return await self.send("observation", beacon={"mac": beacon(number).mac}, **fields)

    def conn(self, session_id="session1"):
        return SimpleNamespace(
            device_id=DEVICE, session_id=session_id, need_bind=False, tts=object(),
            websocket=SimpleNamespace(send=AsyncMock(), close=AsyncMock()),
            server=SimpleNamespace(guide_runtime=self.runtime), beacon_location=None,
        )


@pytest.mark.asyncio
async def test_unknown_cross_venue_and_disabled_never_mutate_or_announce():
    h = Harness(items=[beacon(1), beacon(2, venue_id="other")])
    await h.sync()
    await h.observe(1)
    for number in [2, 999]:
        messages = await h.observe(number)
        assert messages[0]["accepted"] is False
        assert h.runtime.context(DEVICE)["last_beacon_id"] == "asset-1"
    h.client.data.update(revision="2:1", enabled=False)
    await h.runtime.tick(refresh=True)
    assert h.runtime.context(DEVICE) == {}
    h.announce.assert_not_awaited()


@pytest.mark.asyncio
async def test_idle_transition_waits_for_udp_and_tts_readiness():
    h = Harness()
    await h.sync()
    assert not any(item["action"] == "request_hello" for item in await h.observe(1))
    messages = await h.observe(2)
    wake = next(item for item in messages if item["action"] == "request_hello")
    h.announce.assert_not_awaited()
    conn = h.conn()
    assert await h.runtime.bind(conn, {"activation_id": wake["activation_id"], "session_attempt": 1,
                                       "boot_id": "boot1", "connection_id": "connection1"})
    ready = {"activation_id": wake["activation_id"], "session_attempt": 1, "session_id": "session1"}
    messages = await h.send("ready", **ready)
    assert messages[0]["reason"] == "udp_not_ready"
    conn.tts = None
    await h.send("ready", **ready, udp_ready=True)
    h.announce.assert_not_awaited()
    conn.tts = object()
    await h.runtime.tick()
    h.announce.assert_awaited_once_with(conn, beacon(2))
    await h.runtime.tick()
    h.announce.assert_awaited_once()


@pytest.mark.asyncio
async def test_fallback_owner_and_late_disconnect_do_not_duplicate_announcement():
    h = Harness()
    await h.sync()
    await h.observe(1)
    wake = next(item for item in await h.observe(2) if item["action"] == "request_hello")
    first = h.conn()
    second = h.conn("session2")
    metadata = {"activation_id": wake["activation_id"], "boot_id": "boot1", "connection_id": "connection1"}
    assert await h.runtime.bind(first, {**metadata, "session_attempt": 1})
    assert not await h.runtime.bind(second, {**metadata, "session_attempt": 1})
    assert await h.runtime.bind(second, {**metadata, "session_attempt": 2})
    first.websocket.close.assert_awaited_once()
    h.runtime.unbind(first)
    assert h.runtime.state(DEVICE).owner is second
    msg = {"type": "guide_control", "version": 1, "action": "ready", "seq": 20, "request_id": "ready2",
           "boot_id": "boot1", "policy_revision": "1:1", "activation_id": wake["activation_id"],
           "session_id": "session2", "session_attempt": 2}
    await h.runtime.process(DEVICE, "ws:session2", msg, transport="websocket", conn=second)
    await h.runtime.process(DEVICE, "ws:session2", msg, transport="websocket", conn=second)
    h.announce.assert_awaited_once_with(second, beacon(2))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["lost", "expired", "policy"])
async def test_stale_pending_activation_cannot_restore_position(failure):
    h = Harness()
    await h.sync()
    await h.observe(1)
    wake = next(item for item in await h.observe(2) if item["action"] == "request_hello")
    if failure == "lost":
        await h.send("lost")
    elif failure == "expired":
        h.clock.now += 15
    else:
        h.client.data["revision"] = "2:1"
        await h.runtime.tick(refresh=True)
    assert not await h.runtime.bind(h.conn(), {"activation_id": wake["activation_id"], "session_attempt": 1, "boot_id": "boot1"})
    assert h.runtime.context(DEVICE) == {}
    h.announce.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_area_heartbeat_and_reconnect_do_not_count_as_movement():
    h = Harness(items=[beacon(1), beacon(2, area_id="area1")])
    await h.sync()
    await h.observe(1)
    for _ in range(4):
        h.clock.now += 5
        messages = await h.send("heartbeat", beacon={"mac": beacon(1).mac})
        assert all(item["action"] != "request_hello" for item in messages)
    await h.observe(2)
    assert h.runtime.state(DEVICE).pending is None
    conn = h.conn()
    await h.runtime.bind(conn)
    h.runtime.unbind(conn)
    await h.runtime.bind(h.conn("session2"))
    h.announce.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", ["client_is_speaking", "client_have_voice", "guide_chat_count", "calling"])
async def test_active_conversation_is_not_interrupted(busy):
    h = Harness()
    await h.sync()
    conn = h.conn()
    await h.runtime.bind(conn)
    h.runtime.state(DEVICE).owner_ready = True
    setattr(conn, busy, True)
    await h.observe(1)
    await h.observe(2)
    await h.runtime.tick()
    h.announce.assert_not_awaited()
    h.clock.now += 15
    setattr(conn, busy, False)
    await h.runtime.tick()
    h.announce.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejected_legacy_event_skips_original_persistence_path():
    h = Harness()
    conn = h.conn()
    assert await handle_legacy_event(conn, {"event": "beacon_change", "beacon_mac": {"beacon_id": "DA:01:16:00:FF:FF"}})
    assert conn.beacon_location is None
    assert h.runtime.state(DEVICE).current is None
    assert h.client.reports == []
    h.announce.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_and_vision_strip_untrusted_persisted_attributes():
    h = Harness()
    conn = h.conn()
    raw = {"device_id": DEVICE, "language": "zh-CN", "last_beacon_id": "untrusted", "venue_id": "wrong"}
    assert filter_context(conn, raw) == {"device_id": DEVICE, "language": "zh-CN"}
    assert vision_context(conn.server, DEVICE, raw) == filter_context(conn, raw)
    await h.sync()
    await h.observe(1)
    safe = filter_context(conn, raw)
    assert safe["last_beacon_id"] == "asset-1" and safe["venue_id"] == "venue1"
    h.clock.now += 15
    assert "last_beacon_id" not in vision_context(conn.server, DEVICE, raw)


@pytest.mark.asyncio
async def test_large_policy_chunk_retry_and_complete_manifest_ack():
    h = Harness(items=[beacon(number) for number in range(1001)])
    first = (await h.send("sync"))[0]
    assert first["manifest"]["chunk_count"] == 8
    assert len(first["beacons"]) == 128
    assert len(json.dumps(first).encode()) < 100000
    incomplete = await h.send("policy_ack", policy_sha256=first["manifest"]["sha256"], chunk_count=8)
    assert incomplete[0]["accepted"] is False
    duplicate = {"type": "guide_control", "version": 1, "action": "sync", "boot_id": "boot1", "seq": 0, "request_id": "0"}
    response = await h.runtime.process(DEVICE, "connection1", duplicate)
    assert any(item.get("manifest") == first["manifest"] for item in response["messages"])
    all_beacons = first["beacons"]
    digest = hashlib.sha256((first["manifest"]["manifest_json"] + "\n" + first["chunk_json"] + "\n").encode())
    for index in range(1, 8):
        messages = await h.send("sync", chunk_index=index, policy_sha256=first["manifest"]["sha256"])
        all_beacons.extend(messages[0]["beacons"])
        digest.update((messages[0]["chunk_json"] + "\n").encode())
    assert len(all_beacons) == 1001
    assert len({item["mac"] for item in all_beacons}) == 1001
    assert digest.hexdigest() == first["manifest"]["sha256"]
    failed = await h.send("policy_ack", policy_sha256="wrong", chunk_count=8)
    assert failed[0]["accepted"] is False
    result = await h.send("policy_ack", policy_sha256=first["manifest"]["sha256"], chunk_count=8)
    assert result[0]["accepted"] is True
    assert h.runtime.state(DEVICE).ack_hash == first["manifest"]["sha256"]


@pytest.mark.asyncio
async def test_policy_lease_is_not_extended_when_refresh_fails():
    h = Harness()
    await h.sync()
    await h.observe(1)
    h.clock.now = 159
    await h.send("heartbeat", beacon={"mac": beacon(1).mac})
    h.client.failed = True
    await h.runtime.tick(refresh=True)
    h.clock.now = 160
    assert h.runtime.context(DEVICE) == {}


@pytest.mark.asyncio
async def test_acknowledged_manifest_renews_past_sixty_seconds_then_expires_on_outage():
    h = Harness()
    await h.sync()
    messages = await h.observe(1)
    manifest = next(item["manifest"] for item in messages if item["action"] == "policy")
    messages = await h.send("policy_ack", policy_sha256=manifest["sha256"], chunk_count=manifest["chunk_count"])
    assert messages[0]["lease"]["expires_at"] == 160
    for now in range(105, 200, 5):
        h.clock.now = now
        if (now - 100) % 30 == 0:
            await h.runtime.tick(refresh=True)
        messages = await h.send("heartbeat", beacon={"mac": beacon(1).mac})
        assert not any(item["action"] == "policy" for item in messages)
        lease = messages[0]["lease"]
        assert lease["expires_at"] > now
        assert lease["policy_revision"] == "1:1"
        assert lease["policy_sha256"] == manifest["sha256"]
    assert lease["expires_at"] == 250
    h.client.failed = True
    for now in range(200, 250, 5):
        h.clock.now = now
        if (now - 100) % 30 == 0:
            await h.runtime.tick(refresh=True)
        messages = await h.send("heartbeat", beacon={"mac": beacon(1).mac})
        assert messages[0]["lease"]["expires_at"] == 250
    h.clock.now = 250
    messages = await h.send("heartbeat", beacon={"mac": beacon(1).mac})
    assert messages[0]["accepted"] is False
    assert "lease" not in messages[0]
    assert h.runtime.context(DEVICE) == {}
    old_request = {"type": "guide_control", "version": 1, "action": "heartbeat", "boot_id": "boot1",
                   "seq": h.seq - 2, "request_id": str(h.seq - 2), "policy_revision": "1:1", "beacon": {"mac": beacon(1).mac}}
    duplicate = await h.runtime.process(DEVICE, "connection1", old_request)
    assert "lease" not in duplicate["messages"][0]


@pytest.mark.asyncio
async def test_client_coalesces_policy_and_shared_venue_reads():
    clock = Clock()
    client = ManagementClient("http://unused", "token", clock=clock, wall_clock=clock)
    calls = []
    async def request(method, path, **kwargs):
        calls.append(path)
        await asyncio.sleep(0)
        if path == "/devices/sync":
            return {}
        if path == "/policies/query":
            return {"policies": [policy_data()]}
        return {"version": "1", "items": [beacon(1).wire()], "next_offset": None}
    client._request = request
    policies = await asyncio.gather(*(client.policy(DEVICE) for _ in range(100)))
    await asyncio.gather(*(client.resolve(DEVICE, policy, {"mac": beacon(1).mac}) for policy in policies))
    assert calls.count("/policies/query") == 1
    assert calls.count("/devices/sync") == 1
    assert calls.count("/venues/venue1/beacons") == 1
    await client.close()


@pytest.mark.asyncio
async def test_catalog_version_change_prevents_consumer_stale_acceptance():
    clock = Clock()
    client = ManagementClient("http://unused", "token", clock=clock, wall_clock=clock)
    active = {"version": "1", "items": [beacon(1).wire()], "next_offset": None}
    async def request(method, path, **kwargs):
        if path == "/beacons/resolve":
            assert "rssi" not in kwargs["json"]["beacons"][0]
            return {"policy": policy_data(mode="consumer", venue_ids=[], revision="1:2", catalog_revision="2"), "beacons": [], "rejected": []}
        return active
    client._request = request
    first = client._save_policy(policy_data(mode="consumer", venue_ids=[]))
    assert await client.resolve(DEVICE, first, {"mac": beacon(1).mac}, "venue1") == beacon(1)
    second = client._save_policy(policy_data(mode="consumer", venue_ids=[], revision="1:2", catalog_revision="2"))
    active.update(version="2", items=[])
    with pytest.raises(GuideError, match="beacon_not_allowed"):
        await client.resolve(DEVICE, second, {"mac": beacon(1).mac, "rssi": -45}, "venue1")
    await client.close()


@pytest.mark.asyncio
async def test_unknown_dynamic_candidates_are_negative_cached_and_rate_limited():
    clock = Clock()
    client = ManagementClient("http://unused", "token", clock=clock, wall_clock=clock)
    policy = client._save_policy(policy_data(mode="consumer", venue_ids=[]))
    client._request = AsyncMock(return_value={"policy": policy.data, "beacons": []})
    for _ in range(20):
        with pytest.raises(GuideError, match="beacon_not_allowed"):
            await client.resolve(DEVICE, policy, {"mac": beacon(99).mac})
    assert client._request.await_count == 1
    with pytest.raises(GuideError, match="resolve_rate_limited"):
        await client.resolve(DEVICE, policy, {"mac": beacon(98).mac})
    await client.close()


@pytest.mark.asyncio
async def test_http_control_requires_service_auth_and_uses_envelope_device_identity():
    h = Harness()
    app = web.Application()
    register_routes(app, SimpleNamespace(guide_runtime=h.runtime))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/internal/ksz/guide/control"
    body = {"device_mac": DEVICE, "connection_id": "connection1", "transport": "mqtt", "message": {
        "type": "guide_control", "version": 1, "action": "sync", "boot_id": "boot1", "seq": 0,
        "request_id": "sync0", "device_mac": "00:00:00:00:00:00"}}
    try:
        async with ClientSession() as session:
            async with session.post(url, json=body) as response:
                assert response.status == 401
            async with session.post(url, headers={"Authorization": "Bearer " + "x" * 32}, json=body) as response:
                assert response.status == 200
                data = await response.json()
                assert data["messages"][0]["policy"]["device_mac"] == DEVICE
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("value", [
    {"mac": "DA:01:16"}, {"mac": beacon(1).mac, "uuid": "wrong"},
    {"mac": beacon(1).mac, "rssi": float("nan")},
    {"mac": beacon(1).mac, "uuid": "FDA50693-A4E2-4FB1-AFCF-C6EB07647825", "major": True, "minor": 1},
])
def test_malformed_radio_identity_is_rejected(value):
    with pytest.raises(GuideError):
        beacon_identity(value)


def test_chat_activity_guard_cleans_up_after_exception():
    h = Harness()
    conn = h.conn()
    @track_chat
    def failing(instance):
        assert instance.guide_chat_count == 1
        raise ValueError("failed")
    with pytest.raises(ValueError):
        failing(conn)
    assert conn.guide_chat_count == 0


def test_new_protocol_requires_registered_ibeacon_identity_but_legacy_mac_still_works():
    item = beacon(1, uuid="FDA50693-A4E2-4FB1-AFCF-C6EB07647825", major=10001, minor=15609)
    assert not item.matches({"mac": item.mac})
    assert item.matches({"mac": item.mac, "_legacy": True})
    assert item.matches({"mac": item.mac, "uuid": item.uuid, "major": 10001, "minor": 15609})
    assert not item.matches({"mac": item.mac, "uuid": item.uuid, "major": 10001, "minor": 15604})


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [{}, {"boot_id": "", "session_attempt": 1},
    {"boot_id": "x" * 129, "session_attempt": 1}, {"boot_id": "boot1", "session_attempt": 2**53},
    {"boot_id": "boot1", "session_attempt": True}])
async def test_normal_v1_hello_validates_identity(metadata):
    h = Harness()
    assert not await h.runtime.bind(h.conn(), metadata)


@pytest.mark.asyncio
async def test_known_v1_device_cannot_skip_audio_readiness_with_legacy_hello():
    h = Harness()
    await h.sync()
    conn = h.conn()
    assert await h.runtime.bind(conn)
    assert h.runtime.state(DEVICE).owner_ready is False


@pytest.mark.asyncio
async def test_cold_start_devices_are_batched_and_sync_policy_is_reused():
    clock = Clock()
    client = ManagementClient("http://unused", "token", clock=clock, wall_clock=clock)
    calls = []
    async def request(method, path, **kwargs):
        calls.append((path, len(kwargs["json"]["devices"])))
        assert len(kwargs["json"]["devices"]) <= 100
        return {"policies": [policy_data(device_mac=item["device_mac"]) for item in kwargs["json"]["devices"]]}
    client._request = request
    await asyncio.gather(*(client.policy(beacon(number).mac) for number in range(1001)))
    assert len(calls) == 11
    assert all(path == "/devices/sync" for path, _ in calls)
    await client.close()


def test_enabled_extension_fails_when_package_is_not_installed():
    path = Path(__file__).resolve().parents[3] / "main/xiaozhi-server/core/utils/ksz_extension.py"
    enabled = subprocess.run([sys.executable, "-I", str(path)], env={**os.environ, "KSZ_GUIDE_ENABLED": "1"}, capture_output=True, text=True)
    assert enabled.returncode != 0
    assert "KSZ_GUIDE_ENABLED requires the ksz_guide package" in enabled.stderr
    disabled = subprocess.run([sys.executable, "-I", str(path)], env={**os.environ, "KSZ_GUIDE_ENABLED": "0"}, capture_output=True, text=True)
    assert disabled.returncode == 0


@pytest.mark.asyncio
async def test_real_http_server_constructor_and_start_register_control_route(monkeypatch):
    class Placeholder:
        def __init__(self, *args, **kwargs):
            pass

        def __getattr__(self, name):
            async def handle(*args, **kwargs):
                return web.json_response({})
            return handle

    for module, name in (
        ("core.api.device_command_handler", "DeviceCommandHandler"),
        ("core.api.ota_handler", "OTAHandler"), ("core.api.vision_handler", "VisionHandler"),
    ):
        monkeypatch.setitem(sys.modules, module, SimpleNamespace(**{name: Placeholder}))
    from unittest.mock import MagicMock
    monkeypatch.setitem(sys.modules, "config.logger", SimpleNamespace(setup_logging=lambda *args: MagicMock()))
    path = Path(__file__).resolve().parents[3] / "main/xiaozhi-server/core/http_server.py"
    spec = importlib.util.spec_from_file_location("ksz_http_smoke", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    h = Harness()
    websocket_server = SimpleNamespace(guide_runtime=h.runtime)
    http = module.SimpleHttpServer({"server": {"http_port": 1}, "read_config_from_api": True}, websocket_server)
    assert http.websocket_server is websocket_server
    real_site = web.TCPSite
    started = asyncio.Event()
    port = []
    class Site:
        def __init__(self, runner, host, requested_port):
            self.site = real_site(runner, "127.0.0.1", 0)

        async def start(self):
            await self.site.start()
            port.append(self.site._server.sockets[0].getsockname()[1])
            started.set()
    monkeypatch.setattr(module.web, "TCPSite", Site)
    task = asyncio.create_task(http.start())
    try:
        await asyncio.wait_for(started.wait(), 2)
        async with ClientSession() as session:
            async with session.post(f"http://127.0.0.1:{port[0]}/internal/ksz/guide/control") as response:
                assert response.status == 401
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert h.runtime.closed
