import json
import re
from contextlib import contextmanager
from contextvars import ContextVar

from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO
from core.utils.dialogue import Message
from core.utils.tts_emotion import strip_emotion_markers

from .presence import connection_is_open
from .prompt import LANGUAGE_NAMES, response_language
from .tts import tts_complete_sentence


_current = ContextVar("ksz_guide_announcement", default=None)
_sentence_end = re.compile(r"[。！？!?；;\n]|(?<!\bDr)(?<!\bMr)(?<!\bMrs)(?<!\bProf)\.(?!\d)", re.I)
_greeting = re.compile(
    r"^(?:(?:(?:各位|尊敬的|亲爱的)(?:游客|观众|朋友|来宾)[，,\s]*)?(?:大家好|你们好|你好|您好)(?:啊|呀)?"
    r"|(?:hello(?:\s+everyone)?|hi(?:\s+there)?|good\s+(?:morning|afternoon|evening))\b"
    r"|(?:皆さん[、,\s]*)?(?:こんにちは|おはようございます|こんばんは)"
    r"|(?:여러분[ ,]*)?(?:안녕하세요|안녕하십니까))(?=[，,、。.!！?？\s]|$)[，,、。.!！?？\s]*", re.I,
)
_introduction = re.compile(
    r"^(?:我(?:是|叫)|(?:i\s+am|i['’]m|my\s+name\s+is)\s+"
    r"|私(?:は|の名前は)|(?:저는|제\s*이름은)\s*)[^，,、。!！?？;；\n]*[，,、。.!！?？;；\n\s]*", re.I,
)
_location_start = re.compile(
    r"^(?:您|你)(?:(?:现在|目前|此刻|已经|正)\s*)?(?:位于|在|来到了|来到|到了)"
    r"|^(?:you\s+are\s+(?:(?:now|currently)\s+)?(?:at|in)\b|welcome\s+to\b)"
    r"|^欢迎(?:您|你)?(?:来到|莅临)", re.I,
)
_filler = re.compile(r"^(?:好的|好啊|当然(?:可以)?|接下来(?:由我)?(?:为您|为你)?介绍一下|sure|certainly|of course)[，,、。.!！\s]*$", re.I)


def _normalized(text):
    return re.sub(r"[\W_]+", "", text).casefold()


class OpeningFilter:
    def __init__(self, location, prefix):
        self.location = _normalized(location)
        self.prefix = _normalized(prefix)
        self.buffer = ""
        self.opened = False
        self.discarding = False

    def _clean(self, text):
        text = text.lstrip()
        while text:
            reduced = _greeting.sub("", text, count=1)
            reduced = _introduction.sub("", reduced, count=1).lstrip()
            if reduced == text:
                break
            text = reduced
        normalized = _normalized(text)
        if (not normalized or _filler.fullmatch(text) or _location_start.match(text)
                or normalized == self.prefix
                or (text.startswith(("这里是", "这里位于", "此处是", "現在", "今", "현재"))
                    and self.location and self.location in normalized)):
            return ""
        return text

    def feed(self, text, *, final=False):
        if self.opened:
            return text
        self.buffer += text
        while self.buffer:
            boundary = _sentence_end.search(self.buffer)
            if boundary and boundary.group() == "." and boundary.end() == len(self.buffer) and not final:
                boundary = None
            if boundary is None and not final and len(self.buffer) < 1024:
                return ""
            end = boundary.end() if boundary else len(self.buffer)
            sentence, self.buffer = self.buffer[:end], self.buffer[end:]
            cleaned = "" if self.discarding else self._clean(sentence)
            self.discarding = not cleaned and boundary is None and not final
            if cleaned:
                self.opened = True
                output = cleaned + self.buffer
                self.buffer = ""
                return output
        return ""


