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
from plugins_func.register import Action, ActionResponse
from core.utils.dialogue import Message

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
from .prompt import (
    LANGUAGE_NAMES, configured_identity, prepare_system_prompt, prompt_template_path,
    response_language, venue_prompt,
)
from .prompt_owner import agent_owned, configured_context_turns, internal_turn, request_messages, trim_connection
from .role_patch import handle_profile_change, handle_role_rebind, on_hello as role_on_hello
from .runtime import GuideRuntime
from .search_followup import edition_search_intent
from .settings import GuideSettings
from .vision import vision_message, vision_read_timeout
from .intent_embedding import EmbeddingIntentClassifier, explicit_tool_name, external_museum_query
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
    runtime = runtime_for(conn)
    venue = runtime.context(conn.device_id).get("venue_context") if runtime else None
    external = runtime and external_museum_query(text, venue)
    if not external and (classifier is None or not classifier.configured):
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
            available.discard("web_search")
        except Exception:
            pass
    try:
        reply_language = response_language(conn)
        language = {
            "zh-CN": "zh_CN", "en": "en_US", "ja": "ja_JP", "ko": "ko_KR",
        }.get(reply_language, "zh_CN")
        if re.search(
            r"(?:刚才|之前|上一轮).{0,12}(?:聊了什么|聊过什么|说了什么|问了什么|问过什么|谈了什么)"
            r"|what (?:did (?:we|i)|were we) (?:talk(?:ing)? about|discuss(?:ing)?|ask)"
            r"|さっき何を話|아까 무슨 얘기",
            text, re.IGNORECASE,
        ) and explicit_tool_name(text) is None:
            conn.ksz_edition_clarification = None
            result = {"route": "direct_answer", "confidence": 1.0, "margin": 1.0}
        elif "yuanbao_search" in available and (
            edition_result := await edition_search_intent(conn, classifier, text, reply_language)
        ) is not None:
            route = "yuanbao_search" if "function_call" in edition_result else "search_clarification"
            conn.logger.bind(tag=__name__).info(f"活动届次追问路由: {route}")
            return json.dumps(edition_result, ensure_ascii=False)
        elif external and not explicit_tool_name(text):
            search_tool = "yuanbao_search" if "yuanbao_search" in available else None
            result = {"function_call": {"name": search_tool, "arguments": {"query": text}}} if search_tool else None
        elif classifier is not None:
            result = await classifier.classify(text, available, language)
        else:
            result = None
        if result and result.get("route") == "direct_answer":
            conn.ksz_route = "direct_answer"
            conn.ksz_route_version = 1
            conn.ksz_route_confidence = result.get("confidence", 0.0)
            conn.ksz_route_margin = result.get("margin", 0.0)
            result = {
                "function_call": {"name": "continue_chat", "arguments": "{}"},
                **result,
            }
        if result and result.pop("nearby", False):
            _, city = location_context(conn)
            if city:
                result["function_call"]["arguments"]["query"] = f"{city} {text}"
            else:
                result["function_call"]["arguments"]["query"] = ""
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


async def execute_embedding_tool(conn, call):
    handler = conn.func_handler
    if call["name"] != "yuanbao_search":
        return await handler.handle_llm_function_call(conn, call)
    if not handler.has_tool("yuanbao_search"):
        return ActionResponse(Action.ERROR, response="元宝搜索工具未配置")
    if not json.loads(call["arguments"]).get("query"):
        return ActionResponse(Action.RESPONSE, response="您想搜索什么？")
    return await handler.handle_llm_function_call(conn, call)


def tool_reply_unavailable(conn):
    return {
        "en": "I couldn't summarize the results. Please try again.",
        "ja": "検索結果をまとめられませんでした。もう一度お試しください。",
        "ko": "검색 결과를 정리할 수 없습니다. 다시 시도해 주세요.",
    }.get(response_language(conn), "暂时无法整理查询结果，请稍后再试。")


def answer_embedding_tool(conn, call, question, evidence):
    conn.dialogue.put(Message(role="assistant", tool_calls=[{
        "id": call["id"], "type": "function",
        "function": {"name": call["name"], "arguments": call["arguments"]},
    }]))
    conn.dialogue.put(Message(
        role="tool", tool_call_id=call["id"],
        content=evidence if isinstance(evidence, str) else json.dumps(evidence, ensure_ascii=False),
    ))
    trim_connection(conn)
    messages, extra_body = conn._prepare_llm_request(question, conn.sentence_id, depth=5)
    return "".join(conn.llm.response(
        conn.session_id, messages, extra_body=extra_body, **response_options(conn),
    )) or None


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
    options = {"max_tokens": settings.narration_max_tokens}
    if getattr(type(getattr(conn, "llm", None)), "supports_rag_display", False):
        sentence_id = conn.sentence_id

        async def show_rag():
            from core.handle.sendAudioHandle import send_display_message

            if conn.sentence_id == sentence_id and not conn.client_abort:
                await send_display_message(conn, "% RAG")

        options["on_rag"] = lambda: asyncio.run_coroutine_threadsafe(show_rag(), conn.loop)
    return options


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
    result = validated_context(runtime, conn.device_id, values)
    for key in ("ksz_route", "ksz_route_version", "ksz_route_confidence", "ksz_route_margin"):
        value = getattr(conn, key, None)
        if value is not None:
            result[key] = value
            setattr(conn, key, None)
    return result


def validated_context(runtime, device_mac, values):
    result = dict(values)
    for key in ("device_mode", "last_beacon_id", "venue_id", "venue_context", "beacon_mac", "beacon_museum_code", "guide_policy_revision"):
        result.pop(key, None)
    result.update(runtime.context(device_mac))
    return result


def vision_context(server, device_mac, values):
    runtime = getattr(server, "guide_runtime", None)
    result = validated_context(runtime, device_mac, values) if isinstance(runtime, GuideRuntime) and runtime.guide_enabled else values
    find = getattr(server, "find_device_connection", None)
    conn = find({"device_id": device_mac}) if callable(find) else None
    location = location_value(conn) if conn is not None else None
    if conn is not None and (identity := configured_identity(conn)):
        result = {**result, **identity}
    if "device_location" in result or location is not None:
        result = dict(result)
        result.pop("device_location", None)
        if location is not None:
            result["device_location"] = location
    return result


def extend_vision_payload(payload, kwargs):
    for key in ("ksz_prompt_context", "device_mode", "venue_id", "venue_context", "beacon_mac", "guide_policy_revision",
                "device_location", "session_id", "image_id", "image_seq", "captured_at",
                "agent_name", "assistant_name"):
        if kwargs.get(key) is not None:
            payload[key] = kwargs[key]
    if kwargs.get("history"):
        payload["messages"] = [*kwargs["history"], *payload["messages"]]
    if kwargs.get("ksz_prompt_context") is not None:
        return payload
    context = venue_prompt(
        "", kwargs.get("venue_context"), kwargs.get("device_mode"),
        assistant_name=kwargs.get("assistant_name"),
    )
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
