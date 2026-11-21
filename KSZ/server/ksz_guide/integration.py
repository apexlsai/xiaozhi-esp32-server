import asyncio
import hmac
import json
import logging
import os
import functools
import threading
import re
from urllib.parse import urlparse

from aiohttp import web

from .client import ManagementClient
from .announcement import (
    filter_guide_announcement_text, guide_announcement_messages, is_guide_announcement,
    queue_guide_tool_reply, start_guide_announcement,
)
from .feedback import (
    CameraFeedbackScheduler as create_tool_feedback,
    camera_image_received,
    cancel_camera_feedback,
    finish_camera_feedback,
)
from .models import GuideError, mac_address
from .idle_greeting import (
    cancel_idle_greeting, close_idle_greeting, greeting_session,
    handle_idle_greeting, interrupt_idle_greeting,
)
from .prompt import prepare_system_prompt, prompt_template_path, response_language, venue_prompt
from .prompt_owner import agent_owned, internal_turn, request_messages, trim_connection
from .role_patch import handle_profile_change, handle_role_rebind, on_hello as role_on_hello
from .runtime import GuideRuntime
from .settings import GuideSettings
from .vision import vision_message, vision_read_timeout
from .intent_embedding import EmbeddingIntentClassifier
from .visual_context import (
    begin_visual_request, close_visual_context, finish_visual_request,
    ensure_visual_request, prepare_visual_request, visual_chat_context,
    visual_request_payload, visual_state,
)
from .tts import tts_complete_sentence
from .thinking import (
    close_thinking, finish_thinking, finish_thinking_later, send_response_emotion,
    start_thinking, thinking_scope,
)
from .geographic_location import (
    apply_location, close_location, location_context, location_description, location_value,
    start_location, wait_location,
)
from .playback import (
    begin_response, commit_playback_message, handle_playback_progress, interrupt_response,
    playback_context_messages, prepare_playback_message, record_response_delta, record_response_text,
)


log = logging.getLogger(__name__)
chat_lock = threading.Lock()
_INTERNAL_BEACON_TEXT = re.compile(r"^NFC_[A-Za-z0-9_-]{4,}$")


def is_internal_beacon_text(text):
    return bool(_INTERNAL_BEACON_TEXT.fullmatch((text or "").strip()))


def embedding_intent_enabled(conn):
    config = getattr(conn, "config", {}) or {}
    intent_section = config.get("Intent", {})
    intent_config = intent_section.get("embedding", {}) if isinstance(intent_section, dict) else {}
    if not isinstance(intent_config, dict):
        intent_config = {}
    explicit = os.environ.get("KSZ_INTENT_EMBEDDING_ENABLED", "")
    if explicit:
        return explicit.lower() in {"1", "true", "yes", "on"}
    return intent_config.get("enabled") is True or bool(
        intent_config.get("base_url")
        or os.environ.get("KSZ_INTENT_EMBEDDING_BASE_URL")
        or os.environ.get("EMBEDDING_BASE_URL")
    )


def _embedding_classifier(conn):
    if not embedding_intent_enabled(conn):
        return None
    config = getattr(conn, "config", {}) or {}
    intent_section = config.get("Intent", {})
    intent_config = intent_section.get("embedding", {}) if isinstance(intent_section, dict) else {}
    signature = repr(intent_config) + os.environ.get("KSZ_INTENT_EMBEDDING_BASE_URL", "")
    current = getattr(conn, "ksz_embedding_intent", None)
    if current is None or getattr(conn, "ksz_embedding_intent_signature", None) != signature:
        current = EmbeddingIntentClassifier(intent_config)
        conn.ksz_embedding_intent = current
        conn.ksz_embedding_intent_signature = signature
    return current


async def classify_embedding_intent(conn, text):
    classifier = _embedding_classifier(conn)
    if classifier is None or not classifier.configured:
        return None
    handler = getattr(conn, "func_handler", None)
    available = set()
    if handler is not None:
        try:
            deadline = asyncio.get_running_loop().time() + 3
            while not getattr(handler, "finish_init", True) and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
            available.update(
                item.get("function", {}).get("name")
                for item in handler.get_functions()
                if isinstance(item, dict)
            )
        except Exception:
            pass
    try:
        language = {
            "zh-CN": "zh_CN", "en-US": "en_US", "ja-JP": "ja_JP", "ko-KR": "ko_KR",
        }.get(response_language(conn), "zh_CN")
        result = await classifier.classify(text, available, language)
    except Exception as error:
        conn.logger.bind(tag=__name__).warning(
            f"Embedding 意图识别失败，回退普通生成: {type(error).__name__}: {error}"
        )
        return None
    if result is not None:
        conn.logger.bind(tag=__name__).info(
            f"Embedding 意图识别结果: {result.get('function_call', {}).get('name')}"
        )
    return json.dumps(result, ensure_ascii=False) if result is not None else None


