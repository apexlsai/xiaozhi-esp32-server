import asyncio
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.api.vision_handler import VISION_UNAVAILABLE_MESSAGE, VisionHandler
from core.providers.tools.device_mcp.mcp_handler import send_mcp_initialize_message
from core.providers.tts.dto.dto import ContentType, SentenceType
from core.providers.tools.device_mcp.mcp_executor import DeviceMCPExecutor
from plugins_func.register import Action
from core.utils.auth import AuthToken


class VisionHandlerTests(unittest.TestCase):
    def setUp(self):
        self.handler = object.__new__(VisionHandler)
        self.handler.config = {
            "read_config_from_api": False,
            "selected_module": {"VLLM": "mock-vllm"},
            "VLLM": {"mock-vllm": {"type": "mock"}},
        }
        self.handler.logger = MagicMock()
        self.handler.auth = MagicMock()

    def request(self, fields):
        reader = SimpleNamespace(next=AsyncMock(side_effect=[*fields, None]))
        return SimpleNamespace(
            headers={
                "Client-Id": "web_test_client",
                "Device-Id": "test-device",
            },
            multipart=AsyncMock(return_value=reader),
        )

    def field(self, name, value):
        return SimpleNamespace(name=name, read=AsyncMock(return_value=value))

    def direct_connection(self):
        conn = SimpleNamespace(
            session_id="test-session",
            sentence_id="original-turn",
            client_abort=False,
            client_is_speaking=False,
            stop_event=threading.Event(),
            config={},
            websocket=SimpleNamespace(send=AsyncMock()),
            mcp_client=SimpleNamespace(call_results={}),
            need_bind=False,
            tts=SimpleNamespace(
                store_tts_text=MagicMock(),
                tts_text_queue=MagicMock(),
                tts_one_sentence=MagicMock(),
            ),
            dialogue=MagicMock(),
        )
        conn.clearSpeakStatus = MagicMock(
            side_effect=lambda: setattr(conn, "client_is_speaking", False)
        )
        self.handler.websocket_server = SimpleNamespace(
            find_device_connection=MagicMock(return_value=conn)
        )
        return conn

    def assert_no_direct_tts(self, conn):
        conn.tts.store_tts_text.assert_not_called()
        conn.tts.tts_text_queue.put.assert_not_called()
        conn.tts.tts_one_sentence.assert_not_called()
        conn.dialogue.put.assert_not_called()

    def test_missing_question_returns_json_response(self):
        request = self.request([self.field("image", b"\xff\xd8\xffimage")])

        response = asyncio.run(self.handler.handle_post(request))

        self.assertIsNotNone(response)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "application/json")
        self.assertEqual(response.charset, "utf-8")
        self.assertEqual(json.loads(response.text)["message"], "缺少问题字段")

    def test_chinese_question_returns_vllm_result(self):
        request = self.request(
            [
                self.field("question", "请用中文描述这张图片".encode("utf-8")),
                self.field("image", b"\xff\xd8\xffimage"),
            ]
        )
        vllm = MagicMock()
        vllm.response.return_value = "这是一张图片"

        with patch(
            "core.api.vision_handler.create_instance", return_value=vllm
        ):
            response = asyncio.run(self.handler.handle_post(request))

        body = json.loads(response.text)
        self.assertTrue(body["success"])
        self.assertEqual(body["response"], "这是一张图片")
        self.assertEqual(response.content_type, "application/json")
        self.assertEqual(response.charset, "utf-8")
        vllm.response.assert_called_once()
        _, _, kwargs = vllm.response.mock_calls[0]
        self.assertEqual(kwargs["image_mime"], "image/jpeg")
        self.assertEqual(kwargs["device_id"], "test-device")

    def test_vllm_failure_returns_safe_direct_response(self):
        request = self.request(
            [
                self.field("question", "描述图片".encode("utf-8")),
                self.field("image", b"\xff\xd8\xffimage"),
            ]
        )
        vllm = MagicMock()
        vllm.response.side_effect = ValueError(
            'VLLM 请求失败: {"code":"vlm_invalid_response","detail":"internal"}'
        )

        with patch(
            "core.api.vision_handler.create_instance", return_value=vllm
        ):
            response = asyncio.run(self.handler.handle_post(request))

        body = json.loads(response.text)
        self.assertEqual(response.status, 200)
        self.assertEqual(
            body,
            {
                "success": False,
                "action": "RESPONSE",
                "response": VISION_UNAVAILABLE_MESSAGE,
                "message": VISION_UNAVAILABLE_MESSAGE,
            },
        )
        self.assertNotIn("vlm_invalid_response", response.text)
        self.assertNotIn("internal", response.text)

    def test_empty_vllm_result_returns_safe_direct_response(self):
        request = self.request(
            [
                self.field("question", "描述图片".encode("utf-8")),
                self.field("image", b"\xff\xd8\xffimage"),
            ]
        )
        vllm = MagicMock()
        vllm.response.return_value = ""

        with patch(
            "core.api.vision_handler.create_instance", return_value=vllm
        ):
            response = asyncio.run(self.handler.handle_post(request))

        body = json.loads(response.text)
        self.assertFalse(body["success"])
        self.assertEqual(body["action"], "RESPONSE")
        self.assertEqual(body["response"], VISION_UNAVAILABLE_MESSAGE)

    def test_direct_vision_result_pushes_tts_to_online_device(self):
        conn = self.direct_connection()
        tts = conn.tts

        pushed = asyncio.run(
            self.handler._push_direct_tts("test-device", "  这是一件展品。  ")
        )

        self.assertTrue(pushed)
        conn.websocket.send.assert_awaited_once()
        self.assertEqual(
            json.loads(conn.websocket.send.call_args.args[0]),
            {"type": "tts", "state": "start", "session_id": "test-session"},
        )
        self.assertTrue(conn.client_is_speaking)
        tts.store_tts_text.assert_called_once()
        sentence_id, text = tts.store_tts_text.call_args.args
        self.assertEqual(text, "这是一件展品。")
        queue_messages = [call.args[0] for call in tts.tts_text_queue.put.call_args_list]
        self.assertEqual(
            [message.sentence_type for message in queue_messages],
            [SentenceType.FIRST, SentenceType.LAST],
        )
        tts.tts_one_sentence.assert_called_once_with(
            conn,
            ContentType.TEXT,
            content_detail="这是一件展品。",
            sentence_id=sentence_id,
        )
        conn.dialogue.put.assert_called_once()

    def test_mcp_vision_result_does_not_duplicate_tts(self):
        conn = self.direct_connection()
        conn.mcp_client.call_results[3] = object()

        pushed = asyncio.run(
            self.handler._push_direct_tts("test-device", "这是一件展品。")
        )

        self.assertFalse(pushed)
        conn.websocket.send.assert_not_awaited()
        self.assert_no_direct_tts(conn)

    def test_direct_tts_awaits_real_start_before_queueing_audio(self):
        async def run():
            conn = self.direct_connection()
            entered = asyncio.Event()
            release = asyncio.Event()
            events = []

            async def send(message):
                self.assertEqual(
                    json.loads(message),
                    {"type": "tts", "state": "start", "session_id": "test-session"},
                )
                events.append("start-entered")
                entered.set()
                await release.wait()
                events.append("start-completed")

            def record(event):
                self.assertIn("start-completed", events)
                self.assertTrue(conn.client_is_speaking)
                events.append(event)

            conn.websocket.send.side_effect = send
            conn.tts.store_tts_text.side_effect = lambda *_: record("store")
            conn.tts.tts_text_queue.put.side_effect = lambda message: record(
                message.sentence_type.name
            )
            conn.tts.tts_one_sentence.side_effect = lambda *_, **__: record("synthesize")
            conn.dialogue.put.side_effect = lambda _: record("history")
            task = asyncio.create_task(
                self.handler._push_direct_tts("test-device", "这是一件展品。")
            )
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.assert_no_direct_tts(conn)
                release.set()
                self.assertTrue(await asyncio.wait_for(task, 1))
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(
                events,
                ["start-entered", "start-completed", "store", "FIRST", "synthesize", "LAST", "history"],
            )

        asyncio.run(run())

    def test_stale_manual_vision_does_not_start_tts(self):
        for reason in ("offline", "replaced-connection", "abort", "new-turn", "pending-mcp", "stopped"):
            with self.subTest(reason=reason):
                conn = self.direct_connection()
                if reason == "offline":
                    self.handler.websocket_server.find_device_connection.return_value = None
                elif reason == "replaced-connection":
                    self.handler.websocket_server.find_device_connection.return_value = object()
                elif reason == "abort":
                    conn.client_abort = True
                elif reason == "new-turn":
                    conn.sentence_id = "new-turn"
                elif reason == "stopped":
                    conn.stop_event.set()
                else:
                    conn.mcp_client.call_results[0] = object()
                pushed = asyncio.run(
                    self.handler._push_direct_tts(
                        "test-device", "旧照片正文。", expected_conn=conn,
                        expected_sentence_id="original-turn",
                    )
                )
                self.assertFalse(pushed)
                conn.websocket.send.assert_not_awaited()
                self.assert_no_direct_tts(conn)

    def test_direct_tts_drops_old_body_if_state_changes_while_sending_start(self):
        for reason in ("offline", "replaced-connection", "replaced-socket", "abort", "new-turn", "pending-mcp", "stopped"):
            with self.subTest(reason=reason):
                async def run():
                    conn = self.direct_connection()
                    socket = conn.websocket

                    async def send(message):
                        if json.loads(message)["state"] != "start":
                            return
                        await asyncio.sleep(0)
                        if reason == "offline":
                            self.handler.websocket_server.find_device_connection.return_value = None
                        elif reason == "replaced-connection":
                            self.handler.websocket_server.find_device_connection.return_value = object()
                        elif reason == "replaced-socket":
                            conn.websocket = SimpleNamespace(send=AsyncMock())
                        elif reason == "abort":
                            conn.client_abort = True
                            conn.clearSpeakStatus()
                        elif reason == "new-turn":
                            conn.sentence_id = "new-turn"
                            conn.client_is_speaking = True
                        elif reason == "stopped":
                            conn.stop_event.set()
                        else:
                            conn.mcp_client.call_results[0] = object()

                    conn.websocket.send.side_effect = send
                    pushed = await self.handler._push_direct_tts(
                        "test-device", "旧照片正文。", expected_conn=conn,
                        expected_sentence_id="original-turn",
                    )
                    self.assertFalse(pushed)
                    if reason == "pending-mcp":
                        self.assertEqual(
                            [json.loads(call.args[0])["state"] for call in socket.send.await_args_list],
                            ["start", "stop"],
                        )
                        self.assertFalse(conn.client_is_speaking)
                    else:
                        socket.send.assert_awaited_once()
                    if reason == "replaced-socket":
                        conn.websocket.send.assert_not_awaited()
                    elif reason == "new-turn":
                        self.assertTrue(conn.client_is_speaking)
                        conn.clearSpeakStatus.assert_not_called()
                    elif reason == "abort":
                        self.assertFalse(conn.client_is_speaking)
                        conn.clearSpeakStatus.assert_called_once()
                    self.assert_no_direct_tts(conn)

                asyncio.run(run())

    def test_direct_tts_start_failure_does_not_queue_audio(self):
        conn = self.direct_connection()
        conn.websocket.send.side_effect = ConnectionError("socket closed")

        pushed = asyncio.run(
            self.handler._push_direct_tts("test-device", "这是一件展品。")
        )

        self.assertFalse(pushed)
        conn.websocket.send.assert_awaited_once()
        self.assertFalse(conn.client_is_speaking)
        self.assert_no_direct_tts(conn)

    def test_direct_tts_store_failure_closes_started_cycle(self):
        conn = self.direct_connection()
        conn.tts.store_tts_text.side_effect = RuntimeError("store failed")

        pushed = asyncio.run(
            self.handler._push_direct_tts("test-device", "这是一件展品。")
        )

        self.assertFalse(pushed)
        self.assertEqual(
            [json.loads(call.args[0])["state"] for call in conn.websocket.send.await_args_list],
            ["start", "stop"],
        )
        conn.clearSpeakStatus.assert_called_once()
        self.assertFalse(conn.client_is_speaking)
        conn.tts.store_tts_text.assert_called_once()
        conn.tts.tts_text_queue.put.assert_not_called()
        conn.tts.tts_one_sentence.assert_not_called()
        conn.dialogue.put.assert_not_called()

    def test_direct_tts_synthesis_submission_failure_queues_one_last(self):
        conn = self.direct_connection()
        conn.tts.tts_one_sentence.side_effect = RuntimeError("submission failed")

        pushed = asyncio.run(
            self.handler._push_direct_tts("test-device", "这是一件展品。")
        )

        self.assertFalse(pushed)
        self.assertEqual(
            [call.args[0].sentence_type for call in conn.tts.tts_text_queue.put.call_args_list],
            [SentenceType.FIRST, SentenceType.LAST],
        )
        conn.websocket.send.assert_awaited_once()
        self.assertEqual(json.loads(conn.websocket.send.call_args.args[0])["state"], "start")
        self.assertTrue(conn.client_is_speaking)
        conn.clearSpeakStatus.assert_not_called()
        conn.tts.tts_one_sentence.assert_called_once()
        conn.dialogue.put.assert_not_called()

    def test_direct_tts_history_failure_does_not_duplicate_end(self):
        conn = self.direct_connection()
        conn.dialogue.put.side_effect = RuntimeError("history failed")

        pushed = asyncio.run(
            self.handler._push_direct_tts("test-device", "这是一件展品。")
        )

        self.assertFalse(pushed)
        self.assertEqual(
            [call.args[0].sentence_type for call in conn.tts.tts_text_queue.put.call_args_list],
            [SentenceType.FIRST, SentenceType.LAST],
        )
        conn.websocket.send.assert_awaited_once()
        self.assertEqual(json.loads(conn.websocket.send.call_args.args[0])["state"], "start")
        conn.clearSpeakStatus.assert_not_called()
        conn.tts.tts_one_sentence.assert_called_once()
        conn.dialogue.put.assert_called_once()

    def test_direct_tts_start_cancellation_propagates_and_clears_speaking(self):
        conn = self.direct_connection()
        conn.websocket.send.side_effect = asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(
                self.handler._push_direct_tts("test-device", "这是一件展品。")
            )

        conn.websocket.send.assert_awaited_once()
        self.assertFalse(conn.client_is_speaking)
        self.assert_no_direct_tts(conn)

    def test_legacy_http_response_waits_for_tts_start(self):
        async def run():
            conn = self.direct_connection()
            entered = asyncio.Event()
            release = asyncio.Event()
            request = self.request([
                self.field("question", "识别图片".encode("utf-8")),
                self.field("file", b"\xff\xd8\xffimage"),
            ])
            vllm = MagicMock()
            vllm.response.return_value = "这是一件展品。"

            async def send(message):
                self.assertEqual(json.loads(message)["state"], "start")
                entered.set()
                await release.wait()

            conn.websocket.send.side_effect = send
            with patch("core.api.vision_handler.create_instance", return_value=vllm):
                task = asyncio.create_task(self.handler.handle_post(request))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    self.assertFalse(task.done())
                    self.assert_no_direct_tts(conn)
                    release.set()
                    response = await asyncio.wait_for(task, 1)
                finally:
                    release.set()
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.text), {
                "success": True, "action": "RESPONSE", "response": "这是一件展品。",
            })
            conn.websocket.send.assert_awaited_once()
            conn.tts.tts_one_sentence.assert_called_once()

        asyncio.run(run())

    def test_png_and_webp_preserve_image_mime(self):
        images = {
            "image/png": b"\x89PNG\r\n\x1a\nimage",
            "image/webp": b"RIFF\x04\x00\x00\x00WEBPimage",
        }
        for expected_mime, image in images.items():
            with self.subTest(expected_mime=expected_mime):
                request = self.request(
                    [
                        self.field("question", "描述图片".encode("utf-8")),
                        self.field("image", image),
                    ]
                )
                vllm = MagicMock()
                vllm.response.return_value = "识别成功"
                with patch(
                    "core.api.vision_handler.create_instance", return_value=vllm
                ):
                    response = asyncio.run(self.handler.handle_post(request))

                self.assertTrue(json.loads(response.text)["success"])
                _, _, kwargs = vllm.response.mock_calls[0]
                self.assertEqual(kwargs["image_mime"], expected_mime)

    def test_device_context_uses_agent_language_suffix(self):
        self.handler.config["read_config_from_api"] = True
        private_config = {
            "selected_module": {"VLLM": "museum-vision"},
            "VLLM": {"museum-vision": {"type": "openai"}},
            "device_attributes": {
                "agent_name": "小硕-英语",
                "language": "zh-CN",
                "last_beacon_id": "beacon-001",
            },
        }
        request = self.request(
            [
                self.field("question", "What is this?".encode("utf-8")),
                self.field("image", b"\xff\xd8\xffimage"),
            ]
        )
        vllm = MagicMock()
        vllm.response.return_value = "This is an exhibit."

        with patch(
            "core.api.vision_handler.get_private_config_from_api",
            new=AsyncMock(return_value=private_config),
        ), patch("core.api.vision_handler.create_instance", return_value=vllm):
            response = asyncio.run(self.handler.handle_post(request))

        self.assertTrue(json.loads(response.text)["success"])
        _, _, kwargs = vllm.response.mock_calls[0]
        self.assertEqual(kwargs["device_id"], "test-device")
        self.assertEqual(kwargs["language"], "en")
        self.assertEqual(kwargs["last_beacon_id"], "beacon-001")

    def test_vllm_request_does_not_block_event_loop(self):
        async def run():
            request = self.request(
                [
                    self.field("question", "描述图片".encode("utf-8")),
                    self.field("image", b"\xff\xd8\xffimage"),
                ]
            )
            vllm = MagicMock()
            vllm.response.side_effect = lambda *args, **kwargs: (
                time.sleep(0.05) or "识别成功"
            )

            with patch(
                "core.api.vision_handler.create_instance", return_value=vllm
            ):
                task = asyncio.create_task(self.handler.handle_post(request))
                await asyncio.sleep(0.005)
                self.assertFalse(task.done())
                response = await task

            self.assertTrue(json.loads(response.text)["success"])

        asyncio.run(run())

    def test_firmware_multipart_file_upload_returns_chinese_result(self):
        async def run():
            app = web.Application()
            app.router.add_post("/mcp/vision/explain", self.handler.handle_post)
            client = TestClient(TestServer(app))
            await client.start_server()
            form = FormData()
            form.add_field("question", "请用中文描述这张图片")
            form.add_field(
                "file",
                b"\xff\xd8\xffimage",
                filename="camera.jpg",
                content_type="image/jpeg",
            )
            vllm = MagicMock()
            vllm.response.return_value = "这是一张图片"
            try:
                with patch(
                    "core.api.vision_handler.create_instance", return_value=vllm
                ):
                    response = await client.post(
                        "/mcp/vision/explain",
                        data=form,
                        headers={
                            "Client-Id": "web_test_client",
                            "Device-Id": "test-device",
                        },
                    )
                    body = await response.json()
            finally:
                await client.close()

            self.assertEqual(response.status, 200)
            self.assertEqual(
                body,
                {
                    "success": True,
                    "action": "RESPONSE",
                    "response": "这是一张图片",
                },
            )

        asyncio.run(run())

    def test_legacy_image_field_keeps_action_response_contract(self):
        async def run():
            app = web.Application()
            app.router.add_post("/mcp/vision/explain", self.handler.handle_post)
            client = TestClient(TestServer(app))
            await client.start_server()
            form = FormData()
            form.add_field("question", "请描述图片")
            form.add_field(
                "image",
                b"\xff\xd8\xffimage",
                filename="image.jpg",
                content_type="image/jpeg",
            )
            vllm = MagicMock()
            vllm.response.return_value = "原版视觉结果"
            try:
                with patch(
                    "core.api.vision_handler.create_instance", return_value=vllm
                ):
                    response = await client.post(
                        "/mcp/vision/explain",
                        data=form,
                        headers={
                            "Client-Id": "web_test_client",
                            "Device-Id": "test-device",
                        },
                    )
                    body = await response.json()
            finally:
                await client.close()

            self.assertEqual(response.status, 200)
            self.assertEqual(
                body,
                {
                    "success": True,
                    "action": "RESPONSE",
                    "response": "原版视觉结果",
                },
            )

        asyncio.run(run())

    def test_original_mcp_initialize_keeps_url_token_contract(self):
        async def run():
            websocket = SimpleNamespace(send=AsyncMock())
            conn = SimpleNamespace(
                config={
                    "server": {
                        "auth_key": "unit-test-auth-key",
                        "vision_explain": "https://vision.example/mcp/vision/explain",
                    }
                },
                headers={"device-id": "aa:bb:cc:dd:ee:ff"},
                features={"mcp": True},
                websocket=websocket,
            )

            await send_mcp_initialize_message(conn)

            message = json.loads(websocket.send.await_args.args[0])
            vision = message["payload"]["params"]["capabilities"]["vision"]
            self.assertEqual(set(vision), {"url", "token"})
            self.assertEqual(
                vision["url"], "https://vision.example/mcp/vision/explain"
            )
            self.assertEqual(
                AuthToken("unit-test-auth-key").verify_token(vision["token"]),
                (True, "aa:bb:cc:dd:ee:ff"),
            )

        asyncio.run(run())


