"""Move an existing local daemon's relay uplink to WSS without restarting PTYs.

The bridge supports only its existing local owner, not delegated relay accounts.
Each authenticated phone gets a separate local daemon WebSocket and unchanged
binary frames; this process never creates a terminal on its own.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
import hmac
import http.client
import json
import logging
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from .protocol import OP_HELLO, OP_HELLO_OK, OP_SESSIONS, pack, pack_json, unpack, unpack_json
from .security import loopback, request_allowed
from .web import text_response
from .defaults import DEFAULT_RELAY

MAX_SIZE = 1024 * 1024
QUEUE_FRAMES = 32
MAX_PHONES = 64
HEALTH_PORT = 6791
_START_LOCK = threading.Lock()
_STARTED: dict[tuple[str, str], float] = {}


def socket_url(value: str, *, local: bool = False) -> str:
    """Allow public TLS relays and loopback-only plaintext development fixtures."""
    try:
        parts = urlsplit(value)
        scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
        port = parts.port
        if (scheme not in {"ws", "wss"} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or parts.path not in {"", "/", "/ws", "/ws/"}
                or local and not loopback(parts.hostname)
                or scheme == "ws" and not loopback(parts.hostname)
                or port == 0):
            raise ValueError()
    except ValueError:
        raise ValueError("Expected a secure relay or loopback-only backend endpoint") from None
    return urlunsplit((scheme, parts.netloc, "/ws", "", ""))


@dataclass(eq=False)
class Phone:
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(QUEUE_FRAMES))
    job: asyncio.Task | None = None


class RelayBridge:
    def __init__(self, backend: str, token: str, *, machine: str = "", project: str = "", retry_s: float = 2):
        self.backend = socket_url(backend, local=True)
        if not token or len(token) > 4096 or "\r" in token or "\n" in token:
            raise ValueError("An existing owner credential is required")
        self.token = token
        self.machine, self.project = machine[:40], project[:40]
        self.retry_s = retry_s
        self.backend_connected = False
        self.relay_connected = False
        self.relay_endpoint = ""
        self._native_relay_endpoints = frozenset()
        self._backend_changed = asyncio.Event()
        self.phones: dict[int, Phone] = {}
        self.footer_recovery = None

    def authorized(self, token) -> bool:
        return isinstance(token, str) and hmac.compare_digest(token.encode(), self.token.encode())

    def health(self) -> dict:
        return {"ok": True, "role": "relay-bridge", "backend_connected": self.backend_connected,
                "relay_connected": self.relay_connected, "phones": len(self.phones),
                "direct_relay_managed": self.direct_relay_managed,
                "backend": self.backend, "relay": self.relay_endpoint}

    @staticmethod
    def _endpoint_key(value):
        parts = urlsplit(socket_url(value))
        return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "wss" else 80)

    @property
    def direct_relay_managed(self):
        return bool(self.relay_endpoint) and self._endpoint_key(self.relay_endpoint) in self._native_relay_endpoints

    def _backend_state(self, body, *, greeting=False):
        endpoints = self._native_relay_endpoints
        service = body.get("service")
        relays = service.get("relays") if isinstance(service, dict) else None
        if isinstance(relays, list):
            configured = set()
            for target in relays:
                if not isinstance(target, dict) or not isinstance(target.get("url"), str):
                    continue
                try:
                    configured.add(self._endpoint_key(target["url"]))
                except ValueError:
                    continue
            endpoints = frozenset(configured)
        elif greeting:
            endpoints = frozenset()
        changed = not self.backend_connected or endpoints != self._native_relay_endpoints
        self._native_relay_endpoints = endpoints
        self.backend_connected = True
        if changed:
            self._backend_changed.set()

    def _local_socket(self):
        return connect(self.backend, additional_headers={"Authorization": "Bearer " + self.token},
                       max_size=MAX_SIZE, max_queue=QUEUE_FRAMES, compression=None,
                       open_timeout=5, close_timeout=2)

    async def observe_backend(self):
        while True:
            try:
                async with self._local_socket() as ws:
                    await ws.send(pack_json(OP_HELLO, {"token": self.token, "multiplex": True, "reliable": True}))
                    authenticated = False
                    async for message in ws:
                        if not isinstance(message, bytes):
                            continue
                        op, payload = unpack(message)
                        if op in {OP_HELLO_OK, OP_SESSIONS}:
                            if not authenticated and op != OP_HELLO_OK:
                                raise ValueError("Backend HELLO acknowledgement required")
                            body = unpack_json(payload)
                            authenticated = True
                            self.machine = str(body.get("machine") or self.machine)[:40]
                            self.project = str(body.get("project") or self.project)[:40]
                            self._backend_state(body, greeting=op == OP_HELLO_OK)
            except Exception:
                # Transport exceptions can include URLs/headers. Never log secrets.
                pass
            finally:
                self.backend_connected = False
                self._backend_changed.set()
            await asyncio.sleep(self.retry_s)

    def _validate_phone_frame(self, frame: bytes, first: bool) -> None:
        op, payload = unpack(frame)
        if first and op != OP_HELLO:
            raise ValueError("HELLO required")
        if op == OP_HELLO:
            body = unpack_json(payload)
            if (not self.authorized(body.get("token"))
                    or any(name in body for name in ("account", "session", "password"))):
                raise ValueError("Owner required")

    async def _phone(self, phone_id: int, route: Phone, outbound: asyncio.Queue):
        jobs = []
        try:
            async with self._local_socket() as local:
                first = await asyncio.wait_for(route.queue.get(), 5)
                self._validate_phone_frame(first, True)
                await local.send(first)

                async def inbound():
                    while True:
                        frame = await route.queue.get()
                        self._validate_phone_frame(frame, False)
                        await local.send(frame)

                async def replies():
                    state = {}
                    recovery_waiting = set()
                    async for frame in local:
                        if not isinstance(frame, bytes):
                            raise ValueError("Binary daemon frames required")
                        opcode, payload = unpack(frame)
                        if opcode in {OP_HELLO_OK, OP_SESSIONS}:
                            state = unpack_json(payload)
                        elif opcode == 8:
                            sid, separator, _raw = payload.partition(b'\0')
                            if separator and len(recovery_waiting) < 64:
                                recovery_waiting.add(sid)
                        await outbound.put((phone_id, route, struct.pack("<I", phone_id) + frame))
                        if opcode in {OP_HELLO_OK, OP_SESSIONS, 8} and self.footer_recovery is not None:
                            for sid in tuple(recovery_waiting):
                                if sid.decode('utf-8', 'replace') not in state.get('controls', {}):
                                    continue
                                recovery_waiting.discard(sid)
                                supplement = await asyncio.to_thread(self.footer_recovery.supplement, pack(8, sid+b'\0'), state)
                                if supplement is not None:
                                    await outbound.put((phone_id, route, struct.pack("<I", phone_id) + supplement))

                jobs = [asyncio.create_task(inbound()), asyncio.create_task(replies())]
                done, _ = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
                for job in done:
                    job.result()
        except Exception:
            pass
        finally:
            for job in jobs:
                job.cancel()
            if jobs:
                await asyncio.gather(*jobs, return_exceptions=True)
            if self.phones.get(phone_id) is route:
                self.phones.pop(phone_id, None)
                await outbound.put((phone_id, None, json.dumps({"op": "disconnect", "id": phone_id,
                                      "code": 4001, "reason": "Local owner connection ended"})))

    async def _relay_once(self, endpoint: str, code: str):
        outbound = asyncio.Queue(QUEUE_FRAMES)
        writer = None
        async with connect(endpoint, max_size=MAX_SIZE, max_queue=QUEUE_FRAMES,
                           compression=None, open_timeout=5, close_timeout=2) as ws:
            await ws.send(json.dumps({"op": "host", "code": code, "tokens": [self.token],
                                      "machine": self.machine, "project": self.project}))
            ready = await asyncio.wait_for(ws.recv(), 5)
            if not isinstance(ready, str) or json.loads(ready).get("op") != "ready":
                raise ValueError("Relay host rejected")
            self.relay_connected = True

            async def send():
                try:
                    while True:
                        phone_id, route, frame = await outbound.get()
                        if route is None or self.phones.get(phone_id) is route:
                            await ws.send(frame)
                finally:
                    await ws.close()

            async def disconnect(phone_id, reason, code=4001):
                route = self.phones.pop(phone_id, None)
                if route and route.job:
                    route.job.cancel()
                    await asyncio.gather(route.job, return_exceptions=True)
                await outbound.put((phone_id, None, json.dumps({"op": "disconnect", "id": phone_id,
                                                              "code": code, "reason": reason})))

            writer = asyncio.create_task(send())
            try:
                async for message in ws:
                    if isinstance(message, str):
                        body = json.loads(message)
                        phone_id = body.get("id")
                        if not isinstance(phone_id, int) or not 0 < phone_id <= 0xffffffff:
                            continue
                        if body.get("op") in {"leave", "join"}:
                            route = self.phones.pop(phone_id, None)
                            if route and route.job:
                                route.job.cancel()
                                await asyncio.gather(route.job, return_exceptions=True)
                        if body.get("op") == "join":
                            if (not self.authorized(body.get("token"))
                                    or any(name in body for name in ("account", "expires", "session"))):
                                if "account" in body:
                                    await outbound.put((phone_id, None, json.dumps({"op": "joined", "id": phone_id, "ok": False})))
                                await disconnect(phone_id, "Local owner required")
                            elif len(self.phones) >= MAX_PHONES:
                                await disconnect(phone_id, "Bridge capacity reached", 1013)
                            else:
                                route = self.phones[phone_id] = Phone()
                                route.job = asyncio.create_task(self._phone(phone_id, route, outbound))
                        continue
                    if len(message) < 9:
                        continue
                    phone_id = struct.unpack_from("<I", message)[0]
                    route = self.phones.get(phone_id)
                    if route is None:
                        continue
                    try:
                        route.queue.put_nowait(message[4:])
                    except asyncio.QueueFull:
                        await disconnect(phone_id, "Input queue overflow; reconnect", 1013)
            finally:
                self.relay_connected = False
                jobs = [route.job for route in self.phones.values() if route.job]
                self.phones.clear()
                for job in jobs:
                    job.cancel()
                if jobs:
                    await asyncio.gather(*jobs, return_exceptions=True)
                writer.cancel()
                await asyncio.gather(writer, return_exceptions=True)

    async def relay(self, url: str, code: str):
        endpoint = socket_url(url)
        self.relay_endpoint = endpoint
        if not code or len(code) > 256:
            raise ValueError("An existing room code is required")
        while True:
            self._backend_changed.clear()
            if not self.backend_connected or self.direct_relay_managed:
                await self._backend_changed.wait()
                continue
            uplink = asyncio.create_task(self._relay_once(endpoint, code))
            try:
                while not uplink.done():
                    self._backend_changed.clear()
                    if not self.backend_connected or self.direct_relay_managed:
                        break
                    changed = asyncio.create_task(self._backend_changed.wait())
                    try:
                        await asyncio.wait((uplink, changed), return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        changed.cancel()
                        await asyncio.gather(changed, return_exceptions=True)
                if uplink.done():
                    uplink.result()
            except Exception:
                pass
            finally:
                uplink.cancel()
                await asyncio.gather(uplink, return_exceptions=True)
                self.relay_connected = False
            if self.backend_connected and not self.direct_relay_managed:
                await asyncio.sleep(self.retry_s)

    async def serve_http(self, connection, request):
        if not request_allowed(connection, request):
            return text_response(connection, 403, "Forbidden", "text/plain")
        if request.path == "/health":
            return text_response(connection, 200, json.dumps(self.health()), "application/json")
        return text_response(connection, 404, "Not found", "text/plain")


def _local_health() -> tuple[bool, dict | None]:
    """No proxy, redirect, credential, or public network is involved in this probe."""
    connection = http.client.HTTPConnection("127.0.0.1", HEALTH_PORT, timeout=.15)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        raw = response.read(8193)
        if response.status != 200 or len(raw) > 8192:
            return True, None
        value = json.loads(raw)
        return True, value if isinstance(value, dict) else None
    except ConnectionRefusedError:
        return False, None
    except (OSError, ValueError, http.client.HTTPException):
        # A listening but unresponsive/foreign service must not be replaced.
        return True, None
    finally:
        connection.close()


def ensure_running(backend: str, state_dir: Path) -> dict | None:
    """Start an explicitly opted-in fixed WSS bridge without waiting for relay IO.

    Absence of secure-relay.json is a strict no-op, including no health probe.
    Existing unrelated services are left untouched. Return matching existing
    health, or None when disabled, invalid, unavailable, or just background-started.
    """
    state_dir = Path(state_dir)
    config_path = state_dir / "secure-relay.json"
    try:
        if not config_path.is_file() or config_path.stat().st_size > 8192:
            return None
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict) or config.get("enabled") is not True:
            return None
        canonical_backend = socket_url(backend, local=True)
        # This migration is for one user-approved server, never an env override.
        if config.get("relay") != DEFAULT_RELAY or config.get("port", HEALTH_PORT) != HEALTH_PORT:
            return None
        if "backend" in config and socket_url(config["backend"], local=True) != canonical_backend:
            return None
        token_file, code_file = state_dir / "token", state_dir / "relay-code"
        if not (token_file.is_file() and code_file.is_file()
                and 0 < token_file.stat().st_size <= 4096 and 0 < code_file.stat().st_size <= 1024):
            return None
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    with _START_LOCK:
        present, health = _local_health()
        if present:
            if (health and health.get("ok") is True and health.get("role") == "relay-bridge"
                    and health.get("backend") == canonical_backend and health.get("relay") == DEFAULT_RELAY):
                return health
            logging.getLogger(__name__).warning("Secure relay bridge health port is occupied by another service")
            return None
        key = (canonical_backend, str(state_dir.resolve()))
        if time.monotonic() - _STARTED.get(key, -100) < 5:
            return None
        executable = Path(sys.executable)
        if getattr(sys, "frozen", False):
            command = [str(executable), "relay-bridge"]
        else:
            pythonw = executable.with_name("pythonw.exe")
            if sys.platform == "win32" and pythonw.is_file():
                executable = pythonw
            command = [str(executable), "-m", "supermonoagent.relay_bridge"]
        command += ["--backend", canonical_backend, "--state-dir", str(state_dir.resolve()),
                    "--relay", DEFAULT_RELAY, "--code-file", str(code_file.resolve()), "--port", str(HEALTH_PORT)]
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        try:
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        except OSError:
            logging.getLogger(__name__).warning("Secure relay bridge background startup failed")
            return None
        _STARTED[key] = time.monotonic()
    return None


async def run(args):
    token = (args.state_dir / "token").read_text(encoding="utf8").strip()
    code = args.code_file.read_text(encoding="utf8").strip()
    bridge = RelayBridge(args.backend, token)
    from .console_snapshot import FooterRecovery
    bridge.footer_recovery = FooterRecovery(args.state_dir, args.backend)
    # Validate configuration before creating long-lived tasks.
    socket_url(args.relay)
    async def reject(ws):
        await ws.close(code=4001, reason="Health endpoint only")
    async with serve(reject, "127.0.0.1", args.port, process_request=bridge.serve_http, max_size=MAX_SIZE):
        observer = asyncio.create_task(bridge.observe_backend())
        try:
            # relay() waits for authenticated metadata and defers to a native uplink.
            await bridge.relay(args.relay, code)
        finally:
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--relay", required=True)
    parser.add_argument("--code-file", type=Path, required=True)
    parser.add_argument("--port", type=int, default=6791)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (OSError, ValueError):
        raise SystemExit("Relay bridge configuration or startup failed") from None


if __name__ == "__main__":
    main()
