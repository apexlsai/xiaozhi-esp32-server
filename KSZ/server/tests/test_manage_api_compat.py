import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx


class ManageApiCompatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[3] / "main/xiaozhi-server/config/manage_api_client.py"
        spec = importlib.util.spec_from_file_location("ksz_manage_api_compat", source)
        self.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.api)
        self.client_type = self.api.ManageApiClient
        self.client_type({"manager-api": {
            "url": "http://offline.invalid/xiaozhi",
            "secret": "ksz-test-secret", "max_retries": 0,
        }})
        self.clients = []
        self.requests = []
        self.real_client = httpx.AsyncClient

    def transport(self, handler):
        def factory(**kwargs):
            async def handle(request):
                self.requests.append(request)
                return handler(request)

            client = self.real_client(transport=httpx.MockTransport(handle), **kwargs)
            self.clients.append(client)
            return client

        return patch.object(self.api.httpx, "AsyncClient", side_effect=factory)

    async def asyncTearDown(self):
        try:
            self.assertTrue(all(client.is_closed for client in self.clients))
        finally:
            for client in self.clients:
                await client.aclose()
            self.client_type.safe_close()

    async def test_event_and_rebind_preserve_authenticated_wire_contract(self):
        def handle(request):
            self.assertEqual(request.headers["authorization"], "Bearer ksz-test-secret")
            return httpx.Response(200, json={"code": 0, "data": {"success": True}})

        with self.transport(handle):
            event = await self.api.report_device_event(
                "aa:bb:cc:dd:ee:ff", "profile_change",
                {"language": "ja", "role": "child"}, 123,
            )
            rebind = await self.api.rebind_device_agent(
                "aa:bb:cc:dd:ee:ff", "青年-汉语", "儿童-日语-测试",
                confirm=False, language="ja", dev=True, role="child",
            )

        self.assertEqual(event, {"success": True})
        self.assertEqual(rebind, {"success": True})
        self.assertEqual([request.method for request in self.requests], ["POST", "POST"])
        self.assertEqual([request.url.path for request in self.requests], [
            "/xiaozhi/device/event/report", "/xiaozhi/device/rebind",
        ])
        self.assertEqual(json.loads(self.requests[0].content), {
            "deviceId": "aa:bb:cc:dd:ee:ff", "event": "profile_change",
            "payload": {"language": "ja", "role": "child"}, "timestamp": 123,
        })
        self.assertEqual(json.loads(self.requests[1].content), {
            "deviceId": "aa:bb:cc:dd:ee:ff", "currentAgentName": "青年-汉语",
            "targetAgentName": "儿童-日语-测试", "confirm": False,
            "language": "ja", "dev": True, "role": "child",
        })
        self.assertEqual(len(self.clients), 2)

    async def test_business_errors_reach_event_and_rebind_callers(self):
        def handle(request):
            return httpx.Response(200, json={"code": 10041, "msg": "missing"})

        with self.transport(handle):
            with self.assertRaises(self.api.DeviceNotFoundException):
                await self.api.report_device_event("device", "beacon_change")
            with self.assertRaises(self.api.DeviceNotFoundException):
                await self.api.rebind_device_agent("device", "old", "new")

    async def test_shutdown_rejects_both_ksz_entries_without_new_clients(self):
        self.client_type.safe_close()
        with self.transport(lambda request: self.fail("unexpected request")):
            with self.assertRaisesRegex(Exception, "未初始化或已关闭"):
                await self.api.report_device_event("device", "beacon_change")
            with self.assertRaisesRegex(Exception, "未初始化或已关闭"):
                await self.api.rebind_device_agent("device", "old", "new")
        self.assertEqual(self.clients, [])

    async def test_shutdown_keeps_inflight_rebind_result_and_closes_its_client(self):
        def handle(request):
            self.client_type.safe_close()
            return httpx.Response(200, json={"code": 0, "data": {"agentName": "new"}})

        with self.transport(handle):
            result = await self.api.rebind_device_agent("device", "old", "new")
            self.assertEqual(result, {"agentName": "new"})
            with self.assertRaisesRegex(Exception, "未初始化或已关闭"):
                await self.api.rebind_device_agent("device", "new", "next")
        self.assertEqual(len(self.clients), 1)


if __name__ == "__main__":
    unittest.main()
