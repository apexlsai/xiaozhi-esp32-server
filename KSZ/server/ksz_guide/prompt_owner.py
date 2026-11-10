from datetime import datetime
from zoneinfo import ZoneInfo

from ksz_prompt import trim_history


def agent_owned(conn):
    config = getattr(conn, "config", {})
    selected = config.get("selected_module", {}).get("LLM")
    owner = config.get("LLM", {}).get(selected, {}).get("prompt_owner", config.get("prompt_owner", "xiaozhi"))
    if owner not in {"agent", "xiaozhi"}:
        raise ValueError("prompt_owner must be agent or xiaozhi")
    return owner == "agent"


def internal_turn(conn):
    from .announcement import is_guide_announcement
    return agent_owned(conn) and is_guide_announcement(conn)


def trim_connection(conn):
    if not agent_owned(conn):
        return
    history = conn.dialogue.dialogue
    starts = [i for i, message in enumerate(history) if message.role == "user" and not message.is_temporary]
    if len(starts) > 20:
        conn.dialogue.dialogue = [m for m in history[:starts[-20]] if m.role == "system"] + history[starts[-20]:]


def context(conn, values, *, kind=None, depth=0):
    from .playback import playback_state
    from .integration import refresh_location
    refresh_location(conn)
    state = {key: value for key, value in values.items() if key in {
        "device_id", "venue_id", "venue_context", "last_beacon_id", "beacon_mac",
        "guide_policy_revision", "device_location",
    }}
    beacon = getattr(conn, "beacon_location", None)
    if beacon is not None:
        state["indoor_location"] = beacon.to_prompt_context()
    state["current_time"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    playback = playback_state(conn, False)
    if playback is not None:
        with playback.lock:
            reply = next((item for item in reversed(playback.replies.values()) if item.interrupted), None)
            if reply is not None:
                state["playback"] = reply.snapshot()
    return {"version": 1, "base_prompt": conn.config.get("prompt") or "",
            "language": values.get("language", "zh-CN"), "state": state,
            "turn": {"kind": kind or ("narration" if internal_turn(conn) else "user"),
                     "tool_depth_exhausted": depth >= 5}}


def request_messages(conn, messages, values, depth=0, kind=None):
    if not agent_owned(conn):
        return messages
    trim_connection(conn)
    values["ksz_prompt_context"] = context(conn, values, kind=kind, depth=depth)
    return trim_history([message for message in messages if message.get("role") != "system"])
