from copy import deepcopy
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest

from ksz_guide.client import ManagementClient
from ksz_guide.models import GuideError
from ksz_guide.settings import GuideSettings
from test_guide import DEVICE, Harness


def configuration(revision="a", **changes):
    return {"revision": revision * 64, "config": {"guide": {
        **asdict(GuideSettings()), **changes,
    }}}


@pytest.mark.asyncio
async def test_live_configuration_replaces_settings_and_requires_new_device_ack():
    h = Harness()
    original = await h.sync()
    assert h.runtime.state(DEVICE).ack_revision == h.client.data["revision"]
    h.client.beacon_configuration = AsyncMock(return_value=configuration(
        min_announce_interval_ms=2500, same_beacon_cooldown_ms=120000,
        announce_on_first_beacon=False,
    ))
    h.client.data.update(
        revision="1:1:" + "a" * 64, config_revision="a" * 64,
        filter={"min_announce_interval_ms": 2500, "heartbeat_interval_ms": 3000},
    )
    await h.runtime.tick()
    assert h.runtime.settings.min_announce_interval_ms == 2500
    assert h.runtime.settings.same_beacon_cooldown_ms == 120000
    assert h.runtime.settings.announce_on_first_beacon is False
    assert h.runtime.state(DEVICE).ack_revision is None
    part = next(item for item in await h.send("sync") if item["action"] == "policy")
    assert part["manifest"]["sha256"] != original["manifest"]["sha256"]
    assert part["policy"]["filter"]["heartbeat_interval_ms"] == 3000
    assert part["policy"]["filter"]["min_announce_interval_ms"] == 2500
    assert h.runtime.report(h.runtime.state(DEVICE))["config_revision"] == "a" * 64
    await h.send("policy_ack", policy_sha256=part["manifest"]["sha256"], chunk_count=1)
    assert h.runtime.state(DEVICE).ack_revision == h.client.data["revision"]
    h.clock.now += 4
    await h.runtime.tick()
    assert h.client.beacon_configuration.await_count == 1
    h.clock.now += 1
    await h.runtime.tick()
    assert h.client.beacon_configuration.await_count == 2


@pytest.mark.asyncio
async def test_global_disable_cancels_pending_and_failures_keep_settings_without_renewing_lease():
    h = Harness()
    await h.sync()
    await h.observe()
    assert h.runtime.state(DEVICE).pending
    h.client.beacon_configuration = AsyncMock(return_value=configuration(auto_announce_enabled=False))
    h.client.data.update(revision="1:1:" + "a" * 64, auto_announce=False)
    await h.runtime.tick()
    state = h.runtime.state(DEVICE)
    assert state.pending is None
    assert h.runtime.settings.auto_announce_enabled is False
    deadline = state.policy.deadline
    h.client.failed = True
    h.client.beacon_configuration.side_effect = GuideError("management_unavailable")
    h.clock.now += 5
    await h.runtime.tick(refresh=True)
    assert h.runtime.settings.auto_announce_enabled is False
    assert h.runtime.config_revision == "a" * 64
    assert state.policy.deadline == deadline
    h.announce.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {}, {"revision": "bad"}, [],
    configuration(auto_announce_enabled="false"),
    configuration(min_announce_interval_ms=-1),
    configuration(location_templates={"zh-CN": "{location} {other}"}),
    configuration(position_ttl_ms=True),
])
async def test_malformed_remote_config_preserves_environment_and_retries(payload):
    h = Harness(settings=GuideSettings(same_beacon_cooldown_ms=12345))
    before = h.runtime.settings
    h.client.beacon_configuration = AsyncMock(return_value=deepcopy(payload))
    await h.runtime.tick()
    assert h.runtime.settings is before
    assert h.runtime.config_revision is None
    h.clock.now += 5
    h.client.beacon_configuration.return_value = configuration()
    await h.runtime.tick()
    assert h.runtime.settings.same_beacon_cooldown_ms == 60000
    assert h.runtime.config_revision == "a" * 64


@pytest.mark.asyncio
async def test_configuration_uses_existing_integration_base_url():
    client = ManagementClient("http://management/api/v1/integration", "secret")
    client._request = AsyncMock(return_value=configuration())
    assert await client.beacon_configuration() == configuration()
    client._request.assert_awaited_once_with("GET", "/beacon-config")
    await client.close()
