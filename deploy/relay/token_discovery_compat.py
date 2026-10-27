"""Private directory backport for the pre-enrollment relay, installed by its operator.

This adapter preserves that relay's existing terminal protocol and host enrollment
limitations. It is not an alternative to deploying the modern enrolled-host relay.
Only expose its loopback listener through the configured HTTPS reverse proxy.
"""
import asyncio
import ipaddress
import json
import time

from websockets.exceptions import ConnectionClosed


def _loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _Socket:
    """Replay the inspected first frame and observe host control boundaries."""
    def __init__(self, socket, first, *, before=None, ready=None, allowed=None, outbound=None):
        self.socket, self.first = socket, first
        self.before, self.ready, self.allowed, self.outbound = before, ready, allowed, outbound

    def __getattr__(self, name):
        return getattr(self.socket, name)

    async def recv(self):
        if self.first is not None:
            first, self.first = self.first, None
            return first
        return await self.socket.recv()

    async def send(self, message):
        if self.allowed and not self.allowed():
            await self.socket.close(code=4001, reason='Credentials revoked')
            return
        if self.outbound:
            message = self.outbound(message)
        await self.socket.send(message)
        if self.ready and isinstance(message, str):
            try:
                if json.loads(message).get('op') == 'ready':
                    self.ready()
            except (ValueError, AttributeError):
                pass

    async def __aiter__(self):
        async for message in self.socket:
            if self.allowed and not self.allowed():
                await self.socket.close(code=4001, reason='Credentials revoked')
                break
            if self.before and await self.before(message):
                continue
            yield message


