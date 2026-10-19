import argparse
import asyncio
import json
import os
import resource
import socket
import statistics
import sys
import time
from collections import Counter
from pathlib import Path


async def main(args):
    if args.database_url != "postgresql+asyncpg://guide_test:guide_test_only@127.0.0.1:25432/guide_benchmark":
        raise SystemExit("This destructive fixture requires the dedicated local guide_benchmark database")
    os.environ["DATABASE_URL"] = args.database_url
    os.environ["REDIS_URL"] = "redis://127.0.0.1:26379/1"
    os.environ["INTEGRATION_API_KEY"] = "benchmark-only-management-token-32-characters"
    sys.path.insert(0, str(Path(args.management_repo).resolve()))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    import uvicorn
    from sqlalchemy import event
    from app.core.ids import generate_nanoid
    from app.db.base import Base
    from app.db.session import AsyncSessionLocal, engine
    from app.main import app
    from app.models.beacon import BluetoothBeacon
    from app.models.device import Device
    from app.models.management import CatalogRevision, VenueArea
    from app.models.venue import MuseumVenue
    from ksz_guide.client import ManagementClient
    from ksz_guide.runtime import GuideRuntime

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    macs = [f"A0:00:00:{index >> 16:02X}:{(index >> 8) & 255:02X}:{index & 255:02X}" for index in range(args.devices)]
    beacon_macs = [f"DA:01:16:FF:00:{index:02X}" for index in range(10)]
    async with AsyncSessionLocal() as db:
        db.add(CatalogRevision(id=1, revision=1))
        venue_ids = []
        for index in range(10):
            venue_id, area_id = generate_nanoid(), generate_nanoid()
            venue_ids.append(venue_id)
            db.add(MuseumVenue(id=venue_id, code=f"BENCH{index}", name=f"测试场馆{index}", address="隔离性能测试", enabled=True))
            await db.flush()
            db.add(VenueArea(id=area_id, venue_id=venue_id, floor="1F", name="测试区域"))
            await db.flush()
            db.add(BluetoothBeacon(id=generate_nanoid(), beacon_id=f"bench-{index}", beacon_mac=beacon_macs[index],
                                   venue_id=venue_id, area_id=area_id, floor="1F", area="测试区域", location_description="入口", enabled=True))
        for index, mac in enumerate(macs):
            db.add(Device(device_mac=mac, name=f"测试设备{index}", mode="venue", venue_id=venue_ids[index % 10],
                          status="active", model="simulator", auto_announce=False))
        await db.commit()

    sql = Counter()
    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def count_sql(conn, cursor, statement, parameters, context, executemany):
        sql[statement.split(None, 1)[0].upper()] += 1

    requests = Counter()
    @app.middleware("http")
    async def count_requests(request, call_next):
        requests[f"{request.method} {request.url.path}"] += 1
        return await call_next(request)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    while not server.started:
        if server_task.done():
            await server_task
        await asyncio.sleep(0.01)
    client = ManagementClient(f"http://127.0.0.1:{port}/api/v1/integration", os.environ["INTEGRATION_API_KEY"])
    runtime = GuideRuntime(client)
    durations = []
    semaphore = asyncio.Semaphore(100)
    sequence = 0
    manifests = {}

    async def batch(action):
        nonlocal sequence
        sequence += 1
        async def one(index, mac):
            async with semaphore:
                start = time.perf_counter()
                message = {"type": "guide_control", "version": 1, "action": action,
                           "boot_id": f"bench-{index}", "seq": sequence, "request_id": f"{index}-{sequence}"}
                if action != "sync":
                    message["policy_revision"] = client.policies[mac].revision
                if action == "policy_ack":
                    message.update(policy_sha256=manifests[mac]["sha256"], chunk_count=manifests[mac]["chunk_count"])
                elif action != "sync":
                    message["beacon"] = {"mac": beacon_macs[index % 10], "rssi": -50}
                response = await runtime.process(mac, f"connection-{index}", message)
                results = [item for item in response["messages"] if item["action"] == "result"]
                assert results and all(item["accepted"] for item in results), response
                for item in response["messages"]:
                    if item["action"] == "policy":
                        manifests[mac] = item["manifest"]
                if action == "heartbeat":
                    assert not any(item["action"] == "policy" for item in response["messages"]), response
                durations.append((time.perf_counter() - start) * 1000)
        await asyncio.gather(*(one(index, mac) for index, mac in enumerate(macs)))

    started = time.perf_counter()
    try:
        await batch("sync")
        await batch("policy_ack")
        await batch("observation")
        await batch("policy_ack")
        warm_requests, warm_sql = requests.copy(), sql.copy()
        warmed_at = time.perf_counter()
        durations.clear()
        runtime.maintenance_task = asyncio.create_task(runtime.run())
        for iteration in range(args.rounds):
            await batch("heartbeat")
            if iteration + 1 < args.rounds:
                await asyncio.sleep(5)
        assert all(runtime.location(mac) is not None for mac in macs)
        steady_requests = requests - warm_requests
        catalog_requests = sum(count for path, count in steady_requests.items() if "/venues/" in path)
        assert catalog_requests <= len(venue_ids), steady_requests
        ordered = sorted(durations)
        summary = {"devices": args.devices, "venues": 10, "heartbeat_interval_seconds": 5,
                   "heartbeat_rounds": args.rounds, "heartbeat_count": len(durations),
                   "elapsed_seconds": round(time.perf_counter() - started, 2),
                   "warmup_seconds": round(warmed_at - started, 2),
                   "latency_ms": {"median": round(statistics.median(ordered), 3),
                                  "p95": round(ordered[int(len(ordered) * .95)], 3), "max": round(max(ordered), 3)},
                   "steady_http_requests": dict(steady_requests), "steady_sql_statements": dict(sql - warm_sql),
                   "warmup_http_requests": dict(warm_requests), "max_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2)}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        await runtime.close()
        server.should_exit = True
        await server_task
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--management-repo", required=True)
    parser.add_argument("--database-url", default="postgresql+asyncpg://guide_test:guide_test_only@127.0.0.1:25432/guide_benchmark")
    parser.add_argument("--devices", type=int, default=1000)
    parser.add_argument("--rounds", type=int, default=12)
    asyncio.run(main(parser.parse_args()))
