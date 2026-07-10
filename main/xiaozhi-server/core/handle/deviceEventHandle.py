import asyncio
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from config.manage_api_client import report_device_event
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


async def handle_device_event(conn: "ConnectionHandler", msg_json: Dict[str, Any]):
    """处理设备主动上报的事件"""
    event = msg_json.get("event")
    payload = msg_json.get("payload", {})
    timestamp = msg_json.get("timestamp")

    if not event:
        logger.bind(tag=TAG).warning("收到空的 device_event")
        return

    logger.bind(tag=TAG).info(f"设备事件: {event}, payload: {payload}")

    if event == "language_change":
        language = payload.get("language")
        if language:
            await _handle_language_change(conn, language)
    elif event == "beacon_change":
        beacon_id = payload.get("beacon_id")
        if beacon_id:
            await _handle_beacon_change(conn, beacon_id, payload)
    else:
        logger.bind(tag=TAG).debug(f"未识别的设备事件: {event}")

    # 异步上报到 manager-api 持久化
    asyncio.create_task(
        _report_event_to_manager_api(conn, event, payload, timestamp)
    )


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

    # 从 payload 中提取额外信息（如信标名称、位置描述等）
    beacon_name = payload.get("beacon_name", new_beacon_id)
    location_desc = payload.get("location", "")

    # 构造提示信息
    if location_desc:
        prompt = f"[位置变化] 用户已从之前的位置移动到了 {location_desc}（信标: {beacon_name}）。请根据这个新位置为用户提供相关的服务或问候。"
    else:
        prompt = f"[位置变化] 用户的位置发生了变化，当前靠近信标 {beacon_name}。请根据用户的新位置提供相关服务或简短问候。"

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
