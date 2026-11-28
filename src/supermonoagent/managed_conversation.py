"""Source-owned, durable coordination of explicitly managed coding processes.

The journal owns receipts; providers own protocol proof. This layer never treats
an enqueue, a selected pane, or an application UUID as provider authority.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import re
import threading
import time

from .conversation_contract import JournalError, canonical_json, checked_id
from .conversation_sources import joined_io
from .conversation_store import managed_creation_ids
from .hub import Session
from .managed_events import ManagedEvents
from .managed_process import ManagedPty, sanitize_render_text
from .runtime_state import TARGET, ManagedRuntimeSnapshot


_TARGET = frozenset(TARGET)
_PARAMS = {
    'create_managed': {'session_id', 'process_generation', 'provider', 'nonce'},
    'submit_managed': _TARGET | {'nonce', 'text'},
    'managed_status': _TARGET | {'nonce'},
    'interrupt_managed': _TARGET | {'submission_id', 'provider_turn_id', 'nonce'},
    'respond_managed_permission': _TARGET | {'submission_id', 'provider_turn_id',
        'permission_request_id', 'decision', 'nonce'},
    'close_managed': _TARGET | {'nonce'},
}
_POLICY_KEYS = frozenset({'approvalPolicy', 'approvalsReviewer', 'sandbox', 'model',
    'modelProvider', 'permission_mode', 'source', 'host_prompts'})


@dataclass
class _Conversation:
    session: Session
    owner: str
    room: str
    binding: dict
    authorized: object = field(repr=False)
    mutation: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    fence: threading.RLock = field(default_factory=threading.RLock, repr=False)
    driver: object = field(default=None, repr=False)
    state: str = 'starting'
    provider: dict = field(default_factory=dict)
    active: dict | None = None
    permissions: dict = field(default_factory=dict)
    details: dict = field(default_factory=dict, repr=False)
    permission_timers: dict = field(default_factory=dict, repr=False)
    failed: str | None = None
    cancel_pending: int = 0
    closing: bool = False
    ended: bool = False
    physically_sent: bool = False
    provider_busy: bool = False
    last_outcome: dict | None = None
    projection: ManagedRuntimeSnapshot | None = field(default=None, repr=False)
    projection_revision: int = 0
    start_task: object = field(default=None, repr=False)
    close_task: object = field(default=None, repr=False)
    jobs: set = field(default_factory=set, repr=False)

    @property
    def target(self):
        return dict(window_id=self.session.window_id, session_id=self.session.id,
            process_generation=self.session.process_generation, journal_id=self.session.journal_id)

    def set_public_state(self, state, *, clear_permissions=False):
        """Update the projected state while the caller holds the runtime fence."""
        if self.state != state or clear_permissions and self.permissions:
            self.projection = None
        self.state = state
        if clear_permissions:
            self.permissions.clear()
            self.details.clear()


class ManagedConversationService:
    def __init__(self, hub, journals, *, credential_authorized, driver_factory=None, binaries=None):
        self.hub, self.journals, self.store = hub, journals, journals.store
        self.credential_authorized = credential_authorized
        self.driver_factory = driver_factory or self._driver
        self.binaries = dict(binaries or {})
        self._conversations = {}
        self._creation_locks = {}
        self._operations = set()
        self._closed = False
        self._loop = asyncio.get_running_loop()
        self.events = ManagedEvents(hub, self._loop, self.store.host_installation_id, self._event_authorized)
        self._shutdown_task = None

    @staticmethod
    def _driver(provider, cwd, binding, emit, can_send, *, binary=None):
        if provider == 'claude':
            from .managed_claude import ClaudeManagedDriver
            cls = ClaudeManagedDriver
        else:
            from .managed_codex import CodexManagedDriver
            cls = CodexManagedDriver
        return cls(cwd, binding, emit, can_send, binary=binary)

    @staticmethod
    def _validate(action, params):
        allowed = _PARAMS.get(action) if isinstance(action, str) else None
        if allowed is None or not isinstance(params, dict) or set(params) - allowed:
            raise JournalError()
        required = allowed - {'provider_turn_id'}
        if action == 'managed_status':
            required = required - {'nonce'}
        if not required <= set(params):
            raise JournalError()
        for key, value in params.items():
            if key == 'text':
                if not isinstance(value, str) or not value.strip():
                    raise JournalError('invalid_prompt')
                try:
                    size = len(value.encode('utf-8'))
                except UnicodeError:
                    raise JournalError('invalid_prompt') from None
                if size > 65536:
                    raise JournalError('prompt_too_large')
            elif key == 'provider':
                if not isinstance(value, str) or value not in {'claude', 'codex'}:
                    raise JournalError('invalid_provider')
            elif key == 'decision':
                if not isinstance(value, str) or value not in {'allow_once', 'deny'}:
                    raise JournalError('invalid_permission')
            elif key == 'provider_turn_id' and value is None:
                continue
            else:
                checked_id(value, 64 if key == 'nonce' else 512 if key == 'provider_turn_id'
                           else 256 if key == 'permission_request_id' else 128)

    async def handle(self, action, params, *, owner_scope, owner_token, rooms=None, authorize=None,
                     event_cursor=False):
        """Bounded wire operation; cancellation joins accepted storage work.

        The short request fence may depend on a websocket. A controller's stable
        credential fence deliberately does not: detaching cannot cancel a turn.
        """
        self._validate(action, params)
        checked_id(owner_scope)
        if self._closed:
            raise JournalError('managed_unavailable')
        abandoned = False

        def request_authorized():
            try:
                return not abandoned and (authorize is None or authorize() is True)
            except Exception:
                return False

        task = asyncio.create_task(self._handle(action, dict(params), owner_scope, owner_token,
            None if rooms is None else tuple(rooms), request_authorized, event_cursor=event_cursor))
        self._operations.add(task)
        task.add_done_callback(self._operations.discard)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            abandoned = True
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                task.exception()
            raise

    def _credential(self, owner, token, room, session):
        try:
            return self.credential_authorized(owner, token, room, session) is True
        except Exception:
            return False

    def _event_authorized(self, client, session):
        conversation = self._conversations.get(session.journal_id)
        return (conversation is not None and (not client.channel or client.channel == conversation.room)
                and self._credential(conversation.owner, client.token, conversation.room, session))

    def _current(self, c):
        return (self.hub.sessions.get(c.session.id) is c.session
            and c.session.process_generation == c.binding['process_generation']
            and c.session.journal_id == c.binding['journal_id'])

    def _check(self, c, owner, token, rooms, authorize, *, healthy=False):
        if (owner != c.owner or rooms is not None and c.room not in rooms or not self._current(c)
                or not authorize() or not self._credential(owner, token, c.room, c.session)):
            raise JournalError('not_authorized')
        if healthy and (c.failed or c.closing or c.ended or c.session.journal_error or self._closed):
            raise JournalError('managed_unavailable')

    async def _handle(self, action, params, owner, token, rooms, authorize, *, event_cursor=False):
        if not authorize():
            raise JournalError('not_authorized')
        if action == 'create_managed':
            return await self._create(params, owner, token, rooms, authorize)
        target = {key: params[key] for key in _TARGET}
        c = self._conversations.get(target['journal_id'])
        if c is None or (action == 'managed_status' and not self._current(c)):
            # Restored or removed sessions expose durable read-only receipts,
            # never regain process authority. The daemon supplies current owner
            # scope/request credentials; Store repeats owner/room/exact target.
            control = await joined_io(self.store.read_managed_control, owner, target['journal_id'],
                target=target, nonce=params.get('nonce') if action == 'managed_status' else None, rooms=rooms)
            if not authorize():
                raise JournalError('not_authorized')
            if action != 'managed_status':
                raise JournalError('managed_ambiguous')
            state = 'ambiguous'
            last_outcome = None
            if c is not None:
                with c.fence:
                    state = 'error' if c.failed else 'ended' if c.ended else 'ambiguous'
                    last_outcome = c.last_outcome
            result = self._status_result(target, control, state=state, last_outcome=last_outcome, negotiated=event_cursor)
            if event_cursor:
                result.update(self.events.snapshot_cursor(None, None))
            return result
        if target != c.target:
            raise JournalError('stale_target')
        self._check(c, owner, token, rooms, authorize)
        if action == 'interrupt_managed':
            return await self._interrupt(c, params, owner, token, rooms, authorize)
        if action == 'close_managed':
            with c.fence:
                c.closing = True
                c.set_public_state('closing', clear_permissions=True)
            self._publish(c)
            self._schedule_close(c, 'closed')
            return dict(c.target, state='ended' if c.close_task.done() else 'closing')
        driver_call = None
        async with c.mutation:
            self._check(c, owner, token, rooms, authorize)
            if action == 'managed_status':
                nonce = params.get('nonce')
                result_receipt = None
                if not event_cursor or nonce is not None:
                    control = await self._refresh(c, nonce=nonce, rooms=rooms)
                    result_receipt = control['receipt']
                self._check(c, owner, token, rooms, authorize)
                result = self._live_status(c, receipt=result_receipt, negotiated=event_cursor)
                if len(canonical_json(result)) > 192 * 1024 - 4096:
                    raise JournalError('managed_status_limit')
                return result
            if action == 'submit_managed':
                prior = await self._refresh(c, nonce=params['nonce'], rooms=rooms)
                self._check(c, owner, token, rooms, authorize)
                if prior['receipt'] is None:
                    self._check(c, owner, token, rooms, authorize, healthy=True)
                    if c.provider_busy or c.state != 'ready':
                        raise JournalError('managed_busy')
                    capture = self.journals.capture.status(c.session.journal_id)
                    if capture.get('error'):
                        self.capture_failed(c.session.id)
                        raise JournalError('managed_unavailable')
                receipt = await self._store(c, 'accept_managed_submission',
                    nonce=params['nonce'], text=params['text'], rooms=rooms)
                await self._refresh(c, rooms=rooms, accepted=receipt if not receipt['replayed'] else None)
                if receipt['replayed']:
                    self._check(c, owner, token, rooms, lambda: True)
                    return receipt
                try:
                    # Committed input belongs to this stable owner/process, not
                    # the websocket that happened to deliver it. A disconnected
                    # viewer loses its reply, never the already-accepted turn.
                    self._check(c, owner, token, rooms, lambda: True, healthy=True)
                except JournalError:
                    await self._settle_local(c, 'interrupted', 'authorization_revoked')
                    raise
                try:
                    await self._render(c, params['text'])
                except JournalError:
                    await self._settle_local(c, 'interrupted', 'recording_failed')
                    raise
                self._job(c, self._dispatch(c, receipt, params['text']))
                return receipt
            else:
                self._check(c, owner, token, rooms, authorize, healthy=True)
                # Use the request's retained permission version for nonce replay
                # even after its turn has settled. No replay executes again.
                control = await self._refresh(c, rooms=rooms)
                pending = c.permissions.get(params['permission_request_id'])
                if params['decision'] == 'allow_once' and not c.provider.get('confirmed'):
                    raise JournalError('managed_root_unconfirmed')
                # Permissions only originate before this turn's first cancel.
                # Tombstones removed from the active index therefore retain v0.
                version = pending['cancel_version'] if pending else 0
                receipt = await self._store(c, 'decide_managed_permission',
                    permission_request_id=params['permission_request_id'],
                    submission_id=params['submission_id'], provider_turn_id=params.get('provider_turn_id'),
                    decision=params['decision'], nonce=params['nonce'], cancel_version=version, rooms=rooms)
                await self._refresh(c, rooms=rooms)
                self._check(c, owner, token, rooms, authorize, healthy=True)
                if not receipt['replayed']:
                    driver_call = ('respond_permission', (params['permission_request_id'], params['decision']),
                        dict(submission_id=params['submission_id'], cancel_version=receipt['cancel_version']))
        # Drivers synchronously publish events while handling these calls. Never
        # hold the mutation lock across them, even for a short physical write.
        if driver_call:
            method, args, kwargs = driver_call
            if method == 'interrupt':
                self._job(c, self._call_driver(c, method, *args, **kwargs))
            else:
                await self._call_driver(c, method, *args, **kwargs)
        return receipt

    async def _interrupt(self, c, params, owner, token, rooms, authorize):
        # Set this before waiting for the mutation lock or storage. A queued
        # positive write cannot outrun a cancel already waiting to commit.
        with c.fence:
            c.cancel_pending += 1
        try:
            async with c.mutation:
                self._check(c, owner, token, rooms, authorize)
                receipt = await self._store(c, 'cancel_managed_submission',
                    submission_id=params['submission_id'], nonce=params['nonce'],
                    provider_turn_id=params.get('provider_turn_id'), rooms=rooms)
                await self._refresh(c, rooms=rooms)
                if not receipt['replayed'] and receipt['state'] == 'not_dispatched':
                    self._remember_outcome(c, receipt['submission_id'], 'interrupted', 'not_dispatched', False)
                    self._publish(c)
                self._check(c, owner, token, rooms, authorize)
        finally:
            with c.fence:
                c.cancel_pending -= 1
        if not receipt['replayed'] and receipt['state'] == 'interrupt_requested':
            self._job(c, self._call_driver(c, 'interrupt', params['submission_id'], receipt['cancel_version']))
        return receipt

    async def _create(self, params, owner, token, rooms, authorize):
        ids = managed_creation_ids(self.store.host_installation_id, owner, params['nonce'])
        lock = self._creation_locks.setdefault(ids['journal_id'], asyncio.Lock())
        async with lock:
            if self._closed:
                raise JournalError('managed_unavailable')
            # A receipt is identified independently of the original anchor's
            # lifetime. Recover it before asking whether a new launch is legal.
            try:
                prior = await joined_io(self.store.get_managed_creation, owner, ids['journal_id'], rooms=rooms)
            except JournalError as error:
                if error.code != 'not_found':
                    raise
                prior = None
            if not authorize() or self._closed:
                raise JournalError('not_authorized')
            target = {key: ids[key] for key in _TARGET}
            if prior is not None:
                request = prior['request']
                if (params['session_id'] != request['anchor_session_id']
                        or params['process_generation'] != request['anchor_process_generation']
                        or params['provider'] != request['provider']):
                    raise JournalError('creation_conflict')
                c = self._conversations.get(ids['journal_id'])
                live = self.hub.sessions.get(ids['session_id'])
                if (live is not None and live.window_id == target['window_id']
                        and live.process_generation == target['process_generation']
                        and live.journal_id == target['journal_id']
                        and not self._credential(owner, token, request['room'], live)):
                    raise JournalError('not_authorized')
                return dict(target, interaction_mode='managed', provider=params['provider'],
                    state=c.state if c and self._current(c) else 'ambiguous', cwd=request['cwd'], replayed=True)
            anchor = self.hub.sessions.get(params['session_id'])
            window = self.hub.windows.get(anchor.window_id) if anchor else None
            group = self.hub.groups.get(window.group_id) if window else None
            if (anchor is None or group is None or anchor.process_generation != params['process_generation']
                    or getattr(anchor, 'interaction_mode', 'native') != 'native'
                    or rooms is not None and group.channel not in rooms
                    or not self._credential(owner, token, group.channel, anchor) or not authorize()):
                raise JournalError('not_authorized')
            request = dict(anchor_window_id=anchor.window_id, anchor_session_id=anchor.id,
                anchor_process_generation=anchor.process_generation, provider=params['provider'],
                cwd=anchor.cwd, room=group.channel)
            room, cwd, group_id = group.channel, anchor.cwd, window.group_id

            def anchor_current():
                return (not self._closed and authorize() and self.hub.sessions.get(anchor.id) is anchor
                    and self.hub.windows.get(anchor.window_id) is window
                    and self.hub.groups.get(group_id) is group and window.group_id == group_id
                    and group.channel == room and anchor.cwd == cwd
                    and anchor.process_generation == params['process_generation']
                    and self._credential(owner, token, room, anchor))
            if not anchor_current():
                raise JournalError('not_authorized')
            await joined_io(self.store.create, owner, room=group.channel,
                window_id=ids['window_id'], session_id=ids['session_id'],
                process_generation=ids['process_generation'], journal_id=ids['journal_id'],
                source_epoch=self.journals.source_epoch, title=f"{params['provider']} managed",
                agent=params['provider'], managed_creation=dict(nonce=params['nonce'], request=request))
            if not anchor_current():
                # Durable creation is retained, so retry can never relaunch it.
                raise JournalError('not_authorized')
            binding = dict(binding_id=ids['binding_id'], journal_id=ids['journal_id'],
                session_id=ids['session_id'], process_generation=ids['process_generation'], provider=params['provider'])
            session = Session(ids['session_id'], ids['window_id'], params['provider'], anchor.cwd, None,
                process_generation=ids['process_generation'], journal_id=ids['journal_id'],
                interaction_mode='managed', provider=params['provider'])
            # Credentials live only in this private closure, never in a durable
            # control, process environment, event, catalog, or public snapshot.
            c = _Conversation(session, owner, room, binding,
                lambda: self._credential(owner, token, room, session))
            session.pty = ManagedPty(lambda: self._schedule_close(c, 'closed'))
            session.managed_snapshot = lambda: self._snapshot(c)
            try:
                await joined_io(self.journals.register_managed_session, session,
                    owner_scope=owner, room=group.channel, journal_id=ids['journal_id'])
                if not anchor_current():
                    raise JournalError('not_authorized')
                c.driver = self.driver_factory(params['provider'], anchor.cwd, binding,
                    lambda event: self._event(c, event), lambda context: self._can_send(c, context),
                    binary=self.binaries.get(params['provider']))
                self._conversations[ids['journal_id']] = c
                self.hub.add_managed_session(session, group_id=window.group_id,
                    title=f"{params['provider']} managed")
                self._publish(c)
            except Exception:
                session.pty.feed_eof()
                self._conversations.pop(ids['journal_id'], None)
                self.journals.end_session(session, outcome='read_error') if hasattr(self.journals, 'end_session') else None
                raise
            c.start_task = self._job(c, self._start(c))
            return dict(target, interaction_mode='managed', provider=params['provider'],
                state='starting', cwd=request['cwd'], replayed=False)

    async def _store(self, c, method, **kwargs):
        return await joined_io(getattr(self.store, method), c.owner, c.session.journal_id,
            target=c.target, **kwargs)

    async def _refresh(self, c, *, nonce=None, rooms=None, accepted=None):
        control = await self._store(c, 'read_managed_control', nonce=nonce, rooms=rooms)
        with c.fence:
            active = control['active_submission']
            provider = control['provider'] or {}
            permissions = {item['permission_request_id']: item for item in control['permissions']
                if item['state'] in {'pending', 'decided', 'expired'} and not (c.failed or c.closing or c.ended)}
            state = ('error' if c.failed else 'ended' if c.ended else
                     'closing' if c.closing else 'running' if c.provider_busy and active is None
                     else control['state'])
            if (c.active, c.provider, c.permissions, c.state) != (active, provider, permissions, state):
                c.projection = None
            c.active, c.provider, c.permissions, c.state = active, provider, permissions, state
            c.details = {identity: details for identity, details in c.details.items() if identity in permissions}
            self._cancel_timers(c, keep=set(permissions))
        self._publish(c, receipt=accepted)
        return control

    def _runtime_snapshot(self, c):
        with c.fence:
            if c.projection is None:
                c.projection_revision += 1
                c.projection = ManagedRuntimeSnapshot.capture(c.target, state=c.state, provider=c.binding['provider'],
                    descriptor=c.provider, active=c.active,
                    permissions=(item for item in c.permissions.values() if item['state'] == 'pending'),
                    last_outcome=c.last_outcome, source_revision=c.projection_revision)
            return c.projection

    def _live_status(self, c, *, receipt=None, negotiated=False):
        with c.fence:
            snapshot = self._runtime_snapshot(c)
            result = snapshot.control(details=c.details, receipt=receipt)
            if negotiated:
                result.update(self.events.snapshot_cursor(c.session, snapshot))
            return result

    def _publish(self, c, *, receipt=None):
        self.events.observe(c.session, self._runtime_snapshot(c), receipt)

    @staticmethod
    def _status_result(target, control, *, state, last_outcome=None, negotiated=False):
        provider = control.get('provider') or {}
        snapshot = ManagedRuntimeSnapshot.capture(target, state=state,
            provider=provider.get('provider') or control['creation']['request']['provider'],
            descriptor=provider, active=control['active_submission'], permissions=control['permissions'],
            last_outcome=last_outcome, runtime_verification='unverifiable' if state == 'ambiguous' else None)
        return snapshot.control(receipt=control['receipt'], negotiated=negotiated)

    def _snapshot(self, c):
        return self._runtime_snapshot(c).directory()

    @staticmethod
    def _remember_outcome(c, submission_id, outcome, reason, has_final):
        # Reasons are fixed public codes, never native API error text, args,
        # credentials, or a substitute assistant response.
        reason = reason if isinstance(reason, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', reason) else None
        with c.fence:
            outcome_data = dict(submission_id=submission_id, outcome=outcome, reason=reason, has_final=bool(has_final))
            if c.last_outcome != outcome_data:
                c.projection = None
                c.last_outcome = outcome_data

    def source_status(self, journal_id):
        c = self._conversations.get(journal_id)
        if c is None:
            return None
        with c.fence:
            status = self._snapshot(c)
            return dict(binding_id=c.binding['binding_id'], origin='managed', provider=status['provider'],
                provider_version=status['provider_version'], provider_session_id=status['provider_root_id'],
                state=status['state'], reason=c.failed, pending_turns=int(c.active is not None),
                managed_state=status['state'], coverage='managed_rendering', supported=True,
                bound=not c.ended and not self._closed,
                healthy=not (c.failed or c.ended or c.closing or self._closed or c.session.journal_error),
                provider_root_confirmed=status['provider_root_confirmed'],
                input_provenance='app_submission', runtime_verification='runtime_unverified')

    def _can_send(self, c, context):
        """Called by the physical writer thread; RAM plus a quick stable ACL only."""
        with c.fence:
            operation = context.get('operation')
            if operation == 'cleanup':
                return self._current(c)
            if (c.failed or c.closing or c.ended or self._closed or not self._current(c)
                    or c.session.journal_error or not c.authorized()):
                return False
            if context.get('process_generation', c.binding['process_generation']) != c.binding['process_generation']:
                return False
            if operation == 'initialize':
                return c.state == 'starting'
            active = c.active
            if (operation not in {'submit', 'interrupt', 'permission'} or not active
                    or active['submission_id'] != context.get('submission_id')
                    or active['cancel_version'] != context.get('cancel_version')):
                return False
            if operation == 'submit':
                if c.cancel_pending or active['cancel_requested'] or active['dispatch_state'] != 'dispatching':
                    return False
                c.physically_sent = True
                return True
            if operation == 'interrupt':
                return active['cancel_requested']
            pending = c.permissions.get(context.get('permission_request_id'))
            if (not pending or pending['state'] not in {'pending', 'decided', 'expired'}
                    or pending['submission_id'] != active['submission_id']
                    or pending['provider_turn_id'] != active.get('provider_turn_id')
                    or pending['cancel_version'] != active['cancel_version']):
                return False
            if context.get('decision') == 'deny':
                # Native expiry may deny without a fabricated human decision.
                return pending.get('decision') in {None, 'deny'} or pending['expires_at'] <= time.time()
            return (pending['state'] == 'decided' and pending.get('decision') == 'allow_once'
                and pending['expires_at'] > time.time() and not c.cancel_pending
                and not active['cancel_requested'] and c.provider.get('confirmed') is True)

    def _job(self, c, coroutine):
        task = asyncio.create_task(coroutine)
        c.jobs.add(task)

        def finished(job):
            c.jobs.discard(job)
            if not job.cancelled():
                job.exception()  # Retrieve failures without logging private data.
        task.add_done_callback(finished)
        return task

    async def _start(self, c):
        try:
            await c.driver.start()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._fail(c, 'managed_startup_failed')
            self._schedule_close(c, 'startup_failed')

    async def _dispatch(self, c, receipt, text):
        try:
            async with c.mutation:
                with c.fence:
                    active = c.active
                    eligible = (not c.closing and not c.failed and self._current(c) and c.authorized()
                        and active is not None and active['submission_id'] == receipt['submission_id']
                        and not active['cancel_requested'])
                if not eligible:
                    if active and active['submission_id'] == receipt['submission_id']:
                        await self._settle_local(c, 'interrupted', 'authorization_revoked')
                    return
                await self._store(c, 'mark_managed_dispatching', submission_id=receipt['submission_id'],
                    cancel_version=receipt['cancel_version'])
                await self._refresh(c)
                with c.fence:
                    c.physically_sent = False
                    c.provider_busy = True
            result = await c.driver.submit(dict(submission_id=receipt['submission_id'],
                user_message_uuid=receipt['user_message_uuid'], cancel_version=receipt['cancel_version'], text=text))
            if result and result.get('state', result.get('dispatch_state')) == 'not_dispatched':
                async with c.mutation:
                    if c.active and c.active['submission_id'] == receipt['submission_id']:
                        await self._settle_local(c, 'interrupted', 'not_dispatched')
                self._job(c, self._after_settled(c))
        except asyncio.CancelledError:
            raise
        except Exception:
            # Unknown physical delivery is never retried. Preserve a driver-
            # proven terminal result if its callback already settled the turn.
            self._fail(c, 'managed_dispatch_failed')
            self._schedule_close(c, 'dispatch_failed')

    async def _call_driver(self, c, method, *args, **kwargs):
        try:
            return await getattr(c.driver, method)(*args, **kwargs)
        except Exception:
            if not c.closing and not c.failed:
                self._fail(c, 'managed_control_failed')
                self._schedule_close(c, 'control_failed')
            raise JournalError('managed_control_failed') from None

    async def _settle_local(self, c, outcome, reason):
        active = c.active
        if active is None:
            return
        await self._store(c, 'settle_managed_submission', submission_id=active['submission_id'],
            user_message_uuid=active['user_message_uuid'], provider_root_id=c.provider.get('provider_root_id'),
            provider_turn_id=active.get('provider_turn_id'), outcome=outcome, finals=(), reason=reason)
        self._remember_outcome(c, active['submission_id'], outcome, reason, False)
        await self._refresh(c)

    async def _event(self, c, event):
        try:
            async with c.mutation:
                kind = event['kind']
                if c.failed and kind in {'ready', 'permission', 'progress', 'turn_started'}:
                    raise JournalError('managed_unavailable')
                if kind == 'ready':
                    policy = {key: value for key, value in (event.get('policy') or {}).items() if key in _POLICY_KEYS}
                    if len(canonical_json(policy)) > 4096:
                        raise JournalError('invalid_policy')
                    confirmed = event.get('provider_root_confirmed', event.get('provider_root_id') is not None)
                    root = event.get('provider_root_id') if confirmed else event.get('requested_root_id')
                    await self._store(c, 'confirm_managed_provider', provider=event['provider'],
                        provider_version=event['provider_version'], provider_root_id=root,
                        policy=policy, confirmed=confirmed)
                    await self._refresh(c)
                elif kind == 'turn_started':
                    await self._store(c, 'associate_managed_turn', **{key: event[key] for key in
                        ['submission_id', 'user_message_uuid', 'provider_root_id', 'provider_turn_id']})
                    await self._refresh(c)
                elif kind == 'permission':
                    await self._permission(c, event)
                elif kind == 'permission_withdrawn':
                    identity = event['permission_request_id']
                    with c.fence:
                        if c.permissions.pop(identity, None) is not None:
                            c.projection = None
                        c.details.pop(identity, None)
                        self._cancel_timers(c, keep=set(c.permissions))
                    await self._store(c, 'withdraw_managed_permission', permission_request_id=identity)
                    await self._refresh(c)
                elif kind == 'settled':
                    receipt = await self._store(c, 'settle_managed_submission',
                        **{key: event[key] for key in ['submission_id', 'user_message_uuid', 'provider_root_id', 'outcome']},
                        provider_turn_id=event.get('provider_turn_id'), finals=event.get('finals', ()), reason=event.get('reason'))
                    if not receipt['replayed']:
                        self._remember_outcome(c, event['submission_id'], event['outcome'], event.get('reason'),
                            bool(event.get('finals')))
                    await self._refresh(c)
                    for final in (() if receipt['replayed'] else event.get('finals', ())):
                        await self._render(c, final['text'])
                elif kind == 'progress':
                    if event.get('category') in {'tool', 'candidate', 'status'}:
                        await self._render(c, event.get('text', ''))
                elif kind == 'error':
                    if event.get('fatal'):
                        self._fail(c, 'managed_provider_failed')
                        self._schedule_close(c, 'provider_failed')
                elif kind == 'closed':
                    if c.active:
                        await self._settle_local(c, 'ambiguous', 'process_closed')
                    with c.fence:
                        c.ended = True
                        c.set_public_state('error' if c.failed else 'ended', clear_permissions=True)
                    self.events.end(c.session, self._runtime_snapshot(c))
                    await asyncio.sleep(0)
                    c.session.pty.feed_eof()
                else:
                    raise JournalError('invalid_provider_event')
            if kind == 'settled':
                # A provider clears its own active turn only after this callback
                # returns. Do not advertise ready in the intervening interval.
                self._job(c, self._after_settled(c))
        except Exception:
            self._fail(c, 'managed_recording_failed')
            self._schedule_close(c, 'recording_failed')
            raise

    async def _permission(self, c, event):
        original = event.get('details', '')
        # One display-only wire type for both providers. JSON text remains
        # inert when clients render it with textContent; raw arguments never
        # enter the durable permission descriptor or journal event.
        details = original if isinstance(original, str) else canonical_json(original).decode('utf-8')
        size = len(canonical_json(details))
        with c.fence:
            total = sum(len(canonical_json(value)) for key, value in c.details.items()
                        if key != event['permission_request_id'])
        choices = event.get('choices', ['deny'])
        if size > 65536 or total + size > 96 * 1024 or event.get('requires_user_interaction'):
            details = 'Details unavailable; this request can only be denied.'
            choices = ['deny']
        descriptor = await self._store(c, 'register_managed_permission',
            permission_request_id=event['permission_request_id'], submission_id=event['submission_id'],
            provider_turn_id=event.get('provider_turn_id'), tool_name=event.get('tool_name', ''),
            choices=choices, requires_user_interaction=event.get('requires_user_interaction', False))
        await self._refresh(c)
        if descriptor['state'] == 'pending':
            with c.fence:
                c.details[event['permission_request_id']] = details
                if event['permission_request_id'] not in c.permission_timers:
                    c.permission_timers[event['permission_request_id']] = self._job(c,
                        self._expire_permission(c, event['permission_request_id'], descriptor['expires_at']))

    @staticmethod
    def _cancel_timers(c, *, keep=()):
        current = asyncio.current_task()
        for identity, task in list(c.permission_timers.items()):
            if identity not in keep:
                c.permission_timers.pop(identity, None)
                if task is not current:
                    task.cancel()

    async def _expire_permission(self, c, identity, expires_at):
        await asyncio.sleep(max(0, expires_at - time.time()))
        async with c.mutation:
            with c.fence:
                pending, active = c.permissions.get(identity), c.active
                if (not pending or not active or c.failed or c.closing or c.ended
                        or pending['submission_id'] != active['submission_id'] or not c.authorized()):
                    return
                submission, version = active['submission_id'], active['cancel_version']
            await self._refresh(c)
        try:
            result = await c.driver.respond_permission(identity, 'deny',
                submission_id=submission, cancel_version=version)
            if result and result.get('state') == 'answered':
                return
        except Exception:
            # A simultaneous application response can own the driver's write
            # slot. Its expired allow is fenced; cancel that exact turn instead.
            pass
        with c.fence:
            c.cancel_pending += 1
        receipt = None
        try:
            async with c.mutation:
                if (not c.active or c.active['submission_id'] != submission
                        or c.active['cancel_requested'] or c.failed or c.closing or not c.authorized()):
                    return
                nonce = 'expiry-' + hashlib.sha256(identity.encode()).hexdigest()[:48]
                receipt = await self._store(c, 'cancel_managed_submission', submission_id=submission,
                    nonce=nonce, provider_turn_id=c.active.get('provider_turn_id'))
                await self._refresh(c)
        finally:
            with c.fence:
                c.cancel_pending -= 1
        if receipt and receipt['state'] == 'interrupt_requested':
            await self._call_driver(c, 'interrupt', submission, receipt['cancel_version'])

    async def _after_settled(self, c):
        while not (c.closing or c.failed or c.ended):
            if c.driver.snapshot().get('state') == 'ready':
                async with c.mutation:
                    if c.active is None:
                        with c.fence:
                            c.provider_busy = False
                        await self._refresh(c)
                return
            await asyncio.sleep(.001)

    async def _render(self, c, text):
        rendered = (sanitize_render_text(text) + '\n').encode('utf-8')
        deadline = time.monotonic() + 1
        for offset in range(0, len(rendered), 4096):
            chunk = rendered[offset:offset + 4096]
            while c.session.pty.available_capacity < len(chunk):
                if c.failed or c.ended or time.monotonic() >= deadline:
                    self._fail(c, 'managed_render_gap')
                    self._schedule_close(c, 'recording_failed')
                    raise JournalError('managed_render_gap')
                await asyncio.sleep(.001)
            if c.session.pty.feed_output(chunk) is False:
                self._fail(c, 'managed_render_gap')
                self._schedule_close(c, 'recording_failed')
                raise JournalError('managed_render_gap')
            if offset % 32768 == 0:
                await asyncio.sleep(0)

    def _fail(self, c, reason):
        with c.fence:
            c.failed = c.failed or reason
            c.set_public_state('error', clear_permissions=True)
        # capture_failed may originate in a Hub reader thread. Timer mutation
        # stays on the event loop; the RAM fence already revokes positive writes.
        self._loop.call_soon_threadsafe(self._publish, c)

    def capture_failed(self, session_id):
        c = next((item for item in list(self._conversations.values()) if item.session.id == session_id), None)
        if c is not None:
            self._fail(c, 'managed_recording_failed')
            self._loop.call_soon_threadsafe(self._schedule_close, c, 'recording_failed')

    def _schedule_close(self, c, reason):
        if c.close_task is None:
            c.close_task = self._job(c, self._close(c, reason))
        return c.close_task

    async def _close(self, c, reason):
        with c.fence:
            c.closing = True
            c.set_public_state('error' if c.failed else 'closing', clear_permissions=True)
            self._cancel_timers(c)
        self._publish(c)
        if c.start_task is not None and not c.start_task.done() and c.start_task is not asyncio.current_task():
            c.start_task.cancel()
            await asyncio.gather(c.start_task, return_exceptions=True)
        try:
            await c.driver.close(reason)
        except Exception:
            self._fail(c, 'managed_close_failed')
            raise JournalError('managed_close_failed') from None
        finally:
            # close() must join physical termination, even if callbacks failed.
            if c.failed in {'managed_recording_failed', 'managed_render_gap'}:
                try:
                    await joined_io(self.store.append_event, c.session.journal_id,
                        dict(kind='capture_gap', actor='system', origin='managed', state='incomplete',
                            binding_id=c.binding['binding_id'],
                            source_record_id='managed-gap:' + c.session.process_generation,
                            metadata={'reason': c.failed}))
                except Exception:
                    pass  # RAM remains failed; unavailable storage cannot prove recovery.
            with c.fence:
                c.set_public_state('error' if c.failed else 'ended')
            self.events.end(c.session, self._runtime_snapshot(c))
            await asyncio.sleep(0)
            c.session.pty.feed_eof()
        jobs = [task for task in c.jobs if task is not asyncio.current_task() and not task.done()]
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        await asyncio.to_thread(c.session.reader_done.wait)
        with c.fence:
            c.set_public_state('error' if c.failed else 'ended')

    async def aclose(self):
        if self._shutdown_task is None:
            self._closed = True
            self._shutdown_task = asyncio.create_task(self._shutdown())
        try:
            await asyncio.shield(self._shutdown_task)
        except asyncio.CancelledError:
            await asyncio.shield(self._shutdown_task)
            raise

    async def _shutdown(self):
        # Closing physical streams unblocks writes/ACK waits held by in-flight
        # operations. Waiting for those operations first would deadlock close.
        tasks = [self._schedule_close(c, 'shutdown') for c in self._conversations.values()]
        if self._operations:
            await asyncio.gather(*list(self._operations), return_exceptions=True)
        tasks = list({*tasks, *(self._schedule_close(c, 'shutdown') for c in self._conversations.values())})
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self.events.close()
        if any(isinstance(result, BaseException) for result in results):
            raise JournalError('managed_close_failed')
