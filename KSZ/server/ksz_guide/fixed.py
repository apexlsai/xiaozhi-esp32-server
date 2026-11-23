import asyncio
import json
import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse
from uuid import uuid4

import aiohttp
from loguru import logger

from .models import GuideError
from .playback import begin_response, interrupt_response, playback_state, record_response_text
from .presence import connection_is_open
from .prompt import response_language


log = logger.bind(tag=__name__)
LANGUAGES = {"zh-CN": "zh-CN", "en": "en-US", "ja": "ja-JP", "ko": "ko-KR"}
ROLES = {"青年": "youth", "儿童": "child", "专家": "expert"}
RESUME = re.compile(r"^(?:(?:请|帮我|麻烦|可以|你可以)?(?:继续(?:讲解|介绍|播放|刚才的讲解|刚才的介绍)?|接着(?:讲|讲解|说|播放))(?:(?:吧|吗|一下))?|(?:please)?(?:continue|resume)(?:the)?(?:guide|tour|narration|explanation)?|続けて(?:ください)?|계속(?:설명해|재생해)?(?:주세요)?)$", re.I)


def profile(conn):
    attributes = getattr(conn, "device_attributes", None) or {}
    name = attributes.get("agent_name") or getattr(conn, "agent_name", "") or (getattr(conn, "config", None) or {}).get("agent_name", "")
    role = attributes.get("apexls_role") or next(
        (value for prefix, value in ROLES.items() if str(name).startswith(prefix)), attributes.get("role")
    )
    language = LANGUAGES.get(response_language(conn))
    if role not in ROLES.values() or language is None:
        raise GuideError("fixed_profile_unavailable")
    return role, language


