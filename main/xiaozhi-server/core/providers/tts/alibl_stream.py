import os
import uuid
import json
import time
import queue
import asyncio
import traceback
import websockets

from dataclasses import dataclass, field
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Callable, Any
from websockets.protocol import State
from config.logger import setup_logging
from core.utils.tts import MarkdownCleaner
from core.utils.alibl_endpoint import build_ws_connect_options, resolve_ws_url
from core.utils.alibl_event import extract_sentence_start_text
from core.providers.tts.base import TTSProviderBase
from core.providers.tts.dto.dto import SentenceType, ContentType, InterfaceType, TTSMessageDTO

TAG = __name__
logger = setup_logging()


class _SentenceMessage(TTSMessageDTO):
    pass


class _FeedbackMessage(_SentenceMessage):
    pass


@dataclass
class _SynthesisTask:
    sentence_id: str
    ws: Any
    feedback: bool = False
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    error: Exception | None = None
    text: str = ""
    legacy_subtitle_sent: bool = False


class TTSProvider(TTSProviderBase):
    TTS_PARAM_CONFIG = [
        ("ttsVolume", "volume", 0, 100, 50, int),
        ("ttsRate", "rate", 0.5, 2.0, 1.0, lambda v: round(v, 1)),
        ("ttsPitch", "pitch", 0.5, 2.0, 1.0, lambda v: round(v, 1)),
    ]

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)

        self.interface_type = InterfaceType.DUAL_STREAM
        # 基础配置
        self.api_key = config.get("api_key")
        if not self.api_key:
            raise ValueError("api_key is required for CosyVoice TTS")
        self.report_on_last = True

        # WebSocket配置
        self.ws_url = resolve_ws_url(config.get("ws_url"))
        self.ws_connect_options = build_ws_connect_options(self.tts_timeout)
        self.ws = None
        self._monitor_task = None
        self.activate_session = False
        self.last_active_time = None
        self._active_task = None
        self._command_lock = asyncio.Lock()
        self._closing_connections = {}
        self._turn_finished = False
        self._turn_failed = False
        self._feedback_active = False
        self.current_sentence_id = None

        # 模型和音色配置
        self.model = config.get("model", "cosyvoice-v2")
        self.voice = config.get("voice", "longxiaochun_v2")  # 默认音色
        if config.get("private_voice"):
            self.voice = config.get("private_voice")

        # 音频参数配置
        self.format = config.get("format", "pcm")

        volume = config.get("volume", "50")
        self.volume = int(volume) if volume else 50

        rate = config.get("rate", "1.0")
        self.rate = float(rate) if rate else 1.0

        pitch = config.get("pitch", "1.0")
        self.pitch = float(pitch) if pitch else 1.0

        # 应用百分比调整（如果存在），否则使用公有化配置
        self._apply_percentage_params(config)

        self.header = {
            "Authorization": f"Bearer {self.api_key}",
            # "user-agent": "your_platform_info", // 可选
            # "X-DashScope-WorkSpace": workspace, // 可选，阿里云百炼业务空间ID
            "X-DashScope-DataInspection": "enable",
        }

    async def _ensure_connection(self):
        try:
            current_time = time.time()
            if (
                self.ws is not None
                and self.ws.state == State.OPEN
                and self.last_active_time is not None
                and current_time - self.last_active_time < 60
                and self._monitor_task is not None
                and not self._monitor_task.done()
            ):
                return self.ws
            await self._disconnect(self.ws)

            self.ws = await websockets.connect(
                self.ws_url,
                additional_headers=self.header,
                ping_interval=30,
                ping_timeout=10,
                close_timeout=10,
                **self.ws_connect_options,
            )
            self.last_active_time = current_time
            self._monitor_task = asyncio.create_task(
                self._start_monitor_tts_response(self.ws)
            )
            return self.ws
        except Exception as e:
            logger.bind(tag=TAG).error(f"建立连接失败: {str(e)}")
            raise

    def tts_tool_feedback(self, conn, phrase, sentence_id):
        self.tts_text_queue.put(
            _FeedbackMessage(
                sentence_id=sentence_id,
                sentence_type=SentenceType.MIDDLE,
                content_type=ContentType.TEXT,
                content_detail=phrase,
            )
        )

    def tts_complete_sentence(self, conn, text, sentence_id):
        self.tts_text_queue.put(
            _SentenceMessage(
                sentence_id=sentence_id,
                sentence_type=SentenceType.MIDDLE,
                content_type=ContentType.TEXT,
                content_detail=text,
            )
        )

    def tts_text_priority_thread(self):
        while not self.conn.stop_event.is_set():
            try:
                message = self.tts_text_queue.get(timeout=1)
            except queue.Empty:
                if self.conn.client_abort and self.ws is not None:
                    asyncio.run_coroutine_threadsafe(
                        self._close_if_aborted(self.ws, self.current_sentence_id),
                        self.conn.loop,
                    )
                continue

            future = asyncio.run_coroutine_threadsafe(
                self._process_message(message), self.conn.loop
            )
            try:
                deadline = time.monotonic() + self.tts_timeout * 6 + 1
                while True:
                    try:
                        future.result(timeout=0.2)
                        break
                    except FutureTimeoutError:
                        if future.done():
                            raise
                        if (
                            self.conn.client_abort
                            or self.conn.stop_event.is_set()
                            or message.sentence_id != self.conn.sentence_id
                            or time.monotonic() >= deadline
                        ):
                            future.cancel()
                            break
            except Exception as e:
                future.cancel()
                logger.bind(tag=TAG).error(
                    f"处理TTS文本失败: {str(e)}, 类型: {type(e).__name__}, 堆栈: {traceback.format_exc()}"
                )

    def _sentence_is_current(self, sentence_id):
        return (
            sentence_id == self.current_sentence_id == self.conn.sentence_id
            and not self.conn.client_abort
            and not self.conn.stop_event.is_set()
        )

    async def _close_if_aborted(self, ws, sentence_id):
        if (
            self.conn.client_abort
            and self.ws is ws
            and self.current_sentence_id == sentence_id == self.conn.sentence_id
        ):
            await self.close()

    async def _process_message(self, message):
        async with self._command_lock:
            if self.conn.client_abort:
                await self.close()
                return
            if message.sentence_id != self.conn.sentence_id:
                return
            if message.sentence_type == SentenceType.FIRST:
                await self.start_session(message.sentence_id)
                return
            if not self._sentence_is_current(message.sentence_id) or self._turn_finished:
                return

            feedback = isinstance(message, _FeedbackMessage)
            complete_sentence = isinstance(message, _SentenceMessage)
            try:
                if complete_sentence:
                    await self._finish_task()
                    self._feedback_active = feedback
                if message.content_type == ContentType.TEXT and message.content_detail:
                    await self.text_to_speak(message.content_detail, None)
                elif message.content_type == ContentType.FILE:
                    if message.content_file and os.path.exists(message.content_file):
                        audio = []
                        await asyncio.to_thread(
                            self._process_audio_file_stream,
                            message.content_file,
                            callback=audio.append,
                        )
                        if self._sentence_is_current(message.sentence_id):
                            self.before_stop_play_files.extend(
                                (chunk, message.content_detail) for chunk in audio
                            )
                if complete_sentence:
                    await self._finish_task()
            except asyncio.CancelledError:
                await self._disconnect(self.ws)
                raise
            except Exception:
                if not feedback:
                    self._turn_failed = True
                await self._disconnect(self.ws)
                raise
            finally:
                self._feedback_active = False
                if message.sentence_type == SentenceType.LAST:
                    await self.finish_session(message.sentence_id)

    async def text_to_speak(self, text, _):
        if self._turn_failed or not self._sentence_is_current(self.current_sentence_id):
            return
        filtered_text = MarkdownCleaner.clean_markdown(text)
        if not filtered_text or (
            not filtered_text.strip()
            and self._active_task is None
            and not self._pending_prefix
        ):
            return
        confirmed_texts, self._pending_prefix = self._match_stream_text(filtered_text)
        confirmed = "".join(confirmed_texts)
        if confirmed:
            task = self._active_task or await self._begin_task()
            task.text += self._restore_original_text(confirmed)
            await self._send_task(task, "continue-task", {"input": {"text": confirmed}})

    async def start_session(self, session_id):
        if self._active_task is not None:
            await self._disconnect(self.ws)
        self.clear_tts_text(self.current_sentence_id)
        self.current_sentence_id = session_id
        self._turn_finished = False
        self._turn_failed = False
        self._feedback_active = False
        self.tts_audio_first_sentence = True
        self.before_stop_play_files.clear()
        self.reset_stream_state()
        self.opus_encoder.reset_state()

    async def _begin_task(self):
        sentence_id = self.current_sentence_id
        ws = await self._ensure_connection()
        if not self._sentence_is_current(sentence_id):
            raise asyncio.CancelledError()
        task = _SynthesisTask(sentence_id, ws, feedback=self._feedback_active)
        self._active_task = task
        self.activate_session = True
        self.opus_encoder.reset_state()
        await self._send_task(
            task,
            "run-task",
            {
                "task_group": "audio",
                "task": "tts",
                "function": "SpeechSynthesizer",
                "model": self.model,
                "parameters": {
                    "text_type": "PlainText",
                    "voice": self.voice,
                    "format": self.format,
                    "sample_rate": self.conn.sample_rate,
                    "volume": self.volume,
                    "rate": self.rate,
                    "pitch": self.pitch,
                },
                "input": {},
            },
        )
        try:
            await asyncio.wait_for(task.started.wait(), self.tts_timeout)
            if task.error:
                raise task.error
        except BaseException:
            await self._disconnect(ws)
            raise
        return task

    async def _send_task(self, task, action, payload):
        if task.error:
            raise task.error
        if not self._sentence_is_current(task.sentence_id):
            raise asyncio.CancelledError()
        try:
            await asyncio.wait_for(
                task.ws.send(json.dumps({
                    "header": {
                        "action": action,
                        "task_id": task.task_id,
                        "streaming": "duplex",
                    },
                    "payload": payload,
                })),
                self.tts_timeout,
            )
            if not self._sentence_is_current(task.sentence_id):
                raise asyncio.CancelledError()
            if self.ws is task.ws:
                self.last_active_time = time.time()
        except BaseException:
            await self._disconnect(task.ws)
            raise

    async def _finish_task(self):
        if self._pending_prefix and not self._turn_failed:
            pending, self._pending_prefix = self._pending_prefix, ""
            task = self._active_task or await self._begin_task()
            task.text += pending
            await self._send_task(task, "continue-task", {"input": {"text": pending}})
        task = self._active_task
        if task is None:
            return
        try:
            await self._send_task(task, "finish-task", {"input": {}})
            await asyncio.wait_for(task.finished.wait(), self.tts_timeout)
            if task.error:
                raise task.error
        except BaseException:
            await self._disconnect(task.ws)
            raise
        finally:
            if self._active_task is task:
                self._active_task = None
                self.activate_session = False

    async def finish_session(self, session_id):
        if session_id != self.current_sentence_id or self._turn_finished:
            return
        try:
            if self._sentence_is_current(session_id):
                await self._finish_task()
        finally:
            self._turn_finished = True
            self.reset_stream_state()
            self.clear_tts_text(session_id)
            if self._sentence_is_current(session_id):
                self._process_before_stop_play_files()

    def _invalidate_connection(self, ws, error):
        if ws is None:
            return
        if self.ws is ws:
            self.ws = None
            self.last_active_time = None
        task = self._active_task
        if task is not None and task.ws is ws:
            completed = task.finished.is_set() and task.error is None
            if not completed:
                task.error = error
            task.started.set()
            task.finished.set()
            self._active_task = None
            self.activate_session = False
            if task.sentence_id == self.current_sentence_id:
                if not completed and not task.feedback:
                    self._turn_failed = True
                self.reset_stream_state()
                self.opus_encoder.reset_state()

    async def _disconnect(self, ws):
        if ws is None:
            return
        monitor = self._monitor_task if self.ws is ws else None
        self._invalidate_connection(ws, RuntimeError("TTS连接已关闭"))
        if monitor is not None and monitor is not asyncio.current_task():
            if self._monitor_task is monitor:
                self._monitor_task = None
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        self._schedule_socket_close(ws)

    def _schedule_socket_close(self, ws):
        if ws in self._closing_connections:
            return

        async def close_socket():
            try:
                await asyncio.wait_for(ws.close(), min(self.tts_timeout, 10))
            except (Exception, asyncio.CancelledError):
                pass
            finally:
                self._closing_connections.pop(ws, None)

        self._closing_connections[ws] = asyncio.create_task(close_socket())

    async def close(self):
        self._turn_finished = True
        self.reset_stream_state()
        self._sentence_text_map.clear()
        await self._disconnect(self.ws)
        if self._closing_connections:
            await asyncio.gather(*tuple(self._closing_connections.values()))

    def _task_is_current(self, task):
        return (
            self._active_task is task
            and task.error is None
            and self._sentence_is_current(task.sentence_id)
        )

    def _handle_task_audio(self, task, audio):
        if self._task_is_current(task):
            self.tts_audio_queue.put((SentenceType.MIDDLE, audio, None, task.sentence_id))

    async def _start_monitor_tts_response(self, ws):
        try:
            while not self.conn.stop_event.is_set():
                msg = await ws.recv()
                if self.ws is ws:
                    self.last_active_time = time.time()
                task = self._active_task
                if task is None or task.ws is not ws:
                    continue
                if isinstance(msg, (bytes, bytearray)):
                    if self._task_is_current(task) and not task.finished.is_set():
                        self.opus_encoder.encode_pcm_to_opus_stream(
                            msg, False, callback=lambda audio: self._handle_task_audio(task, audio)
                        )
                    continue
                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    logger.bind(tag=TAG).warning("收到无效的JSON消息")
                    continue
                header = data.get("header", {})
                if header.get("task_id") != task.task_id or task.finished.is_set():
                    continue
                event = header.get("event")
                if event == "task-started":
                    task.started.set()
                elif event == "task-failed":
                    error = RuntimeError(
                        f"{header.get('error_code', 'unknown')} - "
                        f"{header.get('error_message', '未知错误')}"
                    )
                    logger.bind(tag=TAG).error(f"TTS任务失败: {error}")
                    self._invalidate_connection(ws, error)
                    break
                elif event == "task-finished":
                    if self._task_is_current(task):
                        self.opus_encoder.encode_pcm_to_opus_stream(
                            b"", True, callback=lambda audio: self._handle_task_audio(task, audio)
                        )
                    task.finished.set()
                elif event == "result-generated" and self._task_is_current(task):
                    output = data.get("payload", {}).get("output", {})
                    tts_text = extract_sentence_start_text(data)
                    if tts_text:
                        tts_text = self._restore_original_text(tts_text)
                    elif not output.get("type") and not task.legacy_subtitle_sent:
                        tts_text = task.text
                        task.legacy_subtitle_sent = bool(tts_text)
                    if tts_text:
                        logger.bind(tag=TAG).info(f"句子语音生成成功： {tts_text}")
                        self.tts_audio_queue.put(
                            (SentenceType.FIRST, [], tts_text, task.sentence_id)
                        )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.bind(tag=TAG).error(f"处理TTS响应时出错: {e}")
            self._invalidate_connection(ws, e)
        finally:
            self._invalidate_connection(ws, RuntimeError("TTS连接已关闭"))
            if self._monitor_task is asyncio.current_task():
                self._monitor_task = None
            self._schedule_socket_close(ws)

    def audio_to_opus_data_stream(
        self, audio_file_path, callback: Callable[[Any], Any] = None
    ):
        """重写父类方法：使用独立的临时编码器处理音频文件，避免与TTS流式编码器并发冲突。
        双流式TTS中，monitor任务在event loop线程接收TTS音频并使用self.opus_encoder编码，
        同时tts_text_priority_thread处理音乐文件也使用self.opus_encoder，
        共享的encoder.buffer非线程安全，并发访问会导致SILK resampler断言失败。
        """
        from core.utils.util import audio_to_data_stream

        return audio_to_data_stream(
            audio_file_path,
            is_opus=True,
            callback=callback,
            sample_rate=self.conn.sample_rate,
            opus_encoder=None,
        )

    def to_tts(self, text: str) -> list:
        """非流式生成音频数据，用于生成音频及测试场景"""
        try:
            # 创建事件循环
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            # 生成会话ID
            session_id = uuid.uuid4().hex
            # 存储音频数据
            audio_data = []

            async def _generate_audio():
                ws = await websockets.connect(
                    self.ws_url,
                    additional_headers=self.header,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=10,
                    max_size=10 * 1024 * 1024,
                    **self.ws_connect_options,
                )

                try:
                    # 发送run-task消息启动会话
                    run_task_message = {
                        "header": {
                            "action": "run-task",
                            "task_id": session_id,
                            "streaming": "duplex",
                        },
                        "payload": {
                            "task_group": "audio",
                            "task": "tts",
                            "function": "SpeechSynthesizer",
                            "model": self.model,
                            "parameters": {
                                "text_type": "PlainText",
                                "voice": self.voice,
                                "format": self.format,
                                "sample_rate": self.conn.sample_rate,
                                "volume": self.volume,
                                "rate": self.rate,
                                "pitch": self.pitch,
                            },
                            "input": {}
                        },
                    }
                    await ws.send(json.dumps(run_task_message))

                    # 等待任务启动
                    task_started = False
                    while not task_started:
                        msg = await ws.recv()
                        if isinstance(msg, str):
                            data = json.loads(msg)
                            header = data.get("header", {})
                            if header.get("event") == "task-started":
                                task_started = True
                                logger.bind(tag=TAG).debug("TTS任务已启动")
                            elif header.get("event") == "task-failed":
                                error_code = header.get("error_code", "unknown")
                                error_message = header.get("error_message", "未知错误")
                                raise Exception(
                                    f"启动任务失败: {error_code} - {error_message}"
                                )

                    # 发送文本
                    filtered_text = MarkdownCleaner.clean_markdown(text)
                    if self._correct_words_pattern:
                        filtered_text = self._correct_words_pattern.sub(lambda m: self.correct_words[m.group(0)], filtered_text)
                    # 发送continue-task消息
                    continue_task_message = {
                        "header": {
                            "action": "continue-task",
                            "task_id": session_id,
                            "streaming": "duplex",
                        },
                        "payload": {"input": {"text": filtered_text}},
                    }
                    await ws.send(json.dumps(continue_task_message))

                    # 发送finish-task消息
                    finish_task_message = {
                        "header": {
                            "action": "finish-task",
                            "task_id": session_id,
                            "streaming": "duplex",
                        },
                        "payload": {
                            "input": {}
                        }
                    }
                    await ws.send(json.dumps(finish_task_message))

                    # 接收音频数据
                    task_finished = False
                    while not task_finished:
                        msg = await ws.recv()
                        if isinstance(msg, (bytes, bytearray)):
                            self.opus_encoder.encode_pcm_to_opus_stream(
                                msg,
                                end_of_stream=False,
                                callback=lambda opus: audio_data.append(opus)
                            )
                        elif isinstance(msg, str):
                            data = json.loads(msg)
                            header = data.get("header", {})
                            if header.get("event") == "task-finished":
                                task_finished = True
                                logger.bind(tag=TAG).debug("TTS任务完成")
                            elif header.get("event") == "task-failed":
                                error_code = header.get("error_code", "unknown")
                                error_message = header.get("error_message", "未知错误")
                                raise Exception(
                                    f"合成失败: {error_code} - {error_message}"
                                )

                finally:
                    # 清理资源
                    try:
                        await ws.close()
                    except:
                        pass

            # 运行异步任务
            loop.run_until_complete(_generate_audio())
            loop.close()

            return audio_data

        except Exception as e:
            logger.bind(tag=TAG).error(f"生成音频数据失败: {str(e)}")
            return []
