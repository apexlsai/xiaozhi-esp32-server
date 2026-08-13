import asyncio
import json
import sys
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

    def test_multipart_http_upload_returns_chinese_result(self):
        async def run():
            app = web.Application()
            app.router.add_post("/mcp/vision/explain", self.handler.handle_post)
            client = TestClient(TestServer(app))
            await client.start_server()
            form = FormData()
            form.add_field("question", "请用中文描述这张图片")
            form.add_field(
                "image",
                b"\xff\xd8\xffimage",
                filename="image.jpg",
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
