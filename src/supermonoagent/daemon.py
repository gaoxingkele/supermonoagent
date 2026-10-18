"""One process owns the consoles and the phone sockets. Nothing polls."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import secrets
import struct
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.server import serve

from . import web
from .accounts import Accounts
from .hub import Hub
from .protocol import (
    OP_CLOSE,
    OP_FOCUS,
    OP_GROUP,
    OP_HELLO,
    OP_HELLO_OK,
    OP_INPUT,
    OP_OPEN,
    OP_PING,
    OP_PONG,
    OP_RESIZE,
    OP_SELECT,
    OP_WINDOW,
    ProtocolError,
    pack,
    pack_json,
    unpack,
    unpack_json,
)

STATE = Path.home() / ".supermonoagent"
QUEUE_FRAMES = 32


def load_token() -> str:
    path = STATE / "token"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    STATE.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(18)
    path.write_text(token, encoding="utf-8")
    return token


def load_code() -> str:
    path = STATE / "relay-code"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    STATE.mkdir(parents=True, exist_ok=True)
    code = secrets.token_hex(3)
    path.write_text(code, encoding="utf-8")
    return code


class Daemon:
    def __init__(self, token: str) -> None:
        self.token = token
        self.accounts = Accounts(STATE / "accounts.json")
        self.hub = Hub()
        self.hub.default_owner = token
        self.hub.load_desktop()
        self.relay_live: set[str] = set()
        self._relay_sockets: dict = {}
        self.relay_targets: list[dict] = []
        self._uplinks: dict[str, tuple[asyncio.Task, str]] = {}
        self._relay_slot = 0

    def set_relays(self, items) -> None:
        self.relay_targets = clean_relays(items)
        path = STATE / "relays.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.relay_targets, ensure_ascii=False, indent=2), encoding="utf-8")
        self.sync_relays()

    def sync_relays(self) -> None:
        wanted = {item["url"]: item["code"] for item in self.relay_targets}
        for url, (task, code) in list(self._uplinks.items()):
            if wanted.get(url) != code:
                task.cancel()
                self._uplinks.pop(url, None)
        for url, code in wanted.items():
            if url in self._uplinks:
                continue
            self._relay_slot += 1
            slot = str(self._relay_slot)
            self._uplinks[url] = (asyncio.create_task(relay_uplink(self, url, code, slot)), code)

    async def handle_socket(self, ws) -> None:
        query = ws.request.path.split("?", 1)
        offered = ""
        if len(query) == 2:
            for part in query[1].split("&"):
                if part.startswith("token="):
                    offered = part.split("=", 1)[1]
        from urllib.parse import unquote

        if not self.allowed(unquote(offered)):
            await self._auth_only(ws)
            return
        queue: asyncio.Queue = asyncio.Queue(QUEUE_FRAMES)
        client = self.hub.attach(queue, token=unquote(offered))
        sender = asyncio.create_task(self._send(ws, queue))
        try:
            async for message in ws:
                if isinstance(message, str):
                    continue
                try:
                    op, payload = unpack(message)
                except ProtocolError:
                    continue
                frames, close_code = await self._op(client, op, payload)
                if close_code:
                    await ws.close(code=close_code)
                    return
                for frame in frames:
                    try:
                        queue.put_nowait(frame)
                    except asyncio.QueueFull:
                        pass
        finally:
            self.hub.detach(client.key)
            sender.cancel()

    async def _send(self, ws, queue: asyncio.Queue) -> None:
        try:
            while True:
                frame = await queue.get()
                await ws.send(frame)
        except asyncio.CancelledError:
            return

    async def _op(self, client, op: int, payload: bytes):
        if op == OP_HELLO:
            body = unpack_json(payload) if payload else {}
            offered = str(body.get("token") or "")
            if offered and offered != client.token:
                if not client.token and self.allowed(offered):
                    client.token = offered
                else:
                    return [], 4001
            if not self.allowed(client.token):
                return [], 4001
            self.hub.ensure_owner(client.token)
            if body.get("channel") is not None:
                self.hub.subscribe(client, str(body.get("channel") or ""))
            chosen = body.get("window")
            if chosen in self.hub.windows and self.hub.owns(client.token, str(chosen)):
                self.hub.select(client, chosen)
            return [self._state(client)], None
        if op == OP_WINDOW:
            body = unpack_json(payload) if payload else {}
            window = self.hub.open_window(body.get("title"), str(body.get("group") or ""), client.token)
            self.hub.select(client, window.id)
            return [self._state(client)], None
        if op == OP_SELECT:
            body = unpack_json(payload)
            self.hub.select(client, str(body.get("window") or ""))
            return [self._state(client)], None
        if op == OP_OPEN:
            body = unpack_json(payload)
            window_id = str(body.get("window") or "")
            if not self.hub.owns(client.token, window_id):
                window_id = self.hub.open_window(group_id=str(body.get("group") or ""), owner=client.token).id
                self.hub.select(client, window_id)
            try:
                session = self.hub.open_cli(window_id, str(body.get("shell") or "powershell"), body.get("cwd"))
            except FileNotFoundError as exc:
                return [self._state(client, error=str(exc))], None
            self.hub.follow(client, session.id)
            return [self._state(client)], None
        if op == OP_FOCUS:
            body = unpack_json(payload)
            self.hub.focus(client, str(body.get("session") or ""))
            return [self._state(client)], None
        if op == OP_INPUT:
            self.hub.write(client, payload)
            return [], None
        if op == OP_RESIZE:
            body = unpack_json(payload)
            try:
                cols = int(body.get("cols", 100))
                rows = int(body.get("rows", 28))
            except (TypeError, ValueError):
                return [], None
            self.hub.resize(client, cols, rows)
            return [], None
        if op == OP_CLOSE:
            body = unpack_json(payload) if payload else {}
            if body.get("window"):
                if self.hub.owns(client.token, str(body["window"])):
                    self.hub.close_window(str(body["window"]))
            elif body.get("session"):
                session = self.hub.sessions.get(str(body["session"]))
                if session is not None and self.hub.owns(client.token, session.window_id):
                    self.hub.close_session(str(body["session"]))
            return [], None
        if op == OP_PING:
            return [pack(OP_PONG, payload)], None
        if op == OP_GROUP:
            body = unpack_json(payload) if payload else {}
            action = str(body.get("action") or "")
            if action == "create":
                self.hub.add_group(str(body.get("name") or ""), str(body.get("channel") or ""), client.token)
            elif action == "rename":
                self.hub.rename_group(str(body.get("id") or ""), body.get("name"), body.get("channel"), client.token)
            elif action == "delete":
                self.hub.delete_group(str(body.get("id") or ""), client.token)
            elif action == "move":
                self.hub.move_window(str(body.get("window") or ""), str(body.get("group") or ""), client.token)
            elif action == "subscribe":
                self.hub.subscribe(client, str(body.get("channel") or ""))
            elif action == "presets":
                if client.token == self.token:
                    self.hub.save_presets(body.get("presets"))
            elif action == "title":
                if self.hub.owns(client.token, str(body.get("window") or "")):
                    self.hub.rename_window(str(body.get("window") or ""), str(body.get("title") or ""))
            elif action == "autotitle":
                if self.hub.owns(client.token, str(body.get("window") or "")):
                    self.hub.autotitle(str(body.get("window") or ""))
            elif action == "relays":
                if client.token == self.token:
                    self.set_relays(body.get("relays"))
            return [self._state(client)], None
        return [], None

    def allowed(self, token: str) -> bool:
        if token and token == self.token:
            return True
        return bool(token) and token in self.accounts.tokens()

    def account_reply(self, body: dict) -> dict:
        email = str(body.get("email") or body.get("name") or "")
        op = str(body.get("op") or "")
        try:
            if op == "register":
                token = self.accounts.register(email, str(body.get("password") or ""))
            elif op == "login":
                token = self.accounts.login(email, str(body.get("password") or ""))
            elif op == "rotate":
                token = self.accounts.rotate(email, str(body.get("password") or ""))
            else:
                raise ValueError("需要登录或注册")
        except ValueError as exc:
            return {"op": "auth", "id": body.get("id"), "error": str(exc)}
        self.hub.ensure_owner(token)
        return {"op": "auth", "id": body.get("id"), "token": token, "email": email}

    def access_tokens(self) -> list[str]:
        found = []
        if self.token:
            found.append(self.token)
        for token in self.accounts.tokens():
            if token not in found:
                found.append(token)
        return found

    def _local_peer(self, ws) -> bool:
        try:
            host = ws.remote_address[0]
        except Exception:
            return False
        return host in {"127.0.0.1", "::1"}

    async def _auth_only(self, ws) -> None:
        if not self._local_peer(ws):
            await ws.close(code=4001)
            return
        try:
            raw = await asyncio.wait_for(ws.recv(), 8)
            body = json.loads(raw) if isinstance(raw, str) else {}
        except (asyncio.TimeoutError, json.JSONDecodeError, TypeError):
            await ws.close(code=4001)
            return
        reply = self.account_reply(body)
        if reply.get("token"):
            self.publish_tokens()
        await ws.send(json.dumps(reply, ensure_ascii=False))
        await ws.close()

    def publish_tokens(self) -> None:
        payload = json.dumps({"op": "tokens", "tokens": self.access_tokens()})
        for sock in list(getattr(self, "_relay_sockets", {}).values()):
            try:
                asyncio.get_running_loop().create_task(sock.send(payload))
            except RuntimeError:
                pass

    def service_status(self) -> dict:
        relays = []
        for item in self.relay_targets:
            relays.append({"name": item.get("name") or item["url"], "url": item["url"], "up": item["url"] in self.relay_live})
        return {
            "ok": True,
            "accounts": len(self.accounts.users),
            "windows": len(self.hub.windows),
            "sessions": len(self.hub.sessions),
            "relays": relays,
        }

    def publish_status(self) -> None:
        for client in list(self.hub.clients.values()):
            if client.key.startswith("relay-"):
                continue
            self.hub._send(client, self._state(client))

    def _state(self, client, error: str | None = None) -> bytes:
        visible = self.hub.summary(client.token)
        service = self.service_status()
        service["windows"] = len(visible["windows"])
        service["sessions"] = sum(len(item["sessions"]) for item in visible["windows"])
        body = {**visible, "selected": client.window_id or "", "session": client.session_id or "", "service": service}
        if error:
            body["error"] = error
        return pack_json(OP_HELLO_OK, body)

    async def serve_http(self, connection, request):
        page = importlib.reload(web)
        path = request.path.split("?", 1)[0]
        if path in page.STATIC:
            name, content_type = page.STATIC[path]
            return page.text_response(connection, 200, page.static_text(name), content_type)
        if path in {"/", "/index.html"}:
            return page.text_response(connection, 200, page.PAGE, "text/html; charset=utf-8")
        if path == "/health":
            payload = {"ok": True, "windows": len(self.hub.windows), "sessions": len(self.hub.sessions)}
            try:
                peer = connection.remote_address[0]
            except Exception:
                peer = ""
            if peer in {"127.0.0.1", "::1"}:
                payload["default_token"] = self.token
            return page.text_response(connection, 200, json.dumps(payload), "application/json")
        if path != "/ws":
            return page.text_response(connection, 404, "not found", "text/plain; charset=utf-8")
        return None


def load_relays() -> list[dict]:
    path = STATE / "relays.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def clean_relays(items) -> list[dict]:
    cleaned = []
    seen = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        code = str(item.get("code") or "").strip()
        if url.startswith("http://"):
            url = "ws://" + url[7:]
        elif url.startswith("https://"):
            url = "wss://" + url[8:]
        elif url and not url.startswith(("ws://", "wss://")):
            url = "ws://" + url
        url = url.rstrip("/")
        parts = urlsplit(url)
        if parts.scheme in {"ws", "wss"} and parts.path in {"", "/"}:
            url = urlunsplit((parts.scheme, parts.netloc, "/ws", parts.query, ""))
        if not url or not code or url in seen:
            continue
        seen.add(url)
        name = str(item.get("name") or url).strip()[:40] or url
        cleaned.append({"name": name, "url": url, "code": code})
    return cleaned


async def relay_uplink(daemon: Daemon, url: str, code: str, slot: str) -> None:
    import websockets

    while True:
        try:
            async with websockets.connect(url, max_size=1024 * 1024) as ws:
                await ws.send(json.dumps({"op": "host", "code": code, "tokens": daemon.access_tokens()}))
                ack = json.loads(await ws.recv())
                if ack.get("op") != "ready":
                    raise RuntimeError(ack)
                daemon._relay_sockets[url] = ws
                daemon.relay_live.add(url)
                daemon.publish_status()
                uplink: asyncio.Queue = asyncio.Queue(QUEUE_FRAMES)
                phones: dict[int, asyncio.Task] = {}

                async def outbound() -> None:
                    while True:
                        phone_id, frame = await uplink.get()
                        await ws.send(struct.pack("<I", phone_id) + frame)

                async def bridge(phone_id: int, queue: asyncio.Queue) -> None:
                    try:
                        while True:
                            await uplink.put((phone_id, await queue.get()))
                    except asyncio.CancelledError:
                        return

                task = asyncio.create_task(outbound())
                try:
                    async for message in ws:
                        if isinstance(message, str):
                            ctrl = json.loads(message)
                            phone_id = int(ctrl.get("id") or 0)
                            if ctrl.get("op") in {"register", "login", "rotate"}:
                                reply = daemon.account_reply(ctrl)
                                if reply.get("token"):
                                    await ws.send(json.dumps({"op": "tokens", "tokens": daemon.access_tokens()}))
                                await ws.send(json.dumps(reply, ensure_ascii=False))
                                continue
                            if ctrl.get("op") == "join":
                                phone_token = str(ctrl.get("token") or "")
                                if not daemon.allowed(phone_token):
                                    continue
                                queue = asyncio.Queue(QUEUE_FRAMES)
                                daemon.hub.attach(queue, key=f"relay-{slot}-{phone_id}", token=phone_token)
                                daemon.hub.ensure_owner(phone_token)
                                phones[phone_id] = asyncio.create_task(bridge(phone_id, queue))
                            elif ctrl.get("op") == "leave":
                                daemon.hub.detach(f"relay-{slot}-{phone_id}")
                                job = phones.pop(phone_id, None)
                                if job is not None:
                                    job.cancel()
                            continue
                        if len(message) < 5:
                            continue
                        phone_id = struct.unpack_from("<I", message)[0]
                        client = daemon.hub.clients.get(f"relay-{slot}-{phone_id}")
                        if client is None:
                            continue
                        try:
                            op, payload = unpack(message[4:])
                        except ProtocolError:
                            continue
                        frames, _close = await daemon._op(client, op, payload)
                        for frame in frames:
                            try:
                                client.queue.put_nowait(frame)
                            except asyncio.QueueFull:
                                pass
                finally:
                    daemon.relay_live.discard(url)
                    daemon._relay_sockets.pop(url, None)
                    daemon.publish_status()
                    for phone_id in list(phones):
                        daemon.hub.detach(f"relay-{slot}-{phone_id}")
                        phones[phone_id].cancel()
                    task.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"relay {url} {exc}", flush=True)
            await asyncio.sleep(2)


def main() -> None:
    parser = argparse.ArgumentParser(description="SuperMonoAgent host")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6769)
    parser.add_argument("--relay", default="", help="ws://relay:6770, outbound only")
    parser.add_argument("--code", default="")
    args = parser.parse_args()
    token = load_token()
    code = args.code or load_code()
    daemon = Daemon(token)
    targets = load_relays()
    if args.relay:
        targets = list(targets) + [{"name": args.relay, "url": args.relay, "code": code}]
    daemon.relay_targets = clean_relays(targets)

    async def run() -> None:
        daemon.hub.attach_loop(asyncio.get_running_loop())
        daemon.sync_relays()
        async with serve(
            daemon.handle_socket,
            args.host,
            args.port,
            process_request=daemon.serve_http,
            max_size=256 * 1024,
            ping_interval=25,
            ping_timeout=25,
        ):
            print(f"page  http://{args.host}:{args.port}/?token={token}")
            for item in daemon.relay_targets:
                print(f"relay {item['url']}  code {item['code']}")
            await asyncio.Future()
        for task, _code in daemon._uplinks.values():
            task.cancel()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
