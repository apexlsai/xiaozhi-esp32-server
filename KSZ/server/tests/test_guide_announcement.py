import asyncio
import json
import queue
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.connection import ConnectionHandler
from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO
from core.utils.dialogue import Dialogue
from ksz_guide.announcement import OpeningFilter, guide_announcement
from ksz_guide.settings import GuideSettings
from plugins_func.register import Action, ActionResponse
from test_guide import Harness, beacon


class RecordingTTS:
    def __init__(self):
        self.tts_text_queue = queue.Queue()
        self.texts = {}
        self.complete = []

    def store_tts_text(self, sentence_id, text):
        self.texts[sentence_id] = text

    def tts_complete_sentence(self, conn, text, sentence_id):
        self.complete.append(text)
        self.tts_text_queue.put(TTSMessageDTO(sentence_id, SentenceType.MIDDLE, ContentType.TEXT, text))


@pytest.fixture
def conn():
    h = Harness()
    instance = ConnectionHandler.__new__(ConnectionHandler)
    instance.__dict__.update(vars(h.conn()))
    instance.sentence_id = "before"
    instance.client_abort = False
    instance.client_is_speaking = True
    instance.features = {"emoji": False}
    instance.config = {}
    instance.logger = Mock()
    instance.memory = None
    instance.intent_type = "none"
    instance.current_speaker = None
    instance.system_introduced_speakers = set()
    instance.device_attributes = {"language": "zh-CN"}
    instance.dialogue = Dialogue()
    instance.dialogue.update_system_message("Keep the configured museum role.")
    instance._ensure_device_attributes_fresh = Mock()
    instance._ensure_beacon_location_fresh = Mock()
    instance._prompt_beacon_location = ""
    instance._init_prompt_enhancement = Mock()
    instance.loop = None
    instance.tool_feedback = Mock()
    instance.tts = RecordingTTS()
    instance.llm = SimpleNamespace(response=Mock(return_value=iter(())), response_with_functions=Mock())
    instance.func_handler = SimpleNamespace(get_functions=Mock(return_value=[]))
    return instance


def narrate(conn, *, settings=None, item=None, valid=lambda: True):
    with guide_announcement(conn, item or beacon(), settings or GuideSettings(), valid) as turn:
        conn.chat("[位置变化] 请直接介绍这里。")
    return turn


def messages(conn):
    return list(conn.tts.tts_text_queue.queue)


def spoken(conn):
    return "".join(message.content_detail for message in messages(conn) if message.content_type == ContentType.TEXT)


def assistant_text(conn):
    return [message.content for message in conn.dialogue.dialogue if message.role == "assistant" and message.content]


@pytest.mark.parametrize("size", [1, 5, 1000])
@pytest.mark.parametrize("location,text,expected", [
    ("出口", "您好，我是小硕，您现在位于出口。这里展示城市规划。", "这里展示城市规划。"),
    ("Hall A", "Hello, I'm your guide. You are now at Hall A. This exhibit shows urban planning.", "This exhibit shows urban planning."),
    ("Hall A", "Hello, I'm Dr. Smith. This exhibit shows urban planning.", "This exhibit shows urban planning."),
    ("展望室", "こんにちは。私はガイドです。現在、展望室にいます。都市の計画を紹介しています。", "都市の計画を紹介しています。"),
    ("전시실", "안녕하세요. 저는 안내원입니다. 현재 전시실에 계십니다. 도시 계획을 소개합니다.", "도시 계획을 소개합니다."),
    ("出口", "你好，我是小硕，这里展示城市规划", "这里展示城市规划"),
    ("出口", "这里展示题为“你好”的作品。我是画中人物的旁白。", "这里展示题为“你好”的作品。我是画中人物的旁白。"),
    ("出口", "你好奇这件展品的尺寸。它高3.5米。", "你好奇这件展品的尺寸。它高3.5米。"),
    ("出口", "您好，我是您的导览员", ""),
    ("出口", "こんにちは", ""),
])
def test_opening_filter_handles_chunk_boundaries_without_changing_body(size, location, text, expected):
    opening = OpeningFilter(location, f"您现在位于{location}。")
    result = "".join(opening.feed(text[index:index + size]) for index in range(0, len(text), size))
    assert result + opening.feed("", final=True) == expected


@pytest.mark.parametrize("template", ["没有占位符", "{place}", "{location}{location}", "{location!r}",
                                      "{location:>10}", "{location.upper}", "{location", "{location} }"])
def test_invalid_location_template_fails_at_startup(template):
    with pytest.raises(ValueError, match="KSZ_GUIDE_LOCATION_TEMPLATE_ZH_CN"):
        GuideSettings.from_env({"KSZ_GUIDE_LOCATION_TEMPLATE_ZH_CN": template})


@pytest.mark.parametrize("language,expected", [
    ("zh-CN", "您现在位于1F，展区1，展柜旁。"),
    ("en", "You are now at 1F，展区1，展柜旁."),
    ("ja", "現在、1F，展区1，展柜旁にいます。"),
    ("ko", "현재 1F，展区1，展柜旁에 계십니다."),
    ("zh-CN-yue", "您现在位于1F，展区1，展柜旁。"),
])
def test_fixed_opening_uses_bound_language_before_model_is_called(conn, language, expected):
    conn.device_attributes["language"] = language

    def response(*args, **kwargs):
        assert conn.tts.complete == [expected]
        assert [item.sentence_type for item in messages(conn)] == [SentenceType.FIRST, SentenceType.MIDDLE]
        return iter(())

    conn.llm.response.side_effect = response
    narrate(conn)
    assert spoken(conn) == expected
    assert assistant_text(conn) == [expected]
    assert conn.tts.texts[conn.sentence_id] == expected
    assert messages(conn)[-1].sentence_type == SentenceType.LAST


