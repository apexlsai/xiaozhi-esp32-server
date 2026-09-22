import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ksz_guide.integration import create_runtime
from ksz_guide.settings import GuideSettings
from test_guide import DEVICE, Harness, beacon


@pytest.mark.parametrize("key,value", [
    ("AUTO_ANNOUNCE_ENABLED", "maybe"),
    ("ANNOUNCE_ON_FIRST_BEACON", ""),
    ("INTERRUPT_ON_BEACON_CHANGE", "2"),
    ("MIN_ANNOUNCE_INTERVAL_MS", "-1"),
    ("POSITION_TTL_MS", "0"),
    ("PENDING_TTL_MS", "0"),
    ("USER_QUIET_MS", "1.5"),
    ("USER_QUIET_MS", "86400001"),
])
def test_invalid_guide_settings_fail_with_environment_key(key, value):
    name = f"KSZ_GUIDE_{key}"
    with pytest.raises(ValueError, match=name):
        GuideSettings.from_env({name: value})


@pytest.mark.asyncio
async def test_environment_configures_runtime_and_effective_device_policy(monkeypatch):
    values = {
        "KSZ_GUIDE_ENABLED": "1", "KSZ_GUIDE_PRESENCE_ENABLED": "0",
        "KSZ_MANAGEMENT_API_URL": "http://unused/api/v1/integration",
        "KSZ_MANAGEMENT_API_TOKEN": "test-token", "KSZ_GUIDE_CONTROL_TOKEN": "x" * 32,
        "KSZ_GUIDE_AUTO_ANNOUNCE_ENABLED": " off ",
        "KSZ_GUIDE_ANNOUNCE_ON_FIRST_BEACON": "yes",
        "KSZ_GUIDE_INTERRUPT_ON_BEACON_CHANGE": "false",
        "KSZ_GUIDE_MIN_ANNOUNCE_INTERVAL_MS": "2500",
        "KSZ_GUIDE_POSITION_TTL_MS": "25000",
        "KSZ_GUIDE_PENDING_TTL_MS": "7500",
        "KSZ_GUIDE_USER_QUIET_MS": "1000",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    runtime = create_runtime(SimpleNamespace())
    try:
        assert runtime.settings == GuideSettings(
            auto_announce_enabled=False, announce_on_first_beacon=True,
            interrupt_on_beacon_change=False, min_announce_interval_ms=2500,
            position_ttl_ms=25000, pending_ttl_ms=7500, user_quiet_ms=1000,
        )
        h = Harness(settings=runtime.settings)
        configured = await h.sync()
        original = await Harness().sync()
        assert configured["policy"]["auto_announce"] is False
        assert configured["policy"]["filter"]["min_announce_interval_ms"] == 2500
        assert configured["manifest"]["sha256"] != original["manifest"]["sha256"]
        assert h.client.policies[DEVICE].data["auto_announce"] is True
        assert h.client.policies[DEVICE].data["filter"]["min_announce_interval_ms"] == 15000
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["legacy", "v1"])
@pytest.mark.parametrize("global_enabled,device_enabled", [(True, True), (False, True), (True, False)])
async def test_first_beacon_respects_global_and_device_switches(transport, global_enabled, device_enabled):
    h = Harness(settings=GuideSettings(auto_announce_enabled=global_enabled, announce_on_first_beacon=True),
                auto_announce=device_enabled)
    conn = h.conn()
    enabled = global_enabled and device_enabled
    if transport == "v1":
        await h.sync()

    async def observe(number):
        if transport == "legacy":
            await h.runtime.legacy(conn, {"beacon_id": beacon(number).mac})
        else:
            messages = await h.observe(number)
            for wake in (item for item in messages if item["action"] == "request_hello"):
                assert await h.runtime.bind(conn, {"activation_id": wake["activation_id"], "session_attempt": 1,
                                                   "boot_id": "boot1", "connection_id": "connection1"})
                await h.send("ready", activation_id=wake["activation_id"], session_attempt=1,
                             session_id=conn.session_id, udp_ready=True)

    await observe(1)
    assert h.announce.await_count == int(enabled)
    await observe(1)
    assert h.announce.await_count == int(enabled)
    h.clock.now += 15
    await observe(2)
    assert [call.args[1] for call in h.announce.await_args_list] == ([beacon(1), beacon(2)] if enabled else [])
    assert h.runtime.context(DEVICE)["beacon_mac"] == beacon(2).mac


