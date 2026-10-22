"""Local-only reproducible transport benchmark; no production credentials or relay.

Run from the checkout with .venv/Scripts/python.exe scripts/benchmark-relay.py.
Uses the same isolated host and deterministic PTY fixture as the integration tests.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import math
import platform
import statistics
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

import websockets
from websockets.asyncio.client import connect

from supermonoagent.protocol import OP_FOCUS, OP_HELLO, OP_HELLO_OK, OP_OUTPUT, OP_PING, OP_PONG, pack, pack_json, unpack
from test_client_integration import barrier, isolated_host, receive_op, subscribed


def resident_bytes():
    """Current process working set, or RSS high-water mark on Unix."""
    if sys.platform == "win32":
        class Counters(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
                (name, ctypes.c_size_t) for name in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                    "PagefileUsage", "PeakPagefileUsage")]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return counters.WorkingSetSize
        return None
    try:
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return rss if sys.platform == "darwin" else rss * 1024
    except ImportError:
        return None


def percentile95(values):
    return sorted(values)[max(0, math.ceil(len(values) * 0.95) - 1)]


async def scenario(count, relayed, pings, rounds, chunk_bytes):
    async with isolated_host() as (daemon, direct, relay):
        window = daemon.hub.open_window(owner="integration-owner")
        sessions = [daemon.hub.open_cli(window.id, "cmd") for _ in range(count)]
        ids = [session.id for session in sessions]
        async with subscribed(relay if relayed else direct, ids, relayed) as ws:
            cpu_started = time.process_time()
            started = time.perf_counter()
            latencies = []
            for i in range(pings):
                payload = str(i).encode()
                ping_started = time.perf_counter()
                await ws.send(pack(OP_PING, payload))
                if await receive_op(ws, OP_PONG) != payload:
                    raise AssertionError("PING payload mismatch")
                latencies.append((time.perf_counter() - ping_started) * 1000)
            payloads = {sid: (f"{index:04d}:".encode() + b"x" * chunk_bytes)[:chunk_bytes]
                        for index, sid in enumerate(ids)}
            received = 0
            transfer_started = time.perf_counter()
            for _ in range(rounds):
                # Each round fits the bounded queues even with 16 sessions.
                for sid in ids:
                    daemon.hub._flushed(sid, payloads[sid], False)
                seen = set()
                while len(seen) < count:
                    op, body = unpack(await asyncio.wait_for(ws.recv(), 5))
                    if op != OP_OUTPUT:
                        continue
                    sid_raw, data = body.split(b"\0", 1)
                    sid = sid_raw.decode()
                    if sid in seen or sid not in payloads or data != payloads[sid]:
                        raise AssertionError("Duplicated, misrouted, or corrupted terminal output")
                    seen.add(sid)
                    received += len(data)
            transfer_seconds = time.perf_counter() - transfer_started
            # Barrier must see no extra output, including duplicates after the last round.
            await ws.send(pack(OP_PING, b"finished"))
            while True:
                op, payload = unpack(await asyncio.wait_for(ws.recv(), 5))
                if op == OP_OUTPUT:
                    raise AssertionError("Unexpected duplicate output after transfer")
                if op == OP_PONG:
                    assert payload == b"finished"
                    break
            elapsed = time.perf_counter() - started
            cpu_seconds = time.process_time() - cpu_started
            expected = count * rounds * chunk_bytes
            assert received == expected
            return {
                "transport": "loopback-relay" if relayed else "direct-loopback", "sessions": count,
                "subscribers": 1, "ping_samples": pings,
                "ping_p50_ms": statistics.median(latencies), "ping_p95_ms": percentile95(latencies),
                "expected_payload_bytes": expected, "received_payload_bytes": received,
                "exact_delivery": True, "transfer_seconds": transfer_seconds,
                "throughput_mib_s": received / transfer_seconds / (1024 * 1024),
                "process_cpu_seconds": cpu_seconds,
                "process_cpu_percent_one_core": cpu_seconds / elapsed * 100,
                "process_rss_bytes": resident_bytes(), "measurement_seconds": elapsed,
            }


async def socket_amplification(count, chunk_bytes):
    """Compare exact network payload copies for the historical relay pane model."""
    async with isolated_host() as (daemon, _, relay):
        window = daemon.hub.open_window(owner="integration-owner")
        sessions = [daemon.hub.open_cli(window.id, "cmd") for _ in range(count)]
        ids = [session.id for session in sessions]
        async with AsyncExitStack() as stack:
            legacy = []
            for sid in ids:
                ws = await stack.enter_async_context(connect(relay, compression=None))
                await ws.send(json.dumps({"op": "phone", "code": "local-test", "token": "integration-owner"}))
                assert json.loads(await ws.recv())["op"] == "ready"
                await ws.send(pack_json(OP_HELLO, {"window": window.id}))
                await receive_op(ws, OP_HELLO_OK)
                await ws.send(pack_json(OP_FOCUS, {"session": sid}))
                await barrier(ws)
                legacy.append(ws)
            modern = await stack.enter_async_context(subscribed(relay, ids, True))
            for sid in ids:
                daemon.hub._flushed(sid, b"x" * chunk_bytes, False)

            async def read_bytes(ws):
                total = 0
                seen = set()
                for _ in ids:
                    payload = await receive_op(ws, OP_OUTPUT)
                    sid, data = payload.split(b"\0", 1)
                    assert sid.decode() in ids and sid not in seen and data == b"x" * chunk_bytes
                    seen.add(sid)
                    total += len(data)
                await ws.send(pack(OP_PING, b"amplification-end"))
                while True:
                    op, body = unpack(await asyncio.wait_for(ws.recv(), 5))
                    assert op != OP_OUTPUT, "Unexpected duplicate payload"
                    if op == OP_PONG:
                        assert body == b"amplification-end"
                        return total

            sizes = await asyncio.gather(*(read_bytes(ws) for ws in [*legacy, modern]))
            legacy_bytes, modern_bytes = sum(sizes[:-1]), sizes[-1]
            assert legacy_bytes == count * modern_bytes
            return {"sessions": count, "legacy_pane_sockets": count, "multiplex_sockets": 1,
                    "legacy_payload_bytes": legacy_bytes, "multiplex_payload_bytes": modern_bytes,
                    "payload_copy_ratio": legacy_bytes / modern_bytes, "exact_delivery": True}


async def run(args):
    results = []
    for count in (1, 4, 16):
        for relayed in (False, True):
            result = await scenario(count, relayed, args.pings, args.rounds, args.chunk_bytes)
            results.append(result)
            print(f"{result['transport']:16} {count:2} sessions  p95 {result['ping_p95_ms']:.3f} ms  "
                  f"{result['throughput_mib_s']:.2f} MiB/s  bytes {result['received_payload_bytes']}", flush=True)
    amplification = []
    if args.compare_legacy:
        for count in (1, 4, 16):
            result = await socket_amplification(count, args.chunk_bytes)
            amplification.append(result)
            print(f"Legacy relay {count} panes: {result['payload_copy_ratio']:.0f}x output payload vs one multiplex socket")
    report = {
        "schema": 1, "recorded_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "machine": {"platform": platform.platform(), "python": sys.version,
                    "processor": platform.processor(), "websockets": websockets.__version__},
        "config": {"sessions": [1, 4, 16], "ping_samples": args.pings, "rounds": args.rounds,
                   "chunk_bytes": args.chunk_bytes, "compression": "client-disabled; server-default",
                   "pty": "controlled deterministic; hub output injection excludes shell/read/pump cost",
                   "cpu_scope": "single process including benchmark client, daemon and local relay",
                   "rss_scope": "current process working set on Windows; Unix high-water RSS",
                   "network": "127.0.0.1 ephemeral ports only; no production relays"},
        "results": results,
        "legacy_socket_comparison": amplification,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {args.output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".omx/artifacts/benchmarks/current.json")
    parser.add_argument("--pings", type=int, default=200)
    parser.add_argument("--rounds", type=int, default=100)
    parser.add_argument("--chunk-bytes", type=int, default=8192)
    parser.add_argument("--compare-legacy", action="store_true", help="also measure relay per-pane socket byte amplification")
    args = parser.parse_args()
    if not (1 <= args.pings <= 1000 and 1 <= args.rounds <= 1000 and 16 <= args.chunk_bytes <= 32768):
        parser.error("pings/rounds must be 1..1000; chunk-bytes 16..32768")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
