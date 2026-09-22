import asyncio
import logging
import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from uuid import uuid4

from .models import Beacon, GuideError, Policy, beacon_identity, mac_address
from .presence import PresenceTracker, connection_is_open, is_mqtt_bridge
from .settings import GuideSettings
from .announcement import guide_announcement


log = logging.getLogger(__name__)
ACTIONS = {"sync", "policy_ack", "observation", "lost", "heartbeat", "ready"}


@dataclass
class Pending:
    activation_id: str
    location_revision: int
    policy_revision: str
    deadline: float
    session_attempt: int
    requested_at: float = 0


@dataclass
class DeviceState:
    mac: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    policy: Policy | None = None
    boot_id: str | None = None
    seq: int = -1
    responses: OrderedDict = field(default_factory=OrderedDict)
    last_seen: float = 0
    last_position_at: float = 0
    current: Beacon | None = None
    baseline_mac: str | None = None
    location_revision: int = 0
    pending: Pending | None = None
    last_announce_at: float = float("-inf")
    owner: object = None
    owner_attempt: int = 0
    owner_ready: bool = False
    transport: str = "mqtt"
    connection_id: str | None = None
    ack_revision: str | None = None
    manifest: dict | None = None
    manifest_key: tuple | None = None
    manifest_sent: set = field(default_factory=set)
    ack_hash: str | None = None
    compatibility: str = "legacy"
    rejection: str | None = None
    rejected_count: int = 0
    report_revision: int = 0
    activity_until: float = 0
    announcement_task: asyncio.Task | None = None
    announcement_sentence_id: str | None = None


