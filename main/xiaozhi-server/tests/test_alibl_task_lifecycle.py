import asyncio
import json
import queue
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from websockets.protocol import State

from core.providers.tts.alibl_stream import TTSProvider
from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO


class FakeEncoder:
    def __init__(self):
        self.pending = b""

    def reset_state(self):
        self.pending = b""

    def encode_pcm_to_opus_stream(self, audio, end_of_stream, callback):
        self.pending += audio
        if end_of_stream and self.pending:
            callback(self.pending)
            self.pending = b""


class FakeWebSocket:
    def __init__(self):
        self.state = State.OPEN
        self.incoming = asyncio.Queue()
        self.sent = []
        self.auto_start = True
        self.auto_finish = True
        self.close_after_finish = False
        self.legacy_subtitles = False
        self.close_gate = None
        self.fail_send = None

    def event(self, name, task_id, **header):
        self.incoming.put_nowait(json.dumps({
            "header": {"event": name, "task_id": task_id, **header},
        }))

    async def send(self, raw):
        message = json.loads(raw)
        action = message["header"]["action"]
        if action == self.fail_send:
            raise RuntimeError("connection closed 1011")
        self.sent.append(message)
        task_id = message["header"]["task_id"]
        if action == "run-task" and self.auto_start:
            self.event("task-started", task_id)
        elif action == "continue-task":
            text = message["payload"]["input"]["text"]
            output = {} if self.legacy_subtitles else {
                "type": "sentence-begin", "original_text": text,
            }
            self.incoming.put_nowait(json.dumps({
                "header": {"event": "result-generated", "task_id": task_id},
                "payload": {"output": output},
            }))
            self.incoming.put_nowait(text.encode())
        elif action == "finish-task" and self.auto_finish:
            self.event("task-finished", task_id)
            if self.close_after_finish:
                self.incoming.put_nowait(RuntimeError("connection closed normally"))

    async def recv(self):
        message = await self.incoming.get()
        if isinstance(message, Exception):
            raise message
        return message

    async def close(self):
        self.state = State.CLOSING
        if self.close_gate is not None:
            await self.close_gate.wait()
        self.state = State.CLOSED


class AliBLTaskLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.provider = TTSProvider({"api_key": "test-only", "tts_timeout": 0.2}, True)
        self.provider.conn = SimpleNamespace(
            sentence_id="turn-1", sample_rate=24000,
            client_abort=False, stop_event=threading.Event(),
        )
        self.provider.opus_encoder = FakeEncoder()
        self.sockets = []

        async def connect(*args, **kwargs):
            ws = FakeWebSocket()
            self.sockets.append(ws)
            return ws

        self.connect_patch = patch(
            "core.providers.tts.alibl_stream.websockets.connect", side_effect=connect
        )
        self.connect_mock = self.connect_patch.start()

    async def asyncTearDown(self):
        for ws in self.sockets:
            if ws.close_gate is not None:
                ws.close_gate.set()
        await self.provider.close()
        self.connect_patch.stop()
        await asyncio.sleep(0)

    async def message(self, stage, text=None, sentence_id=None):
        await self.provider._process_message(TTSMessageDTO(
            sentence_id=sentence_id or self.provider.conn.sentence_id,
            sentence_type=stage,
            content_type=ContentType.TEXT if text is not None else ContentType.ACTION,
            content_detail=text,
        ))

    def output(self):
        result = []
        while True:
            try:
                result.append(self.provider.tts_audio_queue.get_nowait())
            except queue.Empty:
                return result

    async def feedback(self, text="我看看。"):
        self.provider.tts_tool_feedback(
            self.provider.conn, text, self.provider.conn.sentence_id
        )
        await self.provider._process_message(self.provider.tts_text_queue.get_nowait())

    async def complete_sentence(self, text):
        self.provider.tts_complete_sentence(
            self.provider.conn, text, self.provider.conn.sentence_id
        )
        await self.provider._process_message(self.provider.tts_text_queue.get_nowait())

    async def test_first_stays_idle_until_nonempty_text(self):
        await self.message(SentenceType.FIRST)
        with patch("core.providers.tts.alibl_stream.time.time", return_value=100000):
            await self.message(SentenceType.MIDDLE, " ")
        self.connect_mock.assert_not_called()
        await self.message(SentenceType.MIDDLE, "识别结果。")
        self.assertEqual(self.connect_mock.call_count, 1)
        await self.message(SentenceType.LAST)

    async def test_feedback_finishes_before_long_wait_without_device_stop(self):
        await self.message(SentenceType.FIRST)
        await self.feedback()
        ws = self.sockets[0]
        self.assertEqual([m["header"]["action"] for m in ws.sent], [
            "run-task", "continue-task", "finish-task",
        ])
        hint_output = self.output()
        self.assertFalse(any(item[0] == SentenceType.LAST for item in hint_output))
        self.assertEqual(hint_output[1][1], "我看看。".encode())
        self.assertIsNone(self.provider._active_task)
        with patch("core.providers.tts.alibl_stream.time.time", return_value=self.provider.last_active_time + 24):
            await self.message(SentenceType.MIDDLE, "这是识别结果。")
        await self.message(SentenceType.LAST)
        await self.message(SentenceType.LAST)
        runs = [m for m in ws.sent if m["header"]["action"] == "run-task"]
        self.assertEqual(len(runs), 2)
        self.assertNotEqual(runs[0]["header"]["task_id"], runs[1]["header"]["task_id"])
        self.assertTrue(all(m["header"]["task_id"] != "turn-1" for m in runs))
        output = self.output()
        self.assertEqual(sum(item[0] == SentenceType.LAST for item in output), 1)
        self.assertTrue(all(item[3] == "turn-1" for item in hint_output + output))

    async def test_body_waits_for_feedback_task_finished(self):
        await self.message(SentenceType.FIRST)
        ws = await self.provider._ensure_connection()
        ws.auto_finish = False
        hint = asyncio.create_task(self.feedback())
        while not any(m["header"]["action"] == "finish-task" for m in ws.sent):
            await asyncio.sleep(0)
        body = asyncio.create_task(self.message(SentenceType.MIDDLE, "正文。"))
        await asyncio.sleep(0)
        self.assertEqual(sum(m["header"]["action"] == "run-task" for m in ws.sent), 1)
        ws.event("task-finished", ws.sent[0]["header"]["task_id"])
        await hint
        await body
        ws.auto_finish = True
        await self.message(SentenceType.LAST)
        self.assertEqual(sum(m["header"]["action"] == "run-task" for m in ws.sent), 2)

    async def test_complete_sentences_finish_tasks_during_vision_and_rpc_waits(self):
        await self.message(SentenceType.FIRST)
        await self.complete_sentence("第一句。")
        self.assertIsNone(self.provider._active_task)
        self.assertFalse(any(item[0] == SentenceType.LAST for item in self.output()))
        with patch("core.providers.tts.alibl_stream.time.time", return_value=self.provider.last_active_time + 30):
            await self.complete_sentence("第二句。")
        ws = self.sockets[0]
        self.assertIsNone(self.provider._active_task)
        self.assertFalse(any(item[0] == SentenceType.LAST for item in self.output()))
        with patch("core.providers.tts.alibl_stream.time.time", return_value=self.provider.last_active_time + 90):
            await self.message(SentenceType.LAST)
        await self.message(SentenceType.LAST)
        self.assertEqual([m["header"]["action"] for m in ws.sent], [
            "run-task", "continue-task", "finish-task",
            "run-task", "continue-task", "finish-task",
        ])
        self.assertNotEqual(ws.sent[0]["header"]["task_id"], ws.sent[3]["header"]["task_id"])
        self.assertEqual(self.output(), [(SentenceType.LAST, [], None, "turn-1")])

    async def test_late_task_failure_does_not_fail_next_complete_sentence(self):
        await self.message(SentenceType.FIRST)
        await self.complete_sentence("第一句。")
        ws = self.sockets[0]
        old_task_id = ws.sent[0]["header"]["task_id"]
        ws.event("task-failed", old_task_id, error_code="InvalidParameter", error_message="late old-task failure")
        await self.complete_sentence("第二句。")
        await self.message(SentenceType.LAST)
        self.assertFalse(self.provider._turn_failed)
        output = self.output()
        self.assertEqual([item[2] for item in output if item[0] == SentenceType.FIRST], ["第一句。", "第二句。"])
        self.assertTrue(all(item[3] == "turn-1" for item in output))

    async def test_failed_complete_sentence_does_not_restart_remaining_body(self):
        await self.message(SentenceType.FIRST)
        ws = await self.provider._ensure_connection()
        ws.fail_send = "continue-task"
        with self.assertRaisesRegex(RuntimeError, "1011"):
            await self.complete_sentence("第一句。")
        await self.complete_sentence("第二句。")
        await self.message(SentenceType.LAST)
        self.assertEqual(self.connect_mock.call_count, 1)
        self.assertEqual([item[0] for item in self.output()], [SentenceType.LAST])

    async def test_legacy_subtitles_use_individual_task_text(self):
        await self.message(SentenceType.FIRST)
        ws = await self.provider._ensure_connection()
        ws.legacy_subtitles = True
        self.provider.store_tts_text("turn-1", "不得放到提示语字幕的整轮正文")
        await self.feedback()
        await self.message(SentenceType.MIDDLE, "正文。")
        await self.message(SentenceType.LAST)
        subtitles = [item[2] for item in self.output() if item[0] == SentenceType.FIRST]
        self.assertEqual(subtitles, ["我看看。", "正文。"])

    async def test_empty_turn_stops_once_without_starting_vendor_task(self):
        await self.message(SentenceType.FIRST)
        await self.message(SentenceType.LAST)
        await self.message(SentenceType.LAST)
        self.connect_mock.assert_not_called()
        self.assertEqual([item[0] for item in self.output()], [SentenceType.LAST])

    async def test_send_failure_does_not_replay_body_or_leave_turn_open(self):
        await self.message(SentenceType.FIRST)
        ws = await self.provider._ensure_connection()
        ws.fail_send = "continue-task"
        with self.assertRaisesRegex(RuntimeError, "1011"):
            await self.message(SentenceType.MIDDLE, "正文。")
        await self.message(SentenceType.MIDDLE, "后续片段。")
        await self.message(SentenceType.LAST)
        self.assertEqual(self.connect_mock.call_count, 1)
        self.assertIsNone(self.provider.ws)
        self.assertEqual([item[0] for item in self.output()], [SentenceType.LAST])

    async def test_failed_feedback_does_not_prevent_body_new_task(self):
        await self.message(SentenceType.FIRST)
        ws = await self.provider._ensure_connection()
        ws.fail_send = "continue-task"
        with self.assertRaisesRegex(RuntimeError, "1011"):
            await self.feedback()
        await self.message(SentenceType.MIDDLE, "正文。")
        await self.message(SentenceType.LAST)
        self.assertEqual(self.connect_mock.call_count, 2)
        self.assertIn("正文。", [item[2] for item in self.output()])

    async def test_task_failure_invalidates_before_old_socket_close_finishes(self):
        await self.message(SentenceType.FIRST)
        self.provider._feedback_active = True
        task = await self.provider._begin_task()
        self.provider._feedback_active = False
        old_monitor = self.provider._monitor_task
        task.ws.close_gate = asyncio.Event()
        task.ws.event("task-failed", task.task_id, error_code="InvalidParameter", error_message="request timeout after 23 seconds")
        await task.finished.wait()
        self.assertIsNone(self.provider.ws)
        self.assertIsNone(self.provider._active_task)
        await self.message(SentenceType.MIDDLE, "正文。")
        new_ws = self.provider.ws
        task.ws.close_gate.set()
        await old_monitor
        self.assertIs(self.provider.ws, new_ws)
        self.assertEqual(new_ws.state, State.OPEN)
        await self.message(SentenceType.LAST)

    async def test_interrupt_filters_old_subtitles_audio_and_completion(self):
        await self.message(SentenceType.FIRST)
        task = await self.provider._begin_task()
        self.provider.conn.client_abort = True
        task.ws.incoming.put_nowait(json.dumps({
            "header": {"event": "result-generated", "task_id": task.task_id},
            "payload": {"output": {"type": "sentence-begin", "original_text": "旧内容。"}},
        }))
        task.ws.incoming.put_nowait(b"old audio")
        task.ws.event("task-finished", task.task_id)
        await task.finished.wait()
        self.assertEqual(self.output(), [])
        self.provider.conn.client_abort = False
        self.provider.conn.sentence_id = "turn-2"
        await self.message(SentenceType.FIRST)
        await self.message(SentenceType.MIDDLE, "新内容。")
        await self.message(SentenceType.LAST)
        output = self.output()
        self.assertTrue(all(item[3] == "turn-2" for item in output))
        self.assertEqual([item[2] for item in output if item[0] == SentenceType.FIRST], ["新内容。"])

    async def test_correct_word_prefix_is_flushed_at_feedback_boundary(self):
        self.provider.correct_words = {"古代": "古戴"}
        self.provider._words_by_first_char = {"古": ["古代"]}
        await self.message(SentenceType.FIRST)
        await self.feedback("古")
        await self.message(SentenceType.MIDDLE, "代")
        await self.message(SentenceType.LAST)
        texts = [m["payload"]["input"]["text"] for ws in self.sockets for m in ws.sent if m["header"]["action"] == "continue-task"]
        self.assertEqual(texts, ["古", "代"])

    async def test_space_between_english_fragments_is_preserved(self):
        await self.message(SentenceType.FIRST)
        for text in ("hello", " ", "world"):
            await self.message(SentenceType.MIDDLE, text)
        await self.message(SentenceType.LAST)
        texts = [m["payload"]["input"]["text"] for m in self.sockets[0].sent if m["header"]["action"] == "continue-task"]
        self.assertEqual("".join(texts), "hello world")

    async def test_closed_cached_socket_is_replaced(self):
        await self.message(SentenceType.FIRST)
        await self.feedback()
        old_ws = self.sockets[0]
        old_ws.state = State.CLOSED
        await self.message(SentenceType.MIDDLE, "正文。")
        await self.message(SentenceType.LAST)
        self.assertEqual(self.connect_mock.call_count, 2)
        self.assertNotIn("正文。", [m.get("payload", {}).get("input", {}).get("text") for m in old_ws.sent])

    async def test_finish_timeout_still_stops_turn_once(self):
        self.provider.tts_timeout = 0.01
        await self.message(SentenceType.FIRST)
        await self.message(SentenceType.MIDDLE, "正文。")
        self.provider.ws.auto_finish = False
        with self.assertRaises(asyncio.TimeoutError):
            await self.message(SentenceType.LAST)
        await self.message(SentenceType.LAST)
        self.assertIsNone(self.provider.ws)
        self.assertEqual(sum(item[0] == SentenceType.LAST for item in self.output()), 1)

    async def test_socket_close_after_finished_does_not_fail_completed_task(self):
        await self.message(SentenceType.FIRST)
        await self.message(SentenceType.MIDDLE, "正文。")
        self.provider.ws.close_after_finish = True
        await self.message(SentenceType.LAST)
        self.assertFalse(self.provider._turn_failed)
        self.assertEqual(sum(item[0] == SentenceType.LAST for item in self.output()), 1)

    async def test_cancelled_start_does_not_block_new_turn_on_old_close(self):
        await self.message(SentenceType.FIRST)
        old_ws = await self.provider._ensure_connection()
        old_ws.auto_start = False
        old_ws.close_gate = asyncio.Event()
        pending = asyncio.create_task(self.message(SentenceType.MIDDLE, "旧内容。"))
        while not old_ws.sent:
            await asyncio.sleep(0)
        self.provider.conn.sentence_id = "turn-2"
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        await self.message(SentenceType.FIRST)
        await self.message(SentenceType.MIDDLE, "新内容。")
        await self.message(SentenceType.LAST)
        self.assertFalse(old_ws.close_gate.is_set())
        self.assertEqual([item[2] for item in self.output() if item[0] == SentenceType.FIRST], ["新内容。"])

    async def test_worker_cancels_old_wait_when_new_turn_arrives(self):
        self.provider.tts_timeout = 10
        self.provider.conn.loop = asyncio.get_running_loop()
        await self.message(SentenceType.FIRST)
        old_ws = await self.provider._ensure_connection()
        old_ws.auto_start = False
        old_ws.close_gate = asyncio.Event()
        worker = threading.Thread(target=self.provider.tts_text_priority_thread, daemon=True)
        worker.start()

        def enqueue(stage, text=None):
            self.provider.tts_text_queue.put(TTSMessageDTO(
                self.provider.conn.sentence_id, stage,
                ContentType.TEXT if text else ContentType.ACTION,
                content_detail=text,
            ))

        async def await_condition(predicate):
            while not predicate():
                await asyncio.sleep(0.001)

        try:
            enqueue(SentenceType.MIDDLE, "旧内容。")
            await asyncio.wait_for(await_condition(lambda: bool(old_ws.sent)), 1)
            self.provider.conn.sentence_id = "turn-2"
            enqueue(SentenceType.FIRST)
            enqueue(SentenceType.MIDDLE, "新内容。")
            enqueue(SentenceType.LAST)
            await asyncio.wait_for(await_condition(lambda: self.provider._turn_finished), 2)
            output = self.output()
            self.assertEqual([item[2] for item in output if item[0] == SentenceType.FIRST], ["新内容。"])
            self.assertFalse(old_ws.close_gate.is_set())
        finally:
            self.provider.conn.stop_event.set()
            await asyncio.to_thread(worker.join, 2)

    async def test_delayed_abort_cleanup_does_not_close_new_turn(self):
        await self.message(SentenceType.FIRST)
        old_ws = await self.provider._ensure_connection()
        old_sentence_id = self.provider.current_sentence_id
        self.provider.conn.client_abort = True
        cleanup = self.provider._close_if_aborted(old_ws, old_sentence_id)
        self.provider.conn.client_abort = False
        self.provider.conn.sentence_id = "turn-2"
        await self.message(SentenceType.FIRST)
        await self.message(SentenceType.MIDDLE, "新内容。")
        new_ws = self.provider.ws
        await cleanup
        self.assertIs(self.provider.ws, new_ws)
        self.assertFalse(self.provider._turn_finished)
        self.assertEqual(new_ws.state, State.OPEN)
        await self.message(SentenceType.LAST)

    async def test_delayed_abort_cleanup_does_not_close_replaced_socket(self):
        await self.message(SentenceType.FIRST)
        old_ws = await self.provider._ensure_connection()
        await self.provider._disconnect(old_ws)
        new_ws = await self.provider._ensure_connection()
        self.provider.conn.client_abort = True
        await self.provider._close_if_aborted(old_ws, "turn-1")
        self.assertIs(self.provider.ws, new_ws)
        self.assertFalse(self.provider._turn_finished)
        self.assertEqual(new_ws.state, State.OPEN)


if __name__ == "__main__":
    unittest.main()
