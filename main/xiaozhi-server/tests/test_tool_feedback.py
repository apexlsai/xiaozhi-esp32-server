import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.providers.tts.dto.dto import ContentType
from core.utils.tool_feedback import ToolFeedbackScheduler


class FakeTimer:
    instances = []

    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.daemon = False
        self.cancelled = False
        self.started = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback()


class ToolFeedbackSchedulerTests(unittest.TestCase):
    def setUp(self):
        FakeTimer.instances.clear()
        self.tts = SimpleNamespace(tts_one_sentence=MagicMock())
        self.conn = SimpleNamespace(
            config={
                "tool_feedback": {
                    "enabled": True,
                    "delay_ms": 500,
                    "tools": {"self_camera_take_photo": "我看看。"},
                }
            },
            stop_event=threading.Event(),
            client_abort=False,
            sentence_id="sentence-1",
            tts=self.tts,
            logger=MagicMock(),
        )
        self.scheduler = ToolFeedbackScheduler(self.conn, timer_factory=FakeTimer)

    def test_slow_camera_tool_plays_feedback_once(self):
        key = self.scheduler.schedule(
            "self_camera_take_photo", "call-1", "sentence-1"
        )

        self.assertEqual(key, "call-1")
        self.assertTrue(FakeTimer.instances[0].started)
        self.assertEqual(FakeTimer.instances[0].delay, 0.5)

        FakeTimer.instances[0].fire()

        self.tts.tts_one_sentence.assert_called_once_with(
            self.conn,
            ContentType.TEXT,
            content_detail="我看看。",
            sentence_id="sentence-1",
        )

    def test_fast_tool_completion_cancels_feedback(self):
        key = self.scheduler.schedule(
            "self_camera_take_photo", "call-1", "sentence-1"
        )

        self.scheduler.cancel(key)
        FakeTimer.instances[0].fire()

        self.assertTrue(FakeTimer.instances[0].cancelled)
        self.tts.tts_one_sentence.assert_not_called()

    def test_old_or_aborted_session_does_not_play_feedback(self):
        for state in ("old", "aborted"):
            with self.subTest(state=state):
                FakeTimer.instances.clear()
                self.tts.tts_one_sentence.reset_mock()
                self.conn.sentence_id = "sentence-1"
                self.conn.client_abort = False
                self.scheduler.schedule(
                    "self_camera_take_photo", f"call-{state}", "sentence-1"
                )
                if state == "old":
                    self.conn.sentence_id = "sentence-2"
                else:
                    self.conn.client_abort = True

                FakeTimer.instances[0].fire()

                self.tts.tts_one_sentence.assert_not_called()

    def test_non_configured_tool_does_not_schedule_feedback(self):
        key = self.scheduler.schedule("play_music", "call-1", "sentence-1")

        self.assertIsNone(key)
        self.assertEqual(FakeTimer.instances, [])

    def test_duplicate_camera_calls_only_announce_once(self):
        self.scheduler.schedule("self_camera_take_photo", "call-1", "sentence-1")
        self.scheduler.schedule("self_camera_take_photo", "call-2", "sentence-1")

        FakeTimer.instances[0].fire()
        FakeTimer.instances[1].fire()

        self.tts.tts_one_sentence.assert_called_once()

    def test_cancel_all_prevents_pending_feedback(self):
        self.scheduler.schedule("self_camera_take_photo", "call-1", "sentence-1")

        self.scheduler.cancel_all()
        FakeTimer.instances[0].fire()

        self.assertTrue(FakeTimer.instances[0].cancelled)
        self.tts.tts_one_sentence.assert_not_called()


if __name__ == "__main__":
    unittest.main()
