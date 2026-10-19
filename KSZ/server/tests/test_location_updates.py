import asyncio
import ast
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ksz_guide.integration import on_hello, on_hello_complete
from test_guide import DEVICE, Harness, beacon


async def settle():
    for _ in range(12):
        await asyncio.sleep(0)


def messages(conn):
    return [json.loads(call.args[0]) for call in conn.websocket.send.await_args_list]


async def subscribe(h, session="session1"):
    conn = h.conn(session)
    conn.features = {"location_updates": True}
    await h.runtime.bind(conn)
    on_hello_complete(conn)
    await settle()
    return conn


@pytest.mark.asyncio
async def test_built_hello_handler_starts_snapshot_after_hello_and_only_once(tmp_path):
    root = Path(__file__).resolve().parents[3]
    handler = tmp_path / "core/handle/helloHandle.py"
    handler.parent.mkdir(parents=True)
    handler.write_text((root / "main/xiaozhi-server/core/handle/helloHandle.py").read_text())
    subprocess.run(["git", "apply", str(root / "KSZ/patches/location-updates.patch")], cwd=tmp_path, check=True)
    tree = ast.parse(handler.read_text())
    function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "handleHelloMessage")
    namespace = {"json": json, "asyncio": asyncio, "TAG": "test", "MCPClient": Mock(),
                 "send_mcp_initialize_message": AsyncMock(),
                 "guide": SimpleNamespace(on_hello=on_hello, on_hello_complete=on_hello_complete)}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(handler), "exec"), namespace)
    h = Harness(auto_announce=False)
    try:
        conn = h.conn()
        conn.features = {}
        conn.logger = Mock()
        conn.welcome_msg = {"type": "hello", "session_id": conn.session_id}
        hello = {"features": {"location_updates": True, "mcp": True}}
        await namespace["handleHelloMessage"](conn, hello)
        await settle()
        assert [message["type"] for message in messages(conn)] == ["hello", "guide_control"]
        assert messages(conn)[1]["status"] == "unknown"
        await namespace["handleHelloMessage"](conn, hello)
        await settle()
        assert len(h.runtime.location_updates.tasks) == 1
        assert len([message for message in messages(conn) if message["type"] == "guide_control"]) == 1
    finally:
        await h.runtime.close()


@pytest.mark.asyncio
async def test_first_position_renewal_and_expiry_without_speech():
    h = Harness(auto_announce=False)
    try:
        conn = await subscribe(h)
        assert messages(conn)[-1]["status"] == "unknown"
        await h.runtime.legacy(conn, {"beacon_id": beacon(1).mac})
        await settle()
        first = messages(conn)[-1]
        assert first["location"]["area"] == "展区1"
        assert first["session_id"] == conn.session_id
        assert first["expires_in_ms"] == 15000
        h.clock.now += 5
        await h.runtime.tick()
        await settle()
        assert len(messages(conn)) == 2
        await h.runtime.legacy(conn, {"beacon_id": beacon(1).mac})
        await settle()
        renewed = messages(conn)[-1]
        assert renewed["location_revision"] > first["location_revision"]
        assert renewed["location"] == first["location"]
        h.clock.now += 15
        await h.runtime.tick()
        await settle()
        assert messages(conn)[-1]["status"] == "lost"
        assert messages(conn)[-1]["location"] is None
        assert messages(conn)[-1]["expires_in_ms"] == 0
        h.announce.assert_not_awaited()
    finally:
        await h.runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["observation", "heartbeat"])
async def test_v1_observations_push_on_active_connection(action):
    h = Harness(auto_announce=False)
    try:
        await h.sync()
        conn = await subscribe(h)
        await h.send(action, beacon={"mac": beacon(1).mac})
        await settle()
        assert messages(conn)[-1]["location"]["beacon_mac"] == beacon(1).mac
        await h.send("lost")
        await settle()
        assert messages(conn)[-1]["status"] == "lost"
    finally:
        await h.runtime.close()


