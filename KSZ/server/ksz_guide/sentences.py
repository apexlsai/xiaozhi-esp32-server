import re
import unicodedata

from .prompt import normalize_language


_PUNCTUATION = {
    "zh": ("。！？；!?;\n", "，、,"),
    "en": ("!?;\n", ","),
    "ja": ("。｡！？!?;\n", "、，,"),
    "ko": ("!?;\n", ",，"),
}
_COMPATIBLE_ENDINGS = frozenset("。｡！？!?；;\n")
_CLAUSE_MIN_CHARS = {"zh": 12, "en": 40, "ja": 8, "ko": 20}
_WORD_BREAK_CHARS = {"en": 120, "ko": 60}
_PARENTHESES = {"(": ")", "（": "）", "[": "]", "【": "】", "{": "}"}
_CLOSERS = frozenset("\"'”’」』）》】)]")
_ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e",
    "a.m", "p.m", "u.s", "u.k", "no", "fig", "vol", "inc", "ltd", "dept", "approx",
})
_WORD = re.compile(r"[A-Za-z][A-Za-z.]*$")
_INITIALS = re.compile(r"(?:[A-Za-z]\.)*[A-Z]$")


def sentence_boundary(text, *, language="zh-CN", final=False, first_clause=False):
    family = (normalize_language(language) or "zh-CN").split("-", 1)[0]
    endings, clauses = _PUNCTUATION[family]
    parentheses = []
    for index, char in enumerate(text):
        if char in _PARENTHESES:
            parentheses.append(_PARENTHESES[char])
        elif parentheses and char == parentheses[-1]:
            parentheses.pop()
        following = text[index + 1:index + 2]
        previous = text[index - 1:index] if index else ""
        boundary = char in endings or char in _COMPATIBLE_ENDINGS
        if char in ".．":
            if not following:
                boundary = final
            elif following in ".．" or following.isascii() and following.isalnum():
                boundary = False
            elif previous.isdigit() and following.isdigit():
                boundary = False
            else:
                word = _WORD.search(text[:index])
                token = word.group() if word else ""
                boundary = token.lower() not in _ABBREVIATIONS and not _INITIALS.fullmatch(token)
        elif char == "…":
            boundary = bool(following and following != "…") or final and not following
        elif char in clauses and not parentheses and (first_clause or index + 1 >= _CLAUSE_MIN_CHARS[family]):
            boundary = not previous.isdigit() or bool(following and not following.isdigit()) or final and not following
        elif char == " " and not parentheses and index >= _WORD_BREAK_CHARS.get(family, float("inf")):
            words = text[:index].rsplit(None, 1)
            token = words[-1].rstrip(".") if words else ""
            boundary = bool(token) and token.lower() not in _ABBREVIATIONS and not previous.isdigit()
        if boundary:
            end = index + 1
            while end < len(text) and text[end] in _CLOSERS:
                end += 1
            return end
    return len(text) if final and text else None


class SentenceBuffer:
    def __init__(self, language="zh-CN"):
        self.language = normalize_language(language) or "zh-CN"
        self.pending = ""

    def feed(self, text, final=False):
        self.pending += text
        parts = []
        while (end := sentence_boundary(self.pending, language=self.language, final=final)) is not None:
            sentence = self.pending[:end].strip()
            self.pending = self.pending[end:]
            if sentence:
                parts.append(sentence)
        return parts


def synthesize_pending(tts, *, final=False, opus_handler=None):
    from core.utils.textUtils import get_string_no_punctuation_or_emoji

    full_text = "".join(tts.tts_text_buff)
    emitted = False
    while tts.processed_chars < len(full_text):
        conn = tts.conn
        if conn and (
            getattr(conn, "client_abort", False)
            or getattr(conn, "stop_event", None) is not None and conn.stop_event.is_set()
            or getattr(tts, "current_sentence_id", None) not in (None, getattr(conn, "sentence_id", None))
        ):
            break
        pending = full_text[tts.processed_chars:]
        end = sentence_boundary(pending, language=getattr(tts, "ksz_response_language", "zh-CN"),
                                final=final, first_clause=tts.is_first_sentence)
        if end is None:
            break
        text = get_string_no_punctuation_or_emoji(pending[:end])
        tts.processed_chars += end
        if not any(not char.isspace() and not unicodedata.category(char).startswith("P") for char in text):
            continue
        tts.is_first_sentence = False
        tts.to_tts_stream(text, opus_handler=opus_handler or tts.handle_opus,
                          emotion_context=tts.current_emotion_context)
        emitted = True
    return emitted
