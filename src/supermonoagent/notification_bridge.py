"""Read-only notification sidecar for an already running, older desktop daemon.

Subscribes locally, never sends terminal input or creates a shell. The existing
relay forwards a separate ``<room>-events`` room without a server upgrade.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import json
import struct
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from .protocol import (OP_HELLO, OP_HELLO_OK, OP_OUTPUT, OP_SESSIONS,
                       OP_SNAPSHOT, OP_SUBSCRIBE, OP_PING, OP_PONG,
                       OP_TASK_SUBSCRIBE, OP_TASK_EVENT, pack, pack_json,
                       unpack, unpack_json)
from .security import loopback, request_allowed
from .web import text_response
from .task_events import DEFAULT_SUMMARY, TaskEventStore, TaskStateDetector
from .defaults import migrate_legacy_relay


def socket_url(value: str, *, local: bool = False) -> str:
    parts = urlsplit(migrate_legacy_relay(value))
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
    if (scheme not in {"ws", "wss"} or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment
            or parts.path not in {"", "/", "/ws"}
            or scheme != 'wss' and not loopback(parts.hostname or '')
            or local and not loopback(parts.hostname)):
        raise ValueError("Expected a credential-free WebSocket endpoint" + (" on loopback" if local else ""))
    return urlunsplit((scheme, parts.netloc, "/ws", "", ""))


class NotificationBridge:
    def __init__(self, backend: str, token: str, store: TaskEventStore):
        self.backend = socket_url(backend, local=True)
        if not token:
            raise ValueError("An existing local owner token is required")
        self.token, self.store = token, store
        self.catalog: dict = {"windows": [], "groups": []}
        self.detectors: dict[str, TaskStateDetector] = {}
        self.subscribers: set[asyncio.Queue] = set()
        self.connected = False
        self.relay_connected = False
        self.last_probe = -10.0

    def authorized(self, token) -> bool:
        return isinstance(token, str) and hmac.compare_digest(token, self.token)

    def state(self) -> dict:
        return {"capabilities": {"task_events": True, "notification_only": True},
                "windows": [], "groups": [], "machine": self.catalog.get("machine", ""),
                "project": self.catalog.get("project", ""), "connected": self.connected}

    def receive(self, frame: bytes) -> bool:
        """Return whether the local catalog changed. Only live output is detected."""
        op, payload = unpack(frame)
        if op in {OP_HELLO_OK, OP_SESSIONS}:
            self.catalog = unpack_json(payload)
            sessions = self.sessions()
            self.detectors = {sid: self.detectors.get(sid, TaskStateDetector()) for sid in sessions}
            return True
        if op not in {OP_OUTPUT, OP_SNAPSHOT}:
            return False
        sid_raw, separator, raw = payload.partition(b"\0")
        if not separator:
            return False
        sid = sid_raw.decode("ascii", "ignore")
        metadata = self.sessions().get(sid)
        if metadata is None:
            return False
        if op == OP_SNAPSHOT:
            # Gaps, reconnect replay and overflow can never finish a running task.
            self.detectors[sid] = TaskStateDetector()
            return False
        detector = self.detectors.setdefault(sid, TaskStateDetector())
        for _ in range(detector.feed(raw)):
            event = self.store.append(self.token, {
                **metadata, "session": sid, "status": "completed",
                "machine": self.catalog.get("machine", ""),
                "project": self.catalog.get("project", ""),
                "summary": DEFAULT_SUMMARY, "source": "osc7501", "agent": detector.app,
            })
            frame = pack_json(OP_TASK_EVENT, event)
            for queue in tuple(self.subscribers):
                try:
                    queue.put_nowait(frame)
                except asyncio.QueueFull:
                    # Disconnect slow consumers; their durable cursor allows replay.
                    while not queue.empty():
                        queue.get_nowait()
                    queue.put_nowait(None)
                    self.subscribers.discard(queue)
        return False

    def sessions(self) -> dict:
        return {str(session["id"]): {"window": str(window["id"]),
                                    "title": str(window.get("title", ""))}
                for window in self.catalog.get("windows", [])
                for session in window.get("sessions", [])}

    async def observe(self):
        while True:
            try:
                async with connect(self.backend, max_size=1024 * 1024,
                                   additional_headers={"Authorization": "Bearer " + self.token}) as ws:
                    self.detectors.clear()
                    await ws.send(pack_json(OP_HELLO, {"token": self.token, "multiplex": True, "reliable": True}))
                    subscribed = None
                    async for message in ws:
                        if not isinstance(message, bytes):
                            continue
                        changed = self.receive(message)
                        self.connected = True
                        if changed:
                            wanted = sorted(self.sessions())
                            if wanted != subscribed:
                                await ws.send(pack_json(OP_SUBSCRIBE, {"sessions": wanted}))
                                subscribed = wanted
            except Exception:
                # Never log credential-bearing transport exceptions.
                pass
            finally:
                self.connected = False
                self.detectors.clear()
            await asyncio.sleep(2)

    def request(self, frame: bytes, queue: asyncio.Queue) -> list[bytes]:
        op, payload = unpack(frame)
        if op == 20:
            # This entry is reachable only after the socket/relay owner authenticates.
            nonce = unpack_json(payload).get('probe', '')
            now = time.monotonic()
            if not isinstance(nonce, str) or not re.fullmatch(r'[0-9a-f]{8,32}', nonce) or now-self.last_probe < 5:
                raise ValueError('Invalid or repeated notification probe')
            self.last_probe = now
            event = self.store.append(self.token, {'title':'通知链路自测 '+nonce,
                'machine':self.catalog.get('machine',''), 'project':self.catalog.get('project',''),
                'status':'completed', 'summary':'这是通知链路测试，不代表终端任务完成。'})
            reply = pack_json(OP_TASK_EVENT, event)
            for target in tuple(self.subscribers):
                try:target.put_nowait(reply)
                except asyncio.QueueFull:
                    while not target.empty():target.get_nowait()
                    target.put_nowait(None);self.subscribers.discard(target)
            return [reply]
        if op == OP_HELLO:
            body = unpack_json(payload)
            if body.get("token") and not self.authorized(body["token"]):
                raise PermissionError("Owner mismatch")
            return [pack_json(OP_HELLO_OK, self.state())]
        if op == OP_TASK_SUBSCRIBE:
            body = unpack_json(payload)
            self.subscribers.add(queue)
            if body.get("replay") is False:
                events = self.store.replay(self.token)
                return [pack_json(OP_TASK_EVENT, {"type": "cursor",
                        "cursor": events[-1]["event_id"] if events else ""})]
            events = self.store.replay(self.token, after=str(body.get("after") or ""))
            return [pack_json(OP_TASK_EVENT, event) for event in events]
        if op == OP_PING:
            return [pack(OP_PONG, payload)]
        raise ValueError("Notification-only endpoint")

    async def handle_socket(self, ws):
        queue = asyncio.Queue(32)
        sender = None
        try:
            first = await asyncio.wait_for(ws.recv(), 5)
            op, payload = unpack(first)
            if op != OP_HELLO or not self.authorized(unpack_json(payload).get("token")):
                raise PermissionError()
            await ws.send(pack_json(OP_HELLO_OK, self.state()))

            async def send():
                while True:
                    frame = await queue.get()
                    if frame is None:
                        await ws.close(code=1013, reason="Replay required")
                        return
                    await ws.send(frame)

            sender = asyncio.create_task(send())
            async for frame in ws:
                for reply in self.request(frame, queue):
                    await queue.put(reply)
        except Exception:
            await ws.close(code=4001, reason="Notification authorization or protocol error")
        finally:
            self.subscribers.discard(queue)
            if sender:
                sender.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sender

    async def serve_http(self, connection, request):
        if not request_allowed(connection, request):
            return text_response(connection, 403, "Forbidden", "text/plain")
        if request.path == "/health":
            return text_response(connection, 200, json.dumps({"ok": True,
                "role": "notification-bridge", "backend_connected": self.connected,
                "relay_connected": self.relay_connected,
                "subscribers": len(self.subscribers)}), "application/json")
        if request.path != "/ws":
            return text_response(connection, 404, "Not found", "text/plain")
        return None

    async def relay(self, url: str, code: str):
        endpoint = socket_url(url)
        while True:
            queues, jobs = {}, {}
            try:
                async with connect(endpoint, max_size=1024 * 1024) as ws:
                    await ws.send(json.dumps({"op": "host", "code": code, "service": "notifications",
                        "tokens": [self.token], "machine": self.catalog.get("machine", ""),
                        "project": self.catalog.get("project", "")}))
                    if json.loads(await ws.recv()).get("op") != "ready":
                        raise ValueError("Relay host rejected")
                    self.relay_connected = True

                    async def send(phone_id, queue):
                        while True:
                            frame = await queue.get()
                            if frame is None:
                                await ws.send(json.dumps({"op": "disconnect", "id": phone_id,
                                                         "code": 1013, "reason": "Replay required"}))
                                return
                            await ws.send(struct.pack("<I", phone_id) + frame)

                    def remove(phone_id):
                        queue = queues.pop(phone_id, None)
                        self.subscribers.discard(queue)
                        job = jobs.pop(phone_id, None)
                        if job:
                            job.cancel()

                    async for message in ws:
                        if isinstance(message, str):
                            body = json.loads(message)
                            phone_id = body.get("id")
                            if not isinstance(phone_id, int) or not 0 < phone_id <= 0xffffffff:
                                continue
                            if body.get("op") == "leave":
                                remove(phone_id)
                            elif body.get("op") == "join":
                                remove(phone_id)
                                if not self.authorized(body.get("token")) or "account" in body:
                                    await ws.send(json.dumps({"op": "disconnect", "id": phone_id,
                                                             "code": 4001, "reason": "Owner required"}))
                                    continue
                                queue = queues[phone_id] = asyncio.Queue(32)
                                jobs[phone_id] = asyncio.create_task(send(phone_id, queue))
                            continue
                        if len(message) < 9:
                            continue
                        phone_id = struct.unpack_from("<I", message)[0]
                        if phone_id not in queues:
                            continue
                        try:
                            for reply in self.request(message[4:], queues[phone_id]):
                                await queues[phone_id].put(reply)
                        except (ValueError, PermissionError):
                            remove(phone_id)
                            await ws.send(json.dumps({"op": "disconnect", "id": phone_id,
                                                     "code": 4001, "reason": "Notification-only endpoint"}))
            except Exception:
                pass
            finally:
                self.relay_connected = False
                for queue in queues.values():
                    self.subscribers.discard(queue)
                for job in jobs.values():
                    job.cancel()
                if jobs:
                    await asyncio.gather(*jobs.values(), return_exceptions=True)
            await asyncio.sleep(2)


async def run(args):
    state = args.state_dir
    token = (state / "token").read_text(encoding="utf8").strip()
    bridge = NotificationBridge(args.backend, token, TaskEventStore(state / "notification-bridge-events.json"))
    code = args.code if args.code is not None else args.code_file.read_text(encoding="utf8").strip()
    if not code:
        raise ValueError("An existing room code is required")
    async with serve(bridge.handle_socket, "127.0.0.1", args.port, max_size=1024 * 1024,
                     process_request=bridge.serve_http):
        await asyncio.gather(bridge.observe(), bridge.relay(args.relay, code + "-events"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--relay", required=True)
    room = parser.add_mutually_exclusive_group(required=True)
    room.add_argument("--code")
    room.add_argument("--code-file", type=Path)
    parser.add_argument("--port", type=int, default=6790)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
