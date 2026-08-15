import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.providers.vllm.openai import VLLMProvider, _resolve_chat_completions_url


class OpenAIVLLMProviderTests(unittest.TestCase):
    def test_resolve_chat_completions_url(self):
        self.assertEqual(
            _resolve_chat_completions_url("http://agent:15000/v1"),
            "http://agent:15000/v1/chat/completions",
        )
        self.assertEqual(
            _resolve_chat_completions_url(
                "http://agent:15000/v1/chat/completions"
            ),
            "http://agent:15000/v1/chat/completions",
        )

    def test_response_sends_museum_context_and_real_mime(self):
        provider = VLLMProvider(
            {
                "base_url": "http://agent:15000/v1",
                "model_name": "museum-guide-vision",
                "api_key": "test-key",
            }
        )
        response = MagicMock()
        response.json.return_value = {
            "choices": [{"message": {"content": "这是一件藏品"}}]
        }
        client = MagicMock()
        client.__enter__.return_value.post.return_value = response

        with patch("core.providers.vllm.openai.httpx.Client", return_value=client):
            result = provider.response(
                "请识别图片",
                "encoded-image",
                image_mime="image/png",
                device_id="device-001",
                language="zh-CN",
                last_beacon_id="beacon-001",
            )

        self.assertEqual(result, "这是一件藏品")
        _, kwargs = client.__enter__.return_value.post.call_args
        payload = kwargs["json"]
        self.assertEqual(payload["model"], "museum-guide-vision")
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["device_id"], "device-001")
        self.assertEqual(payload["language"], "zh-CN")
        self.assertEqual(payload["last_beacon_id"], "beacon-001")
        self.assertEqual(
            payload["messages"][0]["content"][1]["image_url"]["url"],
            "data:image/png;base64,encoded-image",
        )
        self.assertEqual(payload["messages"][0]["content"][0]["text"], "请识别图片")

    def test_missing_language_keeps_chinese_fallback(self):
        provider = VLLMProvider(
            {
                "base_url": "http://agent:15000",
                "model_name": "museum-guide-vision",
                "api_key": "test-key",
            }
        )
        response = MagicMock()
        response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        client = MagicMock()
        client.__enter__.return_value.post.return_value = response

        with patch("core.providers.vllm.openai.httpx.Client", return_value=client):
            provider.response("Describe", "encoded-image")

        _, kwargs = client.__enter__.return_value.post.call_args
        prompt = kwargs["json"]["messages"][0]["content"][0]["text"]
        self.assertIn("请使用中文回复", prompt)


if __name__ == "__main__":
    unittest.main()
