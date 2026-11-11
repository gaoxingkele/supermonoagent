"""Host-owned journal lifecycle, authorization boundary and optional mirroring.

PTY threads only enqueue bytes. Provider observation, SQLite reads and network
traffic stay outside the terminal reader. Closing this recorder never ends a CLI.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid

from .conversation_capture import JournalCapture
from .conversation_contract import JournalError, checked_id, normalize_destination
from .conversation_sources import SourceRegistry, joined_io
from .conversation_store import JournalStore
from .conversation_sync import MirrorClient, SourceCredentials


_PARAMS = {
    'status': {'journal_id', 'session_id'},
    'list': {'cursor', 'limit'},
    'read_page': {'journal_id', 'cursor', 'limit', 'projection'},
    'read_content': {'journal_id', 'content_id', 'offset', 'limit'},
    'list_sources': {'session_id', 'provider'},
    'bind_source': {'session_id', 'source_ref', 'terminal_generation'},
}


class ConversationManager:
    def __init__(self, root, *, default_token='', accounts=None, drain_seconds=30):
        self.root = Path(root)
        self.default_token, self.accounts = default_token, accounts
        self.store = JournalStore(self.root)
        self.capture = None
        try:
            self.capture = JournalCapture(self.store)
            self.sources = SourceRegistry(self.store, self.root / 'sources')
        except Exception:
            if self.capture is not None:
                self.capture.close(timeout=30)
            self.store.close()
            raise JournalError('storage_unavailable') from None
        self.source_epoch = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._sessions = {}
        self._loop = None
        self._background = None
        self._jobs = set()
        self._pending_ends = {}
        self._orphan_jobs = []
        self._closed = False
        self._storage_closed = False
        self._drain_seconds = drain_seconds
        self._routes = []
        self._mirrors = {}
        self._sync_errors = {}
        self._config_error = None
        self._load_mirrors()

    def owner_scope(self, token, *, default_token=None, accounts=None):
        default = self.default_token if default_token is None else default_token
        if token == default:
            subject = 'host-owner'
        else:
            directory = self.accounts if accounts is None else accounts
            user = next((user for user in getattr(directory, 'users', ())
                         if user.get('token') == token and token), None)
            if not user or not isinstance(user.get('email'), str):
                raise JournalError('owner_unavailable')
            subject = 'account:' + user['email'].strip().lower()
        # Credential rotation must not orphan archives or disclose a token hash.
        digest = hashlib.sha256((self.store.host_installation_id + ':' + subject).encode()).hexdigest()
        return 'owner-' + digest

    def open_session(self, session, *, owner_scope, room, title=''):
        with self._lock:
            if self._closed:
                raise JournalError('recorder_closed')
            old = self._sessions.get(session.id)
            if old and old['session'] is session and old['journal_id']:
                return old['journal_id']
            if old:
                raise JournalError('session_conflict')
            generation = getattr(session, 'process_generation', None) or uuid.uuid4().hex
            session.process_generation = generation
            entry = dict(session=session, journal_id=None, owner_scope=owner_scope,
                         room=room, active=True, position=0)
            self._sessions[session.id] = entry
        try:
            manifest = self.store.create(owner_scope, room=room, window_id=session.window_id,
                session_id=session.id, source_epoch=self.source_epoch, process_generation=generation,
                title=title, agent=session.shell[:128])
        except Exception:
            with self._lock:
                if self._sessions.get(session.id) is entry:
                    del self._sessions[session.id]
            raise
        with self._lock:
            entry['journal_id'] = manifest['journal_id']
        return manifest['journal_id']

    def record_output(self, session, data):
        with self._lock:
            entry = self._sessions.get(session.id)
            if (self._closed or not entry or not entry['journal_id']
                    or entry['session'] is not session or not entry['active']):
                return False
            start = entry['position']
            entry['position'] += len(data)
            return self.capture.record_output(entry['journal_id'], data,
                source_record_id=f'pty:{session.process_generation}:{start}:{start+len(data)}',
                metadata={'byte_start': start, 'byte_end': start + len(data)})

    def start_background(self, loop):
        with self._lock:
            if self._closed or self._background is not None:
                return
            self._loop = loop
            self._background = loop.create_task(self._observe())

    def end_session(self, session, *, outcome='exited'):
        with self._lock:
            entry = self._sessions.get(session.id)
            if self._closed or not entry or entry['session'] is not session or not entry['active']:
                return
            entry['active'] = False
            self._pending_ends[entry['journal_id']] = outcome
            loop = self._loop
            if loop is None or loop.is_closed():
                # EOF is trusted even during loop teardown. Keep its disk work
                # off the reader and mark the missing reconciliation incomplete.
                task = threading.Thread(target=self._finish_without_loop,
                    args=(entry['journal_id'], outcome), daemon=True)
                self._orphan_jobs.append(task)
                task.start()
                return
            def schedule():
                with self._lock:
                    if self._closed:
                        return
                    task = loop.create_task(self._finish(entry['journal_id'], outcome))
                    self._jobs.add(task)
                task.add_done_callback(self._jobs.discard)
            loop.call_soon_threadsafe(schedule)

    def _finish_without_loop(self, journal_id, outcome):
        try:
            self.capture.flush(timeout=30)
            self.sources.terminal_end(journal_id)
            self.store.terminal_end(journal_id,
                outcome='interrupted' if outcome == 'read_error' else 'exited', pending=True)
            self.store.seal(journal_id, incomplete=True)
            with self._lock:
                self._pending_ends.pop(journal_id, None)
        except Exception:
            self._sync_errors[journal_id] = 'drain_incomplete'

    async def _finish(self, journal_id, outcome):
        try:
            flushed = await joined_io(self.capture.flush, 30)
            # Freeze pending IDs before the trusted EOF transition.
            source = self.sources.status(journal_id)
            if source.get('binding_id'):
                deadline = time.monotonic() + 30
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise JournalError('reconcile_timeout')
                    await asyncio.wait_for(self.sources.reconcile(source['binding_id']), remaining)
                    try:
                        await joined_io(self.sources.terminal_end, journal_id)
                        break
                    except JournalError as error:
                        if error.code != 'reconcile_in_progress':
                            raise
                source = self.sources.status(journal_id)
            else:
                await joined_io(self.sources.terminal_end, journal_id)
            await joined_io(self.store.terminal_end, journal_id,
                                    outcome='interrupted' if outcome == 'read_error' else 'exited',
                                    pending=bool(source.get('pending_turns')))
            if source.get('binding_id') and self._drain_seconds:
                deadline = time.monotonic() + self._drain_seconds
                while not self._closed and time.monotonic() < deadline:
                    await self.sources.reconcile(source['binding_id'])
                    await asyncio.sleep(min(2, max(0, deadline - time.monotonic())))
            state = self.sources.status(journal_id)
            error = self.capture.status(journal_id)['error']
            if not state.get('pending_turns'):
                await joined_io(self.store.settle_pending, journal_id)
            await joined_io(self.store.seal, journal_id,
                incomplete=not flushed or bool(error) or bool(state.get('pending_turns'))
                or bool(state.get('reason') not in {None, 'unbound', 'claude_final_unproven'})
                or outcome == 'read_error')
            with self._lock:
                self._pending_ends.pop(journal_id, None)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._sync_errors[journal_id] = 'drain_incomplete'
            try:
                # This path already has a trusted reader EOF, even if semantic
                # reconciliation failed. Do not leave it advertised as live.
                await joined_io(self.store.terminal_end, journal_id,
                    outcome='interrupted' if outcome == 'read_error' else 'exited', pending=True)
                if self.capture.status(journal_id)['pending_items'] == 0:
                    await joined_io(self.store.seal, journal_id, incomplete=True)
                    with self._lock:
                        self._pending_ends.pop(journal_id, None)
            except JournalError:
                pass

    def _entry(self, owner_scope, sid, rooms, *, active=False):
        checked_id(sid)
        with self._lock:
            entry = self._sessions.get(sid)
            if not entry or not entry['journal_id']:
                raise JournalError('session_unavailable')
            if active and not entry['active']:
                raise JournalError('terminal_ended')
        self.store.status(owner_scope, entry['journal_id'], rooms=rooms)
        return entry

    def _current(self, entry):
        with self._lock:
            return (not self._closed and entry['active']
                    and self._sessions.get(entry['session'].id) is entry)

    def request(self, owner_scope, action, params, *, rooms=None, allow_bind=False):
        if self._closed:
            raise JournalError('recorder_closed')
        if action not in _PARAMS or not isinstance(params, dict) or set(params) - _PARAMS[action]:
            raise JournalError()
        if action == 'list':
            return self.store.list_journals(owner_scope, rooms=rooms, **params)
        if action in {'list_sources', 'bind_source'}:
            if not allow_bind:
                raise JournalError('owner_required')
            entry = self._entry(owner_scope, params.get('session_id'), rooms, active=True)
            if action == 'list_sources':
                provider = params.get('provider')
                if provider == 'claude':
                    provider = 'claude-code'
                result = self.sources.list_sources(provider, project_root=entry['session'].cwd)
                if not self._current(entry):
                    raise JournalError('terminal_ended')
                return {'sources': result}
            return self.sources.bind(owner_scope, entry['journal_id'], params.get('source_ref'),
                terminal_generation=params.get('terminal_generation'),
                is_current=lambda: self._current(entry))
        if action == 'status' and 'session_id' in params:
            if 'journal_id' in params:
                raise JournalError()
            journal_id = self._entry(owner_scope, params['session_id'], rooms)['journal_id']
        else:
            journal_id = checked_id(params.get('journal_id'))
        manifest = self.store.status(owner_scope, journal_id, rooms=rooms)
        if action == 'status':
            manifest['source'] = self.sources.status(journal_id)
            manifest['storage'] = dict(available=True, **self.capture.status(journal_id),
                sync_error=self._sync_errors.get(journal_id) or self._config_error)
            return manifest
        kwargs = {key: value for key, value in params.items() if key != 'journal_id'}
        if action == 'read_page':
            return self.store.read_page(owner_scope, journal_id, rooms=rooms, **kwargs)
        if action == 'read_content':
            if 'content_id' not in kwargs:
                raise JournalError()
            return self.store.read_content(owner_scope, journal_id, rooms=rooms, **kwargs)
        raise JournalError()

    def _load_mirrors(self):
        """Operator-private routes; terminal room names never imply relay scope."""
        path = self.root / 'mirror-config.json'
        try:
            self.store._plain_file(path)
            if not path.exists():
                return
            if path.stat().st_size > 65536:
                raise JournalError('invalid_mirror_config')
            config = json.loads(path.read_text(encoding='utf-8'))
            if config.get('schema') != 1 or not isinstance(config.get('routes'), list) or len(config['routes']) > 64:
                raise JournalError('invalid_mirror_config')
            seen = set()
            for route in config['routes']:
                scope, room = checked_id(route['local_owner_scope']), checked_id(route['local_room'])
                if (scope, room) in seen:
                    raise JournalError('invalid_mirror_config')
                seen.add((scope, room))
                destination = normalize_destination(dict(route['destination'],
                    host_installation_id=self.store.host_installation_id, source_epoch=self.source_epoch))
                credentials = SourceCredentials(**route['credentials'])
                # Validate independently of any existing journal/outbox.
                MirrorClient(self.store, destination, credentials, local_owner_scope=scope)
                self._routes.append((scope, room, destination, credentials))
        except Exception:
            self._routes.clear()
            self._config_error = 'invalid_mirror_config'

    async def _observe(self):
        cursors = {}
        while not self._closed:
            targets = self.sources.reconciliation_targets()
            for target in targets:
                await self.sources.reconcile(target['binding_id'])
            for scope, room, destination, credentials in self._routes:
                try:
                    page = await joined_io(self.store.list_journals, scope, rooms=[room],
                        cursor=cursors.get((scope, room)), limit=100)
                    cursors[(scope, room)] = page['next_cursor']
                    for manifest in page['journals']:
                        jid = manifest['journal_id']
                        mirror = self._mirrors.get(jid)
                        if mirror is None:
                            selected = dict(destination, source_epoch=manifest['source_epoch'])
                            mirror = self._mirrors[jid] = MirrorClient(self.store, selected, credentials,
                                                                     local_owner_scope=scope)
                        try:
                            await mirror.sync_once(jid)
                            self._sync_errors.pop(jid, None)
                        except JournalError as error:
                            self._sync_errors[jid] = error.code
                except JournalError:
                    self._config_error = 'mirror_unavailable'
            await asyncio.sleep(3)

    async def aclose(self):
        with self._lock:
            self._closed = True
            tasks = [task for task in [self._background, *self._jobs] if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await joined_io(self._close_storage)

    def _close_storage(self):
        with self._lock:
            if self._storage_closed:
                return
            self._storage_closed = True
        for thread in self._orphan_jobs:
            thread.join(timeout=30)
        if any(thread.is_alive() for thread in self._orphan_jobs):
            # Keep the lease and store alive until the accepted worker finishes.
            self._config_error = 'storage_busy'
            with self._lock:
                self._storage_closed = False
            return
        # Accepted reader EOFs are registered before loop scheduling. A queued
        # callback may never have created its task, or shutdown may cancel it.
        # Reconcile these after joined tasks, before closing registry files.
        with self._lock:
            pending = list(self._pending_ends.items())
        for journal_id, outcome in pending:
            self._finish_without_loop(journal_id, outcome)
        self.sources.close()
        if self.capture.close(timeout=30):
            self.store.close()

    def close(self):
        with self._lock:
            if self._closed:
                return
            if ((self._background is not None and not self._background.done())
                    or any(not task.done() for task in self._jobs)):
                raise JournalError('async_shutdown_required')
            self._closed = True
        self._close_storage()
