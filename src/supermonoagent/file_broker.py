"""Independent authenticated file staging/outbox service. Never operates PTYs.

Serve ``handle_socket`` with websockets ``max_size=MAX_FRAME, max_queue=8``.
TLS is provided by the standalone CLI or a trusted reverse proxy. Storage and
authentication files must be private to this service account. One process owns
each registry; multiple broker writers on the same storage are not supported.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import socket
import ssl
import stat
import threading
import time

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

from .file_transfer import TransferStore, MAX_CHUNK, _plain_directory, _validate_name
from .file_workspaces import timestamped_name

PROTOCOL = 'sma-files-1'
MAX_FRAME = 262144
_ROUTED = frozenset({'workspace.get', 'workspace.browse', 'workspace.set', 'library.get', 'library.set'})
_HOST_ERRORS = frozenset({'invalid_request', 'session_unavailable', 'unauthorized', 'revision_conflict',
                          'target_changed', 'directory_unavailable', 'unavailable', 'permission_denied', 'project_required'})
_ID = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
_HEX = re.compile(r'[a-f0-9]{64}\Z')


def _stream_sha256(source):
    digest = hashlib.sha256()
    for chunk in iter(lambda: source.read(1024 * 1024), b''):
        digest.update(chunk)
    return digest.hexdigest()


class BrokerError(ValueError):
    """A fixed, public error code, never an OS error or credential."""


def _fail(code='invalid_request'):
    raise BrokerError(code)


def _label(value, limit=128):
    if (not isinstance(value, str) or not value or len(value) > limit
            or not value.isprintable() or value != value.strip()):
        _fail()
    return value


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        _fail()
    return value


def _integer(value, minimum=0, maximum=9007199254740991):
    if type(value) is not int or not minimum <= value <= maximum:
        _fail()
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _json(raw):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_FRAME:
        _fail()
    try:
        return json.loads(raw, object_pairs_hook=_unique,
                          parse_constant=lambda _value: _fail())
    except (ValueError, UnicodeError, RecursionError):
        _fail()


def enrollment_proof(key, room, host_id, host_secret, timestamp, nonce):
    """Canonical enrollment HMAC shared with independently installed companions."""
    raw = json.dumps([PROTOCOL, room, host_id, hashlib.sha256(host_secret.encode()).hexdigest(),
                      timestamp, nonce], ensure_ascii=False, separators=(',', ':')).encode()
    return hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()


def _local_path(value):
    path = Path(os.path.abspath(value))
    if os.name == 'nt':
        text = str(path)
        if text.startswith('\\\\?\\'):
            text = text[4:]
        if not re.match(r'[A-Za-z]:\\', text):
            raise ValueError('Broker storage must use a local drive')
        path = Path('\\\\?\\' + text)
    return path


def _auth(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(1024 * 1024 + 1)
    try:
        if len(raw) > 1024 * 1024:
            _fail()
        value = json.loads(raw, object_pairs_hook=_unique)
        if (not isinstance(value, dict) or set(value) != {'schema', 'owners'}
                or type(value['schema']) is not int or value['schema'] != 1
                or not isinstance(value['owners'], list) or not 1 <= len(value['owners']) <= 128):
            _fail()
        owners, tokens = {}, {}
        for owner in value['owners']:
            if not isinstance(owner, dict) or set(owner) != {'scope', 'token_sha256', 'rooms', 'enrollment_key'}:
                _fail()
            scope = _id(owner['scope'])
            if scope in owners:
                _fail()
            rooms, digests, key = owner['rooms'], owner['token_sha256'], owner['enrollment_key']
            if (not isinstance(rooms, list) or not 1 <= len(rooms) <= 256
                    or any(_label(room) != room for room in rooms) or len(set(rooms)) != len(rooms)
                    or '*' in rooms and rooms != ['*']
                    or not isinstance(digests, list) or not 1 <= len(digests) <= 16
                    or not isinstance(key, str) or not 32 <= len(key) <= 8192
                    or any(not 33 <= ord(char) <= 126 for char in key)):
                _fail()
            for digest in digests:
                if not isinstance(digest, str) or not _HEX.fullmatch(digest) or digest in tokens:
                    _fail()
                tokens[digest] = scope
            owners[scope] = {'rooms': frozenset(rooms), 'key': key}
        return owners, tokens
    except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        raise ValueError('Invalid file broker authentication configuration') from None


@dataclass(eq=False)
class _Connection:
    ws: object
    scope: str = ''
    room: str = ''
    role: str = ''
    token_digest: str = ''
    identity: str = field(default_factory=lambda: secrets.token_hex(16))
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    replay: object = None


class Broker:
    def __init__(self, root, auth_file, registry_file=None, *, clock=time.time,
                 max_file_size=512 * 1024**2, max_total_bytes=2 * 1024**3,
                 max_files=4096, max_active=16, upload_ttl=300, route_timeout=15,
                 io_workers=2, max_connections=128, max_routes=64, max_hosts=1024,
                 ticket_ttl=60, max_tickets=4096, http_workers=8):
        for value, low, high in [(max_file_size, 0, 2**40), (max_total_bytes, 0, 2**50),
                (max_files, 1, 100000), (max_active, 1, 1024), (io_workers, 1, 16),
                (max_connections, 1, 4096), (max_routes, 1, 1024), (max_hosts, 1, 100000),
                (max_tickets, 1, 100000), (http_workers, 1, 64)]:
            _integer(value, low, high)
        for value in (upload_ttl, route_timeout, ticket_ttl):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 3600:
                raise ValueError('Invalid file broker timeout')
        self.owners, self.tokens = _auth(auth_file)
        self._auth_file = Path(auth_file).absolute()
        self.root = _local_path(root)
        # Reuse the receiver's ancestor/reparse safeguards before any DB write.
        self._base = TransferStore(self.root / 'file', max_file_size, max_active)
        database = Path(registry_file) if registry_file else self.root / 'broker.sqlite3'
        database = _local_path(database)
        for ancestor in reversed(database.parents):
            _plain_directory(ancestor)
        if database.exists() or database.is_symlink():
            info = database.lstat()
            if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Invalid broker registry path')
        lock_path = database.with_name(database.name + '.lock')
        if lock_path.exists() or lock_path.is_symlink():
            info = lock_path.lstat()
            if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Invalid broker lock path')
        self._process_lock = lock_path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self._process_lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._process_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._process_lock.close()
            raise ValueError('Broker registry is already in use') from None
        self._db = None
        try:
            self._db = sqlite3.connect(database, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.executescript('''PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS hosts(scope TEXT, room TEXT, host_id TEXT, secret TEXT,
                    PRIMARY KEY(scope,room));
                CREATE TABLE IF NOT EXISTS nonces(scope TEXT, nonce TEXT, created INTEGER,
                    PRIMARY KEY(scope,nonce));
                CREATE TABLE IF NOT EXISTS jobs(file_id TEXT PRIMARY KEY, scope TEXT, room TEXT,
                    session TEXT, window_id TEXT, daemon_epoch TEXT, revision INTEGER,
                    name TEXT, size INTEGER, sha256 TEXT, state TEXT, receipt TEXT, error TEXT);
                CREATE INDEX IF NOT EXISTS room_jobs ON jobs(scope,room,state);''')
            if 'project_binding' not in {row[1] for row in self._db.execute('PRAGMA table_info(jobs)')}:
                self._db.execute('ALTER TABLE jobs ADD COLUMN project_binding TEXT')
            self._db.execute("UPDATE jobs SET state='failed',error='project_binding_required' WHERE project_binding IS NULL AND state IN ('staged','queued','downloading')")
            self._db.commit()
        except BaseException:
            if self._db is not None:
                self._db.close()
            self._process_lock.close()
            raise
        self.clock = clock
        self.max_file_size, self.max_total_bytes = max_file_size, max_total_bytes
        self.max_files, self.max_active, self.max_hosts = max_files, max_active, max_hosts
        self.upload_ttl, self.route_timeout = upload_ttl, route_timeout
        self.max_connections, self.max_routes = max_connections, max_routes
        self._stores, self._uploads = {}, {}
        self._lock = threading.RLock()
        try:
            self._disk_bytes, self._disk_files = self._recover()
        except BaseException:
            self._db.close()
            self._process_lock.close()
            raise
        self._executor = ThreadPoolExecutor(max_workers=io_workers, thread_name_prefix='sma-file-io')
        self._io_slots = asyncio.Semaphore(io_workers * 8)
        self._connections, self._handlers, self._hosts, self._routes = set(), set(), {}, {}
        self._closed, self._sweeper = False, None
        self.ticket_ttl, self.max_tickets, self.http_workers = ticket_ttl, max_tickets, http_workers
        self._tickets = {}
        self._http_server = self._http_thread = None
        self._http_stopping = threading.Event()

    def _directory(self, scope, room):
        return self.root / 'file' / hashlib.sha256(scope.encode()).hexdigest() / hashlib.sha256(room.encode()).hexdigest()

    def _store(self, scope, room):
        pair = (scope, room)
        if pair not in self._stores:
            self._stores[pair] = TransferStore(self._directory(*pair), self.max_file_size, self.max_active)
        return self._stores[pair]

    def _recover(self):
        total, count = 0, 0
        # Do not follow arbitrary paths from a database or recurse through links.
        for scope in self._base.root.iterdir():
            if scope.name == '.supermono-partials':
                continue
            if not _HEX.fullmatch(scope.name):
                raise ValueError('Unexpected broker storage entry')
            _plain_directory(scope)
            for room in scope.iterdir():
                if not _HEX.fullmatch(room.name):
                    raise ValueError('Unexpected broker storage entry')
                _plain_directory(room)
                for path in room.iterdir():
                    if path.name == '.supermono-partials':
                        _plain_directory(path)
                        for partial in path.iterdir():
                            if not re.fullmatch('[a-f0-9]{48}\\.part', partial.name):
                                raise ValueError('Unexpected partial entry')
                            info = partial.lstat()
                            if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                                raise ValueError('Invalid partial entry')
                            partial.unlink()
                        continue
                    _validate_name(path.name)
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                        raise ValueError('Invalid staged file')
                    total += info.st_size
                    count += 1
        for row in self._db.execute("SELECT * FROM jobs WHERE state='staged'").fetchall():
            try:
                path = self._job_path(row)
                valid = path.is_file() and path.stat().st_size == row['size'] and self._hash(path) == row['sha256']
            except (OSError, ValueError):
                valid = False
            if valid:
                self._db.execute("UPDATE jobs SET state='queued' WHERE file_id=?", (row['file_id'],))
            else:
                self._db.execute('DELETE FROM jobs WHERE file_id=?', (row['file_id'],))
        self._db.commit()
        return total, count

    @staticmethod
    def _hash(path):
        with path.open('rb') as source:
            return _stream_sha256(source)

    def _job_path(self, row):
        _validate_name(row['name'])
        directory = self._directory(row['scope'], row['room'])
        for ancestor in (*reversed(directory.parents), directory):
            _plain_directory(ancestor)
        path = directory / row['name']
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            _fail('file_unavailable')
        return path

    async def _io(self, function, *args):
        async with self._io_slots:
            return await asyncio.get_running_loop().run_in_executor(self._executor, function, *args)

    def _authenticate(self, hello):
        if (not isinstance(hello, dict) or hello.get('protocol') != PROTOCOL
                or hello.get('role') not in ('client', 'host')
                or set(hello) - {'id', 'protocol', 'role', 'room', 'token', 'host_id', 'host_secret', 'enrollment'}):
            _fail('authentication_failed')
        room, token = _label(hello.get('room')), hello.get('token')
        if not isinstance(token, str) or not token or len(token) > 8192 or not token.isprintable():
            _fail('authentication_failed')
        digest = hashlib.sha256(token.encode()).hexdigest()
        scope = next((owner for known, owner in self.tokens.items() if hmac.compare_digest(digest, known)), None)
        if scope is None or '*' not in self.owners[scope]['rooms'] and room not in self.owners[scope]['rooms']:
            _fail('authentication_failed')
        if hello['role'] == 'client':
            if any(name in hello for name in ('host_id', 'host_secret', 'enrollment')):
                _fail('authentication_failed')
        else:
            self._pin(scope, room, hello)
        return scope, room, hello['role']

    def _pin(self, scope, room, hello):
        host_id, secret = _id(hello.get('host_id')), hello.get('host_secret')
        if (not isinstance(secret, str) or not 32 <= len(secret) <= 256
                or any(not 33 <= ord(char) <= 126 for char in secret)):
            _fail('authentication_failed')
        digest = hashlib.sha256(secret.encode()).hexdigest()
        with self._lock, self._db:
            pinned = self._db.execute('SELECT * FROM hosts WHERE scope=? AND room=?', (scope, room)).fetchone()
            if pinned and (pinned['host_id'] != host_id or not hmac.compare_digest(pinned['secret'], digest)):
                _fail('authentication_failed')
            enrollment = hello.get('enrollment')
            if pinned is None or enrollment is not None:
                if (not isinstance(enrollment, dict) or set(enrollment) != {'timestamp', 'nonce', 'proof'}
                        or type(enrollment['timestamp']) is not int
                        or abs(self.clock() - enrollment['timestamp']) > 300
                        or not isinstance(enrollment['nonce'], str)
                        or not re.fullmatch('[A-Za-z0-9_-]{16,128}', enrollment['nonce'])
                        or not isinstance(enrollment['proof'], str) or not _HEX.fullmatch(enrollment['proof'])):
                    _fail('authentication_failed')
                expected = enrollment_proof(self.owners[scope]['key'], room, host_id, secret,
                                            enrollment['timestamp'], enrollment['nonce'])
                if not hmac.compare_digest(expected, enrollment['proof']):
                    _fail('authentication_failed')
                self._db.execute('DELETE FROM nonces WHERE created<?', (int(self.clock()) - 600,))
                if self._db.execute('SELECT COUNT(*) FROM nonces').fetchone()[0] >= self.max_hosts * 4:
                    _fail('authentication_failed')
                try:
                    self._db.execute('INSERT INTO nonces VALUES(?,?,?)', (scope, enrollment['nonce'], enrollment['timestamp']))
                except sqlite3.IntegrityError:
                    _fail('authentication_failed')
            if pinned is None:
                if self._db.execute('SELECT COUNT(*) FROM hosts').fetchone()[0] >= self.max_hosts:
                    _fail('authentication_failed')
                self._db.execute('INSERT INTO hosts VALUES(?,?,?,?)', (scope, room, host_id, digest))

    async def _send(self, connection, message):
        raw = json.dumps(message, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        if len(raw.encode()) > MAX_FRAME:
            _fail('response_too_large')
        async with connection.send_lock:
            await asyncio.wait_for(connection.ws.send(raw), 2)

    async def handle_socket(self, ws):
        """websockets server handler; authentication never uses URL query tokens."""
        current = asyncio.current_task()
        connection = _Connection(ws)
        if self._closed or len(self._connections) >= self.max_connections:
            await ws.close(1013, 'file_service_busy')
            return
        self._connections.add(connection)
        self._handlers.add(current)
        try:
            request = getattr(ws, 'request', None)
            if request is not None and ('?' in request.path or request.path not in ('/file/ws', '/')):
                _fail('authentication_failed')
            hello = _json(await asyncio.wait_for(ws.recv(), 2))
            identity = _id(hello.get('id', 'hello')) if isinstance(hello, dict) else 'hello'
            try:
                connection.scope, connection.room, connection.role = await self._io(self._authenticate, hello)
                connection.token_digest = hashlib.sha256(hello['token'].encode()).hexdigest()
            except (ValueError, OSError, sqlite3.Error):
                _fail('authentication_failed')
            pair = (connection.scope, connection.room)
            if connection.role == 'host':
                await self._io(self._register_host, connection)
            await self._send(connection, {'type': 'ready', 'id': identity, 'role': connection.role,
                                         'room': connection.room, 'agent_online': pair in self._hosts})
            if self._sweeper is None:
                self._sweeper = asyncio.create_task(self._sweep())
            if connection.role == 'host':
                connection.replay = asyncio.create_task(self._replay(connection))
            async for raw in ws:
                try:
                    body = _json(raw)
                    if isinstance(body, dict) and body.get('type') == 'host_response':
                        self._host_response(connection, body)
                        continue
                    request_id = _id(body.get('id')) if isinstance(body, dict) else ''
                    if (not isinstance(body, dict) or set(body) != {'type', 'id', 'action', 'session', 'data'}
                            or body['type'] != 'request' or not isinstance(body['data'], dict)):
                        _fail()
                    session = _label(body['session'], 256)
                    try:
                        result = await self._dispatch(connection, body['action'], session, body['data'])
                        response = {'type': 'response', 'id': request_id, 'ok': True, 'result': result}
                    except BrokerError as error:
                        response = {'type': 'response', 'id': request_id, 'ok': False, 'error': str(error)}
                    except (OSError, ValueError, sqlite3.Error, UnicodeError):
                        response = {'type': 'response', 'id': request_id, 'ok': False, 'error': 'invalid_transfer'}
                    await self._send(connection, response)
                except (ValueError, TypeError, KeyError, RecursionError):
                    await ws.close(1008, 'invalid_request')
                    break
        except (ConnectionClosed, TimeoutError, ValueError, OSError):
            try:
                await ws.close(1008, 'authentication_failed' if not connection.scope else 'file_service_unavailable')
            except ConnectionClosed:
                pass
        finally:
            if connection.replay:
                connection.replay.cancel()
                await asyncio.gather(connection.replay, return_exceptions=True)
            pair = (connection.scope, connection.room)
            await self._io(self._detach_host, connection)
            for route_id, route in list(self._routes.items()):
                if connection in route[:2]:
                    self._routes.pop(route_id, None)
                    if not route[2].done():
                        route[2].set_exception(BrokerError('host_unavailable'))
            await self._io(self._cancel_owner, connection.identity)
            self._connections.discard(connection)
            self._handlers.discard(current)

    def _register_host(self, connection):
        with self._lock:
            pair = (connection.scope, connection.room)
            if pair in self._hosts:
                _fail('authentication_failed')
            self._hosts[pair] = connection

    def _detach_host(self, connection):
        with self._lock:
            pair = (connection.scope, connection.room)
            if self._hosts.get(pair) is connection:
                self._hosts.pop(pair, None)
            self._tickets = {key: ticket for key, ticket in self._tickets.items()
                             if ticket['host_identity'] != connection.identity}

    async def _route(self, caller, action, session, data):
        host = self._hosts.get((caller.scope, caller.room))
        if host is None:
            _fail('host_unavailable')
        if len(self._routes) >= self.max_routes:
            _fail('service_busy')
        route_id = secrets.token_hex(16)
        future = asyncio.get_running_loop().create_future()
        self._routes[route_id] = (host, caller, future)
        closed = asyncio.create_task(caller.ws.wait_closed())
        try:
            try:
                await self._send(host, {'type': 'host_request', 'id': route_id, 'action': action,
                                        'session': session, 'data': data})
            except (ConnectionClosed, TimeoutError):
                _fail('host_unavailable')
            done, _ = await asyncio.wait([future, closed], timeout=self.route_timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
            if future in done:
                return future.result()
            _fail('route_timeout' if not done else 'caller_disconnected')
        finally:
            self._routes.pop(route_id, None)
            if not future.done():
                future.cancel()
            closed.cancel()
            await asyncio.gather(closed, return_exceptions=True)

    def _host_response(self, connection, body):
        route_id = body.get('id')
        route = self._routes.get(route_id) if isinstance(route_id, str) else None
        if (connection.role != 'host' or route is None or route[0] is not connection
                or self._hosts.get((connection.scope, connection.room)) is not connection):
            return
        future = route[2]
        if future.done():
            return
        if body.get('ok') is True and isinstance(body.get('result'), dict):
            future.set_result(body['result'])
        else:
            error = body.get('error')
            code = error if isinstance(error, str) and error in _HOST_ERRORS else 'host_rejected'
            future.set_exception(BrokerError(code))

    async def _dispatch(self, connection, action, session, data):
        if not isinstance(action, str):
            _fail()
        if connection.role == 'client':
            if action in _ROUTED:
                return await self._route(connection, action, session, data)
            if action == 'upload.begin':
                if not isinstance(data.get('project_binding'), str) or not _HEX.fullmatch(data['project_binding']):
                    _fail('project_binding_required')
                if set(data) != {'original_name', 'size', 'sha256', 'window_id', 'daemon_epoch',
                                  'workspace_revision', 'timezone_offset_minutes', 'project_binding'}:
                    _fail()
                _label(data['window_id'], 64)
                _label(data['daemon_epoch'], 128)
                _integer(data['workspace_revision'])
                _integer(data['size'], 0, self.max_file_size)
                _integer(data['timezone_offset_minutes'], -840, 840)
                if not isinstance(data['sha256'], str) or not _HEX.fullmatch(data['sha256']):
                    _fail()
                target = await self._route(connection, 'workspace.get', session, {})
                if (target.get('window_id') != data['window_id'] or target.get('session_id') != session
                        or target.get('daemon_epoch') != data['daemon_epoch']
                        or type(target.get('revision')) is not int or target['revision'] != data['workspace_revision']):
                    _fail('target_changed')
                if target.get('project_status') == 'selection_required':
                    _fail('project_required')
                if not isinstance(target.get('project_binding'), str) or not _HEX.fullmatch(target['project_binding']):
                    _fail('project_binding_required')
                if not hmac.compare_digest(target['project_binding'], data['project_binding']):
                    _fail('project_binding_changed')
                return await self._io(self._begin, connection, session, data)
            if action in {'upload.chunk', 'upload.finish', 'upload.cancel'}:
                result = await self._io(self._upload, connection, action, session, data)
                if action == 'upload.finish':
                    host = self._hosts.get((connection.scope, connection.room))
                    if host:
                        row = await self._io(self._job, connection, session, result['file_id'])
                        await self._delivery(host, row)
                return result
            if action == 'file.status' and set(data) == {'file_id'}:
                row = await self._io(self._job, connection, session, data['file_id'])
                result = {'file_id': row['file_id'], 'name': row['name'], 'state': row['state']}
                if row['state'] == 'saved':
                    result['receipt'] = json.loads(row['receipt'])
                elif row['state'] == 'failed':
                    result['error'] = row['error']
                return result
        elif connection.role == 'host' and action in {'download.chunk', 'download.ticket', 'delivery.ack', 'delivery.fail'}:
            return await self._io(self._host_file, connection, action, session, data)
        _fail('action_forbidden')

    def _begin(self, connection, session, data):
        with self._lock:
            self._expire()
            reserved = sum(upload['size'] for upload in self._uploads.values())
            if (len(self._uploads) >= self.max_active or self._disk_files + len(self._uploads) >= self.max_files
                    or self._disk_bytes + reserved + data['size'] > self.max_total_bytes):
                _fail('quota_exceeded')
            stamp = datetime.fromtimestamp(self.clock(), timezone(timedelta(minutes=data['timezone_offset_minutes'])))
            name = timestamped_name(data['original_name'], stamp)
            store = self._store(connection.scope, connection.room)
            names = {path.name.casefold() for path in store.root.iterdir()}
            names.update(upload['name'].casefold() for upload in self._uploads.values()
                         if upload['scope'] == connection.scope and upload['room'] == connection.room)
            base = Path(name)
            for number in range(10000):
                candidate = name if number == 0 else f'{base.stem} ({number}){base.suffix}'
                if candidate.casefold() not in names:
                    name = candidate
                    break
            else:
                _fail('quota_exceeded')
            transfer = store.begin(connection.identity, session, name, data['size'], data['sha256'])
            self._uploads[transfer] = {**data, 'scope': connection.scope, 'room': connection.room,
                'owner': connection.identity, 'session': session, 'name': name, 'touched': self.clock(), 'store': store}
            return {'upload_id': transfer, 'name': name}

    def _upload(self, connection, action, session, data):
        expected = {'upload_id', 'offset', 'data'} if action == 'upload.chunk' else {'upload_id'}
        if set(data) != expected:
            _fail()
        with self._lock:
            self._expire()
            upload_id = data['upload_id']
            upload = self._uploads.get(upload_id) if isinstance(upload_id, str) else None
            if (upload is None or upload['owner'] != connection.identity or upload['session'] != session
                    or upload['scope'] != connection.scope or upload['room'] != connection.room):
                _fail('unknown_upload')
            store = upload['store']
            if action == 'upload.cancel':
                self._discard(upload_id)
                return {'cancelled': True}
            if action == 'upload.chunk':
                encoded = data['data']
                if not isinstance(encoded, str) or len(encoded) > 131072:
                    _fail()
                chunk = base64.b64decode(encoded, validate=True)
                received = store.append(connection.identity, upload_id, data['offset'], chunk)
                upload['touched'] = self.clock()
                return {'received': received}
            file_id = secrets.token_hex(24)
            try:
                with self._db:
                    self._db.execute('INSERT INTO jobs(file_id,scope,room,session,window_id,daemon_epoch,revision,name,size,sha256,state,receipt,error,project_binding) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (file_id, upload['scope'], upload['room'], session, upload['window_id'], upload['daemon_epoch'],
                         upload['workspace_revision'], upload['name'], upload['size'], upload['sha256'], 'staged', None, None, upload['project_binding']))
                path = store.finish(connection.identity, upload_id)
                self._disk_files += 1
                self._disk_bytes += upload['size']
                if Path(path).name != upload['name']:
                    _fail('storage_changed')
                with self._db:
                    self._db.execute("UPDATE jobs SET state='queued' WHERE file_id=?", (file_id,))
                return {'file_id': file_id, 'name': upload['name'], 'state': 'queued'}
            except BaseException:
                with self._db:
                    self._db.execute("DELETE FROM jobs WHERE file_id=? AND state='staged'", (file_id,))
                raise
            finally:
                self._discard(upload_id)

    def _job(self, connection, session, file_id):
        if not isinstance(file_id, str) or not re.fullmatch('[a-f0-9]{48}', file_id):
            _fail('unknown_file')
        with self._lock:
            row = self._db.execute('SELECT * FROM jobs WHERE file_id=? AND scope=? AND room=? AND session=?',
                (file_id, connection.scope, connection.room, session)).fetchone()
            if row is None:
                _fail('unknown_file')
            return dict(row)

    def _host_file(self, connection, action, session, data):
        with self._lock:
            row = self._job(connection, session, data.get('file_id'))
            if not row['project_binding']:
                _fail('project_binding_required')
            if action == 'download.ticket':
                if set(data) != {'file_id'}:
                    _fail()
                return self._new_ticket(connection, row)
            if action == 'download.chunk':
                if set(data) != {'file_id', 'offset'} or row['state'] not in {'queued', 'downloading', 'saved'}:
                    _fail()
                offset = _integer(data['offset'], 0, row['size'])
                path = self._job_path(row)
                if path.stat().st_size != row['size']:
                    _fail('file_unavailable')
                with path.open('rb') as source:
                    source.seek(offset)
                    chunk = source.read(MAX_CHUNK)
                if row['state'] != 'saved':
                    with self._db:
                        self._db.execute("UPDATE jobs SET state='downloading' WHERE file_id=?", (row['file_id'],))
                return {'data': base64.b64encode(chunk).decode(), 'next': offset + len(chunk), 'total': row['size']}
            if action == 'delivery.fail':
                if set(data) != {'file_id', 'error'} or row['state'] == 'saved':
                    _fail()
                code = data['error']
                if not isinstance(code, str) or not re.fullmatch('[a-z_]{1,64}', code):
                    _fail()
                with self._db:
                    self._db.execute("UPDATE jobs SET state='failed',error=? WHERE file_id=?", (code, row['file_id']))
                return {'file_id': row['file_id'], 'state': 'failed'}
            if set(data) != {'file_id', 'name', 'path', 'size', 'sha256', 'daemon_epoch', 'workspace_revision', 'project_binding'}:
                _fail()
            if (data['name'] != row['name'] or type(data['size']) is not int or data['size'] != row['size']
                    or data['sha256'] != row['sha256'] or data['daemon_epoch'] != row['daemon_epoch']
                    or type(data['workspace_revision']) is not int or data['workspace_revision'] != row['revision']
                    or data['project_binding'] != row['project_binding']):
                _fail('receipt_mismatch')
            path = _label(data['path'], 4096)
            if '://' in path or path.replace('\\', '/').rsplit('/', 1)[-1] != row['name']:
                _fail('receipt_mismatch')
            receipt = json.dumps(data, ensure_ascii=False, sort_keys=True)
            if row['state'] == 'failed' or row['state'] == 'saved' and row['receipt'] != receipt:
                _fail('receipt_mismatch')
            with self._db:
                self._db.execute("UPDATE jobs SET state='saved',receipt=? WHERE file_id=?", (receipt, row['file_id']))
            return {'file_id': row['file_id'], 'state': 'saved', 'receipt': data}

    def _new_ticket(self, connection, row):
        if self._http_server is None or self._http_stopping.is_set():
            _fail('http_unavailable')
        self._tickets = {key: value for key, value in self._tickets.items()
                         if self.clock() < value['expires_at'] and time.monotonic() < value['deadline']}
        if len(self._tickets) >= self.max_tickets:
            _fail('quota_exceeded')
        pinned = self._db.execute('SELECT * FROM hosts WHERE scope=? AND room=?',
                                  (connection.scope, connection.room)).fetchone()
        if pinned is None:
            _fail('unauthorized')
        ticket = {key: row[key] for key in ('file_id', 'scope', 'room', 'session', 'daemon_epoch',
                                           'revision', 'size', 'sha256', 'name', 'project_binding')}
        ticket.update(host_identity=connection.identity, token_digest=connection.token_digest,
                      host_id=pinned['host_id'], host_secret_hash=pinned['secret'],
                      expires_at=self.clock() + self.ticket_ttl, deadline=time.monotonic() + self.ticket_ttl)
        self._ticket_job(ticket, row['file_id'])
        path = self._job_path(row)
        # Validate immutable stage bytes once before authorizing a new stream.
        with path.open('rb') as source:
            before = os.fstat(source.fileno())
            digest = _stream_sha256(source)
            info = os.fstat(source.fileno())
        if (digest != row['sha256'] or before.st_size != row['size']
                or before.st_mtime_ns != info.st_mtime_ns or before.st_size != info.st_size):
            _fail('file_unavailable')
        ticket['identity'] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        raw = secrets.token_urlsafe(32)
        self._tickets[hashlib.sha256(raw.encode()).hexdigest()] = ticket
        return {'download_path': '/file/download/' + row['file_id'], 'ticket': raw,
                'expires_at': ticket['expires_at'], 'size': row['size'], 'sha256': row['sha256'], 'name': row['name']}

    def _ticket_job(self, ticket, file_id):
        if (self._closed or self._http_stopping.is_set() or ticket['file_id'] != file_id
                or self.clock() >= ticket['expires_at'] or time.monotonic() >= ticket['deadline']):
            _fail('unauthorized')
        host = self._hosts.get((ticket['scope'], ticket['room']))
        if host is None or host.identity != ticket['host_identity'] or host.ws.state is not State.OPEN:
            _fail('unauthorized')
        owners, tokens = _auth(self._auth_file)
        scope = tokens.get(ticket['token_digest'])
        if (scope != ticket['scope'] or scope not in owners
                or '*' not in owners[scope]['rooms'] and ticket['room'] not in owners[scope]['rooms']):
            _fail('unauthorized')
        pinned = self._db.execute('SELECT * FROM hosts WHERE scope=? AND room=?',
                                  (ticket['scope'], ticket['room'])).fetchone()
        if (pinned is None or pinned['host_id'] != ticket['host_id']
                or not hmac.compare_digest(pinned['secret'], ticket['host_secret_hash'])):
            _fail('unauthorized')
        row = self._db.execute('SELECT * FROM jobs WHERE file_id=?', (file_id,)).fetchone()
        if row is None or row['state'] not in {'queued', 'downloading', 'saved'}:
            _fail('unauthorized')
        if any(row[key] != ticket[key] for key in ('scope', 'room', 'session', 'daemon_epoch',
                                                   'revision', 'size', 'sha256', 'name', 'project_binding')):
            _fail('unauthorized')
        return row

    def _http_authorize(self, raw, file_id):
        if not isinstance(raw, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', raw):
            _fail('unauthorized')
        with self._lock:
            ticket = self._tickets.get(hashlib.sha256(raw.encode()).hexdigest())
            if ticket is None:
                _fail('unauthorized')
            row = self._ticket_job(ticket, file_id)
            path = self._job_path(row)
            info = path.stat()
            if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != ticket['identity']:
                _fail('unauthorized')
            return ticket, path

    @staticmethod
    def _range(value, size):
        if value is None:
            return 0, size - 1, False
        if not isinstance(value, str) or len(value) > 128:
            _fail('invalid_range')
        match = re.fullmatch(r'bytes=([0-9]*)-([0-9]*)', value)
        if not match or not any(match.groups()) or size == 0:
            _fail('invalid_range')
        left, right = match.groups()
        if left:
            start, end = int(left), int(right) if right else size - 1
            if start >= size or end < start:
                _fail('invalid_range')
            return start, min(end, size - 1), True
        suffix = int(right)
        if suffix == 0:
            _fail('invalid_range')
        return max(0, size - suffix), size - 1, True

    def start_http(self, host='127.0.0.1', port=0):
        """Start a private loopback HTTP listener; return ``(server, address)``.

        The public proxy exposes only /file/download/<id> over HTTPS. Tickets
        never appear in paths, queries, redirects, cookies, or access logs.
        """
        if host not in {'127.0.0.1', 'localhost', '::1'} or type(port) is not int or not 0 <= port <= 65535:
            raise ValueError('File HTTP listener must use loopback and a valid port')
        if self._closed or self._http_server is not None:
            raise ValueError('File HTTP listener is already started or closed')
        broker = self

        class HTTP(ThreadingHTTPServer):
            address_family = socket.AF_INET6 if host == '::1' else socket.AF_INET
            daemon_threads = False
            block_on_close = True
            allow_reuse_address = True

            def __init__(self):
                self.slots = threading.BoundedSemaphore(broker.http_workers)
                self.sockets, self.guard = set(), threading.Lock()
                super().__init__((host, port), Handler)

            def process_request(self, request, address):
                if not self.slots.acquire(blocking=False):
                    self.shutdown_request(request)
                    return
                with self.guard:
                    self.sockets.add(request)
                try:
                    super().process_request(request, address)
                except BaseException:
                    with self.guard:
                        self.sockets.discard(request)
                    self.slots.release()
                    raise

            def process_request_thread(self, request, address):
                try:
                    super().process_request_thread(request, address)
                finally:
                    with self.guard:
                        self.sockets.discard(request)
                    self.slots.release()

            def handle_error(self, request, address):
                pass  # Never print URL/header credentials or exception bodies.

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.0'

            def setup(self):
                self.request.settimeout(2)
                super().setup()

            def log_message(self, *args):
                pass

            def send_error(self, code, message=None, explain=None):
                self.deny(code)  # No reflected request lines or exception text.

            def deny(self, status, size=None):
                self.send_response(status)
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', '0')
                if status == 401:
                    self.send_header('WWW-Authenticate', 'Bearer')
                if status == 416 and size is not None:
                    self.send_header('Content-Range', 'bytes */' + str(size))
                self.end_headers()

            def do_HEAD(self):
                self.deny(405)

            do_POST = do_HEAD
            do_PUT = do_HEAD
            do_DELETE = do_HEAD

            def do_GET(self):
                match = re.fullmatch(r'/file/download/([a-f0-9]{48})', self.path)
                header = self.headers.get_all('Authorization', [])
                if not match or len(header) != 1 or not header[0].startswith('Bearer '):
                    self.deny(401)
                    return
                try:
                    ticket, path = broker._http_authorize(header[0][7:], match[1])
                except (ValueError, OSError, sqlite3.Error):
                    self.deny(401)
                    return
                ranges = self.headers.get_all('Range', [])
                try:
                    if len(ranges) > 1:
                        _fail('invalid_range')
                    start, end, partial = broker._range(ranges[0] if ranges else None, ticket['size'])
                except ValueError:
                    self.deny(416, ticket['size'])
                    return
                try:
                    with path.open('rb') as source:
                        info = os.fstat(source.fileno())
                        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != ticket['identity']:
                            self.deny(401)
                            return
                        with broker._lock, broker._db:
                            broker._ticket_job(ticket, match[1])
                            broker._db.execute("UPDATE jobs SET state='downloading' WHERE file_id=? AND state='queued'", (match[1],))
                        self.send_response(206 if partial else 200)
                        self.send_header('Cache-Control', 'no-store')
                        self.send_header('Accept-Ranges', 'bytes')
                        self.send_header('Content-Type', 'application/octet-stream')
                        self.send_header('X-Content-Type-Options', 'nosniff')
                        self.send_header('X-Content-SHA256', ticket['sha256'])
                        self.send_header('ETag', '"' + ticket['sha256'] + '"')
                        self.send_header('Content-Length', str(max(0, end - start + 1)))
                        if partial:
                            self.send_header('Content-Range', f'bytes {start}-{end}/{ticket["size"]}')
                        self.end_headers()
                        source.seek(start)
                        remaining = max(0, end - start + 1)
                        while remaining:
                            with broker._lock:
                                broker._ticket_job(ticket, match[1])
                            chunk = source.read(min(65536, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                except (OSError, ValueError, sqlite3.Error):
                    self.close_connection = True

        server = HTTP()
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1},
                                  name='sma-file-http', daemon=True)
        self._http_server, self._http_thread = server, thread
        thread.start()
        return server, server.server_address

    def _stop_http(self):
        self._http_stopping.set()
        server = self._http_server
        if server is None:
            return
        server.shutdown()
        with server.guard:
            sockets = list(server.sockets)
        for connection in sockets:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                # Windows makefile() retains the FD after close(); detach the
                # owned socket so an incomplete header read is interrupted too.
                socket.close(connection.detach())
            except OSError:
                pass
        server.server_close()
        self._http_thread.join(3)

    def _discard(self, upload_id):
        upload = self._uploads.pop(upload_id, None)
        if upload:
            try:
                upload['store'].cancel(upload['owner'], upload_id)
            except (ValueError, OSError):
                pass

    def _cancel_owner(self, owner):
        with self._lock:
            for upload_id, upload in list(self._uploads.items()):
                if upload['owner'] == owner:
                    self._discard(upload_id)

    def _expire(self):
        for upload_id, upload in list(self._uploads.items()):
            if self.clock() - upload['touched'] >= self.upload_ttl:
                self._discard(upload_id)

    def _expire_locked(self):
        with self._lock:
            self._expire()

    async def _sweep(self):
        try:
            while True:
                await asyncio.sleep(min(5, self.upload_ttl / 2))
                await self._io(self._expire_locked)
        except asyncio.CancelledError:
            pass

    async def _delivery(self, host, row):
        if self._hosts.get((host.scope, host.room)) is not host:
            return
        job = {key: row[key] for key in ('file_id', 'name', 'size', 'sha256', 'window_id', 'daemon_epoch')}
        job.update(session_id=row['session'], workspace_revision=row['revision'], project_binding=row['project_binding'])
        try:
            await self._send(host, {'type': 'delivery', 'id': row['file_id'], 'job': job})
        except (ConnectionClosed, TimeoutError):
            pass

    def _queued(self, scope, room):
        with self._lock:
            return [dict(row) for row in self._db.execute(
                "SELECT * FROM jobs WHERE scope=? AND room=? AND state IN ('queued','downloading') ORDER BY rowid",
                (scope, room))]

    async def _replay(self, host):
        try:
            for row in await self._io(self._queued, host.scope, host.room):
                await self._delivery(host, row)
        except (ConnectionClosed, TimeoutError, asyncio.CancelledError):
            pass

    async def close(self):
        """Close owned connections and flush the registry; never touches terminals."""
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(self._stop_http)
        if self._sweeper:
            self._sweeper.cancel()
            await asyncio.gather(self._sweeper, return_exceptions=True)
        await asyncio.gather(*(connection.ws.close(1001, 'file_service_stopping')
                               for connection in list(self._connections)), return_exceptions=True)
        await asyncio.gather(*(task for task in list(self._handlers) if task is not asyncio.current_task()),
                             return_exceptions=True)
        await self._io(self._db.close)
        await self._io(self._process_lock.close)
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)


def main():
    parser = argparse.ArgumentParser(description='Independent authenticated file WSS broker')
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--auth-file', required=True, type=Path)
    parser.add_argument('--registry-file', type=Path)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8819)
    parser.add_argument('--cert', type=Path)
    parser.add_argument('--key', type=Path)
    parser.add_argument('--max-file-size', type=int, default=512 * 1024**2)
    parser.add_argument('--max-total-bytes', type=int, default=2 * 1024**3)
    parser.add_argument('--max-files', type=int, default=4096)
    parser.add_argument('--max-active', type=int, default=16)
    parser.add_argument('--upload-ttl', type=float, default=300)
    parser.add_argument('--route-timeout', type=float, default=15)
    parser.add_argument('--http-host', default='127.0.0.1')
    parser.add_argument('--http-port', type=int, default=8820)
    parser.add_argument('--ticket-ttl', type=float, default=60)
    parser.add_argument('--http-workers', type=int, default=8)
    args = parser.parse_args()
    if bool(args.cert) != bool(args.key):
        parser.error('--cert and --key must be supplied together')
    if not args.cert and args.host not in {'127.0.0.1', '::1', 'localhost'}:
        parser.error('A public listener requires TLS')
    context = None
    if args.cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.cert, args.key)

    async def run():
        broker = Broker(args.root, args.auth_file, args.registry_file,
            max_file_size=args.max_file_size, max_total_bytes=args.max_total_bytes,
            max_files=args.max_files, max_active=args.max_active,
            upload_ttl=args.upload_ttl, route_timeout=args.route_timeout,
            ticket_ttl=args.ticket_ttl, http_workers=args.http_workers)
        try:
            broker.start_http(args.http_host, args.http_port)
            async with serve(broker.handle_socket, args.host, args.port, ssl=context,
                             max_size=MAX_FRAME, max_queue=8, ping_interval=20, ping_timeout=20):
                await asyncio.Future()
        finally:
            await broker.close()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
