import asyncio
import hmac
import json
import logging
import os
import functools
import threading
from urllib.parse import urlparse

from aiohttp import web

from .client import ManagementClient
from .models import GuideError, mac_address
from .runtime import GuideRuntime


log = logging.getLogger(__name__)
chat_lock = threading.Lock()


def create_runtime(server):
    if os.environ.get("KSZ_GUIDE_ENABLED", "0").lower() not in {"1", "true", "yes"}:
        return None
    base_url = os.environ.get("KSZ_MANAGEMENT_API_URL", "").rstrip("/")
    parsed = urlparse(base_url)
    token = os.environ.get("KSZ_MANAGEMENT_API_TOKEN", "")
    control_token = os.environ.get("KSZ_GUIDE_CONTROL_TOKEN", "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("KSZ_MANAGEMENT_API_URL must be an HTTP(S) integration API URL")
    if not token or len(control_token) < 32:
        raise ValueError("KSZ guide requires a management token and a control token of at least 32 characters")
    runtime = GuideRuntime(ManagementClient(base_url, token), server)
    runtime.control_token = control_token
    return runtime


def runtime_for(conn):
    runtime = getattr(getattr(conn, "server", None), "guide_runtime", None)
    return runtime if isinstance(runtime, GuideRuntime) else None


async def close_runtime(server):
    runtime = getattr(server, "guide_runtime", None)
    if isinstance(runtime, GuideRuntime):
        await runtime.close()


def register_routes(app, server):
    runtime = getattr(server, "guide_runtime", None)
    if runtime is None:
        return

    async def control(request):
        expected = f"Bearer {runtime.control_token}"
        if not hmac.compare_digest(request.headers.get("Authorization", ""), expected):
            raise web.HTTPUnauthorized()
        if request.content_length is not None and request.content_length > 65536:
            raise web.HTTPRequestEntityTooLarge(max_size=65536, actual_size=request.content_length)
        try:
            data = await request.json()
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

    async def start(application):
        runtime.maintenance_task = asyncio.create_task(runtime.run())

    async def stop(application):
        await runtime.close()

    app.router.add_post("/internal/ksz/guide/control", control)
    app.on_startup.append(start)
    app.on_cleanup.append(stop)


async def on_hello(conn, message):
    runtime = runtime_for(conn)
    if runtime is None:
        return True
    try:
        accepted = await runtime.bind(conn, message.get("guide"))
    except GuideError:
        accepted = False
    if not accepted:
        await conn.websocket.close()
    return accepted


def on_disconnect(conn):
    runtime = runtime_for(conn)
    if runtime:
        runtime.unbind(conn)


async def handle_control_message(conn, message):
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


async def handle_legacy_event(conn, message):
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
    if runtime is None:
        return values
    return validated_context(runtime, conn.device_id, values)


def validated_context(runtime, device_mac, values):
    result = dict(values)
    for key in ("last_beacon_id", "venue_id", "beacon_mac", "guide_policy_revision"):
        result.pop(key, None)
    result.update(runtime.context(device_mac))
    return result


def vision_context(server, device_mac, values):
    runtime = getattr(server, "guide_runtime", None)
    return validated_context(runtime, device_mac, values) if isinstance(runtime, GuideRuntime) else values


def extend_vision_payload(payload, kwargs):
    for key in ("venue_id", "beacon_mac", "guide_policy_revision"):
        if kwargs.get(key) is not None:
            payload[key] = kwargs[key]
    return payload


def track_chat(function):
    @functools.wraps(function)
    def tracked(conn, *args, **kwargs):
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
