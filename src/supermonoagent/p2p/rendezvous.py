"""sma-p2p rendezvous: presence, signalling, NAT address probe, ciphertext pipe fallback.

Standalone: standard library only, Python 3.10+. Never sees the network key; pipes carry
end-to-end TLS between peers and are spliced without decoding.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import time

LOG = logging.getLogger('sma-p2p-rendezvous')
LINE_LIMIT = 64 * 1024
HEX = re.compile(r'^[0-9a-f]{8,64}$')
TOKEN = re.compile(r'^[A-Za-z0-9_-]{16,64}$')
VERSION = 1


class Peer:
    def __init__(self, net, peer, name, writer, addr):
        self.net, self.peer, self.name, self.writer, self.addr = net, peer, name, writer, addr
        self.queue: asyncio.Queue = asyncio.Queue(512)
        self.since = time.time()
        self.tokens = 0

    def send(self, obj) -> bool:
        try:
            self.queue.put_nowait(obj)
            return True
        except asyncio.QueueFull:
            return False


class Rendezvous:
    def __init__(self, *, max_peers_per_net=64, max_conns_per_ip=48, max_pipes=64,
                 pipe_idle=120.0, control_idle=90.0):
        self.nets: dict[str, dict[str, Peer]] = {}
        self.tokens: dict[str, tuple[str, float]] = {}
        self.waiting: dict[str, tuple[asyncio.StreamReader, asyncio.StreamWriter, asyncio.Future]] = {}
        self.per_ip: dict[str, int] = {}
        self.pipes = 0
        self.max_peers_per_net, self.max_conns_per_ip, self.max_pipes = max_peers_per_net, max_conns_per_ip, max_pipes
        self.pipe_idle, self.control_idle = pipe_idle, control_idle
        self.stats = {'pipe_bytes': 0, 'pipes_total': 0, 'signals': 0}

    # ---------- connection entry ----------
    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peername = writer.get_extra_info('peername') or ('?', 0)
        ip, port = peername[0], peername[1]
        if ip.startswith('::ffff:'):
            ip = ip[7:]
        if self.per_ip.get(ip, 0) >= self.max_conns_per_ip:
            writer.close()
            return
        self.per_ip[ip] = self.per_ip.get(ip, 0) + 1
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            msg = json.loads(line)
            if not isinstance(msg, dict):
                return
            op = msg.get('op')
            if op == 'hello':
                await self.control(reader, writer, msg, (ip, port))
            elif op == 'probe':
                await self.probe(reader, writer, (ip, port))
            elif op == 'pipe':
                await self.pipe(reader, writer, msg)
        except (asyncio.TimeoutError, ValueError, ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        except Exception:  # never let one socket kill the service
            LOG.exception('handler_failed')
        finally:
            self.per_ip[ip] -= 1
            if not self.per_ip[ip]:
                del self.per_ip[ip]
            writer.close()

    @staticmethod
    async def _write(writer, obj) -> None:
        writer.write(json.dumps(obj, separators=(',', ':')).encode() + b'\n')
        await writer.drain()

    # ---------- probe: report the NAT-mapped address, keep the mapping alive ----------
    async def probe(self, reader, writer, addr) -> None:
        await self._write(writer, {'op': 'probe', 'addr': list(addr)})
        try:
            await asyncio.wait_for(reader.read(1), 30)
        except asyncio.TimeoutError:
            pass

    # ---------- control ----------
    def roster(self, net: str) -> list[dict]:
        return [{'peer': p.peer, 'name': p.name, 'ip': p.addr[0], 'since': int(p.since)}
                for p in self.nets.get(net, {}).values()]

    def broadcast(self, net: str) -> None:
        peers = self.roster(net)
        for p in self.nets.get(net, {}).values():
            p.send({'op': 'peers', 'peers': peers})

    async def control(self, reader, writer, msg, addr) -> None:
        net, peer_id, name = msg.get('net'), msg.get('peer'), msg.get('name')
        if (not isinstance(net, str) or not HEX.match(net) or not isinstance(peer_id, str)
                or not HEX.match(peer_id) or not isinstance(name, str) or not 0 < len(name) <= 64):
            await self._write(writer, {'op': 'error', 'error': 'bad hello'})
            return
        members = self.nets.setdefault(net, {})
        if peer_id not in members and len(members) >= self.max_peers_per_net:
            await self._write(writer, {'op': 'error', 'error': 'network full'})
            return
        old = members.get(peer_id)
        peer = Peer(net, peer_id, name, writer, addr)
        members[peer_id] = peer
        if old is not None:
            old.send(None)
        LOG.info('peer_online net=%s peer=%s ip=%s', net[:8], peer_id, addr[0])
        sender = asyncio.ensure_future(self._sender(peer))
        await self._write(writer, {'op': 'welcome', 'addr': list(addr), 'version': VERSION})
        self.broadcast(net)
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), self.control_idle)
                if not line:
                    return
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                op = msg.get('op')
                if op == 'ping':
                    peer.send({'op': 'pong'})
                elif op == 'signal':
                    target = members.get(msg.get('to')) if isinstance(msg.get('to'), str) else None
                    if target is None or target is peer:
                        peer.send({'op': 'signal_failed', 'to': msg.get('to'), 'ref': msg.get('ref')})
                        continue
                    self.stats['signals'] += 1
                    target.send({'op': 'signal', 'from': peer_id, 'data': msg.get('data')})
                elif op == 'relay':
                    token = msg.get('token')
                    self._expire_tokens()
                    if isinstance(token, str) and TOKEN.match(token) and len(self.tokens) < 4096:
                        self.tokens[token] = (net, time.monotonic() + 60)
                        peer.send({'op': 'relay_ok', 'token': token})
        finally:
            if members.get(peer_id) is peer:
                del members[peer_id]
                if not members:
                    self.nets.pop(net, None)
                else:
                    self.broadcast(net)
                LOG.info('peer_offline net=%s peer=%s', net[:8], peer_id)
            peer.send(None)
            await asyncio.gather(sender, return_exceptions=True)

    async def _sender(self, peer: Peer) -> None:
        try:
            while True:
                item = await peer.queue.get()
                if item is None:
                    peer.writer.close()
                    return
                await self._write(peer.writer, item)
        except (ConnectionError, OSError):
            peer.writer.close()

    def _expire_tokens(self) -> None:
        now = time.monotonic()
        for token in [t for t, (_, exp) in self.tokens.items() if exp < now]:
            del self.tokens[token]

    # ---------- pipe fallback ----------
    async def pipe(self, reader, writer, msg) -> None:
        token = msg.get('token')
        self._expire_tokens()
        if not isinstance(token, str) or token not in self.tokens:
            await self._write(writer, {'op': 'error', 'error': 'unknown token'})
            return
        waiting = self.waiting.pop(token, None)
        if waiting is None:
            if self.pipes >= self.max_pipes:
                await self._write(writer, {'op': 'error', 'error': 'relay busy'})
                return
            loop = asyncio.get_running_loop()
            paired, done = loop.create_future(), loop.create_future()
            self.waiting[token] = (reader, writer, (paired, done))
            try:
                await asyncio.wait_for(asyncio.shield(paired), 60)
            except asyncio.TimeoutError:
                if self.waiting.get(token, (None, None, (None,)))[2][0] is paired:
                    del self.waiting[token]
                return
            await done  # the pairing side splices both directions and resolves when finished
            return
        self.tokens.pop(token, None)
        other_reader, other_writer, (paired, done) = waiting
        paired.set_result(None)
        self.pipes += 1
        self.stats['pipes_total'] += 1
        try:
            await self._write(other_writer, {'op': 'paired'})
            await self._write(writer, {'op': 'paired'})
            await asyncio.gather(self._splice(reader, other_writer), self._splice(other_reader, writer))
        except (ConnectionError, OSError):
            pass
        finally:
            self.pipes -= 1
            other_writer.close()
            if not done.done():
                done.set_result(None)

    async def _splice(self, reader, writer) -> None:
        try:
            while True:
                data = await asyncio.wait_for(reader.read(256 * 1024), self.pipe_idle)
                if not data:
                    break
                self.stats['pipe_bytes'] += len(data)
                writer.write(data)
                await writer.drain()
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass
        finally:
            try:
                writer.write_eof()
            except (OSError, RuntimeError, AttributeError):
                writer.close()


async def serve(host: str, port: int, server: Rendezvous | None = None):
    server = server or Rendezvous()
    listener = await asyncio.start_server(server.handle, host, port, limit=LINE_LIMIT)
    return server, listener


def main() -> None:
    parser = argparse.ArgumentParser(description='sma-p2p rendezvous server')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8827)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    async def run():
        server, listener = await serve(args.host, args.port)
        LOG.info('listening host=%s port=%d', args.host, args.port)

        async def report():
            while True:
                await asyncio.sleep(300)
                LOG.info('stats nets=%d peers=%d pipes=%d %s', len(server.nets),
                         sum(len(m) for m in server.nets.values()), server.pipes, server.stats)
        reporter = asyncio.ensure_future(report())
        async with listener:
            await listener.serve_forever()
        reporter.cancel()
    asyncio.run(run())


if __name__ == '__main__':
    main()
