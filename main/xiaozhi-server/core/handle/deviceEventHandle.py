import asyncio
import json
from typing import Dict, Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from config.manage_api_client import report_device_event, rebind_device_agent
from config.logger import setup_logging
from core.utils.language import (
    LANGUAGE_AGENT_SUFFIXES,
    resolve_language_agent_name,
    resolve_language_code_from_agent_name,
)

TAG = __name__
logger = setup_logging()


async def handle_device_event(conn: "ConnectionHandler", msg_json: Dict[str, Any]):
    """处理设备主动上报的事件"""
    event = msg_json.get("event")
    payload = msg_json.get("payload", {})
    timestamp = msg_json.get("timestamp")
    request_id = msg_json.get("request_id") or (
        payload.get("request_id") if isinstance(payload, dict) else None
    )

    if not event:
        logger.bind(tag=TAG).warning("收到空的 device_event")
        return

    logger.bind(tag=TAG).info(f"设备事件: {event}, payload: {payload}")

    if event == "agent_rebind":
        await _handle_agent_rebind(conn, payload if isinstance(payload, dict) else {}, request_id)
        return

    if event == "language_change":
        await _handle_language_change(conn, payload if isinstance(payload, dict) else {}, request_id)
        return

    if event == "beacon_change":
        if not isinstance(payload, dict):
            payload = {}
        if not payload.get("beacon_id") and isinstance(
            msg_json.get("beacon_mac"), dict
        ):
            payload = msg_json["beacon_mac"]
        beacon_id = payload.get("beacon_id")
        if beacon_id:
            await _handle_beacon_change(conn, beacon_id, payload)
    else:
        logger.bind(tag=TAG).debug(f"未识别的设备事件: {event}")

    # 异步上报到 manager-api 持久化
    asyncio.create_task(
        _report_event_to_manager_api(conn, event, payload, timestamp)
    )


async def _handle_agent_rebind(
    conn: "ConnectionHandler", payload: Dict[str, Any], request_id: Any
):
    """处理设备智能体换绑：解析 payload 后交由 _do_rebind 执行"""
    current_agent_name = payload.get("current_agent_name") or payload.get(
        "currentAgentName"
    )
    target_agent_name = payload.get("target_agent_name") or payload.get(
        "targetAgentName"
    )
    confirm = payload.get("confirm", False)
    if isinstance(confirm, str):
        confirm = confirm.lower() in ("true", "1", "yes")
    await _do_rebind(
        conn, "agent_rebind", current_agent_name, target_agent_name, request_id, confirm
    )


async def _do_rebind(
    conn: "ConnectionHandler",
    event_name: str,
    current_agent_name: Optional[str],
    target_agent_name: Optional[str],
    request_id: Any,
    confirm: bool,
    raise_on_error: bool = False,
    language: Optional[str] = None,
    dev: bool = False,
):
    """换绑核心流程：调用 manager-api，回传结果，成功后断开连接促使重连"""
    result_msg = {
        "type": "device_event_result",
        "event": event_name,
        "success": False,
    }
    if request_id is not None:
        result_msg["request_id"] = request_id

    if not current_agent_name or not target_agent_name:
        error = "缺少 current_agent_name 或 target_agent_name"
        if raise_on_error:
            raise ValueError(error)
        result_msg["error"] = error
        await _send_json(conn, result_msg)
        return

    if not confirm:
        error = "必须确认换绑（confirm=true）"
        if raise_on_error:
            raise ValueError(error)
        result_msg["error"] = error
        await _send_json(conn, result_msg)
        return

    if not conn.read_config_from_api:
        error = "当前未启用 manager-api，无法换绑"
        if raise_on_error:
            raise ValueError(error)
        result_msg["error"] = error
        await _send_json(conn, result_msg)
        return

    try:
        rebind_args = {
            "device_id": conn.device_id,
            "current_agent_name": current_agent_name,
            "target_agent_name": target_agent_name,
            "confirm": True,
        }
        if language is not None:
            rebind_args["language"] = language
            rebind_args["dev"] = dev
        data = await rebind_device_agent(**rebind_args)
        result_msg["success"] = True
        result_msg["data"] = data or {}
        await _send_json(conn, result_msg)
        logger.bind(tag=TAG).info(
            f"设备换绑成功: {conn.device_id} {current_agent_name} -> {target_agent_name}，准备断开重连"
        )
        await asyncio.sleep(0.1)
        try:
            await conn.websocket.close()
        except Exception as close_error:
            logger.bind(tag=TAG).warning(f"换绑成功后关闭连接失败: {close_error}")
        return data
    except Exception as e:
        logger.bind(tag=TAG).error(f"设备换绑失败: {e}")
        if raise_on_error:
            raise
        result_msg["error"] = str(e)
        await _send_json(conn, result_msg)


async def _send_json(conn: "ConnectionHandler", payload: Dict[str, Any]):
    try:
        await conn.websocket.send(json.dumps(payload, ensure_ascii=False))
    except Exception as e:
        logger.bind(tag=TAG).error(f"发送设备事件结果失败: {e}")


