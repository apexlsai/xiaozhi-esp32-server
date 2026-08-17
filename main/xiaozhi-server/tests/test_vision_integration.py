import asyncio
import io
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.api.vision_handler import MAX_FILE_SIZE, VisionHandler
from core.connection import ConnectionHandler
from core.handle.receiveAudioHandle import startToChat
from core.handle.textHandler.listenMessageHandler import _handle_photo_tool_choice
from core.providers.tools.device_mcp.mcp_handler import (
    handle_mcp_message,
    send_mcp_initialize_message,
)
from core.utils.auth import AuthToken
from core.utils.util import filter_sensitive_info
from plugins_func.register import Action, ActionResponse


class VisionMcpConfigurationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = {
            "server": {
                "auth_key": "unit-test-auth-key",
                "vision_explain": "https://vision.example/mcp/vision/explain",
            }
        }
        self.conn = SimpleNamespace(
            config=self.config,
            headers={"device-id": "aa:bb:cc:dd:ee:ff"},
            features={"mcp": True},
            websocket=SimpleNamespace(send=AsyncMock()),
        )

    async def test_initialize_contains_dynamic_canonical_and_legacy_fields(self):
        before = int(time.time())
        await send_mcp_initialize_message(self.conn)

        message = json.loads(self.conn.websocket.send.await_args.args[0])
        vision = message["payload"]["params"]["capabilities"]["vision"]
        self.assertTrue(vision["enabled"])
        self.assertEqual(
            vision["endpoint"], "https://vision.example/mcp/vision/explain"
        )
        self.assertEqual(vision["url"], vision["endpoint"])
        self.assertEqual(vision["token"], vision["access_token"])
        self.assertGreaterEqual(vision["expires_at"], before + 3599)
        self.assertEqual(vision["max_image_size"], MAX_FILE_SIZE)
        self.assertEqual(
            AuthToken("unit-test-auth-key").verify_token(vision["access_token"]),
            (True, "aa:bb:cc:dd:ee:ff"),
        )

    async def test_refresh_notification_returns_new_configuration(self):
        await handle_mcp_message(
            self.conn,
            SimpleNamespace(call_results={}),
            {
                "jsonrpc": "2.0",
                "method": "notifications/vision/refresh",
                "params": {"reason": "unauthorized"},
            },
        )

        message = json.loads(self.conn.websocket.send.await_args.args[0])
        self.assertEqual(
            message["payload"]["method"],
            "notifications/vision/configuration",
        )
        vision = message["payload"]["params"]["vision"]
        self.assertTrue(vision["access_token"])
        self.assertGreater(vision["expires_at"], int(time.time()))

    async def test_initialize_disables_vision_without_runtime_endpoint(self):
        self.conn.config["server"]["vision_explain"] = "null"
        await send_mcp_initialize_message(self.conn)

        message = json.loads(self.conn.websocket.send.await_args.args[0])
        vision = message["payload"]["params"]["capabilities"]["vision"]
        self.assertEqual(vision, {"enabled": False})
        self.assertNotIn("access_token", vision)

    def test_authorization_header_is_redacted_from_logs(self):
        filtered = filter_sensitive_info(
            {"authorization": "Bearer unit-test-secret", "device-id": "test-device"}
        )
        self.assertEqual(filtered["authorization"], "***")
        self.assertEqual(filtered["device-id"], "test-device")


class ExplicitPhotoToolChoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_choice_uses_provider_safe_device_tool_name(self):
        mcp_client = SimpleNamespace(is_ready=AsyncMock(return_value=True))
        func_handler = SimpleNamespace(has_tool=MagicMock(return_value=True))
        conn = SimpleNamespace(mcp_client=mcp_client, func_handler=func_handler)

        with patch(
            "core.handle.textHandler.listenMessageHandler.startToChat",
            new_callable=AsyncMock,
        ) as start_chat:
            await _handle_photo_tool_choice(conn, "请描述图片")

        func_handler.has_tool.assert_called_once_with("self_camera_take_photo")
        start_chat.assert_awaited_once_with(
            conn, "请描述图片", tool_choice="self_camera_take_photo"
        )

    async def test_start_chat_forwards_forced_tool_to_chat_worker(self):
        executor = MagicMock()
        conn = SimpleNamespace(
            need_bind=False,
            max_output_size=0,
            client_is_speaking=False,
            client_listen_mode="auto",
            client_abort=True,
            executor=executor,
            chat=MagicMock(),
            chat_with_tool=MagicMock(),
            current_speaker=None,
        )
        with (
            patch(
                "core.handle.receiveAudioHandle.handle_user_intent",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "core.handle.receiveAudioHandle.send_stt_message",
                new_callable=AsyncMock,
            ),
        ):
            await startToChat(
                conn, "请描述图片", tool_choice="self_camera_take_photo"
            )

        executor.submit.assert_called_once_with(
            conn.chat_with_tool,
            "请描述图片",
            "self_camera_take_photo",
            {"question": "请描述图片"},
        )

    async def test_chat_worker_executes_device_tool_without_first_llm_turn(self):
        conn = object.__new__(ConnectionHandler)
        conn.dialogue = SimpleNamespace(put=MagicMock())
        conn.tts = SimpleNamespace(tts_text_queue=SimpleNamespace(put=MagicMock()))
        conn.config = {"tool_call_timeout": 30}
        conn.mcp_client = SimpleNamespace(
            name_mapping={"self_camera_take_photo": "self.camera.take_photo"}
        )
        result = ActionResponse(Action.REQLLM, result="视觉分析结果")
        conn.func_handler = SimpleNamespace(
            handle_llm_function_call=AsyncMock(return_value=result)
        )
        conn.loop = asyncio.get_running_loop()
        conn.logger = MagicMock()
        conn.logger.bind.return_value = conn.logger
        conn._handle_function_result = MagicMock()

        with patch("core.connection.enqueue_tool_report"):
            await asyncio.to_thread(
                conn.chat_with_tool,
                "请描述图片",
                "self_camera_take_photo",
                {"question": "请描述图片"},
            )

        conn.func_handler.handle_llm_function_call.assert_awaited_once()
        conn._handle_function_result.assert_called_once_with(
            [(result, ANY)],
            depth=0,
            allow_followup_tools=False,
        )
        tool_results = conn._handle_function_result.call_args.args[0]
        self.assertIs(tool_results[0][0], result)


class VisionHttpContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        config = {
            "server": {
                "auth_key": "unit-test-auth-key",
                "vision_explain": "https://vision.example/mcp/vision/explain",
            },
            "selected_module": {"VLLM": "UnitVLLM"},
            "VLLM": {"UnitVLLM": {"type": "unit"}},
        }
        self.handler = VisionHandler(config)
        self.handler.auth.verify_token = MagicMock(
            return_value=(True, "aa:bb:cc:dd:ee:ff")
        )
        self.vllm = SimpleNamespace(response=MagicMock(return_value="这是一件青花瓷器。"))
        self.instance_patch = patch(
            "core.api.vision_handler.create_instance", return_value=self.vllm
        )
        self.instance_patch.start()

        app = web.Application()
        app.router.add_post("/mcp/vision/explain", self.handler.handle_post)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.instance_patch.stop()

    def _headers(self):
        return {
            "Authorization": "Bearer unit-test-token",
            "Device-Id": "aa:bb:cc:dd:ee:ff",
            "Client-Id": "unit-test-client",
        }

    async def test_accepts_file_field_in_any_part_order_and_returns_answer(self):
        form = FormData()
        form.add_field(
            "file",
            b"\xff\xd8\xffunit-test-jpeg\xff\xd9",
            filename="camera.jpg",
            content_type="image/jpeg",
        )
        form.add_field("language", "zh-CN")
        form.add_field("question", "这是什么展品？")

        response = await self.client.post(
            "/mcp/vision/explain", data=form, headers=self._headers()
        )
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["answer"], "这是一件青花瓷器。")
        self.assertEqual(payload["response"], payload["answer"])
        self.vllm.response.assert_called_once()
        self.assertEqual(self.vllm.response.call_args.args[0], "这是什么展品？")
        self.assertEqual(self.vllm.response.call_args.kwargs["language"], "zh-CN")

    async def test_rejects_oversized_image_with_413(self):
        form = FormData()
        form.add_field("question", "请描述图片")
        form.add_field(
            "file",
            io.BytesIO(b"\xff\xd8\xff" + b"x" * MAX_FILE_SIZE),
            filename="camera.jpg",
            content_type="image/jpeg",
        )

        response = await self.client.post(
            "/mcp/vision/explain", data=form, headers=self._headers()
        )
        self.assertEqual(response.status, 413)

    async def test_rejects_missing_authorization_with_401(self):
        response = await self.client.post("/mcp/vision/explain")
        self.assertEqual(response.status, 401)


if __name__ == "__main__":
    unittest.main()