class GuideAnnouncement:
    def __init__(self, conn, beacon, settings, valid):
        self.conn = conn
        self.socket = conn.websocket
        self.valid = valid
        self.language = response_language(conn)
        self.location = "，".join(part.strip() for part in (beacon.floor, beacon.area, beacon.location_description)
                                 if part.strip())
        suffix = {"en": "en", "ja": "ja", "ko": "ko"}.get(self.language, "zh_cn")
        template = getattr(settings, f"location_template_{suffix}")
        self.prefix = template.format(location=self.location)
        self.filter = OpeningFilter(self.location, self.prefix)
        self.sentence_id = None
        self.parts = []

    def owns_turn(self):
        return (self.sentence_id is not None and self.conn.sentence_id == self.sentence_id
                and self.conn.websocket is self.socket and not self.conn.client_abort
                and connection_is_open(self.conn))

    def start(self, sentence_id):
        self.sentence_id = sentence_id
        if not self.owns_turn() or not self.valid() or not self.location:
            return False
        self.parts.append(self.prefix)
        self.conn.tts.store_tts_text(sentence_id, self.prefix)
        tts_complete_sentence(self.conn, self.prefix, sentence_id)
        return True

    def accept(self, text, *, final=False):
        if not self.owns_turn() or not self.valid():
            return ""
        output = self.filter.feed(text, final=final)
        if output:
            if len(self.parts) == 1 and self.language in {"en", "ko"}:
                output = " " + output
            self.parts.append(output)
            self.conn.tts.store_tts_text(self.sentence_id, "".join(self.parts))
        return output

    def finish(self):
        if not self.owns_turn():
            return
        try:
            remaining = self.accept("", final=True)
            if remaining:
                tts_complete_sentence(self.conn, remaining, self.sentence_id)
            if self.parts and self.valid():
                self.conn.dialogue.put(Message(role="assistant", content="".join(self.parts)))
        finally:
            if self.owns_turn():
                self.conn.tts.tts_text_queue.put(TTSMessageDTO(self.sentence_id, SentenceType.LAST, ContentType.ACTION))


@contextmanager
def guide_announcement(conn, beacon, settings, valid):
    turn = GuideAnnouncement(conn, beacon, settings, valid)
    token = _current.set(turn)
    try:
        yield turn
    finally:
        try:
            turn.finish()
        finally:
            _current.reset(token)


def _turn(conn, sentence_id=None):
    turn = _current.get()
    if turn is not None and turn.conn is conn and (sentence_id is None or turn.sentence_id == sentence_id):
        return turn
    return None


def is_guide_announcement(conn, sentence_id=None):
    return _turn(conn, sentence_id) is not None


def start_guide_announcement(conn, sentence_id):
    turn = _turn(conn)
    return turn.start(sentence_id) if turn is not None else True


def filter_guide_announcement_text(conn, text, sentence_id):
    turn = _turn(conn, sentence_id)
    return turn.accept(text) if turn is not None else text


def guide_announcement_messages(conn, messages, sentence_id):
    turn = _turn(conn, sentence_id)
    if turn is None:
        return messages
    rule = (
        f"This is an automatic beacon narration in {LANGUAGE_NAMES[turn.language]}. "
        "The device already speaks this exact location introduction: "
        f"{json.dumps(turn.prefix, ensure_ascii=False)}. "
        "Generate only the continuation: one or two short factual sentences about this location or nearby exhibits. "
        "Do not greet, welcome, introduce yourself, repeat the visitor's location, or ask a follow-up question. "
        "Use available evidence and tools when needed; never invent exhibits or facts. "
        "If no relevant facts are available, return no additional narration."
    )
    result = [dict(message) for message in messages]
    system = next((message for message in result if message.get("role") == "system"), None)
    if system is None:
        result.insert(0, {"role": "system", "content": rule})
    else:
        system["content"] = (system.get("content") or "") + "\n\n" + rule
    return result


def queue_guide_tool_reply(conn, text, sentence_id):
    turn = _turn(conn, sentence_id)
    if turn is None:
        return False
    output = turn.accept(strip_emotion_markers(text or ""))
    if output:
        tts_complete_sentence(conn, output, sentence_id)
    return True
