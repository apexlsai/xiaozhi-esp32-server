import re
from datetime import datetime
from zoneinfo import ZoneInfo

from ksz_prompt import CONSUMER_ROLE, DEFAULT_CONTEXT_TURNS, trim_history, valid_context_turns


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


def configured_context_turns(conn):
    turns = conn.config.get("context_turns")
    return turns if valid_context_turns(turns) else None


def trim_connection(conn):
    limit = configured_context_turns(conn)
    if limit is None and agent_owned(conn):
        limit = DEFAULT_CONTEXT_TURNS
    if limit is None:
        return
    history = conn.dialogue.dialogue
    starts = [i for i, message in enumerate(history) if message.role == "user" and not message.is_temporary]
    if len(starts) > limit:
        start = starts[-limit]
        conn.dialogue.dialogue = [
            m for m in history[:start] if m.role == "system" or m.is_temporary
        ] + history[start:]


def context(conn, values, *, kind=None, depth=0):
    from .playback import playback_state
    from .integration import refresh_location
    from .prompt import response_language
    refresh_location(conn)
    state = {key: value for key, value in values.items() if key in {
        "device_id", "device_mode", "venue_id", "venue_context", "last_beacon_id", "beacon_mac",
        "guide_policy_revision", "device_location", "beacon_museum_code",
        "ksz_route", "ksz_route_version", "ksz_route_confidence", "ksz_route_margin",
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
    language = response_language(conn, values.get("language"))
    base_prompt = conn.config.get("prompt") or ""
    if state.get("device_mode") == "consumer":
        base_prompt = CONSUMER_ROLE
        if name := values.get("assistant_name"):
            state["assistant_name"] = name
    return {"version": 1, "base_prompt": base_prompt,
            "language": language, "state": state,
            "turn": {"kind": kind or ("narration" if internal_turn(conn) else "user"),
                     "tool_depth_exhausted": depth >= 5}}


def request_messages(conn, messages, values, depth=0, kind=None):
    from .integration import runtime_for
    from .intent_embedding import external_museum_query
    from .prompt import configured_identity, prepare_system_prompt, venue_prompt

    venue = values.get("venue_context")
    mode = values.get("device_mode")
    runtime = runtime_for(conn)
    owned = agent_owned(conn)
    if runtime or mode or owned:
        values.update(configured_identity(conn))
    if messages and messages[-1].get("role") == "user" and runtime:
        question = messages[-1].get("content")
        if isinstance(question, str) and external_museum_query(question, venue):
            values["ksz_external_museum"] = True
    if not owned:
        if not (runtime or venue or mode == "consumer"):
            return messages
        messages = list(messages)
        assistant_name = values.get("assistant_name")
        system_index = next((i for i, message in enumerate(messages) if message["role"] == "system"), None)
        if system_index is not None:
            system = messages[system_index]
            prompt = system["content"]
            if mode == "consumer":
                prompt, replaced = re.subn(r"<museum_role>.*?</museum_role>", "", prompt, count=1, flags=re.S)
                if not replaced:
                    prompt = prepare_system_prompt(conn, "")
            messages[system_index] = {
                **system, "content": venue_prompt(prompt, venue, mode, assistant_name=assistant_name),
            }
        elif mode == "consumer":
            messages.insert(0, {
                "role": "system", "content": venue_prompt("", None, mode, assistant_name=assistant_name),
            })
        return messages
    trim_connection(conn)
    prompt_context = context(conn, values, kind=kind, depth=depth)
    limit = configured_context_turns(conn)
    if limit is not None:
        prompt_context["context_turns"] = limit
    values["ksz_prompt_context"] = prompt_context
    return trim_history(
        [message for message in messages if message.get("role") != "system"],
        limit or DEFAULT_CONTEXT_TURNS,
    )
