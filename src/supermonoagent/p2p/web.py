"""Loopback-only page and JSON/SSE API for one agent. Never listens on a public address."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import files as fs
from . import secure

LOG = logging.getLogger('sma-p2p-web')
STATIC = Path(__file__).with_name('static')
MAX_BODY = 1 << 20


class WebUI:
    def __init__(self, agent, port: int):
        self.agent, self.port = agent, port
        token_path = agent.state / 'local-token'
        if token_path.exists():
            self.token = token_path.read_text('ascii').strip()
        else:
            self.token = secrets.token_urlsafe(24)
            token_path.write_text(self.token, 'ascii')
        self.server = None

    @property
    def url(self) -> str:
        return f'http://127.0.0.1:{self.port}/#t={self.token}'

    async def start(self) -> None:
        for attempt in range(10):
            try:
                self.server = await asyncio.start_server(self.handle, '127.0.0.1', self.port + attempt if self.port else 0)
                break
            except OSError:
                if not self.port or attempt == 9:
                    raise
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()

    # ---------- HTTP plumbing ----------
    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 30)
                lines = head.decode('latin-1').split('\r\n')
                method, target, _ = lines[0].split(' ', 2)
                headers = {}
                for line in lines[1:]:
                    if ':' in line:
                        k, v = line.split(':', 1)
                        headers[k.strip().lower()] = v.strip()
                length = int(headers.get('content-length') or 0)
                if length > MAX_BODY:
                    await self.respond(writer, 413, {'error': 'too large'})
                    return
                body = await reader.readexactly(length) if length else b''
                keep = await self.route(method, target, headers, body, writer)
                if not keep or headers.get('connection', '').lower() == 'close':
                    return
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, ValueError):
            pass
        finally:
            writer.close()

    async def respond(self, writer, status: int, obj=None, *, body: bytes | None = None, ctype='application/json') -> None:
        if body is None:
            body = json.dumps(obj, ensure_ascii=False).encode()
        reason = {200: 'OK', 400: 'Bad Request', 403: 'Forbidden', 404: 'Not Found', 413: 'Too Large', 500: 'Error'}.get(status, 'OK')
        writer.write((f'HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n'
                      'Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n'
                      "Content-Security-Policy: default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'\r\n"
                      '\r\n').encode() + body)
        await writer.drain()

    def _host_ok(self, headers) -> bool:
        # DNS rebinding guard: only our own loopback authority.
        return headers.get('host') in (f'127.0.0.1:{self.port}', f'localhost:{self.port}')

    def _authorized(self, headers, query) -> bool:
        offered = headers.get('x-sma-token') or (query.get('t') or [''])[0]
        return bool(offered) and hmac.compare_digest(offered, self.token)

    async def route(self, method, target, headers, body, writer) -> bool:
        parts = urlsplit(target)
        path, query = parts.path, parse_qs(parts.query)
        if not self._host_ok(headers):
            await self.respond(writer, 403, {'error': 'host'})
            return False
        if method == 'GET' and path == '/':
            await self.respond(writer, 200, body=(STATIC / 'p2p.html').read_bytes(), ctype='text/html; charset=utf-8')
            return True
        if not path.startswith('/api/'):
            await self.respond(writer, 404, {'error': 'not found'})
            return True
        if not self._authorized(headers, query):
            await self.respond(writer, 403, {'error': '本机页面令牌无效，请从程序打印的地址打开'})
            return True
        if method == 'GET' and path == '/api/events':
            await self.events(writer)
            return False
        if method == 'GET' and path == '/api/state':
            await self.respond(writer, 200, self.agent.snapshot())
            return True
        if method != 'POST':
            await self.respond(writer, 404, {'error': 'not found'})
            return True
        if headers.get('origin') not in (None, f'http://127.0.0.1:{self.port}', f'http://localhost:{self.port}'):
            await self.respond(writer, 403, {'error': 'origin'})
            return True
        try:
            data = json.loads(body or b'{}')
            if not isinstance(data, dict):
                raise ValueError('json object required')
            result = await self.api(path, data)
            await self.respond(writer, 200, result)
        except (ValueError, PermissionError, FileNotFoundError, NotADirectoryError, RuntimeError,
                ConnectionError, KeyError, OSError, asyncio.TimeoutError) as exc:
            await self.respond(writer, 400, {'error': str(exc) or type(exc).__name__})
        return True

    async def events(self, writer) -> None:
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n\r\n')
        queue: asyncio.Queue = asyncio.Queue(2)
        self.agent.subscribers.add(queue)
        try:
            queue.put_nowait(self.agent.snapshot())
            while True:
                try:
                    snapshot = await asyncio.wait_for(queue.get(), 20)
                    writer.write(b'data: ' + json.dumps(snapshot, ensure_ascii=False).encode() + b'\n\n')
                except asyncio.TimeoutError:
                    writer.write(b': keepalive\n\n')
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            self.agent.subscribers.discard(queue)

    # ---------- API ----------
    async def api(self, path: str, data: dict) -> dict:
        agent = self.agent
        if path == '/api/local/list':
            return await agent.io(fs.list_dir, data.get('path') or '', agent.roots)
        if path == '/api/local/mkdir':
            return {'path': await agent.io(fs.make_dir, data.get('path'), data.get('name'), agent.roots)}
        if path == '/api/peer/list':
            control = await asyncio.wait_for(agent.control_for(data.get('peer')), 45)
            return await control.call('list', path=data.get('path') or '')
        if path == '/api/peer/mkdir':
            control = await asyncio.wait_for(agent.control_for(data.get('peer')), 45)
            return await control.call('mkdir', path=data.get('path'), name=data.get('name'))
        if path == '/api/send':
            out = agent.send(data.get('src'), data.get('peer'), data.get('dest'))
            return {'tid': out.tid}
        if path == '/api/transfer':
            tid, action = data.get('tid'), data.get('action')
            if action == 'pause':
                agent.pause(tid)
            elif action == 'resume':
                agent.resume(tid)
            elif action == 'cancel':
                await agent.cancel(tid)
            elif action == 'remove':
                agent.remove(tid)
            else:
                raise ValueError('未知操作')
            return {}
        if path == '/api/join-code':
            return {'code': secure.join_code(agent.key)}
        if path == '/api/join':
            agent.join(data.get('code'))
            return {}
        if path == '/api/rename':
            agent.rename(data.get('name'))
            return {}
        raise ValueError('未知接口')
