import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.handle.deviceEventHandle import _handle_beacon_change, handle_device_event
from core.utils.beacon_location import (
    BeaconLocation,
    fetch_beacon_location,
    parse_beacon_location,
)


class _Response:
    def __init__(self, status, payload=None, error=None):
        self.status = status
        self.payload = payload
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.payload


class _Session:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, *args, **kwargs):
        return self.response


class BeaconLocationTests(unittest.TestCase):
    def setUp(self):
        self.logger = MagicMock()
        self.logger.bind.return_value = self.logger

    def test_parse_accepts_location_fields(self):
        location = parse_beacon_location(
            "jx-pxm-003",
            {
                "beacon_id": "jx-pxm-003",
                "floor": "2F",
                "area": "古代文明展厅",
                "location_description": "北墙 A12 展柜旁",
            },
        )

        self.assertEqual(location.to_prompt_context(), "2F，古代文明展厅，北墙 A12 展柜旁")

    def test_parse_rejects_mismatched_or_empty_response(self):
        self.assertIsNone(parse_beacon_location("jx-pxm-003", {"beacon_id": "other"}))
        self.assertIsNone(parse_beacon_location("jx-pxm-003", {"beacon_id": "jx-pxm-003"}))

    def test_fetch_returns_location_for_success_response(self):
        response = _Response(
            200,
            {
                "beacon_id": "jx-pxm-003",
                "floor": "2F",
                "area": "古代文明展厅",
                "location_description": "北墙 A12 展柜旁",
            },
        )
        with patch(
            "core.utils.beacon_location.aiohttp.ClientSession",
            return_value=_Session(response),
        ):
            location = asyncio.run(fetch_beacon_location("jx-pxm-003", self.logger))

        self.assertEqual(location.area, "古代文明展厅")

    def test_fetch_returns_none_for_not_found_or_timeout(self):
        for response in (_Response(404), _Response(200, error=TimeoutError())):
            with patch(
                "core.utils.beacon_location.aiohttp.ClientSession",
                return_value=_Session(response),
            ):
                self.assertIsNone(
                    asyncio.run(fetch_beacon_location("jx-pxm-003", self.logger))
                )


class BeaconEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_beacon_mac_event_triggers_beacon_lookup(self):
        conn = SimpleNamespace()
        with (
            patch(
                "core.handle.deviceEventHandle._handle_beacon_change",
                new_callable=AsyncMock,
            ) as handle_beacon_change,
            patch(
                "core.handle.deviceEventHandle._report_event_to_manager_api",
                return_value=object(),
            ),
            patch("core.handle.deviceEventHandle.asyncio.create_task"),
        ):
            await handle_device_event(
                conn,
                {
                    "type": "device_event",
                    "event": "beacon_change",
                    "beacon_mac": {
                        "beacon_id": "DA:01:16:00:08:87",
                        "rssi": -65,
                    },
                    "timestamp": 1785223540,
                },
            )

        handle_beacon_change.assert_awaited_once_with(
            conn,
            "DA:01:16:00:08:87",
            {"beacon_id": "DA:01:16:00:08:87", "rssi": -65},
        )

    async def test_first_beacon_only_caches_location(self):
        conn = SimpleNamespace(
            device_attributes={},
            refresh_beacon_location=AsyncMock(),
        )
        with patch(
            "core.handle.deviceEventHandle._trigger_beacon_chat", new_callable=AsyncMock
        ) as trigger:
            await _handle_beacon_change(conn, "jx-pxm-003", {})

        conn.refresh_beacon_location.assert_awaited_once_with(force=True)
        trigger.assert_not_awaited()

    async def test_changed_beacon_uses_resolved_location_for_chat(self):
        location = BeaconLocation("jx-pxm-003", "2F", "古代文明展厅", "北墙 A12 展柜旁")
        conn = SimpleNamespace(
            device_attributes={"last_beacon_id": "previous"},
            beacon_location=location,
            refresh_beacon_location=AsyncMock(),
        )
        with patch(
            "core.handle.receiveAudioHandle.startToChat", new_callable=AsyncMock
        ) as start_chat:
            await _handle_beacon_change(conn, "jx-pxm-003", {})

        prompt = start_chat.await_args.args[1]
        self.assertIn("2F，古代文明展厅，北墙 A12 展柜旁", prompt)
        self.assertIn("不要编造具体展品", prompt)

    async def test_changed_beacon_degrades_when_location_missing(self):
        conn = SimpleNamespace(
            device_attributes={"last_beacon_id": "previous"},
            beacon_location=None,
            refresh_beacon_location=AsyncMock(),
        )
        with patch(
            "core.handle.receiveAudioHandle.startToChat", new_callable=AsyncMock
        ) as start_chat:
            await _handle_beacon_change(conn, "jx-pxm-003", {"beacon_name": "入口"})

        prompt = start_chat.await_args.args[1]
        self.assertIn("位置服务暂时不可用", prompt)
        self.assertIn("不要编造具体位置或展品", prompt)
