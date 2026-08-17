import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.api.vision_handler import VisionHandler


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
            self.assertTrue(body["success"])
            self.assertEqual(body["response"], "这是一张图片")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
