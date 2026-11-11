"""One sma-p2p agent per computer: rendezvous client, connection negotiation, peer RPC, transfers."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import socket
import time
import zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import files as fs
from . import netinfo, secure
from .secure import ACK, CHUNK_HEADER, T_ACK, T_CHUNK, T_JSON
from .transfer import CHUNK, FileChangedError, IncomingTransfer, plan, read_chunk, transfer_id

LOG = logging.getLogger('sma-p2p')
VERSION = '0.1.0'
DEFAULT_RENDEZVOUS = '117.50.48.22:8827'
WINDOW_BYTES = 16 * 1024 * 1024      # unacknowledged bytes per data link
RELAY_WINDOW_BYTES = 2 * 1024 * 1024
MAX_INFLIGHT = 64                    # many small files: count limit, not one block per round trip
RELAY_CHUNK = 1024 * 1024   # slow relayed paths: smaller blocks keep signalling responsive
STREAM_LIMIT = 16 * 1024 * 1024  # above one chunk frame, so a 4 MiB read never stalls the transport
STATUS_ACTIVE = ('scanning', 'connecting', 'sending', 'finishing', 'waiting')


class NegotiationError(ConnectionError):
    pass


class Link:
    """One authenticated TLS connection to a peer."""

    def __init__(self, reader, writer, peer: str, route: str):
        self.reader, self.writer, self.peer, self.route = reader, writer, peer, route

    async def send(self, obj: dict) -> None:
        self.writer.write(secure.pack_json(obj))
        await self.writer.drain()

    def close(self) -> None:
        try:
            self.writer.close()
        except Exception:
            pass

    @property
    def closing(self) -> bool:
        return self.writer.is_closing()


class Negotiation:
    def __init__(self, sid: str, peer: str, role: str, kind: str, tid: str | None):
        self.sid, self.peer, self.role, self.kind, self.tid = sid, peer, role, kind, tid
        loop = asyncio.get_running_loop()
        self.answer = loop.create_future()      # initiator: remote candidates
        self.result = loop.create_future()      # the chosen link
        self.tasks: set[asyncio.Task] = set()
        self.links: list[Link] = []
        self.probe_sock: socket.socket | None = None
        self.punch_port = 0

    def spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def cleanup(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        chosen = self.result.result() if self.result.done() and not self.result.cancelled() and not self.result.exception() else None
        for link in self.links:
            if link is not chosen:
                link.close()
        if self.probe_sock is not None:
            self.probe_sock.close()
            self.probe_sock = None


class Control:
    """JSON RPC in both directions over one control link."""

    def __init__(self, agent: 'Agent', link: Link):
        self.agent, self.link, self.peer = agent, link, link.peer
        self.calls: dict[int, asyncio.Future] = {}
        self.next_id = 1
        self.task = asyncio.ensure_future(self.run())

    @property
    def alive(self) -> bool:
        return not self.task.done() and not self.link.closing

    async def run(self) -> None:
        try:
            while True:
                msg = await secure.read_json(self.link.reader)
                if 'ok' in msg and 'id' in msg:
                    future = self.calls.pop(msg['id'], None)
                    if future is not None and not future.done():
                        future.set_result(msg)
                elif 'op' in msg and 'id' in msg:
                    asyncio.ensure_future(self.serve(msg))
        except (asyncio.IncompleteReadError, ConnectionError, OSError, secure.FrameError, ValueError):
            pass
        finally:
            self.link.close()
            for future in self.calls.values():
                if not future.done():
                    future.set_exception(ConnectionError('对端连接已断开'))
            self.calls.clear()
            if self.agent.controls.get(self.peer) is self:
                del self.agent.controls[self.peer]

    async def serve(self, msg: dict) -> None:
        try:
            result = await self.agent.handle_rpc(self.peer, self.link, msg)
            reply = {'id': msg['id'], 'ok': True, **result}
        except Exception as exc:  # report to the peer; the error text is ours, never a path from them
            reply = {'id': msg['id'], 'ok': False, 'error': str(exc) or type(exc).__name__}
        try:
            await self.link.send(reply)
        except (ConnectionError, OSError):
            pass

    async def call(self, op: str, timeout: float = 30, **body) -> dict:
        request_id = self.next_id
        self.next_id += 1
        future = asyncio.get_running_loop().create_future()
        self.calls[request_id] = future
        try:
            await self.link.send({'id': request_id, 'op': op, **body})
            reply = await asyncio.wait_for(future, timeout)
        finally:
            self.calls.pop(request_id, None)
        if not reply.get('ok'):
            raise RuntimeError(reply.get('error') or '对端拒绝')
        return reply


class Outgoing:
    def __init__(self, record: dict):
        self.tid = record['tid']
        self.src, self.peer, self.dest = record['src'], record['peer'], record['dest']
        self.peer_name = record.get('peer_name', '')
        self.status = record.get('status', 'waiting')
        self.bytes_total = record.get('bytes_total', 0)
        self.bytes_done = record.get('bytes_done', 0)
        self.files = record.get('files', 0)
        self.error = record.get('error', '')
        self.route = ''
        self.speed = 0.0
        self.created = record.get('created', time.time())
        self.finished = record.get('finished', 0)
        self.base = record.get('base', '')
        self.skipped_files = record.get('skipped_files', 0)
        self.chunk = record.get('chunk', 0)
        self.task: asyncio.Task | None = None
        self.manifest = None
        self.paused = self.status == 'paused'
        self.attempts = 0
        self._samples: deque = deque(maxlen=20)

    def record(self) -> dict:
        return {k: getattr(self, k) for k in ('tid', 'src', 'peer', 'peer_name', 'dest', 'status', 'bytes_total',
                                              'bytes_done', 'files', 'error', 'created', 'finished', 'base', 'skipped_files', 'chunk')}

    def view(self) -> dict:
        return {**self.record(), 'direction': 'out', 'route': self.route, 'speed': self.speed,
                'name': os.path.basename(self.src.rstrip('\\/')) or self.src}

    def sample(self) -> None:
        now = time.monotonic()
        self._samples.append((now, self.bytes_done))
        while len(self._samples) > 2 and now - self._samples[0][0] > 3:
            self._samples.popleft()
        if len(self._samples) >= 2 and self._samples[-1][0] > self._samples[0][0]:
            self.speed = (self._samples[-1][1] - self._samples[0][1]) / (self._samples[-1][0] - self._samples[0][0])


class Agent:
    def __init__(self, state_dir: str | Path, *, rendezvous: str | None = None, peer_port: int | None = None,
                 name: str | None = None, no_direct: bool = False, no_punch: bool = False,
                 force_relay: bool = False, workers: int | None = None, chunk: int = CHUNK):
        self.state = Path(state_dir)
        self.state.mkdir(parents=True, exist_ok=True)
        self.config_path = self.state / 'config.json'
        self.config = self._load_config()
        if rendezvous:
            self.config['rendezvous'] = rendezvous
        if peer_port is not None:
            self.config['peer_port'] = peer_port
        if name:
            self.config['name'] = name[:64]
        if workers:
            self.config['workers'] = workers
        self._save_config()
        self.no_direct, self.no_punch, self.force_relay = no_direct, no_punch, force_relay
        self.chunk = chunk
        self.key = base64.b64decode(self.config['network_key'])
        self.peer_id: str = self.config['peer_id']
        self._contexts()
        self.roots = self.config.get('roots') or fs.default_roots()
        self.peers: dict[str, dict] = {}
        self.controls: dict[str, Control] = {}
        self._connecting: dict[str, asyncio.Future] = {}
        self.pending: dict[str, Negotiation] = {}
        self.incoming: dict[str, dict] = {}
        self.outgoing: dict[str, Outgoing] = {}
        self.listen_port = 0
        self.public_ip = ''
        self.rv_state = 'offline'
        self.rv_error = ''
        self._rv_writer = None
        self._rv_lock = asyncio.Lock()
        self._relay_ok: dict[str, asyncio.Future] = {}
        self.executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix='sma-p2p')
        self.subscribers: set[asyncio.Queue] = set()
        self._notify_handle = None
        self._tasks: set[asyncio.Task] = set()
        self._server = None
        self._load_transfers()

    # ---------- config ----------
    def _load_config(self) -> dict:
        config = {}
        if self.config_path.exists():
            try:
                config = json.loads(self.config_path.read_text('utf-8'))
            except (OSError, ValueError):
                config = {}
        config.setdefault('peer_id', secrets.token_hex(8))
        config.setdefault('network_key', base64.b64encode(secure.new_key()).decode())
        config.setdefault('name', socket.gethostname()[:64] or 'computer')
        config.setdefault('rendezvous', DEFAULT_RENDEZVOUS)
        config.setdefault('peer_port', 6796)
        config.setdefault('workers', 4)
        config.setdefault('skip_existing', True)
        config.setdefault('roots', None)
        return config

    def _save_config(self) -> None:
        tmp = self.config_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), 'utf-8')
        os.replace(tmp, self.config_path)

    def _contexts(self) -> None:
        self.net = secure.net_id(self.key)
        self.server_ctx = secure.server_context(self.key)
        self.client_ctx = secure.client_context(self.key, self.peer_id)

    def join(self, code: str) -> None:
        key = secure.parse_join_code(code)
        self.config['network_key'] = base64.b64encode(key).decode()
        self._save_config()
        self.key = key
        self._contexts()
        for control in list(self.controls.values()):
            control.link.close()
        self.peers.clear()
        self._kick_rendezvous()

    def rename(self, name: str) -> None:
        name = (name or '').strip()[:64]
        if not name:
            raise ValueError('名称不能为空')
        self.config['name'] = name
        self._save_config()
        self._kick_rendezvous()

    def _kick_rendezvous(self) -> None:
        if self._rv_writer is not None:
            self._rv_writer.close()

    # ---------- lifecycle ----------
    async def start(self) -> None:
        port = int(self.config['peer_port'])
        for attempt in range(10):
            try:
                self._server = await asyncio.start_server(self._inbound, None, port + attempt if port else 0,
                                                          ssl=self.server_ctx, ssl_handshake_timeout=10,
                                                          limit=STREAM_LIMIT)
                break
            except OSError:
                if not port or attempt == 9:
                    raise
        self.listen_port = self._server.sockets[0].getsockname()[1]
        LOG.info('peer_listening port=%d peer=%s', self.listen_port, self.peer_id)
        self._spawn(self._rendezvous_loop())
        for out in self.outgoing.values():
            if out.status in STATUS_ACTIVE:
                self._start_outgoing(out)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for out in self.outgoing.values():
            if out.task:
                out.task.cancel()
        for control in list(self.controls.values()):
            control.link.close()
        if self._server is not None:
            self._server.close()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for record in self.incoming.values():
            record['transfer'].close()
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def io(self, function, *args):
        return await asyncio.get_running_loop().run_in_executor(self.executor, function, *args)

    # ---------- events for the local page ----------
    def notify(self) -> None:
        if self._notify_handle is None:
            self._notify_handle = asyncio.get_running_loop().call_later(0.25, self._flush)

    def _flush(self) -> None:
        self._notify_handle = None
        for out in self.outgoing.values():
            if out.status == 'sending':
                out.sample()
            else:
                out.speed = 0.0
        for record in self.incoming.values():
            self._sample_incoming(record)
        snapshot = self.snapshot()
        for queue in list(self.subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(snapshot)
        if any(o.status == 'sending' for o in self.outgoing.values()) or any(
                r.get('speed', 0) > 0 for r in self.incoming.values()):
            self.notify()  # keep speed figures fresh while bytes move; stops once they settle

    @staticmethod
    def _sample_incoming(record: dict) -> None:
        transfer = record['transfer']
        now = time.monotonic()
        samples = record.setdefault('samples', deque(maxlen=20))
        samples.append((now, transfer.bytes_done))
        while len(samples) > 2 and now - samples[0][0] > 3:
            samples.popleft()
        if record['status'] == 'receiving' and len(samples) >= 2 and samples[-1][0] > samples[0][0]:
            record['speed'] = (samples[-1][1] - samples[0][1]) / (samples[-1][0] - samples[0][0])
        else:
            record['speed'] = 0.0

    def snapshot(self) -> dict:
        incoming = []
        for tid, record in self.incoming.items():
            transfer = record['transfer']
            incoming.append({'tid': tid, 'direction': 'in', 'peer': record['peer'], 'peer_name': self.peer_name(record['peer']),
                             'name': transfer.name, 'base': str(transfer.base), 'status': record['status'],
                             'bytes_total': transfer.bytes_total, 'bytes_done': transfer.bytes_done,
                             'files': len(transfer.files), 'speed': record.get('speed', 0.0), 'route': record.get('route', ''),
                             'error': record.get('error', ''), 'created': record['created'],
                             'skipped_files': transfer.skipped_files})
        return {
            'me': {'peer': self.peer_id, 'name': self.config['name'], 'net': self.net[:8], 'version': VERSION,
                   'port': self.listen_port, 'public_ip': self.public_ip, 'roots': self.roots,
                   'rendezvous': self.config['rendezvous'], 'rv_state': self.rv_state, 'rv_error': self.rv_error,
                   'workers': self.config['workers']},
            'peers': [{**p, 'connected': p['peer'] in self.controls,
                       'route': self.controls[p['peer']].link.route if p['peer'] in self.controls else ''}
                      for p in sorted(self.peers.values(), key=lambda p: p['name'].lower())],
            'transfers': sorted([o.view() for o in self.outgoing.values()] + incoming,
                                key=lambda t: -t['created']),
        }

    def peer_name(self, peer: str) -> str:
        return self.peers.get(peer, {}).get('name') or peer

    # ---------- rendezvous ----------
    def _rv_address(self) -> tuple[str, int]:
        value = self.config['rendezvous']
        host, _, port = value.rpartition(':')
        return host.strip('[]'), int(port)

    async def _rendezvous_loop(self) -> None:
        delay = 1
        while True:
            host, port = self._rv_address()
            writer = None
            try:
                self.rv_state = 'connecting'
                self.notify()
                reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port, limit=1 << 16), 10)
                await self._rv_send({'op': 'hello', 'net': self.net, 'peer': self.peer_id,
                                     'name': self.config['name'], 'ver': VERSION}, writer)
                welcome = json.loads(await asyncio.wait_for(reader.readline(), 10))
                if welcome.get('op') != 'welcome':
                    raise ConnectionError(welcome.get('error') or '信令服务拒绝')
                self.public_ip = welcome['addr'][0]
                self._rv_writer = writer
                self.rv_state, self.rv_error, delay = 'online', '', 1
                self.notify()
                LOG.info('rendezvous_online server=%s public_ip=%s', self.config['rendezvous'], self.public_ip)
                pinger = asyncio.ensure_future(self._rv_ping(writer))
                try:
                    while True:
                        line = await asyncio.wait_for(reader.readline(), 75)
                        if not line:
                            break
                        self._rv_message(json.loads(line))
                finally:
                    pinger.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.rv_error = str(exc) or type(exc).__name__
            finally:
                if writer is not None:
                    writer.close()
                self._rv_writer = None
                self.rv_state = 'offline'
                self.peers.clear()
                self.notify()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def _rv_ping(self, writer) -> None:
        while True:
            await asyncio.sleep(30)
            await self._rv_send({'op': 'ping'}, writer)

    async def _rv_send(self, obj: dict, writer=None) -> None:
        writer = writer or self._rv_writer
        if writer is None:
            raise NegotiationError('信令服务未连接')
        async with self._rv_lock:
            writer.write(json.dumps(obj, separators=(',', ':')).encode() + b'\n')
            await writer.drain()

    def _rv_message(self, msg: dict) -> None:
        op = msg.get('op')
        if op == 'peers':
            self.peers = {p['peer']: p for p in msg.get('peers', []) if p.get('peer') != self.peer_id}
            for out in self.outgoing.values():
                if out.status == 'waiting' and out.peer in self.peers and out.task is None:
                    self._start_outgoing(out)
            self.notify()
        elif op == 'signal':
            self._signal(msg.get('from'), msg.get('data'))
        elif op == 'relay_ok':
            future = self._relay_ok.pop(msg.get('token'), None)
            if future is not None and not future.done():
                future.set_result(True)
        elif op == 'signal_failed':
            for neg in self.pending.values():
                if neg.peer == msg.get('to') and not neg.answer.done():
                    neg.answer.set_exception(NegotiationError('对方不在线'))

    async def _signal_send(self, peer: str, data: dict) -> None:
        body = {**data, 'from': self.peer_id, 'to': peer, 'ts': int(time.time())}
        await self._rv_send({'op': 'signal', 'to': peer, 'data': {**body, 'mac': secure.sign(self.key, body)}})

    def _signal(self, sender, data) -> None:
        if not isinstance(data, dict) or not isinstance(sender, str):
            return
        mac = data.get('mac')
        body = {k: v for k, v in data.items() if k != 'mac'}
        if (not secure.verify(self.key, body, mac) or body.get('from') != sender or body.get('to') != self.peer_id
                or abs(time.time() - body.get('ts', 0)) > 120):
            LOG.warning('signal_rejected from=%s', sender)
            return
        kind, sid = body.get('t'), body.get('sid')
        if not isinstance(sid, str) or len(sid) != 24:
            return
        if kind == 'connect' and sid not in self.pending:
            if body.get('kind') not in ('control', 'data'):
                return
            neg = Negotiation(sid, sender, 'resp', body['kind'], body.get('tid'))
            self.pending[sid] = neg
            self._spawn(self._respond(neg, body))
        elif kind == 'accept':
            neg = self.pending.get(sid)
            if neg is not None and neg.role == 'init' and neg.peer == sender and not neg.answer.done():
                neg.answer.set_result(body)
        elif kind == 'relay':
            neg = self.pending.get(sid)
            if neg is not None and neg.role == 'resp' and neg.peer == sender:
                neg.spawn(self._relay_dial(neg, body.get('token'), server_side=True))

    # ---------- candidates and dialing ----------
    def _candidates(self, allow_direct: bool) -> list[dict]:
        if not allow_direct or self.no_direct or self.force_relay:
            return []
        items = netinfo.candidates(self.listen_port)
        if self.public_ip and not netinfo.is_private(self.public_ip) and all(c['host'] != self.public_ip for c in items):
            items.append({'kind': 'public', 'host': self.public_ip, 'port': self.listen_port})
        return items

    async def _probe(self, neg: Negotiation) -> dict | None:
        """Open a TCP mapping through our NAT from a fixed local port, as seen by the rendezvous."""
        if self.no_punch or self.force_relay:
            return None
        loop = asyncio.get_running_loop()
        host, port = self._rv_address()
        try:
            infos = await loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('0.0.0.0', 0))
            sock.setblocking(False)
            await asyncio.wait_for(loop.sock_connect(sock, infos[0][4]), 5)
            await loop.sock_sendall(sock, b'{"op":"probe"}\n')
            line = b''
            while not line.endswith(b'\n') and len(line) < 4096:
                part = await asyncio.wait_for(loop.sock_recv(sock, 4096), 5)
                if not part:
                    raise ConnectionError('probe closed')
                line += part
            addr = json.loads(line)['addr']
        except (OSError, ValueError, KeyError, asyncio.TimeoutError, ConnectionError):
            try:
                sock.close()
            except UnboundLocalError:
                pass
            return None
        neg.probe_sock = sock
        neg.punch_port = sock.getsockname()[1]
        return {'host': addr[0], 'port': addr[1]}

    async def _hello_client(self, reader, writer, neg: Negotiation, route: str) -> Link:
        link = Link(reader, writer, neg.peer, route)
        neg.links.append(link)
        await link.send({'op': 'hello', 'peer': self.peer_id, 'sid': neg.sid})
        reply = await asyncio.wait_for(secure.read_json(reader), 10)
        if reply.get('op') != 'hello' or reply.get('peer') != neg.peer:
            link.close()
            raise NegotiationError('对端身份不符')
        return link

    async def _hello_server(self, reader, writer, expected: Negotiation | None, route: str) -> tuple[Link, Negotiation]:
        hello = await asyncio.wait_for(secure.read_json(reader), 10)
        neg = expected or self.pending.get(hello.get('sid'))
        if hello.get('op') != 'hello' or neg is None or hello.get('sid') != neg.sid or hello.get('peer') != neg.peer:
            raise NegotiationError('未知会话')
        link = Link(reader, writer, neg.peer, route)
        neg.links.append(link)
        await link.send({'op': 'hello', 'peer': self.peer_id})
        return link, neg

    async def _wrap(self, sock: socket.socket, server_side: bool):
        loop = asyncio.get_running_loop()
        if server_side:
            reader = asyncio.StreamReader(limit=STREAM_LIMIT)
            protocol = asyncio.StreamReaderProtocol(reader)
            transport, _ = await loop.connect_accepted_socket(lambda: protocol, sock=sock, ssl=self.server_ctx,
                                                              ssl_handshake_timeout=10)
            writer = asyncio.StreamWriter(transport, protocol, reader, loop)
            return reader, writer
        return await asyncio.open_connection(sock=sock, ssl=self.client_ctx, server_hostname='',
                                             ssl_handshake_timeout=10, limit=STREAM_LIMIT)

    async def _linked(self, neg: Negotiation, link: Link) -> None:
        """A link finished hello. The initiator picks the first; the responder waits to be picked."""
        if neg.role == 'init':
            if neg.result.done():
                link.close()
                return
            # Pick and tell in one synchronous step: no other link can be chosen in between.
            neg.result.set_result(link)
            link.writer.write(secure.pack_json({'op': 'chosen'}))
            return
        try:
            msg = await asyncio.wait_for(secure.read_json(link.reader), 40)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError, secure.FrameError, ValueError):
            link.close()
            return
        if msg.get('op') == 'chosen' and not neg.result.done():
            neg.result.set_result(link)
        else:
            link.close()

    async def _dial(self, neg: Negotiation, cand: dict, delay: float) -> None:
        if delay:
            await asyncio.sleep(delay)
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(
                cand['host'], int(cand['port']), ssl=self.client_ctx, server_hostname='',
                ssl_handshake_timeout=5, limit=STREAM_LIMIT), 4)
        except (OSError, asyncio.TimeoutError, ValueError, KeyError):
            return
        try:
            link = await self._hello_client(reader, writer, neg, cand['kind'])
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, NegotiationError, secure.FrameError, ValueError):
            writer.close()
            return
        await self._linked(neg, link)

    async def _punch(self, neg: Negotiation, remote: dict, seconds: float = 5, delay: float = 0) -> None:
        if not neg.punch_port or not remote:
            return
        loop = asyncio.get_running_loop()
        if delay:
            await asyncio.sleep(delay)  # let a LAN or IPv6 dial win when one exists
        deadline = loop.time() + seconds
        target = (remote['host'], int(remote['port']))
        while loop.time() < deadline and not neg.result.done():
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(('0.0.0.0', neg.punch_port))
                sock.setblocking(False)
                await asyncio.wait_for(loop.sock_connect(sock, target), 1.0)
            except (OSError, asyncio.TimeoutError):
                sock.close()
                await asyncio.sleep(0.15)
                continue
            try:
                reader, writer = await self._wrap(sock, server_side=neg.role == 'resp')
                if neg.role == 'init':
                    link = await self._hello_client(reader, writer, neg, 'punch')
                else:
                    link, _ = await self._hello_server(reader, writer, neg, 'punch')
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, NegotiationError, secure.FrameError, ValueError, ssl_error()):
                sock.close()
                return
            await self._linked(neg, link)
            return

    async def _relay_dial(self, neg: Negotiation, token, server_side: bool) -> None:
        if not isinstance(token, str):
            return
        loop = asyncio.get_running_loop()
        host, port = self._rv_address()
        sock = None
        try:
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            sock = socket.socket(infos[0][0], socket.SOCK_STREAM)
            sock.setblocking(False)
            await asyncio.wait_for(loop.sock_connect(sock, infos[0][4]), 8)
            await loop.sock_sendall(sock, json.dumps({'op': 'pipe', 'token': token}).encode() + b'\n')
            line = b''
            while not line.endswith(b'\n') and len(line) < 4096:   # byte by byte: TLS may follow at once
                part = await asyncio.wait_for(loop.sock_recv(sock, 1), 30)
                if not part:
                    raise ConnectionError('中转断开')
                line += part
            if json.loads(line).get('op') != 'paired':
                raise ConnectionError('中转拒绝')
            reader, writer = await self._wrap(sock, server_side=server_side)
            sock = None
            if server_side:
                link, _ = await self._hello_server(reader, writer, neg, 'relay')
            else:
                link = await self._hello_client(reader, writer, neg, 'relay')
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, NegotiationError, secure.FrameError, ValueError, ConnectionError):
            if sock is not None:
                sock.close()
            return
        await self._linked(neg, link)

    async def _inbound(self, reader, writer) -> None:
        peer_addr = writer.get_extra_info('peername') or ('', 0)
        host = peer_addr[0]
        route = 'ipv6' if ':' in host and not host.startswith('::ffff:') else ('lan' if netinfo.is_private(host.replace('::ffff:', '')) else 'public')
        try:
            link, neg = await self._hello_server(reader, writer, None, route)
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, NegotiationError, secure.FrameError, ValueError):
            writer.close()
            return
        await self._linked(neg, link)

    # ---------- negotiation ----------
    async def negotiate(self, peer: str, kind: str, tid: str | None = None, methods=('direct', 'punch', 'relay')) -> Link:
        if peer not in self.peers:
            raise NegotiationError('对方不在线')
        sid = secrets.token_hex(12)
        neg = Negotiation(sid, peer, 'init', kind, tid)
        self.pending[sid] = neg
        try:
            punch = await self._probe(neg) if 'punch' in methods else None
            await self._signal_send(peer, {'t': 'connect', 'sid': sid, 'kind': kind, 'tid': tid,
                                           'cands': self._candidates('direct' in methods), 'punch': punch,
                                           'methods': list(methods)})
            answer = await asyncio.wait_for(neg.answer, 15)
            remote = answer.get('cands') or []
            if 'direct' in methods and not self.force_relay and not self.no_direct:
                for cand in remote[:12]:
                    delay = {'lan': 0, 'ipv6': 0.1, 'public': 0.3}.get(cand.get('kind'), 0.3)
                    neg.spawn(self._dial(neg, cand, delay))
            if 'punch' in methods and punch and answer.get('punch'):
                neg.spawn(self._punch(neg, answer['punch'], delay=0.6 if remote and 'direct' in methods else 0))
            direct_wait = 0 if self.force_relay else (5.5 if punch and answer.get('punch') else 3.5)
            if not ('direct' in methods or 'punch' in methods):
                direct_wait = 0
            if direct_wait:
                try:
                    return await asyncio.wait_for(asyncio.shield(neg.result), direct_wait)
                except asyncio.TimeoutError:
                    pass
            if 'relay' not in methods:
                raise NegotiationError('无法直连')
            token = secrets.token_urlsafe(18)
            ok = asyncio.get_running_loop().create_future()
            self._relay_ok[token] = ok
            await self._rv_send({'op': 'relay', 'token': token})
            await asyncio.wait_for(ok, 15)
            await self._signal_send(peer, {'t': 'relay', 'sid': sid, 'token': token})
            neg.spawn(self._relay_dial(neg, token, server_side=False))
            return await asyncio.wait_for(asyncio.shield(neg.result), 45)
        except asyncio.TimeoutError as exc:
            raise NegotiationError('连接对方超时') from exc
        finally:
            self.pending.pop(sid, None)
            neg.cleanup()

    async def _respond(self, neg: Negotiation, offer: dict) -> None:
        try:
            methods = offer.get('methods') or ['direct', 'punch', 'relay']
            punch = await self._probe(neg) if 'punch' in methods and offer.get('punch') else None
            await self._signal_send(neg.peer, {'t': 'accept', 'sid': neg.sid,
                                               'cands': self._candidates('direct' in methods), 'punch': punch})
            if 'direct' in methods and not self.force_relay and not self.no_direct:
                for cand in (offer.get('cands') or [])[:12]:
                    delay = {'lan': 0, 'ipv6': 0.1, 'public': 0.3}.get(cand.get('kind'), 0.3)
                    neg.spawn(self._dial(neg, cand, delay))
            if punch and offer.get('punch'):
                neg.spawn(self._punch(neg, offer['punch'], delay=0.6 if offer.get('cands') and 'direct' in methods else 0))
            link = await asyncio.wait_for(asyncio.shield(neg.result), 90)
        except (asyncio.TimeoutError, NegotiationError, OSError):
            return
        finally:
            self.pending.pop(neg.sid, None)
            neg.cleanup()
        if neg.kind == 'control':
            self._install_control(link)
        elif neg.kind == 'data':
            self._spawn(self._serve_data(link, neg.tid))

    def _install_control(self, link: Link) -> Control:
        control = Control(self, link)
        old = self.controls.get(link.peer)
        self.controls[link.peer] = control
        if old is not None and old.alive:
            # Keep the old one running for in-flight calls; new calls use the newest.
            pass
        self.notify()
        return control

    async def control_for(self, peer: str) -> Control:
        current = self.controls.get(peer)
        if current is not None and current.alive:
            return current
        pending = self._connecting.get(peer)
        if pending is None:
            pending = asyncio.ensure_future(self.negotiate(peer, 'control'))
            self._connecting[peer] = pending
            pending.add_done_callback(lambda _f, p=peer: self._connecting.pop(p, None))
        link = await asyncio.shield(pending)
        current = self.controls.get(peer)
        if current is not None and current.alive and (current.link is link or link.closing):
            return current
        return self._install_control(link)

    # ---------- RPC handlers (requests from a peer) ----------
    async def handle_rpc(self, peer: str, link: Link, msg: dict) -> dict:
        op = msg.get('op')
        if op == 'info':
            return {'name': self.config['name'], 'version': VERSION, 'roots': self.roots}
        if op == 'list':
            return await self.io(fs.list_dir, msg.get('path') or '', self.roots)
        if op == 'mkdir':
            return {'path': await self.io(fs.make_dir, msg.get('path'), msg.get('name'), self.roots)}
        if op == 'offer':
            return await self._offer(peer, link, msg)
        if op == 'finish':
            record = self.incoming.get(msg.get('tid'))
            if record is None or record['peer'] != peer:
                raise RuntimeError('没有这个传输')
            result = await self.io(record['transfer'].finish)
            record['status'] = 'done' if result['complete'] else 'receiving'
            self.notify()
            return result
        if op == 'cancel':
            record = self.incoming.get(msg.get('tid'))
            if record is not None and record['peer'] == peer:
                await self.io(record['transfer'].close, bool(msg.get('discard')))
                record['status'] = 'cancelled' if msg.get('discard') else 'paused'
                self.notify()
            return {}
        raise RuntimeError('不支持的操作')

    async def _offer(self, peer: str, link: Link, msg: dict) -> dict:
        tid, dest = msg.get('tid'), msg.get('dest')
        await self.io(fs.check_allowed, dest, self.roots)
        try:
            manifest = json.loads(zlib.decompress(base64.b64decode(msg['manifest'])))
        except (KeyError, ValueError, zlib.error) as exc:
            raise RuntimeError('清单无效') from exc
        offer = {'name': msg.get('name'), 'single': msg.get('single'), 'chunk': msg.get('chunk'),
                 'files': manifest.get('files'), 'dirs': manifest.get('dirs', [])}
        old = self.incoming.get(tid)
        if old is not None:
            if old['peer'] != peer:
                raise RuntimeError('传输号冲突')
            await self.io(old['transfer'].close)
        transfer = IncomingTransfer(tid, dest, offer, skip_existing=bool(self.config.get('skip_existing', True)))
        if not fs.within(transfer.base, dest):
            raise RuntimeError('目标越界')
        result = await self.io(transfer.prepare)
        self.incoming[tid] = {'transfer': transfer, 'peer': peer, 'status': 'receiving', 'route': link.route,
                              'created': old['created'] if old else time.time()}
        self.notify()
        return result

    # ---------- receiving data ----------
    async def _serve_data(self, link: Link, tid) -> None:
        record = self.incoming.get(tid)
        if record is None or record['peer'] != link.peer:
            link.close()
            return
        transfer: IncomingTransfer = record['transfer']
        record['route'] = link.route
        queue: asyncio.Queue = asyncio.Queue(3)

        async def writer_loop():
            while True:
                item = await queue.get()
                if item is None:
                    return
                index, offset, data, digest = item
                status = await self.io(transfer.write, index, offset, data, digest)
                link.writer.write(secure.pack_ack(index, offset, status))
                await link.writer.drain()
                self.notify()

        writer = asyncio.ensure_future(writer_loop())
        receiver = secure.FrameReceiver.attach(link.writer, link.reader)
        try:
            while not writer.done():
                kind, payload = await receiver.read()
                if kind != T_CHUNK or len(payload) < CHUNK_HEADER.size:
                    break
                index, offset, length, digest = CHUNK_HEADER.unpack_from(payload)
                data = memoryview(payload)[CHUNK_HEADER.size:]
                if len(data) != length:
                    break
                await queue.put((index, offset, data, digest))
        except (asyncio.IncompleteReadError, ConnectionError, OSError, secure.FrameError):
            pass
        finally:
            if not writer.done():
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
            link.close()

    # ---------- sending ----------
    def _load_transfers(self) -> None:
        path = self.state / 'transfers.json'
        try:
            records = json.loads(path.read_text('utf-8'))
        except (OSError, ValueError):
            records = []
        for record in records:
            try:
                out = Outgoing(record)
            except KeyError:
                continue
            if out.status in STATUS_ACTIVE:
                out.status = 'waiting'
            self.outgoing[out.tid] = out

    def save_transfers(self) -> None:
        path = self.state / 'transfers.json'
        records = [o.record() for o in sorted(self.outgoing.values(), key=lambda o: -o.created)[:200]]
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(records, ensure_ascii=False, indent=1), 'utf-8')
        os.replace(tmp, path)

    def send(self, src: str, peer: str, dest: str) -> Outgoing:
        fs.check_allowed(src, self.roots)
        if not os.path.exists(src):
            raise FileNotFoundError('源路径不存在')
        if not isinstance(dest, str) or not dest:
            raise ValueError('请选择目标文件夹')
        if peer not in self.peers:
            raise ValueError('对方不在线')
        tid = transfer_id(self.peer_id, src, peer, dest)
        out = self.outgoing.get(tid)
        if out is not None and out.task is not None and not out.task.done():
            return out
        out = Outgoing({'tid': tid, 'src': os.path.abspath(src), 'peer': peer, 'dest': dest,
                        'peer_name': self.peer_name(peer), 'status': 'scanning',
                        'created': out.created if out else time.time()})
        self.outgoing[tid] = out
        self._start_outgoing(out)
        return out

    def _start_outgoing(self, out: Outgoing) -> None:
        out.paused = False
        out.error = ''
        out.task = asyncio.ensure_future(self._run_outgoing(out))
        out.task.add_done_callback(lambda _t, o=out: setattr(o, 'task', None))
        self.save_transfers()
        self.notify()

    def pause(self, tid: str) -> None:
        out = self.outgoing[tid]
        out.paused = True
        if out.task:
            out.task.cancel()
        out.status = 'paused'
        self.save_transfers()
        self.notify()

    def resume(self, tid: str) -> None:
        out = self.outgoing[tid]
        if out.task is None:
            out.status = 'connecting'
            out.attempts = 0
            self._start_outgoing(out)

    async def cancel(self, tid: str) -> None:
        out = self.outgoing.get(tid)
        if out is None:
            record = self.incoming.pop(tid, None)
            if record is not None:
                await self.io(record['transfer'].close, record['status'] != 'done')
            self.notify()
            return
        out.paused = True
        if out.task:
            out.task.cancel()
        if out.status != 'done':
            try:
                control = await asyncio.wait_for(self.control_for(out.peer), 10)
                await control.call('cancel', tid=tid, discard=True, timeout=10)
            except Exception:
                pass
        del self.outgoing[tid]
        self.save_transfers()
        self.notify()

    def remove(self, tid: str) -> None:
        out = self.outgoing.get(tid)
        if out is not None and (out.task is None or out.task.done()):
            del self.outgoing[tid]
            self.save_transfers()
        record = self.incoming.get(tid)
        if record is not None and record['status'] in ('done', 'cancelled', 'paused'):
            del self.incoming[tid]
        self.notify()

    async def _run_outgoing(self, out: Outgoing) -> None:
        delay = 2
        while True:
            try:
                await self._attempt(out)
                return
            except asyncio.CancelledError:
                if not out.paused:
                    out.status = 'waiting'
                self.save_transfers()
                self.notify()
                raise
            except (FileChangedError, FileNotFoundError, PermissionError, NotADirectoryError) as exc:
                out.status, out.error = 'failed', str(exc) or type(exc).__name__
                if isinstance(exc, FileChangedError):
                    out.error = '源文件在传输中被修改，请重新发送：' + str(exc)
                self.save_transfers()
                self.notify()
                return
            except Exception as exc:
                out.attempts += 1
                out.error = str(exc) or type(exc).__name__
                out.status = 'waiting'
                out.route = ''
                self.save_transfers()
                self.notify()
                LOG.info('transfer_retry tid=%s error=%s attempt=%d', out.tid, out.error, out.attempts)
                if out.peer not in self.peers:
                    # Event-driven: wake when the roster changes, not on a timer loop.
                    out.task = None
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def _attempt(self, out: Outgoing) -> None:
        if out.manifest is None:
            out.status = 'scanning'
            self.notify()
            out.manifest = await self.io(fs.scan, out.src, self.roots)
            out.bytes_total = out.manifest['bytes']
            out.files = len(out.manifest['files'])
        manifest = out.manifest
        out.status = 'connecting'
        self.notify()
        control = await self.control_for(out.peer)
        out.route = control.link.route
        if not out.chunk:
            out.chunk = RELAY_CHUNK if out.route == 'relay' and self.chunk > RELAY_CHUNK else self.chunk
        packed = base64.b64encode(zlib.compress(json.dumps(
            {'files': manifest['files'], 'dirs': manifest['dirs']}, separators=(',', ':')).encode(), 6)).decode()
        reply = await control.call('offer', timeout=300, tid=out.tid, dest=out.dest, name=manifest['name'],
                                   single=manifest['single'], chunk=out.chunk, manifest=packed)
        out.bytes_done = reply['bytes_done']
        out.base = reply.get('base', '')
        out.skipped_files = reply.get('skipped_files', 0)
        items = plan(manifest['files'], reply['need'], out.chunk)
        out.status = 'sending'
        self.save_transfers()
        self.notify()
        if items:
            await self._pump(out, control, items)
        out.status = 'finishing'
        self.notify()
        result = await control.call('finish', timeout=120, tid=out.tid)
        if not result.get('complete'):
            raise RuntimeError(f"还有 {result.get('missing_files')} 个文件未完成")
        out.status, out.error, out.finished, out.speed = 'done', '', time.time(), 0.0
        out.bytes_done = out.bytes_total
        self.save_transfers()
        self.notify()

    async def _pump(self, out: Outgoing, control: Control, items: list) -> None:
        route = control.link.route
        if route == 'relay':
            methods = ('relay',)
        elif route == 'punch':
            methods = ('punch', 'relay')
        else:
            methods = ('direct', 'relay')
        workers = max(1, min(16, int(self.config.get('workers', 4))))
        if route == 'relay':
            workers = 1
        if len(items) < workers:
            workers = max(1, len(items))
        results = await asyncio.gather(*[self.negotiate(out.peer, 'data', out.tid, methods) for _ in range(workers)],
                                       return_exceptions=True)
        links = [r for r in results if isinstance(r, Link)]
        if not links:
            raise ConnectionError('数据通道建立失败：' + '; '.join(str(r) for r in results if isinstance(r, Exception))[:200])
        out.route = links[0].route
        self.notify()
        queue = deque(items)
        state = {'remaining': len(items), 'retries': {}}
        done = asyncio.Event()
        wake = asyncio.Event()
        root = manifest_root(out.manifest)
        files = out.manifest['files']

        async def worker(link: Link):
            budget = RELAY_WINDOW_BYTES if link.route == 'relay' else WINDOW_BYTES
            pending: dict[tuple[int, int], tuple[int, int, int]] = {}
            inflight = [0]
            freed = asyncio.Event()

            async def acks():
                while True:
                    kind, payload = await secure.read_frame(link.reader)
                    if kind != T_ACK:
                        raise secure.FrameError('ack expected')
                    index, offset, status = ACK.unpack(payload)
                    item = pending.pop((index, offset), None)
                    if item is None:
                        continue
                    inflight[0] -= item[2]
                    freed.set()
                    if status == 0:
                        out.bytes_done += item[2]
                        state['remaining'] -= 1
                        if state['remaining'] == 0:
                            done.set()
                            wake.set()
                        self.notify()
                    else:
                        count = state['retries'].get(item[:2], 0) + 1
                        state['retries'][item[:2]] = count
                        if count > 3:
                            raise RuntimeError('对方多次校验或写盘失败：' + files[index][0])
                        queue.appendleft(item)
                        wake.set()

            ack_task = asyncio.ensure_future(acks())
            try:
                while not done.is_set():
                    if ack_task.done():
                        ack_task.result()
                        raise ConnectionError('数据通道断开')
                    if not queue:
                        wake.clear()
                        waiter = asyncio.ensure_future(wake.wait())
                        await asyncio.wait({waiter, ack_task}, return_when=asyncio.FIRST_COMPLETED)
                        waiter.cancel()
                        continue
                    if pending and (inflight[0] + queue[0][2] > budget or len(pending) >= MAX_INFLIGHT):
                        freed.clear()
                        waiter = asyncio.ensure_future(freed.wait())
                        await asyncio.wait({waiter, ack_task}, return_when=asyncio.FIRST_COMPLETED)
                        waiter.cancel()
                        continue
                    item = queue.popleft()
                    index, offset, length = item
                    rel, size, mtime = files[index]
                    path = os.path.join(root, *rel.split('/'))
                    pending[(index, offset)] = item
                    inflight[0] += length
                    try:
                        data, digest = await self.io(read_chunk, path, offset, length, size, mtime)
                    except Exception:
                        pending.pop((index, offset), None)
                        inflight[0] -= length
                        raise
                    link.writer.write(secure.chunk_header(index, offset, length, digest))
                    link.writer.write(data)
                    await link.writer.drain()
            finally:
                ack_task.cancel()
                await asyncio.gather(ack_task, return_exceptions=True)
                if pending:
                    queue.extendleft(pending.values())
                    pending.clear()
                    wake.set()
                link.close()

        tasks = [asyncio.ensure_future(worker(link)) for link in links]
        try:
            pending_tasks = set(tasks)
            error = None
            while pending_tasks and not done.is_set():
                finished, pending_tasks = await asyncio.wait(pending_tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in finished:
                    exc = task.exception()
                    if isinstance(exc, (FileChangedError, FileNotFoundError, PermissionError)):
                        raise exc
                    if isinstance(exc, RuntimeError):
                        raise exc
                    error = exc or error
            if not done.is_set():
                raise ConnectionError(f'全部数据通道已断开：{error}')
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def manifest_root(manifest: dict) -> str:
    return manifest['root']


def ssl_error():
    import ssl
    return ssl.SSLError
