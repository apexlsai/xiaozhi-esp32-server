import time
from typing import Any

from loguru import logger

from core.utils.language import (
    LANGUAGE_AGENT_SUFFIXES,
    resolve_language_code_from_agent_name,
)


TAG = __name__

ROLE_AGENT_PREFIXES = {
    "child": "儿童",
    "youth": "青年",
    "expert": "专家",
}
PROFILE_LANGUAGES = {"zh-CN", "en", "ja", "ko"}

_LEGACY_FIELDS = {
    "current_agent_name",
    "currentAgentName",
    "target_agent_name",
    "targetAgentName",
    "confirm",
}
_pending_reconnects = {}


def resolve_role_agent_name(current_agent_name: Any, language: Any, role: Any):
    if not isinstance(current_agent_name, str) or not current_agent_name:
        return None
    if not isinstance(language, str) or language not in LANGUAGE_AGENT_SUFFIXES:
        return None
    prefix = ROLE_AGENT_PREFIXES.get(role)
    if prefix is None:
        return None
    suffix = LANGUAGE_AGENT_SUFFIXES[language]
    test_suffix = "-测试" if current_agent_name.endswith("-测试") else ""
    return f"{prefix}-{suffix}{test_suffix}"


def _request_id(message, payload):
    return message.get("request_id") or payload.get("request_id")


async def _send_error(conn, event, request_id, error):
    from core.handle.deviceEventHandle import _send_json

    result = {
        "type": "device_event_result",
        "event": event,
        "success": False,
        "error": error,
    }
    if request_id is not None:
        result["request_id"] = request_id
    await _send_json(conn, result)


async def _send_noop(conn, request_id, role, language, current_agent_name):
    from core.handle.deviceEventHandle import _send_json

    result = {
        "type": "device_event_result",
        "event": "agent_rebind",
        "success": True,
        "data": {
            "deviceId": conn.device_id,
            "role": role,
            "language": language,
            "previousAgentName": current_agent_name,
            "agentName": current_agent_name,
            "confirmed": True,
            "reconnectRequired": False,
        },
    }
    if request_id is not None:
        result["request_id"] = request_id
    await _send_json(conn, result)


async def handle_role_rebind(conn, message):
    if message.get("event") != "agent_rebind":
        return False
    payload = message.get("payload")
    if not isinstance(payload, dict) or "role" not in payload:
        return False

    request_id = _request_id(message, payload)
    role = payload.get("role")
    logger.bind(tag=TAG).info(
        f"KSZ角色换绑收到: device={conn.device_id} request_id={request_id} role={role}"
    )

    if any(field in payload for field in _LEGACY_FIELDS):
        await _send_error(conn, "agent_rebind", request_id, "role 不能与旧换绑字段同时使用")
        return True
    if not isinstance(role, str) or role not in ROLE_AGENT_PREFIXES:
        await _send_error(
            conn,
            "agent_rebind",
            request_id,
            f"role 必须是: {', '.join(ROLE_AGENT_PREFIXES)}",
        )
        return True

    attributes = conn.device_attributes or {}
    current_agent_name = attributes.get("agent_name")
    if not isinstance(current_agent_name, str) or not current_agent_name:
        await _send_error(conn, "agent_rebind", request_id, "无法获取设备当前绑定智能体")
        return True

    language = attributes.get("language")
    if not isinstance(language, str) or language not in LANGUAGE_AGENT_SUFFIXES:
        language = resolve_language_code_from_agent_name(current_agent_name)
    target_agent_name = resolve_role_agent_name(current_agent_name, language, role)
    if target_agent_name is None:
        await _send_error(conn, "agent_rebind", request_id, "无法从当前智能体推导角色对应语言")
        return True

    logger.bind(tag=TAG).info(
        f"KSZ角色换绑解析: device={conn.device_id} request_id={request_id} "
        f"language={language} current={current_agent_name} target={target_agent_name}"
    )
    if target_agent_name == current_agent_name:
        await _send_noop(conn, request_id, role, language, current_agent_name)
        logger.bind(tag=TAG).info(
            f"KSZ角色换绑无需变更: device={conn.device_id} request_id={request_id}"
        )
        return True

    from core.handle.deviceEventHandle import _do_rebind

    _pending_reconnects[conn.device_id] = {
        "request_id": request_id,
        "target": target_agent_name,
        "created_at": time.monotonic(),
    }
    data = None
    try:
        data = await _do_rebind(
            conn,
            "agent_rebind",
            current_agent_name,
            target_agent_name,
            request_id,
            confirm=True,
        )
    finally:
        if data is None or data.get("reconnectRequired") is not True:
            _pending_reconnects.pop(conn.device_id, None)
    if data is not None and data.get("reconnectRequired") is True:
        logger.bind(tag=TAG).info(
            f"KSZ角色换绑等待重连: device={conn.device_id} request_id={request_id} "
            f"target={target_agent_name}"
        )
    return True


async def handle_profile_change(conn, message):
    if message.get("event") != "profile_change":
        return False
    payload = message.get("payload")
    request_id = _request_id(message, payload if isinstance(payload, dict) else {})
    if not isinstance(payload, dict):
        await _send_error(conn, "profile_change", request_id, "payload 必须是对象")
        return True

    unexpected = set(payload) - {"language", "role", "dev"}
    if unexpected:
        await _send_error(
            conn,
            "profile_change",
            request_id,
            f"profile_change 不支持字段: {', '.join(sorted(unexpected))}",
        )
        return True

    language = payload.get("language")
    role = payload.get("role")
    dev = payload.get("dev", False)
    if not isinstance(language, str) or language not in PROFILE_LANGUAGES:
        await _send_error(
            conn,
            "profile_change",
            request_id,
            f"language 必须是: {', '.join(sorted(PROFILE_LANGUAGES))}",
        )
        return True
    if not isinstance(role, str) or role not in ROLE_AGENT_PREFIXES:
        await _send_error(
            conn,
            "profile_change",
            request_id,
            f"role 必须是: {', '.join(ROLE_AGENT_PREFIXES)}",
        )
        return True
    if not isinstance(dev, bool):
        await _send_error(conn, "profile_change", request_id, "dev 必须是布尔值")
        return True

    current_agent_name = (conn.device_attributes or {}).get("agent_name")
    if not isinstance(current_agent_name, str) or not current_agent_name:
        await _send_error(conn, "profile_change", request_id, "无法获取设备当前绑定智能体")
        return True
    target_agent_name = (
        f"{ROLE_AGENT_PREFIXES[role]}-{LANGUAGE_AGENT_SUFFIXES[language]}"
        f"{'-测试' if dev else ''}"
    )

    from core.handle.deviceEventHandle import _do_rebind

    _pending_reconnects[conn.device_id] = {
        "request_id": request_id,
        "target": target_agent_name,
        "created_at": time.monotonic(),
    }
    data = None
    try:
        data = await _do_rebind(
            conn,
            "profile_change",
            current_agent_name,
            target_agent_name,
            request_id,
            confirm=True,
            language=language,
            dev=dev,
            role=role,
        )
    finally:
        if data is None or data.get("reconnectRequired") is not True:
            _pending_reconnects.pop(conn.device_id, None)
    return True


def on_hello(conn):
    pending = _pending_reconnects.pop(conn.device_id, None)
    if pending is None:
        return
    logger.bind(tag=TAG).info(
        f"KSZ角色换绑重连完成: device={conn.device_id} "
        f"request_id={pending['request_id']} target={pending['target']} "
        f"elapsed_ms={int((time.monotonic() - pending['created_at']) * 1000)}"
    )
