import queue
import re
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO
from core.providers.tts.mimo import TTSProvider
from ksz_guide.announcement import guide_announcement, start_guide_announcement
from ksz_guide.settings import GuideSettings
from test_guide import beacon


class ProbeMimo(TTSProvider):
    def to_tts_stream(self, text, **kwargs):
        self.synthesized.append((text, kwargs.get("emotion_context")))


@pytest.fixture
def conn():
    conn = SimpleNamespace(
        sentence_id="turn", session_id="session", client_abort=False,
        stop_event=threading.Event(), logger=Mock(), dialogue=Mock(), tool_feedback=Mock(),
        websocket=SimpleNamespace(send=AsyncMock()), clear_queues=Mock(), clearSpeakStatus=Mock(),
    )

    class DrainQueue(queue.Queue):
        def get(self, *args, **kwargs):
            try:
                return super().get(block=False)
            except queue.Empty:
                conn.stop_event.set()
                raise

    conn.tts = ProbeMimo({"api_key": "synthetic-fixture"}, True)
    conn.tts.conn = conn
    conn.tts.synthesized = []
    conn.tts.tts_text_queue = DrainQueue()
    conn.tts.tts_text_queue.put(TTSMessageDTO(conn.sentence_id, SentenceType.FIRST, ContentType.ACTION))
    drain(conn)
    return conn


def drain(conn):
    conn.stop_event.clear()
    conn.tts.tts_text_priority_thread()
    conn.stop_event.clear()


def test_beacon_location_sentence_is_synthesized_before_model_or_last(conn):
    with guide_announcement(conn, beacon(), GuideSettings(), lambda: True) as turn:
        assert start_guide_announcement(conn, conn.sentence_id)
        drain(conn)
        synthesized = "".join(text for text, _ in conn.tts.synthesized)
        assert re.sub(r"\W", "", synthesized) == re.sub(r"\W", "", turn.prefix)
        assert conn.tts.tts_text_buff == []
