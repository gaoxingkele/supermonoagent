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


def managed_creation_ids(host_installation_id, owner_scope, nonce):
    """Parameter-independent durable lookup; anchor/provider belong in a digest."""
    for value in [host_installation_id, owner_scope]:
        checked_id(value)
    checked_id(nonce, 64)
    key = uuid.uuid5(uuid.NAMESPACE_URL, canonical_json(
        ['sma-managed-create-1', host_installation_id, owner_scope, nonce]).decode('utf-8'))
    return dict(creation_key=str(key), **{field: str(uuid.uuid5(key, label)) for field, label in
        [('journal_id', 'journal'), ('window_id', 'window'), ('session_id', 'session'),
         ('process_generation', 'process'), ('binding_id', 'binding')]})


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
                creation = self._control(db, 'managed_creation')
                # A reopened recorder has no evidence that its original PTY died.
                if (manifest['terminal_state'] == 'running' or creation is not None
                        and manifest['terminal_state'] not in {'ended', 'interrupted'}):
                    with db:
                        if manifest['terminal_state'] == 'running':
                            manifest.update(terminal_state='unknown', capture_state='suspended', local_error='recorder_restart')
                        if creation is not None:
                            creation['state'] = 'ambiguous'
                            self._set_control(db, 'managed_creation', creation)
                            active_id = self._control(db, 'managed_active')
                            if active_id:
                                active = self._managed_submission(db, active_id)
                                active['dispatch_state'] = 'ambiguous'
                                self._save_managed_submission(db, active)
                                self._invalidate_managed_permissions(db, active, 'stale')
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
               host_installation_id=None, managed_creation=None):
        checked_id(owner_scope)
        for value in [window_id, session_id, source_epoch, process_generation]:
            checked_id(value)
        checked_label(room)
        checked_label(title, 1024, empty=True)
        checked_label(agent, 128, empty=True)
        checked_label(coverage, 128)
        host = checked_id(host_installation_id or self.host_installation_id)
        creation = None
        if managed_creation is not None:
            if (not isinstance(managed_creation, dict) or set(managed_creation) != {'nonce', 'request'}
                    or host != self.host_installation_id):
                raise JournalError('creation_conflict')
            ids = managed_creation_ids(host, owner_scope, managed_creation['nonce'])
            request = json.loads(canonical_json(managed_creation['request']))
            if (not isinstance(request, dict) or set(request) != {'anchor_window_id', 'anchor_session_id',
                    'anchor_process_generation', 'provider', 'cwd', 'room'}
                    or request.get('provider') not in {'claude', 'codex'} or request.get('room') != room):
                raise JournalError('creation_conflict')
            for key in ['anchor_window_id', 'anchor_session_id', 'anchor_process_generation']:
                checked_id(request[key])
            checked_label(request['cwd'], 32768)
            if (window_id != ids['window_id'] or session_id != ids['session_id']
                    or process_generation != ids['process_generation']
                    or journal_id is not None and journal_id != ids['journal_id']):
                raise JournalError('creation_conflict')
            journal_id = ids['journal_id']
            creation = dict(schema=1, nonce=managed_creation['nonce'], creation_key=ids['creation_key'],
                binding_id=ids['binding_id'], request=request,
                request_sha256=hashlib.sha256(canonical_json(request)).hexdigest(), state='starting')
        journal = checked_id(journal_id or str(uuid.uuid4()))
        with self._guard():
            db = self._open(journal, create=True)
            if db.execute('SELECT 1 FROM manifest').fetchone():
                old = self._manifest(db)
                prior = self._control(db, 'managed_creation')
                if creation is not None or prior is not None:
                    if (creation is None or prior is None or prior['creation_key'] != creation['creation_key']
                            or prior['request_sha256'] != creation['request_sha256']):
                        raise JournalError('creation_conflict')
                binding = dict(owner_scope=owner_scope, room=room, window_id=window_id,
                    session_id=session_id, source_epoch=source_epoch,
                    process_generation=process_generation, host_installation_id=host)
                if any(old[key] != value for key, value in binding.items()):
                    raise JournalError('journal_conflict')
                if prior is not None:
                    return old  # A known creation never resumes or launches itself.
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
                if creation is not None:
                    self._set_control(db, 'managed_creation', creation)
                    self._set_control(db, 'managed_provider', None)
                    self._set_control(db, 'managed_active', None)
                self._save_manifest(db, manifest)
                self._append_status(db, manifest)
            return manifest

    def _managed_context(self, db, owner_scope, journal_id, target, rooms):
        manifest = self._authorize(db, owner_scope, rooms)
        fields = {'window_id', 'session_id', 'process_generation', 'journal_id'}
        if (not isinstance(target, dict) or set(target) != fields
                or any(target[key] != manifest[key] for key in fields)
                or target['journal_id'] != journal_id):
            raise JournalError('stale_target')
        creation = self._control(db, 'managed_creation')
        if creation is None:
            raise JournalError('not_managed')
        return manifest, creation

    def _managed_live(self, manifest, creation):
        if creation['state'] == 'ambiguous':
            raise JournalError('managed_ambiguous')
        if (manifest['terminal_state'] != 'running' or manifest['capture_state'] != 'capturing'
                or manifest['local_error'] is not None):
            raise JournalError('managed_unavailable')

    def _managed_submission(self, db, submission_id):
        checked_id(submission_id)
        nonce = self._control(db, 'managed_submission_id:' + submission_id)
        row = self._control(db, 'managed_submission:' + nonce) if nonce else None
        if row is None:
            raise JournalError('stale_submission')
        return row

    def _save_managed_submission(self, db, submission):
        self._set_control(db, 'managed_submission:' + submission['nonce'], submission)

    def _managed_active(self, db, submission_id):
        if self._control(db, 'managed_active') != submission_id:
            raise JournalError('stale_submission')
        return self._managed_submission(db, submission_id)

    def _invalidate_managed_permissions(self, db, active, state):
        for permission_id in active['permission_ids']:
            permission = self._control(db, 'managed_permission:' + permission_id)
            if permission is not None and permission['state'] in {'pending', 'decided'}:
                permission['state'] = state
                self._set_control(db, 'managed_permission:' + permission_id, permission)

    def _managed_event(self, db, manifest, creation, *, kind, source_id, submission=None,
                       content=b'', proof=None, state=None, metadata=None, provider_item_id=None):
        provider = self._control(db, 'managed_provider') or {}
        return self._append_event(manifest['journal_id'], dict(kind=kind,
            actor='human' if kind == 'user_input' else 'assistant' if kind == 'assistant_final' else 'system',
            origin='managed', binding_id=creation['binding_id'],
            provider_thread_id=provider.get('provider_root_id'),
            provider_turn_id=submission.get('provider_turn_id') if submission else None,
            provider_item_id=provider_item_id, source_record_id=source_id,
            state=state, proof=proof, metadata=metadata or {}), content, database=db)

    def get_managed_creation(self, owner_scope, journal_id, *, rooms=None):
        with self._guard():
            db = self._open(journal_id)
            self._authorize(db, owner_scope, rooms)
            return self._control(db, 'managed_creation')

    def confirm_managed_provider(self, owner_scope, journal_id, *, target, provider,
                                 provider_version, provider_root_id, policy=None,
                                 confirmed=True, rooms=None):
        if provider not in {'claude', 'codex'} or type(confirmed) is not bool:
            raise JournalError('invalid_provider')
        checked_id(provider_root_id, 512)
        checked_label(provider_version, 64)
        policy = {} if policy is None else json.loads(canonical_json(policy))
        if not isinstance(policy, dict) or len(canonical_json(policy)) > 4096:
            raise JournalError('invalid_policy')
        _no_credentials(policy)
        pending = [policy]
        while pending:
            node = pending.pop()
            if isinstance(node, dict):
                if set(node) & {'input', 'arguments', 'tool_args', 'details', 'messages', 'control', 'environment'}:
                    raise JournalError('invalid_policy')
                pending.extend(node.values())
            elif isinstance(node, list):
                pending.extend(node)
        with self._guard():
            db = self._open(journal_id)
            manifest, creation = self._managed_context(db, owner_scope, journal_id, target, rooms)
            self._managed_live(manifest, creation)
            if provider != creation['request']['provider']:
                raise JournalError('binding_conflict')
            prior = self._control(db, 'managed_provider')
            descriptor = dict(provider=provider, provider_version=provider_version,
                provider_root_id=provider_root_id if confirmed else None,
                requested_root_id=provider_root_id, confirmed=confirmed, policy=policy)
            if prior is not None:
                if (any(prior[key] != descriptor[key] for key in
                        ['provider', 'provider_version', 'requested_root_id', 'policy'])
                        or prior['confirmed'] and not confirmed):
                    raise JournalError('binding_conflict')
                if prior == descriptor:
                    return prior
            with self._transaction(db):
                self._set_control(db, 'managed_provider', descriptor)
                creation['state'] = 'ready'
                self._set_control(db, 'managed_creation', creation)
                self._managed_event(db, manifest, creation, kind='binding',
                    source_id='managed-provider:' + ('confirmed' if confirmed else 'requested'),
                    state='confirmed' if confirmed else 'requested', metadata=dict(
                        provider=provider, provider_version=provider_version,
                        provider_root_id=descriptor['provider_root_id']))
            return descriptor

    def read_managed_control(self, owner_scope, journal_id, *, target, nonce=None, rooms=None):
        if nonce is not None:
            checked_id(nonce, 64)
        with self._guard():
            db = self._open(journal_id)
            manifest, creation = self._managed_context(db, owner_scope, journal_id, target, rooms)
            provider = self._control(db, 'managed_provider')
            active_id = self._control(db, 'managed_active')
            active = self._managed_submission(db, active_id) if active_id else None
            permissions = []
            if active:
                for permission_id in active['permission_ids']:
                    permission = self._control(db, 'managed_permission:' + permission_id)
                    if permission is not None:
                        if permission['expires_at'] <= self._clock() and permission['state'] in {'pending', 'decided'}:
                            permission['state'] = 'expired'
                        permissions.append(permission)
            state = ('ended' if manifest['terminal_state'] in {'ended', 'interrupted'} else
                'ambiguous' if creation['state'] == 'ambiguous' else
                'error' if manifest['local_error'] else
                'interrupting' if active and active['cancel_requested'] else
                'waiting_permission' if any(item['state'] == 'pending' for item in permissions) else
                'running' if active else 'ready' if provider else 'starting')
            submission = self._control(db, 'managed_submission:' + nonce) if nonce else None
            return dict(state=state, creation=creation, provider=provider, active_submission=active,
                permissions=permissions, receipt=submission['receipt'] if submission else None)

    def accept_managed_submission(self, owner_scope, journal_id, *, target, nonce, text, rooms=None):
        checked_id(nonce, 64)
        if not isinstance(text, str) or not text:
            raise JournalError('invalid_prompt')
        try:
            raw = text.encode('utf-8')
        except UnicodeError:
            raise JournalError('invalid_prompt') from None
        if len(raw) > 65536:
            raise JournalError('prompt_too_large')
        digest = hashlib.sha256(raw).hexdigest()
        with self._guard():
            db = self._open(journal_id)
            manifest, creation = self._managed_context(db, owner_scope, journal_id, target, rooms)
            prior = self._control(db, 'managed_submission:' + nonce)
            if prior is not None:
                if prior['body_sha256'] != digest:
                    raise JournalError('nonce_conflict')
                return dict(prior['receipt'], replayed=True)
            self._managed_live(manifest, creation)
            if self._control(db, 'managed_provider') is None:
                raise JournalError('managed_not_ready')
            if self._control(db, 'managed_active') is not None:
                raise JournalError('managed_busy')
            submission_id = str(uuid.uuid5(uuid.UUID(journal_id), 'submission:' + nonce))
            user_uuid = str(uuid.uuid5(uuid.UUID(submission_id), 'provider-user'))
            submission = dict(submission_id=submission_id, user_message_uuid=user_uuid, nonce=nonce,
                body_sha256=digest, dispatch_state='intent', cancel_requested=False, cancel_version=0,
                provider_turn_id=None, permission_ids=[], outcome=None, settlement=None)
            with self._transaction(db):
                event = self._managed_event(db, manifest, creation, kind='user_input',
                    source_id='managed-input:' + submission_id, submission=submission,
                    content=text, state='accepted', proof=dict(kind='app_submission', submission_id=submission_id,
                        user_message_uuid=user_uuid, process_generation=manifest['process_generation']))
                receipt = dict(target, submission_id=submission_id, user_message_uuid=user_uuid,
                    nonce=nonce, event_id=event['event_id'], seq=event['seq'], state='accepted',
                    dispatch_state='intent', cancel_version=0)
                submission.update(event_id=event['event_id'], seq=event['seq'], content_id=event['content_id'], receipt=receipt)
                self._save_managed_submission(db, submission)
                self._set_control(db, 'managed_submission_id:' + submission_id, nonce)
                self._set_control(db, 'managed_active', submission_id)
            return dict(receipt, replayed=False)

    def mark_managed_dispatching(self, owner_scope, journal_id, *, target,
                                submission_id, cancel_version, rooms=None):
        checked_integer(cancel_version)
        with self._guard():
            db = self._open(journal_id)
            manifest, creation = self._managed_context(db, owner_scope, journal_id, target, rooms)
            self._managed_live(manifest, creation)
            active = self._managed_active(db, submission_id)
            if (active['cancel_requested'] or active['cancel_version'] != cancel_version
                    or active['dispatch_state'] != 'intent'):
                raise JournalError('stale_dispatch')
            active['dispatch_state'] = 'dispatching'
            with self._transaction(db):
                self._save_managed_submission(db, active)
                self._managed_event(db, manifest, creation, kind='status', source_id='managed-dispatch:' + submission_id,
                    submission=active, state='dispatching', metadata={'submission_id': submission_id})
            return active

    def associate_managed_turn(self, owner_scope, journal_id, *, target, submission_id,
                               user_message_uuid, provider_root_id, provider_turn_id, rooms=None):
        checked_id(provider_turn_id, 512)
        with self._guard():
            db = self._open(journal_id)
            manifest, creation = self._managed_context(db, owner_scope, journal_id, target, rooms)
            if creation['state'] == 'ambiguous':
                raise JournalError('managed_ambiguous')
            active = self._managed_active(db, submission_id)
            provider = self._control(db, 'managed_provider')
            if (provider is None or not provider['confirmed'] or provider['provider_root_id'] != provider_root_id
                    or active['user_message_uuid'] != user_message_uuid
                    or active['dispatch_state'] not in {'dispatching', 'running'}
                    or active['provider_turn_id'] is not None and active['provider_turn_id'] != provider_turn_id):
                raise JournalError('binding_conflict')
            if active['provider_turn_id'] == provider_turn_id:
                return active
            active.update(provider_turn_id=provider_turn_id, dispatch_state='running')
            with self._transaction(db):
                self._save_managed_submission(db, active)
                self._managed_event(db, manifest, creation, kind='binding',
                    source_id='managed-turn:' + submission_id, submission=active, state='associated',
                    metadata=dict(submission_id=submission_id, user_message_uuid=user_message_uuid))
            return active

    def _settle_managed(self, db, manifest, creation, active, *, outcome, finals, reason, digest):
        event_ids = []
        provider = self._control(db, 'managed_provider') or {}
        for final in finals:
            proof = dict(final['proof'])
            canonical = dict(provider=provider['provider'], provider_version=provider['provider_version'],
                binding_id=creation['binding_id'], provider_root_id=provider['provider_root_id'],
                process_generation=manifest['process_generation'], submission_id=active['submission_id'],
                user_message_uuid=active['user_message_uuid'], provider_turn_id=active['provider_turn_id'],
                provider_record_id=final['provider_record_id'], terminal_status='completed')
            if any(key in proof and proof[key] != value for key, value in canonical.items()):
                raise JournalError('invalid_proof')
            proof.update(canonical)
            event = self._managed_event(db, manifest, creation, kind='assistant_final',
                source_id='managed-final:' + active['submission_id'] + ':' + final['provider_record_id'],
                submission=active, provider_item_id=final['provider_record_id'], content=final['text'],
                state='completed', proof=proof)
            event_ids.append(event['event_id'])
        outcome_event = self._managed_event(db, manifest, creation, kind='turn_outcome',
            source_id='managed-outcome:' + active['submission_id'], submission=active, state=outcome,
            metadata=dict(submission_id=active['submission_id'], outcome=outcome, reason=reason))
        result = dict(submission_id=active['submission_id'], outcome=outcome,
            event_ids=event_ids, outcome_event_id=outcome_event['event_id'], seq=outcome_event['seq'])
        active.update(outcome=outcome, settlement=result, settlement_sha256=digest,
                      dispatch_state='not_dispatched' if reason == 'not_dispatched' else 'settled')
        self._save_managed_submission(db, active)
        self._invalidate_managed_permissions(db, active, 'settled')
        self._set_control(db, 'managed_active', None)
        return result

    def settle_managed_submission(self, owner_scope, journal_id, *, target, submission_id,
                                  user_message_uuid, provider_root_id, provider_turn_id=None,
                                  outcome, finals=(), reason=None, rooms=None):
        if outcome not in {'completed', 'failed', 'interrupted', 'blocked', 'ambiguous'}:
            raise JournalError('invalid_outcome')
        if reason is not None:
            if not isinstance(reason, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', reason):
                raise JournalError('invalid_outcome')
        if not isinstance(finals, (list, tuple)) or len(finals) > 32 or outcome != 'completed' and finals:
            raise JournalError('invalid_proof')
        finals = json.loads(canonical_json(finals))
        for final in finals:
            if (not isinstance(final, dict) or set(final) != {'provider_record_id', 'text', 'proof'}
                    or not isinstance(final['text'], str) or not final['text']
                    or not isinstance(final['proof'], dict)
                    or final['proof'].get('kind') not in {'provider_final_phase', 'provider_terminal_result'}):
                raise JournalError('invalid_proof')
            checked_id(final['provider_record_id'], 256)
            _no_credentials(final['proof'])
            proof = final['proof']
            if (set(proof) - {'kind', 'provider', 'provider_version', 'provider_root_id', 'provider_turn_id',
                    'submission_id', 'user_message_uuid', 'client_user_message_id', 'provider_record_id',
                    'phase', 'terminal_status', 'result_uuid', 'result_index', 'conversation_epoch',
                    'terminal_reason', 'num_turns', 'origin', 'binding_id', 'process_generation',
                    'schema', 'schema_version'}
                    or 'phase' in proof and proof['phase'] != 'final_answer'
                    or 'terminal_reason' in proof and proof['terminal_reason'] != 'completed'
                    or 'num_turns' in proof and (type(proof['num_turns']) is not int or proof['num_turns'] <= 0)):
                raise JournalError('invalid_proof')
        if len({final['provider_record_id'] for final in finals}) != len(finals):
            raise JournalError('invalid_proof')
        digest = hashlib.sha256(canonical_json(dict(submission_id=submission_id,
            user_message_uuid=user_message_uuid, provider_root_id=provider_root_id,
            provider_turn_id=provider_turn_id, outcome=outcome, finals=finals, reason=reason))).hexdigest()
        with self._guard():
            db = self._open(journal_id)
            manifest, creation = self._managed_context(db, owner_scope, journal_id, target, rooms)
            known = self._managed_submission(db, submission_id)
            if known['settlement'] is not None:
                if known.get('settlement_sha256') != digest:
                    raise JournalError('settlement_conflict')
                return dict(known['settlement'], replayed=True)
            active = self._managed_active(db, submission_id)
            provider = self._control(db, 'managed_provider') or {}
            if (active['user_message_uuid'] != user_message_uuid
                    or active['provider_turn_id'] != provider_turn_id
                    or provider_root_id is not None and provider.get('provider_root_id') != provider_root_id):
                raise JournalError('binding_conflict')
            if finals and (not provider.get('confirmed') or not active['provider_turn_id']
                    or provider_root_id != provider.get('provider_root_id')
                    or creation['state'] == 'ambiguous'):
                raise JournalError('invalid_proof')
            if finals and any(final['proof'].get('client_user_message_id', user_message_uuid) != user_message_uuid
                    or provider.get('provider') == 'claude' and (
                        final['proof']['kind'] != 'provider_terminal_result'
                        or final['proof'].get('origin', 'human') != 'human') for final in finals):
                raise JournalError('invalid_proof')
            with self._transaction(db):
                result = self._settle_managed(db, manifest, creation, active,
                    outcome=outcome, finals=finals, reason=reason, digest=digest)
            return dict(result, replayed=False)

    def cancel_managed_submission(self, owner_scope, journal_id, *, target, submission_id,
                                  nonce, provider_turn_id=None, rooms=None):
        checked_id(nonce, 64)
        request = dict(submission_id=submission_id, provider_turn_id=provider_turn_id)
        with self._guard():
            db = self._open(journal_id)
            manifest, creation = self._managed_context(db, owner_scope, journal_id, target, rooms)
            prior = self._control(db, 'managed_cancel:' + nonce)
            if prior is not None:
                if prior['request'] != request:
                    raise JournalError('nonce_conflict')
                return dict(prior['receipt'], replayed=True)
            active = self._managed_active(db, submission_id)
            if provider_turn_id is not None and provider_turn_id != active['provider_turn_id']:
                raise JournalError('binding_conflict')
            active['cancel_requested'] = True
            active['cancel_version'] += 1
            not_dispatched = active['dispatch_state'] == 'intent'
            receipt = dict(submission_id=submission_id, cancel_version=active['cancel_version'],
                state='not_dispatched' if not_dispatched else 'interrupt_requested')
            with self._transaction(db):
                self._invalidate_managed_permissions(db, active, 'cancelled')
                if not_dispatched:
                    self._settle_managed(db, manifest, creation, active, outcome='interrupted',
                        finals=(), reason='not_dispatched', digest='cancel:' + nonce)
                else:
                    self._save_managed_submission(db, active)
                    self._managed_event(db, manifest, creation, kind='status',
                        source_id='managed-cancel:' + nonce, submission=active, state='interrupt_requested',
                        metadata=dict(submission_id=submission_id, cancel_version=active['cancel_version']))
                self._set_control(db, 'managed_cancel:' + nonce, dict(request=request, receipt=receipt))
            return dict(receipt, replayed=False)

    def register_managed_permission(self, owner_scope, journal_id, *, target,
                                    permission_request_id, submission_id, provider_turn_id=None,
                                    tool_name='', choices=('deny',), requires_user_interaction=False,
                                    expires_at=None, rooms=None):
        checked_id(permission_request_id, 256)
        checked_label(tool_name, 128, empty=True)
        if not isinstance(choices, (list, tuple)) or not all(isinstance(choice, str) for choice in choices):
            raise JournalError('invalid_permission')
        choices = tuple(choices)
        if (not choices or len(choices) != len(set(choices))
                or set(choices) - {'allow_once', 'deny'} or 'deny' not in choices
                or type(requires_user_interaction) is not bool
                or requires_user_interaction and 'allow_once' in choices):
            raise JournalError('invalid_permission')
        now = self._clock()
        expires_at = now + 300 if expires_at is None else expires_at
        if type(expires_at) not in (int, float) or not math.isfinite(expires_at) or not now < expires_at <= now + 300:
            raise JournalError('invalid_permission')
        with self._guard():
            db = self._open(journal_id)
            manifest, creation = self._managed_context(db, owner_scope, journal_id, target, rooms)
            self._managed_live(manifest, creation)
            active = self._managed_active(db, submission_id)
            if active['cancel_requested'] or provider_turn_id != active['provider_turn_id']:
                raise JournalError('stale_permission')
            descriptor = dict(permission_request_id=permission_request_id, submission_id=submission_id,
                provider_turn_id=provider_turn_id, tool_name=tool_name, choices=list(choices),
                requires_user_interaction=requires_user_interaction, expires_at=expires_at,
                cancel_version=active['cancel_version'], state='pending')
            prior = self._control(db, 'managed_permission:' + permission_request_id)
            if prior is not None:
                # A provider replay cannot resurrect an already consumed ID.
                if any(prior[key] != descriptor[key] for key in ['submission_id', 'provider_turn_id',
                        'tool_name', 'choices', 'requires_user_interaction']):
                    raise JournalError('source_conflict')
                return prior
            if len(active['permission_ids']) >= 32:
                raise JournalError('permission_limit')
            active['permission_ids'].append(permission_request_id)
            with self._transaction(db):
                self._set_control(db, 'managed_permission:' + permission_request_id, descriptor)
                self._save_managed_submission(db, active)
                self._managed_event(db, manifest, creation, kind='status',
                    source_id='managed-permission:' + permission_request_id, submission=active,
                    state='waiting_permission', metadata=dict(permission_request_id=permission_request_id,
                        submission_id=submission_id, choices=list(choices)))
            return descriptor

    def withdraw_managed_permission(self, owner_scope, journal_id, *, target,
                                    permission_request_id, rooms=None):
        checked_id(permission_request_id, 256)
        with self._guard():
            db = self._open(journal_id)
            manifest, creation = self._managed_context(db, owner_scope, journal_id, target, rooms)
            permission = self._control(db, 'managed_permission:' + permission_request_id)
            if permission is None:
                raise JournalError('stale_permission')
            if permission['state'] not in {'pending', 'decided'}:
                return permission
            permission['state'] = 'withdrawn'
            with self._transaction(db):
                self._set_control(db, 'managed_permission:' + permission_request_id, permission)
                if self._control(db, 'managed_active') == permission['submission_id']:
                    active = self._managed_submission(db, permission['submission_id'])
                    if permission_request_id in active['permission_ids']:
                        active['permission_ids'].remove(permission_request_id)
                        self._save_managed_submission(db, active)
                self._managed_event(db, manifest, creation, kind='status',
                    source_id='managed-withdraw:' + permission_request_id, state='withdrawn',
                    metadata={'permission_request_id': permission_request_id})
            return permission

    def decide_managed_permission(self, owner_scope, journal_id, *, target,
                                  permission_request_id, submission_id, decision, nonce,
                                  cancel_version, provider_turn_id=None, rooms=None):
        checked_id(nonce, 64)
        checked_id(permission_request_id, 256)
        checked_integer(cancel_version)
        if decision not in {'allow_once', 'deny'}:
            raise JournalError('invalid_permission')
        request = dict(permission_request_id=permission_request_id, submission_id=submission_id,
            provider_turn_id=provider_turn_id, decision=decision, cancel_version=cancel_version)
        with self._guard():
            db = self._open(journal_id)
            manifest, creation = self._managed_context(db, owner_scope, journal_id, target, rooms)
            prior = self._control(db, 'managed_permission_nonce:' + nonce)
            if prior is not None:
                if prior['request'] != request:
                    raise JournalError('nonce_conflict')
                return dict(prior['receipt'], replayed=True)
            self._managed_live(manifest, creation)
            active = self._managed_active(db, submission_id)
            permission = self._control(db, 'managed_permission:' + permission_request_id)
            if (permission is None or permission['state'] != 'pending'
                    or permission['submission_id'] != submission_id
                    or permission['provider_turn_id'] != provider_turn_id
                    or active['provider_turn_id'] != provider_turn_id
                    or active['cancel_requested'] or active['cancel_version'] != cancel_version
                    or permission['cancel_version'] != cancel_version
                    or permission['expires_at'] <= self._clock() or decision not in permission['choices']):
                raise JournalError('stale_permission')
            receipt = dict(request, state='decided')
            permission.update(state='decided', decision=decision)
            with self._transaction(db):
                self._set_control(db, 'managed_permission:' + permission_request_id, permission)
                self._set_control(db, 'managed_permission_nonce:' + nonce, dict(request=request, receipt=receipt))
                self._managed_event(db, manifest, creation, kind='status',
                    source_id='managed-decision:' + nonce, submission=active, state='decided',
                    metadata=dict(permission_request_id=permission_request_id, decision=decision,
                                  submission_id=submission_id, cancel_version=cancel_version))
            return dict(receipt, replayed=False)

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