@pytest.mark.asyncio
async def test_unknown_and_cross_venue_observations_cannot_replace_valid_position():
    h = Harness(auto_announce=False, items=[beacon(1), beacon(2, venue_id="other")])
    try:
        conn = await subscribe(h)
        await h.runtime.legacy(conn, {"beacon_id": beacon(1).mac})
        await settle()
        count = len(messages(conn))
        for number in [2, 999]:
            assert not await h.runtime.legacy(conn, {"beacon_id": beacon(number).mac})
        await settle()
        assert len(messages(conn)) == count
        assert messages(conn)[-1]["location"]["beacon_mac"] == beacon(1).mac
    finally:
        await h.runtime.close()


@pytest.mark.asyncio
async def test_policy_expiry_limits_display_lease_and_policy_revocation_clears_it():
    h = Harness(auto_announce=False)
    try:
        conn = await subscribe(h)
        h.clock.now = 155
        await h.runtime.legacy(conn, {"beacon_id": beacon(1).mac})
        await settle()
        assert messages(conn)[-1]["expires_in_ms"] == 5000
        h.client.data.update(enabled=False, revision="2:1")
        await h.runtime.tick(refresh=True)
        await settle()
        assert messages(conn)[-1]["status"] == "lost"
        assert messages(conn)[-1]["location"] is None
    finally:
        await h.runtime.close()


@pytest.mark.asyncio
async def test_reconnect_gets_remaining_lease_and_old_connection_cannot_publish():
    h = Harness(auto_announce=False)
    try:
        old = await subscribe(h)
        await h.runtime.legacy(old, {"beacon_id": beacon(1).mac})
        await settle()
        old_count = len(messages(old))
        h.clock.now += 7
        new = await subscribe(h, "session2")
        assert messages(new)[-1]["expires_in_ms"] == 8000
        assert messages(new)[-1]["session_id"] == "session2"
        assert messages(new)[-1]["location_revision"] == 1
        h.runtime.unbind(old)
        await h.runtime.legacy(new, {"beacon_id": beacon(2).mac})
        await settle()
        assert len(messages(old)) == old_count
        assert messages(new)[-1]["location"]["beacon_id"] == beacon(2).beacon_id
        h.runtime.unbind(new)
        await settle()
        assert not h.runtime.location_updates.subscriptions
    finally:
        await h.runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("features", [{}, {"location_updates": False}, {"location_updates": "true"}])
async def test_old_devices_receive_no_location_events(features):
    h = Harness(auto_announce=False)
    try:
        conn = h.conn()
        conn.features = features
        await h.runtime.bind(conn)
        on_hello_complete(conn)
        await h.runtime.legacy(conn, {"beacon_id": beacon(1).mac})
        await settle()
        conn.websocket.send.assert_not_awaited()
    finally:
        await h.runtime.close()


@pytest.mark.asyncio
async def test_slow_or_failed_location_delivery_does_not_block_observation_or_announce():
    h = Harness()
    release = asyncio.Event()
    try:
        conn = await subscribe(h)
        conn.websocket.send = AsyncMock()

        async def slow_send(_):
            await release.wait()
            raise ConnectionError("closed")

        conn.websocket.send.side_effect = slow_send
        await asyncio.wait_for(h.runtime.legacy(conn, {"beacon_id": beacon(1).mac}), timeout=0.2)
        await settle()
        await asyncio.wait_for(h.runtime.legacy(conn, {"beacon_id": beacon(2).mac}), timeout=0.2)
        await settle()
        h.announce.assert_awaited_once_with(conn, beacon(2))
        assert h.runtime.location(DEVICE) == beacon(2)
        release.set()
        await settle()
        assert not h.runtime.location_updates.subscriptions
    finally:
        release.set()
        await h.runtime.close()


@pytest.mark.asyncio
async def test_shutdown_cancels_waiting_senders():
    h = Harness(auto_announce=False)
    await subscribe(h)
    assert len(h.runtime.location_updates.tasks) == 1
    await h.runtime.close()
    assert not h.runtime.location_updates.tasks