class ToolErrorContractTests(unittest.TestCase):
    def test_device_mcp_error_is_structured_for_agent(self):
        conn = SimpleNamespace(mcp_client=None)
        result = asyncio.run(
            DeviceMCPExecutor(conn).execute(conn, "self_get_device_status", {})
        )

        self.assertEqual(result.action, Action.REQLLM)
        payload = json.loads(result.result)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["tool"], "self_get_device_status")
        self.assertIn("message", payload)

    def execute_camera(self, payload):
        conn = SimpleNamespace(
            mcp_client=SimpleNamespace(is_ready=AsyncMock(return_value=True))
        )
        with patch(
            "core.providers.tools.device_mcp.mcp_executor.call_mcp_tool",
            new=AsyncMock(return_value=json.dumps(payload, ensure_ascii=False)),
        ):
            return asyncio.run(
                DeviceMCPExecutor(conn).execute(
                    conn,
                    "self_camera_take_photo",
                    {"question": "描述图片"},
                )
            )

    def test_camera_success_bypasses_second_llm(self):
        result = self.execute_camera(
            {
                "success": True,
                "action": "RESPONSE",
                "response": "这是一件展品。",
            }
        )

        self.assertEqual(result.action, Action.RESPONSE)
        self.assertEqual(result.response, "这是一件展品。")
        self.assertIsNone(result.result)

    def test_legacy_nested_camera_success_bypasses_second_llm(self):
        result = self.execute_camera(
            {
                "success": True,
                "vision_analysis": {
                    "success": True,
                    "action": "RESPONSE",
                    "response": "这是旧客户端返回的展品。",
                },
            }
        )

        self.assertEqual(result.action, Action.RESPONSE)
        self.assertEqual(result.response, "这是旧客户端返回的展品。")

    def test_legacy_nested_camera_failure_does_not_leak_raw_error(self):
        result = self.execute_camera(
            {
                "success": True,
                "vision_analysis": {
                    "success": False,
                    "message": 'VLLM 请求失败: {"secret":"internal"}',
                },
            }
        )

        self.assertEqual(result.action, Action.RESPONSE)
        self.assertEqual(result.response, VISION_UNAVAILABLE_MESSAGE)
        self.assertNotIn("internal", result.response)

    def test_camera_failure_does_not_trust_public_response_text(self):
        result = self.execute_camera(
            {
                "success": False,
                "action": "RESPONSE",
                "response": 'VLLM 请求失败: {"secret":"internal"}',
            }
        )

        self.assertEqual(result.action, Action.RESPONSE)
        self.assertEqual(result.response, VISION_UNAVAILABLE_MESSAGE)
        self.assertNotIn("internal", result.response)

    def test_malformed_camera_result_uses_safe_direct_response(self):
        result = self.execute_camera({"success": True, "message": "描述图片"})

        self.assertEqual(result.action, Action.RESPONSE)
        self.assertEqual(result.response, VISION_UNAVAILABLE_MESSAGE)


if __name__ == "__main__":
    unittest.main()
