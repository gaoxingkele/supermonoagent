"""One process owns the consoles and phone sockets without polling terminals."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import math
import os
import secrets
import socket
import struct
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.server import serve

from . import web
from .security import request_allowed, tls_context, transport_options, loopback, FIRST_FRAME_S
from .accounts import Accounts
from .defaults import migrate_legacy_relay
from .hub import Hub
from .history_context import unpack_history_context, valid_stream_id
from .task_events import TaskEventStore

from .protocol import (
    OP_CLOSE,
    OP_DAEMON_LOGS,
    OP_FILE_TRANSFER,
    OP_ASSISTANT_CONTEXT,
    OP_HISTORY_CONTEXT,
    OP_FOCUS,
    OP_GROUP,
    OP_HELLO,
    OP_HELLO_OK,
    OP_INPUT,
    OP_INPUT_SESSION,
    OP_OPEN,
    OP_OUTPUT,
    OP_PING,
    OP_PONG,
    OP_RESIZE,
    OP_SELECT,
    OP_SUBSCRIBE,
    OP_SNAPSHOT,
    OP_TASK_SUBSCRIBE,
    OP_WINDOW,
    ProtocolError,
    pack,
    pack_json,
    unpack,
    unpack_json,
)

LOGGER = logging.getLogger('supermonoagent.daemon')
LOGGER.addHandler(logging.NullHandler())
STATE = Path(os.environ.get("SUPERMONO_STATE_DIR") or Path.home() / ".supermonoagent")
# Bounded per-client output queue. Interactive UI clients stall while parsing
# multi-hundred-KB snapshot replays; the bound must outlast a replay round at
# realistic live-output rates or "reliable" clients livelock on 1013 reconnects.
QUEUE_FRAMES = 256


def log_endpoint(value: str) -> str:
    """Describe a relay without URL credentials, query strings or private paths."""
    try:
        parts = urlsplit(value)
        host = parts.hostname or '?'
        if ':' in host: host = '['+host+']'
        return parts.scheme+'://'+host+(':'+str(parts.port) if parts.port else '')
    except ValueError:
        return '<invalid endpoint>'


def relay_binding_url(value: str) -> str:
    """Canonical trusted relay endpoint; never accept credential-bearing URLs."""
    parts = urlsplit(str(value).strip())
    scheme = {"https": "wss", "http": "ws"}.get(parts.scheme, parts.scheme)
    if (scheme not in {"ws", "wss"} or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or parts.path not in {"", "/", "/ws", "/ws/"}):
        raise ValueError("Use an explicit relay https/wss URL without credentials")
    if scheme != "wss" and not loopback(parts.hostname):
        raise ValueError("Public relay account bindings require wss")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    authority = host if port in {None, 443 if scheme == "wss" else 80} else f"{host}:{port}"
    return urlunsplit((scheme, authority, "/ws", "", ""))


class RelayBindings:
    """Explicit local owner mappings, independently revocable from relay grants."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[tuple[str, str, str], str] = {}
        self._stamp = None
        self.reload()

    def reload(self) -> None:
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp == self._stamp:
                return
            data = json.loads(self.path.read_text(encoding="utf8"))
            entries = {}
            for entry in data.get("bindings", []):
                key = (relay_binding_url(entry["url"]), str(entry["code"]), str(entry["account"]))
                if all(key) and isinstance(entry.get("owner"), str) and entry["owner"]:
                    entries[key] = entry["owner"]
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            self.entries = {}
            self._stamp = None
            return
        self.entries = entries
        self._stamp = stamp

    def owner(self, url: str, code: str, account: str) -> str | None:
        self.reload()
        return self.entries.get((relay_binding_url(url), code, account))

    def bind(self, url: str, code: str, account: str, owner: str | None) -> None:
        self.reload()
        key = (relay_binding_url(url), code.strip(), account.strip())
        if not all(key) or len(account) > 128:
            raise ValueError("Room code and account ID are required")
        if owner:
            self.entries[key] = owner
        else:
            self.entries.pop(key, None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + "." + secrets.token_hex(6) + ".tmp")
        try:
            temporary.write_text(json.dumps({"bindings": [
                {"url": url, "code": code, "account": account, "owner": owner}
                for (url, code, account), owner in sorted(self.entries.items())
            ]}, ensure_ascii=False, indent=2), encoding="utf8")
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self._stamp = None
        self.reload()


@dataclass
class RelayDelegation:
    client: object
    url: str
    code: str
    account: str
    expires: float
    owner: str
    generation: object


def load_token(explicit: str | None = None) -> str:
    path = STATE / "token"
    if explicit is not None:
        if not explicit or len(explicit) > 4096 or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in explicit):
            raise ValueError("Token must contain 1 to 4096 characters without whitespace or control characters")
        STATE.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=STATE, prefix=".token-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(explicit)
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return explicit
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    STATE.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(18)
    path.write_text(token, encoding="utf-8")
    return token


def default_machine() -> str:
    name = (socket.gethostname() or "").strip()
    return name[:40] or "电脑"


def load_identity() -> dict:
    path = STATE / "machine.json"
    data = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    project = str(data.get("project") or "默认")[:40] or "默认"
    return {"machine": default_machine(), "project": project}


def save_identity(machine: str, project: str) -> dict:
    ident = {
        "machine": default_machine(),
        "project": str(project or "默认")[:40] or "默认",
    }
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "machine.json").write_text(json.dumps(ident, ensure_ascii=False, indent=2), encoding="utf-8")
    return ident


def _new_room_name(explicit: str | None = None) -> str:
    code = (explicit or "").strip() or default_machine()
    if len(code) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in code):
        raise ValueError("Room name must contain 1 to 128 characters without control characters")
    return code


def load_code(explicit: str | None = None) -> str:
    path = STATE / "relay-code"
    if explicit is None and path.exists():
        saved = path.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    code = _new_room_name(explicit)
    STATE.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=STATE, prefix=".relay-code-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(code)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return code


