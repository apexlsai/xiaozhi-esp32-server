import importlib
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from loguru import logger


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "main/xiaozhi-server"))
sys.path.insert(0, str(ROOT / "KSZ/server"))

from core.utils.language import LANGUAGE_AGENT_SUFFIXES
from ksz_guide import integration, role_patch


DEVICE = "AA:BB:CC:DD:EE:FF"


def connection(agent_name, language=None):
    attributes = {"agent_name": agent_name}
    if language is not None:
        attributes["language"] = language
    return SimpleNamespace(device_id=DEVICE, device_attributes=attributes)


def handler_module(*, rebind=None, send=None):
    module = ModuleType("core.handle.deviceEventHandle")
    module._do_rebind = rebind or AsyncMock()
    module._send_json = send or AsyncMock()
    return module


class RoleResolverTests(unittest.TestCase):
    def test_role_and_language_matrix(self):
        for role, prefix in {
            "child": "儿童",
            "youth": "青年",
            "expert": "专家",
        }.items():
            for language, suffix in LANGUAGE_AGENT_SUFFIXES.items():
                with self.subTest(role=role, language=language):
                    self.assertEqual(
                        role_patch.resolve_role_agent_name("儿童-汉语", language, role),
                        f"{prefix}-{suffix}",
                    )
                    self.assertEqual(
                        role_patch.resolve_role_agent_name("儿童-汉语-测试", language, role),
                        f"{prefix}-{suffix}-测试",
                    )


class RoleRebindTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        role_patch._pending_reconnects.clear()

    async def test_uses_server_side_current_agent(self):
        rebind = AsyncMock(return_value={"agentName": "青年-英语", "reconnectRequired": True})
        module = handler_module(rebind=rebind)
        conn = connection("儿童-英语", "en")

        with patch.dict(sys.modules, {"core.handle.deviceEventHandle": module}):
            handled = await role_patch.handle_role_rebind(conn, {
                "event": "agent_rebind",
                "request_id": "role-1",
                "payload": {"role": "youth"},
            })

        self.assertTrue(handled)
        module._do_rebind.assert_awaited_once_with(
            conn, "agent_rebind", "儿童-英语", "青年-英语", "role-1", confirm=True,
        )
        self.assertEqual(role_patch._pending_reconnects[DEVICE]["target"], "青年-英语")

    async def test_falls_back_to_agent_language_suffix(self):
        rebind = AsyncMock(return_value={"agentName": "专家-粤语-测试"})
        module = handler_module(rebind=rebind)
        conn = connection("儿童-粤语-测试")

        with patch.dict(sys.modules, {"core.handle.deviceEventHandle": module}):
            await role_patch.handle_role_rebind(conn, {
                "event": "agent_rebind",
                "payload": {"role": "expert"},
            })

        self.assertEqual(module._do_rebind.await_args.args[3], "专家-粤语-测试")

    async def test_same_role_is_idempotent_without_reconnect(self):
        module = handler_module()
        conn = connection("青年-汉语", "zh-CN")

        with patch.dict(sys.modules, {"core.handle.deviceEventHandle": module}):
            await role_patch.handle_role_rebind(conn, {
                "event": "agent_rebind",
                "request_id": "same-role",
                "payload": {"role": "youth"},
            })

        module._do_rebind.assert_not_awaited()
        result = module._send_json.await_args.args[1]
        self.assertTrue(result["success"])
        self.assertEqual(result["request_id"], "same-role")
        self.assertFalse(result["data"]["reconnectRequired"])

    async def test_invalid_role_is_rejected(self):
        module = handler_module()
        conn = connection("青年-汉语", "zh-CN")

        with patch.dict(sys.modules, {"core.handle.deviceEventHandle": module}):
            handled = await role_patch.handle_role_rebind(conn, {
                "event": "agent_rebind",
                "payload": {"role": "elder"},
            })

        self.assertTrue(handled)
        module._do_rebind.assert_not_awaited()
        self.assertIn("role 必须是", module._send_json.await_args.args[1]["error"])

    async def test_missing_current_agent_is_rejected(self):
        module = handler_module()
        conn = SimpleNamespace(device_id=DEVICE, device_attributes={})

        with patch.dict(sys.modules, {"core.handle.deviceEventHandle": module}):
            await role_patch.handle_role_rebind(conn, {
                "event": "agent_rebind",
                "payload": {"role": "child"},
            })

        module._do_rebind.assert_not_awaited()
        self.assertIn("当前绑定智能体", module._send_json.await_args.args[1]["error"])

    async def test_unknown_language_is_rejected(self):
        module = handler_module()
        conn = connection("无语言后缀", "unknown")

        with patch.dict(sys.modules, {"core.handle.deviceEventHandle": module}):
            await role_patch.handle_role_rebind(conn, {
                "event": "agent_rebind",
                "payload": {"role": "expert"},
            })

        module._do_rebind.assert_not_awaited()
        self.assertIn("角色对应语言", module._send_json.await_args.args[1]["error"])

    async def test_mixed_new_and_legacy_fields_are_rejected(self):
        module = handler_module()
        conn = connection("青年-汉语", "zh-CN")

        with patch.dict(sys.modules, {"core.handle.deviceEventHandle": module}):
            handled = await role_patch.handle_role_rebind(conn, {
                "event": "agent_rebind",
                "payload": {"role": "child", "target_agent_name": "儿童-汉语"},
            })

        self.assertTrue(handled)
        module._do_rebind.assert_not_awaited()
        self.assertIn("不能与旧换绑字段", module._send_json.await_args.args[1]["error"])

    async def test_legacy_agent_rebind_falls_through(self):
        module = handler_module()
        conn = connection("青年-汉语", "zh-CN")

        with patch.dict(sys.modules, {"core.handle.deviceEventHandle": module}):
            handled = await role_patch.handle_role_rebind(conn, {
                "event": "agent_rebind",
                "payload": {
                    "current_agent_name": "青年-汉语",
                    "target_agent_name": "专家-汉语",
                    "confirm": True,
                },
            })

        self.assertFalse(handled)

    async def test_three_role_changes_can_run_sequentially(self):
        rebind = AsyncMock(side_effect=[
            {"agentName": "青年-汉语"},
            {"agentName": "专家-汉语"},
            {"agentName": "儿童-汉语"},
        ])
        module = handler_module(rebind=rebind)

        with patch.dict(sys.modules, {"core.handle.deviceEventHandle": module}):
            for current, role in [
                ("儿童-汉语", "youth"),
                ("青年-汉语", "expert"),
                ("专家-汉语", "child"),
            ]:
                await role_patch.handle_role_rebind(
                    connection(current, "zh-CN"),
                    {"event": "agent_rebind", "payload": {"role": role}},
                )

        self.assertEqual(
            [call.args[3] for call in module._do_rebind.await_args_list],
            ["青年-汉语", "专家-汉语", "儿童-汉语"],
        )

    async def test_integration_routes_role_before_guide_runtime(self):
        route = AsyncMock(return_value=True)
        conn = connection("儿童-汉语", "zh-CN")
        message = {"event": "agent_rebind", "payload": {"role": "expert"}}

        with patch.object(integration, "handle_role_rebind", route):
            self.assertTrue(await integration.handle_legacy_event(conn, message))
        route.assert_awaited_once_with(conn, message)

    async def test_profile_change_rebinds_language_role_and_environment_once(self):
        rebind = AsyncMock(return_value={"agentName": "专家-日语-测试", "reconnectRequired": True})
        module = handler_module(rebind=rebind)
        conn = connection("儿童-汉语", "zh-CN")

        with patch.dict(sys.modules, {"core.handle.deviceEventHandle": module}):
            handled = await role_patch.handle_profile_change(conn, {
                "event": "profile_change",
                "request_id": "profile-1",
                "payload": {"language": "ja", "role": "expert", "dev": True},
            })

        self.assertTrue(handled)
        module._do_rebind.assert_awaited_once_with(
            conn,
            "profile_change",
            "儿童-汉语",
            "专家-日语-测试",
            "profile-1",
            confirm=True,
            language="ja",
            dev=True,
            role="expert",
        )
        self.assertEqual(role_patch._pending_reconnects[DEVICE]["target"], "专家-日语-测试")

    async def test_profile_change_rejects_non_product_language(self):
        module = handler_module()
        conn = connection("儿童-汉语", "zh-CN")

        with patch.dict(sys.modules, {"core.handle.deviceEventHandle": module}):
            handled = await role_patch.handle_profile_change(conn, {
                "event": "profile_change",
                "payload": {"language": "zh-CN-yue", "role": "child"},
            })

        self.assertTrue(handled)
        module._do_rebind.assert_not_awaited()
        self.assertIn("language 必须是", module._send_json.await_args.args[1]["error"])

    async def test_profile_change_defaults_dev_to_production(self):
        rebind = AsyncMock(return_value={"agentName": "青年-韩语", "reconnectRequired": False})
        module = handler_module(rebind=rebind)
        conn = connection("青年-韩语-测试", "ko")

        with patch.dict(sys.modules, {"core.handle.deviceEventHandle": module}):
            await role_patch.handle_profile_change(conn, {
                "event": "profile_change",
                "payload": {"language": "ko", "role": "youth"},
            })

        module._do_rebind.assert_awaited_once_with(
            conn,
            "profile_change",
            "青年-韩语-测试",
            "青年-韩语",
            None,
            confirm=True,
            language="ko",
            dev=False,
            role="youth",
        )
        self.assertNotIn(DEVICE, role_patch._pending_reconnects)

    async def test_integration_routes_profile_before_guide_runtime(self):
        route = AsyncMock(return_value=True)
        conn = connection("儿童-汉语", "zh-CN")
        message = {
            "event": "profile_change",
            "payload": {"language": "en", "role": "youth", "dev": False},
        }

        with patch.object(integration, "handle_profile_change", route):
            self.assertTrue(await integration.handle_legacy_event(conn, message))
        route.assert_awaited_once_with(conn, message)


class DeviceEventDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with patch("config.logger.setup_logging", return_value=logger):
            self.handler = importlib.import_module("core.handle.deviceEventHandle")
        self.conn = connection("儿童-汉语-测试", "zh-CN")
        self.conn.read_config_from_api = True
        self.conn.websocket = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
        role_patch._pending_reconnects.clear()

    async def test_firmware_role_payload_reaches_new_handler(self):
        with patch.object(self.handler, "guide", integration), patch.object(
            self.handler, "rebind_device_agent", new_callable=AsyncMock,
            return_value={"agentName": "青年-汉语-测试", "reconnectRequired": True},
        ) as rebind, patch.object(self.handler.asyncio, "sleep", new_callable=AsyncMock):
            await self.handler.handle_device_event(self.conn, {
                "type": "device_event",
                "event": "agent_rebind",
                "request_id": "role-192459",
                "payload": {"role": "youth"},
            })

        rebind.assert_awaited_once_with(
            device_id=DEVICE,
            current_agent_name="儿童-汉语-测试",
            target_agent_name="青年-汉语-测试",
            confirm=True,
        )
        self.conn.websocket.send.assert_awaited_once()
        result = json.loads(self.conn.websocket.send.await_args.args[0])
        self.assertTrue(result["success"])
        self.assertEqual(result["request_id"], "role-192459")
        self.conn.websocket.close.assert_awaited_once()

    async def test_combined_profile_rebinds_and_closes_once(self):
        with patch.object(self.handler, "guide", integration), patch.object(
            self.handler, "rebind_device_agent", new_callable=AsyncMock,
            return_value={"agentName": "专家-日语", "reconnectRequired": True},
        ) as rebind, patch.object(self.handler.asyncio, "sleep", new_callable=AsyncMock):
            await self.handler.handle_device_event(self.conn, {
                "type": "device_event", "event": "profile_change",
                "request_id": "boot-profile",
                "payload": {"language": "ja", "role": "expert"},
            })

        rebind.assert_awaited_once_with(
            device_id=DEVICE,
            current_agent_name="儿童-汉语-测试",
            target_agent_name="专家-日语",
            confirm=True, language="ja", role="expert", dev=False,
        )
        self.conn.websocket.send.assert_awaited_once()
        self.conn.websocket.close.assert_awaited_once()

    async def test_invalid_profiles_keep_binding_and_connection(self):
        payloads = [
            {"language": "en"},
            {"language": "en", "role": []},
            {"language": [], "role": "child"},
            {"language": "en", "role": "child", "dev": "false"},
        ]
        with patch.object(self.handler, "guide", integration), patch.object(
            self.handler, "rebind_device_agent", new_callable=AsyncMock,
        ) as rebind:
            for payload in payloads:
                with self.subTest(payload=payload):
                    await self.handler.handle_device_event(self.conn, {
                        "type": "device_event", "event": "profile_change", "payload": payload,
                    })
                    result = json.loads(self.conn.websocket.send.await_args.args[0])
                    self.assertFalse(result["success"])
        rebind.assert_not_awaited()
        self.conn.websocket.close.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
