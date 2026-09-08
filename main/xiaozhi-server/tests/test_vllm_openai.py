import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.providers.vllm.openai import VLLMProvider, _resolve_chat_completions_url
from core.providers.vllm.base import VLLMProviderBase


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


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks, pause=None):
        self.chunks = chunks
        self.pause = pause
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk
        if self.pause is not None:
            self.pause.set()
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


class OpenAIVLLMStreamingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.provider = VLLMProvider(
            {
                "base_url": "http://agent:15000/v1",
                "model_name": "museum-guide-vision",
                "api_key": "test-key",
            }
        )

    @staticmethod
    def event(delta=None, finish_reason=None):
        return (
            "data: "
            + json.dumps(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": delta or {},
                            "finish_reason": finish_reason,
                        }
                    ]
                },
                ensure_ascii=False,
            )
            + "\n\n"
        ).encode()

    def stream_client(self, stream, status=200):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(
                status,
                headers={"Content-Type": "text/event-stream"},
                stream=stream,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        factory = patch(
            "core.providers.vllm.openai.httpx.AsyncClient", return_value=client
        )
        return factory, requests

    async def collect(self, **kwargs):
        return [
            text
            async for text in self.provider.response_stream(
                "请识别图片", "encoded-image", **kwargs
            )
        ]

    async def test_streams_content_before_completion_with_context(self):
        stream = ChunkStream(
            [
                self.event({"reasoning_content": "不可播报", "content": "第一句。"}),
                self.event({"content": "第二句。"}),
                self.event(finish_reason="stop"),
            ]
        )
        factory, requests = self.stream_client(stream)
        with factory as mocked_client:
            iterator = self.provider.response_stream(
                "请识别图片",
                "encoded-image",
                image_mime="image/png",
                device_id="device-001",
                language="zh-CN",
                last_beacon_id="beacon-001",
            )
            self.assertEqual(await anext(iterator), "第一句。")
            self.assertFalse(stream.closed)
            self.assertEqual([text async for text in iterator], ["第二句。"])
        self.assertTrue(stream.closed)
        self.assertEqual(len(requests), 1)
        payload = json.loads(requests[0].content)
        self.assertTrue(payload["stream"])
        self.assertNotIn("enable_thinking", payload)
        self.assertEqual(payload["device_id"], "device-001")
        self.assertEqual(payload["language"], "zh-CN")
        self.assertEqual(payload["last_beacon_id"], "beacon-001")
        self.assertEqual(
            payload["messages"][0]["content"][1]["image_url"]["url"],
            "data:image/png;base64,encoded-image",
        )
        self.assertEqual(payload["messages"][0]["content"][0]["text"], "请识别图片")
        self.assertFalse(mocked_client.call_args.kwargs["trust_env"])
        timeout = mocked_client.call_args.kwargs["timeout"]
        self.assertEqual(timeout.connect, 10)
        self.assertEqual(timeout.read, 30)

    async def test_sse_comments_empty_events_multiline_and_split_unicode(self):
        event = (
            ': keep-alive\r\n\r\nevent: message\r\ndata:\r\n\r\n'
            'data: {"choices": [\r\ndata: {"delta": {"content": "藏品。"}}]}\r\n\r\n'
            'data: [DONE]\r\n\r\n'
        ).encode()
        stream = ChunkStream([event[index:index + 1] for index in range(len(event))])
        factory, _ = self.stream_client(stream)
        with factory:
            self.assertEqual(await self.collect(), ["藏品。"])

    async def test_reasoning_tool_calls_and_usage_are_not_spoken(self):
        stream = ChunkStream(
            [
                self.event({"role": "assistant"}),
                self.event({"reasoning_content": "内部思考"}),
                self.event({"tool_calls": [{"function": {"name": "camera"}}]}),
                b'data: {"choices":[],"usage":{"total_tokens":12}}\n\n',
                self.event({"content": "正文。"}),
                b"data: [DONE]\n\n",
            ]
        )
        factory, _ = self.stream_client(stream)
        with factory:
            self.assertEqual(await self.collect(), ["正文。"])

    async def test_invalid_empty_and_truncated_streams_fail(self):
        cases = {
            "empty": [b"data: [DONE]\n\n"],
            "whitespace": [self.event({"content": " "}), b"data: [DONE]\n\n"],
            "reasoning_only": [self.event({"reasoning_content": "思考"}), b"data: [DONE]\n\n"],
            "invalid_json": [b"data: {broken\n\n"],
            "error_event": [b'data: {"error":{"message":"internal"}}\n\n'],
            "unexpected_payload": [b'data: {"text":"unexpected"}\n\n'],
            "missing_terminal": [self.event({"content": "未完"})],
            "length_limit": [self.event({"content": "未完"}), self.event(finish_reason="length")],
            "invalid_content": [self.event({"content": {"text": "非文本"}})],
        }
        for name, chunks in cases.items():
            with self.subTest(name=name):
                stream = ChunkStream(chunks)
                factory, requests = self.stream_client(stream)
                with factory, self.assertRaises(ValueError):
                    await self.collect()
                self.assertTrue(stream.closed)
                self.assertEqual(len(requests), 1)

    async def test_http_error_is_logged_but_public_exception_is_safe(self):
        stream = ChunkStream([b'{"error":"private provider detail"}'])
        factory, requests = self.stream_client(stream, status=503)
        with factory, self.assertRaisesRegex(ValueError, "^VLLM 请求失败$"):
            await self.collect()
        self.assertEqual(len(requests), 1)
        self.assertTrue(stream.closed)

    async def test_read_error_closes_stream_without_retry(self):
        stream = ChunkStream(
            [self.event({"content": "第一句。"}), httpx.ReadTimeout("read timed out")]
        )
        factory, requests = self.stream_client(stream)
        with factory, self.assertRaises(httpx.ReadTimeout):
            await self.collect()
        self.assertTrue(stream.closed)
        self.assertEqual(len(requests), 1)

    async def test_cancellation_closes_http_stream(self):
        paused = asyncio.Event()
        stream = ChunkStream([self.event({"content": "第一句。"})], pause=paused)
        factory, requests = self.stream_client(stream)
        with factory:
            task = asyncio.create_task(self.collect())
            await asyncio.wait_for(paused.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(stream.closed)
        self.assertEqual(len(requests), 1)

    async def test_explicit_closing_releases_partial_stream(self):
        stream = ChunkStream([self.event({"content": "第一句。"}), b"data: [DONE]\n\n"])
        factory, _ = self.stream_client(stream)
        with factory:
            iterator = self.provider.response_stream("识别", "image")
            self.assertEqual(await anext(iterator), "第一句。")
            await iterator.aclose()
        self.assertTrue(stream.closed)

    async def test_deadline_clips_timeouts_and_is_not_sent_to_model(self):
        stream = ChunkStream([self.event({"content": "完成"}, finish_reason="stop")])
        factory, requests = self.stream_client(stream)
        self.provider.enable_thinking = False
        with factory as mocked_client:
            await self.collect(deadline=asyncio.get_running_loop().time() + 2)
        timeout = mocked_client.call_args.kwargs["timeout"]
        self.assertGreater(timeout.read, 0)
        self.assertLessEqual(timeout.read, 2)
        self.assertLessEqual(timeout.connect, 2)
        payload = json.loads(requests[0].content)
        self.assertNotIn("deadline", payload)
        self.assertFalse(payload["enable_thinking"])

    async def test_expired_deadline_does_not_start_request(self):
        with patch("core.providers.vllm.openai.httpx.AsyncClient") as factory:
            with self.assertRaises(TimeoutError):
                await self.collect(deadline=asyncio.get_running_loop().time() - 1)
        factory.assert_not_called()

    def test_other_providers_are_not_forced_to_stream(self):
        self.assertFalse(VLLMProviderBase.supports_streaming)
        self.assertTrue(self.provider.supports_streaming)


if __name__ == "__main__":
    unittest.main()