class Daemon:
    def __init__(self, token: str) -> None:
        self.token = token
        self.started_at = time.monotonic()
        self.log_state_dir = Path(STATE).resolve()
        self.receive_dir = self.log_state_dir / 'received-files'
        self.file_receiver = None
        self._file_receiver_lock = asyncio.Lock()
        self._file_cleanup_tasks = set()
        self._sidecar_workers = {}
        self._sidecar_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='sma-files')
        self.accounts = Accounts(STATE / "accounts.json")
        self.hub = Hub()
        self.hub.task_events = TaskEventStore(STATE / "task-events.json")
        self.hub.default_owner = token
        self.hub.load_desktop()
        ident = load_identity()
        self.hub.set_identity(ident["machine"], ident["project"])
        self.relay_live: set[str] = set()
        self._relay_sockets: dict = {}
        self._direct_sockets: dict = {}
        self.relay_targets: list[dict] = []
        self._uplinks: dict[str, tuple[asyncio.Task, str]] = {}
        self._relay_slot = 0
        self.relay_bindings = RelayBindings(STATE / "relay-bindings.json")
        self._relay_generations: dict[str, object] = {}
        self._delegations: dict[int, RelayDelegation] = {}

    def client_authorized(self, client) -> bool:
        if self.hub.clients.get(client.key) is not client or client.failure:
            return False
        if not client.delegated:
            return self.allowed(client.token)
        grant = self._delegations.get(id(client))
        if grant is None or grant.client is not client or grant.owner != client.token:
            return False
        return (self._relay_generations.get(grant.url) is grant.generation
                and grant.expires > time.time()
                and self.relay_bindings.owner(grant.url, grant.code, grant.account) == grant.owner)

    def delegated_client(self, url: str, code: str, generation: object, phone_id: int, ctrl: dict, key: str):
        account, expires = ctrl.get("account"), ctrl.get("expires")
        try:
            deadline = float(expires) if isinstance(expires, (int, float)) and not isinstance(expires, bool) else 0.0
        except OverflowError:
            return None
        if (not isinstance(account, str) or not account or len(account) > 128
                or not math.isfinite(deadline) or deadline <= time.time()
                or self._relay_generations.get(url) is not generation
                or any(name in ctrl for name in ("token", "session", "password"))):
            return None
        owner = self.relay_bindings.owner(url, code, account)
        if not owner:
            return None
        client = self.hub.attach(asyncio.Queue(QUEUE_FRAMES), key=key, token=owner)
        client.delegated = True
        self._delegations[id(client)] = RelayDelegation(client, url, code, account, deadline, owner, generation)
        client.authorize = lambda: self.client_authorized(client)
        client.reliable = ctrl.get("reliable") is True
        self.hub.ensure_owner(owner)
        return client

    def forget_relay_client(self, client, reason: str = "Relay authorization ended") -> None:
        self._delegations.pop(id(client), None)
        # Failure and queue pruning precede detach broadcasts or socket shutdown.
        client.failure = reason
        while not client.queue.empty():
            client.queue.get_nowait()
        if self.hub.clients.get(client.key) is client:
            self.hub.detach(client.key)
        self._stop_sidecar_worker(client)
        if self.file_receiver is not None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self.file_receiver.cancel_owner(client.key)
            else:
                cleanup = loop.create_task(self._sidecar_io(self.file_receiver.cancel_owner, client.key))
                self._file_cleanup_tasks.add(cleanup)
                def finished(task):
                    self._file_cleanup_tasks.discard(task)
                    if not task.cancelled() and task.exception() is not None:
                        LOGGER.warning('file_cleanup_failed error=%s', type(task.exception()).__name__)
                cleanup.add_done_callback(finished)

    async def _sidecar_io(self, function, *args):
        return await asyncio.get_running_loop().run_in_executor(self._sidecar_executor, function, *args)

    async def close_sidecars(self):
        """Called after sockets close; leave no optional-feature worker tasks."""
        jobs = [worker[1] for worker in self._sidecar_workers.values()]
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        if self._file_cleanup_tasks:
            await asyncio.gather(*list(self._file_cleanup_tasks), return_exceptions=True)
        self._sidecar_executor.shutdown(wait=False, cancel_futures=True)

    def _stop_sidecar_worker(self, client):
        worker = self._sidecar_workers.pop(id(client), None)
        if worker is not None:
            worker[1].cancel()

    def queue_sidecar_request(self, client, op, payload, close):
        """Keep disk-backed features off the terminal connection's receive lane."""
        identity = id(client)
        current = self._sidecar_workers.get(identity)
        if current is None:
            pending = asyncio.Queue(4)
            async def run():
                try:
                    while True:
                        operation, data = await pending.get()
                        try:
                            frames, code = await self._op(client, operation, data)
                        except ProtocolError:
                            continue
                        if code:
                            await close(code)
                            return
                        if self.hub.clients.get(client.key) is not client:
                            return
                        for frame in frames:
                            self.hub._send(client, frame)
                finally:
                    worker = self._sidecar_workers.get(identity)
                    if worker is not None and worker[1] is asyncio.current_task():
                        self._sidecar_workers.pop(identity, None)
            job = asyncio.create_task(run())
            current = (pending, job)
            self._sidecar_workers[identity] = current
        try:
            current[0].put_nowait((op, payload))
        except asyncio.QueueFull:
            try:
                request = unpack_json(payload).get('request', '')
                if not isinstance(request, str) or len(request) > 64:
                    return
                self.hub._send(client, pack_json(op, {'request': request, 'ok': False,
                    'error_code': 'busy', 'error': '后台文件与助理任务繁忙，请稍后重试。'}))
            except ProtocolError:
                pass

    def _file_session_allowed(self, key, sid):
        current = self.hub.clients.get(key)
        session = self.hub.sessions.get(sid)
        return (current is not None and self.client_authorized(current)
                and not current.delegated and current.token == self.token
                and session is not None and self.hub._channel_ok(current, session))

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
        first = None
        if '?' in ws.request.path:
            LOGGER.warning('client_rejected reason=query_authentication')
            await ws.close(code=4001, reason='Use first HELLO authentication')
            return
        authorization = ws.request.headers.get('Authorization', '')
        offered = authorization[7:] if authorization.startswith('Bearer ') else ''
        if not self.allowed(offered):
            try:
                first = await asyncio.wait_for(ws.recv(), FIRST_FRAME_S)
                if len(first) > 1024 * 1024:
                    raise ValueError("Oversized authentication frame")
                if isinstance(first, str):
                    if not self._local_peer(ws):
                        raise ValueError('Local account operation required')
                    body = json.loads(first)
                    if not isinstance(body, dict) or body.get('op') not in {'register', 'login', 'rotate'}:
                        raise ValueError('Invalid account operation')
                    reply = self.account_reply(body)
                    if reply.get('token'):
                        self.publish_tokens()
                    await ws.send(json.dumps(reply, ensure_ascii=False))
                    await ws.close()
                    return
                op, payload = unpack(first)
                if op != OP_HELLO:
                    raise ValueError('HELLO required')
                offered = str(unpack_json(payload).get('token') or '')
                if not offered and self._local_owner_peer(ws):
                    offered = self.token
                if not self.allowed(offered):
                    raise ValueError('Invalid credentials')
            except (asyncio.TimeoutError, ValueError, TypeError, ProtocolError):
                LOGGER.warning('client_rejected reason=authentication')
                await ws.close(code=4001)
                return
        queue: asyncio.Queue = asyncio.Queue(QUEUE_FRAMES)
        client = self.hub.attach(queue, token=offered)
        LOGGER.info('client_connected clients=%d', len(self.hub.clients))
        sender = asyncio.create_task(self._send(ws, queue, client))
        self._direct_sockets[client.key] = (ws, sender)
        try:
            if first is not None:
                op, payload = unpack(first)
                frames, close_code = await self._op(client, op, payload)
                if close_code:
                    await ws.close(code=close_code)
                    return
                for frame in frames:
                    self.hub._send(client, frame)
            async for message in ws:
                if isinstance(message, str):
                    continue
                try:
                    op, payload = unpack(message)
                except ProtocolError:
                    continue
                if op in (OP_FILE_TRANSFER, OP_ASSISTANT_CONTEXT):
                    self.queue_sidecar_request(client, op, payload, lambda code: ws.close(code=code))
                    continue
                try:
                    frames, close_code = await self._op(client, op, payload)
                except ProtocolError:
                    continue
                if close_code:
                    await ws.close(code=close_code)
                    return
                for frame in frames:
                    self.hub._send(client, frame)
        finally:
            self._direct_sockets.pop(client.key, None)
            self.hub.detach(client.key)
            self._stop_sidecar_worker(client)
            if self.file_receiver is not None:
                await self._sidecar_io(self.file_receiver.cancel_owner, client.key)
            LOGGER.info('client_disconnected clients=%d', len(self.hub.clients))
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    async def _send(self, ws, queue: asyncio.Queue, client=None) -> None:
        try:
            while True:
                frame = await queue.get()
                if frame is None:
                    await ws.close(code=1013, reason=(client.failure if client else "Slow client")[:120])
                    return
                await ws.send(frame)
                supplement = await self.snapshot_supplement(client, frame)
                if supplement is not None:
                    await ws.send(supplement)
        except asyncio.CancelledError:
            return

    async def snapshot_supplement(self, client, frame):
        """Read a session's genuine footer after replay, without losing history."""
        if client is None or not isinstance(frame, bytes) or not self.client_authorized(client):
            return None
        try:
            opcode, payload = unpack(frame)
            context = None
            if opcode == OP_HISTORY_CONTEXT:
                sid, context, _ = unpack_history_context(payload)
                if (context['kind'] not in ('snapshot', 'resume') or not client.history_context
                        or not client.multiplex or (context['v'] == 2 and not client.history_resume)):
                    return None
                session = self.hub.sessions.get(sid)
            elif opcode != OP_SNAPSHOT:
                return None
            elif client.multiplex or client.key.startswith('relay-'):
                raw_sid, separator, _ = payload.partition(b'\0')
                if not separator:
                    return None
                sid = raw_sid.decode('utf-8')
                session = self.hub.sessions.get(sid)
            else:
                session = self.hub.view(client)
                sid = session.id if session is not None else ''
            if session is None:
                return None
            pty, dimensions = session.pty, (session.cols, session.rows)
            if context is not None and dimensions != (context['cols'], context['rows']):
                return None
            mode = (client.multiplex, client.key.startswith('relay-'))

            def current():
                return (self.client_authorized(client) and self.hub.sessions.get(sid) is session
                        and session.pty is pty and (session.cols, session.rows) == dimensions
                        and (context is None or session.pump.history_context()['epoch'] == context['epoch'])
                        and (context is None or context['v'] == 1 or session.pump.stream_id == context['stream_id'])
                        and (client.multiplex, client.key.startswith('relay-')) == mode
                        and self.hub._channel_ok(client, session)
                        and (sid in client.subscriptions if client.multiplex else
                             mode[1] or self.hub.view(client) is session))

            reader = getattr(pty, 'footer_patch', None)
            if not callable(reader) or not current():
                return None
            supplement = await asyncio.to_thread(reader, *dimensions)
            if not current() or not isinstance(supplement, bytes) or not supplement or len(supplement) > 65536:
                return None
            return self.hub._feed_frame(client, OP_OUTPUT, sid, supplement)
        except Exception:
            # Recovery is optional. No terminal cells or credentials enter logs.
            return None

    async def _op(self, client, op: int, payload: bytes):
        if not self.client_authorized(client):
            self._revoke_clients()
            return [], 4001
        if op == OP_ASSISTANT_CONTEXT:
            body = unpack_json(payload)
            request, sid = body.get('request', ''), body.get('session', '')
            if not isinstance(request, str) or len(request) > 64 or not isinstance(sid, str) or len(sid) > 256:
                raise ProtocolError('Invalid assistant context request')
            session = self.hub.sessions.get(sid)
            def permitted():
                return (self.client_authorized(client) and not client.delegated and client.token == self.token
                        and session is not None and self.hub.sessions.get(sid) is session
                        and self.hub._channel_ok(client, session))
            if not permitted():
                return [pack_json(OP_ASSISTANT_CONTEXT, {'request': request, 'ok': False, 'error': '目标项目上下文不可用或未获授权。'})], None
            from .assistant_context import read_project_context
            context = await self._sidecar_io(read_project_context, session.cwd, os.environ.get('SUPERMONO_MYLIB', 'D:/aicoding/mylib'))
            if not self.client_authorized(client):
                return [], 4001
            if not permitted():
                return [pack_json(OP_ASSISTANT_CONTEXT, {'request': request, 'ok': False, 'error': '目标项目上下文已失效。'})], None
            return [pack_json(OP_ASSISTANT_CONTEXT, {'request': request, 'ok': True, **context})], None
        if op == OP_FILE_TRANSFER:
            body = unpack_json(payload)
            request = body.get('request', '')
            if not isinstance(request, str) or len(request) > 64:
                raise ProtocolError('Invalid file transfer request')
            if client.delegated or client.token != self.token:
                return [pack_json(OP_FILE_TRANSFER, {'request': request, 'ok': False, 'error_code': 'owner_required', 'error': '文件接收仅对后台所有者开放。'})], None
            async with self._file_receiver_lock:
                if self.file_receiver is None:
                    from .file_transfer_rpc import FileTransferRPC
                    try:
                        self.file_receiver = await self._sidecar_io(FileTransferRPC, self.receive_dir, self._file_session_allowed)
                    except (OSError, ValueError):
                        return [pack_json(OP_FILE_TRANSFER, {'request': request, 'ok': False, 'error_code': 'unavailable', 'error': '文件接收目录暂不可用。'})], None
            # Bounded chunks use their own protocol; never enter a PTY queue.
            result = await self._sidecar_io(self.file_receiver.handle, client.key, body)
            if not self.client_authorized(client):
                return [], 4001
            if result.get('ok') and not self._file_session_allowed(client.key, body['session']):
                await self._sidecar_io(self.file_receiver.handle, client.key,
                    {'request': request, 'action': 'cancel', 'session': body['session'], 'id': result.get('id')})
                result = {'request': request, 'ok': False, 'error_code': 'session_unavailable', 'error': '目标窗口已关闭或不再授权。'}
            return [pack_json(OP_FILE_TRANSFER, result)], None
        if op == OP_DAEMON_LOGS:
            body = unpack_json(payload)
            request = body.get('request', '')
            lines = body.get('lines', 80)
            if not isinstance(request, str) or len(request) > 64 or type(lines) is not int or lines < 0:
                raise ProtocolError('Invalid daemon log request')
            reply = {'request': request, 'pid': os.getpid(), 'uptime_seconds': int(time.monotonic()-self.started_at)}
            if client.delegated or client.token != self.token:
                return [pack_json(OP_DAEMON_LOGS, {**reply, 'error_code': 'owner_required', 'error': '运行日志仅对后台所有者开放。'})], None
            from .daemon_logs import read_log_tail
            try:
                text = await asyncio.to_thread(read_log_tail, self.log_state_dir, min(lines, 200))
            except OSError:
                text = None
            # Reading a file may outlive an authorization or socket generation.
            if not self.client_authorized(client) or client.delegated or client.token != self.token:
                return [], 4001
            result = {'text': text} if text is not None else {'error_code': 'unavailable', 'error': '运行日志暂不可用，请检查日志目录权限。'}
            return [pack_json(OP_DAEMON_LOGS, {**reply, **result})], None
        if op == OP_HELLO:
            body = unpack_json(payload) if payload else {}
            offered = str(body.get("token") or "")
            if client.delegated and any(name in body for name in ("token", "session", "password", "account")):
                return [], 4001
            if offered and offered != client.token:
                if not client.token and self.allowed(offered):
                    client.token = offered
                else:
                    return [], 4001
            if not self.client_authorized(client):
                return [], 4001
            client.multiplex = body.get("multiplex") is True
            client.reliable = body.get("reliable") is True
            client.history_context = client.multiplex and type(body.get('history_context')) is int and body['history_context'] == 1
            client.history_resume = client.history_context and type(body.get('history_resume')) is int and body['history_resume'] == 1
            client.history_chunks = client.history_resume and type(body.get('history_chunks')) is int and body['history_chunks'] == 1
            self.hub.ensure_owner(client.token)
            if body.get("channel") is not None:
                self.hub.subscribe(client, str(body.get("channel") or ""))
            chosen = body.get("window")
            if chosen in self.hub.windows and self.hub.owns(client.token, str(chosen)):
                self.hub.select(client, chosen)
            return [self._state(client)], None
        if op == OP_TASK_SUBSCRIBE:
            body = unpack_json(payload) if payload else {}
            after = body.get("after", "")
            if not isinstance(after, str) or len(after) > 128:
                raise ProtocolError("Invalid task event cursor")
            return self.hub.subscribe_task_events(client, after, body.get("replay") is not False), None
        if op == OP_SUBSCRIBE:
            body = unpack_json(payload)
            sessions = body.get("sessions")
            if isinstance(sessions, list) and len(sessions) <= 256:
                history_after = None
                if 'history_after' in body and client.history_resume:
                    history_after = body['history_after']
                    if (not isinstance(history_after, dict) or len(history_after) > 256
                            or any(not isinstance(sid, str) or not sid
                                   or any(ord(char) < 32 or ord(char) == 127 or 0xd800 <= ord(char) <= 0xdfff for char in sid)
                                   or len(sid.encode('utf-8')) > 256
                                   or not isinstance(cursor, dict) or set(cursor) != {'position', 'epoch', 'stream_id'}
                                   or not valid_stream_id(cursor['stream_id'])
                                   or any(type(cursor[key]) is not int or not 0 <= cursor[key] <= 9007199254740991
                                          for key in ('position', 'epoch'))
                                   for sid, cursor in history_after.items())):
                        raise ProtocolError('Invalid history cursor')
                existing = set(client.subscriptions) if body.get("refresh") is True else None
                if body.get('replay') is False and existing is None:
                    self.hub.set_subscriptions(client, sessions, replay=False)
                else:
                    self.hub.set_subscriptions(client, sessions, history_after=history_after)
                if existing is not None:
                    # Newly added sessions already received their initial snapshot above.
                    self.hub.refresh_subscriptions(client, existing)
            return [self._state(client)], None
        if op == OP_INPUT_SESSION:
            sid, separator, data = payload.partition(b"\0")
            if separator and len(sid) == 8:
                try:
                    session_id = sid.decode("ascii")
                except UnicodeDecodeError:
                    return [], None
                await asyncio.to_thread(self.hub.write_session, client, session_id, data)
            return [], None
        if op == OP_WINDOW:
            body = unpack_json(payload) if payload else {}
            window = self.hub.open_window(body.get("title"), str(body.get("group") or ""), client.token)
            LOGGER.info('window_opened windows=%d', len(self.hub.windows))
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
                LOGGER.warning('terminal_open_failed error=%s', type(exc).__name__)
                return [self._state(client, error=str(exc))], None
            self.hub.follow(client, session.id)
            LOGGER.info('terminal_opened windows=%d terminals=%d', len(self.hub.windows), len(self.hub.sessions))
            return [self._state(client)], None
        if op == OP_FOCUS:
            body = unpack_json(payload)
            self.hub.focus(client, str(body.get("session") or ""))
            return [self._state(client)], None
        if op == OP_INPUT:
            if not client.multiplex:
                await asyncio.to_thread(self.hub.write, client, payload)
            return [], None
        if op == OP_RESIZE:
            body = unpack_json(payload)
            try:
                cols = int(body.get("cols", 100))
                rows = int(body.get("rows", 28))
            except (TypeError, ValueError):
                return [], None
            if client.multiplex:
                accepted = await asyncio.to_thread(
                    self.hub.resize_session, client, str(body.get("session") or ""), cols, rows,
                    body.get("take_control") is True,
                )
                ack = {"session": str(body.get("session") or ""), "request": body.get("request"), "accepted": accepted}
                return [self._state(client, resize_ack=ack)], None
            await asyncio.to_thread(self.hub.resize, client, cols, rows)
            return [], None
        if op == OP_CLOSE:
            body = unpack_json(payload) if payload else {}
            before = len(self.hub.windows), len(self.hub.sessions)
            if body.get("window"):
                if self.hub.owns(client.token, str(body["window"])):
                    self.hub.close_window(str(body["window"]))
            elif body.get("session"):
                session = self.hub.sessions.get(str(body["session"]))
                if session is not None and self.hub.owns(client.token, session.window_id):
                    self.hub.close_session(str(body["session"]))
            if before != (len(self.hub.windows), len(self.hub.sessions)):
                LOGGER.info('terminal_closed windows=%d terminals=%d', len(self.hub.windows), len(self.hub.sessions))
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
                if client.token == self.token and not client.delegated:
                    self.hub.save_presets(body.get("presets"))
            elif action == "title":
                if self.hub.owns(client.token, str(body.get("window") or "")):
                    self.hub.rename_window(str(body.get("window") or ""), str(body.get("title") or ""))
            elif action == "autotitle":
                if self.hub.owns(client.token, str(body.get("window") or "")):
                    self.hub.autotitle(str(body.get("window") or ""))
            elif action == "tab-theme":
                if self.hub.owns(client.token, str(body.get("window") or "")):
                    self.hub.set_tab_theme(str(body.get("window") or ""), str(body.get("theme") or ""))
            elif action == "relays":
                if client.token == self.token and not client.delegated:
                    self.set_relays(body.get("relays"))
            elif action == "identity":
                if client.token == self.token and not client.delegated:
                    ident = save_identity(default_machine(), str(body.get("project") or ""))
                    self.hub.set_identity(ident["machine"], ident["project"])
                    await self.push_identity()
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
        self._revoke_clients()
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

    def _local_owner_peer(self, ws) -> bool:
        """Only an IPv4 loopback request may omit the direct owner credential."""
        try:
            return (ws.remote_address[0] == "127.0.0.1"
                    and urlsplit("http://" + ws.request.headers.get("Host", "")).hostname == "127.0.0.1"
                    and request_allowed(ws, ws.request))
        except (AttributeError, IndexError, TypeError, ValueError):
            return False

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

    def _revoke_clients(self) -> None:
        closing = []
        for client in list(self.hub.clients.values()):
            if self.client_authorized(client):
                continue
            # Mark failed before detach broadcasts, and prune all queued terminal bytes.
            self.hub.fail_client(client, 'Credentials revoked')
            socket = self._direct_sockets.pop(client.key, None)
            if socket is not None:
                ws, sender = socket
                sender.cancel()
                closing.append(ws)
        # Routing and pending sends for every revoked client are removed before shutdown.
        for ws in closing:
            asyncio.get_running_loop().create_task(ws.close(code=4001, reason='Credentials revoked'))

    def publish_tokens(self) -> None:
        self._revoke_clients()
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
            "pid": os.getpid(),
            "uptime_seconds": int(time.monotonic()-self.started_at),
            "accounts": len(self.accounts.users),
            "windows": len(self.hub.windows),
            "sessions": len(self.hub.sessions),
            "relays": relays,
        }

    async def push_identity(self) -> None:
        payload = json.dumps({
            "op": "identity",
            "machine": self.hub.machine,
            "project": self.hub.project,
        }, ensure_ascii=False)
        for ws in list(self._relay_sockets.values()):
            try:
                await ws.send(payload)
            except Exception:
                pass

    def publish_status(self) -> None:
        for client in list(self.hub.clients.values()):
            if client.key.startswith("relay-"):
                continue
            self.hub._send(client, self._state(client))

    def _state(self, client, error: str | None = None, resize_ack: dict | None = None) -> bytes:
        visible = self.hub.summary(client.token, history_context=True) if client.history_context else self.hub.summary(client.token)
        service = self.service_status()
        service['owner_authenticated'] = client.token == self.token and not client.delegated
        if client.token == self.token and not client.delegated:
            service['relays'] = [{**status, 'code': target['code']}
                                 for status, target in zip(service['relays'], self.relay_targets)]
        service["windows"] = len(visible["windows"])
        service["sessions"] = sum(len(item["sessions"]) for item in visible["windows"])
        body = {**visible, "selected": client.window_id or "", "session": client.session_id or "", "service": service,
                "controls": self.hub.controls(client) if client.multiplex else {}}
        if error:
            body["error"] = error
        if resize_ack is not None:
            body["resize_ack"] = resize_ack
        return pack_json(OP_HELLO_OK, body)

    async def serve_http(self, connection, request):
        page = importlib.reload(web)
        if not request_allowed(connection, request, getattr(self, 'allowed_origins', ())):
            return page.text_response(connection, 403, 'Forbidden', 'text/plain')
        path = request.path.split("?", 1)[0]
        content = page.page_content(path)
        if content is not None:
            text, content_type = content
            return page.text_response(connection, 200, text, content_type)
        if path == "/health":
            payload = {'ok': True, 'role': 'daemon'}
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
        url = migrate_legacy_relay(url)
        url = url.rstrip("/")
        parts = urlsplit(url)
        if parts.scheme in {"ws", "wss"} and parts.path in {"", "/"}:
            url = urlunsplit((parts.scheme, parts.netloc, "/ws", parts.query, ""))
        if not url or not code or url in seen:
            continue
        seen.add(url)
        name = str(item.get("name") or url).strip()[:40] or url
        cleaned.append({"name": name, "url": url, "code": code, **({'owner_secret': str(item['owner_secret'])} if item.get('owner_secret') else {})})
    return cleaned


