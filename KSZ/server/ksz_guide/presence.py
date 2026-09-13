import math
from dataclasses import dataclass
from uuid import UUID

from .models import GuideError, mac_address


@dataclass
class ConnectionPresence:
    device_mac: str
    connection_id: str
    instance_id: str
    sequence: int
    source: str
    online: bool
    connected_at: float
    observed_at: float
    received_at: float


class PresenceTracker:
    def __init__(self, clock, wall_clock, ttl=30):
        self.clock = clock
        self.wall_clock = wall_clock
        self.ttl = ttl
        self.connections = {}
        self.by_device = {}
        self.signatures = {}

    @staticmethod
    def identifier(value):
        try:
            if not isinstance(value, str):
                raise ValueError()
            return UUID(value).hex
        except ValueError:
            raise GuideError("invalid_presence_identifier") from None

    def validate(self, payload):
        if not isinstance(payload, dict):
            raise GuideError("invalid_presence_batch")
        instance = self.identifier(payload.get("instance_id"))
        sequence = payload.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or not 0 <= sequence <= 2**53 - 1:
            raise GuideError("invalid_presence_sequence")
        reports = payload.get("reports")
        if not isinstance(reports, list) or not 1 <= len(reports) <= 100:
            raise GuideError("invalid_presence_batch")
        validated = []
        seen = set()
        for item in reports:
            if not isinstance(item, dict) or not isinstance(item.get("online"), bool):
                raise GuideError("invalid_presence_report")
            mac = mac_address(item.get("device_mac"))
            connection = self.identifier(item.get("connection_id"))
            if connection in seen:
                raise GuideError("duplicate_presence_connection")
            seen.add(connection)
            connected = item.get("connected_at")
            observed = item.get("observed_at")
            for value in (connected, observed):
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or value > self.wall_clock() + 5:
                    raise GuideError("invalid_presence_timestamp")
            if connected > observed + 5:
                raise GuideError("invalid_presence_timestamp")
            previous = self.connections.get((instance, connection))
            if previous and (previous.device_mac != mac or previous.connected_at != connected):
                raise GuideError("presence_identity_conflict")
            validated.append({"device_mac": mac, "connection_id": connection, "online": item["online"],
                              "connected_at": connected, "observed_at": observed})
        return instance, sequence, validated

    def update(self, instance, sequence, reports, source):
        accepted = 0
        touched = set()
        changed = set()
        for item in reports:
            key = (instance, item["connection_id"])
            previous = self.connections.get(key)
            if previous and sequence <= previous.sequence:
                continue
            self.connections[key] = ConnectionPresence(
                **item, instance_id=instance, sequence=sequence, source=source, received_at=self.clock(),
            )
            self.by_device.setdefault(item["device_mac"], set()).add(key)
            accepted += 1
            touched.add(item["device_mac"])
            if previous is None or previous.online != item["online"]:
                changed.add(item["device_mac"])
        for mac in touched:
            signature = {key for key in self.by_device[mac] if self._active(self.connections[key])}
            if self.signatures.get(mac, set()) != signature:
                changed.add(mac)
            self.signatures[mac] = signature
        return accepted, touched, changed

    def _active(self, item):
        return item.online and self.wall_clock() - item.observed_at < self.ttl

    def fields(self, mac):
        entries = [self.connections[key] for key in self.by_device.get(mac, ())]
        if not entries:
            return {"online": False, "transport": None, "last_seen_at": None, "connected_at": None}
        active = [item for item in entries if self._active(item)]
        latest = max(active or entries, key=lambda item: (item.connected_at, item.observed_at))
        return {
            "online": bool(active), "transport": latest.source,
            "last_seen_at": max(item.observed_at for item in entries),
            "connected_at": latest.connected_at,
            "transports": sorted({item.source for item in active}),
        }

    def sweep(self):
        changed = set()
        signatures = {}
        for item in self.connections.values():
            signatures.setdefault(item.device_mac, set())
            if self._active(item):
                signatures[item.device_mac].add((item.instance_id, item.connection_id))
        for mac in set(self.signatures) | set(signatures):
            if self.signatures.get(mac, set()) != signatures.get(mac, set()):
                changed.add(mac)
        self.signatures = signatures
        for key, item in list(self.connections.items()):
            if self.clock() - item.received_at >= 300 and not self._active(item):
                self.connections.pop(key, None)
                self.by_device[item.device_mac].discard(key)
                if not self.by_device[item.device_mac]:
                    self.by_device.pop(item.device_mac, None)
        return changed


def is_mqtt_bridge(conn):
    path = getattr(getattr(getattr(conn, "websocket", None), "request", None), "path", "")
    return getattr(conn, "conn_from_mqtt_gateway", False) is True or (isinstance(path, str) and "from=mqtt_gateway" in path)


def connection_is_open(conn):
    socket = getattr(conn, "websocket", None)
    if socket is None or getattr(socket, "closed", False) is True:
        return False
    state = getattr(socket, "state", None)
    if state is None:
        return True
    return getattr(state, "name", None) == "OPEN" or state == 1