@pytest.mark.parametrize("mode", ["text", "direct_answer"])
def test_real_chat_filters_stream_and_saves_exact_spoken_reply(conn, mode):
    text = "🙂您好，我是小硕，您现在位于1F，展区1，展柜旁。这里展示城市规划"
    if mode == "text":
        conn.llm.response.return_value = iter(text)
        request = conn.llm.response
    else:
        conn.intent_type = "function_call"
        arguments = json.dumps({"response": text}, ensure_ascii=False)
        conn.llm.response_with_functions.return_value = iter([
            (None, [SimpleNamespace(index=0, id="answer" if index == 0 else None,
                                   function=SimpleNamespace(name="direct_answer" if index == 0 else None, arguments=char))])
            for index, char in enumerate(arguments)
        ])
        request = conn.llm.response_with_functions
    turn = narrate(conn)
    expected = turn.prefix + "这里展示城市规划"
    assert spoken(conn) == expected
    assert assistant_text(conn) == [expected]
    assert conn.tts.texts[conn.sentence_id] == expected
    assert {item.sentence_id for item in messages(conn)} == {conn.sentence_id}
    assert sum(item.sentence_type == SentenceType.FIRST for item in messages(conn)) == 1
    assert sum(item.sentence_type == SentenceType.LAST for item in messages(conn)) == 1
    system = request.call_args.args[1][0]["content"]
    assert "Do not greet" in system and turn.prefix in system
    assert conn.dialogue.dialogue[0].content == "Keep the configured museum role."


@pytest.mark.parametrize("stage", ["request", "stream"])
def test_model_failure_keeps_location_sentence_and_finishes(conn, stage):
    if stage == "request":
        conn.llm.response.side_effect = RuntimeError("unavailable")
    else:
        def broken():
            yield "您好，我是"
            raise RuntimeError("stream lost")
        conn.llm.response.return_value = broken()
    turn = narrate(conn)
    assert spoken(conn) == turn.prefix
    assert assistant_text(conn) == [turn.prefix]
    assert messages(conn)[-1].sentence_type == SentenceType.LAST


@pytest.mark.parametrize("change", ["abort", "new_beacon", "disconnect", "position_expired"])
def test_late_model_output_is_discarded_when_guide_loses_ownership(conn, change):
    valid = True

    def response():
        nonlocal valid
        yield "您好，我是"
        if change == "abort":
            conn.client_abort = True
        elif change == "new_beacon":
            conn.sentence_id = "next-beacon"
        elif change == "disconnect":
            conn.websocket.closed = True
        else:
            valid = False
        yield "小硕。过期的地点介绍。"

    conn.llm.response.return_value = response()
    turn = narrate(conn, valid=lambda: valid)
    assert spoken(conn) == turn.prefix
    assert assistant_text(conn) == []
    if change != "position_expired":
        assert all(item.sentence_type != SentenceType.LAST for item in messages(conn))


def test_missing_location_never_reaches_model(conn):
    narrate(conn, item=beacon(floor=" ", area="", location_description=""))
    conn.llm.response.assert_not_called()
    assert spoken(conn) == ""


def test_custom_template_and_empty_fields_are_rendered_once(conn):
    settings = GuideSettings.from_env({"KSZ_GUIDE_LOCATION_TEMPLATE_ZH_CN": "当前位置：{location}。"})
    narrate(conn, settings=settings, item=beacon(floor="", area="展区", location_description=" "))
    assert spoken(conn) == "当前位置：展区。"
    assert GuideSettings.from_env({"KSZ_GUIDE_LOCATION_TEMPLATE_ZH_CN": ""}) == GuideSettings()


def test_following_user_chat_keeps_greetings_and_its_original_prompt(conn):
    narrate(conn)
    conn.tts.tts_text_queue = queue.Queue()
    conn.llm.response.return_value = iter(["你好，我是小硕。"])
    conn.chat("你好")
    assert spoken(conn) == "你好，我是小硕。"
    assert assistant_text(conn)[-1] == "你好，我是小硕。"
    assert "automatic beacon narration" not in conn.llm.response.call_args.args[1][0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [Action.REQLLM, Action.RESPONSE])
async def test_tool_result_uses_same_narration_without_repeating_prefix(conn, monkeypatch, action):
    conn.loop = asyncio.get_running_loop()
    conn.intent_type = "function_call"
    conn.func_handler.handle_llm_function_call = AsyncMock(return_value=ActionResponse(action, result="您好，我是小硕。这里展示城市规划。"))
    monkeypatch.setattr("core.connection.enqueue_tool_report", Mock())
    first = iter([(None, [SimpleNamespace(index=0, id="facts", function=SimpleNamespace(name="museum_facts", arguments="{}"))])])
    second = iter([("您好，我是小硕。这里展示城市规划。", None)])
    conn.llm.response_with_functions.side_effect = [first, second]
    turn = await asyncio.to_thread(narrate, conn)
    assert spoken(conn) == turn.prefix + "这里展示城市规划。"
    assert assistant_text(conn) == [spoken(conn)]
    assert sum(item.sentence_type == SentenceType.FIRST for item in messages(conn)) == 1
    assert sum(item.sentence_type == SentenceType.LAST for item in messages(conn)) == 1
    if action == Action.REQLLM:
        assert any(message.role == "tool" for message in conn.dialogue.dialogue)
        assert "Do not greet" in conn.llm.response_with_functions.call_args.args[1][0]["content"]
