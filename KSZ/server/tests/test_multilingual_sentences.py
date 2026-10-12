import queue
import threading
from types import SimpleNamespace

import pytest

from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO
from core.providers.tts.mimo import TTSProvider
from core.utils.vision_stream import SentenceBuffer


SENTENCES = {
    "zh-CN": ["这是一件陶器。", "表面有蓝色花纹。", "它被放在展柜里。"],
    "en": ["This is a ceramic vessel.", "It has blue decoration.", "It is displayed in a case."],
    "ja": ["これは陶器です。", "青い模様があります。", "展示ケースに入っています。"],
    "ko": ["이것은 도자기입니다.", "파란 무늬가 있습니다.", "전시함 안에 있습니다."],
}


@pytest.mark.parametrize("language", SENTENCES)
@pytest.mark.parametrize("chunk_size", [1, 4, 1000])
def test_streaming_and_batched_sentences_are_neither_merged_nor_lost(language, chunk_size):
    expected = SENTENCES[language]
    text = " ".join(expected)
    buffer = SentenceBuffer(language=language)
    actual = []
    for start in range(0, len(text), chunk_size):
        actual.extend(buffer.feed(text[start:start + chunk_size]))
    assert len(actual) >= 2
    actual.extend(buffer.feed("", final=True))
    assert actual == expected
    assert buffer.feed("", final=True) == []


@pytest.mark.parametrize("language,text,expected", [
    ("en", "Dr. Smith saw item No. 3. It is 3.14 m long. Visit museum.example.org.",
     ["Dr. Smith saw item No. 3.", "It is 3.14 m long.", "Visit museum.example.org."]),
    ("ko", "길이는 3.14 m입니다. 다음 전시품입니다.", ["길이는 3.14 m입니다.", "다음 전시품입니다."]),
    ("ja", "長さは１．５メートルです｡次の展示です！", ["長さは１．５メートルです｡", "次の展示です！"]),
    ("zh-CN", "版本是1.2.3。网址是https://museum.example.org。", ["版本是1.2.3。", "网址是https://museum.example.org。"]),
    ("en", "It came from the U.S.A. last year. It arrived at 9 a.m. today.",
     ["It came from the U.S.A. last year.", "It arrived at 9 a.m. today."]),
    ("en", "He said \"Look.\" Then stopped... Another label!", ["He said \"Look.\"", "Then stopped...", "Another label!"]),
])
def test_decimal_abbreviation_url_quotes_and_ellipsis_across_every_two_part_split(language, text, expected):
    for position in range(len(text) + 1):
        buffer = SentenceBuffer(language)
        parts = buffer.feed(text[:position]) + buffer.feed(text[position:]) + buffer.feed("", final=True)
        assert parts == expected, position


def test_final_tail_and_newlines_do_not_repeat_or_emit_empty_sentences():
    buffer = SentenceBuffer("ko")
    assert buffer.feed("\n\n첫 번째 줄\n\n마지막 설명") == ["첫 번째 줄"]
    assert buffer.feed("", final=True) == ["마지막 설명"]
    assert buffer.feed("", final=True) == []


@pytest.mark.parametrize("language,expected", [
    ("ja", ["申し訳ありませんが、", "現在撮影されている画像には人物やオフィス環境が含まれており、",
            "博物館の収蔵品である可能性を確認することができません。"]),
    ("ja", ["正確な案内のため、", "展示されている具体的な作品（絵画、工芸品、遺跡など）を正面から撮影し直していただけますでしょうか？"]),
    ("zh-CN", ["画面里可以看到一张办公桌，", "桌上放着一台电脑和几本文具。"]),
    ("en", ["The image shows a desk with a laptop on it,", " and there is a chair beside the desk."]),
    ("ko", ["사진에는 컴퓨터가 놓인 사무실 책상이 보이고,", " 옆에는 의자가 있습니다."]),
])
@pytest.mark.parametrize("chunk_size", [1, 4, 1000])
def test_long_clauses_stream_before_the_final_sentence_mark(language, expected, chunk_size):
    text = "".join(expected)
    buffer = SentenceBuffer(language)
    parts = []
    for start in range(0, len(text), chunk_size):
        parts.extend(buffer.feed(text[start:start + chunk_size]))
    parts.extend(buffer.feed("", final=True))
    assert parts == [part.strip() for part in expected]


@pytest.mark.parametrize("language,limit,word", [("en", 120, "exhibit"), ("ko", 60, "전시품")])
def test_unpunctuated_long_phrases_split_at_spaces_without_cutting_words(language, limit, word):
    text = " ".join([word] * 70) + "."
    buffer = SentenceBuffer(language)
    parts = []
    for char in text:
        parts.extend(buffer.feed(char))
    parts.extend(buffer.feed("", final=True))
    assert " ".join(parts) == text
    assert max(map(len, parts)) <= limit + len(word)