async def apply_language_change(
    conn: "ConnectionHandler",
    language: str,
    *,
    current_agent_name: Optional[str] = None,
    target_agent_name: Optional[str] = None,
    request_id: Any = None,
    dev: bool = False,
) -> dict:
    """语言切换=智能体换绑。供 WS 事件与内部 HTTP 共用。"""
    if language not in LANGUAGE_AGENT_SUFFIXES:
        raise ValueError(
            f"language 必须使用规范码: {', '.join(LANGUAGE_AGENT_SUFFIXES)}"
        )

    resolved_current = current_agent_name or (conn.device_attributes or {}).get(
        "agent_name"
    )
    resolved_target = target_agent_name or resolve_language_agent_name(
        resolved_current, language, dev
    )
    if not resolved_target:
        raise ValueError("无法从当前智能体名称推导目标语言智能体")

    logger.bind(tag=TAG).info(
        f"设备语言切换: {conn.device_id} {language} dev={dev} "
        f"{resolved_current} -> {resolved_target}"
    )

    await _do_rebind(
        conn,
        "language_change",
        resolved_current,
        resolved_target,
        request_id,
        confirm=True,
        raise_on_error=True,
        language=language,
        dev=dev,
    )
    conn.device_language = language
    if conn.device_attributes is None:
        conn.device_attributes = {}
    conn.device_attributes["language"] = language
    return {
        "deviceId": conn.device_id,
        "language": language,
        "dev": dev,
        "currentAgentName": resolved_current,
        "targetAgentName": resolved_target,
    }


async def _handle_language_change(
    conn: "ConnectionHandler", payload: Dict[str, Any], request_id: Any
):
    """语言变更：改为触发智能体换绑，语言切换=智能体切换。

    目标智能体只按 language/dev 推导；current_agent_name 缺省从设备扩展属性读取。
    language 以基础规范码随换绑事务持久化，不再用于强制翻译。
    """
    language = payload.get("language")
    dev = payload.get("dev", False)
    if not isinstance(language, str) or language not in LANGUAGE_AGENT_SUFFIXES:
        result_msg = {
            "type": "device_event_result",
            "event": "language_change",
            "success": False,
            "error": f"language 必须使用规范码: {', '.join(LANGUAGE_AGENT_SUFFIXES)}",
        }
        if request_id is not None:
            result_msg["request_id"] = request_id
        await _send_json(conn, result_msg)
        return
    if not isinstance(dev, bool):
        result_msg = {
            "type": "device_event_result",
            "event": "language_change",
            "success": False,
            "error": "dev 必须是布尔值",
        }
        if request_id is not None:
            result_msg["request_id"] = request_id
        await _send_json(conn, result_msg)
        return

    try:
        await apply_language_change(
            conn,
            language,
            current_agent_name=(
                payload.get("current_agent_name")
                or payload.get("currentAgentName")
            ),
            request_id=request_id,
            dev=dev,
        )
    except Exception as error:
        result_msg = {
            "type": "device_event_result",
            "event": "language_change",
            "success": False,
            "error": str(error),
        }
        if request_id is not None:
            result_msg["request_id"] = request_id
        await _send_json(conn, result_msg)


async def _handle_beacon_change(
    conn: "ConnectionHandler", beacon_id: str, payload: Dict[str, Any]
):
    """处理蓝牙信标变更事件，并在信标变化时触发 LLM 对话"""
    if conn.device_attributes is None:
        conn.device_attributes = {}

    old_beacon_id = conn.device_attributes.get("last_beacon_id")
    conn.device_attributes["last_beacon_id"] = beacon_id
    logger.bind(tag=TAG).info(f"设备蓝牙信标已更新: {beacon_id}")
    await conn.refresh_beacon_location(force=True)

    # 信标变化时触发 LLM 对话
    if old_beacon_id != beacon_id and old_beacon_id is not None:
        await _trigger_beacon_chat(conn, beacon_id, old_beacon_id, payload)


async def _trigger_beacon_chat(
    conn: "ConnectionHandler",
    new_beacon_id: str,
    old_beacon_id: str,
    payload: Dict[str, Any],
):
    """信标变化时触发 LLM 对话"""
    from core.handle.receiveAudioHandle import startToChat

    location = conn.beacon_location
    if location is not None and location.beacon_id == new_beacon_id:
        prompt = (
            f"[位置变化] 用户当前位于{location.to_prompt_context()}。"
            "请直接说明用户现在所在的位置，并简要介绍附近可能的展品；"
            "信息不足时请坦诚说明，不要编造具体展品。"
        )
    else:
        beacon_name = payload.get("beacon_name", new_beacon_id)
        prompt = (
            f"[位置变化] 用户当前靠近信标 {beacon_name}，但位置服务暂时不可用。"
            "请简短提示用户已进入新区域，不要编造具体位置或展品。"
        )

    logger.bind(tag=TAG).info(f"信标变化触发 LLM 对话: {old_beacon_id} -> {new_beacon_id}")

    try:
        await startToChat(conn, prompt)
    except Exception as e:
        logger.bind(tag=TAG).error(f"信标变化触发 LLM 对话失败: {e}")


async def _report_event_to_manager_api(
    conn: "ConnectionHandler", event: str, payload: Dict[str, Any], timestamp: Any
):
    """将设备事件上报给 manager-api 持久化"""
    if not conn.read_config_from_api:
        return
    try:
        await report_device_event(
            device_id=conn.device_id,
            event=event,
            payload=payload,
            timestamp=timestamp,
        )
    except Exception as e:
        logger.bind(tag=TAG).error(f"上报设备事件失败: {e}")
