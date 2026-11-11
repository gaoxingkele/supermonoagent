"""Private, independent journal mirror. Never controls or restarts a terminal.

Run behind the existing relay's TLS authority at exactly /journals/ws. Authentication
uses the file broker owner configuration, but the database and protocol are separate.
Only an enrolled and pinned source may append; viewer credentials grant reads only.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import threading
import time

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from .conversation_contract import (BATCH_IDENTITY, CONTENT_CHUNK, EVENT_KINDS,
    MAX_ENVELOPE, MIRROR_BATCH_EVENTS, PAGE_DEFAULT, PAGE_MAX, PREVIEW_CHARS,
    PROTOCOL, JournalError, batch_digest, canonical_json, checked_id,
    checked_integer, checked_label, decode_json, normalize_destination)
from .file_broker import _auth
from .file_transfer import _plain_directory

_MANIFEST = frozenset('schema_version journal_id owner_scope host_installation_id source_epoch window_id '
    'session_id process_generation room title agent created_at capture_started_at coverage terminal_state '
    'capture_state terminal_ended_at sealed_at last_seq local_error mirror'.split())
_EVENT = frozenset('event_id seq kind actor origin binding_id provider_thread_id provider_turn_id '
    'provider_item_id source_record_id source_created_at observed_at state proof supersedes_event_id '
    'metadata content_id content_bytes content_sha256 encoding preview'.split())
_PRIVATE_KEYS = frozenset({'token', 'token_sha256', 'enrollment_key', 'enrollment_proof',
    'host_secret', 'authorization', 'password', 'access_token', 'refresh_token',
    'secret', 'cookie', 'credentials', 'auth'})
_STATE_FIELDS = frozenset({'terminal_state', 'capture_state', 'terminal_ended_at',
    'sealed_at', 'local_error', 'coverage'})


def _digest(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        raise JournalError()
    return value


def _safe_metadata(value, depth=0):
    if depth > 12:
        raise JournalError()
    if isinstance(value, dict):
        if any(not isinstance(k, str) or k.lower() in _PRIVATE_KEYS for k in value):
            raise JournalError()
        for child in value.values():
            _safe_metadata(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _safe_metadata(child, depth + 1)


def source_proof(secret, room, host_id, timestamp, nonce):
    """Fresh proof of the separate pinned source secret (never the viewer token)."""
    return hmac.new(secret.encode(), canonical_json([PROTOCOL, 'source', room, host_id,
        timestamp, nonce]), hashlib.sha256).hexdigest()


def enrollment_proof(key, room, host_id, secret, timestamp, nonce):
    return hmac.new(key.encode(), canonical_json([PROTOCOL, 'enroll', room, host_id,
        hashlib.sha256(secret.encode()).hexdigest(), timestamp, nonce]), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class _Principal:
    scope: str
    room: str
    role: str
    digest: str
    host: str = ''
    secret_digest: str = ''


class JournalService:
    """One process and one SQLite writer per private root; all socket I/O bounded."""
    def __init__(self, root, auth_file, *, origins=(), clock=time.time,
                 max_connections=64, max_content_bytes=64 * 1024**2,
                 max_staged_bytes=512 * 1024**2, max_storage_bytes=2 * 1024**3,
                 max_content_items=100000, max_pending_io=32):
        checked_integer(max_connections, 1, 1024)
        checked_integer(max_content_bytes, 0, 2**30)
        checked_integer(max_staged_bytes, 1, 2**40)
        checked_integer(max_storage_bytes, 1, 2**40)
        checked_integer(max_content_items, 1, 1000000)
        checked_integer(max_pending_io, 1, 1024)
        self.auth_file = Path(auth_file).absolute()
        _auth(self.auth_file)
        self.origins = frozenset(origins)
        for origin in self.origins:
            normalize_destination(dict(destination_id='origin', authority=origin,
                service_path='/journals/ws', owner_scope='origin', external_room='origin',
                host_installation_id='origin', source_epoch='origin'))
        self.root = Path(os.path.abspath(root))
        for ancestor in (*reversed(self.root.parents), self.root):
            if ancestor.exists() or ancestor.is_symlink():
                _plain_directory(ancestor)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _plain_directory(self.root)
        if os.name != 'nt' and stat.S_IMODE(self.root.stat().st_mode) & 0o077:
            raise ValueError('Journal storage must be private')
        self._directories = {p: (p.stat().st_dev, p.stat().st_ino)
            for p in (*reversed(self.root.parents), self.root)}
        self._clock = clock
        self.max_connections = max_connections
        self.max_content_bytes = max_content_bytes
        self.max_staged_bytes = max_staged_bytes
        self.max_storage_bytes = max_storage_bytes
        self.max_content_items = max_content_items
        self._active = 0
        self._lock = threading.RLock()
        database = self.root / 'journals.sqlite3'
        lock_path = self.root / 'writer.lock'
        for path in (database, lock_path, Path(str(database) + '-wal'), Path(str(database) + '-shm')):
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise ValueError('Invalid journal storage')
        self._process_lock = lock_path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self._process_lock.seek(0)
                msvcrt.locking(self._process_lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._process_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._process_lock.close()
            raise ValueError('Journal storage is already in use') from None
        try:
            self._db = sqlite3.connect(database, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS hosts(scope TEXT,room TEXT,host TEXT,secret_digest TEXT,
                    PRIMARY KEY(scope,room,host));
                CREATE TABLE IF NOT EXISTS nonces(scope TEXT,nonce TEXT,stamp INTEGER,
                    PRIMARY KEY(scope,nonce));
                CREATE TABLE IF NOT EXISTS journals(scope TEXT,room TEXT,journal TEXT,host TEXT,
                    epoch TEXT,destination TEXT,manifest TEXT,last_seq INTEGER NOT NULL,
                    PRIMARY KEY(scope,journal));
                CREATE TABLE IF NOT EXISTS content(scope TEXT,room TEXT,host TEXT,journal TEXT,
                    content TEXT,size INTEGER,sha256 TEXT,data BLOB,complete INTEGER NOT NULL,
                    PRIMARY KEY(scope,journal,content));
                CREATE TABLE IF NOT EXISTS events(scope TEXT,journal TEXT,seq INTEGER,event TEXT,
                    descriptor TEXT,PRIMARY KEY(scope,journal,seq),UNIQUE(scope,journal,event));
                CREATE TABLE IF NOT EXISTS receipts(scope TEXT,journal TEXT,batch TEXT,digest TEXT,
                    receipt TEXT,PRIMARY KEY(scope,journal,batch));''')
            with self._db:
                self._db.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',
                    ('cursor_key', secrets.token_hex(32)))
            self._cursor_key = bytes.fromhex(self._db.execute(
                'SELECT value FROM settings WHERE key=?', ('cursor_key',)).fetchone()[0])
            self._database = database
            self._database_identity = (database.stat().st_dev, database.stat().st_ino)
        except BaseException:
            if hasattr(self, '_db'):
                self._db.close()
            self._process_lock.close()
            raise
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='sma-journal-io')
        self._io_slots = asyncio.Semaphore(max_pending_io)

    def close(self):
        self._executor.shutdown(wait=True, cancel_futures=True)
        with self._lock:
            self._db.close()
            self._process_lock.close()

    def _check_directories(self):
        for path, identity in self._directories.items():
            info = _plain_directory(path)
            if (info.st_dev, info.st_ino) != identity:
                raise JournalError('storage_unavailable')
        for path in (self._database, Path(str(self._database) + '-wal'), Path(str(self._database) + '-shm')):
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if (not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                        or path == self._database and (info.st_dev, info.st_ino) != self._database_identity):
                    raise JournalError('storage_unavailable')

    def _authorized(self, principal):
        owners, tokens = _auth(self.auth_file)
        owner = owners.get(principal.scope)
        if (tokens.get(principal.digest) != principal.scope or owner is None
                or principal.room not in owner['rooms'] and '*' not in owner['rooms']):
            raise JournalError('unauthorized')
        if principal.role == 'source':
            row = self._db.execute('SELECT secret_digest FROM hosts WHERE scope=? AND room=? AND host=?',
                (principal.scope, principal.room, principal.host)).fetchone()
            if row is None or not hmac.compare_digest(row[0], principal.secret_digest):
                raise JournalError('unauthorized')
        return owner

    def _authenticate(self, hello):
        self._check_directories()
        if (hello.get('op') != 'hello' or hello.get('protocol') != PROTOCOL
                or hello.get('role') not in {'viewer', 'source'}):
            raise JournalError('unauthorized')
        allowed = {'op', 'protocol', 'role', 'token', 'room'}
        if hello['role'] == 'source':
            allowed |= {'host_installation_id', 'host_secret', 'timestamp', 'nonce', 'proof', 'enrollment_proof'}
        if set(hello) - allowed:
            raise JournalError('unauthorized')
        token = hello.get('token')
        if not isinstance(token, str) or not 1 <= len(token) <= 8192:
            raise JournalError('unauthorized')
        digest = hashlib.sha256(token.encode()).hexdigest()
        owners, tokens = _auth(self.auth_file)
        scope = tokens.get(digest)
        room = checked_label(hello.get('room'))
        if scope is None or room not in owners[scope]['rooms'] and '*' not in owners[scope]['rooms']:
            raise JournalError('unauthorized')
        if hello['role'] == 'viewer':
            return _Principal(scope, room, 'viewer', digest)
        host = checked_id(hello.get('host_installation_id'))
        secret = hello.get('host_secret')
        if not isinstance(secret, str) or not 16 <= len(secret) <= 8192:
            raise JournalError('unauthorized')
        stamp, nonce = checked_integer(hello.get('timestamp')), checked_id(hello.get('nonce'))
        if abs(self._clock() - stamp) > 120:
            raise JournalError('unauthorized')
        expected = source_proof(secret, room, host, stamp, nonce)
        if not isinstance(hello.get('proof'), str) or not hmac.compare_digest(expected, hello['proof']):
            raise JournalError('unauthorized')
        secret_digest = hashlib.sha256(secret.encode()).hexdigest()
        with self._db:
            # Nonces survive process restarts. Old expired nonces cannot pass the time gate.
            self._db.execute('DELETE FROM nonces WHERE stamp<?', (int(self._clock()) - 240,))
            if self._db.execute('SELECT 1 FROM nonces WHERE scope=? AND nonce=?', (scope, nonce)).fetchone():
                raise JournalError('replay_denied')
            row = self._db.execute('SELECT secret_digest FROM hosts WHERE scope=? AND room=? AND host=?',
                (scope, room, host)).fetchone()
            if row is None:
                expected = enrollment_proof(owners[scope]['key'], room, host, secret, stamp, nonce)
                if not isinstance(hello.get('enrollment_proof'), str) or not hmac.compare_digest(expected, hello['enrollment_proof']):
                    raise JournalError('unauthorized')
                if self._db.execute('SELECT count(*) FROM hosts').fetchone()[0] >= 4096:
                    raise JournalError('capacity_exceeded')
                self._db.execute('INSERT INTO hosts VALUES(?,?,?,?)', (scope, room, host, secret_digest))
            elif not hmac.compare_digest(row[0], secret_digest):
                raise JournalError('pin_conflict')
            self._db.execute('INSERT INTO nonces VALUES(?,?,?)', (scope, nonce, stamp))
            principal = _Principal(scope, room, 'source', digest, host, secret_digest)
            self._authorized(principal)
        return principal

    async def _io(self, function, *args):
        async with self._io_slots:
            return await asyncio.get_running_loop().run_in_executor(self._executor, function, *args)

    def _run(self, principal, action, params):
        with self._lock:
            self._check_directories()
            self._authorized(principal)
            with self._db:
                result = self._dispatch(principal, action, params)
                # Revocation/rotation after queued storage work also blocks writes and reads.
                self._authorized(principal)
            return result

    async def handle_socket(self, ws):
        request = getattr(ws, 'request', None)
        if (request is not None and request.path != '/journals/ws'
                or self._active >= self.max_connections):
            await ws.close(1008, 'permission_denied')
            return
        if request is not None:
            try:
                origin = request.headers.get('Origin')
            except Exception:
                await ws.close(1008, 'permission_denied')
                return
            if origin is not None and origin not in self.origins:
                await ws.close(1008, 'permission_denied')
                return
        self._active += 1
        try:
            raw = await asyncio.wait_for(ws.recv(), 10)
            principal = await self._io(self._authenticate, decode_json(raw))
            await ws.send(canonical_json(dict(op='hello', protocol=PROTOCOL, ok=True, role=principal.role)).decode())
            async for raw in ws:
                request_id = None
                try:
                    message = decode_json(raw)
                    request_id = checked_id(message.get('request_id'), 64)
                    if set(message) != {'op', 'request_id', 'action', 'params'} or message['op'] != 'request':
                        raise JournalError()
                    action, params = message['action'], message['params']
                    if not isinstance(action, str) or not isinstance(params, dict):
                        raise JournalError()
                    result = await self._io(self._run, principal, action, params)
                    # Revalidate once more immediately before sending a queued result.
                    await self._io(self._authorized, principal)
                    response = dict(op='response', request_id=request_id, ok=True, result=result)
                    if len(canonical_json(response)) > MAX_ENVELOPE:
                        raise JournalError('frame_too_large')
                except JournalError as error:
                    response = dict(op='response', request_id=request_id, ok=False, error=error.code)
                except Exception:
                    response = dict(op='response', request_id=request_id, ok=False, error='unavailable')
                await ws.send(canonical_json(response).decode())
        except JournalError as error:
            await ws.send(canonical_json(dict(op='hello', ok=False, error=error.code)).decode())
            await ws.close(1008, error.code)
        except (ConnectionClosed, asyncio.TimeoutError):
            pass
        except Exception:
            await ws.close(1011, 'unavailable')
        finally:
            self._active -= 1

    def _journal(self, p, journal_id):
        checked_id(journal_id)
        row = self._db.execute('SELECT * FROM journals WHERE scope=? AND room=? AND journal=?',
            (p.scope, p.room, journal_id)).fetchone()
        if row is None:
            raise JournalError('not_found')
        return row

    def _cursor(self, context, value):
        raw = canonical_json([context, value])
        return base64.urlsafe_b64encode(raw + hmac.digest(self._cursor_key, raw, 'sha256')).decode().rstrip('=')

    def _uncursor(self, context, cursor, default):
        if cursor is None:
            return default
        try:
            if not isinstance(cursor, str) or len(cursor) > 2048:
                raise ValueError()
            encoded = base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True)
            raw, signature = encoded[:-32], encoded[-32:]
            if not hmac.compare_digest(hmac.digest(self._cursor_key, raw, 'sha256'), signature):
                raise ValueError()
            saved, value = json.loads(raw)
            if saved != context:
                raise ValueError()
            return value
        except (ValueError, TypeError, UnicodeError):
            raise JournalError('invalid_cursor') from None

    def _dispatch(self, p, action, params):
        if action in {'append_batch', 'content.stage'}:
            if p.role != 'source':
                raise JournalError('permission_denied')
            return self._stage(p, params) if action == 'content.stage' else self._append(p, params)
        fields = {'status': {'journal_id'}, 'list': {'cursor', 'limit'},
            'read_page': {'journal_id', 'cursor', 'limit', 'projection'},
            'read_content': {'journal_id', 'content_id', 'offset', 'limit'}}
        if action not in fields or set(params) - fields[action]:
            raise JournalError()
        if action == 'list':
            limit = checked_integer(params.get('limit', PAGE_DEFAULT), 1, PAGE_MAX)
            context = [p.scope, p.room, 'list']
            after = self._uncursor(context, params.get('cursor'), '')
            rows = self._db.execute('SELECT journal,manifest FROM journals WHERE scope=? AND room=? '
                'AND journal>? ORDER BY journal LIMIT ?', (p.scope, p.room, after, limit + 1)).fetchall()
            journals = []
            for row in rows[:limit]:
                item = json.loads(row['manifest'])
                if len(canonical_json(dict(journals=journals + [item], next_cursor='x' * 2048))) > MAX_ENVELOPE - 512:
                    break
                journals.append(item)
            cursor = self._cursor(context, journals[-1]['journal_id']) if journals and len(rows) > len(journals) else None
            return dict(journals=journals, next_cursor=cursor)
        row = self._journal(p, params.get('journal_id'))
        if action == 'status':
            return json.loads(row['manifest'])
        if action == 'read_content':
            content_id = checked_id(params.get('content_id'))
            offset = checked_integer(params.get('offset', 0))
            limit = checked_integer(params.get('limit', CONTENT_CHUNK), 1, CONTENT_CHUNK)
            content = self._db.execute('SELECT c.size,c.sha256,substr(c.data,?,?) AS chunk FROM content c '
                'WHERE c.scope=? AND c.journal=? AND c.content=? AND c.complete=1 '
                'AND EXISTS(SELECT 1 FROM events e WHERE e.scope=c.scope AND e.journal=c.journal '
                "AND json_extract(e.descriptor,'$.content_id')=c.content)",
                (offset + 1, limit, p.scope, row['journal'], content_id)).fetchone()
            if content is None:
                raise JournalError('not_found')
            if offset > content['size']:
                raise JournalError()
            chunk = content['chunk']
            return dict(content_id=content_id, offset=offset, data_base64=base64.b64encode(chunk).decode(),
                total_bytes=content['size'], sha256=content['sha256'],
                next_offset=offset + len(chunk) if offset + len(chunk) < content['size'] else None)
        projection = params.get('projection', 'history')
        if projection not in {'history', 'conversation'}:
            raise JournalError()
        limit = checked_integer(params.get('limit', PAGE_DEFAULT), 1, PAGE_MAX)
        context = [p.scope, p.room, row['journal'], projection]
        after = self._uncursor(context, params.get('cursor'), 0)
        checked_integer(after)
        sql = 'SELECT seq,descriptor FROM events WHERE scope=? AND journal=? AND seq>? '
        if projection == 'conversation':
            sql += "AND ((json_extract(descriptor,'$.kind')='user_input' " \
                "AND json_extract(descriptor,'$.state')='accepted' " \
                "AND json_extract(descriptor,'$.actor') IN ('human','user') " \
                "AND coalesce(json_extract(descriptor,'$.proof.kind'),json_extract(descriptor,'$.proof')) " \
                "IN ('app_submission','provider_user_item','provider_human_input','user_attestation')) OR " \
                "(json_extract(descriptor,'$.kind')='assistant_final' " \
                "AND json_extract(descriptor,'$.state') IN ('completed','confirmed') " \
                "AND coalesce(json_extract(descriptor,'$.proof.kind'),json_extract(descriptor,'$.proof')) " \
                "IN ('provider_final_phase','provider_terminal_result','user_attestation'))) "
        rows = self._db.execute(sql + 'ORDER BY seq LIMIT ?', (p.scope, row['journal'], after, limit + 1)).fetchall()
        events = []
        journal = json.loads(row['manifest'])
        for event_row in rows[:limit]:
            event = json.loads(event_row['descriptor'])
            if event['kind'] in {'raw_output', 'bootstrap'}:
                event['preview'] = ''
            if len(canonical_json(dict(journal=journal, events=events + [event], next_cursor='x' * 2048))) > MAX_ENVELOPE - 512:
                break
            events.append(event)
        cursor = self._cursor(context, events[-1]['seq']) if events and len(rows) > len(events) else None
        return dict(journal=journal, events=events, next_cursor=cursor)

    def _stage(self, p, params):
        if set(params) != {'journal_id', 'content_id', 'offset', 'total_bytes', 'sha256', 'data_base64'}:
            raise JournalError()
        journal, content_id = checked_id(params['journal_id']), checked_id(params['content_id'])
        total = checked_integer(params['total_bytes'], 0, self.max_content_bytes)
        offset = checked_integer(params['offset'], 0, total)
        sha = _digest(params['sha256'])
        try:
            data = base64.b64decode(params['data_base64'], validate=True)
        except (ValueError, TypeError):
            raise JournalError() from None
        if len(data) > CONTENT_CHUNK or offset + len(data) > total:
            raise JournalError()
        known = self._db.execute('SELECT host,epoch,room FROM journals WHERE scope=? AND journal=?', (p.scope, journal)).fetchone()
        if known and (known['host'] != p.host or known['room'] != p.room):
            raise JournalError('pin_conflict')
        row = self._db.execute('SELECT *,length(data) AS received FROM content WHERE scope=? AND journal=? AND content=?',
            (p.scope, journal, content_id)).fetchone()
        if row is None:
            if offset:
                raise JournalError('content_incomplete')
            used = self._db.execute('SELECT coalesce(sum(size),0) FROM content WHERE complete=0').fetchone()[0]
            count, allocated = self._db.execute('SELECT count(*),coalesce(sum(size),0) FROM content').fetchone()
            if (used + total > self.max_staged_bytes or allocated + total > self.max_storage_bytes
                    or count >= self.max_content_items):
                raise JournalError('capacity_exceeded')
            self._db.execute('INSERT INTO content VALUES(?,?,?,?,?,?,?,?,0)',
                (p.scope, p.room, p.host, journal, content_id, total, sha, b''))
            received = 0
        else:
            if (row['host'] != p.host or row['room'] != p.room or row['size'] != total or row['sha256'] != sha):
                raise JournalError('content_conflict')
            received = row['received']
        if offset < received:
            old = self._db.execute('SELECT substr(data,?,?) FROM content WHERE scope=? AND journal=? AND content=?',
                (offset + 1, len(data), p.scope, journal, content_id)).fetchone()[0]
            if offset + len(data) > received or bytes(old) != data:
                raise JournalError('content_conflict')
        elif offset == received:
            self._db.execute('UPDATE content SET data=CAST(data || ? AS BLOB) WHERE scope=? AND journal=? AND content=?',
                (data, p.scope, journal, content_id))
            received += len(data)
        else:
            raise JournalError('content_incomplete')
        complete = received == total
        if complete:
            body = self._db.execute('SELECT data FROM content WHERE scope=? AND journal=? AND content=?',
                (p.scope, journal, content_id)).fetchone()[0]
            if hashlib.sha256(bytes(body)).hexdigest() != sha:
                raise JournalError('content_hash_mismatch')
            self._db.execute('UPDATE content SET complete=1 WHERE scope=? AND journal=? AND content=?', (p.scope, journal, content_id))
        return dict(content_id=content_id, received_bytes=received, complete=complete)

    def _append(self, p, params):
        if set(params) != {'batch', 'journal'}:
            raise JournalError()
        batch, manifest = params['batch'], params['journal']
        if (not isinstance(batch, dict) or set(batch) != set(BATCH_IDENTITY) | {'events', 'contents'}
                or batch.get('batch_sha256') != batch_digest(batch)):
            raise JournalError('invalid_batch')
        for field in ('destination_id', 'owner_scope', 'host_installation_id', 'journal_id', 'source_epoch', 'batch_id'):
            checked_id(batch[field])
        if batch['owner_scope'] != p.scope or batch['external_room'] != p.room or batch['host_installation_id'] != p.host:
            raise JournalError('permission_denied')
        if not isinstance(manifest, dict) or set(manifest) != _MANIFEST:
            raise JournalError('invalid_batch')
        if any(manifest.get(key) != batch[key] for key in ('owner_scope', 'host_installation_id', 'journal_id', 'source_epoch')) or manifest['room'] != p.room:
            raise JournalError('permission_denied')
        _safe_metadata(manifest)
        if len(canonical_json(manifest)) > 16384:
            raise JournalError('invalid_batch')
        if (manifest['schema_version'] != 1 or manifest['terminal_state'] not in {'running', 'ended', 'unknown', 'interrupted'}
                or manifest['capture_state'] not in {'capturing', 'suspended', 'draining', 'sealed', 'incomplete'}):
            raise JournalError('invalid_batch')
        events, contents = batch['events'], batch['contents']
        first, last = checked_integer(batch['first_seq'], 1), checked_integer(batch['last_seq'], 1)
        if (not isinstance(events, list) or not 1 <= len(events) <= MIRROR_BATCH_EVENTS
                or last - first + 1 != len(events) or not isinstance(contents, list)
                or len(contents) > len(events) or checked_integer(manifest['last_seq']) < last):
            raise JournalError('invalid_batch')
        for index, event in enumerate(events):
            if not isinstance(event, dict) or set(event) != _EVENT:
                raise JournalError('invalid_batch')
            checked_id(event['event_id'])
            checked_id(event['content_id'])
            if (checked_integer(event['seq'], 1) != first + index or event['kind'] not in EVENT_KINDS
                    or not isinstance(event['preview'], str) or len(event['preview']) > PREVIEW_CHARS
                    or len(canonical_json(event)) > MAX_ENVELOPE // 4):
                raise JournalError('invalid_batch')
            if not isinstance(event['metadata'], dict) or event['encoding'] not in {'utf-8', 'binary'}:
                raise JournalError('invalid_batch')
            for key in ('actor', 'origin', 'state'):
                if event[key] is not None:
                    checked_label(event[key], 128, empty=True)
            for key in ('binding_id', 'provider_thread_id', 'provider_turn_id', 'provider_item_id',
                        'source_record_id', 'supersedes_event_id'):
                if event[key] is not None:
                    checked_id(event[key], 512)
            _safe_metadata(event['metadata'])
            _safe_metadata(event['proof'])
            checked_integer(event['content_bytes'], 0, self.max_content_bytes)
            _digest(event['content_sha256'])
        expected = {}
        for event in events:
            reference = (event['content_bytes'], event['content_sha256'])
            if event['content_id'] in expected and expected[event['content_id']] != reference:
                raise JournalError('invalid_batch')
            expected[event['content_id']] = reference
        if len(contents) != len(expected):
            raise JournalError('invalid_batch')
        seen = set()
        for content in contents:
            if not isinstance(content, dict) or set(content) != {'content_id', 'bytes', 'sha256'}:
                raise JournalError('invalid_batch')
            content_id = checked_id(content['content_id'])
            checked_integer(content['bytes'], 0, self.max_content_bytes)
            _digest(content['sha256'])
            if content_id in seen or expected.get(content_id) != (content['bytes'], content['sha256']):
                raise JournalError('invalid_batch')
            seen.add(content_id)
            row = self._db.execute('SELECT * FROM content WHERE scope=? AND journal=? AND content=?',
                (p.scope, batch['journal_id'], content_id)).fetchone()
            if (row is None or not row['complete'] or row['room'] != p.room or row['host'] != p.host
                    or row['size'] != content['bytes'] or row['sha256'] != content['sha256']):
                raise JournalError('content_incomplete')
            if len(row['data']) != content['bytes'] or hashlib.sha256(row['data']).hexdigest() != content['sha256']:
                raise JournalError('content_hash_mismatch')
        row = self._db.execute('SELECT * FROM journals WHERE scope=? AND journal=?',
            (p.scope, batch['journal_id'])).fetchone()
        if row:
            if (row['room'], row['host'], row['epoch'], row['destination']) != (
                    p.room, p.host, batch['source_epoch'], batch['destination_id']):
                raise JournalError('pin_conflict')
            through = row['last_seq']
            previous_manifest = json.loads(row['manifest'])
            immutable = _MANIFEST - _STATE_FIELDS - {'last_seq', 'mirror'}
            if any(previous_manifest[key] != manifest[key] for key in immutable):
                raise JournalError('manifest_conflict')
        else:
            through = 0
            previous_manifest = dict(manifest, terminal_state='unknown', capture_state='suspended',
                terminal_ended_at=None, sealed_at=None, local_error=None, coverage='unknown')
        previous = self._db.execute('SELECT * FROM receipts WHERE scope=? AND journal=? AND batch=?',
            (p.scope, batch['journal_id'], batch['batch_id'])).fetchone()
        if previous:
            if previous['digest'] != batch['batch_sha256']:
                raise JournalError('batch_conflict')
            return dict(receipt=json.loads(previous['receipt']))
        if first != through + 1:
            raise JournalError('sequence_conflict')
        # The wrapper may describe future source events beyond this pending batch.
        # Only immutable committed status events can advance the archived lifecycle.
        stored_manifest = dict(previous_manifest, last_seq=last)
        for event in events:
            if event['kind'] == 'status' and isinstance(event['metadata'], dict):
                state = event['metadata'].get('journal_state')
                if state is not None:
                    if not isinstance(state, dict) or set(state) - _STATE_FIELDS:
                        raise JournalError('invalid_batch')
                    if ('terminal_state' in state and state['terminal_state'] not in {'running','ended','unknown','interrupted'}
                            or 'capture_state' in state and state['capture_state'] not in {'capturing','suspended','draining','sealed','incomplete'}):
                        raise JournalError('invalid_batch')
                    stored_manifest.update(state)
        stored_manifest['mirror'] = dict(through_seq=last, destination_id=batch['destination_id'])
        try:
            self._db.executemany('INSERT INTO events VALUES(?,?,?,?,?)',
                [(p.scope, batch['journal_id'], event['seq'], event['event_id'], canonical_json(event).decode()) for event in events])
        except sqlite3.IntegrityError:
            raise JournalError('event_conflict') from None
        self._db.execute('INSERT INTO journals VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(scope,journal) '
            'DO UPDATE SET manifest=excluded.manifest,last_seq=excluded.last_seq',
            (p.scope, p.room, batch['journal_id'], p.host, batch['source_epoch'], batch['destination_id'], canonical_json(stored_manifest).decode(), last))
        receipt = {key: batch[key] for key in BATCH_IDENTITY}
        receipt.update(through_seq=last, last_event_id=events[-1]['event_id'], commit_id=secrets.token_hex(16))
        self._db.execute('INSERT INTO receipts VALUES(?,?,?,?,?)',
            (p.scope, batch['journal_id'], batch['batch_id'], batch['batch_sha256'], canonical_json(receipt).decode()))
        return dict(receipt=receipt)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Independent private journal service behind a TLS proxy')
    parser.add_argument('--root', required=True)
    parser.add_argument('--auth-file', required=True)
    parser.add_argument('--port', type=int, default=8794)
    parser.add_argument('--origin', action='append', default=[])
    args = parser.parse_args(argv)
    checked_integer(args.port, 1, 65535)
    service = JournalService(args.root, args.auth_file, origins=args.origin)
    async def run():
        async with serve(service.handle_socket, '127.0.0.1', args.port,
                max_size=MAX_ENVELOPE, max_queue=8, compression=None,
                ping_interval=20, ping_timeout=20):
            await asyncio.Future()
    try:
        asyncio.run(run())
    finally:
        service.close()


if __name__ == '__main__':
    main()
