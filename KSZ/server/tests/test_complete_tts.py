import asyncio
import queue
import re
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO
from core.providers.tts.mimo import TTSProvider
from core.utils.vision_stream import get_vision_sessions
from ksz_guide.tts import tts_complete_sentence
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


@pytest.mark.parametrize("phrase", ["Let me take a look.", "잠시만 기다려 주세요.", "確認しますね。", "我来看看。"])
def test_complete_feedback_is_synthesized_before_answer_or_last(conn, phrase):
    tts_complete_sentence(conn, phrase, conn.sentence_id, feedback=True)
    drain(conn)
    assert len(conn.tts.synthesized) == 1
    assert conn.tts.synthesized[0][0].strip()
    assert conn.tts.tts_text_buff == []
    assert conn.tts.tts_audio_queue.empty()


def test_beacon_location_sentence_is_synthesized_before_model_or_last(conn):
    with guide_announcement(conn, beacon(), GuideSettings(), lambda: True) as turn:
        assert start_guide_announcement(conn, conn.sentence_id)
        drain(conn)
        synthesized = "".join(text for text, _ in conn.tts.synthesized)
        assert re.sub(r"\W", "", synthesized) == re.sub(r"\W", "", turn.prefix)
        assert conn.tts.tts_text_buff == []


@pytest.mark.parametrize("mode", ["push", "mcp"])
def test_english_camera_feedback_and_answer_are_separate_without_waiting_for_last(conn, mode):
    sessions = get_vision_sessions(conn)
    if mode == "push":
        request = asyncio.run(sessions.begin_push("photo"))
    else:
        request = sessions.begin_mcp(conn.sentence_id, time.monotonic() + 120)
    tts_complete_sentence(conn, "Let me take a look.", conn.sentence_id, feedback=True)
    drain(conn)
    assert len(conn.tts.synthesized) == 1
    for text in ["A red square is visible.", "It is next to a blue circle."]:
        assert sessions.emit(request, text)
        drain(conn)
    assert len(conn.tts.synthesized) == 3
    assert "Let me" in conn.tts.synthesized[0][0]
    assert "red square" in conn.tts.synthesized[1][0]
    assert "blue circle" in conn.tts.synthesized[2][0]
    assert all("Let me" not in text for text, _ in conn.tts.synthesized[1:])
    sessions.finish(request)
    drain(conn)
    assert len(conn.tts.synthesized) == 3


def test_complete_sentence_flushes_existing_partial_without_losing_following_text(conn):
    conn.tts.tts_text_queue.put(TTSMessageDTO(conn.sentence_id, SentenceType.MIDDLE, ContentType.TEXT,
                                           content_detail="Earlier partial"))
    tts_complete_sentence(conn, "The complete answer.", conn.sentence_id)
    drain(conn)
    tts_complete_sentence(conn, "Next.", conn.sentence_id)
    drain(conn)
    assert [text for text, _ in conn.tts.synthesized] == ["Earlier partial", "The complete answer", "Next"]


@pytest.mark.parametrize("cancelled", ["abort", "new_turn"])
def test_stale_complete_sentences_are_not_synthesized(conn, cancelled):
    tts_complete_sentence(conn, "Let me take a look.", conn.sentence_id, feedback=True)
    if cancelled == "abort":
        conn.client_abort = True
    else:
        conn.sentence_id = "replacement"
    drain(conn)
    assert conn.tts.synthesized == []


def test_complete_sentence_retains_emotion_context(conn):
    context = object()
    conn.tts.current_emotion_context = context
    tts_complete_sentence(conn, "A red square.", conn.sentence_id)
    drain(conn)
    assert conn.tts.synthesized[0][1] is context


def test_ordinary_chat_keeps_its_existing_stream_segmentation(conn):
    conn.tts.tts_one_sentence(conn, ContentType.TEXT, content_detail="Pending words.", sentence_id=conn.sentence_id)
    drain(conn)
    assert conn.tts.synthesized == []
    conn.tts.tts_text_queue.put(TTSMessageDTO(conn.sentence_id, SentenceType.LAST, ContentType.ACTION))
    drain(conn)
    assert len(conn.tts.synthesized) == 1


def test_provider_native_complete_sentence_methods_are_preserved():
    tts = SimpleNamespace(tts_tool_feedback=Mock(), tts_complete_sentence=Mock())
    conn = SimpleNamespace(tts=tts)
    tts_complete_sentence(conn, "Wait.", "turn", feedback=True)
    tts_complete_sentence(conn, "Answer.", "turn")
    tts.tts_tool_feedback.assert_called_once_with(conn, "Wait.", "turn")
    tts.tts_complete_sentence.assert_called_once_with(conn, "Answer.", "turn")