async def relay_uplink(daemon: Daemon, url: str, code: str, slot: str) -> None:
    import websockets

    target = urlsplit(url)
    if target.scheme != 'wss' and not loopback(target.hostname or ''):
        LOGGER.warning('relay_refused reason=insecure_transport')
        return
    while True:
        try:
            async with websockets.connect(url, max_size=1024 * 1024) as ws:
                try:
                    await ws.send(json.dumps({
                        "op": "host",
                        "code": code,
                        "tokens": daemon.access_tokens(),
                        "owner_secret": next((item.get("owner_secret", "") for item in daemon.relay_targets if item["url"] == url and item["code"] == code), ""),
                        "machine": daemon.hub.machine,
                        "project": daemon.hub.project,
                        "flow_control": 1,
                    }))
                    ack = json.loads(await ws.recv())
                    if ack.get("op") != "ready":
                        raise RuntimeError(ack)
                    flow_control = ack.get("flow_control") == 1
                    generation = object()
                    daemon._relay_generations[url] = generation
                    daemon._relay_sockets[url] = ws
                    daemon.relay_live.add(url)
                    LOGGER.info('relay_connected server=%s room=%s', log_endpoint(url), code)
                    daemon.publish_status()
                    uplink: asyncio.Queue = asyncio.Queue(QUEUE_FRAMES)
                    phones: dict[int, tuple[object, asyncio.Task]] = {}

                    def route_valid(phone_id, client) -> bool:
                        current = phones.get(phone_id)
                        return (current is not None and current[0] is client
                                and daemon._relay_generations.get(url) is generation
                                and daemon.client_authorized(client))

                    def remove_route(phone_id: int, reason: str = "Relay client left"):
                        current = phones.pop(phone_id, None)
                        if current is None:
                            return
                        client, job = current
                        daemon.forget_relay_client(client, reason)
                        LOGGER.info('relay_client_disconnected clients=%d', len(daemon.hub.clients))
                        if job is not asyncio.current_task():
                            job.cancel()
                        retained = []
                        while not uplink.empty():
                            pending = uplink.get_nowait()
                            if pending[1] is not client:
                                retained.append(pending)
                        for pending in retained:
                            uplink.put_nowait(pending)

                    async def disconnect(phone_id: int, reason: str, close_code: int = 4001) -> None:
                        remove_route(phone_id, reason)
                        await ws.send(json.dumps({"op": "disconnect", "id": phone_id,
                                                  "code": close_code, "reason": reason}))

                    async def outbound() -> None:
                        while True:
                            phone_id, client, frame = await uplink.get()
                            if route_valid(phone_id, client):
                                await ws.send(struct.pack("<I", phone_id) + frame)

                    async def bridge(phone_id: int, client) -> None:
                        try:
                            while True:
                                frame = await client.queue.get()
                                if frame is None:
                                    reason = client.failure or "Output overflow; reconnect"
                                    await disconnect(phone_id, reason, 1013 if "overflow" in reason.lower() else 4001)
                                    return
                                if not route_valid(phone_id, client):
                                    await disconnect(phone_id, "Relay authorization ended")
                                    return
                                await uplink.put((phone_id, client, frame))
                                supplement = await daemon.snapshot_supplement(client, frame)
                                if supplement is not None and route_valid(phone_id, client):
                                    await uplink.put((phone_id, client, supplement))
                        except asyncio.CancelledError:
                            return

                    async def monitor() -> None:
                        while True:
                            await asyncio.sleep(0.25)
                            for phone_id, (client, _job) in list(phones.items()):
                                if not route_valid(phone_id, client):
                                    await disconnect(phone_id, "Relay authorization ended")

                    task = asyncio.create_task(outbound())
                    guard = asyncio.create_task(monitor())
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
                                    if phone_id <= 0 or phone_id > 0xffffffff:
                                        continue
                                    remove_route(phone_id, "Relay route replaced")
                                    delegated = "account" in ctrl or "expires" in ctrl
                                    key = f"relay-{slot}-{phone_id}"
                                    if delegated:
                                        attached = daemon.delegated_client(url, code, generation, phone_id, ctrl, key)
                                    else:
                                        phone_token = str(ctrl.get("token") or "")
                                        attached = (daemon.hub.attach(asyncio.Queue(QUEUE_FRAMES), key=key, token=phone_token)
                                                    if daemon.allowed(phone_token) else None)
                                        if attached is not None:
                                            attached.reliable = ctrl.get("reliable") is True
                                            attached.authorize = lambda client=attached: daemon.client_authorized(client)
                                            daemon.hub.ensure_owner(phone_token)
                                    if attached is None:
                                        if delegated:
                                            await ws.send(json.dumps({"op": "joined", "id": phone_id, "ok": False}))
                                        continue
                                    phones[phone_id] = (attached, asyncio.create_task(bridge(phone_id, attached)))
                                    LOGGER.info('relay_client_connected clients=%d', len(daemon.hub.clients))
                                    if delegated:
                                        await ws.send(json.dumps({"op": "joined", "id": phone_id, "ok": True}))
                                elif ctrl.get("op") == "leave":
                                    remove_route(phone_id)
                                elif flow_control and ctrl.get("op") == "pace":
                                    route = phones.get(phone_id)
                                    paused = ctrl.get("paused")
                                    if route is not None and type(paused) is bool and route_valid(phone_id, route[0]):
                                        daemon.hub.set_remote_paused(route[0], paused, ctrl.get("resync") is True)
                                continue
                            if len(message) < 5:
                                continue
                            phone_id = struct.unpack_from("<I", message)[0]
                            route = phones.get(phone_id)
                            if route is None:
                                continue
                            client = route[0]
                            if not route_valid(phone_id, client):
                                await disconnect(phone_id, "Relay authorization ended")
                                continue
                            try:
                                op, payload = unpack(message[4:])
                            except ProtocolError:
                                continue
                            if op in (OP_FILE_TRANSFER, OP_ASSISTANT_CONTEXT):
                                async def close_sidecar(close_code, phone_id=phone_id, attached=client):
                                    if phones.get(phone_id, (None,))[0] is attached:
                                        await disconnect(phone_id, 'Invalid credentials', close_code)
                                daemon.queue_sidecar_request(client, op, payload, close_sidecar)
                                continue
                            frames, _close = await daemon._op(client, op, payload)
                            if _close:
                                await disconnect(phone_id, "Invalid credentials", _close)
                                continue
                            for frame in frames:
                                daemon.hub._send(client, frame)
                    finally:
                        if daemon._relay_generations.get(url) is generation:
                            daemon._relay_generations.pop(url, None)
                            daemon.relay_live.discard(url)
                            LOGGER.info('relay_disconnected server=%s', log_endpoint(url))
                        if daemon._relay_sockets.get(url) is ws:
                            daemon._relay_sockets.pop(url, None)
                        daemon.publish_status()
                        for phone_id in list(phones):
                            remove_route(phone_id, "Host uplink disconnected")
                        task.cancel()
                        guard.cancel()
                        await asyncio.gather(task, guard, return_exceptions=True)
                except asyncio.CancelledError:
                    # Cancellation is an intentional host shutdown. Close before the client
                    # context manager interprets the exception as an internal error (1011).
                    await ws.close(code=1000, reason='Host uplink stopped')
                    raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            close_code = getattr(getattr(exc, 'rcvd', None), 'code', None)
            LOGGER.warning('relay_retry server=%s error=%s close_code=%s errno=%s retry_seconds=2',
                           log_endpoint(url), type(exc).__name__, close_code, getattr(exc, 'errno', None))
            await asyncio.sleep(2)


