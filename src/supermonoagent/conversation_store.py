"""Private, immutable per-PTY journals. Callers authenticate before writer APIs.

Reader APIs deliberately repeat owner and room checks for closed archives. Bodies
are SQLite blobs, never interpolated into filenames, diagnostics or credentials.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
import uuid

from .conversation_contract import (CONTENT_CHUNK, EVENT_KINDS, MAX_ENVELOPE,
    MIRROR_BATCH_EVENTS, PAGE_DEFAULT, PAGE_MAX, PREVIEW_CHARS, SCHEMA_VERSION,
    JournalError, batch_digest, canonical_json, checked_id, checked_integer,
    checked_label, normalize_destination, validate_receipt)
from .file_transfer import _plain_directory
from .file_companion_manager import (_secure as _secure_storage,
    _private as _check_private_storage)


_EVENT_FIELDS = frozenset({'kind', 'actor', 'origin', 'binding_id', 'provider_thread_id',
    'provider_turn_id', 'provider_item_id', 'source_record_id', 'source_created_at',
    'observed_at', 'state', 'proof', 'supersedes_event_id', 'metadata'})
_PRIVATE_KEYS = frozenset({'token', 'authorization', 'password', 'enrollment_key',
    'secret', 'access_token', 'refresh_token', 'cookie', 'credentials', 'auth',
    'host_secret', 'host_proof', 'enrollment_proof', 'api_key', 'client_secret',
    'auth_token', 'token_digest'})


def _no_credentials(value):
    pending = [(value, 0)]
    seen = 0
    while pending:
        node, depth = pending.pop()
        seen += 1
        if depth > 32 or seen > 10000:
            raise JournalError('invalid_metadata')
        if isinstance(node, dict):
            for key, child in node.items():
                if not isinstance(key, str) or key.lower() in _PRIVATE_KEYS:
                    raise JournalError('invalid_metadata')
                pending.append((child, depth + 1))
        elif isinstance(node, list):
            pending.extend((child, depth + 1) for child in node)


def _eligible(event):
    proof = event.get('proof')
    if isinstance(proof, dict):
        proof = proof.get('kind')
    if not isinstance(proof, str):
        return False
    return (event.get('kind') == 'user_input' and event.get('state') == 'accepted'
        and event.get('actor') in {'human', 'user'} and proof in {'app_submission',
            'provider_user_item', 'provider_human_input', 'user_attestation'} or
        event.get('kind') == 'assistant_final' and event.get('state') in {'confirmed', 'completed'}
        and proof in {'provider_final_phase', 'provider_terminal_result', 'user_attestation'})


class JournalStore:
    """One recorder owns a root. All DB operations are thread safe and durable."""

    def __init__(self, root: Path, *, host_installation_id=None, clock=None):
        self.root = Path(os.path.abspath(root))
        self._lock = threading.RLock()
        self._guard_depth = threading.local()
        self._dbs = {}
        self._db_paths = {}
        self._closed = False
        self._clock = clock or time.time
        self._lease = None
        try:
            for path in (*reversed(self.root.parents), self.root):
                if path.exists() or path.is_symlink():
                    _plain_directory(path)
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._protect(self.root, directory=True)
            self._directories = {path: (info.st_dev, info.st_ino)
                for path in (*reversed(self.root.parents), self.root)
                for info in [_plain_directory(path)]}
            # Existing explicit file grants can bypass directory traversal on
            # Windows. Secure canonical sidecars/exports as well as their root.
            for pattern in ['*.history.sqlite', '*.history.sqlite-wal', '*.history.sqlite-shm',
                            '*.conversation.jsonl', '*.conversation.md',
                            '*.conversation.jsonl.tmp', '*.conversation.md.tmp']:
                for path in self.root.glob(pattern):
                    self._protect_file(path)
            lease_path = self.root / '.journal-writer.lock'
            self._plain_file(lease_path)
            self._protect_file(lease_path)
            self._lease = lease_path.open('a+b')
            self._protect_file(lease_path)
            if os.name == 'nt':
                import msvcrt
                self._lease.seek(0)
                msvcrt.locking(self._lease.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            identity = self.root / '.host-installation-id'
            self._protect_file(identity)
            if identity.exists():
                self.host_installation_id = checked_id(identity.read_text(encoding='ascii').strip())
                if host_installation_id is not None and host_installation_id != self.host_installation_id:
                    raise JournalError('host_conflict')
            else:
                self.host_installation_id = checked_id(host_installation_id or str(uuid.uuid4()))
                with identity.open('x', encoding='ascii') as stream:
                    self._protect_file(identity)
                    stream.write(self.host_installation_id)
                    stream.flush()
                    os.fsync(stream.fileno())
            for path in self.root.glob('*.history.sqlite'):
                journal = path.name.removesuffix('.history.sqlite')
                db = self._open(journal)
                manifest = self._manifest(db)
                # A reopened recorder has no evidence that its original PTY died.
                if manifest['terminal_state'] == 'running':
                    manifest.update(terminal_state='unknown', capture_state='suspended', local_error='recorder_restart')
                    with db:
                        self._save_manifest(db, manifest)
                        self._append_status(db, manifest)
        except JournalError:
            self.close()
            raise
        except (OSError, sqlite3.Error, ValueError):
            self.close()
            raise JournalError('storage_error') from None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def _plain_file(self, path):
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0) & 0x400 or info.st_nlink != 1):
                raise JournalError('unsafe_storage')

    def _protect(self, path, *, directory=False):
        try:
            # The existing helper replaces the complete protected DACL, unlike
            # additive grants which leave old explicit Everyone/Users ACEs.
            _secure_storage(path, directory)
            _check_private_storage(path, directory)
        except (OSError, ValueError):
            raise JournalError('private_storage_unavailable') from None

    def _protect_file(self, path):
        self._plain_file(path)
        if path.exists():
            self._protect(path)

    def _check_paths(self):
        if self._closed:
            raise JournalError('store_closed')
        for path, identity in self._directories.items():
            info = _plain_directory(path)
            if (info.st_dev, info.st_ino) != identity:
                raise JournalError('unsafe_storage')

    @contextmanager
    def _guard(self):
        with self._lock:
            depth = getattr(self._guard_depth, 'value', 0)
            try:
                if not depth:
                    self._check_paths()
                self._guard_depth.value = depth + 1
                yield
            except JournalError:
                raise
            except (OSError, sqlite3.Error, ValueError):
                raise JournalError('storage_error') from None
            finally:
                self._guard_depth.value = depth

    @contextmanager
    def _transaction(self, db):
        """Nested appends participate in their caller's one atomic raw batch."""
        if db.in_transaction:
            yield
        else:
            with db:
                db.execute('BEGIN')
                yield

    def _open(self, journal, *, create=False):
        checked_id(journal)
        # General wire IDs permit colons; filenames must never permit Windows
        # alternate data streams or reserved device names.
        if ':' in journal or re.fullmatch(r'CON|PRN|AUX|NUL|CLOCK\$|COM[1-9]|LPT[1-9]', journal.split('.')[0], re.I):
            raise JournalError()
        path = self.root / (journal + '.history.sqlite')
        for suffix in ['', '-wal', '-shm']:
            self._plain_file(Path(str(path) + suffix))
        if journal in self._dbs:
            info = path.stat()
            if (info.st_dev, info.st_ino) != self._db_paths[journal]:
                raise JournalError('unsafe_storage')
            return self._dbs[journal]
        if not create and not path.exists():
            raise JournalError('not_found')
        if not path.exists():
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        for suffix in ['', '-wal', '-shm']:
            self._protect_file(Path(str(path) + suffix))
        db = sqlite3.connect(path, check_same_thread=False)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA foreign_keys=ON')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS manifest (singleton INTEGER PRIMARY KEY CHECK(singleton=1), value BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS content (id TEXT PRIMARY KEY, body BLOB NOT NULL, sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE,
                    source_key TEXT UNIQUE, fingerprint TEXT NOT NULL, descriptor BLOB NOT NULL,
                    content_id TEXT NOT NULL REFERENCES content(id));
                CREATE TABLE IF NOT EXISTS control (key TEXT PRIMARY KEY, value BLOB NOT NULL);
                CREATE TRIGGER IF NOT EXISTS immutable_events_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_events_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_content_update BEFORE UPDATE ON content BEGIN SELECT RAISE(ABORT,'immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_content_delete BEFORE DELETE ON content BEGIN SELECT RAISE(ABORT,'immutable'); END;
            ''')
            self._dbs[journal] = db
            info = path.stat()
            self._db_paths[journal] = (info.st_dev, info.st_ino)
            return db
        except BaseException:
            db.close()
            raise

    def _manifest(self, db):
        row = db.execute('SELECT value FROM manifest WHERE singleton=1').fetchone()
        if row is None:
            raise JournalError('not_found')
        return json.loads(row[0])

    def _save_manifest(self, db, manifest):
        db.execute('INSERT OR REPLACE INTO manifest VALUES(1,?)', (canonical_json(manifest),))

    def _control(self, db, key, default=None):
        row = db.execute('SELECT value FROM control WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set_control(self, db, key, value):
        db.execute('INSERT OR REPLACE INTO control VALUES(?,?)', (key, canonical_json(value)))

    def _append_status(self, db, manifest):
        """Lifecycle is part of the replicated immutable sequence, not an ACK hint."""
        state = {key: manifest[key] for key in ['terminal_state', 'capture_state',
            'terminal_ended_at', 'sealed_at', 'local_error', 'coverage']}
        descriptor = {key: None for key in _EVENT_FIELDS}
        descriptor.update(event_id=str(uuid.uuid4()), seq=manifest['last_seq'] + 1,
            kind='status', actor='system', origin='recorder', state=manifest['capture_state'],
            observed_at=self._clock(), metadata={'journal_state': state}, content_id=str(uuid.uuid4()),
            content_bytes=0, content_sha256=hashlib.sha256(b'').hexdigest(), encoding='utf-8', preview='')
        db.execute('INSERT INTO content VALUES(?,?,?)', (descriptor['content_id'], b'', descriptor['content_sha256']))
        db.execute('INSERT INTO events VALUES(?,?,?,?,?,?)', (descriptor['seq'], descriptor['event_id'],
            None, hashlib.sha256(canonical_json(descriptor)).hexdigest(), canonical_json(descriptor), descriptor['content_id']))
        manifest['last_seq'] = descriptor['seq']
        self._save_manifest(db, manifest)

    def _authorize(self, db, owner, rooms):
        manifest = self._manifest(db)
        if manifest['owner_scope'] != owner or rooms is not None and manifest['room'] not in rooms:
            raise JournalError('not_found')
        return manifest

    def _cursor(self, journal, projection, through):
        return base64.urlsafe_b64encode(canonical_json([journal, projection, through])).decode('ascii').rstrip('=')

    def _read_cursor(self, cursor, journal, projection):
        if cursor is None:
            return 0
        if not isinstance(cursor, str) or len(cursor) > 512:
            raise JournalError('invalid_cursor')
        try:
            value = json.loads(base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True))
            if not isinstance(value, list) or len(value) != 3 or value[:2] != [journal, projection]:
                raise JournalError('invalid_cursor')
            return checked_integer(value[2])
        except (ValueError, TypeError, UnicodeError):
            raise JournalError('invalid_cursor') from None

    def create(self, owner_scope, *, room, window_id, session_id, source_epoch,
               process_generation, title='', agent='', coverage='full', journal_id=None,
               host_installation_id=None):
        checked_id(owner_scope)
        for value in [window_id, session_id, source_epoch, process_generation]:
            checked_id(value)
        checked_label(room)
        checked_label(title, 1024, empty=True)
        checked_label(agent, 128, empty=True)
        checked_label(coverage, 128)
        host = checked_id(host_installation_id or self.host_installation_id)
        journal = checked_id(journal_id or str(uuid.uuid4()))
        with self._guard():
            db = self._open(journal, create=True)
            if db.execute('SELECT 1 FROM manifest').fetchone():
                old = self._manifest(db)
                binding = dict(owner_scope=owner_scope, room=room, window_id=window_id,
                    session_id=session_id, source_epoch=source_epoch,
                    process_generation=process_generation, host_installation_id=host)
                if any(old[key] != value for key, value in binding.items()):
                    raise JournalError('journal_conflict')
                if old['terminal_state'] in {'unknown', 'running'}:
                    old.update(terminal_state='running', capture_state='capturing')
                    with db:
                        self._save_manifest(db, old)
                        self._append_status(db, old)
                return old
            now = self._clock()
            manifest = dict(schema_version=SCHEMA_VERSION, journal_id=journal,
                owner_scope=owner_scope, host_installation_id=host, source_epoch=source_epoch,
                window_id=window_id, session_id=session_id, process_generation=process_generation,
                room=room, title=title, agent=agent, created_at=now, capture_started_at=now,
                coverage=coverage, terminal_state='running', capture_state='capturing',
                terminal_ended_at=None, sealed_at=None, last_seq=0, local_error=None,
                mirror=dict(destination=None, through_seq=0, pending_batch=None, last_receipt=None))
            with db:
                self._save_manifest(db, manifest)
                self._append_status(db, manifest)
            return manifest

    def append_raw(self, journal_id, data: bytes, *, source_record_id=None, observed_at=None, metadata=None):
        if not isinstance(data, bytes):
            raise JournalError()
        event = dict(kind='raw_output', actor='terminal', origin='pty',
            source_record_id=source_record_id, metadata=metadata or {})
        if observed_at is not None:
            event['observed_at'] = observed_at
        return self.append_event(journal_id, event, data)

    def append_event(self, journal_id, event: dict, content: bytes | str = b''):
        return self._append_event(journal_id, event, content)

    def append_raw_many(self, journal_id, records):
        """Internal writer API: up to 64 original chunks/256 KiB in one commit.

        IDs, descriptors and bytes stay separate; failure rolls back every new
        record in this transaction, including rows preceding a conflicting ID.
        """
        if (not isinstance(records, (list, tuple)) or not 1 <= len(records) <= 64
                or any(not isinstance(record, dict) or set(record) -
                    {'data', 'source_record_id', 'observed_at', 'metadata'}
                    or not isinstance(record.get('data'), bytes) for record in records)
                or sum(len(record['data']) for record in records) > 256 * 1024):
            raise JournalError('invalid_batch')
        with self._guard():
            db = self._open(journal_id)
            with self._transaction(db):
                events = []
                for record in records:
                    event = dict(kind='raw_output', actor='terminal', origin='pty',
                        source_record_id=record.get('source_record_id'), metadata=record.get('metadata') or {})
                    if record.get('observed_at') is not None:
                        event['observed_at'] = record['observed_at']
                    events.append(self._append_event(journal_id, event, record['data'], database=db))
                return events

    def _append_event(self, journal_id, event, content, *, repair=False, database=None):
        if not isinstance(event, dict) or set(event) - _EVENT_FIELDS or event.get('kind') not in EVENT_KINDS:
            raise JournalError()
        if isinstance(content, str):
            try:
                raw = content.encode('utf-8')
            except UnicodeError:
                raise JournalError() from None
            encoding = 'utf-8'
        elif isinstance(content, bytes):
            raw = content
            try:
                raw.decode('utf-8')
                encoding = 'utf-8'
            except UnicodeError:
                encoding = 'binary'
        else:
            raise JournalError()
        immutable = dict(event)
        for key in ['binding_id', 'provider_thread_id', 'provider_turn_id', 'provider_item_id', 'source_record_id', 'supersedes_event_id']:
            if immutable.get(key) is not None:
                checked_id(immutable[key], 512)
        for key in ['actor', 'origin', 'state']:
            if immutable.get(key) is not None:
                checked_label(immutable[key], 128, empty=True)
        _no_credentials(immutable.get('metadata', {}))
        _no_credentials(immutable.get('proof', {}))
        if not isinstance(immutable.get('metadata', {}), dict):
            raise JournalError('invalid_metadata')
        digest = hashlib.sha256(raw).hexdigest()
        # Host observation time is not source identity: retries may arrive later.
        fingerprint = hashlib.sha256(canonical_json({key: value for key, value in immutable.items()
            if key != 'observed_at'}) + b'\0' + raw).hexdigest()
        source = immutable.get('source_record_id')
        source_key = canonical_json([immutable.get('binding_id'), immutable.get('origin'),
            immutable.get('provider_thread_id'), source]).decode() if source else None
        with self._guard():
            db = database if database is not None else self._open(journal_id)
            manifest = self._manifest(db)
            if source_key:
                previous = db.execute('SELECT fingerprint,descriptor FROM events WHERE source_key=?', (source_key,)).fetchone()
                if previous:
                    if previous[0] != fingerprint:
                        raise JournalError('source_conflict')
                    return json.loads(previous[1])
            if manifest['capture_state'] in {'sealed', 'incomplete'} and not repair:
                raise JournalError('journal_sealed')
            descriptor = {key: immutable.get(key) for key in _EVENT_FIELDS}
            descriptor.update(event_id=str(uuid.uuid4()), seq=manifest['last_seq'] + 1,
                observed_at=immutable.get('observed_at', self._clock()),
                metadata=immutable.get('metadata', {}), proof=immutable.get('proof'),
                content_id=str(uuid.uuid4()), content_bytes=len(raw), content_sha256=digest,
                encoding=encoding, preview='' if event['kind'] == 'raw_output' else
                    raw.decode('utf-8', errors='replace')[:PREVIEW_CHARS])
            if len(canonical_json(descriptor)) > MAX_ENVELOPE // 4:
                raise JournalError('event_too_large')
            manifest['last_seq'] = descriptor['seq']
            if event['kind'] == 'capture_gap':
                manifest['local_error'] = 'capture_gap'
            with self._transaction(db):
                db.execute('INSERT INTO content VALUES(?,?,?)', (descriptor['content_id'], raw, digest))
                db.execute('INSERT INTO events VALUES(?,?,?,?,?,?)', (descriptor['seq'], descriptor['event_id'],
                    source_key, fingerprint, canonical_json(descriptor), descriptor['content_id']))
                self._save_manifest(db, manifest)
                if event['kind'] == 'capture_gap':
                    self._append_status(db, manifest)
            return descriptor

    def authorize_pending_repair(self, journal_id, *, binding_id, provider_thread_id,
                                 turn_ids, deadline, source_record_ids):
        """Freeze an already known provider boundary before PTY EOF.

        This trusted method is not a viewer action. The source registry verifies
        its application capability and generation before calling it.
        """
        checked_id(binding_id, 512)
        checked_id(provider_thread_id, 512)
        if (not isinstance(turn_ids, (list, tuple, set)) or not 1 <= len(turn_ids) <= 256
                or not isinstance(source_record_ids, (list, tuple, set))
                or not 1 <= len(source_record_ids) <= 1024
                or type(deadline) not in (int, float) or not math.isfinite(deadline)):
            raise JournalError('invalid_repair')
        for value in [*turn_ids, *source_record_ids]:
            checked_id(value, 512)
        policy = dict(binding_id=binding_id, provider_thread_id=provider_thread_id,
                      turn_ids=sorted(set(turn_ids)), source_record_ids=sorted(set(source_record_ids)), deadline=deadline)
        with self._guard():
            db = self._open(journal_id)
            manifest = self._manifest(db)
            old = self._control(db, 'pending_repair')
            if old is not None:
                if old != policy:
                    raise JournalError('repair_conflict')
                return
            if (manifest['terminal_state'] not in {'running', 'unknown'}
                    or not self._clock() < deadline <= self._clock() + 86400):
                raise JournalError('invalid_repair')
            with db:
                self._set_control(db, 'pending_repair', policy)

    def reconcile_event(self, journal_id, event, content=b''):
        """Append one frozen pending-turn result, even after incomplete sealing."""
        with self._guard():
            db = self._open(journal_id)
            manifest = self._manifest(db)
            policy = self._control(db, 'pending_repair')
            if (not isinstance(event, dict) or policy is None
                    or manifest['terminal_state'] not in {'ended', 'interrupted'}
                    or manifest['terminal_ended_at'] is None
                    or self._clock() > min(policy['deadline'], manifest['terminal_ended_at'] + 86400)
                    or event.get('kind') not in {'assistant_final', 'turn_outcome'}
                    or event.get('binding_id') != policy['binding_id']
                    or event.get('provider_thread_id') != policy['provider_thread_id']
                    or event.get('provider_turn_id') not in policy['turn_ids']
                    or event.get('source_record_id') not in policy['source_record_ids']):
                raise JournalError('invalid_repair')
            if event['kind'] == 'assistant_final' and not _eligible(event):
                raise JournalError('invalid_repair')
            descriptor = self._append_event(journal_id, event, content, repair=True)
            manifest = self._manifest(db)
            if manifest['capture_state'] in {'sealed', 'incomplete'}:
                try:
                    self._export(db, manifest)
                except (OSError, UnicodeError, JournalError):
                    manifest['local_error'] = 'export_error'
                    with db:
                        self._save_manifest(db, manifest)
                        self._append_status(db, manifest)
            return descriptor

    def status(self, owner_scope, journal_id, *, rooms=None):
        with self._guard():
            return self._authorize(self._open(journal_id), owner_scope, rooms)

    def list_journals(self, owner_scope, *, rooms=None, cursor=None, limit=PAGE_DEFAULT):
        checked_integer(limit, 1, PAGE_MAX)
        offset = self._read_cursor(cursor, owner_scope, 'list:' + hashlib.sha256(canonical_json(sorted(rooms) if rooms is not None else None)).hexdigest())
        with self._guard():
            manifests = []
            for path in self.root.glob('*.history.sqlite'):
                manifest = self._manifest(self._open(path.name.removesuffix('.history.sqlite')))
                if manifest['owner_scope'] == owner_scope and (rooms is None or manifest['room'] in rooms):
                    manifests.append(manifest)
            manifests.sort(key=lambda item: (item['created_at'], item['journal_id']), reverse=True)
            selected = []
            for manifest in manifests[offset:offset + limit]:
                if len(canonical_json(dict(journals=selected + [manifest], next_cursor='x' * 512))) > MAX_ENVELOPE - 2048:
                    break
                selected.append(manifest)
            end = offset + len(selected)
            return dict(journals=selected, next_cursor=self._cursor(owner_scope,
                'list:' + hashlib.sha256(canonical_json(sorted(rooms) if rooms is not None else None)).hexdigest(), end)
                if end < len(manifests) else None)

    def read_page(self, owner_scope, journal_id, *, rooms=None, cursor=None, limit=PAGE_DEFAULT, projection='history'):
        checked_integer(limit, 1, PAGE_MAX)
        if projection not in {'history', 'conversation'}:
            raise JournalError()
        through = self._read_cursor(cursor, journal_id, projection)
        with self._guard():
            db = self._open(journal_id)
            manifest = self._authorize(db, owner_scope, rooms)
            events = []
            more = False
            for (raw,) in db.execute('SELECT descriptor FROM events WHERE seq>? ORDER BY seq', (through,)):
                event = json.loads(raw)
                if projection == 'conversation' and not _eligible(event):
                    continue
                if len(events) >= limit or len(canonical_json(dict(journal=manifest, events=events + [event], next_cursor='x' * 512))) > MAX_ENVELOPE - 2048:
                    more = True
                    break
                events.append(event)
            return dict(journal=manifest, events=events,
                next_cursor=self._cursor(journal_id, projection, events[-1]['seq']) if more and events else None)

    def read_content(self, owner_scope, journal_id, content_id, *, rooms=None, offset=0, limit=CONTENT_CHUNK):
        checked_integer(offset)
        checked_integer(limit, 1, CONTENT_CHUNK)
        checked_id(content_id)
        with self._guard():
            db = self._open(journal_id)
            self._authorize(db, owner_scope, rooms)
            row = db.execute('SELECT length(body),sha256,substr(body,?,?) FROM content WHERE id=?',
                             (offset + 1, limit, content_id)).fetchone()
            if row is None:
                raise JournalError('not_found')
            if offset > row[0]:
                raise JournalError()
            end = offset + len(row[2])
            return dict(content_id=content_id, offset=offset, data_base64=base64.b64encode(row[2]).decode('ascii'),
                total_bytes=row[0], sha256=row[1], next_offset=end if end < row[0] else None)

    def content_bytes(self, journal_id, content_id):
        checked_id(content_id)
        with self._guard():
            row = self._open(journal_id).execute('SELECT body FROM content WHERE id=?', (content_id,)).fetchone()
            if row is None:
                raise JournalError('not_found')
            return bytes(row[0])

    def terminal_end(self, journal_id, *, outcome='exited', pending=False):
        if outcome not in {'exited', 'interrupted'} or type(pending) is not bool:
            raise JournalError()
        with self._guard():
            db = self._open(journal_id)
            manifest = self._manifest(db)
            if manifest['capture_state'] in {'sealed', 'incomplete'}:
                return manifest
            manifest.update(terminal_state='interrupted' if outcome == 'interrupted' else 'ended',
                capture_state='draining', terminal_ended_at=(manifest['terminal_ended_at']
                    if manifest['terminal_ended_at'] is not None else self._clock()))
            with db:
                self._set_control(db, 'pending', pending)
                self._save_manifest(db, manifest)
                self._append_status(db, manifest)
            return manifest

    def suspend(self, journal_id, *, reason='recorder_stopped'):
        checked_label(reason)
        with self._guard():
            db = self._open(journal_id)
            manifest = self._manifest(db)
            if manifest['capture_state'] not in {'sealed', 'incomplete', 'draining'}:
                manifest['capture_state'] = 'suspended'
                with db:
                    self._set_control(db, 'suspend_reason', reason)
                    self._save_manifest(db, manifest)
                    self._append_status(db, manifest)
            return manifest

    def resume_capture(self, journal_id, *, process_generation, source_epoch):
        """Resume only a positively identified original live PTY generation.

        This is a trusted recorder operation, never a viewer RPC. Its caller must
        possess live-backend evidence; a missing catalog or reconnected socket
        alone does not establish that the original process still exists.
        """
        checked_id(process_generation)
        checked_id(source_epoch)
        with self._guard():
            db = self._open(journal_id)
            manifest = self._manifest(db)
            if (manifest['process_generation'] != process_generation
                    or manifest['source_epoch'] != source_epoch):
                raise JournalError('journal_conflict')
            if (manifest['terminal_state'] not in {'running', 'unknown'}
                    or manifest['capture_state'] in {'sealed', 'incomplete'}):
                raise JournalError('terminal_ended')
            if manifest['terminal_state'] == 'running' and manifest['capture_state'] == 'capturing':
                return manifest
            # A resumed writer cannot erase a previously observed recorder gap.
            manifest.update(terminal_state='running', capture_state='capturing')
            with db:
                self._save_manifest(db, manifest)
                self._append_status(db, manifest)
            return manifest

    def settle_pending(self, journal_id):
        """Trusted registry checkpoint after all known outcomes were committed.

        This does not infer EOF or start another end transition. The caller must
        have authoritative registry evidence that no semantic work is pending.
        """
        with self._guard():
            db = self._open(journal_id)
            manifest = self._manifest(db)
            if (manifest['terminal_state'] not in {'ended', 'interrupted'}
                    or manifest['capture_state'] != 'draining'):
                raise JournalError('invalid_lifecycle')
            if self._control(db, 'pending', False):
                with db:
                    self._set_control(db, 'pending', False)
                    self._append_status(db, manifest)
            return manifest

    def seal(self, journal_id, *, incomplete=False):
        if type(incomplete) is not bool:
            raise JournalError()
        with self._guard():
            db = self._open(journal_id)
            manifest = self._manifest(db)
            if manifest['terminal_state'] not in {'ended', 'interrupted'}:
                raise JournalError('terminal_running')
            if self._control(db, 'pending', False) and not incomplete:
                raise JournalError('capture_pending')
            if manifest['capture_state'] not in {'sealed', 'incomplete'}:
                manifest.update(capture_state='incomplete' if incomplete or manifest['local_error'] else 'sealed',
                                sealed_at=(manifest['sealed_at'] if manifest['sealed_at'] is not None else self._clock()))
                with db:
                    self._save_manifest(db, manifest)
                    self._append_status(db, manifest)
            try:
                self._export(db, manifest)
            except (OSError, UnicodeError, JournalError):
                manifest['local_error'] = 'export_error'
                with db:
                    self._save_manifest(db, manifest)
                    self._append_status(db, manifest)
            return manifest

    def _export(self, db, manifest):
        journal = manifest['journal_id']
        json_path = self.root / (journal + '.conversation.jsonl')
        md_path = self.root / (journal + '.conversation.md')
        for path in (json_path, md_path):
            self._protect_file(path)
            self._protect_file(path.with_suffix(path.suffix + '.tmp'))
        header = dict(schema_version=SCHEMA_VERSION, journal_id=journal, through_seq=manifest['last_seq'], derivative=True)
        json_tmp = json_path.with_suffix(json_path.suffix + '.tmp')
        md_tmp = md_path.with_suffix(md_path.suffix + '.tmp')
        with json_tmp.open('wb') as js, md_tmp.open('wb') as md:
            self._protect_file(json_tmp)
            self._protect_file(md_tmp)
            js.write(canonical_json(header) + b'\n')
            md.write(('# Conversation\n\nThrough sequence: ' + str(manifest['last_seq']) + '\n\n').encode())
            for (raw,) in db.execute('SELECT descriptor FROM events ORDER BY seq'):
                event = json.loads(raw)
                if not _eligible(event):
                    continue
                body = db.execute('SELECT body FROM content WHERE id=?', (event['content_id'],)).fetchone()[0]
                text = body.decode('utf-8') if event['encoding'] == 'utf-8' else None
                record = {**event, 'text': text}
                if text is None:
                    record['data_base64'] = base64.b64encode(body).decode('ascii')
                js.write(canonical_json(record) + b'\n')
                md.write(('## ' + (event['actor'] or event['kind']) + '\n\n').encode())
                md.write(body if text is not None else b'[Binary body retained in JSONL export]')
                md.write(b'\n\n')
            for stream in (js, md):
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(json_tmp, json_path)
        os.replace(md_tmp, md_path)

    def pin_mirror(self, journal_id, destination: dict):
        destination = normalize_destination(destination)
        with self._guard():
            db = self._open(journal_id)
            manifest = self._manifest(db)
            # Local and central owner scopes are independently configured. The
            # authenticated server derives its owner; only this trusted pin may
            # map scopes, and it remains immutable for the journal lifetime.
            if any(destination[key] != manifest[key] for key in ['host_installation_id', 'source_epoch']):
                raise JournalError('destination_conflict')
            old = manifest['mirror']['destination']
            if old is not None and old != destination:
                raise JournalError('destination_conflict')
            manifest['mirror']['destination'] = destination
            with db:
                self._save_manifest(db, manifest)
            return manifest

    def mirror_batch(self, journal_id, *, limit=MIRROR_BATCH_EVENTS):
        checked_integer(limit, 1, MIRROR_BATCH_EVENTS)
        with self._guard():
            db = self._open(journal_id)
            manifest = self._manifest(db)
            mirror = manifest['mirror']
            destination = mirror['destination']
            if destination is None:
                raise JournalError('mirror_unconfigured')
            if mirror['pending_batch'] is not None:
                return self._control(db, 'pending_batch')
            through = mirror['through_seq']
            events, contents = [], []
            batch = {key: destination[key] for key in ['destination_id', 'owner_scope', 'external_room', 'host_installation_id', 'source_epoch']}
            batch.update(journal_id=journal_id, batch_id=str(uuid.uuid4()), first_seq=through + 1, last_seq=through,
                         events=events, contents=contents)
            for (raw,) in db.execute('SELECT descriptor FROM events WHERE seq>? ORDER BY seq LIMIT ?', (through, limit)):
                event = json.loads(raw)
                descriptor = dict(content_id=event['content_id'], bytes=event['content_bytes'], sha256=event['content_sha256'])
                events.append(event)
                contents.append(descriptor)
                batch['last_seq'] = event['seq']
                if len(canonical_json(batch)) > MAX_ENVELOPE - 20 * 1024:
                    events.pop()
                    contents.pop()
                    batch['last_seq'] -= 1
                    break
            if not events:
                return None
            batch['batch_sha256'] = batch_digest(batch)
            mirror['pending_batch'] = {key: batch[key] for key in ['batch_id', 'first_seq', 'last_seq', 'batch_sha256']}
            with db:
                self._set_control(db, 'pending_batch', batch)
                self._save_manifest(db, manifest)
            return batch

    def accept_receipt(self, journal_id, receipt: dict):
        with self._guard():
            db = self._open(journal_id)
            manifest = self._manifest(db)
            mirror = manifest['mirror']
            batch = self._control(db, 'pending_batch')
            if batch is None:
                if mirror['last_receipt'] == receipt and receipt is not None:
                    return manifest
                raise JournalError('invalid_receipt')
            validate_receipt(batch, receipt)
            if batch['first_seq'] != mirror['through_seq'] + 1:
                raise JournalError('invalid_receipt')
            destination = mirror['destination']
            if any(batch[key] != destination[key] for key in ['destination_id', 'owner_scope', 'external_room', 'host_installation_id', 'source_epoch']):
                raise JournalError('invalid_receipt')
            mirror.update(through_seq=receipt['through_seq'], pending_batch=None, last_receipt=receipt)
            with db:
                self._set_control(db, 'pending_batch', None)
                self._save_manifest(db, manifest)
            return manifest

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for db in self._dbs.values():
                db.close()
            self._dbs.clear()
            self._db_paths.clear()
            if self._lease is not None:
                self._lease.close()
                self._lease = None
