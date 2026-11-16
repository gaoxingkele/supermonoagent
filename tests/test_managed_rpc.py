import asyncio
import json
import time
from unittest.mock import patch

from supermonoagent.daemon import Daemon
from supermonoagent.protocol import OP_HELLO, OP_JOURNAL, unpack
from supermonoagent.protocol import pack_json
from supermonoagent.managed_conversation import ManagedConversationService
from test_managed_conversation import FakeDriver
from test_client_integration import isolated_host
from websockets.asyncio.client import connect
from supermonoagent.hub import Session
from test_windows import FakePty


class FakeManagedService:
    def __init__(self):
        self.calls = []
        self.closed = False

    async def handle(self, action, params, **kwargs):
        assert kwargs['authorize']()
        self.calls.append((action, params, kwargs['owner_scope']))
        return {'state': 'starting'}

    async def aclose(self):
        self.closed = True


def test_managed_control_requires_separate_negotiation_and_never_falls_to_archive(tmp_path):
    async def run():
        with patch('supermonoagent.daemon.STATE', tmp_path):
            daemon = Daemon('owner')
        daemon.hub.persist = False
        service = FakeManagedService()
        daemon.managed = daemon.hub.managed_service = service
        class JournalIdentity:
            def owner_scope(self, token, **_kwargs):
                assert token == 'owner'
                return 'stable-owner'
            async def aclose(self):
                pass
        daemon.journals = JournalIdentity()
        client = daemon.hub.attach(asyncio.Queue(), token='owner')
        request = json.dumps(dict(request_id='create-one', action='create_managed',
            params=dict(session_id='native', process_generation='generation', provider='claude', nonce='nonce'))).encode()
        client.conversation_journal = True
        frames, close = await daemon._op(client, OP_JOURNAL, request)
        assert close is None
        assert json.loads(unpack(frames[0])[1])['error'] == 'not_negotiated'
        assert not service.calls
        frames, _ = await daemon._op(client, OP_HELLO, json.dumps(dict(token='owner', multiplex=True,
            conversation_journal=1, managed_conversation=1)).encode())
        assert json.loads(unpack(frames[0])[1])['capabilities']['managed_conversation'] == 1
        frames, close = await daemon._op(client, OP_JOURNAL, request)
        assert close is None
        assert json.loads(unpack(frames[0])[1])['ok'] is True
        assert service.calls[0][0] == 'create_managed'
        bad = json.loads(request)
        bad['params']['cwd'] = 'C:/caller-selected'
        frames, _ = await daemon._op(client, OP_JOURNAL, json.dumps(bad).encode())
        assert json.loads(unpack(frames[0])[1])['ok'] is False
        assert len(service.calls) == 1
        await daemon.close_sidecars()
        assert service.closed
    asyncio.run(run())


def test_legacy_controls_cannot_close_or_reassign_managed_sessions(tmp_path):
    async def run():
        with patch('supermonoagent.daemon.STATE', tmp_path):
            daemon = Daemon('owner')
        daemon.hub.persist = False
        daemon.hub.pty_factory = lambda *_: FakePty()
        daemon.hub.attach_loop(asyncio.get_running_loop())
        group = daemon.hub.add_group('managed group', owner=daemon.token)
        window = daemon.hub.open_window(group_id=group.id, owner=daemon.token)
        target = Session('managed-guard', window.id, 'claude', '.', FakePty(),
                         interaction_mode='managed', provider='claude')
        daemon.hub.sessions[target.id] = window.sessions[target.id] = target
        owner = daemon.hub.attach(asyncio.Queue(), token=daemon.token)
        relay, code, generation = 'ws://127.0.0.1:6770/ws', 'room', object()
        daemon._relay_generations[relay] = generation
        daemon.relay_bindings.bind(relay, code, 'account-a', daemon.token)
        delegated = daemon.delegated_client(relay, code, generation, 1,
            {'account': 'account-a', 'expires': time.time() + 300}, 'relay-test')
        assert delegated is not None and daemon.client_authorized(delegated)
        try:
            for client in (owner, delegated):
                for op, body in [(9, {'session': target.id}), (9, {'window': window.id}),
                    (6, {'window': window.id, 'shell': 'cmd'}),
                    (15, {'action': 'move', 'window': window.id, 'group': 'default'}),
                    (15, {'action': 'delete', 'id': group.id}),
                    (15, {'action': 'rename', 'id': group.id, 'channel': 'different-room'})]:
                    frames, close = await daemon._op(client, op, json.dumps(body).encode())
                    assert close is None and frames
                    assert daemon.hub.sessions.get(target.id) is target
                    assert list(window.sessions) == [target.id]
                    assert window.group_id == group.id and group.id in daemon.hub.groups
                    assert daemon.hub.groups[group.id].channel != 'different-room'
            native_window = daemon.hub.open_window(owner=daemon.token)
            native = daemon.hub.open_cli(native_window.id, 'cmd')
            await daemon._op(delegated, 9, json.dumps({'session': native.id}).encode())
            assert native.id not in daemon.hub.sessions
        finally:
            for wid in list(daemon.hub.windows):
                daemon.hub.close_window(wid)
            await daemon.close_sidecars()
    asyncio.run(run())


