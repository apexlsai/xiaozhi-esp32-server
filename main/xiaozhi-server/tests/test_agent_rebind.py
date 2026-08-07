import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.api.device_command_handler import DeviceCommandHandler
from core.handle.deviceEventHandle import (
    LANGUAGE_AGENT_SUFFIXES,
    handle_device_event,
    resolve_language_agent_name,
)


class AgentRebindEventTests(unittest.TestCase):
    def setUp(self):
        self.conn = SimpleNamespace(
            device_id="aa:bb:cc:dd:ee:ff",
            read_config_from_api=True,
            websocket=MagicMock(),
            device_attributes={},
        )
        self.conn.websocket.send = AsyncMock()
        self.conn.websocket.close = AsyncMock()

    def test_rebind_uses_connection_mac_and_closes_on_success(self):
        async def run():
            with patch(
                "core.handle.deviceEventHandle.rebind_device_agent",
                new_callable=AsyncMock,
            ) as rebind, patch(
                "core.handle.deviceEventHandle.asyncio.sleep",
                new_callable=AsyncMock,
            ):
                rebind.return_value = {
                    "deviceId": "aa:bb:cc:dd:ee:ff",
                    "agentName": "导游B",
                    "confirmed": True,
                    "reconnectRequired": True,
                }
                await handle_device_event(
                    self.conn,
                    {
                        "type": "device_event",
                        "event": "agent_rebind",
                        "request_id": "req-1",
                        "payload": {
                            "current_agent_name": "导游A",
                            "target_agent_name": "导游B",
                            "confirm": True,
                        },
                    },
                )
                rebind.assert_awaited_once_with(
                    device_id="aa:bb:cc:dd:ee:ff",
                    current_agent_name="导游A",
                    target_agent_name="导游B",
                    confirm=True,
                )
                sent = json.loads(self.conn.websocket.send.await_args.args[0])
                self.assertEqual(sent["type"], "device_event_result")
                self.assertTrue(sent["success"])
                self.assertEqual(sent["request_id"], "req-1")
                self.assertEqual(sent["data"]["agentName"], "导游B")
                self.conn.websocket.close.assert_awaited_once()

        asyncio.run(run())

    def test_rebind_failure_returns_error_without_close(self):
        async def run():
            with patch(
                "core.handle.deviceEventHandle.rebind_device_agent",
                new_callable=AsyncMock,
            ) as rebind:
                rebind.side_effect = Exception("API返回错误: 当前智能体名称与设备绑定不符")
                await handle_device_event(
                    self.conn,
                    {
                        "type": "device_event",
                        "event": "agent_rebind",
                        "request_id": "req-2",
                        "payload": {
                            "current_agent_name": "导游A",
                            "target_agent_name": "导游B",
                            "confirm": True,
                        },
                    },
                )
                sent = json.loads(self.conn.websocket.send.await_args.args[0])
                self.assertFalse(sent["success"])
                self.assertEqual(sent["request_id"], "req-2")
                self.assertIn("当前智能体名称", sent["error"])
                self.conn.websocket.close.assert_not_awaited()

        asyncio.run(run())

    def test_rebind_requires_confirm(self):
        async def run():
            with patch(
                "core.handle.deviceEventHandle.rebind_device_agent",
                new_callable=AsyncMock,
            ) as rebind:
                await handle_device_event(
                    self.conn,
                    {
                        "type": "device_event",
                        "event": "agent_rebind",
                        "payload": {
                            "current_agent_name": "导游A",
                            "target_agent_name": "导游B",
                            "confirm": False,
                        },
                    },
                )
                rebind.assert_not_awaited()
                sent = json.loads(self.conn.websocket.send.await_args.args[0])
                self.assertFalse(sent["success"])
                self.assertIn("确认", sent["error"])

        asyncio.run(run())


