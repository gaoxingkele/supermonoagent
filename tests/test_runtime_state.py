"""Runtime projections share a generation while the journal owns recovery."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from supermonoagent.conversation_contract import JournalError
from supermonoagent.conversation_store import JournalStore
from supermonoagent.managed_conversation import ManagedConversationService
from supermonoagent.protocol import OP_RUNTIME_EVENT, unpack, unpack_json
from supermonoagent.runtime_state import ManagedRuntimeSnapshot, RuntimeEvent
from test_managed_conversation import Fixture
from test_managed_store import accept, managed


def test_live_projection_is_reused_and_older_generation_cannot_rewind(tmp_path):
    async def run():
        fixture = Fixture(tmp_path)
        try:
            await fixture.create()
            accepted = await fixture.submit()
            await fixture.started()
            sid = fixture.target['session_id']
            conversation = fixture.service._conversations[fixture.target['journal_id']]
            driver = fixture.drivers[0]
            with patch.object(ManagedRuntimeSnapshot, 'capture', wraps=ManagedRuntimeSnapshot.capture) as capture:
                await driver.emit(dict(kind='permission', permission_request_id='approve',
                    submission_id=accepted['submission_id'], provider_turn_id='turn', tool_name='tool',
                    details='transient private details', choices=['deny']))
                first = fixture.service._runtime_snapshot(conversation)
                assert capture.call_count == 1

                legacy = await fixture.call('managed_status', fixture.target)
                negotiated = await fixture.call('managed_status', fixture.target, event_cursor=True)
                next_viewer = await fixture.call('managed_status', fixture.target, event_cursor=True)
                assert negotiated == next_viewer
                assert {key: value for key, value in negotiated.items()
                        if key not in {'host_installation_id', 'runtime_epoch', 'revision'}} == legacy
                assert fixture.hub.sessions[sid].managed_snapshot()['state'] == legacy['state'] == 'waiting_permission'
                assert fixture.service.source_status(fixture.target['journal_id'])['managed_state'] == legacy['state']
                assert legacy['permissions'][0]['details'] == 'transient private details'
                assert capture.call_count == 1

                events = fixture.service.events
                track = events._tracks[sid]
                op, raw = unpack(track.recent[-1][1])
                assert op == OP_RUNTIME_EVENT
                public = unpack_json(raw)
                assert public['revision'] == negotiated['revision']
                assert public['state'] == negotiated['state']
                assert 'details' not in public['permissions'][0]
                assert 'receipt' not in public['active_submission']

                await driver.emit(dict(kind='permission_withdrawn', permission_request_id='approve'))
                newer = await fixture.call('managed_status', fixture.target, event_cursor=True)
                assert newer['state'] == 'running' and newer['permissions'] == []
                assert newer['revision'] > negotiated['revision']
                assert capture.call_count == 2
                events.observe(conversation.session, first, accepted)
                with pytest.raises(ValueError, match='Stale managed runtime snapshot'):
                    events.snapshot_cursor(conversation.session, first)
                assert await fixture.call('managed_status', fixture.target, event_cursor=True) == newer
                assert capture.call_count == 2
        finally:
            await fixture.finish()

    asyncio.run(run())


def test_reopened_journal_receipt_is_read_only_and_runtime_is_unverifiable(tmp_path):
    async def run():
        hub = SimpleNamespace(sessions={}, clients={})
        with JournalStore(tmp_path) as original:
            journal_id, target, _ = managed(original)
            accepted = accept(original, journal_id, target)
            manager = SimpleNamespace(store=original)
            before = ManagedConversationService(hub, manager, credential_authorized=lambda *_: False)
            old_cursor = before.events.snapshot_cursor(None, None)
            await before.aclose()

        with JournalStore(tmp_path) as reopened:
            manager = SimpleNamespace(store=reopened)
            recovered = ManagedConversationService(hub, manager, credential_authorized=lambda *_: False)
            request = {**target, 'nonce': 'submit-1'}
            try:
                legacy = await recovered.handle('managed_status', request, owner_scope='owner', owner_token='token')
                result = await recovered.handle('managed_status', request, owner_scope='owner', owner_token='token',
                                                event_cursor=True)
                assert legacy['state'] == result['state'] == 'ambiguous'
                assert legacy['last_outcome'] is None and result['last_outcome'] is None
                assert legacy['receipt'] == result['receipt']
                assert result['receipt']['event_id'] == accepted['event_id']
                assert result['receipt']['submission_id'] == accepted['submission_id']
                assert result['active_submission']['dispatch_state'] == 'ambiguous'
                assert 'runtime_verification' not in legacy
                assert result['runtime_verification'] == 'unverifiable'
                assert result['host_installation_id'] == old_cursor['host_installation_id']
                assert result['runtime_epoch'] != old_cursor['runtime_epoch'] and result['revision'] == 0
                for scope, rooms in [('other-owner', None), ('owner', ['wrong-room'])]:
                    with pytest.raises(JournalError):
                        await recovered.handle('managed_status', request, owner_scope=scope, owner_token='token',
                                               rooms=rooms, event_cursor=True)
                with pytest.raises(JournalError, match='managed_ambiguous'):
                    await recovered.handle('submit_managed', {**request, 'text': 'new input'},
                                           owner_scope='owner', owner_token='token')
                assert len(reopened.read_page('owner', journal_id, projection='conversation')['events']) == 1
            finally:
                await recovered.aclose()

    asyncio.run(run())


def test_projection_owns_its_nested_state_and_event_payload_is_scrubbed():
    target = dict(window_id='window', session_id='session', journal_id='journal', process_generation='process')
    descriptor = dict(policy={'sandbox': {'settings': ['checked']}})
    permissions = [dict(permission_request_id='permission', submission_id='submit', state='pending', choices=['deny'])]
    active = dict(submission_id='submit', cancel_version=0, body_sha256='private-body-hash')
    snapshot = ManagedRuntimeSnapshot.capture(target, state='waiting_permission', provider='codex',
        descriptor=descriptor, active=active, permissions=permissions, last_outcome=None)
    permission_correct = snapshot.control(details={'permission': 'private detail'})
    active['cancel_version'] = 7
    permissions[0]['choices'].append('allow_once')
    descriptor['policy']['sandbox']['settings'].clear()
    permission_correct['active_submission']['cancel_version'] = 6
    permission_correct['permissions'][0]['choices'].clear()

    event = RuntimeEvent('host', 'epoch', 4, target, snapshot.event(), dict(target, state='accepted',
        nonce='nonce', seq=1, event_id='receipt', user_message_uuid='private-user-uuid')).wire_payload()
    assert event['active_submission'] == {'submission_id': 'submit', 'cancel_version': 0}
    assert event['permissions'][0]['choices'] == ['deny']
    assert 'details' not in event['permissions'][0]
    assert 'user_message_uuid' not in event['receipt']
    assert snapshot.directory()['policy']['sandbox']['settings'] == ['checked']
    assert 'permissions' not in snapshot.directory()
    assert not {'history_epoch', 'stream_id', 'output_position'}.intersection(event)
