"""Independent host file helper; never writes, resizes or restarts a terminal.

Only HELLO/PING are sent to the configured loopback parent. Files arrive through
the enrolled broker protocol, never through download URLs or terminal commands.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import hashlib
import hmac
import http.client
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import stat
import threading
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from .file_broker import enrollment_proof, PROTOCOL, FILE_PROTOCOL, FILE_PROTOCOL_QUERY, MAX_FRAME
from .file_transfer import TransferStore, MAX_CHUNK, _plain_directory, _validate_name
from .file_workspaces import FileWorkspaces, ProjectSelectionRequired, RevisionConflict, _directory, directory_binding
from .protocol import pack, unpack, pack_json

_TRANSPORT_LOG = logging.getLogger('supermonoagent.file_companion.private_transport')
_TRANSPORT_LOG.setLevel(logging.CRITICAL + 1)
_TRANSPORT_LOG.propagate = False
_TRANSPORT_LOG.addHandler(logging.NullHandler())


class CompanionError(ValueError):
    """A value-free, public error code."""


class _DirectConnect(connect):
    def process_redirect(self, exc):
        return exc


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def _json(raw):
    if not isinstance(raw, str) or len(raw.encode('utf8')) > MAX_FRAME:
        raise CompanionError('invalid_frame')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise CompanionError('invalid_frame')
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(CompanionError('invalid_frame')))


def _endpoint(value, *, backend=False):
    try:
        if not isinstance(value, str) or any(ord(c) < 33 or ord(c) == 127 for c in value):
            raise ValueError()
        parsed = urlsplit(value)
        port = parsed.port or (443 if parsed.scheme == 'wss' and not backend else None)
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or not port or not 1 <= port <= 65535):
            raise ValueError()
        loopback = parsed.hostname in {'127.0.0.1', '::1', 'localhost'}
        if backend:
            if parsed.scheme != 'http' or not loopback or parsed.path not in {'', '/'}:
                raise ValueError()
            netloc = f'127.0.0.1:{port}' if parsed.hostname == 'localhost' else parsed.netloc
            return urlunsplit(('http', netloc, '', '', ''))
        if parsed.scheme not in {'ws', 'wss'} or parsed.scheme == 'ws' and not loopback or parsed.path != '/file/ws':
            raise ValueError()
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))
    except (ValueError, TypeError, AttributeError):
        raise CompanionError('invalid_endpoint') from None


def _secret(path, *, maximum=8192):
    value = Path(path)
    _directory(value.parent, required=True)
    info = value.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise CompanionError('invalid_secret_file')
    with value.open('rb') as stream:
        data = stream.read(maximum * 4 + 4)
    try:
        text = data.decode('utf-8-sig').strip()
        if not 1 <= len(text) <= maximum or any(ord(c) < 33 or ord(c) == 127 for c in text):
            raise ValueError()
        return text
    except (ValueError, UnicodeError):
        raise CompanionError('invalid_secret_file') from None


def _present(path):
    """Never follow a preexisting file link merely to test its presence."""
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _process_birth(pid):
    if type(pid) is not int or pid <= 0:
        raise CompanionError('parent_identity_unavailable')
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            raise CompanionError('parent_identity_unavailable')
        try:
            values = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *[ctypes.byref(value) for value in values]):
                raise CompanionError('parent_identity_unavailable')
            return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime
        finally:
            kernel.CloseHandle(handle)
    try:
        # Linux test/runtime support; PID start ticks survive helper restarts.
        return int(Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        raise CompanionError('parent_identity_unavailable') from None


def _health(endpoint):
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    with opener.open(endpoint + '/health', timeout=2) as response:
        data = response.read(16385)
    if len(data) > 16384:
        raise CompanionError('invalid_health')
    body = json.loads(data)
    if not isinstance(body, dict) or body.get('ok') is not True:
        raise CompanionError('invalid_health')
    return body


class _IO:
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='sma-companion-fs')
        self.slots = asyncio.Semaphore(16)

    async def __call__(self, function, *args, **kwargs):
        async with self.slots:
            return await asyncio.get_running_loop().run_in_executor(self.pool, partial(function, *args, **kwargs))

    async def close(self):
        await asyncio.to_thread(self.pool.shutdown, wait=True, cancel_futures=True)


class LegacyInspector:
    """Read-only multiplex catalog, authenticated independently of HELLO success."""
    def __init__(self, backend, owner_token_file, *, io=None, refresh_interval=.5):
        self.backend = _endpoint(backend, backend=True)
        self.owner_token_file = Path(owner_token_file)
        self.io = io or _IO()
        self._own_io = io is None
        self.refresh_interval = max(.05, min(5, refresh_interval))
        self.valid = threading.Event()
        self._lock = threading.RLock()
        self._catalog, self._epoch, self._version = {}, '', 0
        self._ws = None
        self._closed = False

    def _invalidate(self):
        self.valid.clear()
        with self._lock:
            self._version += 1

    async def _decode(self, body, token):
        capabilities = body.get('capabilities') if isinstance(body, dict) else None
        if not isinstance(capabilities, dict) or capabilities.get('multiplex') is not True:
            raise CompanionError('multiplex_required')
        service = body.get('service', {})
        if not isinstance(service, dict):
            raise CompanionError('owner_unverified')
        health = None
        if 'owner_authenticated' in service:
            if service['owner_authenticated'] is not True:
                raise CompanionError('owner_unverified')
        else:
            try:
                health = await self.io(_health, self.backend)
            except (OSError, ValueError):
                health = {}
            if 'default_token' in health:
                owner = health['default_token']
                if not isinstance(owner, str) or not 0 < len(owner) <= 4096 or not hmac.compare_digest(owner.encode(), token.encode()):
                    raise CompanionError('owner_unverified')
            else:
                owner = await self.io(_secret, self.owner_token_file, maximum=4096)
                if not hmac.compare_digest(owner.encode(), token.encode()):
                    raise CompanionError('owner_unverified')
        pid = service.get('pid')
        if pid is None:
            if health is None:
                health = await self.io(_health, self.backend)
            pid = health.get('pid')
        birth = await self.io(_process_birth, pid)
        epoch = hashlib.sha256(json.dumps([self.backend, pid, birth], separators=(',', ':')).encode()).hexdigest()
        windows = body.get('windows')
        if not isinstance(windows, list) or len(windows) > 256:
            raise CompanionError('invalid_catalog')
        catalog = {}
        for window in windows:
            wid = window.get('id') if isinstance(window, dict) else None
            sessions = window.get('sessions') if isinstance(window, dict) else None
            if not isinstance(wid, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', wid) or not isinstance(sessions, list):
                raise CompanionError('invalid_catalog')
            for session in sessions:
                sid = session.get('id') if isinstance(session, dict) else None
                cwd = session.get('cwd') if isinstance(session, dict) else None
                if not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', sid) or sid in catalog or len(catalog) >= 1024:
                    raise CompanionError('invalid_catalog')
                canonical, _ = await self.io(_directory, cwd)
                catalog[sid] = {'window_id': wid, 'session_id': sid, 'cwd': canonical, 'daemon_epoch': epoch}
        return epoch, catalog

    def _apply(self, epoch, catalog):
        with self._lock:
            if epoch != self._epoch or catalog != self._catalog:
                self._version += 1
            self._epoch, self._catalog = epoch, catalog
            self.valid.set()

    def snapshot(self, session):
        with self._lock:
            if not self.valid.is_set() or session not in self._catalog:
                raise CompanionError('target_unavailable')
            return {**self._catalog[session], 'catalog_version': self._version}

    def current(self, snapshot):
        try:
            return self.snapshot(snapshot['session_id']) == snapshot
        except (ValueError, KeyError):
            return False

    async def _open(self):
        token = await self.io(_secret, self.owner_token_file, maximum=4096)
        parsed = urlsplit(self.backend)
        url = urlunsplit(('ws', parsed.netloc, '/ws', '', ''))
        ws = await _DirectConnect(url, proxy=None, ping_interval=None, open_timeout=2, close_timeout=1, max_size=MAX_FRAME, max_queue=8, logger=_TRANSPORT_LOG)
        try:
            await ws.send(pack_json(1, {'token': token, 'multiplex': True}))
            while True:
                raw = await asyncio.wait_for(ws.recv(), 2)
                if not isinstance(raw, bytes):
                    raise CompanionError('invalid_parent_frame')
                op, payload = unpack(raw)
                if op == 2:
                    return ws, token, await self._decode(_json(payload.decode('utf8')), token)
        except BaseException:
            await ws.close()
            raise

    async def fresh(self, session):
        """New HELLO immediately before publication; no cached owner proof."""
        before = self.snapshot(session)
        try:
            ws, _, (epoch, catalog) = await self._open()
        except (OSError, ValueError, TimeoutError, WebSocketException):
            self._invalidate()
            raise CompanionError('owner_unverified') from None
        try:
            if catalog.get(session) != {key: before[key] for key in ('window_id', 'session_id', 'cwd', 'daemon_epoch')} or epoch != before['daemon_epoch'] or not self.current(before):
                self._invalidate()
                raise CompanionError('target_changed')
            return before
        finally:
            await ws.close()

    async def run(self):
        try:
            while not self._closed:
                try:
                    ws, token, (epoch, catalog) = await self._open()
                    self._ws = ws
                    self._apply(epoch, catalog)
                    async with ws:
                        while not self._closed:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), self.refresh_interval)
                            except TimeoutError:
                                await ws.send(pack(10, b'file-companion'))
                                continue
                            if not isinstance(raw, bytes):
                                raise CompanionError('invalid_parent_frame')
                            op, payload = unpack(raw)
                            if op in (2, 7):
                                # Invalidate before any asynchronous filesystem/proof checks.
                                self.valid.clear()
                                self._apply(*(await self._decode(_json(payload.decode('utf8')), token)))
                except (OSError, ValueError, TimeoutError, WebSocketException):
                    self._invalidate()
                    if not self._closed:
                        await asyncio.sleep(self.refresh_interval)
                finally:
                    self._ws = None
                    self._invalidate()
        finally:
            self._invalidate()

    async def close(self):
        self._closed = True
        self._invalidate()
        if self._ws:
            await self._ws.close()
        if self._own_io:
            await self.io.close()


def _private_state(path):
    _directory(path)
    path.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        os.chmod(path, 0o700)


class FileCompanion:
    """One helper per private state_dir; configured secret files are owner-only.

    HTTP is the default transport. ``download_base_url`` is a loopback-only
    fixture/local-gateway override, never a field accepted from RPC or a job.
    """
    def __init__(self, backend, broker_url, room, state_dir, owner_token_file,
                 enrollment_key_file, *, broker_token_file=None,
                 default_mylib_root=None, download_mode='http', download_base_url=None):
        self.backend = _endpoint(backend, backend=True)
        self.broker_url = _endpoint(broker_url)
        if download_mode not in {'http', 'ws'}:
            raise CompanionError('invalid_download_mode')
        self.download_mode = download_mode
        parsed = urlsplit(self.broker_url)
        self.download_base_url = urlunsplit(('https' if parsed.scheme == 'wss' else 'http', parsed.netloc, '', '', ''))
        if download_base_url is not None:
            self.download_base_url = _endpoint(download_base_url, backend=True)
        self._streams, self._stream_lock = {}, threading.Lock()
        if not isinstance(room, str) or not room or len(room) > 128 or not room.isprintable():
            raise CompanionError('invalid_room')
        self.room, self.state_dir = room, Path(state_dir).absolute()
        self.owner_token_file, self.enrollment_key_file = Path(owner_token_file).absolute(), Path(enrollment_key_file).absolute()
        self.broker_token_file = Path(broker_token_file or owner_token_file).absolute()
        self.io = _IO()
        self.inspector = LegacyInspector(backend, owner_token_file, io=self.io)
        self.workspaces = FileWorkspaces(self.state_dir / 'workspaces.json', default_mylib_root or os.environ.get('SUPERMONO_MYLIB', 'D:/aicoding/mylib'))
        self._ws, self._generation, self._closed = None, 0, False
        self._pending, self._tasks, self._deliveries = {}, set(), set()
        self._queued = {}
        self._send_lock = asyncio.Lock()
        self._db_lock = threading.RLock()
        self._db = None
        self._lease = None
        self.progress = {}
        self.status_id = ''
        self._status_state = 'connecting'
        self._self_birth = None
        self._status_lock = threading.Lock()

    def _write_status(self, state=None):
        with self._status_lock:
            self._store_status(state)

    def _store_status(self, state=None):
        _directory(self.state_dir, required=True)
        value = state or self._status_state
        if value == 'online' and not self.inspector.valid.is_set():
            value = 'parent_unavailable'
        body = {'schema': 1, 'role': 'file-companion', 'protocol': PROTOCOL, 'file_protocol': FILE_PROTOCOL,
                'pid': os.getpid(), 'process_start': self._self_birth,
                'backend': self.backend, 'broker_url': self.broker_url, 'room': self.room,
                'status_id': self.status_id, 'state': value, 'updated_at': time.time()}
        target = self.state_dir/'status.json'
        if _present(target):
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise CompanionError('invalid_status')
        temporary = self.state_dir/('.status-'+secrets.token_hex(8)+'.tmp')
        try:
            fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf8') as stream:
                json.dump(body, stream, separators=(',', ':'));stream.flush();os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _initialize(self):
        _private_state(self.state_dir)
        lock = self.state_dir / 'companion.lock'
        if _present(lock):
            info = lock.lstat()
            if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise CompanionError('invalid_identity')
        lease = lock.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                lease.seek(0); msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lease.close()
            raise CompanionError('helper_already_running') from None
        self._lease = lease
        identity = self.state_dir / 'host.json'
        try:
            info = identity.lstat()
            if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise CompanionError('invalid_identity')
            if info.st_size > 1024:
                raise CompanionError('invalid_identity')
            value = _json(identity.read_text(encoding='utf8'))
            if set(value) != {'host_id', 'host_secret'} or not re.fullmatch('[a-f0-9]{32}', value['host_id']) or not re.fullmatch('[a-f0-9]{64}', value['host_secret']):
                raise CompanionError('invalid_identity')
        except FileNotFoundError:
            value = {'host_id': secrets.token_hex(16), 'host_secret': secrets.token_hex(32)}
            fd = os.open(identity, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf8') as stream:
                json.dump(value, stream);stream.flush();os.fsync(stream.fileno())
        database = self.state_dir / 'deliveries.sqlite3'
        if _present(database):
            info = database.lstat()
            if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise CompanionError('invalid_journal')
        self._db = sqlite3.connect(database, check_same_thread=False)
        self._db.execute('PRAGMA synchronous=FULL')
        self._db.execute('CREATE TABLE IF NOT EXISTS deliveries(file_id TEXT PRIMARY KEY, binding TEXT, target TEXT, partial TEXT, receipt TEXT)')
        if 'target_binding' not in {row[1] for row in self._db.execute('PRAGMA table_info(deliveries)')}:
            self._db.execute('ALTER TABLE deliveries ADD COLUMN target_binding TEXT')
        self._db.commit()
        self._recover_partials()
        return value

    def _recover_partials(self):
        rows = self._db.execute('SELECT file_id,binding,target,partial FROM deliveries WHERE partial IS NOT NULL LIMIT 100001').fetchall()
        if len(rows) > 100000:
            raise CompanionError('invalid_journal')
        for file_id, binding, target, partial_id in rows:
            # Legacy metadata is accepted ONLY to retire its exact owned part.
            # Delivery still requires a binding, and NULL target_binding never
            # authorizes a migrated row to download or publish.
            job = self._job(_json(binding), _legacy_cleanup=True)
            if file_id != job['file_id'] or not isinstance(partial_id, str) or not re.fullmatch('[a-f0-9]{48}', partial_id):
                raise CompanionError('invalid_journal')
            path = Path(target)
            if path.name != job['name']:
                raise CompanionError('invalid_journal')
            _, status = _directory(path.parent)
            directory = path.parent / '.supermono-partials'
            if status != 'missing' and _present(directory):
                _plain_directory(directory)
                owned = directory / (partial_id + '.part')
                if _present(owned):
                    info = owned.lstat()
                    if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                        raise CompanionError('invalid_partial')
                    owned.unlink()
            with self._db:
                self._db.execute('UPDATE deliveries SET partial=NULL WHERE file_id=?', (file_id,))

    def _release(self):
        if self._lease:
            self._lease.close()
            self._lease = None

    async def _send(self, body, generation=None):
        if self._ws is None or generation is not None and generation != self._generation:
            raise CompanionError('broker_disconnected')
        raw = json.dumps(body, ensure_ascii=False, separators=(',', ':'))
        if len(raw.encode()) > MAX_FRAME:
            raise CompanionError('frame_too_large')
        async with self._send_lock:
            await self._ws.send(raw)

    async def _request(self, action, session, data, generation):
        identity = secrets.token_hex(16)
        future = asyncio.get_running_loop().create_future()
        self._pending[identity] = future
        try:
            await self._send({'type': 'request', 'id': identity, 'action': action, 'session': session, 'data': data}, generation)
            return await asyncio.wait_for(future, 15)
        finally:
            self._pending.pop(identity, None)

    async def _workspace(self, session):
        binding = await self.inspector.fresh(session)
        cfg = await self.io(self.workspaces.get, binding['window_id'], binding['cwd'])
        library = await self.io(self.workspaces.get_mylib)
        try:
            project_binding = await self.io(self._project_binding, binding, cfg)
        except (OSError, ValueError):
            project_binding = ''  # Settings remain inspectable/repairable, but uploads fail closed.
        return {**cfg, 'session_id': session, 'daemon_epoch': binding['daemon_epoch'],
                'project_binding': project_binding,
                'mylib_root': library['mylib_root'], 'mylib_revision': library['revision'],
                'mylib_status': library['status'], 'host_id': self.identity['host_id']}

    async def _route(self, body, generation):
        identity = body.get('id')
        try:
            if not isinstance(identity, str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}', identity):
                raise CompanionError('invalid_request')
            session, action, data = body.get('session'), body.get('action'), body.get('data')
            if not isinstance(data, dict) or not isinstance(session, str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}', session):
                raise CompanionError('invalid_request')
            binding = await self.inspector.fresh(session)
            if action == 'workspace.get' and not data:
                result = await self._workspace(session)
            elif action == 'workspace.browse' and set(data) <= {'path', 'offset', 'limit'}:
                result = await self.io(self.workspaces.browse, data.get('path'), offset=data.get('offset', 0), limit=data.get('limit', 100))
            elif action == 'workspace.set' and set(data) == {'project_dir', 'exchange_dir', 'expected_revision'}:
                result = await self.io(self.workspaces.save, binding['window_id'], data['project_dir'], data['exchange_dir'], expected_revision=data['expected_revision'])
                result = await self._workspace(session)
            elif action == 'library.get' and not data:
                result = await self.io(self.workspaces.get_mylib)
            elif action == 'library.set' and set(data) == {'mylib_root', 'expected_revision'}:
                result = await self.io(self.workspaces.save_mylib, data['mylib_root'], expected_revision=data['expected_revision'])
            else:
                raise CompanionError('invalid_request')
            if not self.inspector.current(binding):
                raise CompanionError('target_changed')
            await self._send({'type': 'host_response', 'id': identity, 'ok': True, 'result': result}, generation)
        except (ValueError, OSError, TimeoutError, WebSocketException) as error:
            code = 'directory_unavailable'
            if isinstance(error, RevisionConflict):
                code = 'revision_conflict'
            elif isinstance(error, ProjectSelectionRequired):
                code = 'project_required'
            elif isinstance(error, CompanionError):
                code = {'target_unavailable': 'session_unavailable', 'owner_unverified': 'unauthorized',
                        'target_changed': 'target_changed', 'invalid_request': 'invalid_request'}.get(str(error), 'unavailable')
            await self._send({'type': 'host_response', 'id': identity, 'ok': False, 'error': code}, generation)

    @staticmethod
    def _job(job, *, _legacy_cleanup=False):
        legacy = _legacy_cleanup and isinstance(job, dict) and 'project_binding' not in job
        if isinstance(job, dict) and 'project_binding' not in job and not legacy:
            raise CompanionError('project_binding_required')
        expected = {'file_id', 'name', 'size', 'sha256', 'window_id', 'session_id', 'daemon_epoch', 'workspace_revision', 'project_binding'}
        if legacy:
            expected.remove('project_binding')
        if not isinstance(job, dict) or set(job) != expected:
            raise CompanionError('invalid_job')
        for field, pattern in [('file_id', '[a-f0-9]{48}'), ('sha256', '[a-f0-9]{64}'), ('project_binding', '[a-f0-9]{64}'), ('daemon_epoch', '[a-f0-9]{64}'), ('window_id', '[A-Za-z0-9][A-Za-z0-9_-]{0,63}'), ('session_id', '[A-Za-z0-9_-]{1,64}')]:
            if legacy and field == 'project_binding':
                continue
            if not isinstance(job[field], str) or not re.fullmatch(pattern, job[field]):
                raise CompanionError('invalid_job')
        _validate_name(job['name'])
        if type(job['size']) is not int or not 0 <= job['size'] <= 512 * 1024**2 or type(job['workspace_revision']) is not int or not 0 <= job['workspace_revision'] <= 9007199254740991:
            raise CompanionError('invalid_job')
        return job

    def _row(self, job):
        with self._db_lock:
            row = self._db.execute('SELECT binding,target,partial,receipt,target_binding FROM deliveries WHERE file_id=?', (job['file_id'],)).fetchone()
        if row and row[0] != json.dumps(job, sort_keys=True, separators=(',', ':')):
            raise CompanionError('journal_mismatch')
        return row

    def _project_binding(self, binding, cfg, state=None):
        state = directory_binding(cfg) if state is None else state
        document = ['sma-project-binding-1', self.identity['host_id'], binding['window_id'],
                    binding['session_id'], binding['daemon_epoch'], binding['cwd'],
                    cfg['source'], cfg['revision'], state]
        return hmac.new(bytes.fromhex(self.identity['host_secret']),
                        json.dumps(document, ensure_ascii=False, separators=(',', ':')).encode('utf8'),
                        hashlib.sha256).hexdigest()

    def _check_project(self, job, binding, cfg=None):
        cfg = cfg or self.workspaces.guard(binding['window_id'], binding['cwd'], job['workspace_revision'])
        row = self._row(job)
        if row is None or not isinstance(row[4], str) or not re.fullmatch('[a-f0-9]{64}', row[4]):
            raise CompanionError('project_binding_required')
        if not hmac.compare_digest(self._project_binding(binding, cfg), row[4]):
            raise CompanionError('project_binding_changed')
        return cfg

    @staticmethod
    def _verify_file(path, job):
        _directory(path.parent, required=True)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400 or info.st_size != job['size']:
            raise CompanionError('saved_file_changed')
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            while chunk := stream.read(MAX_CHUNK):
                digest.update(chunk)
        if digest.hexdigest() != job['sha256']:
            raise CompanionError('saved_file_changed')

    def _begin(self, job, binding):
        cfg = self.workspaces.guard(binding['window_id'], binding['cwd'], job['workspace_revision'])
        target = Path(cfg['exchange_dir']) / job['name']
        row = self._row(job)
        state = directory_binding(cfg)
        expected_binding = row[4] if row else job['project_binding']
        if not isinstance(expected_binding, str) or not re.fullmatch('[a-f0-9]{64}', expected_binding):
            raise CompanionError('project_binding_required')
        if not hmac.compare_digest(self._project_binding(binding, cfg, state), expected_binding):
            raise CompanionError('project_binding_changed')
        if row:
            if row[1] != str(target):
                raise CompanionError('target_changed')
            if _present(target):
                self._verify_file(target, job)
                return None, None, str(target)
            if row[3]:
                raise CompanionError('saved_file_changed')
            if row[2]:
                if not isinstance(row[2], str) or not re.fullmatch('[a-f0-9]{48}', row[2]):
                    raise CompanionError('invalid_journal')
                old = target.parent / '.supermono-partials' / (row[2] + '.part')
                if _present(old):
                    _plain_directory(old.parent)
                    info = old.lstat()
                    if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                        raise CompanionError('invalid_partial')
                    old.unlink()
        elif _present(target):
            raise CompanionError('target_conflict')
        store = TransferStore(cfg['exchange_dir'])
        transfer = store.begin(self.identity['host_id'], job['session_id'], job['name'], job['size'], job['sha256'])
        try:
            created = directory_binding(cfg)
            if created['directories'][:len(state['directories'])] != state['directories']:
                raise CompanionError('project_binding_changed')
            target_binding = self._project_binding(binding, cfg, created)
            with self._db_lock, self._db:
                self._db.execute('INSERT OR REPLACE INTO deliveries(file_id,binding,target,partial,receipt,target_binding) VALUES(?,?,?,?,NULL,?)',
                    (job['file_id'], json.dumps(job, sort_keys=True, separators=(',', ':')), str(target), transfer, target_binding))
        except BaseException:
            store.cancel_owner(self.identity['host_id'])
            raise
        return store, transfer, None

    def _finish(self, store, transfer, job, binding, generation):
        with self.workspaces.publish_guard(binding['window_id'], binding['cwd'], job['workspace_revision']) as cfg:
            self._check_project(job, binding, cfg)
            expected = Path(cfg['exchange_dir']) / job['name']
            if store.root != Path(cfg['exchange_dir']):
                raise CompanionError('target_changed')
            if generation != self._generation or not self.inspector.current(binding) or self._closed:
                raise CompanionError('target_changed')
            if _present(expected):
                raise CompanionError('target_conflict')
            def can_publish():
                # finish() may block in flush/fsync. Revalidate after that wait,
                # at its final publication callback, rather than trusting entry.
                self._check_project(job, binding, cfg)
                store._check_directories()
                return generation == self._generation and self.inspector.current(binding) and not self._closed
            path = store.finish(self.identity['host_id'], transfer,
                can_publish=can_publish,
                strict_name=True)
            if path != str(expected):
                raise CompanionError('target_conflict')
            receipt = {name: job[name] for name in ('file_id', 'name', 'size', 'sha256', 'daemon_epoch', 'workspace_revision', 'project_binding')}
            receipt['path'] = path
            with self._db_lock, self._db:
                self._db.execute('UPDATE deliveries SET partial=NULL,receipt=? WHERE file_id=?', (json.dumps(receipt), job['file_id']))
            return receipt

    def _http_open(self, job, ticket, binding, generation):
        self._check_project(job, binding)
        path = '/file/download/' + job['file_id']
        if (not isinstance(ticket, dict) or set(ticket) != {'download_path', 'ticket', 'expires_at', 'size', 'sha256', 'name'}
                or ticket['download_path'] != path or ticket['size'] != job['size']
                or ticket['sha256'] != job['sha256'] or ticket['name'] != job['name']
                or not isinstance(ticket['ticket'], str) or not re.fullmatch('[A-Za-z0-9_-]{16,128}', ticket['ticket'])
                or type(ticket['expires_at']) not in (int, float) or not ticket['expires_at'] > time.time() - 5):
            raise CompanionError('invalid_download_ticket')
        if self._closed or generation != self._generation or not self.inspector.current(binding):
            raise CompanionError('target_changed')
        endpoint = urlsplit(self.download_base_url)
        connection_type = http.client.HTTPSConnection if endpoint.scheme == 'https' else http.client.HTTPConnection
        connection = connection_type(endpoint.hostname, endpoint.port, timeout=10)
        connection.set_debuglevel(0)
        with self._stream_lock:
            self._streams[job['file_id']] = [connection, None, None]
        try:
            # http.client neither follows redirects nor reads proxy environment.
            connection.request('GET', path, headers={'Authorization': 'Bearer ' + ticket['ticket'], 'Accept-Encoding': 'identity',
                                                   'X-SuperMono-File-Protocol': str(FILE_PROTOCOL)})
            transport = connection.sock
            with self._stream_lock:
                self._streams[job['file_id']][1] = transport
            response = connection.getresponse()
            with self._stream_lock:
                self._streams[job['file_id']][2] = response
            if (response.status != 200 or response.getheader('Content-Length') != str(job['size'])
                    or response.getheader('X-SuperMono-File-Protocol') != str(FILE_PROTOCOL)
                    or response.getheader('X-Content-SHA256') != job['sha256']
                    or response.getheader('Transfer-Encoding') is not None
                    or response.getheader('Content-Encoding') not in (None, 'identity')):
                raise CompanionError('invalid_download_response')
            return response
        except BaseException:
            self._http_close(job['file_id'])
            connection.close()
            raise

    def _http_read(self, response, binding, generation):
        if self._closed or generation != self._generation or not self.inspector.current(binding):
            raise CompanionError('target_changed')
        data = response.read(MAX_CHUNK)
        if self._closed or generation != self._generation or not self.inspector.current(binding):
            raise CompanionError('target_changed')
        return data

    def _abort_http(self, file_id=None):
        # Socket shutdown is nonblocking; it interrupts a worker's pending read
        # before cleanup is queued on that worker pool. No filesystem work here.
        with self._stream_lock:
            streams = [value for key, value in self._streams.items() if file_id is None or key == file_id]
        for connection, transport, _ in streams:
            transport = transport or connection.sock
            if transport:
                try:
                    transport.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                # HTTP/1.0 may already have called socket.close() while its
                # makefile reader retains the handle. Detach before force-close
                # so a blocked Windows recv is interrupted without double-close.
                try:
                    handle = transport.detach()
                    if handle != -1:
                        socket.close(handle)
                except OSError:
                    pass

    def _http_close(self, file_id):
        with self._stream_lock:
            value = self._streams.pop(file_id, None)
        if value:
            connection, _, response = value
            try:
                if response:
                    response.close()
            finally:
                connection.close()

    def _progress(self, file_id, value):
        if file_id not in self.progress and len(self.progress) >= 256:
            expired = next((key for key in self.progress if key not in self._deliveries), None)
            if expired is not None:
                self.progress.pop(expired)
        self.progress[file_id] = value

    async def _deliver(self, job, generation):
        store = transfer = None
        failure = None
        file_id = job.get('file_id') if isinstance(job, dict) else None
        try:
            job = self._job(job)
            async with asyncio.timeout(3):
                while not self.inspector.valid.is_set():
                    if self._closed or generation != self._generation:
                        raise CompanionError('broker_disconnected')
                    await asyncio.sleep(.025)
            binding = await self.inspector.fresh(job['session_id'])
            if binding['window_id'] != job['window_id'] or binding['daemon_epoch'] != job['daemon_epoch']:
                raise CompanionError('target_changed')
            store, transfer, saved = await self.io(self._begin, job, binding)
            self._progress(file_id, {'state': 'downloading', 'received': 0, 'size': job['size']})
            if saved:
                await self.inspector.fresh(job['session_id'])
                await self.io(self._check_project, job, binding)
                receipt = {name: job[name] for name in ('file_id', 'name', 'size', 'sha256', 'daemon_epoch', 'workspace_revision', 'project_binding')}
                receipt['path'] = saved
            else:
                offset = 0
                await self.io(self._check_project, job, binding)
                if self.download_mode == 'http':
                    ticket = await self._request('download.ticket', job['session_id'], {'file_id': file_id}, generation)
                    response = await self.io(self._http_open, job, ticket, binding, generation)
                    while offset < job['size']:
                        data = await self.io(self._http_read, response, binding, generation)
                        if not data or offset + len(data) > job['size']:
                            raise CompanionError('invalid_chunk')
                        offset = await self.io(store.append, self.identity['host_id'], transfer, offset, data)
                        self.progress[file_id]['received'] = offset
                    if await self.io(self._http_read, response, binding, generation):
                        raise CompanionError('invalid_chunk')
                else:
                    while offset < job['size']:
                        if not self.inspector.current(binding):
                            raise CompanionError('target_changed')
                        chunk = await self._request('download.chunk', job['session_id'], {'file_id': file_id, 'offset': offset}, generation)
                        data = base64.b64decode(chunk['data'], validate=True)
                        if not 0 < len(data) <= MAX_CHUNK or type(chunk['next']) is not int or chunk['next'] != offset + len(data) or chunk.get('total') != job['size']:
                            raise CompanionError('invalid_chunk')
                        offset = await self.io(store.append, self.identity['host_id'], transfer, offset, data)
                        self.progress[file_id]['received'] = offset
                fresh = await self.inspector.fresh(job['session_id'])
                if fresh != binding:
                    raise CompanionError('target_changed')
                receipt = await self.io(self._finish, store, transfer, job, binding, generation)
                transfer = None
            if not self.inspector.current(binding) or generation != self._generation:
                raise CompanionError('target_changed')
            await self._request('delivery.ack', job['session_id'], receipt, generation)
            self._progress(file_id, {'state': 'saved', 'received': job['size'], 'size': job['size']})
        except asyncio.CancelledError:
            raise
        except (ValueError, OSError, TimeoutError, KeyError, TypeError, sqlite3.Error, WebSocketException, http.client.HTTPException) as error:
            code = 'delivery_failed'
            if isinstance(error, RevisionConflict):
                code = 'revision_conflict'
            elif isinstance(error, ProjectSelectionRequired):
                code = 'project_required'
            elif isinstance(error, CompanionError) and str(error) in {
                    'target_changed', 'target_unavailable', 'owner_unverified', 'saved_file_changed',
                    'target_conflict', 'invalid_download_ticket', 'invalid_download_response', 'journal_mismatch',
                    'project_binding_required', 'project_binding_changed'}:
                code = str(error)
            failure = code
        finally:
            self._abort_http(file_id)
            async def cleanup():
                try:
                    await self.io(self._http_close, file_id)
                except (OSError, http.client.HTTPException):
                    pass
                finally:
                    if store is not None and transfer is not None:
                        try:
                            await self.io(store.cancel_owner, self.identity['host_id'])
                        except (ValueError, OSError):
                            pass
            cleanup_task = asyncio.create_task(cleanup())
            cancelled = False
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup_task.result()
            try:
                # Terminal failure is observable remotely. Publish it only after
                # the owned HTTP stream and receiver partial have been retired.
                if failure is not None and not cancelled:
                    self._progress(file_id, {'state': 'failed', 'error': failure})
                    if isinstance(job, dict) and isinstance(job.get('session_id'), str) and generation == self._generation:
                        try:
                            await self._request('delivery.fail', job['session_id'], {'file_id': file_id, 'error': failure}, generation)
                        except (OSError, ValueError, TimeoutError, ConnectionClosed):
                            pass
            finally:
                self._deliveries.discard(file_id)
                self._pump()
            if cancelled:
                raise asyncio.CancelledError

    def _pump(self):
        if self._closed:
            return
        while self._queued and len(self._deliveries) < 2:
            file_id = next(iter(self._queued))
            job, generation = self._queued.pop(file_id)
            self._deliveries.add(file_id)
            self._spawn(self._deliver(job, generation))

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        def done(finished):
            self._tasks.discard(finished)
            if not finished.cancelled():
                finished.exception()  # Errors are safe protocol state, never raw logs.
        task.add_done_callback(done)

    async def run(self, stop_event=None):
        try:
            await self._run(stop_event)
        finally:
            await self.inspector.close()
            try:
                if self._db:
                    await self.io(self._db.close)
            finally:
                await self.io(self._release)
                await self.io.close()

    async def _run(self, stop_event=None):
        self.identity = await self.io(self._initialize)
        self._self_birth = await self.io(_process_birth, os.getpid())
        async def heartbeat():
            while not self._closed:
                try: await self.io(self._write_status)
                except (OSError, ValueError): pass
                await asyncio.sleep(1)
        status_writer = asyncio.create_task(heartbeat())
        watcher = asyncio.create_task(self.inspector.run())
        async def wait_stop():
            while not self._closed:
                if stop_event and stop_event.is_set():
                    await self.close()
                    return
                await asyncio.sleep(.1)
        stopper = asyncio.create_task(wait_stop())
        try:
            while not self._closed and not (stop_event and stop_event.is_set()):
                try:
                    self._status_state = 'connecting'
                    while not self.inspector.valid.is_set() and not self._closed:
                        await asyncio.sleep(.025)
                    if self._closed:
                        break
                    token = await self.io(_secret, self.broker_token_file, maximum=4096)
                    key = await self.io(_secret, self.enrollment_key_file)
                    stamp, nonce = int(time.time()), secrets.token_hex(16)
                    hello = {'protocol': PROTOCOL, 'file_protocol': FILE_PROTOCOL, 'role': 'host', 'room': self.room, 'token': token, **self.identity,
                             'enrollment': {'timestamp': stamp, 'nonce': nonce, 'proof': enrollment_proof(key, self.room, self.identity['host_id'], self.identity['host_secret'], stamp, nonce)}}
                    async with _DirectConnect(self.broker_url + '?' + FILE_PROTOCOL_QUERY, proxy=None, ping_interval=None, open_timeout=3, close_timeout=1, max_size=MAX_FRAME, max_queue=8, logger=_TRANSPORT_LOG) as ws:
                        if self._closed:
                            break
                        self._ws = ws
                        self._generation += 1
                        generation = self._generation
                        await self._send(hello)
                        ready = _json(await asyncio.wait_for(ws.recv(), 2))
                        if (not isinstance(ready, dict) or ready.get('type') != 'ready' or ready.get('role') != 'host' or ready.get('room') != self.room
                                or type(ready.get('file_protocol')) is not int or ready['file_protocol'] != FILE_PROTOCOL):
                            raise CompanionError('broker_rejected')
                        self._status_state = 'online'
                        async for raw in ws:
                            if not isinstance(raw, str) or len(raw.encode()) > MAX_FRAME:
                                raise CompanionError('invalid_frame')
                            body = _json(raw)
                            if not isinstance(body, dict):
                                raise CompanionError('invalid_frame')
                            if body.get('type') == 'response':
                                future = self._pending.get(body.get('id'))
                                if future is not None and not future.done():
                                    if body.get('ok') is True and isinstance(body.get('result'), dict):
                                        future.set_result(body['result'])
                                    else:
                                        future.set_exception(CompanionError('broker_rejected'))
                            elif body.get('type') == 'host_request':
                                if len(self._tasks) < 10:
                                    self._spawn(self._route(body, generation))
                                else:
                                    await self._send({'type': 'host_response', 'id': body.get('id'), 'ok': False, 'error': 'service_busy'})
                            elif body.get('type') == 'delivery':
                                job = self._job(body.get('job'))
                                if job['file_id'] not in self._deliveries:
                                    if len(self._queued) >= 4096:
                                        raise CompanionError('delivery_queue_full')
                                    self._queued[job['file_id']] = (job, generation)
                                    self._pump()
                except (OSError, ValueError, TimeoutError, WebSocketException) as error:
                    frame = getattr(error, 'rcvd', None)
                    self._status_state = 'authentication_failed' if frame is not None and frame.reason == 'authentication_failed' else 'broker_unavailable'
                finally:
                    self._generation += 1
                    self._abort_http()
                    self._ws = None
                    self._queued.clear()
                    for future in self._pending.values():
                        if not future.done():
                            future.cancel()
                    tasks = list(self._tasks)
                    for task in tasks:
                        task.cancel()
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                if not self._closed:
                    await asyncio.sleep(.25)
        finally:
            status_writer.cancel()
            await asyncio.gather(status_writer, return_exceptions=True)
            try: await self.io(self._write_status, 'stopped')
            except (OSError, ValueError): pass
            stopper.cancel()
            await asyncio.gather(stopper, return_exceptions=True)
            await self.inspector.close()
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def close(self):
        self._closed = True
        self._generation += 1
        self._abort_http()
        if self._ws:
            await self._ws.close()
        await self.inspector.close()


def main():
    parser = argparse.ArgumentParser(description='Independent hidden file helper; secrets are file paths only')
    parser.add_argument('--backend', default='http://127.0.0.1:8765')
    parser.add_argument('--broker', required=True)
    parser.add_argument('--state-dir', required=True, type=Path)
    parser.add_argument('--owner-token-file', required=True, type=Path)
    parser.add_argument('--broker-token-file', type=Path)
    parser.add_argument('--enrollment-key-file', required=True, type=Path)
    parser.add_argument('--download-mode', choices=('http', 'ws'), default='http')
    parser.add_argument('--status-id', default='')
    parser.add_argument('--room')
    args = parser.parse_args()
    room = args.room
    if room is None:
        try:
            room = (args.state_dir.parent / 'relay-code').read_text(encoding='utf8').strip()
        except FileNotFoundError:
            room = socket.gethostname()
    companion = FileCompanion(args.backend, args.broker, room, args.state_dir,
        args.owner_token_file, args.enrollment_key_file, broker_token_file=args.broker_token_file,
        download_mode=args.download_mode)
    if args.status_id and not re.fullmatch('[a-f0-9]{32}', args.status_id):
        parser.error('Invalid status identity')
    companion.status_id = args.status_id
    try:
        asyncio.run(companion.run())
    except (ValueError, OSError, sqlite3.Error, KeyboardInterrupt):
        raise SystemExit('File helper unavailable') from None


if __name__ == '__main__':
    main()