class LanguageChangeEventTests(unittest.TestCase):
    def setUp(self):
        self.conn = SimpleNamespace(
            device_id="aa:bb:cc:dd:ee:ff",
            read_config_from_api=True,
            websocket=MagicMock(),
            device_attributes={},
            device_language=None,
        )
        self.conn.websocket.send = AsyncMock()
        self.conn.websocket.close = AsyncMock()

    def test_language_change_triggers_rebind_with_current_from_attributes(self):
        async def run():
            self.conn.device_attributes = {"agent_name": "导游A"}
            with patch(
                "core.handle.deviceEventHandle.rebind_device_agent",
                new_callable=AsyncMock,
            ) as rebind, patch(
                "core.handle.deviceEventHandle.asyncio.sleep",
                new_callable=AsyncMock,
            ):
                rebind.return_value = {
                    "deviceId": "aa:bb:cc:dd:ee:ff",
                    "agentName": "English Guide",
                }
                await handle_device_event(
                    self.conn,
                    {
                        "type": "device_event",
                        "event": "language_change",
                        "request_id": "lang-1",
                        "payload": {
                            "language": "en",
                            "target_agent_name": "English Guide",
                        },
                    },
                )
                rebind.assert_awaited_once_with(
                    device_id="aa:bb:cc:dd:ee:ff",
                    current_agent_name="导游A",
                    target_agent_name="English Guide",
                    confirm=True,
                )
                sent = json.loads(self.conn.websocket.send.await_args.args[0])
                self.assertEqual(sent["event"], "language_change")
                self.assertTrue(sent["success"])
                self.assertEqual(sent["request_id"], "lang-1")
                self.conn.websocket.close.assert_awaited_once()
                self.assertEqual(self.conn.device_language, "en")
                self.assertEqual(self.conn.device_attributes["language"], "en")

        asyncio.run(run())

    def test_language_change_derives_target_from_agent_name(self):
        async def run():
            self.conn.device_attributes = {"agent_name": "小硕-汉语"}
            with patch(
                "core.handle.deviceEventHandle.rebind_device_agent",
                new_callable=AsyncMock,
            ) as rebind, patch(
                "core.handle.deviceEventHandle.asyncio.sleep",
                new_callable=AsyncMock,
            ):
                rebind.return_value = {"agentName": "小硕-英语"}
                await handle_device_event(
                    self.conn,
                    {
                        "type": "device_event",
                        "event": "language_change",
                        "request_id": "lang-2",
                        "payload": {"language": "en"},
                    },
                )
                rebind.assert_awaited_once_with(
                    device_id="aa:bb:cc:dd:ee:ff",
                    current_agent_name="小硕-汉语",
                    target_agent_name="小硕-英语",
                    confirm=True,
                )
                sent = json.loads(self.conn.websocket.send.await_args.args[0])
                self.assertTrue(sent["success"])
                self.assertEqual(sent["event"], "language_change")
                self.conn.websocket.close.assert_awaited_once()

        asyncio.run(run())

    def test_all_canonical_languages_derive_agent_suffix(self):
        for language, target_suffix in LANGUAGE_AGENT_SUFFIXES.items():
            with self.subTest(language=language):
                self.assertEqual(
                    resolve_language_agent_name("小硕-汉语", language),
                    f"小硕-{target_suffix}",
                )

    def test_language_change_rejects_noncanonical_case(self):
        async def run():
            self.conn.device_attributes = {"agent_name": "小硕-汉语"}
            with patch(
                "core.handle.deviceEventHandle.rebind_device_agent",
                new_callable=AsyncMock,
            ) as rebind:
                await handle_device_event(
                    self.conn,
                    {
                        "type": "device_event",
                        "event": "language_change",
                        "request_id": "lang-invalid",
                        "payload": {"language": "zh-cn"},
                    },
                )
                rebind.assert_not_awaited()
                sent = json.loads(self.conn.websocket.send.await_args.args[0])
                self.assertFalse(sent["success"])
                self.assertEqual(sent["request_id"], "lang-invalid")
                self.assertIn("规范码", sent["error"])
                self.assertNotIn("language", self.conn.device_attributes)

        asyncio.run(run())

    def test_internal_http_language_change_rebinds_directly(self):
        async def run():
            self.conn.device_attributes = {"agent_name": "小硕-英语"}
            request = SimpleNamespace(
                headers={"Authorization": "Bearer test-secret"},
                json=AsyncMock(
                    return_value={
                        "deviceId": "aa:bb:cc:dd:ee:ff",
                        "language": "zh-CN-yue",
                    }
                ),
            )
            with patch(
                "core.api.device_command_handler.setup_logging",
                return_value=MagicMock(),
            ), patch(
                "core.handle.deviceEventHandle.rebind_device_agent",
                new_callable=AsyncMock,
            ) as rebind, patch(
                "core.handle.deviceEventHandle.asyncio.sleep",
                new_callable=AsyncMock,
            ):
                handler = DeviceCommandHandler(
                    {"manager-api": {"secret": "test-secret"}},
                    SimpleNamespace(
                        find_device_connection=MagicMock(return_value=self.conn)
                    ),
                )
                rebind.return_value = {"agentName": "小硕-粤语"}
                response = await handler.handle_language_change(request)
                self.assertEqual(response.status, 200)
                body = json.loads(response.text)
                self.assertEqual(body["targetAgentName"], "小硕-粤语")
                rebind.assert_awaited_once_with(
                    device_id="aa:bb:cc:dd:ee:ff",
                    current_agent_name="小硕-英语",
                    target_agent_name="小硕-粤语",
                    confirm=True,
                )
                self.conn.websocket.close.assert_awaited_once()
                self.assertEqual(self.conn.device_attributes["language"], "zh-CN-yue")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
