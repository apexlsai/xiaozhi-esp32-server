from types import SimpleNamespace

import pytest

from ksz_guide.settings import GuideSettings
from test_guide import DEVICE, Harness, beacon


def members(count=2):
    return [beacon(number, area_id="area1", area="展区1", place_id="place1", place_name="入口",
                   place_beacon_count=count) for number in range(1, count + 1)]


async def observer(h, transport):
    if transport == "v1":
        await h.sync()
    conn = h.conn()
    await h.runtime.bind(conn)
    h.runtime.state(DEVICE).owner_ready = True

    async def observe(number):
        if transport == "v1":
            assert (await h.observe(number))[0]["accepted"] is True
        else:
            assert await h.runtime.legacy(conn, {"beacon_id": beacon(number).mac})

    return observe


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["legacy", "v1"])
@pytest.mark.parametrize("count", [1, 2, 3])
async def test_position_member_count_selects_one_exclusive_trigger_mode(transport, count):
    h = Harness(items=members(count))
    observe = await observer(h, transport)
    try:
        await observe(1)
        assert h.announce.await_count == (1 if count == 1 else 0)
        await observe(1)
        assert h.announce.await_count == (1 if count == 1 else 0)
        if count > 1:
            h.clock.now = 102
            await observe(2)
            h.announce.assert_awaited_once()
        h.clock.now = 180
        await h.runtime.tick(refresh=True)
        await observe(1)
        if count > 1:
            await observe(count)
        h.announce.assert_awaited_once()
        assert h.runtime.state(DEVICE).pending is None
        assert h.runtime.location(DEVICE).to_prompt_context() == "1F，展区1，入口"
    finally:
        await h.runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["legacy", "v1"])
async def test_same_position_switch_keeps_one_pending_and_other_position_rearms(transport):
    h = Harness(items=[*members(), beacon(3, area_id="area1", place_id="place2")])
    observe = await observer(h, transport)
    try:
        await observe(3)
        h.clock.now = 101
        await observe(1)
        assert h.runtime.state(DEVICE).pending is None
        h.clock.now = 102
        await observe(2)
        pending = h.runtime.state(DEVICE).pending
        assert pending is not None
        h.clock.now = 104
        await observe(1)
        assert h.runtime.state(DEVICE).pending is pending
        h.clock.now = 115
        await h.runtime.tick()
        assert [call.args[1].place_id for call in h.announce.await_args_list] == ["place2", "place1"]
        h.clock.now = 180
        await h.runtime.tick(refresh=True)
        await observe(2)
        await observe(1)
        assert h.announce.await_count == 2
        await observe(3)
        h.clock.now = 181
        await observe(1)
        h.clock.now = 182
        await observe(2)
        h.clock.now = 195
        await h.runtime.tick()
        assert [call.args[1].place_id for call in h.announce.await_args_list] == ["place2", "place1", "place2", "place1"]
    finally:
        await h.runtime.close()


@pytest.mark.asyncio
async def test_transition_survives_sparse_reports_but_cannot_cross_another_position():
    h = Harness(items=[*members(), beacon(3, place_id="place2", place_beacon_count=2)])
    observe = await observer(h, "legacy")
    try:
        await observe(1)
        h.clock.now = 120
        await h.runtime.tick()
        assert h.runtime.location(DEVICE) is None
        h.clock.now = 130
        await observe(2)
        h.announce.assert_awaited_once()
        await observe(3)
        await observe(1)
        assert h.runtime.state(DEVICE).pending is None
        assert h.runtime.state(DEVICE).announcement_status == "waiting_for_place_transition"
        h.announce.assert_awaited_once()
    finally:
        await h.runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["finished", "cancelled", "running"])
async def test_finished_or_cancelled_photo_does_not_block_later_beacon(status):
    h = Harness()
    conn = h.conn()
    conn.vision_sessions = SimpleNamespace(active=SimpleNamespace(
        finished=status == "finished", cancelled=status == "cancelled"))
    try:
        assert await h.runtime.legacy(conn, {"beacon_id": beacon(1).mac})
        assert h.announce.await_count == (0 if status == "running" else 1)
        if status == "running":
            assert h.runtime.report(h.runtime.state(DEVICE))["announcement_status"] == "vision_active"
            conn.vision_sessions.active.finished = True
            await h.runtime.tick()
            h.announce.assert_awaited_once()
    finally:
        await h.runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("tts unavailable"), False])
async def test_failed_announcement_can_retry_without_another_member_transition(failure):
    h = Harness(items=members())
    observe = await observer(h, "legacy")
    h.announce.side_effect = [failure, None]
    try:
        await observe(1)
        await observe(2)
        assert h.runtime.state(DEVICE).last_announced_zone is None
        h.clock.now = 116
        await observe(2)
        assert h.announce.await_count == 2
        assert h.runtime.state(DEVICE).last_announced_zone == ("venue1", "place1")
    finally:
        await h.runtime.close()


@pytest.mark.asyncio
async def test_explicit_lost_requires_a_new_multiple_member_transition():
    h = Harness(items=members())
    observe = await observer(h, "v1")
    try:
        await observe(1)
        await h.send("lost")
        await observe(2)
        h.announce.assert_not_awaited()
        await observe(1)
        h.announce.assert_awaited_once()
    finally:
        await h.runtime.close()


@pytest.mark.asyncio
async def test_first_beacon_disabled_does_not_disable_multiple_member_confirmation():
    h = Harness(items=members(), settings=GuideSettings(announce_on_first_beacon=False))
    observe = await observer(h, "legacy")
    try:
        await observe(1)
        h.announce.assert_not_awaited()
        await observe(2)
        h.announce.assert_awaited_once()
    finally:
        await h.runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_member", [1, 2])
async def test_returning_to_a_different_member_still_respects_shared_position_cooldown(first_member):
    h = Harness(items=[*members(), beacon(3, place_id="place2")])
    observe = await observer(h, "legacy")
    try:
        await observe(1)
        await observe(2)
        h.clock.now = 115
        await observe(3)
        h.clock.now = 116
        await observe(first_member)
        await observe(3 - first_member)
        assert h.announce.await_count == 2
        assert h.runtime.state(DEVICE).pending is None
        h.clock.now = 161
        await h.runtime.tick(refresh=True)
        assert h.announce.await_count == 2
        await observe(first_member)
        assert h.announce.await_count == 3
    finally:
        await h.runtime.close()
