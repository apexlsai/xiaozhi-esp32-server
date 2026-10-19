import random
import threading
from dataclasses import dataclass

from core.utils.tool_feedback import ToolFeedbackScheduler
from .vision import vision_language
from .tts import tts_complete_sentence


CAMERA_TOOLS = {"self_camera_take_photo", "self.camera.take_photo"}
CAMERA_PHRASES = {
    "zh-CN": (
        "我看看。", "让我瞧瞧。", "看看拍到了什么。", "拍的是什么呢？我来看看。",
        "我来帮你看看。", "稍等，我看一下。", "一起看看这是什么。", "我来仔细瞧瞧。",
        "收到图片啦。", "图片收到了，我看看。", "让我看看这张照片。", "正在解析图片。",
        "正在查看图片内容。", "我来辨认一下。", "我来看看有哪些细节。", "稍等，正在识别。",
    ),
    "en": (
        "Let me take a look.", "Let's have a look.", "Let's see what you captured.",
        "What could it be? Let's see.", "I'll take a look for you.", "One moment, please.",
        "Let's see what this is.", "Let me look more closely.",
        "Got your photo!", "Your photo is here. Let's see.", "Let me check this photo.",
        "I'm analyzing the photo.", "I'm checking what's in the photo.",
        "Let me see if I can identify it.", "Let me check the details.",
        "One moment, I'm checking.",
    ),
    "ja": (
        "確認しますね。", "ちょっと見てみますね。", "何が写ったか見てみましょう。",
        "何でしょうね。見てみます。", "私が確認しますね。", "少々お待ちください。",
        "一緒に見てみましょう。", "じっくり見てみますね。",
        "写真が届きました。", "写真を受け取りました。確認しますね。", "この写真を見てみますね。",
        "写真を解析しています。", "写真の内容を確認しています。", "何か調べてみますね。",
        "細かいところを見てみますね。", "ただいま確認中です。",
    ),
    "ko": (
        "한번 살펴볼게요.", "어디 한번 볼까요?", "무엇이 찍혔는지 볼게요.",
        "무엇일까요? 한번 볼게요.", "제가 살펴볼게요.", "잠시만 기다려 주세요.",
        "무엇인지 함께 볼까요?", "자세히 살펴볼게요.",
        "사진이 도착했어요.", "사진을 받았어요. 살펴볼게요.", "이 사진을 한번 볼게요.",
        "사진을 분석하고 있어요.", "사진 속 내용을 확인하고 있어요.", "무엇인지 확인해 볼게요.",
        "세부 내용을 살펴볼게요.", "잠시만요, 확인 중이에요.",
    ),
}


@dataclass(frozen=True)
class Phrase:
    text: str
    requires_image: bool = False


DEFAULT_PHRASES = {
    language: tuple(Phrase(text, index >= 8) for index, text in enumerate(phrases))
    for language, phrases in CAMERA_PHRASES.items()
}


@dataclass
class CameraWait:
    sentence_id: str
    request_id: str = None
    image_received: bool = False
    closed: bool = False
    announced: bool = False
    key: str = None
    timer: object = None


