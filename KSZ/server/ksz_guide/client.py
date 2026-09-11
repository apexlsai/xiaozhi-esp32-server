import asyncio
import time
from collections import OrderedDict
from urllib.parse import quote

import aiohttp

from .models import Beacon, GuideError, Policy, mac_address


class ManagementClient:
    def __init__(self, base_url, token, *, clock=time.monotonic, wall_clock=time.time, session=None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.clock = clock
        self.wall_clock = wall_clock
        self.session = session
        self.policies = OrderedDict()
        self.catalogs = OrderedDict()
        self.wire_catalogs = OrderedDict()
        self.resolutions = OrderedDict()
        self.inflight = {}
        self.registered = set()
        self.resolve_after = {}
        self.last_error = None
        self.request_serial = 0
        self.policy_serials = {}
        self.pending_policies = {}
        self.policy_batch_task = None

    async def close(self):
        tasks = list(self.inflight.values())
        if self.policy_batch_task:
            tasks.append(self.policy_batch_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for _, future in self.pending_policies.values():
            future.cancel()
        if self.session is not None:
            await self.session.close()

    async def _request(self, method, path, **kwargs):
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=2),
                connector=aiohttp.TCPConnector(limit=32),
            )
        try:
            async with self.session.request(
                method, self.base_url + path,
                headers={"Authorization": f"Bearer {self.token}"}, **kwargs,
            ) as response:
                if response.status != 200:
                    raise GuideError(f"management_http_{response.status}")
                data = await response.json()
                if not isinstance(data, dict):
                    raise GuideError("invalid_management_response")
                self.last_error = None
                return data
        except (aiohttp.ClientError, TimeoutError) as error:
            self.last_error = type(error).__name__
            raise GuideError("management_unavailable") from error
        except ValueError as error:
            self.last_error = "invalid_management_json"
            raise GuideError("invalid_management_response") from error

    async def _singleflight(self, key, operation):
        task = self.inflight.get(key)
        if task is None:
            task = asyncio.create_task(operation())
            self.inflight[key] = task
            def finished(done):
                if self.inflight.get(key) is done:
                    self.inflight.pop(key, None)
                if not done.cancelled():
                    done.exception()
            task.add_done_callback(finished)
        return await asyncio.shield(task)

    @staticmethod
    def _store(cache, key, value, maximum):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > maximum:
            cache.popitem(last=False)

    def _save_policy(self, data, serial=None):
        policy = Policy.parse(data, self.clock(), self.wall_clock())
        mac = mac_address(data["device_mac"])
        if serial is not None:
            if self.policy_serials.get(mac, -1) > serial and mac in self.policies:
                return self.policies[mac]
            self.policy_serials[mac] = serial
        self._store(self.policies, mac, policy, 10000)
        return policy

    async def policy(self, mac, metadata=None):
        mac = mac_address(mac)
        cached = self.policies.get(mac)
        if cached and cached.deadline > self.clock():
            return cached
        async def fetch():
            future = asyncio.get_running_loop().create_future()
            self.pending_policies[mac] = (metadata, future)
            if self.policy_batch_task is None:
                self.policy_batch_task = asyncio.create_task(self._flush_policies())
            return await future
        return await self._singleflight(("policy", mac), fetch)

    async def _flush_policies(self):
        try:
            await asyncio.sleep(0.01)
            while self.pending_policies:
                queued = self.pending_policies
                self.pending_policies = {}
                macs = list(queued)
                for start in range(0, len(macs), 100):
                    batch = macs[start:start + 100]
                    try:
                        registrations = []
                        for mac in batch:
                            if mac in self.registered:
                                continue
                            device = {"device_mac": mac}
                            metadata = queued[mac][0]
                            for field in ("model", "hardware_version", "firmware_version"):
                                value = (metadata or {}).get(field) if isinstance(metadata, dict) else None
                                if isinstance(value, str) and len(value) <= 100:
                                    device[field] = value
                            registrations.append(device)
                        if registrations:
                            self.request_serial += 1
                            serial = self.request_serial
                            data = await self._request("POST", "/devices/sync", json={"devices": registrations})
                            self.registered.update(item["device_mac"] for item in registrations)
                            entries = data.get("policies", [])
                            if not isinstance(entries, list):
                                raise GuideError("invalid_policies")
                            for entry in entries:
                                if not isinstance(entry, dict):
                                    raise GuideError("invalid_policy")
                                if mac_address(entry.get("device_mac")) not in batch:
                                    raise GuideError("policy_identity_mismatch")
                                self._save_policy(entry, serial)
                        missing = [mac for mac in batch if mac not in self.policies or self.policies[mac].deadline <= self.clock()]
                        refreshed = await self.refresh_policies(missing) if missing else {}
                        policies = {mac: self.policies[mac] for mac in batch if mac in self.policies}
                        policies.update(refreshed)
                        for mac in batch:
                            future = queued[mac][1]
                            if not future.done():
                                if mac in policies:
                                    future.set_result(policies[mac])
                                else:
                                    future.set_exception(GuideError("missing_policy"))
                    except Exception as error:
                        for mac in batch:
                            future = queued[mac][1]
                            if not future.done():
                                future.set_exception(error)
        finally:
            self.policy_batch_task = None

    async def refresh_policies(self, macs):
        results = {}
        for start in range(0, len(macs), 100):
            self.request_serial += 1
            serial = self.request_serial
            devices = []
            for mac in macs[start:start + 100]:
                item = {"device_mac": mac}
                if mac in self.policies:
                    item["revision"] = self.policies[mac].revision
                devices.append(item)
            data = await self._request("POST", "/policies/query", json={"devices": devices})
            entries = data.get("policies")
            if not isinstance(entries, list):
                raise GuideError("invalid_policies")
            requested = {item["device_mac"] for item in devices}
            for entry in entries:
                if not isinstance(entry, dict):
                    raise GuideError("invalid_policy")
                if mac_address(entry.get("device_mac")) not in requested:
                    raise GuideError("policy_identity_mismatch")
                policy = self._save_policy(entry, serial)
                results[mac_address(entry["device_mac"])] = policy
        return results

    async def catalog(self, venue, catalog_revision):
        cache_key = (venue, catalog_revision)
        cached = self.catalogs.get(cache_key)
        if cached and cached[0] > self.clock():
            return cached[1]
        async def fetch():
            items = {}
            offset = 0
            version = None
            for _ in range(100):
                data = await self._request("GET", f"/venues/{quote(venue, safe='')}/beacons", params={"offset": offset, "limit": 500})
                if not isinstance(data.get("items"), list) or not isinstance(data.get("version"), str):
                    raise GuideError("invalid_catalog")
                if data["version"] != catalog_revision:
                    raise GuideError("catalog_revision_mismatch")
                if version is not None and version != data["version"]:
                    raise GuideError("catalog_changed_during_pagination")
                version = data["version"]
                for item in data["items"]:
                    beacon = Beacon.parse(item)
                    if beacon.venue_id != venue or beacon.mac in items:
                        raise GuideError("invalid_catalog_identity")
                    items[beacon.mac] = beacon
                next_offset = data.get("next_offset")
                if next_offset is None:
                    self._store(self.catalogs, cache_key, (self.clock() + 60, items), 256)
                    return items
                if isinstance(next_offset, bool) or not isinstance(next_offset, int) or next_offset <= offset:
                    raise GuideError("invalid_catalog_pagination")
                offset = next_offset
            raise GuideError("catalog_too_large")
        return await self._singleflight(("catalog", cache_key), fetch)

    async def whitelist(self, policy, current_venue=None):
        venues = list(policy.data["venue_ids"])
        if policy.data.get("mode") == "consumer" and current_venue:
            venues = [current_venue]
        catalog_revision = policy.data.get("catalog_revision")
        if venues and not isinstance(catalog_revision, str):
            raise GuideError("missing_catalog_revision")
        catalogs = await asyncio.gather(*(self.catalog(venue, catalog_revision) for venue in venues))
        result = {}
        for catalog in catalogs:
            result.update(catalog)
        for item in policy.data.get("beacon_allowlist", []):
            beacon = Beacon.parse(item)
            result[beacon.mac] = beacon
        return {mac: beacon for mac, beacon in result.items() if policy.allows(beacon)}

    async def resolve(self, mac, policy, observed, current_venue=None):
        known = await self.whitelist(policy, current_venue)
        beacon = known.get(observed["mac"])
        if beacon is not None:
            if not beacon.matches(observed):
                raise GuideError("beacon_identity_mismatch")
            return beacon
        dynamic = policy.data.get("mode") == "consumer" or bool(policy.data["beacon_ids"])
        if not dynamic:
            raise GuideError("beacon_not_allowed")
        identity = tuple(observed.get(key) for key in ("mac", "uuid", "major", "minor", "_legacy"))
        key = (mac, policy.revision, identity)
        cached = self.resolutions.get(key)
        if cached and cached[0] > self.clock():
            if cached[1] is None:
                raise GuideError("beacon_not_allowed")
            return cached[1]
        async def fetch():
            now = self.clock()
            if now < self.resolve_after.get(mac, 0):
                raise GuideError("resolve_rate_limited")
            self.resolve_after[mac] = now + 1
            self.request_serial += 1
            serial = self.request_serial
            identity = {key: observed[key] for key in ("mac", "uuid", "major", "minor") if key in observed}
            data = await self._request("POST", "/beacons/resolve", json={"device_mac": mac, "beacons": [identity]})
            if mac_address(data.get("policy", {}).get("device_mac")) != mac:
                raise GuideError("policy_identity_mismatch")
            returned = self._save_policy(data.get("policy"), serial)
            if returned.revision != policy.revision or not returned.valid(self.clock()):
                raise GuideError("policy_changed")
            entries = data.get("beacons")
            if not isinstance(entries, list):
                raise GuideError("invalid_resolution")
            result = None
            for item in entries:
                candidate = Beacon.parse(item)
                if candidate.matches(observed) and policy.allows(candidate):
                    result = candidate
                    break
            self._store(self.resolutions, key, (min(policy.deadline, self.clock() + 60), result), 10000)
            if result is None:
                raise GuideError("beacon_not_allowed")
            return result
        return await self._singleflight(("resolve", key), fetch)

    async def wire_whitelist(self, policy, current_venue=None):
        key = (policy.data.get("catalog_revision"), policy.data.get("mode"),
               tuple(policy.data["venue_ids"]), tuple(policy.data["beacon_ids"]), current_venue)
        cached = self.wire_catalogs.get(key)
        if cached and cached[0] > self.clock():
            return cached[1]
        items = await self.whitelist(policy, current_venue)
        beacons = [items[mac].wire() for mac in sorted(items)]
        self._store(self.wire_catalogs, key, (self.clock() + 60, beacons), 256)
        return beacons

    async def report(self, reports):
        for start in range(0, len(reports), 100):
            await self._request("POST", "/runtime-reports", json={"reports": reports[start:start + 100]})
