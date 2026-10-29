import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

import httpx


log = logging.getLogger(__name__)
START = "\n\n<ksz_device_location>\n"
END = "\n</ksz_device_location>"


@dataclass
class GeographicLocation:
    value: dict = field(default_factory=lambda: {"source": "unknown", "status": "pending"})
    task: object = None
    closed: bool = False


def location_state(conn):
    if not getattr(conn, "read_config_from_api", False):
        return None
    state = getattr(conn, "ksz_geographic_location", None)
    if not isinstance(state, GeographicLocation):
        state = GeographicLocation()
        conn.ksz_geographic_location = state
    return state


def location_context(conn):
    state = location_state(conn)
    if state is None or state.value.get("source") == "ip":
        return False, ""
    expires = state.value.get("expiresAt", 0)
    if expires and expires <= time.time() * 1000:
        return True, ""
    return True, state.value.get("city") or ""


def location_prompt(conn, prompt):
    prompt = re.sub(re.escape(START) + r".*?" + re.escape(END), "", prompt, flags=re.S)
    handled, city = location_context(conn)
    if not handled:
        return prompt
    prompt = re.sub(r"(?m)^Approximate device location:[^\n]*(?:\n|$)", "", prompt)
    state = location_state(conn)
    source = state.value.get("source")
    if city:
        if source == "default":
            rule = (f"Default city for local queries: {city}. This is a configured fallback, "
                    "not a measured current position. Do not claim the visitor is located there.")
        elif source == "fixed":
            rule = f"Configured venue city: {city}. Use this city for local queries without a named city."
        else:
            rule = (f"Approximate device location: {city}. Source: cell tower estimate. "
                    "It establishes an approximate city, not an exact street, building, floor or room.")
        rule += " A city explicitly named in the current user request takes precedence for that query."
    else:
        rule = ("The device city is not currently known. Ask which city when a local query needs one. "
                "Do not infer it from an IP address, SIM carrier, earlier assistant guesses or old weather results.")
    return prompt + START + rule + END


async def fetch_location(conn):
    config = conn.config.get("manager-api") or {}
    base = config.get("url", "").rstrip("/")
    secret = config.get("secret")
    if not base or not secret:
        raise ValueError("manager_api_unavailable")
    async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(9, connect=2)) as client:
        response = await client.post(
            base + "/config/device-location", json={"deviceId": conn.device_id},
            headers={"Authorization": "Bearer " + secret},
        )
        if response.status_code != 200:
            raise ValueError("location_service_unavailable")
        payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("invalid_location_response")
    value = payload.get("data")
    if payload.get("code") != 0 or not isinstance(value, dict):
        raise ValueError("invalid_location_response")
    if value.get("source") not in {"ip", "cell", "fixed", "default", "unknown"}:
        raise ValueError("invalid_location_source")
    city = value.get("city")
    if city is not None and (not isinstance(city, str) or len(city) > 100 or "\n" in city):
        raise ValueError("invalid_city")
    for name in ("locatedAt", "expiresAt", "retryAt"):
        timestamp = value.get(name, 0)
        if isinstance(timestamp, str) and timestamp.isdecimal():
            timestamp = int(timestamp)
        if type(timestamp) is not int or timestamp < 0:
            raise ValueError("invalid_location_timestamp")
        value[name] = timestamp
    return value


def apply_location(conn):
    prompt = getattr(conn, "prompt", None)
    if isinstance(prompt, str) and prompt:
        conn.change_system_prompt(prompt)


async def watch_location(conn, state):
    try:
        while not state.closed:
            apply_location(conn)
            try:
                value = await fetch_location(conn)
                if state.closed:
                    return
                previous = state.value
                state.value = value
                if value != previous:
                    if value.get("source") == "ip":
                        await asyncio.to_thread(conn._init_prompt_enhancement)
                    else:
                        apply_location(conn)
                retry_at = value.get("retryAt") or value.get("expiresAt") or 0
                delay = min(60, max(1, retry_at / 1000 - time.time())) if retry_at else 60
            except (httpx.HTTPError, ValueError, TypeError):
                log.warning("设备 %s 位置读取失败，将使用有效位置或等待重试", conn.device_id)
                apply_location(conn)
                delay = 60
            await asyncio.sleep(delay)
    except asyncio.CancelledError:
        pass


def start_location(conn):
    state = location_state(conn)
    if state is None or state.task is not None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    state.task = loop.create_task(watch_location(conn, state))


def close_location(conn):
    state = getattr(conn, "ksz_geographic_location", None)
    if isinstance(state, GeographicLocation):
        state.closed = True
        if state.task is not None:
            state.task.cancel()