class GuideRuntime:
    def __init__(self, client, server=None, *, clock=time.monotonic, wall_clock=time.time, announcer=None,
                 guide_enabled=True, settings=None):
        self.client = client
        self.server = server
        self.clock = clock
        self.wall_clock = wall_clock
        self.announcer = announcer or self._announce
        self.devices = {}
        self.instance_id = uuid4().hex
        self.closed = False
        self.maintenance_task = None
        self.announcement_tasks = set()
        self.guide_enabled = guide_enabled
        self.settings = settings if settings is not None else GuideSettings()
        self.presence = PresenceTracker(clock, wall_clock)
        self.voice_connections = {}
        self.voice_by_device = {}
        self.presence_sequence = 0
        self.dirty_reports = {}
        self.pending_registration = set()
        self.presence_task = None
        self.presence_lock = asyncio.Lock()
        self.presence_retry_at = 0
        self.next_connection_snapshot = 0
        self.report_sequence = 0

    def _mark_reports(self, macs):
        for mac in macs:
            self.dirty_reports[mac] = self.dirty_reports.get(mac, 0) + 1

    def _schedule_presence(self):
        if self.closed or self.clock() < self.presence_retry_at:
            return
        if self.presence_task is None or self.presence_task.done():
            async def flush():
                await asyncio.sleep(0.1)
                await self.flush_presence()
            self.presence_task = asyncio.create_task(flush())

    def receive_presence(self, payload):
        instance, sequence, reports = self.presence.validate(payload)
        for item in reports:
            self.state(item["device_mac"])
        accepted, touched, changed = self.presence.update(instance, sequence, reports, "mqtt")
        self._presence_touched(touched, changed)
        return {"accepted": accepted, "ignored": len(reports) - accepted}

    def _presence_touched(self, touched, changed):
        for mac in touched:
            state = self.state(mac)
            observed = self.presence.fields(mac)["last_seen_at"]
            if observed is not None:
                state.last_seen = max(state.last_seen, self.clock() - max(0, self.wall_clock() - observed))
        self.pending_registration.update(touched)
        self._mark_reports(changed)
        if self.pending_registration or self.dirty_reports:
            self._schedule_presence()

    def connection_opened(self, conn, connected_at=None):
        if conn in self.voice_connections:
            return
        try:
            state = self.state(conn.device_id)
        except GuideError:
            return
        entry = {"device_mac": state.mac, "connection_id": uuid4().hex,
                 "connected_at": connected_at if connected_at is not None else self.wall_clock(),
                 "online": True, "observed_at": self.wall_clock()}
        self.voice_connections[conn] = (entry, is_mqtt_bridge(conn))
        self.voice_by_device.setdefault(state.mac, set()).add(conn)
        self.pending_registration.add(state.mac)
        self._mark_reports({state.mac})
        self._update_ws_presence(conn)
        self._schedule_presence()

    def _update_ws_presence(self, conn, online=True):
        entry, mqtt = self.voice_connections[conn]
        if mqtt:
            return
        self.presence_sequence += 1
        report = {**entry, "online": online, "observed_at": self.wall_clock()}
        _, touched, changed = self.presence.update(self.instance_id, self.presence_sequence, [report], "websocket")
        self._presence_touched(touched, changed)

    def connection_closed(self, conn):
        found = self.voice_connections.get(conn)
        if found is None:
            return
        self._update_ws_presence(conn, False)
        self.voice_connections.pop(conn, None)
        mac = found[0]["device_mac"]
        self.voice_by_device[mac].discard(conn)
        if not self.voice_by_device[mac]:
            self.voice_by_device.pop(mac, None)
        self._mark_reports({mac})
        self._schedule_presence()

    def snapshot_connections(self):
        connections = getattr(self.server, "device_connections", None)
        if not isinstance(connections, dict):
            return
        active = {conn for conn in connections if connection_is_open(conn)}
        for conn in list(self.voice_connections):
            if conn not in active:
                self.connection_closed(conn)
                if self.guide_enabled:
                    self.unbind(conn)
        for conn in active:
            if conn not in self.voice_connections:
                self.connection_opened(conn, connections[conn].get("connected_at"))
            elif not self.voice_connections[conn][1]:
                self._update_ws_presence(conn)

    async def flush_presence(self):
        async with self.presence_lock:
            registrations = set(self.pending_registration)
            try:
                if registrations:
                    await self.client.ensure_registered(list(registrations))
                    self.pending_registration.difference_update(registrations)
                    if self.guide_enabled:
                        for mac in registrations:
                            state = self.devices.get(mac)
                            policy = self.client.policies.get(mac)
                            if state and policy:
                                async with state.lock:
                                    self._apply_policy(state, policy)
                versions = dict(self.dirty_reports)
                reports = [self.report(self.devices[mac]) for mac in versions if mac in self.devices]
                if reports:
                    await self.client.report(reports)
                for mac, version in versions.items():
                    if self.dirty_reports.get(mac) == version:
                        self.dirty_reports.pop(mac, None)
                self.presence_retry_at = 0
            except GuideError:
                self.presence_retry_at = self.clock() + 5
                log.warning("Device presence synchronization unavailable; retrying automatically")

    def state(self, mac):
        mac = mac_address(mac)
        state = self.devices.get(mac)
        if state is None:
            if len(self.devices) >= 10000:
                raise GuideError("runtime_capacity_reached")
            state = self.devices[mac] = DeviceState(mac)
        return state

    @staticmethod
    def message(action, **values):
        return {"type": "guide_control", "version": 1, "action": action, **values}

    def _result(self, message, accepted, reason=None):
        return self.message("result", request_id=message.get("request_id"), seq=message.get("seq"), accepted=accepted, reason=reason)

    def _attach_lease(self, state, result):
        result.pop("lease", None)
        policy = state.policy
        manifest = state.manifest
        if (result.get("accepted") is not True or policy is None or not policy.valid(self.clock())
                or manifest is None or state.ack_revision != policy.revision
                or state.ack_hash != manifest["sha256"]):
            return
        result["lease"] = {
            "policy_revision": policy.revision,
            "policy_sha256": manifest["sha256"],
            "expires_at": min(policy.data["expires_at"], self.wall_clock() + max(0, policy.deadline - self.clock())),
        }

    def _invalidate(self, state, *, reset_baseline=False):
        state.current = None
        state.pending = None
        state.location_revision += 1
        if state.announcement_task and not state.announcement_task.done():
            state.announcement_task.cancel()
        if reset_baseline:
            state.baseline_mac = None
        if state.owner is not None:
            state.owner.beacon_location = None

    def _expire(self, state):
        now = self.clock()
        if state.current is not None and (not state.policy or not state.policy.valid(now)
                                          or now - state.last_position_at >= self.settings.position_ttl_ms / 1000):
            self._invalidate(state)
        if state.pending and now >= state.pending.deadline:
            state.pending = None

    def _apply_policy(self, state, policy):
        if state.policy and state.policy.revision != policy.revision:
            self._invalidate(state, reset_baseline=True)
            state.ack_revision = None
            state.ack_hash = None
            state.manifest = None
        state.policy = policy
        self._expire(state)

    async def _policy_message(self, state, chunk_index=0, expected_hash=None):
        policy = state.policy
        if not policy:
            raise GuideError("policy_unavailable")
        venue = state.current.venue_id if state.current else None
        key = (policy.revision, venue, policy.valid(self.clock()))
        if state.manifest is None or state.manifest_key != key:
            beacons = await self.client.wire_whitelist(policy, venue) if policy.valid(self.clock()) else []
            config = {key: value for key, value in policy.data.items() if key not in {"expires_at", "beacon_allowlist"}}
            config["auto_announce"] = config.get("auto_announce") is True and self.settings.auto_announce_enabled
            config["filter"] = {**config.get("filter", {}), "min_announce_interval_ms": self.settings.min_announce_interval_ms}
            config["current_venue_id"] = venue
            manifest_json = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            digest = hashlib.sha256((manifest_json + "\n").encode())
            for start in range(0, max(1, len(beacons)), 128):
                chunk_json = json.dumps(beacons[start:start + 128], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                digest.update((chunk_json + "\n").encode())
            state.manifest = {"sha256": digest.hexdigest(), "chunk_count": max(1, (len(beacons) + 127) // 128),
                              "beacon_count": len(beacons), "chunk_size": 128, "beacons": beacons, "policy": config,
                              "manifest_json": manifest_json}
            state.manifest_key = key
            state.manifest_sent.clear()
        manifest = state.manifest
        if expected_hash and expected_hash != manifest["sha256"]:
            chunk_index = 0
        if isinstance(chunk_index, bool) or not isinstance(chunk_index, int) or not 0 <= chunk_index < manifest["chunk_count"]:
            raise GuideError("invalid_policy_chunk")
        state.manifest_sent.add(chunk_index)
        return self.message("policy", policy_revision=policy.revision,
                            policy={**manifest["policy"], "expires_at": policy.data["expires_at"]},
                            manifest={key: manifest[key] for key in ("sha256", "chunk_count", "beacon_count", "chunk_size", "manifest_json")},
                            chunk_index=chunk_index, beacons=manifest["beacons"][chunk_index * 128:(chunk_index + 1) * 128],
                            chunk_json=json.dumps(manifest["beacons"][chunk_index * 128:(chunk_index + 1) * 128], sort_keys=True, separators=(",", ":"), ensure_ascii=False))

    async def process(self, device_mac, connection_id, message, *, transport="mqtt", udp_ready=False, conn=None):
        state = self.state(device_mac)
        if not isinstance(message, dict) or message.get("type") != "guide_control" or message.get("version") != 1:
            raise GuideError("unsupported_protocol")
        action = message.get("action")
        boot_id = message.get("boot_id")
        seq = message.get("seq")
        if action not in ACTIONS or not isinstance(boot_id, str) or not 1 <= len(boot_id) <= 128:
            raise GuideError("invalid_control_message")
        if isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq <= 2**53 - 1:
            raise GuideError("invalid_sequence")
        request_id = message.get("request_id")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise GuideError("invalid_request_id")
        async with state.lock:
            self._expire(state)
            if state.boot_id != boot_id:
                if action != "sync":
                    return {"messages": [self._result(message, False, "sync_required")]}
                self._invalidate(state, reset_baseline=True)
                state.boot_id = boot_id
                state.seq = -1
                state.responses.clear()
                state.owner_attempt = 0
                state.owner_ready = False
            if seq <= state.seq:
                cached = state.responses.get(seq)
                result = dict(cached) if cached else self._result(message, False, "stale_sequence")
                messages = [result]
                if cached and action == "sync" and state.policy:
                    try:
                        messages.append(await self._policy_message(state, message.get("chunk_index", 0), message.get("policy_sha256")))
                    except GuideError:
                        pass
                if cached and cached.get("accepted"):
                    wake = self._wake_message(state)
                    if wake:
                        messages.append(wake)
                self._attach_lease(state, result)
                return {"messages": messages}
            state.seq = seq
            state.last_seen = self.clock()
            state.connection_id = connection_id
            state.transport = transport
            state.compatibility = "v1"
            try:
                policy = await self.client.policy(state.mac, message.get("device"))
                self._apply_policy(state, policy)
                messages = []
                if action == "sync":
                    messages.append(await self._policy_message(state, message.get("chunk_index", 0), message.get("policy_sha256")))
                elif action == "policy_ack":
                    if message.get("policy_revision") != policy.revision:
                        raise GuideError("policy_revision_mismatch")
                    await self._policy_message(state)
                    if (message.get("policy_sha256") != state.manifest["sha256"] or message.get("chunk_count") != state.manifest["chunk_count"]
                            or len(state.manifest_sent) != state.manifest["chunk_count"]):
                        raise GuideError("incomplete_policy_ack")
                    state.ack_revision = policy.revision
                    state.ack_hash = state.manifest["sha256"]
                else:
                    if not policy.valid(self.clock()):
                        raise GuideError(policy.data.get("reason") or "policy_disabled")
                    if message.get("policy_revision") != policy.revision:
                        raise GuideError("policy_revision_mismatch")
                    if action in {"observation", "heartbeat"} and message.get("beacon") is not None:
                        await self._observe(state, message["beacon"])
                    elif action == "observation":
                        raise GuideError("beacon_required")
                    elif action == "lost":
                        self._invalidate(state)
                    elif action == "ready":
                        self._ready(state, message, transport, udp_ready, conn)
                state.rejection = None
                result = self._result(message, True)
                messages.append(result)
                if action != "sync":
                    chunk = await self._policy_message(state)
                    if state.ack_revision != policy.revision or state.ack_hash != chunk["manifest"]["sha256"]:
                        messages.append(chunk)
                await self._try_announce(state)
                wake = self._wake_message(state)
                if wake:
                    messages.append(wake)
            except GuideError as error:
                reason = str(error)
                state.rejection = reason
                state.rejected_count += 1
                result = self._result(message, False, reason)
                messages = [result]
                self._expire(state)
                if reason in {"policy_revision_mismatch", "policy_changed"}:
                    cached = self.client.policies.get(state.mac)
                    if cached:
                        self._apply_policy(state, cached)
                    try:
                        messages.append(await self._policy_message(state))
                    except GuideError:
                        pass
            self._attach_lease(state, result)
            state.responses[seq] = result
            while len(state.responses) > 32:
                state.responses.popitem(last=False)
            return {"messages": messages}

    async def _observe(self, state, data, *, legacy=False):
        observed = beacon_identity(data)
        if legacy:
            observed["_legacy"] = True
        policy = state.policy
        location_revision = state.location_revision
        beacon = await self.client.resolve(state.mac, policy, observed, state.current.venue_id if state.current else None)
        latest = self.client.policies.get(state.mac, policy)
        if latest.revision != policy.revision or not policy.valid(self.clock()) or location_revision != state.location_revision:
            raise GuideError("policy_changed")
        if not policy.allows(beacon):
            raise GuideError("beacon_not_allowed")
        previous = state.baseline_mac
        should_announce = (
            (previous is not None or self.settings.announce_on_first_beacon)
            and previous != beacon.mac
            and self.settings.auto_announce_enabled
            and policy.data.get("auto_announce") is True
            and bool(beacon.to_prompt_context())
        )
        if should_announce and self.settings.interrupt_on_beacon_change:
            await self._interrupt_announcement(state)
        state.current = beacon
        state.last_position_at = self.clock()
        if state.owner is not None:
            state.owner.beacon_location = beacon
        if previous != beacon.mac:
            state.location_revision += 1
            state.baseline_mac = beacon.mac
            state.pending = None
            if should_announce:
                state.pending = Pending(uuid4().hex, state.location_revision, policy.revision,
                                        self.clock() + self.settings.pending_ttl_ms / 1000, state.owner_attempt + 1)

    async def _interrupt_announcement(self, state):
        owner = state.owner
        task = state.announcement_task
        task_active = task is not None and not task.done()
        sentence_active = (
            owner is not None
            and state.announcement_sentence_id is not None
            and getattr(owner, "sentence_id", None) == state.announcement_sentence_id
            and getattr(owner, "client_is_speaking", False)
        )
        if owner is None or not (task_active or sentence_active):
            return False
        from core.handle.abortHandle import handleAbortMessage
        await handleAbortMessage(owner)
        owner.sentence_id = uuid4().hex
        state.announcement_sentence_id = None
        state.last_announce_at = float("-inf")
        if task_active:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if state.announcement_task is task:
                state.announcement_task = None
        return True

    def _ready(self, state, message, transport, udp_ready, conn):
        owner = state.owner
        if owner is None or (conn is not None and owner is not conn):
            raise GuideError("session_not_current")
        if message.get("session_id") != getattr(owner, "session_id", None):
            raise GuideError("session_mismatch")
        if message.get("session_attempt") != state.owner_attempt:
            raise GuideError("session_attempt_mismatch")
        activation_id = message.get("activation_id")
        if activation_id and (not state.pending or activation_id != state.pending.activation_id):
            raise GuideError("activation_expired")
        if transport == "mqtt" and (udp_ready is not True or getattr(owner, "guide_connection_id", None) != state.connection_id):
            raise GuideError("udp_not_ready")
        state.owner_ready = True

    def _wake_message(self, state):
        pending = state.pending
        now = self.clock()
        if not pending or state.owner is not None or state.compatibility != "v1":
            return None
        if now - state.last_announce_at < self.settings.min_announce_interval_ms / 1000 or now >= pending.deadline:
            return None
        if pending.requested_at and now - pending.requested_at < 3:
            return None
        pending.requested_at = now
        return self.message("request_hello", activation_id=pending.activation_id,
                            session_attempt=pending.session_attempt, boot_id=state.boot_id,
                            policy_revision=pending.policy_revision,
                            expires_in_ms=max(0, int((pending.deadline - now) * 1000)))

    async def bind(self, conn, metadata=None):
        state = self.state(conn.device_id)
        has_metadata = metadata is not None
        if has_metadata and not isinstance(metadata, dict):
            return False
        metadata = metadata or {}
        async with state.lock:
            activation_id = metadata.get("activation_id")
            if activation_id:
                self._expire(state)
                if not state.pending or activation_id != state.pending.activation_id:
                    return False
            attempt = metadata.get("session_attempt")
            if has_metadata:
                if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 2**53 - 1:
                    return False
                boot_id = metadata.get("boot_id")
                if not isinstance(boot_id, str) or not 1 <= len(boot_id) <= 128:
                    return False
                if state.boot_id is not None and state.boot_id != boot_id:
                    return False
                if state.boot_id is None:
                    state.boot_id = boot_id
                if attempt <= state.owner_attempt and state.owner is not conn:
                    return False
                if activation_id and attempt < state.pending.session_attempt:
                    return False
                if state.owner is conn and attempt == state.owner_attempt:
                    return True
            else:
                attempt = state.owner_attempt + 1
            old = state.owner
            state.owner = conn
            state.owner_attempt = attempt
            state.owner_ready = not has_metadata and state.compatibility == "legacy"
            conn.guide_connection_id = metadata.get("connection_id")
            conn.guide_session_attempt = attempt
            if has_metadata:
                state.compatibility = "v1"
            conn.beacon_location = self.location(state.mac)
            if old is not None and old is not conn:
                await old.websocket.close()
            return True

    def unbind(self, conn):
        try:
            state = self.devices.get(mac_address(conn.device_id))
        except GuideError:
            return
        if state and state.owner is conn:
            state.owner = None
            state.owner_ready = False
            state.pending = None

    async def legacy(self, conn, payload):
        state = self.state(conn.device_id)
        async with state.lock:
            state.last_seen = self.clock()
            if state.owner is None:
                state.owner = conn
                state.owner_ready = True
            try:
                self._apply_policy(state, await self.client.policy(state.mac))
                if not state.policy.valid(self.clock()):
                    raise GuideError("policy_disabled")
                await self._observe(state, {"mac": payload.get("beacon_id"), "rssi": payload.get("rssi")}, legacy=True)
                await self._try_announce(state)
                return True
            except GuideError as error:
                state.rejection = str(error)
                state.rejected_count += 1
                self._expire(state)
                return False

    def location(self, device_mac):
        try:
            state = self.devices.get(mac_address(device_mac))
        except GuideError:
            return None
        if state is None:
            return None
        if (not state.policy or not state.policy.valid(self.clock())
                or self.clock() - state.last_position_at >= self.settings.position_ttl_ms / 1000):
            return None
        return state.current

    def context(self, device_mac):
        beacon = self.location(device_mac)
        if beacon is None:
            return {}
        state = self.devices[mac_address(device_mac)]
        return {"last_beacon_id": beacon.beacon_id, "venue_id": beacon.venue_id,
                "beacon_mac": beacon.mac, "guide_policy_revision": state.policy.revision}

    def activity(self, conn, message):
        state = self.devices.get(mac_address(conn.device_id))
        if not state:
            return
        if message.get("type") == "listen":
            state.pending = None
            if state.announcement_task and not state.announcement_task.done():
                state.announcement_task.cancel()
            if message.get("state") == "start":
                state.activity_until = self.clock() + 120
            elif message.get("state") in {"stop", "detect"}:
                state.activity_until = self.clock() + self.settings.user_quiet_ms / 1000
        elif message.get("type") == "abort":
            state.pending = None
            if state.announcement_task and not state.announcement_task.done():
                state.announcement_task.cancel()

    async def _try_announce(self, state):
        self._expire(state)
        pending = state.pending
        owner = state.owner
        now = self.clock()
        if not pending or not owner or not state.owner_ready or not state.current or not state.policy:
            return
        if pending.location_revision != state.location_revision or pending.policy_revision != state.policy.revision:
            state.pending = None
            return
        if (now - state.last_announce_at < self.settings.min_announce_interval_ms / 1000
                or now < state.activity_until or getattr(owner, "need_bind", True)
                or getattr(owner, "tts", None) is None or getattr(owner, "client_is_speaking", False)
                or getattr(owner, "client_have_voice", False) or getattr(owner, "calling", False)
                or getattr(owner, "guide_chat_count", 0)
                or (state.announcement_task is not None and not state.announcement_task.done())
                or getattr(getattr(owner, "vision_sessions", None), "active", None) is not None):
            return
        state.pending = None
        state.last_announce_at = now
        beacon = state.current
        async def announce():
            if state.owner is not owner or state.location_revision != pending.location_revision or self.location(state.mac) is not beacon:
                return
            try:
                await self.announcer(owner, beacon)
            except Exception:
                state.rejection = "announcement_failed"
                log.exception("Guide announcement failed")
        task = asyncio.create_task(announce())
        state.announcement_task = task
        self.announcement_tasks.add(task)
        task.add_done_callback(self.announcement_tasks.discard)
        await asyncio.sleep(0)

    async def _announce(self, conn, beacon):
        from core.handle.sendAudioHandle import send_tts_message
        from core.utils.output_counter import check_device_output_limit

        limit = getattr(conn, "max_output_size", 0)
        if limit > 0 and check_device_output_limit(conn.device_id, limit):
            return
        sentence_id = conn.sentence_id
        socket = conn.websocket
        cancelled = False

        def owns_playback():
            return (conn.websocket is socket and conn.sentence_id == sentence_id
                    and not conn.client_abort and connection_is_open(conn))

        def chat():
            state = self.devices.get(mac_address(conn.device_id))
            if (not cancelled and owns_playback() and state and state.owner is conn
                    and not getattr(conn, "client_have_voice", False)
                    and not getattr(conn, "guide_chat_count", 0)
                    and self.location(state.mac) is beacon):
                def valid():
                    return not cancelled and state.owner is conn and self.location(state.mac) is beacon

                with guide_announcement(conn, beacon, self.settings, valid):
                    result = conn.chat(f"[位置变化] 用户当前位于{beacon.to_prompt_context()}。设备已播报位置，请只生成后续简短介绍，不要问候、自我介绍或重复位置；没有可靠资料时不补充，不要编造。")
                state = self.devices.get(mac_address(conn.device_id))
                if (result is not False and state and state.owner is conn
                        and getattr(conn, "client_is_speaking", False)):
                    state.announcement_sentence_id = conn.sentence_id

        conn.client_abort = False
        conn.client_is_speaking = True
        conn.last_activity_time = self.wall_clock() * 1000
        try:
            await send_tts_message(conn, "start")
            if owns_playback():
                await asyncio.wrap_future(conn.executor.submit(chat))
        finally:
            cancelled = True
            if owns_playback():
                try:
                    await send_tts_message(conn, "stop")
                except Exception:
                    if owns_playback():
                        conn.client_is_speaking = False
                    log.warning("Guide playback cleanup failed")

    def report(self, state):
        if self.guide_enabled:
            self._expire(state)
        self.report_sequence += 1
        state.report_revision = self.report_sequence
        result = {
            "device_mac": state.mac, "instance_id": self.instance_id,
            "report_revision": state.report_revision, "reported_at": self.wall_clock(),
            **self.presence.fields(state.mac),
            "session_active": any(connection_is_open(conn) for conn in self.voice_by_device.get(state.mac, ())),
        }
        if not self.guide_enabled:
            return result
        result.update({
            "location_observed_at": self.wall_clock() - max(0, self.clock() - state.last_position_at) if state.current else None,
            "policy_revision": state.policy.revision if state.policy else None,
            "applied_policy_revision": state.ack_revision, "compatibility": state.compatibility,
            "current_venue_id": state.current.venue_id if state.current else None,
            "current_beacon_id": state.current.id if state.current else None,
            "location_revision": state.location_revision, "last_error": state.rejection,
            "rejected_count": state.rejected_count,
        })
        return result

    async def tick(self, *, refresh=False):
        if self.clock() >= self.next_connection_snapshot:
            self.snapshot_connections()
            self.next_connection_snapshot = self.clock() + 10
        self._mark_reports(self.presence.sweep())
        states = list(self.devices.values())
        if refresh and self.guide_enabled:
            macs = [state.mac for state in states if self.clock() - state.last_seen < 60 or state.owner is not None]
            try:
                await self.client.ensure_registered(macs)
                policies = await self.client.refresh_policies(macs)
                for mac, policy in policies.items():
                    state = self.devices.get(mac)
                    if state:
                        async with state.lock:
                            self._apply_policy(state, policy)
            except GuideError:
                log.warning("Guide policy refresh unavailable; existing leases are not extended")
        for state in states:
            async with state.lock:
                if self.guide_enabled:
                    await self._try_announce(state)
                if (state.owner is None and self.clock() - state.last_seen >= 300
                        and not self.presence.fields(state.mac)["online"]
                        and state.mac not in self.pending_registration and state.mac not in self.dirty_reports):
                    self.devices.pop(state.mac, None)
        if refresh:
            self._mark_reports(self.devices)
        if (self.pending_registration or self.dirty_reports) and self.clock() >= self.presence_retry_at:
            await self.flush_presence()

    async def run(self):
        next_refresh = self.clock() + 30
        while not self.closed:
            await asyncio.sleep(1)
            refresh = self.clock() >= next_refresh
            await self.tick(refresh=refresh)
            if refresh:
                next_refresh = self.clock() + 30

    async def close(self):
        if self.closed:
            return
        self.closed = True
        if self.maintenance_task:
            self.maintenance_task.cancel()
            await asyncio.gather(self.maintenance_task, return_exceptions=True)
        if self.presence_task:
            self.presence_task.cancel()
            await asyncio.gather(self.presence_task, return_exceptions=True)
        for task in self.announcement_tasks:
            task.cancel()
        await asyncio.gather(*self.announcement_tasks, return_exceptions=True)
        await self.client.close()