def create_runtime(server):
    guide_enabled = os.environ.get("KSZ_GUIDE_ENABLED", "0").lower() in {"1", "true", "yes"}
    presence_enabled = os.environ.get("KSZ_GUIDE_PRESENCE_ENABLED", "0").lower() in {"1", "true", "yes"}
    if not guide_enabled and not presence_enabled:
        return None
    base_url = os.environ.get("KSZ_MANAGEMENT_API_URL", "").rstrip("/")
    parsed = urlparse(base_url)
    token = os.environ.get("KSZ_MANAGEMENT_API_TOKEN", "")
    control_token = os.environ.get("KSZ_GUIDE_CONTROL_TOKEN", "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("KSZ_MANAGEMENT_API_URL must be an HTTP(S) integration API URL")
    if not token or len(control_token) < 32:
        raise ValueError("KSZ guide requires a management token and a control token of at least 32 characters")
    environment = os.environ.get("KSZ_DEVICE_ENVIRONMENT")
    if environment not in {"museum", "showroom"}:
        raise ValueError("KSZ_DEVICE_ENVIRONMENT must be museum or showroom")
    settings = GuideSettings.from_env()
    client = ManagementClient(base_url, token, environment=environment,
                              metadata_config=getattr(server, "config", {}).get("manager-api"))
    runtime = GuideRuntime(client, server, guide_enabled=guide_enabled, settings=settings)
    runtime.control_token = control_token
    return runtime


def runtime_for(conn, *, guide_only=True):
    runtime = getattr(getattr(conn, "server", None), "guide_runtime", None)
    return runtime if isinstance(runtime, GuideRuntime) and (runtime.guide_enabled or not guide_only) else None


async def close_runtime(server):
    runtime = getattr(server, "guide_runtime", None)
    if isinstance(runtime, GuideRuntime):
        await runtime.close()


def register_routes(app, server):
    runtime = getattr(server, "guide_runtime", None)
    if runtime is None:
        return

    async def authenticated_body(request):
        expected = f"Bearer {runtime.control_token}"
        if not hmac.compare_digest(request.headers.get("Authorization", ""), expected):
            raise web.HTTPUnauthorized()
        if request.content_length is not None and request.content_length > 65536:
            raise web.HTTPRequestEntityTooLarge(max_size=65536, actual_size=request.content_length)
        body = bytearray()
        async for chunk in request.content.iter_chunked(65536):
            body.extend(chunk)
            if len(body) > 65536:
                raise web.HTTPRequestEntityTooLarge(max_size=65536, actual_size=len(body))
        return json.loads(body)

    async def control(request):
        try:
            data = await authenticated_body(request)
            if not isinstance(data, dict) or data.get("transport") != "mqtt":
                raise GuideError("invalid_transport")
            connection_id = data.get("connection_id")
            if not isinstance(connection_id, str) or not 1 <= len(connection_id) <= 128:
                raise GuideError("invalid_connection_id")
            result = await runtime.process(data.get("device_mac"), connection_id, data.get("message"),
                                           transport="mqtt", udp_ready=data.get("udp_ready") is True)
            return web.json_response(result)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text="invalid_json") from None
        except GuideError as error:
            raise web.HTTPBadRequest(text=str(error)) from None

    async def presence(request):
        try:
            return web.json_response(runtime.receive_presence(await authenticated_body(request)))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text="invalid_json") from None
        except GuideError as error:
            raise web.HTTPBadRequest(text=str(error)) from None

    async def start(application):
        runtime.maintenance_task = asyncio.create_task(runtime.run())

    async def stop(application):
        await runtime.close()

    if runtime.guide_enabled:
        app.router.add_post("/internal/ksz/guide/control", control)
    app.router.add_post("/internal/ksz/guide/presence", presence)
    app.on_startup.append(start)
    app.on_cleanup.append(stop)


async def on_hello(conn, message):
    role_on_hello(conn)
    runtime = runtime_for(conn)
    if runtime is None:
        await wait_location(conn)
        return True
    try:
        accepted, _ = await asyncio.gather(runtime.bind(conn, message.get("guide")), wait_location(conn))
    except GuideError:
        accepted = False
    if not accepted:
        await conn.websocket.close()
    else:
        apply_location(conn)
    return accepted


def on_connect(conn):
    start_location(conn)
    greeting_session(conn)
    visual_state(conn)
    runtime = runtime_for(conn, guide_only=False)
    if runtime:
        runtime.connection_opened(conn)


def on_hello_complete(conn):
    greeting_session(conn).ready = True
    runtime = runtime_for(conn)
    if runtime:
        runtime.location_updates.start(conn)


def on_disconnect(conn):
    tour = getattr(conn, "ksz_fixed_tour", None)
    if tour and tour.task and not tour.task.done():
        tour.task.cancel()
    close_thinking(conn)
    interrupt_response(conn, "disconnected")
    close_location(conn)
    close_idle_greeting(conn)
    close_visual_context(conn)
    runtime = runtime_for(conn, guide_only=False)
    if runtime:
        runtime.connection_closed(conn)
        if runtime.guide_enabled:
            runtime.unbind(conn)


