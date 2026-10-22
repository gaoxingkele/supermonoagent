"""Loopback-only multi-host protocol cost; never opens a real shell or user relay."""

import argparse
import asyncio
import gc
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import tracemalloc
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from supermonoagent.daemon import Daemon, relay_uplink
from supermonoagent.protocol import pack, pack_json, unpack
from supermonoagent.relay import Relay
from test_client_integration import ControlledPty


async def measure(count):
    gc.collect()
    started, cpu = time.perf_counter(), time.process_time()
    tracemalloc.reset_peak()
    memory_start = tracemalloc.get_traced_memory()[0]
    hosts, uplinks, clients, readers = [], [], [], []
    records = [{"tx": 0, "rx": 0, "terminal_bytes": 0, "output_frames": 0, "events": asyncio.Queue()} for _ in range(count)]
    with tempfile.TemporaryDirectory(prefix="sma-multihost-benchmark-") as temporary, patch.dict(os.environ, {"SUPERMONO_STATE_DIR": temporary}):
        relay = Relay(development=True)
        async with serve(relay.handler, "127.0.0.1", 0, process_request=relay.serve_page) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ws"
            try:
                for index in range(count):
                    with patch("supermonoagent.daemon.STATE", Path(temporary) / str(index)):
                        host = Daemon(f"synthetic-host-{index}")
                    host.hub.persist = False
                    host.hub.pty_factory = lambda *_: ControlledPty()
                    host.hub.attach_loop(asyncio.get_running_loop())
                    host.hub.set_identity(f"test-machine-{index}", "Benchmark")
                    window = host.hub.open_window(owner=host.token)
                    session = host.hub.open_cli(window.id, "cmd")
                    hosts.append((host, session))
                    uplinks.append(asyncio.create_task(relay_uplink(host, url, f"room-{index}", str(index))))
                async with asyncio.timeout(10):
                    while not all(url in host.relay_live for host, _ in hosts):
                        await asyncio.sleep(.01)

                async def read(ws, record):
                    async for message in ws:
                        record["rx"] += len(message.encode() if isinstance(message, str) else message)
                        if isinstance(message, bytes):
                            op, payload = unpack(message)
                            if op == 3:
                                record["terminal_bytes"] += len(payload.partition(b"\0")[2])
                                record["output_frames"] += 1
                            await record["events"].put((op, payload))

                async def send(index, message):
                    records[index]["tx"] += len(message.encode() if isinstance(message, str) else message)
                    await clients[index].send(message)

                async def until(index, op, payload=None):
                    async with asyncio.timeout(5):
                        while True:
                            received, body = await records[index]["events"].get()
                            if received == op and (payload is None or body == payload):
                                return

                for index, (host, _session) in enumerate(hosts):
                    ws = await connect(url, compression=None)
                    clients.append(ws)
                    await send(index, json.dumps({"op": "phone", "code": f"room-{index}", "token": host.token}))
                    await ws.recv()  # ready; no terminal payload
                    readers.append(asyncio.create_task(read(ws, records[index])))
                    await send(index, pack_json(1, {"token": host.token, "multiplex": True}))
                    await until(index, 2)

                before = sum(row["tx"] + row["rx"] for row in records)
                for index in range(count):
                    await send(index, pack(10, b"heartbeat-probe"))
                await asyncio.gather(*(until(index, 11, b"heartbeat-probe") for index in range(count)))
                heartbeat_bytes = sum(row["tx"] + row["rx"] for row in records) - before

                for _host, session in hosts:
                    session.pty.inbox.put(b"unsubscribed-output\r\n" * 100)
                await asyncio.sleep(.1)  # bounded coalescer flush before sampling non-subscription
                assert sum(row["terminal_bytes"] for row in records) == 0
                for index in range(min(2, count)):
                    await send(index, pack_json(16, {"sessions": [hosts[index][1].id]}))
                    await until(index, 2)
                for _host, session in hosts:
                    session.pty.inbox.put(b"X" * 32768)
                async with asyncio.timeout(5):
                    while sum(row["terminal_bytes"] for row in records) < min(2, count) * 32768:
                        await asyncio.sleep(.01)
                assert all(row["terminal_bytes"] == 0 for row in records[min(2, count):])
                current, peak = tracemalloc.get_traced_memory()
                return {"hosts": count, "terminal_subscriptions": min(2, count), "heartbeat_roundtrip_application_bytes": heartbeat_bytes,
                        "projected_heartbeat_bytes_per_second_at_15s": heartbeat_bytes / 15,
                        "unopened_terminal_output_bytes": 0, "subscribed_output_bytes": sum(row["terminal_bytes"] for row in records),
                        "python_memory_delta_bytes": current-memory_start, "python_peak_delta_bytes": peak-memory_start,
                        "cpu_seconds": time.process_time()-cpu, "wall_seconds": time.perf_counter()-started}
            finally:
                for ws in clients:
                    await ws.close()
                for host, _session in hosts:
                    for ws in list(host._relay_sockets.values()):
                        await ws.close()
                for task in readers + uplinks:
                    task.cancel()
                await asyncio.gather(*readers, *uplinks, return_exceptions=True)
                for host, _session in hosts:
                    for window_id in list(host.hub.windows):
                        host.hub.close_window(window_id)


async def main(args):
    tracemalloc.start()
    report = {"environment": "loopback, development relay, synthetic PTYs; no actual shells", "scope": "application-message bytes and Python relay/client process memory, not RF/battery or Internet latency", "samples": []}
    for count in args.hosts:
        report["samples"].append(await measure(count))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hosts", nargs="+", type=int, default=[1, 4, 16])
    parser.add_argument("--output", type=Path, default=ROOT / ".omx/artifacts/global-workspace/resources.json")
    arguments = parser.parse_args()
    if any(count < 1 or count > 32 for count in arguments.hosts):
        parser.error("Host counts must be between 1 and 32")
    asyncio.run(main(arguments))
