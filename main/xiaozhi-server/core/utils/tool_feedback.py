import threading

from core.providers.tts.dto.dto import ContentType


TAG = __name__

DEFAULT_TOOL_FEEDBACK = {
    "enabled": True,
    "delay_ms": 500,
    "tools": {"self_camera_take_photo": "我看看。"},
}


class ToolFeedbackScheduler:
    def __init__(self, conn, timer_factory=threading.Timer):
        self.conn = conn
        self.timer_factory = timer_factory
        self.lock = threading.Lock()
        self.timers = {}
        self.announced = set()

    def _settings(self, tool_name):
        config = self.conn.config.get("tool_feedback")
        if not isinstance(config, dict):
            config = DEFAULT_TOOL_FEEDBACK
        if not config.get("enabled", True):
            return None
        tools = config.get("tools")
        if not isinstance(tools, dict):
            tools = DEFAULT_TOOL_FEEDBACK["tools"]
        phrase = tools.get(tool_name)
        if not isinstance(phrase, str) or not phrase.strip():
            return None
        try:
            delay_ms = max(0, int(config.get("delay_ms", 500)))
        except (TypeError, ValueError):
            delay_ms = 500
        return delay_ms / 1000, phrase.strip()

    def schedule(self, tool_name, call_id, sentence_id):
        settings = self._settings(tool_name)
        if settings is None or not sentence_id:
            return None

        delay, phrase = settings
        key = str(call_id or f"{sentence_id}:{tool_name}")

        def emit():
            with self.lock:
                entry = self.timers.pop(key, None)
                announced_key = (sentence_id, tool_name)
                if entry is None or announced_key in self.announced:
                    return
                if (
                    self.conn.stop_event.is_set()
                    or self.conn.client_abort
                    or self.conn.sentence_id != sentence_id
                    or self.conn.tts is None
                ):
                    return
                self.announced = {
                    item for item in self.announced if item[0] == sentence_id
                }
                self.announced.add(announced_key)

            self.conn.tts.tts_one_sentence(
                self.conn,
                ContentType.TEXT,
                content_detail=phrase,
                sentence_id=sentence_id,
            )
            self.conn.logger.bind(tag=TAG).info(
                f"工具等待反馈已播放: tool={tool_name}, delay_ms={int(delay * 1000)}"
            )

        timer = self.timer_factory(delay, emit)
        timer.daemon = True
        with self.lock:
            previous = self.timers.pop(key, None)
            if previous is not None:
                previous.cancel()
            self.timers[key] = timer
        timer.start()
        self.conn.logger.bind(tag=TAG).debug(
            f"工具等待反馈已调度: tool={tool_name}, delay_ms={int(delay * 1000)}"
        )
        return key

    def cancel(self, key):
        if key is None:
            return
        with self.lock:
            timer = self.timers.pop(str(key), None)
        if timer is not None:
            timer.cancel()

    def cancel_all(self):
        with self.lock:
            timers = list(self.timers.values())
            self.timers.clear()
            self.announced.clear()
        for timer in timers:
            timer.cancel()