def test_real_socket_managed_acceptance_uses_canonical_journal_and_keeps_native_session():
    async def run():
        async with isolated_host() as (daemon, direct, _relay):
            await daemon.managed.aclose()
            drivers = []
            def factory(*args, **kwargs):
                driver = FakeDriver(*args, **kwargs)
                drivers.append(driver)
                return driver
            service = ManagedConversationService(daemon.hub, daemon.journals,
                credential_authorized=daemon._managed_credential_authorized, driver_factory=factory)
            daemon.managed = daemon.hub.managed_service = daemon.journals.managed_sources = service
            original = daemon.hub.open_window('Native anchor', owner=daemon.token)
            anchor = daemon.hub.open_cli(original.id, 'cmd')
            async with connect(direct) as ws:
                async def result(op, rid=None):
                    async with asyncio.timeout(10):
                        async for raw in ws:
                            if not isinstance(raw, bytes):
                                continue
                            actual, body = unpack(raw)
                            if actual == op:
                                value = json.loads(body)
                                if rid is None or value.get('request_id') == rid:
                                    return value
                async def rpc(action, params, rid='rpc'):
                    await ws.send(pack_json(OP_JOURNAL, dict(request_id=rid, action=action, params=params)))
                    response = await result(OP_JOURNAL, rid)
                    assert response['ok'], response
                    return response['result']
                await ws.send(pack_json(OP_HELLO, dict(token=daemon.token, multiplex=True,
                    conversation_journal=1, managed_conversation=1)))
                hello = await result(2)
                assert hello['capabilities']['managed_conversation'] == 1
                created = await rpc('create_managed', dict(session_id=anchor.id,
                    process_generation=anchor.process_generation, provider='codex', nonce='new-mode'))
                target = {name: created[name] for name in ('window_id', 'session_id', 'process_generation', 'journal_id')}
                for _ in range(200):
                    state = await rpc('managed_status', target)
                    if state['state'] == 'ready':
                        break
                    await asyncio.sleep(.01)
                assert state['state'] == 'ready'
                accepted = await rpc('submit_managed', dict(target, nonce='human-one', text='original human request'))
                assert accepted['state'] == 'accepted'
                assert accepted['seq'] > 0
                again = await rpc('submit_managed', dict(target, nonce='human-one', text='original human request'))
                assert again['event_id'] == accepted['event_id']
                await asyncio.wait_for(drivers[0].entered.wait(), 3)
                assert len(drivers[0].sent) == 1
                page = await rpc('read_page', dict(journal_id=target['journal_id'], projection='conversation'))
                inputs = [item for item in page['events'] if item['kind'] == 'user_input']
                assert len(inputs) == 1 and inputs[0]['preview'] == 'original human request'
                assert daemon.hub.sessions[anchor.id] is anchor
                assert not anchor.pty.written
    asyncio.run(run())


def test_physical_credential_fence_binds_original_manager_and_service():
    async def run():
        async with isolated_host() as (daemon, _direct, _relay):
            manager, service = daemon.journals, daemon.managed
            window = daemon.hub.open_window(owner=daemon.token)
            target = daemon.hub.open_cli(window.id, 'cmd')
            owner = manager.owner_scope(daemon.token)
            room = daemon.hub.groups[window.group_id].channel
            allowed = service.credential_authorized
            assert allowed(owner, daemon.token, room, target)
            daemon.managed = object()
            assert not allowed(owner, daemon.token, room, target)
            daemon.managed = service
            daemon.hub.managed_service = object()
            assert not allowed(owner, daemon.token, room, target)
            daemon.hub.managed_service = service
            daemon.journals = object()
            assert not allowed(owner, daemon.token, room, target)
            daemon.journals = manager
            assert allowed(owner, daemon.token, room, target)
    asyncio.run(run())
