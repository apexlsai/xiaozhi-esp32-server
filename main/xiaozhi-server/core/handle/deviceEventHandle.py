import asyncio
import json
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from config.manage_api_client import report_device_event, rebind_device_agent
from config.logger import setup_logging

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
        language = payload.get("language")
        if language:
            await _handle_language_change(conn, language)
    elif event == "beacon_change":
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
    """处理设备智能体换绑：调用 manager-api，回传结果，成功后断开连接促使重连"""
    current_agent_name = payload.get("current_agent_name") or payload.get(
        "currentAgentName"
    )
    target_agent_name = payload.get("target_agent_name") or payload.get(
        "targetAgentName"
    )
    confirm = payload.get("confirm", False)
    if isinstance(confirm, str):
        confirm = confirm.lower() in ("true", "1", "yes")

    result_msg = {
        "type": "device_event_result",
        "event": "agent_rebind",
        "success": False,
    }
    if request_id is not None:
        result_msg["request_id"] = request_id

    if not current_agent_name or not target_agent_name:
        result_msg["error"] = "缺少 current_agent_name 或 target_agent_name"
        await _send_json(conn, result_msg)
        return

    if not confirm:
        result_msg["error"] = "必须确认换绑（confirm=true）"
        await _send_json(conn, result_msg)
        return

    if not conn.read_config_from_api:
        result_msg["error"] = "当前未启用 manager-api，无法换绑"
        await _send_json(conn, result_msg)
        return

    try:
        data = await rebind_device_agent(
            device_id=conn.device_id,
            current_agent_name=current_agent_name,
            target_agent_name=target_agent_name,
            confirm=True,
        )
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
    except Exception as e:
        result_msg["error"] = str(e)
        logger.bind(tag=TAG).error(f"设备换绑失败: {e}")
        await _send_json(conn, result_msg)


async def _send_json(conn: "ConnectionHandler", payload: Dict[str, Any]):
    try:
        await conn.websocket.send(json.dumps(payload, ensure_ascii=False))
    except Exception as e:
        logger.bind(tag=TAG).error(f"发送设备事件结果失败: {e}")


async def _handle_language_change(conn: "ConnectionHandler", language: str):
    """处理语言变更事件"""
    conn.device_language = language
    if conn.device_attributes is None:
        conn.device_attributes = {}
    conn.device_attributes["language"] = language
    logger.bind(tag=TAG).info(f"设备语言已切换为: {language}")
    # 刷新提示词增强，使 device_language 参与后续 prompt 模板渲染
    try:
        conn._init_prompt_enhancement()
    except Exception as e:
        logger.bind(tag=TAG).warning(f"语言切换后刷新提示词失败: {e}")


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