@pytest.mark.parametrize("language,text", [
    ("ja", "こちらの展示作品（絵画、彫刻、工芸品）は３，０００円です。"),
    ("en", "The exact measured volume is 1,234.56 cubic metres."),
])
def test_clause_splitting_preserves_parenthetical_lists_and_digit_groups(language, text):
    for position in range(len(text) + 1):
        buffer = SentenceBuffer(language)
        assert buffer.feed(text[:position]) + buffer.feed(text[position:]) + buffer.feed("", final=True) == [text]


class ProbeMimo(TTSProvider):
    def to_tts_stream(self, text, **kwargs):
        self.spoken.append(text)
        if self.abort_after_first:
            self.conn.client_abort = True
        if self.disconnect_after_first:
            self.conn.stop_event.set()


def tts_connection(language):
    conn = SimpleNamespace(sentence_id="turn", client_abort=False, stop_event=threading.Event(),
                           device_attributes={"language": language})

    class DrainQueue(queue.Queue):
        def get(self, *args, **kwargs):
            try:
                return super().get(block=False)
            except queue.Empty:
                conn.stop_event.set()
                raise

    conn.tts = ProbeMimo({"api_key": "synthetic-fixture"}, True)
    conn.tts.conn = conn
    conn.tts.spoken = []
    conn.tts.abort_after_first = False
    conn.tts.disconnect_after_first = False
    conn.tts.tts_text_queue = DrainQueue()
    send(conn, SentenceType.FIRST, ContentType.ACTION)
    return conn


def send(conn, sentence_type, content_type, text=None):
    conn.stop_event.clear()
    conn.tts.tts_text_queue.put(TTSMessageDTO(conn.sentence_id, sentence_type, content_type, content_detail=text))
    conn.tts.tts_text_priority_thread()
    conn.stop_event.clear()


@pytest.mark.parametrize("language", SENTENCES)
def test_real_mimo_queue_synthesizes_each_sentence_before_turn_end(language):
    conn = tts_connection(language)
    text = " ".join(SENTENCES[language]) + " "
    send(conn, SentenceType.MIDDLE, ContentType.TEXT, text)
    assert len(conn.tts.spoken) == 3
    send(conn, SentenceType.LAST, ContentType.ACTION)
    assert len(conn.tts.spoken) == 3


def test_language_is_frozen_until_next_turn_and_old_messages_are_rejected():
    conn = tts_connection("ja")
    conn.device_attributes = {"language": "en"}
    send(conn, SentenceType.MIDDLE, ContentType.TEXT, "赤、青。")
    assert conn.tts.spoken == ["赤", "青"]
    assert conn.tts.ksz_response_language == "ja"
    conn.sentence_id = "new-turn"
    send(conn, SentenceType.FIRST, ContentType.ACTION)
    assert conn.tts.ksz_response_language == "en"
    conn.tts.tts_text_queue.put(TTSMessageDTO("turn", SentenceType.MIDDLE, ContentType.TEXT,
                                           content_detail="Old response. "))
    send(conn, SentenceType.MIDDLE, ContentType.TEXT, "赤、青。")
    assert conn.tts.spoken == ["赤", "青", "赤、青"]


@pytest.mark.parametrize("reason", ["abort", "disconnect"])
def test_cancellation_during_synthesis_drops_remaining_sentences(reason):
    conn = tts_connection("en")
    setattr(conn.tts, reason + "_after_first", True)
    send(conn, SentenceType.MIDDLE, ContentType.TEXT, " ".join(SENTENCES["en"]) + " ")
    assert len(conn.tts.spoken) == 1


def test_punctuation_only_chunks_do_not_start_synthesis():
    conn = tts_connection("en")
    send(conn, SentenceType.MIDDLE, ContentType.TEXT, "?!；。\n")
    send(conn, SentenceType.LAST, ContentType.ACTION)
    assert conn.tts.spoken == []


def test_emotion_flush_does_not_skip_text_arriving_after_partial_synthesis():
    conn = tts_connection("en")
    send(conn, SentenceType.MIDDLE, ContentType.TEXT, "The first sentence. A partial phrase")
    send(conn, SentenceType.MIDDLE, ContentType.EMOTION)
    send(conn, SentenceType.MIDDLE, ContentType.TEXT, " Another sentence. ")
    send(conn, SentenceType.LAST, ContentType.ACTION)
    assert conn.tts.spoken == ["The first sentence", "A partial phrase", "Another sentence"]
