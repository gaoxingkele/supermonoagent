"""Bounded managed state replay with real direct and Relay viewer sockets."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from websockets.asyncio.client import connect

from supermonoagent.hub import Client
from supermonoagent.managed_conversation import ManagedConversationService
from supermonoagent.managed_events import CURSOR, TARGET, ManagedEvents, RETAINED_REVISIONS
from supermonoagent.protocol import OP_HELLO, OP_HELLO_OK, OP_JOURNAL, OP_RUNTIME_EVENT, OP_SUBSCRIBE
from supermonoagent.protocol import pack_json, unpack
from test_client_integration import isolated_host, receive_op
from test_managed_conversation import FakeDriver


async def _hello(ws, daemon, *, events, relay=False):
    if relay:
        await ws.send(json.dumps(dict(op='phone', code='local-test', token=daemon.token, reliable=True)))
        assert json.loads(await ws.recv())['op'] == 'ready'
    await ws.send(pack_json(OP_HELLO, dict(token=daemon.token, multiplex=True, reliable=True,
        conversation_journal=1, managed_conversation=1, managed_events=events)))
    return json.loads(await receive_op(ws, OP_HELLO_OK))


async def _rpc(ws, action, params):
    await ws.send(pack_json(OP_JOURNAL, dict(request_id=action, action=action, params=params)))
    response = json.loads(await receive_op(ws, OP_JOURNAL))
    assert response['ok'], response
    return response['result']


async def _event(ws, predicate):
    async with asyncio.timeout(4):
        while True:
            body = json.loads(await receive_op(ws, OP_RUNTIME_EVENT))
            if predicate(body):
                return body


@pytest.mark.parametrize('relay', [False, True])
def test_live_events_permission_details_legacy_and_replay(relay):
    async def run():
        async with isolated_host() as (daemon, direct, relay_url):
            await daemon.managed.aclose()
            drivers = []

            def factory(*args, **kwargs):
                driver = FakeDriver(*args, **kwargs)
                drivers.append(driver)
                return driver

            service = ManagedConversationService(daemon.hub, daemon.journals,
                credential_authorized=daemon._managed_credential_authorized, driver_factory=factory)
            daemon.managed = daemon.hub.managed_service = daemon.journals.managed_sources = service
            anchor_window = daemon.hub.open_window(owner=daemon.token)
            anchor = daemon.hub.open_cli(anchor_window.id, 'cmd')
            viewer_url = relay_url if relay else direct
            async with connect(viewer_url) as old, connect(viewer_url) as ws:
                old_hello = await _hello(old, daemon, events=0, relay=relay)
                new_hello = await _hello(ws, daemon, events=1, relay=relay)
                assert 'managed_events' not in old_hello['capabilities']
                assert new_hello['capabilities']['managed_events'] == 1
                created = await _rpc(ws, 'create_managed', dict(session_id=anchor.id,
                    process_generation=anchor.process_generation, provider='codex', nonce='managed-create'))
                target = {key: created[key] for key in TARGET}
                subscription = dict(sessions=[target['session_id']])
                await old.send(pack_json(OP_SUBSCRIBE, subscription))
                await ws.send(pack_json(OP_SUBSCRIBE, subscription))
                async with asyncio.timeout(3):
                    while drivers[0].state != 'ready':
                        await asyncio.sleep(.005)
                with patch.object(service.store, 'read_managed_control', wraps=service.store.read_managed_control) as read:
                    baseline = await _rpc(ws, 'managed_status', target)
                    repeated = await _rpc(ws, 'managed_status', target)
                    assert baseline == repeated and baseline['state'] == 'ready' and read.call_count == 0
                    assert baseline['host_installation_id'] == new_hello['host_installation_id']
                    assert 'revision' not in await _rpc(old, 'managed_status', target)
                    assert read.call_count == 1

                accepted = await _rpc(ws, 'submit_managed', {**target, 'nonce': 'send-once', 'text': 'private source text'})
                recorded = await _event(ws, lambda body: bool(body.get('receipt')))
                assert recorded['receipt']['event_id'] == accepted['event_id']
                assert recorded['receipt']['nonce'] == 'send-once'
                assert 'private source text' not in json.dumps(recorded)
                assert 'body_sha256' not in recorded['active_submission']
                if recorded['active_submission'].get('provider_turn_id') != 'turn':
                    await _event(ws, lambda body: (body.get('active_submission') or {}).get('provider_turn_id') == 'turn')
                driver = drivers[0]
                await driver.emit(dict(kind='permission', permission_request_id='allow', submission_id=accepted['submission_id'],
                    provider_turn_id='turn', tool_name='Test', details='private permission payload',
                    choices=['allow_once', 'deny']))
                waiting = await _event(ws, lambda body: body.get('state') == 'waiting_permission')
                assert waiting['permissions'][0]['permission_request_id'] == 'allow'
                assert 'private permission payload' not in json.dumps(waiting)
                assert 'details' not in waiting['permissions'][0]
                detailed = await _rpc(ws, 'managed_status', target)
                assert detailed['revision'] == waiting['revision']
                assert detailed['permissions'][0]['details'] == 'private permission payload'
                with pytest.raises(TimeoutError):
                    await receive_op(old, OP_RUNTIME_EVENT, timeout=.1)

            await driver.emit(dict(kind='permission_withdrawn', permission_request_id='allow'))
            async with connect(viewer_url) as restored:
                await _hello(restored, daemon, events=1, relay=relay)
                await restored.send(pack_json(OP_SUBSCRIBE, {**subscription, 'managed_after': {
                    target['session_id']: {key: waiting[key] for key in CURSOR}}}))
                current = await _event(restored, lambda body: body.get('state') == 'running')
                assert current['revision'] == waiting['revision'] + 1
                assert current['permissions'] == []
                await restored.send(pack_json(OP_SUBSCRIBE, {'sessions': []}))
                await receive_op(restored, OP_HELLO_OK)
                await restored.send(pack_json(OP_SUBSCRIBE, {**subscription, 'managed_after': {
                    target['session_id']: {**{key: waiting[key] for key in CURSOR}, 'runtime_epoch': 'old-runtime'}}}))
                resync = await _event(restored, lambda body: body.get('resync') is True)
                assert resync['revision'] == current['revision'] and 'state' not in resync
                assert (await _rpc(restored, 'managed_status', target))['revision'] == current['revision']
                await restored.send(pack_json(OP_JOURNAL, dict(request_id='closing', action='close_managed',
                    params={**target, 'nonce': 'close'})))
                closed = await _event(restored, lambda body: body.get('state') == 'ended')
                assert closed['session_id'] == target['session_id'] and closed['revision'] > current['revision']

    asyncio.run(run())


def test_replay_cannot_fill_fifo_and_queued_event_rechecks_subscription():
    async def run():
        server = SimpleNamespace(sessions={}, clients={})
        session = SimpleNamespace(id='sid')
        server.sessions[session.id] = session
        client = Client('viewer', asyncio.Queue(32), multiplex=True, managed_events=True,
                        subscriptions={session.id})
        server.clients[client.key] = client
        server.subscribed_session = lambda viewer, sid: server.sessions.get(sid) if sid in viewer.subscriptions else None
        server._send = lambda viewer, frame: viewer.queue.put_nowait(frame)
        access = {'allowed': True}
        events = ManagedEvents(server, asyncio.get_running_loop(), 'host', lambda viewer, target: access['allowed'])
        status = dict(window_id='win', session_id=session.id, journal_id='journal', process_generation='generation',
                      state='ready', active_submission=None, permissions=[])
        try:
            first = events.snapshot_cursor(session, status)
            for revision in range(RETAINED_REVISIONS):
                events.snapshot_cursor(session, {**status, 'state': f'phase-{revision}'})
            events.subscribe(client, {session.id: {**status, **first}})
            async with asyncio.timeout(2):
                while client.queue.qsize() < 16:
                    await asyncio.sleep(.01)
            assert client.queue.qsize() == 16
            op, payload = unpack(client.queue.get_nowait())
            assert op == OP_RUNTIME_EVENT and json.loads(payload)['revision'] == 2
            assert events.delivery_current(client, payload)
            access['allowed'] = False
            assert not events.delivery_current(client, payload)
            access['allowed'] = True
            client.delegated = True
            assert not events.delivery_current(client, payload)
            client.delegated = False
            client.subscriptions.clear()
            events.subscribe(client, None)
            assert not events.delivery_current(client, payload)
            assert client.managed_after == {}
            while not client.queue.empty():
                client.queue.get_nowait()
            client.subscriptions.add(session.id)
            events.subscribe(client, {session.id: {**status, **first, 'revision': 0}})
            op, payload = unpack(await asyncio.wait_for(client.queue.get(), 1))
            assert op == OP_RUNTIME_EVENT
            assert json.loads(payload)['resync'] is True
            large = [{
                'permission_request_id': f'permission-{index}' + 'p' * 240,
                'submission_id': 'submit', 'provider_turn_id': 't' * 512, 'tool_name': 'Tool',
                'state': 'pending', 'choices': ['deny'], 'details': 'private',
            } for index in range(32)]
            committed = events.snapshot_cursor(session, {**status, 'permissions': large})
            op, payload = unpack(await asyncio.wait_for(client.queue.get(), 1))
            assert op == OP_RUNTIME_EVENT
            assert len(payload) < 1024
            assert json.loads(payload)['revision'] == committed['revision']
            assert json.loads(payload)['resync'] is True
        finally:
            events.close()

    asyncio.run(run())
