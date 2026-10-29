import asyncio
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
async def test_any_single_member_triggers_independently_of_position_member_count(transport, count):
    h = Harness(items=members(count))
    observe = await observer(h, transport)
    try:
        await observe(1)
        h.announce.assert_awaited_once()
        assert h.runtime.location(DEVICE).mac == beacon(1).mac
        await observe(1)
        if count > 1:
            h.clock.now = 102
            await observe(count)
        h.announce.assert_awaited_once()
        h.clock.now = 180
        await h.runtime.tick(refresh=True)
        await observe(count)
        await observe(1)
        h.announce.assert_awaited_once()
        assert h.runtime.state(DEVICE).pending is None
        assert h.runtime.location(DEVICE).to_prompt_context() == "1F，展区1，入口"
    finally:
        await h.runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["legacy", "v1"])
async def test_one_member_creates_pending_and_same_position_members_keep_its_deadline(transport):
    h = Harness(items=[*members(), beacon(3, area_id="area1", place_id="place2")])
    observe = await observer(h, transport)
    try:
        await observe(3)
        h.clock.now = 101
        await observe(1)
        pending = h.runtime.state(DEVICE).pending
        assert pending is not None
        assert pending.deadline == 116
        h.clock.now = 102
        await observe(2)
        assert h.runtime.state(DEVICE).pending is pending
        assert pending.deadline == 116
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
        h.clock.now = 195
        await h.runtime.tick()
        assert [call.args[1].place_id for call in h.announce.await_args_list] == ["place2", "place1", "place2", "place1"]
    finally:
        await h.runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["legacy", "v1"])
async def test_new_position_with_multiple_members_accepts_just_one_member(transport):
    h = Harness(items=[*members(), beacon(3, area_id="area1", place_id="place2", place_beacon_count=2)])
    observe = await observer(h, transport)
    try:
        await observe(1)
        h.announce.assert_awaited_once()
        h.clock.now = 115
        await observe(3)
        assert h.runtime.location(DEVICE).place_id == "place2"
        assert h.runtime.context(DEVICE)["beacon_mac"] == beacon(3).mac
        assert [call.args[1].place_id for call in h.announce.await_args_list] == ["place1", "place2"]
    finally:
        await h.runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["legacy", "v1"])
@pytest.mark.parametrize("failure", [RuntimeError("tts unavailable"), False])
async def test_failed_announcement_retries_with_the_same_single_member(transport, failure):
    h = Harness(items=members())
    observe = await observer(h, transport)
    h.announce.side_effect = [failure, None]
    try:
        await observe(1)
        assert h.runtime.state(DEVICE).last_announced_zone is None
        h.clock.now = 116
        await observe(1)
        assert h.announce.await_count == 2
        assert h.runtime.state(DEVICE).last_announced_zone == ("venue1", "place1")
    finally:
        await h.runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reset", ["lost", "boot", "policy"])
async def test_reset_allows_single_member_recovery_without_collecting_a_pair(reset):
    h = Harness(items=members())
    observe = await observer(h, "v1")
    h.announce.side_effect = [False, None]
    try:
        await observe(1)
        h.clock.now = 115
        boot_id = "boot1"
        if reset == "lost":
            await h.send("lost")
        elif reset == "boot":
            boot_id = "boot2"
            await h.send("sync", boot_id=boot_id)
        else:
            h.client.data["revision"] = "2:1"
            await h.runtime.tick(refresh=True)
        h.runtime.state(DEVICE).owner_ready = True
        assert (await h.observe(2, boot_id=boot_id))[0]["accepted"] is True
        assert h.runtime.context(DEVICE)["beacon_mac"] == beacon(2).mac
        assert h.announce.await_count == 2
    finally:
        await h.runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["legacy", "v1"])
@pytest.mark.parametrize("count", [1, 2, 3])
async def test_first_beacon_setting_applies_to_every_position(transport, count):
    h = Harness(items=[*members(count), beacon(4, place_id="place2", place_beacon_count=2)],
                settings=GuideSettings(announce_on_first_beacon=False))
    observe = await observer(h, transport)
    try:
        await observe(1)
        await observe(1)
        h.announce.assert_not_awaited()
        assert h.runtime.context(DEVICE)["beacon_mac"] == beacon(1).mac
        assert h.runtime.state(DEVICE).announcement_status == "first_beacon_disabled"
        await observe(2 if count > 1 else 4)
        h.announce.assert_awaited_once()
    finally:
        await h.runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["legacy", "v1"])
@pytest.mark.parametrize("first_member", [1, 2])
async def test_returning_single_member_still_respects_shared_position_cooldown(transport, first_member):
    h = Harness(items=[*members(), beacon(3, place_id="place2")])
    observe = await observer(h, transport)
    try:
        await observe(1)
        h.clock.now = 115
        await observe(3)
        h.clock.now = 116
        await observe(first_member)
        assert h.announce.await_count == 2
        assert h.runtime.state(DEVICE).pending is None
        h.clock.now = 161
        await h.runtime.tick(refresh=True)
        assert h.announce.await_count == 2
        await observe(first_member)
        assert h.announce.await_count == 2
        await observe(3 - first_member)
        assert h.announce.await_count == 3
    finally:
        await h.runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["legacy", "v1"])
async def test_same_position_member_does_not_interrupt_running_announcement(transport):
    h = Harness(items=members(), settings=GuideSettings(min_announce_interval_ms=0))
    observe = await observer(h, transport)
    finished = asyncio.Event()

    async def announce(*_):
        await finished.wait()

    h.announce.side_effect = announce
    try:
        await observe(1)
        state = h.runtime.state(DEVICE)
        task = state.announcement_task
        h.clock.now = 110
        await observe(2)
        assert state.announcement_task is task
        assert not task.done()
        assert state.last_announced_zone == ("venue1", "place1")
        assert state.pending is None
        assert h.runtime.location(DEVICE).mac == beacon(2).mac
        finished.set()
        await task
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