@pytest.mark.asyncio
@pytest.mark.parametrize("interval_ms", [0, 5000])
async def test_configured_cooldown_controls_active_session(interval_ms):
    h = Harness(settings=GuideSettings(min_announce_interval_ms=interval_ms),
                items=[beacon(1), beacon(2), beacon(3)])
    conn = h.conn()
    for number in (1, 2):
        await h.runtime.legacy(conn, {"beacon_id": beacon(number).mac})
    h.announce.assert_awaited_once_with(conn, beacon(2))
    h.clock.now += 1
    await h.runtime.legacy(conn, {"beacon_id": beacon(3).mac})
    if interval_ms:
        h.announce.assert_awaited_once()
        h.clock.now = 100 + interval_ms / 1000
        await h.runtime.tick()
    assert [call.args[1] for call in h.announce.await_args_list] == [beacon(2), beacon(3)]


@pytest.mark.asyncio
async def test_configured_cooldown_also_controls_idle_wakeup():
    h = Harness(settings=GuideSettings(min_announce_interval_ms=5000),
                items=[beacon(1), beacon(2), beacon(3)])
    await h.sync()
    conn = h.conn()
    await h.runtime.bind(conn)
    h.runtime.state(DEVICE).owner_ready = True
    await h.observe(1)
    await h.observe(2)
    h.announce.assert_awaited_once()
    h.runtime.unbind(conn)
    h.clock.now = 104.9
    assert not any(item["action"] == "request_hello" for item in await h.observe(3))
    h.clock.now = 105
    messages = await h.send("heartbeat", beacon={"mac": beacon(3).mac})
    assert any(item["action"] == "request_hello" for item in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("expires", ["position", "pending"])
async def test_configured_expiry_prevents_stale_wakeup(expires):
    settings = GuideSettings(position_ttl_ms=3000 if expires == "position" else 30000,
                             pending_ttl_ms=3000 if expires == "pending" else 30000)
    h = Harness(settings=settings)
    await h.sync()
    await h.observe(1)
    wake = next(item for item in await h.observe(2) if item["action"] == "request_hello")
    assert wake["expires_in_ms"] == settings.pending_ttl_ms
    h.clock.now = 102.9
    await h.runtime.tick()
    assert h.runtime.context(DEVICE)["beacon_mac"] == beacon(2).mac
    assert h.runtime.state(DEVICE).pending is not None
    h.clock.now = 103
    assert bool(h.runtime.context(DEVICE)) == (expires != "position")
    await h.runtime.tick()
    assert h.runtime.state(DEVICE).pending is None
    assert not await h.runtime.bind(h.conn(), {"activation_id": wake["activation_id"],
                                              "session_attempt": 1, "boot_id": "boot1"})
    h.announce.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("quiet_ms", [0, 2000])
async def test_configured_user_quiet_period_defers_announcement(quiet_ms):
    h = Harness(settings=GuideSettings(user_quiet_ms=quiet_ms))
    conn = h.conn()
    await h.runtime.legacy(conn, {"beacon_id": beacon(1).mac})
    h.runtime.activity(conn, {"type": "listen", "state": "stop"})
    await h.runtime.legacy(conn, {"beacon_id": beacon(2).mac})
    if quiet_ms:
        h.announce.assert_not_awaited()
        h.clock.now += quiet_ms / 1000
        await h.runtime.tick()
    h.announce.assert_awaited_once_with(conn, beacon(2))


@pytest.mark.asyncio
async def test_disabled_interruption_waits_for_current_guide(monkeypatch):
    h = Harness(settings=GuideSettings(interrupt_on_beacon_change=False, min_announce_interval_ms=0),
                items=[beacon(1), beacon(2), beacon(3)])
    conn = h.conn()
    finish = asyncio.Event()
    abort = AsyncMock()
    monkeypatch.setattr("core.handle.abortHandle.handleAbortMessage", abort)

    async def announce(owner, item):
        if item == beacon(2):
            owner.client_is_speaking = True
            await finish.wait()
            owner.client_is_speaking = False

    h.announce.side_effect = announce
    try:
        for number in (1, 2, 3):
            await h.runtime.legacy(conn, {"beacon_id": beacon(number).mac})
        abort.assert_not_awaited()
        h.announce.assert_awaited_once_with(conn, beacon(2))
        finish.set()
        await h.runtime.state(DEVICE).announcement_task
        await h.runtime.tick()
        assert [call.args[1] for call in h.announce.await_args_list] == [beacon(2), beacon(3)]
    finally:
        await h.runtime.close()
