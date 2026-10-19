import asyncio
import json
import queue
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.api.vision_handler import VisionHandler
from core.providers.tools.device_mcp.mcp_executor import DeviceMCPExecutor
from core.providers.tts.dto.dto import ContentType, SentenceType
from core.utils.vision_stream import (
    SentenceBuffer,
    VISION_INTERRUPTED_MESSAGE,
    VISION_UNAVAILABLE_MESSAGE,
    get_vision_sessions,
    run_vision_tool,
)
from plugins_func.register import Action


def connection(streaming=True):
    return SimpleNamespace(
        sentence_id="turn-1",
        session_id="connection-1",
        client_abort=False,
        client_is_speaking=False,
        need_bind=False,
        stop_event=threading.Event(),
        features={"vision_stream": streaming},
        logger=MagicMock(),
        dialogue=SimpleNamespace(put=MagicMock()),
        tool_feedback=MagicMock(),
        tts=SimpleNamespace(
            tts_text_queue=queue.Queue(),
            store_tts_text=MagicMock(),
            tts_one_sentence=MagicMock(),
        ),
        mcp_client=SimpleNamespace(
            is_ready=AsyncMock(return_value=True), call_results={}
        ),
        websocket=SimpleNamespace(send=AsyncMock()),
        clear_queues=MagicMock(),
        clearSpeakStatus=MagicMock(),
    )


def spoken_text(conn):
    return [
        message.content_detail
        for message in conn.tts.tts_text_queue.queue
        if message.content_type == ContentType.TEXT
    ]


def camera_result(text, is_error=False):
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


class SentenceBufferTests(unittest.TestCase):
    def test_cross_chunk_sentences_and_final_tail_are_emitted_once(self):
        buffer = SentenceBuffer()
        self.assertEqual(buffer.feed("这是第"), [])
        self.assertEqual(buffer.feed("一句。第二"), ["这是第一句。"])
        self.assertEqual(buffer.feed("句！第三句；尾句"), ["第二句！", "第三句；"])
        self.assertEqual(buffer.feed("", final=True), ["尾句"])
        self.assertEqual(buffer.feed("", final=True), [])

    def test_english_period_and_decimal_across_chunks(self):
        buffer = SentenceBuffer()
        self.assertEqual(buffer.feed("The value is 3."), [])
        self.assertEqual(buffer.feed("14. Next"), ["The value is 3.14."])
        self.assertEqual(buffer.feed(" sentence. "), ["Next sentence."])
        self.assertEqual(buffer.feed("", final=True), [])

    def test_whitespace_and_newline_do_not_create_empty_speech(self):
        buffer = SentenceBuffer()
        self.assertEqual(buffer.feed("\n  \n一行\n\n尾句"), ["一行"])
        self.assertEqual(buffer.feed("", final=True), ["尾句"])


class VisionSessionsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = connection()
        self.sessions = get_vision_sessions(self.conn)

    def begin(self, deadline=None):
        return self.sessions.begin_mcp(
            self.conn.sentence_id, deadline or time.monotonic() + 120
        )

    def test_upload_claim_is_single_use_and_connection_scoped(self):
        request = self.begin()
        other_sessions = get_vision_sessions(connection())
        with self.assertRaises(ValueError):
            other_sessions.claim_mcp(request.request_id)
        self.assertIs(self.sessions.claim_mcp(request.request_id), request)
        with self.assertRaises(ValueError):
            self.sessions.claim_mcp(request.request_id)

    async def test_new_request_cancels_old_and_rejects_late_content(self):
        old = self.begin()
        old.task = asyncio.create_task(asyncio.Event().wait())
        current = self.begin()
        self.assertTrue(old.cancel_event.is_set())
        self.assertTrue(old.cancelled)
        with self.assertRaises(asyncio.CancelledError):
            await old.task
        with self.assertRaises(ValueError):
            self.sessions.claim_mcp(old.request_id)
        self.assertFalse(self.sessions.emit(old, "旧内容。"))
        self.sessions.finish(old)
        self.assertTrue(self.sessions.emit(current, "新内容。"))
        self.assertEqual(spoken_text(self.conn), ["新内容。"])
        self.conn.dialogue.put.assert_not_called()

    def test_new_turn_and_abort_reject_old_output_and_history(self):
        request = self.begin()
        self.conn.sentence_id = "turn-2"
        self.assertFalse(self.sessions.emit(request, "旧轮次。"))
        self.sessions.finish(request)
        self.conn.dialogue.put.assert_not_called()
        self.conn.sentence_id = request.sentence_id
        self.conn.client_abort = True
        self.assertFalse(self.sessions.emit(request, "已打断。"))
        self.assertEqual(spoken_text(self.conn), [])

    def test_finished_or_expired_request_cannot_claim_upload(self):
        finished = self.begin()
        self.sessions.finish(finished)
        with self.assertRaises(ValueError):
            self.sessions.claim_mcp(finished.request_id)
        expired = self.begin(deadline=time.monotonic() - 1)
        with self.assertRaises(ValueError):
            self.sessions.claim_mcp(expired.request_id)

    def test_stale_mcp_registration_cannot_cancel_current_request(self):
        self.conn.sentence_id = "turn-2"
        current = self.begin()
        with self.assertRaises(ValueError):
            self.sessions.begin_mcp("turn-1", time.monotonic() + 120)
        self.assertIs(self.sessions.active, current)
        self.assertFalse(current.cancelled)
        self.assertTrue(self.sessions.emit(current, "新一轮结果。"))

    def test_finish_records_once_and_does_not_replay_sentences(self):
        request = self.begin()
        self.sessions.emit(request, "第一句。")
        self.sessions.emit(request, "第二句。")
        self.sessions.finish(request)
        self.sessions.finish(request)
        self.assertFalse(self.sessions.emit(request, "迟到。"))
        self.assertEqual(spoken_text(self.conn), ["第一句。", "第二句。"])
        self.conn.dialogue.put.assert_called_once()
        self.assertEqual(
            self.conn.dialogue.put.call_args.args[0].content, "第一句。第二句。"
        )

    def test_successful_finish_preserves_original_english_spacing_in_history(self):
        request = self.begin()
        text = "First sentence. Next sentence.\nA final fragment"
        buffer = SentenceBuffer()
        for sentence in buffer.feed(text, final=True):
            self.sessions.emit(request, sentence)
        self.sessions.finish(request, full_text=text)
        self.conn.dialogue.put.assert_called_once()
        self.assertEqual(self.conn.dialogue.put.call_args.args[0].content, text)
        self.assertEqual(
            spoken_text(self.conn),
            ["First sentence.", "Next sentence.", "A final fragment"],
        )

    async def test_manual_request_supersedes_mcp_and_closes_once(self):
        previous = self.begin()
        with patch(
            "core.handle.sendAudioHandle.send_tts_message", new=AsyncMock()
        ):
            current = await self.sessions.begin_push("manual-1")
        self.assertTrue(previous.cancelled)
        self.assertNotEqual(current.sentence_id, previous.sentence_id)
        self.sessions.emit(current, "手动拍照。")
        self.sessions.finish(current)
        self.sessions.finish(current)
        messages = list(self.conn.tts.tts_text_queue.queue)
        self.assertEqual(
            [message.sentence_type for message in messages],
            [SentenceType.FIRST, SentenceType.MIDDLE, SentenceType.LAST],
        )
        with self.assertRaises(ValueError):
            await self.sessions.begin_push("manual-1")


class CameraExecutorStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def execute(self, conn, response=None, error=None):
        with patch(
            "core.providers.tools.device_mcp.mcp_executor.call_mcp_tool",
            new=AsyncMock(return_value=response, side_effect=error),
        ):
            return await DeviceMCPExecutor(conn).execute(
                conn, "self_camera_take_photo", {"question": "描述图片"}
            )

    async def test_legacy_plain_text_and_numeric_content_are_direct_answers(self):
        for text in ("这是一件展品。", "123", "2026", '"红色"'):
            with self.subTest(text=text):
                result = await self.execute(connection(False), camera_result(text))
                self.assertEqual(result.action, Action.RESPONSE)
                self.assertEqual(result.response, text)

    async def test_explicit_mcp_error_is_not_treated_as_vision_text(self):
        result = await self.execute(
            connection(False), camera_result("HTTP 503 private provider error", True)
        )
        self.assertEqual(result.action, Action.RESPONSE)
        self.assertEqual(result.response, VISION_UNAVAILABLE_MESSAGE)

    async def test_rpc_finish_before_upload_rejects_late_claim(self):
        conn = connection()
        result = await self.execute(conn, camera_result("摄像头拍摄失败", True))
        self.assertEqual(result.action, Action.RESPONSE)
        request = conn.vision_sessions.active
        with self.assertRaises(ValueError):
            conn.vision_sessions.claim_mcp(request.request_id)

    async def test_cancelled_mcp_call_cannot_return_old_text(self):
        conn = connection()
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def pending_rpc(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch(
            "core.providers.tools.device_mcp.mcp_executor.call_mcp_tool",
            new=pending_rpc,
        ):
            task = asyncio.create_task(DeviceMCPExecutor(conn).execute(
                conn, "self_camera_take_photo", {"question": "描述图片"}
            ))
            await asyncio.wait_for(entered.wait(), 1)
            conn.vision_sessions.cancel("aborted")
            result = await asyncio.wait_for(task, 1)
        self.assertEqual(result.action, Action.NONE)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(spoken_text(conn), [])

    async def test_rpc_timeout_is_fixed_speech_without_chat_llm(self):
        conn = connection()
        result = await self.execute(conn, error=TimeoutError("private detail"))
        self.assertEqual(result.action, Action.NONE)
        self.assertEqual(spoken_text(conn), [VISION_UNAVAILABLE_MESSAGE])
        self.conn_finished(conn)

    async def test_root_deadline_emits_one_failure_even_when_rpc_and_http_are_pending(self):
        for partial in ("", "已确认第一句。"):
            with self.subTest(partial=partial):
                conn = connection()
                entered = asyncio.Event()

                async def rpc(*args, **kwargs):
                    sessions = conn.vision_sessions
                    request = sessions.claim_mcp(kwargs["metadata"]["vision_request_id"])
                    request.task = asyncio.create_task(asyncio.Event().wait())
                    if partial:
                        sessions.emit(request, partial)
                    entered.set()
                    await asyncio.Event().wait()

                async def dispatch(conn, call):
                    return await DeviceMCPExecutor(conn).execute(
                        conn, call["name"], call["arguments"]
                    )

                conn.func_handler = SimpleNamespace(handle_llm_function_call=dispatch)
                with patch(
                    "core.providers.tools.device_mcp.mcp_executor.call_mcp_tool", new=rpc
                ):
                    result = await asyncio.wait_for(run_vision_tool(
                        conn,
                        {"name": "self_camera_take_photo", "arguments": {}},
                        conn.sentence_id,
                        time.monotonic() + 0.05,
                    ), 1)
                self.assertTrue(entered.is_set())
                self.assertEqual(result.action, Action.NONE)
                expected = [partial, VISION_INTERRUPTED_MESSAGE] if partial else [VISION_UNAVAILABLE_MESSAGE]
                self.assertEqual(spoken_text(conn), expected)
                self.assertTrue(conn.vision_sessions.active.finished)
                self.assertFalse(conn.vision_sessions.active.cancelled)
                with self.assertRaises(asyncio.CancelledError):
                    await conn.vision_sessions.active.task
                conn.dialogue.put.assert_called_once()

    async def test_user_cancel_within_root_deadline_remains_silent(self):
        conn = connection()
        entered = asyncio.Event()

        async def rpc(*args, **kwargs):
            request = conn.vision_sessions.claim_mcp(kwargs["metadata"]["vision_request_id"])
            request.task = asyncio.create_task(asyncio.Event().wait())
            entered.set()
            await asyncio.Event().wait()

        async def dispatch(conn, call):
            return await DeviceMCPExecutor(conn).execute(conn, call["name"], {})

        conn.func_handler = SimpleNamespace(handle_llm_function_call=dispatch)
        with patch(
            "core.providers.tools.device_mcp.mcp_executor.call_mcp_tool", new=rpc
        ):
            task = asyncio.create_task(run_vision_tool(
                conn, {"name": "self_camera_take_photo"},
                conn.sentence_id, time.monotonic() + 10,
            ))
            await asyncio.wait_for(entered.wait(), 1)
            conn.client_abort = True
            conn.vision_sessions.cancel("aborted")
            result = await asyncio.wait_for(task, 1)
        self.assertEqual(result.action, Action.NONE)
        self.assertEqual(spoken_text(conn), [])
        conn.dialogue.put.assert_not_called()
        with self.assertRaises(asyncio.CancelledError):
            await conn.vision_sessions.active.task

    def conn_finished(self, conn):
        self.assertTrue(conn.vision_sessions.active.finished)
        conn.dialogue.put.assert_called_once()


class VisionHTTPStreamingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = connection()
        self.sessions = get_vision_sessions(self.conn)
        self.handler = object.__new__(VisionHandler)
        self.handler.config = {
            "read_config_from_api": False,
            "selected_module": {"VLLM": "mock"},
            "VLLM": {"mock": {"type": "mock"}},
        }
        self.handler.logger = MagicMock()
        self.handler.auth = MagicMock()
        self.handler.websocket_server = SimpleNamespace(
            find_device_connection=MagicMock(return_value=self.conn)
        )

    def request(self, mode="mcp", request_id=None):
        fields = [("question", "描述图片".encode()), ("image", b"\xff\xd8\xffimage")]
        if mode is not None:
            fields.append(("delivery_mode", mode.encode()))
        if request_id is not None:
            fields.append(("request_id", request_id.encode()))
        reader = SimpleNamespace(next=AsyncMock(side_effect=[
            *[SimpleNamespace(name=name, read=AsyncMock(return_value=value))
              for name, value in fields], None,
        ]))
        return SimpleNamespace(
            headers={"Client-Id": "web_test_client", "Device-Id": "device-1"},
            multipart=AsyncMock(return_value=reader),
        )

    def begin(self):
        return self.sessions.begin_mcp(self.conn.sentence_id, time.monotonic() + 120)

    async def test_first_sentence_queued_before_vlm_and_http_finish(self):
        paused = asyncio.Event()
        release = asyncio.Event()

        async def stream(*args, **kwargs):
            yield "第一"
            yield "句。未完成的"
            paused.set()
            await release.wait()
            yield "尾句"

        provider = SimpleNamespace(supports_streaming=True, response_stream=stream)
        request = self.begin()
        with patch("core.api.vision_handler.create_instance", return_value=provider):
            task = asyncio.create_task(self.handler.handle_post(
                self.request(request_id=request.request_id)
            ))
            try:
                await asyncio.wait_for(paused.wait(), 1)
                self.assertFalse(task.done())
                self.assertEqual(spoken_text(self.conn), ["第一句。"])
                self.conn.dialogue.put.assert_not_called()
            finally:
                release.set()
                response = await asyncio.wait_for(task, 1)
        body = json.loads(response.text)
        self.assertTrue(body["success"])
        self.assertEqual(body["delivery"], "streamed")
        self.assertEqual(body["request_id"], request.request_id)
        self.assertEqual(body["response"], "第一句。未完成的尾句")
        self.assertEqual(spoken_text(self.conn), ["第一句。", "未完成的尾句"])

    async def test_partial_stream_error_speaks_only_completed_text_and_safe_hint(self):
        async def stream(*args, **kwargs):
            yield "已经确认。未确认的残片"
            raise ValueError("private provider detail")

        request = self.begin()
        provider = SimpleNamespace(supports_streaming=True, response_stream=stream)
        with patch("core.api.vision_handler.create_instance", return_value=provider):
            response = await self.handler.handle_post(
                self.request(request_id=request.request_id)
            )
        body = json.loads(response.text)
        self.assertFalse(body["success"])
        self.assertEqual(body["delivery"], "streamed")
        self.assertEqual(spoken_text(self.conn), ["已经确认。", VISION_INTERRUPTED_MESSAGE])
        self.assertNotIn("private", response.text)
        self.assertNotIn("未确认", self.conn.dialogue.put.call_args.args[0].content)

    async def test_buffered_return_does_not_push_even_when_device_is_online(self):
        provider = SimpleNamespace(supports_streaming=False, response=lambda *a, **k: "完整结果。")
        with patch("core.api.vision_handler.create_instance", return_value=provider):
            response = await self.handler.handle_post(self.request(mode="return"))
        self.assertTrue(json.loads(response.text)["success"])
        self.assertEqual(spoken_text(self.conn), [])
        self.conn.tts.tts_one_sentence.assert_not_called()
        self.conn.dialogue.put.assert_not_called()

    async def test_old_mcp_upload_never_switches_to_push_after_rpc_disappears(self):
        self.conn.mcp_client.call_results = {1: object()}

        def response(*args, **kwargs):
            self.conn.mcp_client.call_results.clear()
            return "迟到的旧结果。"

        provider = SimpleNamespace(supports_streaming=False, response=response)
        with patch("core.api.vision_handler.create_instance", return_value=provider):
            result = await self.handler.handle_post(self.request(mode=None))
        self.assertTrue(json.loads(result.text)["success"])
        self.conn.tts.tts_one_sentence.assert_not_called()
        self.assertEqual(spoken_text(self.conn), [])

    async def test_legacy_manual_request_cannot_replace_a_new_chat_turn(self):
        def response(*args, **kwargs):
            self.conn.sentence_id = "new-chat-turn"
            return "旧图片结果。"

        provider = SimpleNamespace(supports_streaming=False, response=response)
        with patch("core.api.vision_handler.create_instance", return_value=provider):
            await self.handler.handle_post(self.request(mode=None))
        self.assertEqual(self.conn.sentence_id, "new-chat-turn")
        self.conn.tts.tts_one_sentence.assert_not_called()

    async def test_cancelled_stream_closes_provider_and_does_not_append_failure(self):
        paused = asyncio.Event()
        closed = asyncio.Event()

        async def stream(*args, **kwargs):
            try:
                yield "第一句。"
                paused.set()
                await asyncio.Event().wait()
            finally:
                closed.set()

        provider = SimpleNamespace(supports_streaming=True, response_stream=stream)
        request = self.begin()
        with patch("core.api.vision_handler.create_instance", return_value=provider):
            task = asyncio.create_task(self.handler.handle_post(
                self.request(request_id=request.request_id)
            ))
            await asyncio.wait_for(paused.wait(), 1)
            self.sessions.cancel("new_turn")
            response = await asyncio.wait_for(task, 1)
        self.assertTrue(closed.is_set())
        self.assertEqual(json.loads(response.text)["delivery"], "cancelled")
        self.assertEqual(spoken_text(self.conn), ["第一句。"])
        self.conn.dialogue.put.assert_not_called()

    async def test_http_disconnect_stops_speech_once_and_closes_generation(self):
        paused = asyncio.Event()
        closed = asyncio.Event()

        async def stream(*args, **kwargs):
            try:
                yield "第一句。"
                paused.set()
                await asyncio.Event().wait()
            finally:
                closed.set()

        def clear_queue():
            while not self.conn.tts.tts_text_queue.empty():
                self.conn.tts.tts_text_queue.get_nowait()

        self.conn.clear_queues.side_effect = clear_queue
        provider = SimpleNamespace(supports_streaming=True, response_stream=stream)
        request = self.begin()
        closed_http = SimpleNamespace(transport=SimpleNamespace(is_closing=lambda: True))
        with patch("core.api.vision_handler.create_instance", return_value=provider):
            task = asyncio.create_task(self.handler.handle_post(
                self.request(request_id=request.request_id)
            ))
            await asyncio.wait_for(paused.wait(), 1)
            await self.handler._watch_disconnect(closed_http, request)
            response = await asyncio.wait_for(task, 1)
            await self.sessions.disconnect(request)
        self.assertTrue(closed.is_set())
        self.assertTrue(request.cancelled)
        self.assertTrue(self.conn.client_abort)
        self.assertEqual(json.loads(response.text)["delivery"], "cancelled")
        self.assertEqual(spoken_text(self.conn), [])
        self.conn.clear_queues.assert_called_once()
        self.conn.websocket.send.assert_awaited_once()
        message = json.loads(self.conn.websocket.send.await_args.args[0])
        self.assertEqual((message["type"], message["state"]), ("tts", "stop"))
        self.conn.dialogue.put.assert_not_called()

    async def test_finalized_deadline_cleanup_does_not_erase_failure_speech(self):
        paused = asyncio.Event()

        async def stream(*args, **kwargs):
            yield "已确认第一句。"
            paused.set()
            await asyncio.Event().wait()

        provider = SimpleNamespace(supports_streaming=True, response_stream=stream)
        request = self.begin()
        with patch("core.api.vision_handler.create_instance", return_value=provider):
            task = asyncio.create_task(self.handler.handle_post(
                self.request(request_id=request.request_id)
            ))
            await asyncio.wait_for(paused.wait(), 1)
            self.sessions.fail(request)
            request.task.cancel()
            await asyncio.wait_for(task, 1)
        self.assertFalse(self.conn.client_abort)
        self.conn.clear_queues.assert_not_called()
        self.conn.websocket.send.assert_not_awaited()
        self.assertEqual(
            spoken_text(self.conn), ["已确认第一句。", VISION_INTERRUPTED_MESSAGE]
        )
        self.conn.dialogue.put.assert_called_once()

    async def test_http_stream_and_plain_mcp_receipt_do_not_repeat_speech(self):
        async def stream(*args, **kwargs):
            yield "这是一件展品。"

        async def rpc(*args, **kwargs):
            request_id = kwargs["metadata"]["vision_request_id"]
            response = await self.handler.handle_post(self.request(request_id=request_id))
            return camera_result(json.loads(response.text)["response"])

        provider = SimpleNamespace(supports_streaming=True, response_stream=stream)
        with patch("core.api.vision_handler.create_instance", return_value=provider), patch(
            "core.providers.tools.device_mcp.mcp_executor.call_mcp_tool", new=rpc
        ):
            result = await DeviceMCPExecutor(self.conn).execute(
                self.conn, "self_camera_take_photo", {"question": "描述图片"}
            )
        self.assertEqual(result.action, Action.NONE)
        self.assertEqual(spoken_text(self.conn), ["这是一件展品。"])
        self.conn.dialogue.put.assert_called_once()


if __name__ == "__main__":
    unittest.main()
