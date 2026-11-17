import asyncio
import base64
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx


def load_provider():
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory() as directory:
        provider_path = Path(directory) / "core/providers/asr/qwen3_asr_flash.py"
        provider_path.parent.mkdir(parents=True)
        shutil.copyfile(
            root.parent / "main/xiaozhi-server/core/providers/asr/qwen3_asr_flash.py",
            provider_path,
        )
        patch_path = root / "patches/qwen-fun-asr.patch"
        subprocess.run(["git", "apply", "--check", str(patch_path)], cwd=directory, check=True)
        subprocess.run(["git", "apply", str(patch_path)], cwd=directory, check=True)
        logger = ModuleType("config.logger")
        logger.setup_logging = lambda: Mock()
        audio_handler = ModuleType("core.handle.receiveAudioHandle")
        audio_handler.startToChat = Mock()
        audio_handler.handleAudioMessage = Mock()
        report = ModuleType("core.handle.reportHandle")
        report.enqueue_asr_report = Mock()
        util = ModuleType("core.utils.util")
        util.remove_punctuation_and_length = Mock()
        dto = ModuleType("core.providers.asr.dto.dto")
        dto.InterfaceType = SimpleNamespace(NON_STREAM="NON_STREAM")
        with patch.dict(sys.modules, {
            "config.logger": logger,
            "core.handle.receiveAudioHandle": audio_handler,
            "core.handle.reportHandle": report,
            "core.utils.util": util,
            "core.providers.asr.dto.dto": dto,
        }):
            base_spec = importlib.util.spec_from_file_location(
                "core.providers.asr.base",
                root.parent / "main/xiaozhi-server/core/providers/asr/base.py",
            )
            base = importlib.util.module_from_spec(base_spec)
            base_spec.loader.exec_module(base)
            with patch.dict(sys.modules, {"core.providers.asr.base": base}):
                spec = importlib.util.spec_from_file_location(
                    "ksz_qwen_asr_test", provider_path,
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
        return module


provider_module = load_provider()
ASRProvider = provider_module.ASRProvider
REAL_ASYNC_CLIENT = httpx.AsyncClient


class QwenASRTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.pcm = b"\x01\x00" * 160

    def provider(self, **config):
        provider = ASRProvider({
            "api_key": "test-key", "output_dir": self.directory.name,
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            **config,
        }, True)
        self.addAsyncCleanup(provider.close)
        return provider

    async def http_server(self, close_after_response=False):
        connections = []
        handlers = set()
        disconnected = asyncio.Event()

        async def handle(reader, writer):
            task = asyncio.current_task()
            handlers.add(task)
            connections.append(writer)
            try:
                while True:
                    try:
                        headers = await reader.readuntil(b"\r\n\r\n")
                    except asyncio.IncompleteReadError:
                        break
                    length = next(
                        int(line.split(b":", 1)[1])
                        for line in headers.split(b"\r\n")
                        if line.lower().startswith(b"content-length:")
                    )
                    await reader.readexactly(length)
                    body = b'{"output":{"text":"reused"}}'
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        + f"Content-Length: {len(body)}\r\n\r\n".encode()
                        + body
                    )
                    await writer.drain()
                    if close_after_response:
                        break
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(task)
                disconnected.set()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)

        async def cleanup():
            server.close()
            await server.wait_closed()
            for writer in connections:
                writer.close()
            if handlers:
                await asyncio.gather(*list(handlers), return_exceptions=True)

        self.addAsyncCleanup(cleanup)
        port = server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/generation", connections, disconnected

    def client(self, handler):
        return patch.object(provider_module.httpx, "AsyncClient", side_effect=lambda **kwargs:
            REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs))

    async def test_official_preset_uploads_real_wav_and_preserves_options(self):
        async def handler(request):
            self.assertEqual(request.url.path, "/api/v1/services/aigc/multimodal-generation/generation")
            self.assertEqual(request.headers["X-DashScope-SSE"], "disable")
            body = json.loads(request.content)
            self.assertEqual(body["model"], "qwen3-asr-flash")
            self.assertEqual(body["parameters"]["asr_options"], {
                "enable_lid": True, "enable_itn": False, "language": "zh",
            })
            audio = body["input"]["messages"][-1]["content"][0]["audio"]
            self.assertTrue(audio.startswith("data:audio/wav;base64,"))
            with wave.open(io.BytesIO(base64.b64decode(audio.split(",", 1)[1]))) as wav:
                self.assertEqual(wav.getframerate(), 16000)
                self.assertEqual(wav.readframes(160), self.pcm)
            return httpx.Response(200, json={"output": {"choices": [{"message": {
                "content": [{"text": "官方预设可用。"}],
            }}]}})

        with self.client(handler):
            result = await self.provider(language="zh", enable_itn=False).speech_to_text_wrapper([self.pcm], "probe")
        self.assertEqual(result, ("官方预设可用。", None))
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    async def test_fun_flash_uses_input_audio_protocol(self):
        async def handler(request):
            body = json.loads(request.content)
            content = body["input"]["messages"][-1]["content"][0]
            self.assertEqual(content["type"], "input_audio")
            self.assertTrue(content["input_audio"]["data"].startswith("data:audio/wav;base64,"))
            self.assertEqual(body["parameters"], {"format": "wav", "sample_rate": "16000"})
            return httpx.Response(200, json={"output": {"text": "Fun识别成功。"}})

        with self.client(handler):
            result = await self.provider(model_name="fun-asr-flash-2026-06-15").speech_to_text_wrapper([self.pcm], "probe")
        self.assertEqual(result[0], "Fun识别成功。")

    async def test_shared_model_uses_each_connections_bound_language(self):
        extension = ModuleType("core.utils.ksz_extension")
        extension.guide = SimpleNamespace(response_language=lambda conn: conn.language)
        languages = {"zh-CN": "zh", "en": "en", "ja": "ja", "ko": "ko"}
        for model in ("qwen3-asr-flash", "fun-asr-flash-2026-06-15"):
            shared = {"model_name": model, "language": "auto"}
            providers = [self.provider(**shared) for _ in languages]
            with patch.dict(sys.modules, {extension.__name__: extension}), patch.object(
                ASRProvider.__mro__[1], "open_audio_channels", new_callable=AsyncMock,
            ) as opened:
                await asyncio.gather(*(
                    provider.open_audio_channels(SimpleNamespace(language=language))
                    for provider, language in zip(providers, languages)
                ))
            self.assertEqual(opened.await_count, len(languages))
            for provider, expected in zip(providers, languages.values()):
                parameters = provider._build_payload("audio")["parameters"]
                if model == "qwen3-asr-flash":
                    self.assertEqual(parameters["asr_options"]["language"], expected)
                else:
                    self.assertEqual(parameters["language_hints"], [expected])
            self.assertEqual(shared["language"], "auto")

    async def test_unmapped_binding_or_missing_extension_preserves_model_language(self):
        extension = ModuleType("core.utils.ksz_extension")
        for guide in (None, SimpleNamespace(response_language=lambda conn: "zh-CN-yue")):
            extension.guide = guide
            provider = self.provider(language="en")
            with patch.dict(sys.modules, {extension.__name__: extension}), patch.object(
                ASRProvider.__mro__[1], "open_audio_channels", new_callable=AsyncMock,
            ) as opened:
                await provider.open_audio_channels(SimpleNamespace())
            self.assertEqual(provider.language, "en")
            opened.assert_awaited_once()

    async def test_stalled_cloud_request_keeps_loop_running_and_expires(self):
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        async def handler(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        provider = self.provider(timeout=0.1)
        with self.client(handler):
            task = asyncio.create_task(provider.speech_to_text_wrapper([self.pcm], "probe"))
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            result = await asyncio.wait_for(task, 1)
        self.assertEqual(result, ("", None))
        self.assertTrue(cancelled.is_set())

    async def test_cloud_error_returns_empty_without_breaking_loop(self):
        with self.client(lambda request: httpx.Response(401, json={"code": "InvalidApiKey"})):
            self.assertEqual(await self.provider().speech_to_text_wrapper([self.pcm], "probe"), ("", None))

    async def test_cancellation_propagates_and_cleans_temp_file(self):
        entered = asyncio.Event()
        async def handler(request):
            entered.set()
            await asyncio.Event().wait()

        provider = self.provider()
        paths = []
        original_build = provider.build_temp_file
        def build(pcm):
            path = original_build(pcm)
            paths.append(path)
            return path
        provider.build_temp_file = build
        with self.client(handler):
            task = asyncio.create_task(provider.speech_to_text_wrapper([self.pcm], "probe"))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(Path(paths[0]).exists())

    async def test_reuses_tcp_connection_beyond_five_seconds_and_closes_on_disconnect(self):
        url, connections, disconnected = await self.http_server()
        provider = self.provider(base_url=url)
        first = await provider.speech_to_text_wrapper([self.pcm], "probe")
        await asyncio.sleep(5.1)
        second = await provider.speech_to_text_wrapper([self.pcm], "probe")
        self.assertEqual(first[0], "reused")
        self.assertEqual(second[0], "reused")
        self.assertEqual(len(connections), 1)
        await provider.close()
        await asyncio.wait_for(disconnected.wait(), 1)
        self.assertTrue(provider._client.is_closed)
        await provider.close()

    async def test_expired_idle_connection_is_replaced(self):
        url, connections, _ = await self.http_server()
        provider = self.provider(base_url=url, keepalive_expiry=0.05)
        self.assertEqual((await provider.speech_to_text_wrapper([self.pcm], "probe"))[0], "reused")
        await asyncio.sleep(0.1)
        self.assertEqual((await provider.speech_to_text_wrapper([self.pcm], "probe"))[0], "reused")
        self.assertEqual(len(connections), 2)

    async def test_cloud_closed_idle_connection_is_replaced(self):
        url, connections, disconnected = await self.http_server(close_after_response=True)
        provider = self.provider(base_url=url)
        self.assertEqual((await provider.speech_to_text_wrapper([self.pcm], "probe"))[0], "reused")
        await asyncio.wait_for(disconnected.wait(), 1)
        await asyncio.sleep(0.01)
        self.assertEqual((await provider.speech_to_text_wrapper([self.pcm], "probe"))[0], "reused")
        self.assertEqual(len(connections), 2)

    def test_nested_fun_result(self):
        self.assertEqual(ASRProvider._extract_text({"output": {"output": {
            "sentence": {"text": "嵌套结果"},
        }}}), "嵌套结果")


if __name__ == "__main__":
    unittest.main()