class FixedClient:
    def __init__(self, base_url, token):
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path.rstrip("/"):
            raise GuideError("fixed_rag_url_invalid")
        if not token:
            raise GuideError("fixed_rag_token_missing")
        self.base_url = base_url.rstrip("/")
        self.token = token

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("KSZ_RAG_API_URL", ""), os.environ.get("KSZ_RAG_API_KEY", ""))

    async def request(self, path, *, params=None, audio=False):
        if not path.startswith("/api/v1/guides") or path.startswith("//"):
            raise GuideError("fixed_audio_url_invalid")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
                async with session.get(self.base_url + path, params=params, headers={"Authorization": f"Bearer {self.token}"}, allow_redirects=False) as response:
                    if response.status != 200:
                        raise GuideError(f"fixed_rag_http_{response.status}")
                    limit = 20 * 1024 * 1024 if audio else 4 * 1024 * 1024
                    content = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        content.extend(chunk)
                        if len(content) > limit:
                            raise GuideError("fixed_response_too_large")
                    if audio:
                        kind = response.headers.get("Content-Type", "").split(";")[0]
                        formats = {"audio/wav": "wav", "audio/mpeg": "mp3", "audio/ogg": "ogg"}
                        if kind not in formats or not content:
                            raise GuideError("fixed_audio_invalid")
                        return bytes(content), formats[kind]
                    return json.loads(content)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            raise GuideError("fixed_rag_unavailable") from error

    async def guide(self, beacon, role, language):
        data = await self.request("/api/v1/guides", params={"museum_id": beacon.museum_code, "place_id": beacon.place_id, "role": role, "language": language})
        if (not isinstance(data, dict) or data.get("museum_id") != beacon.museum_code or data.get("place_id") != beacon.place_id
                or data.get("role") != role or data.get("language") != language):
            raise GuideError("fixed_guide_identity_mismatch")
        segments = data.get("segments")
        if not isinstance(segments, list) or not 1 <= len(segments) <= 500:
            raise GuideError("fixed_segments_invalid")
        for segment in segments:
            if (not isinstance(segment, dict) or not isinstance(segment.get("text"), str)
                    or not segment["text"].strip() or len(segment["text"]) > 5000
                    or not isinstance(segment.get("audio_id"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", segment.get("audio_id", ""))):
                raise GuideError("fixed_segments_invalid")
        return data

    async def audio(self, beacon, segment):
        return await self.request("/api/v1/guides/audio/" + segment["audio_id"], params={"museum_id": beacon.museum_code}, audio=True)


@dataclass
class FixedTour:
    key: tuple
    guide: dict
    client: FixedClient
    next_segment: int = 0
    task: asyncio.Task | None = None
    response_id: str = ""


def tour_key(conn, beacon):
    return conn.session_id, id(conn.websocket), beacon.zone, *profile(conn)


def encode_audio(content, kind, conn):
    from core.utils.util import audio_bytes_to_data_stream
    frames = []
    audio_bytes_to_data_stream(content, kind, conn.audio_format == "opus", frames.append, sample_rate=conn.sample_rate)
    if not frames:
        raise GuideError("fixed_audio_empty")
    return frames


async def stop_fixed(conn):
    tour = getattr(conn, "ksz_fixed_tour", None)
    task = tour.task if tour else None
    if task and task is not asyncio.current_task() and not task.done():
        from core.handle.abortHandle import handleAbortMessage
        if conn.sentence_id == tour.response_id:
            await handleAbortMessage(conn)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def play_fixed(runtime, conn, beacon, *, resume=False, query=None):
    from core.handle.sendAudioHandle import sendAudioMessage, send_tts_message
    from core.providers.tts.dto.dto import SentenceType
    from core.utils.dialogue import Message
    from core.handle.reportHandle import enqueue_tts_report
    from core.utils.output_counter import add_device_output, check_device_output_limit

    if beacon.guide_mode != "fixed":
        return False
    limit = getattr(conn, "max_output_size", 0)
    if limit > 0 and check_device_output_limit(conn.device_id, limit):
        return False
    key = tour_key(conn, beacon)
    previous_sentence = conn.sentence_id
    previous_socket = conn.websocket
    tour = getattr(conn, "ksz_fixed_tour", None)
    if not resume:
        client = FixedClient.from_env()
        data = await client.guide(beacon, *key[-2:])
        current = runtime.location(conn.device_id)
        if (conn.sentence_id != previous_sentence or conn.websocket is not previous_socket or current is None
                or current.guide_mode != "fixed" or tour_key(conn, current) != key):
            return False
        tour = conn.ksz_fixed_tour = FixedTour(key, data, client)
        log.info("Fixed guide selected: museum={} place={} role={} language={} segments={}",
                 beacon.museum_code, beacon.place_id, *key[-2:], len(data["segments"]))
    elif tour is None or tour.key != key or tour.next_segment >= len(tour.guide["segments"]):
        return False
    state = runtime.devices.get(conn.device_id.upper())
    if state is None or state.owner is not conn:
        return False
    await stop_fixed(conn)
    tour.task = asyncio.current_task()
    sentence_id = uuid4().hex
    tour.response_id = sentence_id
    conn.sentence_id = sentence_id
    socket = conn.websocket

    invalid_logged = None

    def valid():
        nonlocal invalid_logged
        current = runtime.location(conn.device_id)
        if current is None:
            reason = "position_expired"
        elif state is None or state.owner is not conn:
            reason = "connection_replaced"
        elif current.guide_mode != "fixed":
            reason = "guide_disabled"
        elif current.zone != beacon.zone or tour_key(conn, current) != key:
            reason = "beacon_changed"
        elif conn.sentence_id != sentence_id:
            reason = "client_abort"
        elif conn.websocket is not socket or not connection_is_open(conn):
            reason = "connection_closed"
        elif conn.client_abort:
            reason = "client_abort"
        else:
            reason = None
        if reason and reason != invalid_logged:
            invalid_logged = reason
            log.info("Fixed guide stopped: device={} place={} segment={} reason={}",
                     conn.device_id, beacon.place_id, tour.next_segment + 1, reason)
        return reason is None

    conn.client_abort = False
    conn.client_is_speaking = True
    conn.last_activity_time = runtime.wall_clock() * 1000
    if state:
        state.announcement_sentence_id = sentence_id
    begin_response(conn, sentence_id, query or "固定讲解")
    if query:
        conn.dialogue.put(Message(role="user", content=query))
    stopped = True
    try:
        while tour.next_segment < len(tour.guide["segments"]):
            if not valid():
                return False
            limit = getattr(conn, "max_output_size", 0)
            if limit > 0 and check_device_output_limit(conn.device_id, limit):
                return False
            segment = tour.guide["segments"][tour.next_segment]
            content, kind = await tour.client.audio(beacon, segment)
            frames = await asyncio.to_thread(encode_audio, content, kind, conn)
            if not valid():
                return False
            stopped = False
            await send_tts_message(conn, "start")
            record_response_text(conn, segment["text"], sentence_id)
            await sendAudioMessage(conn, SentenceType.FIRST, frames, segment["text"], sentence_id=sentence_id)
            await send_tts_message(conn, "stop")
            stopped = True
            if valid():
                conn.client_is_speaking = True
            if (getattr(conn, "features", None) or {}).get("playback_progress") is True:
                playback = playback_state(conn)
                reply = playback.replies[sentence_id]
                deadline = asyncio.get_running_loop().time() + len(frames) * .06 + 10
                while valid() and not reply.interrupted and reply.played_through < len(reply.segments):
                    if asyncio.get_running_loop().time() >= deadline:
                        raise GuideError("fixed_playback_confirmation_timeout")
                    await asyncio.sleep(.05)
                if reply.interrupted:
                    return False
            if not valid():
                return False
            conn.dialogue.put(Message(role="assistant", content=segment["text"]))
            add_device_output(conn.device_id, len(segment["text"]))
            enqueue_tts_report(conn, segment["text"], frames)
            tour.next_segment += 1
            if tour.next_segment < len(tour.guide["segments"]):
                sentence_id = uuid4().hex
                tour.response_id = conn.sentence_id = state.announcement_sentence_id = sentence_id
                begin_response(conn, sentence_id, query or "固定讲解")
        return True
    finally:
        try:
            if conn.sentence_id == sentence_id and conn.websocket is socket:
                if tour.next_segment < len(tour.guide["segments"]):
                    interrupt_response(conn, "fixed_playback_interrupted")
                try:
                    if not stopped:
                        await send_tts_message(conn, "stop")
                finally:
                    conn.client_is_speaking = False
        finally:
            if tour.task is asyncio.current_task():
                tour.task = None


def resume_fixed_chat(conn, query):
    if not isinstance(query, str) or not RESUME.fullmatch(re.sub(r"[\s，。！？,.!?]", "", query)):
        return False
    tour = getattr(conn, "ksz_fixed_tour", None)
    if tour is None or tour.next_segment >= len(tour.guide["segments"]):
        return False
    from .integration import runtime_for
    runtime = runtime_for(conn)
    beacon = runtime.location(conn.device_id) if runtime else None
    if beacon is None or beacon.guide_mode != "fixed":
        return False
    try:
        if tour.key != tour_key(conn, beacon):
            return False
    except GuideError:
        return False
    async def resume():
        try:
            return await play_fixed(runtime, conn, beacon, resume=True, query=query)
        except Exception as error:
            state = runtime.devices.get(conn.device_id.upper())
            if state:
                state.rejection = str(error) if isinstance(error, GuideError) else "fixed_playback_failed"
            log.warning("Fixed guide resume unavailable: reason={} museum={} place={} rag_fallback={}",
                        state.rejection if state else type(error).__name__, beacon.museum_code,
                        beacon.place_id, beacon.fixed_rag_fallback)
            current = runtime.location(conn.device_id)
            if beacon.fixed_rag_fallback and not conn.client_abort and current is not None and current.zone == beacon.zone and current.guide_mode == "fixed":
                return await runtime._announce_rag(conn, current)
            return False

    future = asyncio.run_coroutine_threadsafe(resume(), conn.loop)
    try:
        future.result()
    except Exception:
        log.exception("Fixed guide resume failed")
    return True
