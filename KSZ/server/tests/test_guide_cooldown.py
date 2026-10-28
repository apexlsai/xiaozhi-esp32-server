import asyncio
from unittest.mock import AsyncMock

import pytest

from ksz_guide.settings import GuideSettings
from ksz_guide.models import Policy
from test_guide import DEVICE, Harness, beacon


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["legacy", "v1"])
@pytest.mark.parametrize("repeat_ms", [0, 60000])
async def test_returning_beacon_respects_repeat_cooldown_without_delayed_replay(transport, repeat_ms):
    h = Harness(settings=GuideSettings(same_beacon_cooldown_ms=repeat_ms))
    if transport == "v1":
        await h.sync()
    conn = h.conn()
    await h.runtime.bind(conn)
    h.runtime.state(DEVICE).owner_ready = True

    async def observe(number):
        if transport == "legacy":
            assert await h.runtime.legacy(conn, {"beacon_id": beacon(number).mac})
        else:
            messages = await h.observe(number)
            assert messages[0]["accepted"] is True

    try:
        await observe(1)
        h.clock.now = 115
        await observe(2)
        h.clock.now = 130
        await observe(1)
        assert h.announce.await_count == (2 if repeat_ms else 3)
        assert h.runtime.context(DEVICE)["beacon_mac"] == beacon(1).mac
        assert h.runtime.state(DEVICE).pending is None

        h.clock.now = 160
        await h.client.refresh_policies([DEVICE])
        await observe(1)
        await h.runtime.tick()
        assert h.announce.await_count == (2 if repeat_ms else 3)
        await observe(2)
        await observe(1)
        if repeat_ms:
            assert [call.args[1] for call in h.announce.await_args_list] == [beacon(1), beacon(2), beacon(1)]
        else:
            assert h.announce.await_count == 4
    finally:
        await h.runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reset", ["lost", "policy", "reconnect"])
async def test_position_reset_does_not_erase_recent_beacon_announcement(reset):
    h = Harness()
    await h.sync()
    conn = h.conn()
    await h.runtime.bind(conn)
    state = h.runtime.state(DEVICE)
    state.owner_ready = True
    try:
        await h.observe(1)
        h.announce.assert_awaited_once()
        h.clock.now += 1
        if reset == "lost":
            await h.send("lost")
        elif reset == "policy":
            h.client.data = {**h.client.data, "revision": "1:2"}
            h.client.policies[DEVICE] = Policy.parse(h.client.data, h.clock(), h.clock())
        else:
            h.runtime.unbind(conn)
            conn = h.conn("session2")
            await h.runtime.bind(conn)
            state.owner_ready = True
        await h.observe(1)
        h.announce.assert_awaited_once()
        assert h.runtime.context(DEVICE)["beacon_mac"] == beacon(1).mac
        assert state.pending is None
    finally:
        await h.runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["generating", "playing"])
async def test_default_interruption_cannot_bypass_cooldown(monkeypatch, phase):
    h = Harness()
    conn = h.conn()
    conn.sentence_id = "guide-turn"
    conn.client_is_speaking = False
    abort = AsyncMock(side_effect=lambda owner: setattr(owner, "client_is_speaking", False))
    monkeypatch.setattr("core.handle.abortHandle.handleAbortMessage", abort)

    async def announce(owner, item):
        if item == beacon(1):
            owner.client_is_speaking = True
            if phase == "generating":
                await asyncio.Future()
            h.runtime.state(DEVICE).announcement_sentence_id = owner.sentence_id

    h.announce.side_effect = announce
    try:
        await h.runtime.legacy(conn, {"beacon_id": beacon(1).mac})
        h.clock.now = 101
        await h.runtime.legacy(conn, {"beacon_id": beacon(2).mac})
        abort.assert_not_awaited()
        h.announce.assert_awaited_once()
        assert h.runtime.state(DEVICE).last_announce_at == 100

        h.clock.now = 114.9
        await h.runtime.legacy(conn, {"beacon_id": beacon(2).mac})
        abort.assert_not_awaited()
        h.clock.now = 115
        await h.runtime.tick()
        abort.assert_awaited_once_with(conn)
        assert [call.args[1] for call in h.announce.await_args_list] == [beacon(1), beacon(2)]
        assert h.runtime.state(DEVICE).last_announce_at == 115
    finally:
        await h.runtime.close()


@pytest.mark.asyncio
async def test_bouncing_back_during_playback_cancels_pending_repeat(monkeypatch):
    h = Harness()
    conn = h.conn()
    conn.client_is_speaking = False
    finish = asyncio.Event()
    abort = AsyncMock()
    monkeypatch.setattr("core.handle.abortHandle.handleAbortMessage", abort)

    async def announce(owner, item):
        owner.client_is_speaking = True
        await finish.wait()
        owner.client_is_speaking = False

    h.announce.side_effect = announce
    try:
        await h.runtime.legacy(conn, {"beacon_id": beacon(1).mac})
        task = h.runtime.state(DEVICE).announcement_task
        h.clock.now = 101
        await h.runtime.legacy(conn, {"beacon_id": beacon(2).mac})
        assert h.runtime.state(DEVICE).pending is not None
        h.clock.now = 102
        await h.runtime.legacy(conn, {"beacon_id": beacon(1).mac})
        assert h.runtime.state(DEVICE).pending is None
        finish.set()
        await task
        await h.runtime.tick()
        h.announce.assert_awaited_once_with(conn, beacon(1))
        abort.assert_not_awaited()
    finally:
        await h.runtime.close()