def install(module, *, reverse_proxy_tls=True, trusted_proxies=('127.0.0.1', '::1')):
    """Replace only this imported legacy module's Relay before calling main()."""
    legacy = module.Relay

    class DiscoveryRelay(legacy):
        def __init__(self):
            super().__init__()
            self.watchers = {}
            self.revision = 0
            self.phone_credentials = {}
            self.flow_queues = {}
            from supermonoagent.release_notifications import ReleaseNotifier
            self.release_notifier = ReleaseNotifier(lambda: self.rooms)

        @staticmethod
        def _flow_ready(room, message):
            if getattr(room, 'flow_control', False) and isinstance(message, str):
                try:
                    body = json.loads(message)
                    if body.get('op') == 'ready':
                        body['flow_control'] = 1
                        return json.dumps(body)
                except ValueError:
                    pass
            return message

        def _schedule_flow(self, room, phone_id, paused):
            if not getattr(room, 'flow_control', False) or room.host is None or phone_id not in room.phones:
                return
            if paused:
                room.paused.add(phone_id)
            else:
                room.paused.discard(phone_id)
            asyncio.create_task(self._send_flow(room, phone_id, paused))

        async def _send_flow(self, room, phone_id, paused):
            host = room.host
            if host is None or phone_id not in room.phones or (phone_id in room.paused) != paused:
                return
            resync = not paused and phone_id in room.resync
            try:
                await host.send(json.dumps({'op': 'pace', 'id': phone_id, 'paused': paused, 'resync': resync}))
            except ConnectionClosed:
                return
            if not paused and phone_id not in room.paused:
                room.resync.discard(phone_id)

        def _phone_sent(self, room, phone_id):
            queue = room.queues.get(phone_id)
            if queue is not None and phone_id in room.paused and queue.qsize() <= 8:
                self._schedule_flow(room, phone_id, False)

        def _enqueue(self, room, phone_id, message):
            if not getattr(room, 'flow_control', False):
                return super()._enqueue(room, phone_id, message)
            queue = room.queues.get(phone_id)
            if queue is None:
                return
            if phone_id not in room.paused and queue.qsize() >= 24:
                self._schedule_flow(room, phone_id, True)
            if queue.full():
                self._schedule_flow(room, phone_id, True)
                room.resync.add(phone_id)
                return
            self.flow_queues[id(queue)] = (room, phone_id)
            queue.put_nowait(message)

        async def _drain(self, ws, queue):
            try:
                while True:
                    await ws.send(await queue.get())
                    binding = self.flow_queues.get(id(queue))
                    if binding:
                        self._phone_sent(*binding)
            except (asyncio.CancelledError, ConnectionClosed):
                return

        def _secure(self, ws):
            transport = getattr(ws, 'transport', None)
            if transport and transport.get_extra_info('ssl_object'):
                return True
            peer = ws.remote_address[0]
            return (_loopback(peer) and not reverse_proxy_tls or
                    reverse_proxy_tls and peer in trusted_proxies and
                    ws.request.headers.get('X-Forwarded-Proto') == 'https')

        @staticmethod
        def _credentials(hello):
            if 'session' in hello:
                raise ValueError('Account sessions are unavailable on this relay')
            tokens, rooms = hello.get('tokens', []), hello.get('rooms', [])
            if (not isinstance(tokens, list) or len(tokens) > 32 or
                    any(not isinstance(token, str) or not token or len(token) > 4096 for token in tokens) or
                    len(set(tokens)) != len(tokens) or not isinstance(rooms, list) or len(rooms) > 64):
                raise ValueError('Invalid discovery credentials')
            for room in rooms:
                if (not isinstance(room, dict) or not isinstance(room.get('code'), str) or
                        not 1 <= len(room['code']) <= 128 or not isinstance(room.get('token'), str) or
                        len(room['token']) > 4096):
                    raise ValueError('Invalid known room')
            return rooms, tokens

        def _snapshot(self, rooms, tokens):
            hosts = []
            for known in rooms:
                room = self.rooms.get(known['code'])
                item = dict(code=known['code'], online=False)
                if room and room.host is not None and known['token'] and known['token'] in room.tokens:
                    item.update(online=True, machine=getattr(room, 'machine', ''), project=getattr(room, 'project', ''))
                hosts.append(item)
            for code, room in self.rooms.items():
                if room.host is None or getattr(room, 'service', 'terminal') != 'terminal':
                    continue
                index = next((i for i, token in enumerate(tokens) if token in room.tokens), None)
                if index is None:
                    continue
                item = next((item for item in hosts if item['code'] == code and item['online']), None)
                if item is not None:
                    item['token_index'] = index
                elif len(hosts) < 64:
                    hosts.append(dict(code=code, online=True, machine=getattr(room, 'machine', ''),
                                      project=getattr(room, 'project', ''), token_index=index))
            return json.dumps(dict(op='directory', revision=self.revision,
                                   capabilities=dict(known_rooms=True, directory_push=True, token_discovery=True,
                                                     host_enrollment=False, protocol_version=2), hosts=hosts))

        def _publish(self):
            self.revision += 1
            for rooms, tokens, queue in self.watchers.values():
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(self._snapshot(rooms, tokens))

        async def _watch(self, ws, hello):
            queue = asyncio.Queue(1)
            sender = asyncio.create_task(self._drain(ws, queue))
            updates = []
            try:
                while True:
                    now = time.monotonic()
                    updates = [stamp for stamp in updates if now - stamp < 1]
                    if len(updates) >= 10 or hello.get('op') != 'watch':
                        raise ValueError('Watch rate exceeded')
                    updates.append(now)
                    rooms, tokens = self._credentials(hello)
                    if not self._secure(ws):
                        await ws.close(code=4001)
                        return
                    self.watchers[ws] = (rooms, tokens, queue)
                    self._publish()
                    raw = await ws.recv()
                    if not isinstance(raw, str) or len(raw) > 1048576:
                        raise ValueError('Invalid watch')
                    hello = json.loads(raw)
                    if not isinstance(hello, dict):
                        raise ValueError('Invalid watch')
            except ConnectionClosed:
                pass
            except (ValueError, TypeError):
                await ws.close(code=4000)
            finally:
                self.watchers.pop(ws, None)
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)

        async def _control(self, room, message):
            if not isinstance(message, str):
                return False
            try:
                body = json.loads(message)
            except ValueError:
                return False
            if not isinstance(body, dict):
                return True
            if body.get('op') == 'identity':
                room.machine = str(body.get('machine') or room.machine)[:40]
                room.project = str(body.get('project') or room.project)[:40]
                self._publish()
                return True
            if body.get('op') != 'tokens':
                return False
            tokens = body.get('tokens')
            if not isinstance(tokens, list):
                return True
            room.tokens = {token for token in tokens if isinstance(token, str) and token}
            self._publish()
            revoked = []
            for phone_id, phone in list(room.phones.items()):
                token = self.phone_credentials.get((id(room), phone_id))
                if token not in room.tokens:
                    queue = room.queues.pop(phone_id, None)
                    if queue:
                        while not queue.empty():
                            queue.get_nowait()
                    room.phones.pop(phone_id, None)
                    revoked.append(phone.close(code=4001, reason='Credentials revoked'))
            if revoked:
                await asyncio.gather(*revoked, return_exceptions=True)
            return True

        async def handler(self, ws):
            self.release_notifier.start()
            if '?' in ws.request.path:
                await ws.close(code=4001)
                return
            try:
                raw = await asyncio.wait_for(ws.recv(), 2)
                if not isinstance(raw, str) or len(raw) > 1048576:
                    raise ValueError('Invalid hello')
                hello = json.loads(raw)
                if not isinstance(hello, dict):
                    raise ValueError('Invalid hello')
            except (ValueError, TypeError, asyncio.TimeoutError, ConnectionClosed):
                await ws.close(code=4000)
                return
            if hello.get('op') == 'watch':
                await self._watch(ws, hello)
                return
            code, role = hello.get('code'), hello.get('op')
            if not isinstance(code, str) or not 1 <= len(code) <= 128:
                await ws.close(code=4000)
                return
            room = self.rooms.get(code)
            if role == 'host':
                if room and room.host is not None:
                    await ws.close(code=4003)
                    return
                if not isinstance(hello.get('tokens', []), list):
                    await ws.close(code=4000)
                    return
                room = self.room(code)
                room.machine = str(hello.get('machine') or '')[:40]
                room.project = str(hello.get('project') or '')[:40]
                room.service = 'notifications' if hello.get('service') == 'notifications' else 'terminal'
                room.flow_control = hello.get('flow_control') == 1
                room.paused, room.resync = set(), set()
                proxy = _Socket(ws, raw, before=lambda message: self._control(room, message), ready=self._publish,
                                outbound=lambda message: self._flow_ready(room, message))
                try:
                    await super().handler(proxy)
                finally:
                    self._publish()
            else:
                if role not in {'phone', 'register', 'login', 'rotate'} or room is None:
                    await ws.close(code=4001)
                    return
                await super().handler(_Socket(ws, raw))

        async def _phone(self, ws, code, room, token):
            phone_id = room.next_id
            marker = (id(room), phone_id)
            self.phone_credentials[marker] = token
            proxy = _Socket(ws, None, allowed=lambda: room.host is not None and token in room.tokens)
            if getattr(room, 'service', 'terminal') == 'terminal' and self.release_notifier.current is not None:
                original_send = proxy.send
                async def ready_then_hint(message):
                    await original_send(message)
                    if isinstance(message, str) and json.loads(message).get('op') == 'ready':
                        await self.release_notifier.send_current(proxy)
                proxy.send = ready_then_hint
            try:
                await super()._phone(proxy, code, room, token)
            finally:
                self.phone_credentials.pop(marker, None)
                room.paused.discard(phone_id)
                room.resync.discard(phone_id)
                self.flow_queues = {key: value for key, value in self.flow_queues.items() if value != (room, phone_id)}

        async def serve_page(self, connection, request):
            if request.path.split('?', 1)[0] == '/health':
                return module.web.text_response(connection, 200, json.dumps({'ok': True, 'role': 'relay', 'update_hints': True, 'update_revision': json.loads(self.release_notifier.current)['revision'] if self.release_notifier.current else None}), 'application/json')
            return await super().serve_page(connection, request)

    module.Relay = DiscoveryRelay
    return DiscoveryRelay