async def log_daemon_status(daemon: Daemon, interval: float = 15) -> None:
    """An event-loop heartbeat uses metadata only; it never reads a PTY."""
    started = time.monotonic()
    while True:
        if daemon.file_receiver is not None:
            await daemon._sidecar_io(daemon.file_receiver.expire)
        LOGGER.info('heartbeat uptime_seconds=%d windows=%d terminals=%d clients=%d relays_online=%d relays_configured=%d',
                    int(time.monotonic()-started), len(daemon.hub.windows), len(daemon.hub.sessions),
                    len(daemon.hub.clients), len(daemon.relay_live), len(daemon.relay_targets))
        await asyncio.sleep(interval)


def main() -> None:
    global STATE
    parser = argparse.ArgumentParser(description="SuperMonoAgent host")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6769)
    parser.add_argument("--relay", default="", help="ws://relay:6770, outbound only")
    parser.add_argument("--code", default="")
    credentials = parser.add_mutually_exclusive_group()
    credentials.add_argument("--token", default=None, help="Set and save this host's owner token; default reads private token.txt then the saved token")
    credentials.add_argument("--token-file", metavar="PATH", help="Read and save the host owner token from a private file")
    parser.add_argument("--state-dir", help="Isolated state directory; ignores global token-file defaults unless --token-file is explicit")
    parser.add_argument("--receive-dir", help="Preset folder for received clipboard files; defaults to the state directory's received-files folder")
    parser.add_argument("--no-relay", action="store_true", help="Do not load saved outbound relays")
    log_window = parser.add_mutually_exclusive_group()
    log_window.add_argument('--log-window', dest='log_window', action='store_true', default=None, help='Open the independent scrolling daemon log window')
    log_window.add_argument('--no-log-window', dest='log_window', action='store_false', help='Keep file logs without opening a window')
    transport_options(parser)
    parser.add_argument('--relay-owner-secret', default='', help='Distinct enrolled relay room owner secret')
    binding = parser.add_mutually_exclusive_group()
    binding.add_argument('--bind-relay-account', metavar='ACCOUNT_ID', help='Bind one relay account to a local terminal owner, then exit')
    binding.add_argument('--unbind-relay-account', metavar='ACCOUNT_ID', help='Remove one local relay account binding, then exit')
    parser.add_argument('--relay-local-account', metavar='EMAIL', help='Explicit local account owner for --bind-relay-account; default is the local host owner')
    args = parser.parse_args()
    context = tls_context(args.tls_cert, args.tls_key)
    if not loopback(args.host) and not context and not (args.reverse_proxy_tls and args.allowed_origin and all(origin.startswith('https://') for origin in args.allowed_origin)):
        parser.error('Public serving requires TLS or --reverse-proxy-tls with --allowed-origin')
    if args.code:
        try:
            _new_room_name(args.code)
        except ValueError as exc:
            parser.error(str(exc))
    if args.state_dir:
        STATE = Path(args.state_dir).resolve()
        os.environ["SUPERMONO_STATE_DIR"] = str(STATE)
    metadata_operation = bool(args.bind_relay_account or args.unbind_relay_account)
    effective_token = args.token
    try:
        from .token_config import read_default_token
        if args.token_file is not None:
            effective_token = read_default_token(args.token_file, state_dir=STATE)
            if not effective_token:
                parser.error('Token file does not exist or contains no token')
        elif args.token is None and not args.state_dir and not metadata_operation:
            effective_token = read_default_token(None, state_dir=STATE) or None
    except ValueError as exc:
        parser.error(str(exc))
    if effective_token is not None and not metadata_operation:
        # A second host must not replace the running host's saved credential
        # before discovering that its listening port is already occupied.
        family = socket.AF_INET6 if ':' in args.host else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.bind((args.host, args.port))
        except OSError:
            parser.error('Host address/port is unavailable; the saved token was not changed')
    try:
        token = load_token(effective_token)
    except ValueError as exc:
        parser.error(str(exc))
    if args.bind_relay_account or args.unbind_relay_account:
        if not args.relay or not args.code:
            parser.error('Relay account binding requires explicit --relay URL and --code ROOM')
        owner = token
        if args.relay_local_account:
            user = Accounts(STATE / 'accounts.json')._user(args.relay_local_account)
            if user is None or not user.get('token'):
                parser.error('Unknown local account owner')
            owner = str(user['token'])
        try:
            RelayBindings(STATE / 'relay-bindings.json').bind(
                args.relay, args.code, args.bind_relay_account or args.unbind_relay_account,
                owner if args.bind_relay_account else None,
            )
        except ValueError as exc:
            parser.error(str(exc))
        print('Local relay account binding saved; running hosts reload within one second.')
        return
    targets = [] if args.no_relay else load_relays()
    existing = None
    if args.relay:
        requested = clean_relays([{"url": args.relay, "code": "lookup"}])
        if requested:
            existing = next((item for item in clean_relays(targets) if item["url"] == requested[0]["url"]), None)
    try:
        code = load_code(args.code) if args.code else existing["code"] if existing else load_code()
    except ValueError as exc:
        parser.error(str(exc))
    daemon = Daemon(token)
    if args.receive_dir:
        daemon.receive_dir = Path(args.receive_dir).absolute()
    daemon.allowed_origins = args.allowed_origin
    if args.relay:
        selected = {**(existing or {}), "name": (existing or {}).get("name") or args.relay, "url": args.relay, "code": code}
        if args.relay_owner_secret:
            selected["owner_secret"] = args.relay_owner_secret
        targets = [selected, *targets]
    daemon.relay_targets = clean_relays(targets)
    if args.relay:
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / 'relays.json').write_text(json.dumps(daemon.relay_targets), encoding='utf-8')

    async def run() -> None:
        daemon.hub.attach_loop(asyncio.get_running_loop())
        daemon.sync_relays()
        heartbeat = None
        try:
            async with serve(
                daemon.handle_socket, args.host, args.port, process_request=daemon.serve_http,
                ssl=context, max_size=1024 * 1024, ping_interval=25, ping_timeout=25,
            ):
                LOGGER.info('daemon_listening address=%s port=%d room=%s', args.host, args.port, code)
                for item in daemon.relay_targets:
                    LOGGER.info('relay_configured server=%s room=%s', log_endpoint(item['url']), item['code'])
                # The main workspace owns the consolidated monitor. Never pop
                # a console for each host; the console remains an explicit CLI tool.
                show_logs = args.log_window is True
                if show_logs:
                    from .daemon_logs import open_log_window
                    try:
                        if not open_log_window(STATE):
                            LOGGER.warning('log_window_unavailable reason=launch_failed')
                    except OSError as error:
                        LOGGER.warning('log_window_unavailable error=%s', type(error).__name__)
                heartbeat = asyncio.create_task(log_daemon_status(daemon))
                await asyncio.Future()
        finally:
            jobs = [task for task, _code in daemon._uplinks.values()]
            if heartbeat is not None: jobs.append(heartbeat)
            for task in jobs: task.cancel()
            if jobs: await asyncio.gather(*jobs, return_exceptions=True)
            await daemon.close_sidecars()

    from .daemon_logs import configure_logging, shutdown_logging
    logger = configure_logging(STATE, secrets=[token, *[item.get('owner_secret', '') for item in daemon.relay_targets]])
    LOGGER.info('daemon_start pid=%d machine=%s address=%s port=%d', os.getpid(), default_machine(), args.host, args.port)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        LOGGER.info('daemon_interrupted')
    except Exception as error:
        LOGGER.error('daemon_failed error=%s errno=%s winerror=%s', type(error).__name__, getattr(error, 'errno', None), getattr(error, 'winerror', None))
        raise
    finally:
        LOGGER.info('daemon_stopped')
        shutdown_logging(logger)


if __name__ == "__main__":
    main()
