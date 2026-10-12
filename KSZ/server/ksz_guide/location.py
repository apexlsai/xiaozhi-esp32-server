import asyncio
import json
import logging


log = logging.getLogger(__name__)


class LocationUpdates:
    def __init__(self, runtime):
        self.runtime = runtime
        self.subscriptions = {}
        self.tasks = set()

    def start(self, conn):
        if not self.runtime.guide_enabled or (getattr(conn, "features", None) or {}).get("location_updates") is not True:
            return
        state = self.runtime.state(conn.device_id)
        if state.owner is not conn or self.runtime.closed or id(conn) in self.subscriptions:
            return
        event = asyncio.Event()
        event.set()
        task = asyncio.create_task(self._send(conn, state, event))
        self.subscriptions[id(conn)] = (event, task)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def notify(self, state):
        subscription = self.subscriptions.get(id(state.owner))
        if subscription:
            subscription[0].set()

    def stop(self, conn):
        subscription = self.subscriptions.pop(id(conn), None)
        if subscription:
            subscription[1].cancel()

    async def _send(self, conn, state, event):
        previous = None
        revision = 0
        located = False
        try:
            while not self.runtime.closed and state.owner is conn:
                await event.wait()
                event.clear()
                if state.owner is not conn:
                    return
                now = self.runtime.clock()
                beacon = self.runtime.location(state.mac)
                if beacon is not None and not beacon.to_prompt_context().strip():
                    beacon = None
                deadline = min(state.last_position_at + self.runtime.settings.position_ttl_ms / 1000,
                               state.policy.deadline) if beacon is not None else 0
                ttl = max(0, int((deadline - now) * 1000)) if beacon is not None else 0
                if not ttl:
                    beacon = None
                    deadline = 0
                located = located or beacon is not None
                status = "located" if beacon is not None else (
                    "lost" if located or state.baseline_mac is not None else "unknown"
                )
                signature = (status, beacon, deadline)
                if signature == previous:
                    continue
                revision += 1
                location = None if beacon is None else {
                    "beacon_id": beacon.beacon_id, "beacon_mac": beacon.mac,
                    "venue_id": beacon.venue_id, "floor": beacon.floor,
                    "area": beacon.area, "location_description": beacon.location_description,
                }
                message = self.runtime.message(
                    "location", session_id=conn.session_id, location_revision=revision,
                    status=status, expires_in_ms=ttl, location=location,
                )
                await asyncio.wait_for(conn.websocket.send(json.dumps(message, ensure_ascii=False)), timeout=2)
                previous = signature
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.warning("Location update unavailable for %s: %s", state.mac, type(error).__name__)
        finally:
            subscription = self.subscriptions.get(id(conn))
            if subscription and subscription[1] is asyncio.current_task():
                self.subscriptions.pop(id(conn), None)

    async def close(self):
        tasks = list(self.tasks)
        self.subscriptions.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
