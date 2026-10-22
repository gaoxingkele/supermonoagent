"""Pair an outbound host with outbound phones and copy frames. Do not decode them."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import struct
import hashlib
import hmac
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit
from .security import request_allowed, tls_context, transport_options, loopback
from dataclasses import dataclass, field

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from . import web
from .relay_identity import RelayIdentity, normalize_public_url

QUEUE_FRAMES = 32
FIRST_FRAME_S = 2.0


@dataclass
class Room:
    code: str = ""
    host: object | None = None
    phones: dict = field(default_factory=dict)
    queues: dict = field(default_factory=dict)
    reliable: set[int] = field(default_factory=set)
    tokens: set = field(default_factory=set)
    pending: dict = field(default_factory=dict)
    phone_tokens: dict = field(default_factory=dict)
    senders: dict = field(default_factory=dict)
    phone_sessions: dict = field(default_factory=dict)
    joins: dict = field(default_factory=dict)
    next_id: int = 1
    machine: str = ""
    project: str = "默认"


def group_hosts(hosts: list[dict]) -> list[dict]:
    """Fold host announcements into 项目 → 房间. One project can hold several computers."""
    order: list[str] = []
    buckets: dict[str, list] = {}
    for item in hosts:
        name = str(item.get("project") or "默认")[:40] or "默认"
        if name not in buckets:
            buckets[name] = []
            order.append(name)
        buckets[name].append({
            "code": str(item.get("code") or ""),
            "machine": str(item.get("machine") or item.get("code") or "")[:40],
        })
    return [{"project": name, "rooms": buckets[name]} for name in order]


class Relay:
    def __init__(self, enrollment_file=None, *, development=False, allowed_origins=(), identity_file=None, public_url="", reverse_proxy_tls=False, trusted_proxies=("127.0.0.1", "::1")) -> None:
        self.rooms: dict[str, Room] = {}
        self.development = development
        self.allowed_origins = tuple(allowed_origins)
        self.enrollment_file = Path(enrollment_file) if enrollment_file else None
        self.enrolled = json.loads(self.enrollment_file.read_text()) if self.enrollment_file and self.enrollment_file.exists() else {}
        self.watchers = {}
        self.revision = 0
        self.identity = RelayIdentity(identity_file) if identity_file else None
        self.public_url = normalize_public_url(public_url) if public_url else ''
        self.reverse_proxy_tls = reverse_proxy_tls
        self.trusted_proxies = set(trusted_proxies)
        self.account_watchers = {}
        self._identity_monitor = None
        self._login_slots = asyncio.Semaphore(4)
        self._login_attempts = {}
        self._last_identity_revision = -1

    def _secure_accounts(self, ws):
        transport = getattr(ws, 'transport', None)
        if transport and transport.get_extra_info('ssl_object'):
            return True
        peer = ws.remote_address[0]
        if loopback(peer) and (self.development or self.public_url and loopback(urlsplit(self.public_url).hostname)):
            return True
        return self.reverse_proxy_tls and peer in self.trusted_proxies and ws.request.headers.get('X-Forwarded-Proto') == 'https'

    def _start_identity_monitor(self):
        if self.identity and (self._identity_monitor is None or self._identity_monitor.done()):
            self._identity_monitor = asyncio.create_task(self.monitor_identity())

    async def monitor_identity(self):
        while True:
            await asyncio.sleep(.25)
            self._refresh_identity()

    def _account_snapshot(self, session):
        account = self.identity.authenticate(session) if self.identity else None
        hosts = []
        if account:
            for code in self.identity.rooms(account['id']):
                room = self.rooms.get(code)
                item = {'endpoint': self.public_url, 'code': code, 'online': bool(room and room.host is not None)}
                if item['online']:
                    item.update(machine=room.machine, project=room.project)
                hosts.append(item)
        return json.dumps({'op':'directory','revision':self.revision,
                           'capabilities':{'known_rooms':True,'directory_push':True,'host_enrollment':True,'protocol_version':2,'relay_accounts':True},'hosts':hosts})

    def _delegation_valid(self, room, phone_id):
        session = room.phone_sessions.get(phone_id)
        account = self.identity.authenticate(session) if self.identity and session else None
        return bool(account and room.code in self.identity.rooms(account['id']))

    def _drop_phone(self, room, phone_id, code=4001, reason='Authorization revoked'):
        phone = room.phones.pop(phone_id, None)
        queue = room.queues.pop(phone_id, None)
        if queue is not None:
            while not queue.empty():
                queue.get_nowait()
        sender = room.senders.pop(phone_id, None)
        if sender:
            sender.cancel()
        pending = room.joins.get(phone_id)
        if pending and not pending.done():
            pending.set_result(False)
        if phone:
            # A stalled host must never delay the subscriber's authentication closure.
            asyncio.create_task(phone.close(code=code, reason=reason[:120]))
            asyncio.create_task(self._notify_leave(room, phone_id))

    async def _notify_leave(self, room, phone_id):
        host = room.host
        if host is None:
            return
        try:
            await asyncio.wait_for(host.send(json.dumps({'op':'leave','id':phone_id})), .25)
        except ConnectionClosed:
            pass
        except asyncio.TimeoutError:
            # A cancelled congested send is no longer a healthy ordered control channel.
            asyncio.create_task(host.close(code=1013, reason='Host control channel backpressure'))

    def _refresh_identity(self):
        if not self.identity:
            return
        revision = self.identity.revision
        changed = revision != self._last_identity_revision
        self._last_identity_revision = revision
        for room in list(self.rooms.values()):
            for phone_id in list(room.phone_sessions):
                if not self._delegation_valid(room, phone_id):
                    self._drop_phone(room, phone_id)
        for ws, (session, queue, sender) in list(self.account_watchers.items()):
            if not self.identity.authenticate(session):
                while not queue.empty():
                    queue.get_nowait()
                sender.cancel()
                self.account_watchers.pop(ws, None)
                asyncio.create_task(ws.close(code=4001, reason='Login session ended'))
        if changed:
            self._publish()

    async def _relay_account(self, ws, hello):
        if not self.identity or not self.public_url or not self._secure_accounts(ws):
            await ws.close(code=4001, reason='Relay accounts require a configured secure endpoint')
            return
        if hello['op'] == 'relay_logout':
            self.identity.logout(hello.get('session'))
            self._refresh_identity()
            await ws.send(json.dumps({'op':'relay_auth','logged_out':True}))
            await ws.close()
            return
        peer = ws.remote_address[0]
        now = time.monotonic()
        attempts = [stamp for stamp in self._login_attempts.get(peer, []) if now-stamp < 60]
        if len(attempts) >= 10:
            await ws.close(code=4001, reason='Login temporarily unavailable')
            return
        # Bounded bookkeeping even when many addresses probe authentication.
        if len(self._login_attempts) >= 1024:
            self._login_attempts = {address: stamps for address, stamps in self._login_attempts.items() if stamps and now-stamps[-1] < 60}
        if len(self._login_attempts) >= 1024 and peer not in self._login_attempts:
            await ws.close(code=4001)
            return
        self._login_attempts[peer] = attempts + [now]
        try:
            await asyncio.wait_for(self._login_slots.acquire(), FIRST_FRAME_S)
            job = asyncio.create_task(asyncio.to_thread(self.identity.login, hello.get('email'), hello.get('password')))
            def finished(task):
                self._login_slots.release()
                if not task.cancelled():
                    task.exception()  # Retrieve even when the client timed out or disconnected.
            job.add_done_callback(finished)
            result = await asyncio.wait_for(asyncio.shield(job), FIRST_FRAME_S)
            await ws.send(json.dumps({'op':'relay_auth', **result}))
        except (ValueError, asyncio.TimeoutError):
            await ws.send(json.dumps({'op':'relay_auth','error':'Invalid credentials'}))
            await ws.close(code=4001)
            return
        await ws.close()

    def enroll(self, code, secret=None):
        if not isinstance(code, str) or not 1 <= len(code) <= 128:
            raise ValueError('Room code must contain 1 to 128 characters')
        if not self.enrollment_file:
            raise ValueError('Enrollment requires a persisted verifier file')
        if code in self.enrolled:
            raise ValueError('Room is already enrolled')
        secret = secret or secrets.token_urlsafe(32)
        self.enrolled[code] = hashlib.sha256(secret.encode()).hexdigest()
        self.enrollment_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.enrollment_file.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.enrolled), encoding='utf-8')
        temporary.replace(self.enrollment_file)
        return secret

    def _owner_allowed(self, ws, code, secret):
        verifier = self.enrolled.get(code)
        if verifier:
            return bool(secret) and hmac.compare_digest(verifier, hashlib.sha256(secret.encode()).hexdigest())
        return self.development and loopback(ws.remote_address[0])

    def _snapshot(self, rooms):
        hosts = []
        for code, token in rooms:
            room = self.rooms.get(code)
            item = {'code': code, 'online': False}
            if room and room.host is not None and token and token in room.tokens:
                item.update(online=True, machine=room.machine, project=room.project)
            hosts.append(item)
        return json.dumps({'op': 'directory', 'revision': self.revision,
                           'capabilities': {'known_rooms': True, 'directory_push': True, 'host_enrollment': True, 'protocol_version': 2, 'relay_accounts': bool(self.identity and self.public_url)}, 'hosts': hosts})

    def _publish(self):
        self.revision += 1
        for rooms, queue in self.watchers.values():
            # Replace pending snapshots, especially identities invalidated by rotation.
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(self._snapshot(rooms))
        for session, queue, _sender in self.account_watchers.values():
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(self._account_snapshot(session))

    @staticmethod
    def _watch_rooms(hello):
        rooms = hello.get('rooms')
        if not isinstance(rooms, list) or len(rooms) > 64:
            raise ValueError('Too many rooms')
        result = []
        for item in rooms:
            if not isinstance(item, dict):
                raise ValueError('Invalid room')
            code, token = item.get('code'), item.get('token')
            if not isinstance(code, str) or not 1 <= len(code) <= 128 or not isinstance(token, str) or len(token) > 4096:
                raise ValueError('Invalid room credentials')
            result.append((code, token))
        return result

    async def _watch(self, ws, hello):
        queue = asyncio.Queue(QUEUE_FRAMES)
        sender = asyncio.create_task(self._drain(ws, queue))
        updates = []
        try:
            while True:
                now = time.monotonic()
                updates = [stamp for stamp in updates if now - stamp < 1]
                if len(updates) >= 10 or hello.get('op') != 'watch':
                    raise ValueError('Watch rate exceeded')
                updates.append(now)
                if 'session' in hello:
                    if 'rooms' in hello or not self.identity or not self.public_url or not self._secure_accounts(ws) or not self.identity.authenticate(hello['session']):
                        await ws.close(code=4001)
                        return
                    self.watchers.pop(ws, None)
                    self.account_watchers[ws] = (hello['session'], queue, sender)
                else:
                    self.account_watchers.pop(ws, None)
                    self.watchers[ws] = (self._watch_rooms(hello), queue)
                self._publish()
                raw = await ws.recv()
                if len(raw) > 1024 * 1024:
                    raise ValueError('Oversized message')
                hello = json.loads(raw)
                if not isinstance(hello, dict):
                    raise ValueError('Invalid watch')
        except ConnectionClosed:
            pass
        except (ValueError, TypeError):
            await ws.close(code=4000)
        finally:
            self.watchers.pop(ws, None)
            self.account_watchers.pop(ws, None)
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    async def _replace_tokens(self, room, tokens):
        if not isinstance(tokens, list):
            return
        room.tokens = {item for item in tokens if isinstance(item, str) and item}
        self._publish()
        closing = []
        for phone_id, phone in list(room.phones.items()):
            if phone_id not in room.phone_sessions and room.phone_tokens.get(phone_id) not in room.tokens:
                queue = room.queues.pop(phone_id, None)
                if queue is not None:
                    while not queue.empty():
                        queue.get_nowait()
                sender = room.senders.pop(phone_id, None)
                if sender is not None:
                    sender.cancel()
                closing.append(phone.close(code=4001, reason='Credentials revoked'))
        # Every revoked route and pending sender is removed before any network await.
        if closing:
            await asyncio.gather(*closing, return_exceptions=True)

    def room(self, code: str) -> Room:
        return self.rooms.setdefault(code, Room(code=code))

    async def serve_page(self, connection, request):
        page = importlib.reload(web)
        if not request_allowed(connection, request, self.allowed_origins):
            return page.text_response(connection, 403, 'Forbidden', 'text/plain')
        path = request.path.split("?", 1)[0]
        content = page.page_content(path)
        if content is not None:
            text, content_type = content
            return page.text_response(connection, 200, text, content_type)
        if path == "/health":
            body = json.dumps({'ok': True, 'role': 'relay'})
            return page.text_response(connection, 200, body, "application/json")
        return None

    def host_listing(self) -> list[dict]:
        hosts = []
        for code, room in self.rooms.items():
            if room.host is None:
                continue
            hosts.append({
                "code": code,
                "machine": room.machine,
                "project": room.project or "默认",
            })
        return hosts

    async def handler(self, ws) -> None:
        self._start_identity_monitor()
        if '?' in ws.request.path:
            await ws.close(code=4001, reason='Credentials must be in the first frame')
            return
        try:
            raw = await asyncio.wait_for(ws.recv(), FIRST_FRAME_S)
            if len(raw) > 1024 * 1024:
                raise ValueError('Oversized hello')
            hello = json.loads(raw)
            if not isinstance(hello, dict):
                raise ValueError('Invalid hello')
        except (asyncio.TimeoutError, ValueError, TypeError, ConnectionClosed):
            await ws.close(code=4000)
            return
        if not isinstance(hello.get('op'), str):
            await ws.close(code=4000)
            return
        if hello.get('op') in {'relay_login', 'relay_logout'}:
            await self._relay_account(ws, hello)
            return
        if hello.get('op') == 'watch':
            await self._watch(ws, hello)
            return
        code = hello.get("code")
        role = hello.get("op")
        if not isinstance(code, str) or not code or len(code) > 128 or role not in {"host", "phone", "register", "login", "rotate"}:
            await ws.close(code=4000)
            return
        room = self.rooms.get(code)
        if role == "host":
            owner_secret = str(hello.get('owner_secret') or '')
            tokens = hello.get('tokens') or []
            if not isinstance(tokens, list) or owner_secret and owner_secret in tokens or not self._owner_allowed(ws, code, owner_secret):
                await ws.close(code=4001)
                return
            if room is not None and room.host is not None:
                await ws.close(code=4003)
                return
            room = self.room(code)
            if room.host is None:
                room.tokens = {item for item in tokens if isinstance(item, str) and item}
                room.machine = str(hello.get("machine") or "")[:40]
                room.project = str(hello.get("project") or "默认")[:40] or "默认"
            await self._host(ws, code, room)
        elif role in {"register", "login", "rotate"}:
            await self._account(ws, code, hello)
        else:
            if 'session' in hello:
                if 'token' in hello or not self.identity or not self.public_url or not self._secure_accounts(ws):
                    await ws.close(code=4001)
                    return
                account = self.identity.authenticate(hello['session'])
                if not account or code not in self.identity.rooms(account['id']) or room is None:
                    await ws.close(code=4001)
                    return
                await self._phone(ws, code, room, '', hello.get('reliable') is True, session=hello['session'], account=account)
                return
            token = str(hello.get("token") or "")
            if room is None or token not in room.tokens:
                await ws.close(code=4001)
                return
            await self._phone(ws, code, room, token, hello.get("reliable") is True)

    async def _host(self, ws, code: str, room: Room) -> None:
        if room.host is not None:
            await ws.close(code=4003)
            return
        room.host = ws
        self._publish()
        try:
            await ws.send(json.dumps({"op": "ready"}))
            async for message in ws:
                if isinstance(message, str):
                    try:
                        ctrl = json.loads(message)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(ctrl, dict):
                        continue
                    if ctrl.get("op") == "tokens":
                        await self._replace_tokens(room, ctrl.get("tokens") or [])
                    elif ctrl.get("op") == "identity":
                        room.machine = str(ctrl.get("machine") or room.machine)[:40]
                        room.project = str(ctrl.get("project") or room.project or "默认")[:40] or "默认"
                        self._publish()
                    elif ctrl.get('op') == 'joined':
                        pending = room.joins.get(int(ctrl.get('id') or 0))
                        if pending and not pending.done():
                            pending.set_result(ctrl.get('ok') is True)
                    elif ctrl.get("op") == "disconnect":
                        close_code = 4001 if ctrl.get('code') == 4001 else 1013
                        self._drop_phone(room, int(ctrl.get('id') or 0), close_code, str(ctrl.get('reason') or 'Host disconnected viewer'))
                    elif ctrl.get("op") == "auth" and ctrl.get("id") is not None:
                        fut = room.pending.get(int(ctrl["id"]))
                        if fut is not None and not fut.done():
                            fut.set_result(ctrl)
                    continue
                if len(message) > 1024 * 1024:
                    await ws.close(code=1009)
                    break
                if len(message) < 4:
                    continue
                phone_id = struct.unpack_from("<I", message)[0]
                self._enqueue(room, phone_id, message[4:])
        finally:
            if room.host is ws:
                room.host = None
                for phone_id in list(room.phones):
                    self._drop_phone(room, phone_id)
                await self._replace_tokens(room, [])
            if room.host is None and not room.phones and self.rooms.get(code) is room:
                self.rooms.pop(code, None)

    async def _account(self, ws, code: str, hello: dict) -> None:
        room = self.rooms.get(code)
        if room is None or room.host is None:
            await ws.send(json.dumps({"op": "auth", "error": "办公室主机还没连上"}, ensure_ascii=False))
            await ws.close(code=4004)
            return
        auth_id = room.next_id
        room.next_id += 1
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        room.pending[auth_id] = fut
        try:
            await room.host.send(json.dumps({
                "op": hello.get("op"),
                "id": auth_id,
                "email": hello.get("email") or hello.get("name") or "",
                "password": hello.get("password") or "",
            }))
            reply = await asyncio.wait_for(fut, 8)
            await ws.send(json.dumps(reply, ensure_ascii=False))
        except Exception:
            await ws.send(json.dumps({"op": "auth", "error": "注册没有完成"}, ensure_ascii=False))
        finally:
            room.pending.pop(auth_id, None)
            await ws.close()

    async def _phone(self, ws, code: str, room: Room, token: str, reliable: bool = False, *, session=None, account=None) -> None:
        if room.host is None:
            await ws.close(code=4004)
            return
        phone_id = room.next_id
        room.next_id += 1
        queue = asyncio.Queue(QUEUE_FRAMES)
        room.phones[phone_id] = ws
        room.phone_tokens[phone_id] = token
        room.queues[phone_id] = queue
        if session:
            room.phone_sessions[phone_id] = session
        if reliable:
            room.reliable.add(phone_id)
        sender = None
        try:
            if account:
                pending = asyncio.get_running_loop().create_future()
                room.joins[phone_id] = pending
                await room.host.send(json.dumps({'op':'join','id':phone_id,'account':account['id'],'expires':account['expires'],'reliable':reliable}))
                try:
                    accepted = await asyncio.wait_for(pending,FIRST_FRAME_S)
                except asyncio.TimeoutError:
                    accepted = False
                if not accepted or not self._delegation_valid(room,phone_id) or phone_id not in room.phones:
                    self._drop_phone(room,phone_id)
                    await ws.close(code=4001, reason="Host account binding unavailable")
                    return
            else:
                await room.host.send(json.dumps({'op':'join','id':phone_id,'token':token,'reliable':reliable}))
            await ws.send(json.dumps({'op':'ready'}))
            sender = asyncio.create_task(self._drain(ws,queue))
            room.senders[phone_id] = sender
            async for message in ws:
                host = room.host
                allowed = self._delegation_valid(room,phone_id) if session else token in room.tokens
                if host is None or phone_id not in room.phones or not allowed:
                    self._drop_phone(room,phone_id)
                    break
                body = message if isinstance(message,bytes) else message.encode()
                await host.send(struct.pack('<I',phone_id)+body)
        except ConnectionClosed as exc:
            # Only our already-revoked route's expected auth/overflow closure is routine.
            if phone_id in room.queues or exc.sent is None or exc.sent.code not in {4001,1013}:
                raise
        finally:
            room.phones.pop(phone_id,None)
            room.phone_tokens.pop(phone_id,None)
            room.phone_sessions.pop(phone_id,None)
            room.joins.pop(phone_id,None)
            room.senders.pop(phone_id,None)
            room.queues.pop(phone_id,None)
            room.reliable.discard(phone_id)
            if sender:
                sender.cancel()
                await asyncio.gather(sender,return_exceptions=True)
            host = room.host
            if host is not None:
                await self._notify_leave(room,phone_id)
            if room.host is None and not room.phones and self.rooms.get(code) is room:
                self.rooms.pop(code,None)

    def _enqueue(self, room: Room, phone_id: int, message: bytes) -> None:
        if phone_id in room.phone_sessions and not self._delegation_valid(room, phone_id):
            self._drop_phone(room, phone_id)
            return
        queue = room.queues.get(phone_id)
        if queue is None:
            return
        if queue.full():
            if phone_id in room.reliable:
                phone = room.phones.get(phone_id)
                if phone is not None:
                    asyncio.create_task(phone.close(code=1013, reason="Slow relay subscriber; reconnect for bounded history replay"))
                return
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            room.phones.pop(phone_id, None)

    async def _drain(self, ws, queue: asyncio.Queue) -> None:
        try:
            while True:
                await ws.send(await queue.get())
        except (asyncio.CancelledError, ConnectionClosed):
            return


def main() -> None:
    parser = argparse.ArgumentParser(description="SuperMonoAgent relay")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=6770)
    transport_options(parser)
    parser.add_argument('--enrollment-file', default=str(Path.home() / '.supermonoagent' / 'relay-owners.json'))
    parser.add_argument('--enroll', metavar='ROOM', help='Enroll a room, print its new owner secret, and exit')
    parser.add_argument('--development', action='store_true', help='Explicit loopback-only fixture host claims')
    parser.add_argument('--identity-file', default=str(Path.home() / '.supermonoagent' / 'relay-accounts.sqlite3'))
    parser.add_argument('--public-url', default='', help='Trusted public HTTPS/WSS relay address advertised to authorized accounts')
    parser.add_argument('--trusted-proxy', action='append', default=['127.0.0.1','::1'], help='Actual TLS reverse proxy peer IP; requires X-Forwarded-Proto: https')
    account_admin = parser.add_mutually_exclusive_group()
    account_admin.add_argument('--create-account', metavar='EMAIL')
    account_admin.add_argument('--reset-account-password', metavar='EMAIL_OR_ID')
    account_admin.add_argument('--grant-room', metavar='ROOM')
    account_admin.add_argument('--revoke-room', metavar='ROOM')
    parser.add_argument('--account', metavar='EMAIL_OR_ID', help='Account for --grant-room / --revoke-room')
    parser.add_argument('--password-stdin', action='store_true', help='Read account password from one stdin line; otherwise getpass')
    args = parser.parse_args()
    if args.development and not loopback(args.host):
        parser.error('--development requires a loopback bind address')
    try:
        relay = Relay(args.enrollment_file, development=args.development, allowed_origins=args.allowed_origin,
                      identity_file=args.identity_file, public_url=args.public_url, reverse_proxy_tls=args.reverse_proxy_tls,
                      trusted_proxies=args.trusted_proxy)
        if args.create_account or args.reset_account_password:
            import getpass
            import sys
            password = sys.stdin.readline().rstrip('\r\n') if args.password_stdin else getpass.getpass('Account password: ')
            if args.create_account:
                print(relay.identity.create_account(args.create_account,password))
            else:
                relay.identity.reset_password(args.reset_account_password,password)
                print('Password reset; account sessions revoked')
            return
        if args.grant_room or args.revoke_room:
            if not args.account:
                parser.error('--account is required for room grants')
            code = args.grant_room or args.revoke_room
            if args.grant_room:
                if code not in relay.enrolled:
                    parser.error('Enroll the room owner before granting account access')
                relay.identity.grant(args.account,code)
            else:
                relay.identity.revoke(args.account,code)
            print('Room authorization updated')
            return
    except ValueError as exc:
        parser.error(str(exc))
    if args.enroll:
        print(relay.enroll(args.enroll))
        return
    context = tls_context(args.tls_cert, args.tls_key)
    if not loopback(args.host) and not context and not (args.reverse_proxy_tls and args.allowed_origin and all(origin.startswith('https://') for origin in args.allowed_origin)):
        parser.error('Public serving requires TLS or --reverse-proxy-tls with --allowed-origin')

    async def run() -> None:
        async with serve(
            relay.handler,
            args.host,
            args.port,
            process_request=relay.serve_page,
            ssl=context,
            max_size=1024 * 1024,
            ping_interval=25,
            ping_timeout=25,
        ):
            print(f"relay {'wss' if context else 'ws'}://{args.host}:{args.port}")
            relay._start_identity_monitor()
            try:
                await asyncio.Future()
            finally:
                relay._identity_monitor.cancel()
                await asyncio.gather(relay._identity_monitor, return_exceptions=True)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
