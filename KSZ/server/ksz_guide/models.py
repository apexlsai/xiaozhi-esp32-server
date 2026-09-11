import math
import re
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID


class GuideError(Exception):
    pass


def mac_address(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}", value):
        raise GuideError("invalid_mac")
    return value.upper()


def beacon_identity(value: Any) -> dict:
    if not isinstance(value, dict):
        raise GuideError("invalid_beacon")
    result = {"mac": mac_address(value.get("mac"))}
    supplied = [value.get(key) is not None for key in ("uuid", "major", "minor")]
    if any(supplied):
        if not all(supplied):
            raise GuideError("incomplete_ibeacon_identity")
        try:
            result["uuid"] = str(UUID(value["uuid"])).upper()
        except (ValueError, TypeError, AttributeError):
            raise GuideError("invalid_uuid") from None
        for key in ("major", "minor"):
            number = value[key]
            if isinstance(number, bool) or not isinstance(number, int) or not 0 <= number <= 65535:
                raise GuideError("invalid_ibeacon_number")
            result[key] = number
    rssi = value.get("rssi")
    if rssi is not None:
        if isinstance(rssi, bool) or not isinstance(rssi, (int, float)) or not math.isfinite(rssi) or not -127 <= rssi <= 20:
            raise GuideError("invalid_rssi")
        result["rssi"] = rssi
    return result


@dataclass(frozen=True)
class Policy:
    data: dict
    deadline: float

    @classmethod
    def parse(cls, data, now=None, wall_time=None):
        if not isinstance(data, dict) or not isinstance(data.get("revision"), str):
            raise GuideError("invalid_policy")
        mac_address(data.get("device_mac"))
        expiry = data.get("expires_at")
        if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
            raise GuideError("invalid_policy_expiry")
        for field in ("venue_ids", "beacon_ids"):
            if not isinstance(data.get(field), list) or not all(isinstance(item, str) for item in data[field]):
                raise GuideError("invalid_policy_scope")
        duration = min(60, max(0, expiry - (time.time() if wall_time is None else wall_time)))
        return cls(dict(data), (time.monotonic() if now is None else now) + duration)

    @property
    def revision(self):
        return self.data["revision"]

    def valid(self, now):
        return now < self.deadline and self.data.get("enabled") is True and self.data.get("status") == "active"

    def allows(self, beacon):
        if self.data.get("mode") == "consumer":
            return True
        return beacon.venue_id in self.data["venue_ids"] or beacon.id in self.data["beacon_ids"]


@dataclass(frozen=True)
class Beacon:
    id: str
    mac: str
    beacon_id: str
    venue_id: str
    area_id: str | None
    floor: str
    area: str
    location_description: str
    uuid: str | None = None
    major: int | None = None
    minor: int | None = None

    @classmethod
    def parse(cls, item):
        if not isinstance(item, dict) or not all(isinstance(item.get(key), str) and item[key] for key in ("id", "venue_id", "beacon_id")):
            raise GuideError("invalid_catalog")
        identity = beacon_identity({key: item.get(key) for key in ("mac", "uuid", "major", "minor")})
        return cls(
            id=item["id"], mac=identity["mac"], beacon_id=item["beacon_id"],
            venue_id=item["venue_id"], area_id=item.get("area_id"),
            floor=str(item.get("floor") or ""), area=str(item.get("area") or ""),
            location_description=str(item.get("location_description") or ""),
            uuid=identity.get("uuid"), major=identity.get("major"), minor=identity.get("minor"),
        )

    def matches(self, observed):
        return self.mac == observed["mac"] and (
            self.uuid is None or observed.get("_legacy") is True or (self.uuid, self.major, self.minor) == (
                observed.get("uuid"), observed.get("major"), observed.get("minor"),
            )
        )

    @property
    def zone(self):
        return (self.venue_id, self.area_id or ((self.floor, self.area) if self.area else self.id))

    def to_prompt_context(self):
        return "，".join(part for part in (self.floor, self.area, self.location_description) if part)

    def wire(self):
        return {key: value for key, value in vars(self).items() if value is not None}
