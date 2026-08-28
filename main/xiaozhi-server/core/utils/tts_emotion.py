from dataclasses import dataclass
from typing import Optional

from core.utils import textUtils


DEFAULT_EMOJI = "🙂"
DEFAULT_MAX_CHANGES = 3

VOICE_STYLE_PROFILES = {
    "neutral": "平稳自然，语气克制。",
    "warm": "亲切温暖，带自然笑意。",
    "joy": "轻松活泼，略带幽默感。",
    "sad": "温和低沉，语速稍缓，避免夸张哭腔。",
    "sleepy": "轻柔慵懒，语速稍缓，避免含混。",
    "angry": "语气坚定有力度，保持克制，不要吼叫。",
    "surprise": "带短暂惊讶感，语调适度上扬，同时保持专业。",
    "thinking": "若有所思，语速稍缓，带探索感。",
    "confident": "从容自信，节奏清晰，带轻微幽默感。",
}

EMOJI_VOICE_PROFILES = {
    "😶": "neutral",
    "🙂": "warm",
    "😉": "warm",
    "😍": "warm",
    "😘": "warm",
    "😆": "joy",
    "😂": "joy",
    "😜": "joy",
    "🤤": "joy",
    "😔": "sad",
    "😭": "sad",
    "😴": "sleepy",
    "😠": "angry",
    "😲": "surprise",
    "😱": "surprise",
    "😳": "surprise",
    "🤔": "thinking",
    "🙄": "thinking",
    "😌": "confident",
    "😎": "confident",
    "😏": "confident",
}


@dataclass(frozen=True)
class TTSEmotionContext:
    emoji: str
    emotion: str
    voice_profile: str
    style_prompt: str


@dataclass(frozen=True)
class EmotionStreamEvent:
    text: str = ""
    context: Optional[TTSEmotionContext] = None


def build_emotion_context(emoji: str) -> TTSEmotionContext:
    voice_profile = EMOJI_VOICE_PROFILES.get(emoji, "neutral")
    return TTSEmotionContext(
        emoji=emoji,
        emotion=textUtils.EMOJI_MAP.get(emoji, "neutral"),
        voice_profile=voice_profile,
        style_prompt=VOICE_STYLE_PROFILES[voice_profile],
    )


def compose_style_prompt(
    base_style: str,
    emotion_context: Optional[TTSEmotionContext],
    enabled: bool,
) -> str:
    style_parts = []
    if base_style.strip():
        style_parts.append(base_style.strip())
    if enabled and emotion_context is not None:
        style_parts.append(
            f"当前片段的表达方式：{emotion_context.style_prompt}"
        )
    return " ".join(style_parts)

def strip_emotion_markers(text: str) -> str:
    parser = EmotionStreamParser(enabled=False)
    return "".join(event.text for event in parser.feed(text))


class EmotionStreamParser:
    def __init__(self, enabled: bool = True, max_changes: int = DEFAULT_MAX_CHANGES):
        self.enabled = enabled
        self.max_changes = max(1, max_changes)
        self.current_context: Optional[TTSEmotionContext] = None
        self.change_count = 0

    def feed(self, text: str) -> list[EmotionStreamEvent]:
        if not text:
            return []

        events = []
        buffer = []

        def flush_buffer():
            if not buffer:
                return
            buffered_text = "".join(buffer)
            if self.enabled and self.current_context is None and buffered_text.strip():
                context = build_emotion_context(DEFAULT_EMOJI)
                self.current_context = context
                self.change_count = 1
                events.append(EmotionStreamEvent(context=context))
            events.append(EmotionStreamEvent(text=buffered_text))
            buffer.clear()

        for char in text:
            if char in textUtils.EMOJI_MAP:
                flush_buffer()
                self._append_context(events, char)
                continue
            if textUtils.is_emoji(char) or char in {"\ufe0f", "\u200d"}:
                continue
            buffer.append(char)

        flush_buffer()

        return events

    def _append_context(self, events: list[EmotionStreamEvent], emoji: str):
        if not self.enabled:
            return
        context = build_emotion_context(emoji)
        if self.current_context == context:
            return
        if self.change_count >= self.max_changes:
            return
        self.current_context = context
        self.change_count += 1
        events.append(EmotionStreamEvent(context=context))
