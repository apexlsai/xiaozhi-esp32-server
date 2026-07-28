from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote

import aiohttp


BEACON_LOCATION_API = "http://127.0.0.1:14000/api/v1/beacons/by-beacon-id"
BEACON_LOCATION_TIMEOUT_SECONDS = 2


@dataclass(frozen=True)
class BeaconLocation:
    beacon_id: str
    floor: str
    area: str
    location_description: str

    def to_prompt_context(self) -> str:
        parts = [self.floor, self.area, self.location_description]
        return "，".join(part for part in parts if part)


def _text_field(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    return value.strip() if isinstance(value, str) else ""


def parse_beacon_location(beacon_id: str, payload: Any) -> Optional[BeaconLocation]:
    if not isinstance(payload, dict) or _text_field(payload, "beacon_id") != beacon_id:
        return None

    location = BeaconLocation(
        beacon_id=beacon_id,
        floor=_text_field(payload, "floor"),
        area=_text_field(payload, "area"),
        location_description=_text_field(payload, "location_description"),
    )
    return location if location.to_prompt_context() else None


async def fetch_beacon_location(beacon_id: str, logger) -> Optional[BeaconLocation]:
    url = f"{BEACON_LOCATION_API}/{quote(beacon_id, safe='')}"
    timeout = aiohttp.ClientTimeout(total=BEACON_LOCATION_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"Accept": "application/json"}) as response:
                if response.status == 404:
                    logger.bind(tag=__name__).warning(f"未找到信标位置: {beacon_id}")
                    return None
                if response.status != 200:
                    logger.bind(tag=__name__).warning(
                        f"查询信标位置失败: {beacon_id}, HTTP {response.status}"
                    )
                    return None
                location = parse_beacon_location(beacon_id, await response.json())
                if location is None:
                    logger.bind(tag=__name__).warning(f"信标位置响应无效: {beacon_id}")
                return location
    except aiohttp.ClientError as e:
        logger.bind(tag=__name__).warning(f"查询信标位置失败: {beacon_id}, {type(e).__name__}")
    except TimeoutError:
        logger.bind(tag=__name__).warning(f"查询信标位置超时: {beacon_id}")
    return None
