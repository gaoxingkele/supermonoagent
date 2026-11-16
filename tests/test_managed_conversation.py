"""Managed coordinator integration with real durable storage and fake providers."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from supermonoagent.conversation_contract import JournalError
from supermonoagent.conversation_store import JournalStore
from supermonoagent.hub import Session
from supermonoagent.managed_conversation import ManagedConversationService


class FakeDriver:
    def __init__(self, provider, cwd, binding, emit, can_send, **kwargs):
        self.provider, self.binding, self.emit, self.can_send = provider, binding, emit, can_send
        self.sent, self.interrupts, self.answers = [], [], []
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.release.set()
        self.ack = asyncio.Event()
        self.ack.set()
        self.closed = False
        self.state = 'starting'
        self.close_gate = asyncio.Event()
        self.close_gate.set()

    async def start(self):
        assert self.can_send({'operation': 'initialize'})
        await self.emit(dict(kind='ready', provider=self.provider, provider_version='0.167.0',
            provider_root_id='root', policy={'mode': 'default'}))
        self.state = 'ready'
        return self.snapshot()

    def snapshot(self):
        return {'state': self.state, 'provider': self.provider}

    async def submit(self, submission):
        self.state = 'running'
        self.entered.set()
        await self.release.wait()
        self.active = submission
        if not self.can_send(dict(operation='submit', submission_id=submission['submission_id'],
                                 cancel_version=submission['cancel_version'])):
            self.state = 'ready'
            return {'state': 'not_dispatched'}
        self.sent.append(submission)
        await self.ack.wait()
        await self.emit(dict(kind='turn_started', submission_id=submission['submission_id'],
            user_message_uuid=submission['user_message_uuid'], provider_root_id='root', provider_turn_id='turn'))

    async def interrupt(self, submission_id, cancel_version):
        self.interrupts.append((submission_id, cancel_version))
        return {'state': 'interrupt_requested'}

    async def respond_permission(self, permission_request_id, decision, *, submission_id, cancel_version):
        self.entered.set()
        await self.release.wait()
        if self.can_send(dict(operation='permission', permission_request_id=permission_request_id,
                decision=decision, submission_id=submission_id, cancel_version=cancel_version)):
            self.answers.append(decision)
        await self.emit(dict(kind='permission_withdrawn', permission_request_id=permission_request_id))
        return {'state': 'answered'}

    async def close(self, reason='closed'):
        await self.close_gate.wait()
        self.closed = True
        self.state = 'ended'
        await self.emit(dict(kind='closed', reason='closed'))


class Fixture:
    def __init__(self, path):
        self.store = JournalStore(path)
        self.anchor = Session('native', 'native-window', 'powershell', 'D:/synthetic', object(),
                              process_generation='native-gen')
        self.hub = SimpleNamespace(sessions={'native': self.anchor},
            windows={'native-window': SimpleNamespace(group_id='group')},
            groups={'group': SimpleNamespace(channel='room', owner='token')})
        self.hub.add_managed_session = self.add_session
        self.journals = SimpleNamespace(store=self.store, source_epoch='epoch',
            register_managed_session=lambda *args, **kwargs: None,
            capture=SimpleNamespace(status=lambda jid: {'error': None}))
        self.drivers = []
        self.authorized = True
        self.service = ManagedConversationService(self.hub, self.journals,
            credential_authorized=lambda scope, token, room, session: (
                self.authorized and scope == 'owner' and token == 'token' and room == 'room'),
            driver_factory=self.factory)

    def add_session(self, session, *, group_id, title):
        self.hub.sessions[session.id] = session
        self.hub.windows[session.window_id] = SimpleNamespace(group_id=group_id)
        # No actual Hub reader in this fixture; lifecycle tests control this event.
        session.reader_done.set()

    def factory(self, *args, **kwargs):
        driver = FakeDriver(*args, **kwargs)
        self.drivers.append(driver)
        return driver

    async def call(self, action, params, **kwargs):
        return await self.service.handle(action, params, owner_scope='owner', owner_token='token', **kwargs)

    async def create(self):
        result = await self.call('create_managed', dict(session_id='native', process_generation='native-gen',
            provider='codex', nonce='create'))
        self.target = {key: result[key] for key in ['window_id', 'session_id', 'process_generation', 'journal_id']}
        for _ in range(200):
            status = await self.call('managed_status', self.target)
            if status['state'] == 'ready':
                return result
            await asyncio.sleep(.001)
        raise AssertionError('provider never ready')

    async def submit(self, nonce='submit', **kwargs):
        return await self.call('submit_managed', dict(self.target, nonce=nonce, text='original\x1b[31m🙂'), **kwargs)

    async def finish(self):
        for driver in self.drivers:
            driver.release.set()
            driver.ack.set()
            driver.close_gate.set()
        await self.service.aclose()
        self.store.close()

    async def started(self):
        for _ in range(200):
            status = await self.call('managed_status', self.target)
            if status.get('active_submission', {}).get('provider_turn_id'):
                return status['active_submission']
            await asyncio.sleep(.001)
        raise AssertionError('turn never associated')

    async def settle(self, accepted, text='final\x1b[31m🙂'):
        driver = self.drivers[0]
        await driver.emit(dict(kind='settled', submission_id=accepted['submission_id'],
            user_message_uuid=accepted['user_message_uuid'], provider_root_id='root', provider_turn_id='turn',
            outcome='completed', finals=[dict(provider_record_id='item', text=text,
                proof={'kind': 'provider_final_phase', 'phase': 'final_answer', 'terminal_status': 'completed'})]))
        driver.state = 'ready'

    async def idle(self):
        for _ in range(200):
            if (await self.call('managed_status', self.target))['state'] == 'ready':
                return
            await asyncio.sleep(.001)
        raise AssertionError('turn never idle')


def test_durable_acceptance_fast_ack_and_nonce_recovery(tmp_path):
    async def run():
        f = Fixture(tmp_path)
        try:
            await f.create()
            driver = f.drivers[0]
            driver.ack.clear()
            accepted = await asyncio.wait_for(f.submit(), 1)
            assert accepted['state'] == 'accepted'
            await driver.entered.wait()
            assert f.store.read_page('owner', f.target['journal_id'], projection='conversation')['events'][0]['kind'] == 'user_input'
            again = await f.submit()
            assert again['event_id'] == accepted['event_id'] and again['replayed']
            with pytest.raises(JournalError, match='busy'):
                await f.submit('other')
            status = await f.call('managed_status', dict(f.target, nonce='submit'))
            assert status['receipt']['event_id'] == accepted['event_id']
            assert len(driver.sent) == 1
        finally:
            await f.finish()
    asyncio.run(run())


def test_closed_removed_target_recovers_receipt_readonly_with_store_authorization(tmp_path):
    async def run():
        f = Fixture(tmp_path)
        try:
            await f.create()
            accepted = await f.submit()
            await f.started()
            await f.settle(accepted)
            await f.idle()
            await f.call('close_managed', dict(f.target, nonce='close'))
            for _ in range(200):
                if (await f.call('managed_status', f.target))['state'] == 'ended':
                    break
                await asyncio.sleep(.001)
            f.hub.sessions.pop(f.target['session_id'])
            f.hub.windows.pop(f.target['window_id'])
            # Closed receipts use the same owner/room read boundary as restart
            # recovery; a physical-process credential no longer exists.
            f.service.credential_authorized = lambda *args: False
            status = await f.call('managed_status', dict(f.target, nonce='submit'))
            assert status['state'] == 'ended'
            assert status['receipt']['event_id'] == accepted['event_id']
            assert status['receipt']['submission_id'] == accepted['submission_id']
            assert len(f.drivers) == 1 and len(f.drivers[0].sent) == 1
            for invalid in [dict(f.target, process_generation='stale'), dict(f.target, session_id='other')]:
                with pytest.raises(JournalError):
                    await f.call('managed_status', dict(invalid, nonce='submit'))
            with pytest.raises(JournalError):
                await f.call('managed_status', dict(f.target, nonce='submit'), rooms=['other-room'])
            with pytest.raises(JournalError):
                await f.service.handle('managed_status', dict(f.target, nonce='submit'),
                    owner_scope='other-owner', owner_token='token')
            with pytest.raises(JournalError):
                await f.call('managed_status', dict(f.target, nonce='submit'), authorize=lambda: False)
            with pytest.raises(JournalError):
                await f.submit('another')
            with pytest.raises(JournalError):
                await f.call('interrupt_managed', dict(f.target,
                    submission_id=accepted['submission_id'], nonce='old-cancel'))
        finally:
            await f.finish()
    asyncio.run(run())


def test_creation_retry_does_not_relaunch_and_changed_request_conflicts(tmp_path):
    async def run():
        f = Fixture(tmp_path)
        try:
            original = await f.create()
            params = dict(session_id='native', process_generation='native-gen', provider='codex', nonce='create')
            again = await f.call('create_managed', params)
            assert again['journal_id'] == original['journal_id'] and len(f.drivers) == 1
            with pytest.raises(JournalError, match='creation_conflict'):
                await f.call('create_managed', dict(params, provider='claude'))
            replacement = ManagedConversationService(f.hub, f.journals,
                credential_authorized=lambda *args: True, driver_factory=f.factory)
            prior = await replacement.handle('create_managed', params, owner_scope='owner', owner_token='token')
            assert prior['state'] == 'ambiguous' and len(f.drivers) == 1
            await replacement.aclose()
        finally:
            await f.finish()
    asyncio.run(run())


def test_create_receipt_survives_closed_anchor_and_rejects_changed_request_or_room(tmp_path):
    async def run():
        f = Fixture(tmp_path)
        try:
            original = await f.create()
            params = dict(session_id='native', process_generation='native-gen', provider='codex', nonce='create')
            f.hub.sessions.pop('native')
            f.hub.windows.pop('native-window')
            again = await f.call('create_managed', params)
            assert all(again[key] == original[key] for key in f.target)
            assert again['cwd'] == original['cwd'] and len(f.drivers) == 1
            for changed in [dict(params, session_id='other'), dict(params, process_generation='replacement'),
                            dict(params, provider='claude')]:
                with pytest.raises(JournalError, match='creation_conflict'):
                    await f.call('create_managed', changed)
            with pytest.raises(JournalError):
                await f.call('create_managed', params, rooms=['another-room'])
            with pytest.raises(JournalError):
                await f.call('create_managed', dict(params, nonce='new-creation'))
            # No controller after restart still recovers the original receipt.
            replacement = ManagedConversationService(f.hub, f.journals,
                credential_authorized=lambda *args: True, driver_factory=f.factory)
            recovered = await replacement.handle('create_managed', params, owner_scope='owner', owner_token='token')
            assert recovered['journal_id'] == original['journal_id'] and recovered['state'] == 'ambiguous'
            assert len(f.drivers) == 1
            await replacement.aclose()
        finally:
            await f.finish()
    asyncio.run(run())


def test_cancel_before_physical_send_and_awaiting_ack(tmp_path):
    async def run():
        f = Fixture(tmp_path)
        try:
            await f.create()
            driver = f.drivers[0]
            driver.release.clear()
            accepted = await f.submit()
            await driver.entered.wait()
            cancel = await f.call('interrupt_managed', dict(f.target, submission_id=accepted['submission_id'], nonce='cancel'))
            assert cancel['state'] == 'interrupt_requested'
            driver.release.set()
            await asyncio.sleep(.02)
            assert driver.sent == []
            assert driver.interrupts == [(accepted['submission_id'], 1)]
            again = await f.call('interrupt_managed', dict(f.target, submission_id=accepted['submission_id'], nonce='cancel'))
            assert again['replayed'] and len(driver.interrupts) == 1
        finally:
            await f.finish()
    asyncio.run(run())


def test_queued_allow_fenced_by_withdrawal_and_no_raw_details_persisted(tmp_path):
    async def run():
        f = Fixture(tmp_path)
        try:
            await f.create()
            accepted = await f.submit()
            driver = f.drivers[0]
            for _ in range(200):
                state = await f.call('managed_status', f.target)
                if state.get('active_submission', {}).get('provider_turn_id'):
                    break
                await asyncio.sleep(.001)
            await driver.emit(dict(kind='permission', permission_request_id='perm', submission_id=accepted['submission_id'],
                provider_turn_id='turn', tool_name='test', details={'command': 'SENSITIVE_TRANSIENT'},
                choices=['allow_once', 'deny'], requires_user_interaction=False))
            status = await f.call('managed_status', f.target)
            assert json.loads(status['permissions'][0]['details'])['command'] == 'SENSITIVE_TRANSIENT'
            control = f.store.read_managed_control('owner', f.target['journal_id'], target=f.target)
            assert 'details' not in control['permissions'][0]
            driver.release.clear()
            driver.entered.clear()
            response = asyncio.create_task(f.call('respond_managed_permission', dict(f.target,
                submission_id=accepted['submission_id'], provider_turn_id='turn', permission_request_id='perm',
                decision='allow_once', nonce='answer')))
            await driver.entered.wait()
            await driver.emit(dict(kind='permission_withdrawn', permission_request_id='perm'))
            driver.release.set()
            await response
            assert driver.answers == []
        finally:
            await f.finish()
    asyncio.run(run())


def test_exact_target_owner_room_and_capture_failure(tmp_path):
    async def run():
        f = Fixture(tmp_path)
        try:
            await f.create()
            with pytest.raises(JournalError):
                await f.call('managed_status', dict(f.target, process_generation='stale'))
            with pytest.raises(JournalError):
                await f.call('managed_status', f.target, rooms=['wrong'])
            f.authorized = False
            with pytest.raises(JournalError):
                await f.submit()
            f.authorized = True
            f.service.capture_failed(f.target['session_id'])
            with pytest.raises(JournalError):
                await f.submit()
            assert f.drivers[0].sent == []
        finally:
            await f.finish()
    asyncio.run(run())


def test_shutdown_waits_for_physical_close_and_hub_reader(tmp_path):
    async def run():
        f = Fixture(tmp_path)
        await f.create()
        driver = f.drivers[0]
        driver.close_gate.clear()
        session = f.hub.sessions[f.target['session_id']]
        session.reader_done.clear()
        closing = asyncio.create_task(f.service.aclose())
        await asyncio.sleep(.01)
        assert not closing.done()
        driver.close_gate.set()
        await asyncio.sleep(.01)
        assert driver.closed and not closing.done()
        session.reader_done.set()
        await asyncio.wait_for(closing, 1)
        assert f.hub.sessions['native'] is f.anchor
        f.store.close()
    asyncio.run(run())


def test_real_commit_precedes_dispatch_and_failure_keeps_original_input(tmp_path):
    async def run():
        f = Fixture(tmp_path)
        entered, release = threading.Event(), threading.Event()
        try:
            await f.create()
            accept = f.store.accept_managed_submission
            def held(*args, **kwargs):
                entered.set()
                assert release.wait(3)
                return accept(*args, **kwargs)
            f.store.accept_managed_submission = held
            pending = asyncio.create_task(f.submit())
            assert await asyncio.to_thread(entered.wait, 1)
            assert f.drivers[0].sent == []
            assert f.store.read_page('owner', f.target['journal_id'], projection='conversation')['events'] == []
            def disk_failed(*args, **kwargs):
                raise JournalError('storage_unavailable')
            f.store.mark_managed_dispatching = disk_failed
            release.set()
            accepted = await pending
            for _ in range(200):
                if f.drivers[0].closed:
                    break
                await asyncio.sleep(.001)
            assert accepted['state'] == 'accepted' and f.drivers[0].sent == []
            events = f.store.read_page('owner', f.target['journal_id'], projection='conversation')['events']
            assert events[0]['kind'] == 'user_input'
            assert f.store.content_bytes(f.target['journal_id'], events[0]['content_id']).decode() == 'original\x1b[31m🙂'
        finally:
            release.set()
            await f.finish()
    asyncio.run(run())


@pytest.mark.parametrize('revoke', [False, True])
def test_disconnect_during_commit_uses_stable_credential_fence(tmp_path, revoke):
    async def run():
        f = Fixture(tmp_path)
        entered, release = threading.Event(), threading.Event()
        try:
            await f.create()
            accept = f.store.accept_managed_submission
            def held(*args, **kwargs):
                entered.set()
                assert release.wait(3)
                return accept(*args, **kwargs)
            f.store.accept_managed_submission = held
            pending = asyncio.create_task(f.submit())
            assert await asyncio.to_thread(entered.wait, 1)
            pending.cancel()
            f.authorized = not revoke
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            for _ in range(200):
                control = f.store.read_managed_control('owner', f.target['journal_id'], target=f.target, nonce='submit')
                if f.drivers[0].sent or control['active_submission'] is None:
                    break
                await asyncio.sleep(.001)
            assert control['receipt']['state'] == 'accepted'
            assert len(f.drivers[0].sent) == (0 if revoke else 1)
            if revoke:
                assert control['active_submission'] is None
        finally:
            release.set()
            f.authorized = True
            await f.finish()
    asyncio.run(run())


def test_cancel_intent_settles_without_driver_call(tmp_path):
    async def run():
        f = Fixture(tmp_path)
        release = asyncio.Event()
        try:
            await f.create()
            dispatch = f.service._dispatch
            async def held(*args):
                await release.wait()
                return await dispatch(*args)
            f.service._dispatch = held
            accepted = await f.submit()
            result = await f.call('interrupt_managed', dict(f.target,
                submission_id=accepted['submission_id'], nonce='cancel'))
            assert result['state'] == 'not_dispatched'
            release.set()
            await asyncio.sleep(.01)
            status = await f.call('managed_status', f.target)
            assert status['active_submission'] is None
            assert f.drivers[0].sent == f.drivers[0].interrupts == []
        finally:
            release.set()
            await f.finish()
    asyncio.run(run())


def test_completed_proof_survives_requested_cancel_and_releases_after_callback(tmp_path):
    async def run():
        f = Fixture(tmp_path)
        try:
            await f.create()
            accepted = await f.submit()
            await f.started()
            await f.call('interrupt_managed', dict(f.target, submission_id=accepted['submission_id'], nonce='cancel'))
            driver = f.drivers[0]
            event = dict(kind='settled', submission_id=accepted['submission_id'],
                user_message_uuid=accepted['user_message_uuid'], provider_root_id='root', provider_turn_id='turn',
                outcome='completed', finals=[dict(provider_record_id='item', text='final\x1b[31m🙂',
                    proof={'kind': 'provider_final_phase', 'terminal_status': 'completed'})])
            await driver.emit(event)
            # Store is idle but provider has not returned from its settle path.
            status = await f.call('managed_status', f.target)
            assert status['active_submission'] is None and status['state'] == 'running'
            with pytest.raises(JournalError, match='busy'):
                await f.submit('second')
            driver.state = 'ready'
            await f.idle()
            await driver.emit(event)  # Exact duplicate cannot append a final.
            events = f.store.read_page('owner', f.target['journal_id'], projection='conversation')['events']
            finals = [event for event in events if event['kind'] == 'assistant_final']
            assert len(finals) == 1
            assert f.store.content_bytes(f.target['journal_id'], finals[0]['content_id']).decode() == 'final\x1b[31m🙂'
            raw = f.hub.sessions[f.target['session_id']].pty.read()
            assert b'\x1b' not in raw and b'\\x1b[31m' in raw
            second = await f.submit('second')
            assert second['submission_id'] != accepted['submission_id']
        finally:
            await f.finish()
    asyncio.run(run())


def test_failed_provider_outcome_keeps_input_and_reports_bounded_last_result(tmp_path):
    async def run():
        f = Fixture(tmp_path)
        try:
            await f.create()
            accepted = await f.submit()
            await f.started()
            driver = f.drivers[0]
            await driver.emit(dict(kind='settled', submission_id=accepted['submission_id'],
                user_message_uuid=accepted['user_message_uuid'], provider_root_id='root', provider_turn_id='turn',
                outcome='failed', reason='auth_failed', finals=[]))
            driver.state = 'ready'
            await f.idle()
            expected = dict(submission_id=accepted['submission_id'], outcome='failed',
                            reason='auth_failed', has_final=False)
            status = await f.call('managed_status', f.target)
            assert status['last_outcome'] == expected
            assert status['active_submission'] is None and status['state'] == 'ready'
            session = f.hub.sessions[f.target['session_id']]
            assert session.managed_snapshot()['last_outcome'] == expected
            assert f.service.source_status(f.target['journal_id'])['healthy']
            events = f.store.read_page('owner', f.target['journal_id'], projection='conversation')['events']
            assert [event['kind'] for event in events] == ['user_input']
            assert f.store.content_bytes(f.target['journal_id'], events[0]['content_id']).decode() == 'original\x1b[31m🙂'
            # Ordinary provider failure leaves recording healthy for a new input.
            assert (await f.submit('next'))['state'] == 'accepted'
        finally:
            await f.finish()
    asyncio.run(run())


def test_permission_is_once_only_and_large_details_are_deny_only(tmp_path):
    async def run():
        f = Fixture(tmp_path)
        try:
            await f.create()
            accepted = await f.submit()
            await f.started()
            driver = f.drivers[0]
            event = dict(kind='permission', permission_request_id='perm', submission_id=accepted['submission_id'],
                provider_turn_id='turn', tool_name='test', details={'command': 'synthetic'},
                choices=['allow_once', 'deny'], requires_user_interaction=False)
            await driver.emit(event)
            params = dict(f.target, submission_id=accepted['submission_id'], provider_turn_id='turn',
                permission_request_id='perm', decision='allow_once', nonce='answer')
            first = await f.call('respond_managed_permission', params)
            again = await f.call('respond_managed_permission', params)
            assert first['state'] == 'decided' and again['replayed']
            assert driver.answers == ['allow_once']
            await driver.emit(dict(event, permission_request_id='large', details={'text': 'x' * 65537}))
            status = await f.call('managed_status', f.target)
            permission = status['permissions'][0]
            assert permission['choices'] == ['deny'] and isinstance(permission['details'], str)
            assert len(permission['details']) < 200
            with pytest.raises(JournalError):
                await f.call('respond_managed_permission', dict(params, permission_request_id='large', nonce='bad'))
        finally:
            await f.finish()
    asyncio.run(run())


def test_codex_dictionary_permission_is_visible_string_on_wire_and_transient_only(tmp_path):
    async def run():
        f = Fixture(tmp_path)
        try:
            await f.create()
            accepted = await f.submit()
            await f.started()
            raw = {'command': 'echo <script>synthetic</script>', 'cwd': 'D:/synthetic',
                   'reason': 'synthetic command approval', 'requiresNetworkApproval': False}
            await f.drivers[0].emit(dict(kind='permission', permission_request_id='codex-permission',
                submission_id=accepted['submission_id'], provider_turn_id='turn', tool_name='shell',
                details=raw, choices=['allow_once', 'deny'], requires_user_interaction=False))
            status = await f.call('managed_status', f.target)
            permission = status['permissions'][0]
            assert isinstance(permission['details'], str)
            assert json.loads(permission['details']) == raw
            assert permission['choices'] == ['allow_once', 'deny']
            raw['command'] = 'mutated after emission'
            assert json.loads(permission['details'])['command'] == 'echo <script>synthetic</script>'
            durable = f.store.read_managed_control('owner', f.target['journal_id'], target=f.target)
            assert 'details' not in durable['permissions'][0]
            descriptors = f.store.read_page('owner', f.target['journal_id'])['events']
            assert all('synthetic command approval' not in json.dumps(event) for event in descriptors)
        finally:
            await f.finish()
    asyncio.run(run())


def test_large_final_rendering_drains_without_loss(tmp_path):
    async def run():
        f = Fixture(tmp_path)
        chunks = []
        reader = None
        try:
            await f.create()
            pty = f.hub.sessions[f.target['session_id']].pty
            async def drain():
                while True:
                    data = await asyncio.to_thread(pty.read)
                    if not data:
                        return
                    chunks.append(data)
            reader = asyncio.create_task(drain())
            accepted = await f.submit()
            await f.started()
            await f.settle(accepted, 'z' * (2 * 1024 * 1024))
            await f.idle()
            assert not pty.overflowed
        finally:
            await f.finish()
            if reader:
                await reader
        assert b''.join(chunks).endswith(b'z' * (2 * 1024 * 1024) + b'\n')
    asyncio.run(run())


def test_permission_expiry_denies_without_fabricated_human_decision(tmp_path):
    async def run():
        f = Fixture(tmp_path)
        try:
            await f.create()
            accepted = await f.submit()
            await f.started()
            register = f.store.register_managed_permission
            def short(*args, **kwargs):
                import time
                return register(*args, **kwargs, expires_at=time.time() + .03)
            f.store.register_managed_permission = short
            driver = f.drivers[0]
            await driver.emit(dict(kind='permission', permission_request_id='expiring',
                submission_id=accepted['submission_id'], provider_turn_id='turn', tool_name='test',
                details='synthetic', choices=['allow_once', 'deny'], requires_user_interaction=False))
            for _ in range(200):
                if driver.answers:
                    break
                await asyncio.sleep(.001)
            assert driver.answers == ['deny']
            control = f.store.read_managed_control('owner', f.target['journal_id'], target=f.target)
            assert control['permissions'] == []
            assert not control['active_submission']['cancel_requested']
            page = f.store.read_page('owner', f.target['journal_id'])
            assert not any(event.get('metadata', {}).get('decision') for event in page['events'])
        finally:
            await f.finish()
    asyncio.run(run())


def test_claude_bootstrap_accepts_input_but_unconfirmed_root_cannot_allow(tmp_path):
    async def run():
        f = Fixture(tmp_path)
        try:
            original_factory = f.factory
            def factory(*args, **kwargs):
                driver = original_factory(*args, **kwargs)
                async def start():
                    await driver.emit(dict(kind='ready', provider='claude', provider_version='2.1.310',
                        provider_root_id=None, requested_root_id='root', provider_root_confirmed=False,
                        policy={'permission_mode': 'default'}))
                    driver.state = 'ready'
                driver.start = start
                async def submit(submission):
                    driver.state = 'running'
                    driver.active = submission
                    if driver.can_send(dict(operation='submit', submission_id=submission['submission_id'], cancel_version=0)):
                        driver.sent.append(submission)
                driver.submit = submit
                return driver
            f.service.driver_factory = factory
            result = await f.call('create_managed', dict(session_id='native', process_generation='native-gen',
                provider='claude', nonce='bootstrap'))
            f.target = {key: result[key] for key in ['window_id', 'session_id', 'process_generation', 'journal_id']}
            await f.idle()
            status = await f.call('managed_status', f.target)
            assert status['provider_root_id'] is None and not status['provider_root_confirmed']
            accepted = await f.submit()
            driver = f.drivers[0]
            for _ in range(200):
                if driver.sent:
                    break
                await asyncio.sleep(.001)
            assert len(driver.sent) == 1
            await driver.emit(dict(kind='permission', permission_request_id='bootstrap-perm',
                submission_id=accepted['submission_id'], provider_turn_id=None, tool_name='test',
                details='synthetic', choices=['allow_once', 'deny'], requires_user_interaction=False))
            with pytest.raises(JournalError, match='managed_root_unconfirmed'):
                await f.call('respond_managed_permission', dict(f.target,
                    submission_id=accepted['submission_id'], permission_request_id='bootstrap-perm',
                    decision='allow_once', nonce='blind-allow'))
            assert driver.answers == []
            await driver.emit(dict(kind='ready', provider='claude', provider_version='2.1.310',
                provider_root_id='root', requested_root_id='root', provider_root_confirmed=True,
                policy={'permission_mode': 'default'}))
            assert (await f.call('managed_status', f.target))['state'] == 'waiting_permission'
        finally:
            await f.finish()
    asyncio.run(run())


def test_cancel_transition_blocks_queued_allow_before_cancel_commit(tmp_path):
    async def run():
        f = Fixture(tmp_path)
        entered, release = threading.Event(), threading.Event()
        try:
            await f.create()
            accepted = await f.submit()
            await f.started()
            driver = f.drivers[0]
            await driver.emit(dict(kind='permission', permission_request_id='perm', submission_id=accepted['submission_id'],
                provider_turn_id='turn', tool_name='test', details='synthetic',
                choices=['allow_once', 'deny'], requires_user_interaction=False))
            driver.release.clear()
            driver.entered.clear()
            answer = asyncio.create_task(f.call('respond_managed_permission', dict(f.target,
                submission_id=accepted['submission_id'], provider_turn_id='turn', permission_request_id='perm',
                decision='allow_once', nonce='answer')))
            await driver.entered.wait()
            cancel = f.store.cancel_managed_submission
            def held(*args, **kwargs):
                entered.set()
                assert release.wait(3)
                return cancel(*args, **kwargs)
            f.store.cancel_managed_submission = held
            interrupt = asyncio.create_task(f.call('interrupt_managed', dict(f.target,
                submission_id=accepted['submission_id'], nonce='cancel')))
            assert await asyncio.to_thread(entered.wait, 1)
            driver.release.set()
            await asyncio.sleep(.02)
            assert driver.answers == []
            release.set()
            await asyncio.gather(answer, interrupt)
        finally:
            release.set()
            await f.finish()
    asyncio.run(run())