class CameraFeedbackScheduler(ToolFeedbackScheduler):
    def __init__(self, conn, timer_factory=threading.Timer, choose=random.choice):
        super().__init__(conn, timer_factory)
        self.choose = choose
        self.camera = None
        self.used = {}
        self.last_phrase = {}

    def _camera_settings(self, tool_name):
        config = self.conn.config.get("tool_feedback")
        config = config if isinstance(config, dict) else {}
        if not config.get("enabled", True):
            return None
        tools = config.get("tools")
        if isinstance(tools, dict):
            other_name = next(name for name in CAMERA_TOOLS if name != tool_name)
            value = tools.get(tool_name, tools.get(other_name))
        else:
            value = {}
        if not isinstance(value, (dict, str)) or isinstance(value, str) and not value.strip():
            return None
        try:
            delay = max(0, int(config.get("delay_ms", 500))) / 1000
        except (TypeError, ValueError, OverflowError):
            delay = 0.5
        return delay, value

    def _language(self):
        return vision_language(self.conn)

    def _select(self, value, state):
        language = self._language()
        if isinstance(value, str):
            return language, value.strip()
        if language is None:
            return None, None
        catalog = value.get("phrases")
        if catalog is None:
            phrases = DEFAULT_PHRASES[language]
        elif isinstance(catalog, dict):
            entries = catalog.get(language, [])
            if not isinstance(entries, list):
                return language, None
            phrases = []
            for entry in entries:
                if isinstance(entry, str) and entry.strip():
                    phrases.append(Phrase(entry.strip()))
                elif isinstance(entry, dict):
                    text = entry.get("text")
                    requires_image = entry.get("requires_image", False)
                    if isinstance(text, str) and text.strip() and isinstance(requires_image, bool):
                        phrases.append(Phrase(text.strip(), requires_image))
        else:
            return language, None
        eligible = list(dict.fromkeys(
            phrase.text for phrase in phrases if state.image_received or not phrase.requires_image
        ))
        if not eligible:
            return language, None
        used = self.used.setdefault(language, set())
        unused = [phrase for phrase in eligible if phrase not in used]
        if not unused:
            used.clear()
            unused = eligible
        candidates = [phrase for phrase in unused if phrase != self.last_phrase.get(language)]
        text = self.choose(candidates or unused)
        used.add(text)
        self.last_phrase[language] = text
        return language, text

    def _current(self, state):
        return (
            self.camera is state and self.conn.sentence_id == state.sentence_id
            and not self.conn.stop_event.is_set() and not self.conn.client_abort
            and self.conn.tts is not None
        )

    def _state(self, sentence_id):
        if self.camera is None or self.camera.sentence_id != sentence_id:
            if self.camera is not None:
                self._close(self.camera)
            self.camera = CameraWait(sentence_id)
        return self.camera

    def _close(self, state):
        state.closed = True
        if state.timer is not None:
            state.timer.cancel()
            if self.timers.get(state.key) is state.timer:
                self.timers.pop(state.key)

    def close(self, state):
        with self.lock:
            self._close(state)

    def schedule(self, tool_name, call_id, sentence_id):
        if tool_name not in CAMERA_TOOLS:
            return super().schedule(tool_name, call_id, sentence_id)
        settings = self._camera_settings(tool_name)
        if settings is None or not sentence_id:
            return None
        delay, value = settings
        with self.lock:
            if self.conn.sentence_id != sentence_id:
                return None
            state = self._state(sentence_id)
            if not self._current(state) or state.closed or state.announced:
                return None
            if state.key is not None:
                return state.key
            key = str(call_id or f"{sentence_id}:camera")

            def emit():
                with self.lock:
                    if self.timers.get(key) is not timer or not self._current(state):
                        return
                    self.timers.pop(key)
                    if state.closed or state.announced:
                        return
                    language, phrase = self._select(value, state)
                    if not phrase:
                        state.closed = True
                        return
                    state.announced = True
                    tts_complete_sentence(self.conn, phrase, sentence_id, feedback=True)
                self.conn.logger.info(
                    f"拍照等待提示已提交: language={language} image_received={state.image_received} "
                    f"delay_ms={int(delay * 1000)} text={phrase}"
                )

            timer = self.timer_factory(delay, emit)
            timer.daemon = True
            state.key = key
            state.timer = timer
            self.timers[key] = timer
        timer.start()
        return key

    def image_received(self, sentence_id, request_id=None):
        with self.lock:
            if self.conn.sentence_id != sentence_id:
                return None
            state = self._state(sentence_id)
            if not self._current(state):
                return None
            state.image_received = True
            state.request_id = request_id
            return state

    def cancel_request(self, request):
        with self.lock:
            state = self.camera
            if (state is not None and state.sentence_id == request.sentence_id
                    and state.request_id in (None, request.request_id)):
                self._close(state)

    def cancel(self, key):
        if key is None:
            return
        with self.lock:
            if self.camera is not None and self.camera.key == str(key):
                self._close(self.camera)
            timer = self.timers.pop(str(key), None)
            if timer is not None:
                timer.cancel()

    def cancel_all(self):
        with self.lock:
            self._close(self._state(self.conn.sentence_id))
            for timer in self.timers.values():
                timer.cancel()
            self.timers.clear()
            self.announced.clear()


def camera_image_received(conn, request=None, *, legacy_mcp=False, sentence_id=None):
    scheduler = getattr(conn, "tool_feedback", None)
    if not isinstance(scheduler, CameraFeedbackScheduler):
        return None
    if request is not None:
        if request.cancelled or request.finished:
            return None
        state = scheduler.image_received(request.sentence_id, request.request_id)
        if state is not None and request.mode == "push":
            scheduler.schedule("self_camera_take_photo", request.request_id, request.sentence_id)
    elif legacy_mcp:
        state = scheduler.image_received(sentence_id)
    else:
        return None
    return (scheduler, state) if state is not None else None


def finish_camera_feedback(handle):
    if handle is not None:
        scheduler, state = handle
        scheduler.close(state)


def cancel_camera_feedback(conn, request):
    scheduler = getattr(conn, "tool_feedback", None)
    if isinstance(scheduler, CameraFeedbackScheduler):
        scheduler.cancel_request(request)