async def handle_control_message(conn, message):
    if handle_playback_progress(conn, message):
        return True
    await interrupt_idle_greeting(conn, message)
    runtime = runtime_for(conn)
    if runtime is None:
        return False
    if message.get("type") != "guide_control":
        try:
            runtime.activity(conn, message)
        except GuideError:
            pass
        return False
    try:
        if getattr(conn, "conn_from_mqtt_gateway", False):
            raise GuideError("use_mqtt_control_channel")
        result = await runtime.process(conn.device_id, f"ws:{conn.session_id}", message,
                                       transport="websocket", conn=conn)
        for item in result["messages"]:
            await conn.websocket.send(json.dumps(item, ensure_ascii=False))
    except GuideError as error:
        await conn.websocket.send(json.dumps(runtime._result(message, False, str(error))))
    return True


def response_options(conn):
    runtime = runtime_for(conn)
    settings = runtime.settings if runtime else GuideSettings.from_env()
    return {"max_tokens": settings.narration_max_tokens}


async def handle_legacy_event(conn, message):
    if await handle_idle_greeting(conn, message, runtime_for(conn)):
        return True
    if await handle_profile_change(conn, message):
        return True
    if await handle_role_rebind(conn, message):
        return True
    runtime = runtime_for(conn)
    if runtime is None or message.get("event") != "beacon_change":
        return False
    payload = message.get("payload")
    if not isinstance(payload, dict) or not payload.get("beacon_id"):
        payload = message.get("beacon_mac")
    if not isinstance(payload, dict):
        return True
    try:
        await runtime.legacy(conn, payload)
    except GuideError:
        pass
    return True


def refresh_location(conn):
    runtime = runtime_for(conn)
    if runtime is None:
        return False
    conn.beacon_location = runtime.location(conn.device_id)
    return True


def filter_context(conn, values):
    runtime = runtime_for(conn)
    location = location_value(conn)
    if runtime is not None or location is not None:
        apply_location(conn)
    values = visual_chat_context(conn, values)
    values.pop("device_location", None)
    if location is not None:
        values["device_location"] = location
    if runtime is None:
        return values
    return validated_context(runtime, conn.device_id, values)


def validated_context(runtime, device_mac, values):
    result = dict(values)
    for key in ("last_beacon_id", "venue_id", "venue_context", "beacon_mac", "guide_policy_revision"):
        result.pop(key, None)
    result.update(runtime.context(device_mac))
    return result


def vision_context(server, device_mac, values):
    runtime = getattr(server, "guide_runtime", None)
    result = validated_context(runtime, device_mac, values) if isinstance(runtime, GuideRuntime) and runtime.guide_enabled else values
    find = getattr(server, "find_device_connection", None)
    conn = find({"device_id": device_mac}) if callable(find) else None
    location = location_value(conn) if conn is not None else None
    if "device_location" in result or location is not None:
        result = dict(result)
        result.pop("device_location", None)
        if location is not None:
            result["device_location"] = location
    return result


def extend_vision_payload(payload, kwargs):
    for key in ("ksz_prompt_context", "venue_id", "venue_context", "beacon_mac", "guide_policy_revision",
                "device_location", "session_id", "image_id", "image_seq", "captured_at"):
        if kwargs.get(key) is not None:
            payload[key] = kwargs[key]
    if kwargs.get("history"):
        payload["messages"] = [*kwargs["history"], *payload["messages"]]
    if kwargs.get("ksz_prompt_context") is not None:
        return payload
    context = venue_prompt("", kwargs.get("venue_context"))
    if location := kwargs.get("device_location"):
        context += "\n" + location_description(location)
    if context.strip():
        payload["messages"] = [{"role": "system", "content": context.strip()}, *payload["messages"]]
    return payload


def track_chat(function):
    @functools.wraps(function)
    def tracked(conn, *args, **kwargs):
        cancel_idle_greeting(conn)
        if (kwargs.get("depth", args[1] if len(args) > 1 else 0) == 0
                and getattr(conn, "ksz_fixed_tour", None) is not None
                and getattr(conn, "loop", None) is not None):
            from .fixed import resume_fixed_chat, stop_fixed
            asyncio.run_coroutine_threadsafe(stop_fixed(conn), conn.loop).result()
            conn.client_abort = False
            if resume_fixed_chat(conn, args[0] if args else kwargs.get("query")):
                return True
        with thinking_scope():
            if runtime_for(conn) is None:
                return function(conn, *args, **kwargs)
            with chat_lock:
                conn.guide_chat_count = getattr(conn, "guide_chat_count", 0) + 1
            try:
                return function(conn, *args, **kwargs)
            finally:
                with chat_lock:
                    conn.guide_chat_count -= 1
    return tracked
