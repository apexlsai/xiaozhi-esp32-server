import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
TAG = __name__


async def handleAbortMessage(conn: "ConnectionHandler"):
    conn.logger.bind(tag=TAG).info("Abort message received")
    sentence_id = conn.sentence_id
    # 设置成打断状态，会自动打断llm、tts任务
    conn.close_after_chat = False
    conn.client_abort = True
    sessions = getattr(conn, "vision_sessions", None)
    if sessions is not None:
        sessions.cancel("aborted")
    feedback = getattr(conn, "tool_feedback", None)
    if feedback is not None:
        feedback.cancel_all()
    conn.clear_queues()
    # 打断客户端说话状态
    await conn.websocket.send(
        json.dumps({"type": "tts", "state": "stop", "session_id": conn.session_id})
    )
    if conn.sentence_id == sentence_id:
        conn.clearSpeakStatus()
    conn.logger.bind(tag=TAG).info("Abort message received-end")
