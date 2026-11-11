"""Host journal RPC is opt-in, bounded and reauthorizes disk-backed replies."""
import asyncio
import json
import os
import threading
from unittest.mock import patch
from functools import wraps

import pytest
from websockets.asyncio.client import connect

from supermonoagent import daemon as module
from supermonoagent.conversation_contract import JournalError, MAX_ENVELOPE
from supermonoagent.hub import Session
from supermonoagent.protocol import (CAPABILITIES, OP_HELLO, OP_HELLO_OK, OP_JOURNAL,
                                     OP_PING, OP_PONG, pack, pack_json, unpack)
from test_client_integration import ControlledPty, isolated_host, receive_op


def run_async(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return wrapped


class Manager:
    """Minimal storage boundary; live auth remains owned by the real daemon."""
    def __init__(self):
        self.calls = []
        self.handler = lambda *_args, **_kwargs: {'available': True}
        self.closed = False

    def owner_scope(self, token, *, default_token=None, accounts=None):
        if token == default_token:
            return 'local-owner'
        for user in accounts.users:
            if user.get('token') == token:
                return 'account-' + user['email']
        raise JournalError('permission_denied')

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.handler(*args, **kwargs)

    def close(self):
        self.closed = True


@pytest.fixture
def host(tmp_path):
    with patch.object(module, 'STATE', tmp_path), patch.dict(os.environ, {'SUPERMONO_STATE_DIR': str(tmp_path)}):
        daemon = module.Daemon('owner-token')
    daemon.hub.persist = False
    original = daemon.journals
    if original is not None:
        original.close()
    manager = Manager()
    daemon.journals = daemon.hub.journals = manager
    yield daemon, manager
    # Async test cases explicitly drain sidecars before their loop closes.
    daemon._sidecar_executor.shutdown(wait=True, cancel_futures=True)


def client_for(daemon, token='owner-token', *, channel='', negotiated=True):
    client = daemon.hub.attach(asyncio.Queue(256), token=token)
    client.conversation_journal = negotiated
    client.channel = channel
    return client


async def request(daemon, client, action='status', params=None, request_id='request-1'):
    frames, close = await daemon._op(client, OP_JOURNAL, json.dumps({
        'request_id': request_id, 'action': action, 'params': params or {}}).encode())
    return (json.loads(unpack(frames[0])[1]) if frames else None), close


@run_async
async def test_rpc_derives_owner_and_exact_room_without_subscribing(host):
    daemon, manager = host
    client = client_for(daemon, channel='room-A')
    result, close = await request(daemon, client, 'list')
    assert close is None and result['ok']
    assert manager.calls == [(('local-owner', 'list', {}), {'rooms': {'room-A'}, 'allow_bind': True})]
    assert not client.subscriptions and not daemon.hub.sessions
    await daemon.close_sidecars()


@run_async
async def test_account_and_delegated_archive_boundaries(host):
    daemon, manager = host
    daemon.accounts.users = [{'email': 'second@example.test', 'token': 'second-token'}]
    other = client_for(daemon, 'second-token')
    reply, _ = await request(daemon, other, 'list')
    assert reply['ok'] and manager.calls[-1][0][0] == 'account-second@example.test'
    reply, _ = await request(daemon, other, 'list_sources', {'session_id': 'missing', 'provider': 'codex'})
    assert reply['error'] == 'permission_denied'
    delegated = client_for(daemon)
    delegated.delegated = True
    with patch.object(daemon, 'client_authorized', return_value=True):
        reply, _ = await request(daemon, delegated, 'list')
    assert reply['error'] == 'permission_denied'
    assert len(manager.calls) == 1
    await daemon.close_sidecars()


@run_async
@pytest.mark.parametrize('raw', [b'{"request_id":"a","request_id":"b","action":"list"}',
    b'{"request_id":"a","action":"list","params":{"limit":NaN}}',
    b'{"request_id":"a","action":"list","params":{"owner_scope":"forged"}}',
    b'{"request_id":"a","action":"bind_source","params":{"path":"C:/private"}}',
    b'{"request_id":"' + b'x' * 65 + b'","action":"list"}',
    b'{"request_id":"a","action":"list","token":"forged"}', b'[]', b'\xff',
    b'x' * (MAX_ENVELOPE + 1), b'{"request_id":"\\ud800","action":"list"}'], ids=['duplicate', 'nan', 'owner', 'path', 'request-id', 'token', 'array', 'utf8', 'oversized', 'surrogate'])
async def test_malformed_and_untrusted_fields_fail_before_storage(host, raw):
    daemon, manager = host
    frames, close = await daemon._op(client_for(daemon), OP_JOURNAL, raw)
    result = json.loads(unpack(frames[0])[1])
    assert close is None and not result['ok'] and result['error'] in {'invalid_request', 'frame_too_large'}
    assert not manager.calls
    await daemon.close_sidecars()


@run_async
async def test_old_client_has_no_rpc_and_storage_failure_has_fixed_error(host):
    daemon, manager = host
    legacy = client_for(daemon, negotiated=False)
    reply, _ = await request(daemon, legacy, 'list')
    assert reply['error'] == 'not_negotiated' and not manager.calls
    modern = client_for(daemon)
    manager.handler = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError('C:/secret owner-token'))
    reply, _ = await request(daemon, modern, 'list')
    assert reply == {'request_id': 'request-1', 'ok': False, 'error': 'unavailable'}
    manager.handler = lambda *_args, **_kwargs: {'huge': 'x' * MAX_ENVELOPE}
    reply, _ = await request(daemon, modern, 'list')
    assert reply['error'] == 'frame_too_large'
    assert 'conversation_journal' not in CAPABILITIES
    await daemon.close_sidecars()


@run_async
@pytest.mark.parametrize('change', ['revoked', 'room', 'owner'])
async def test_authorization_changes_during_io_suppress_stale_reply(host, change):
    daemon, manager = host
    daemon.accounts.users = [{'email': 'one@example.test', 'token': 'account-token'},
                             {'email': 'two@example.test', 'token': 'other-token'}]
    client = client_for(daemon, 'account-token', channel='room-A')
    entered, release = threading.Event(), threading.Event()
    def blocked(*_args, **_kwargs):
        entered.set()
        assert release.wait(3)
        return {'private': 'must-not-escape'}
    manager.handler = blocked
    job = asyncio.create_task(request(daemon, client, 'list'))
    assert await asyncio.to_thread(entered.wait, 3)
    if change == 'revoked':
        daemon.accounts.users = []
    elif change == 'room':
        client.channel = 'room-B'
    else:
        client.token = 'other-token'
    release.set()
    reply, close = await job
    if change == 'revoked':
        assert reply is None and close == 4001
    else:
        assert reply['error'] == 'permission_denied'
    await daemon.close_sidecars()


@run_async
async def test_bind_revalidates_exact_terminal_generation_after_io(host):
    daemon, manager = host
    window = daemon.hub.open_window(owner=daemon.token)
    target = Session('session-one', window.id, 'cmd', '.', ControlledPty())
    target.process_generation = 'generation-one'
    daemon.hub.sessions[target.id] = window.sessions[target.id] = target
    client = client_for(daemon)
    entered, release = threading.Event(), threading.Event()
    def blocked(*_args, **_kwargs):
        entered.set()
        assert release.wait(3)
        return {'binding_id': 'binding-one'}
    manager.handler = blocked
    job = asyncio.create_task(request(daemon, client, 'bind_source', {'session_id': target.id,
        'source_ref': 'opaque-source', 'terminal_generation': target.process_generation}))
    assert await asyncio.to_thread(entered.wait, 3)
    target.process_generation = 'generation-two'
    release.set()
    reply, _ = await job
    assert reply['error'] == 'permission_denied'
    await daemon.close_sidecars()


@run_async
async def test_shutdown_waits_for_cancelled_viewers_disk_call(host):
    daemon, manager = host
    entered, release = threading.Event(), threading.Event()
    def blocked(*_args, **_kwargs):
        entered.set()
        assert release.wait(3)
        assert not manager.closed
        return {}
    manager.handler = blocked
    job = asyncio.create_task(request(daemon, client_for(daemon), 'list'))
    assert await asyncio.to_thread(entered.wait, 3)
    job.cancel()
    await asyncio.gather(job, return_exceptions=True)
    closing = asyncio.create_task(daemon.close_sidecars())
    await asyncio.sleep(0)
    assert not closing.done() and not manager.closed
    release.set()
    await closing
    assert manager.closed


@run_async
@pytest.mark.parametrize('relayed', [False, True])
async def test_direct_and_relay_negotiation_and_sidecar_ping_isolation(relayed):
    async with isolated_host() as (daemon, direct_url, relay_url):
        original = daemon.journals
        manager = Manager()
        daemon.journals = daemon.hub.journals = manager
        if original is not None:
            await original.aclose()
        entered, release = threading.Event(), threading.Event()
        def blocked(*_args, **_kwargs):
            entered.set()
            assert release.wait(3)
            return {'journals': [], 'next_cursor': None}
        manager.handler = blocked
        headers = {} if relayed else {'Authorization': 'Bearer integration-owner'}
        async with connect(relay_url if relayed else direct_url, additional_headers=headers) as ws:
            if relayed:
                await ws.send(json.dumps({'op': 'phone', 'code': 'local-test', 'token': 'integration-owner'}))
                assert json.loads(await ws.recv())['op'] == 'ready'
            await ws.send(pack_json(OP_HELLO, {'token': 'integration-owner', 'multiplex': True}))
            hello = json.loads(await receive_op(ws, OP_HELLO_OK))
            assert 'conversation_journal' not in hello['capabilities']
            await ws.send(pack_json(OP_HELLO, {'token': 'integration-owner', 'multiplex': True,
                                               'conversation_journal': 1}))
            modern = json.loads(await receive_op(ws, OP_HELLO_OK))
            assert modern['capabilities']['conversation_journal'] == 1
            await ws.send(pack_json(OP_JOURNAL, {'request_id': 'wire', 'action': 'list', 'params': {}}))
            assert await asyncio.to_thread(entered.wait, 3)
            await ws.send(pack(OP_PING, b'journal-sidecar'))
            assert await receive_op(ws, OP_PONG) == b'journal-sidecar'
            release.set()
            response = json.loads(await receive_op(ws, OP_JOURNAL))
            assert response == {'request_id': 'wire', 'ok': True, 'result': {'journals': [], 'next_cursor': None}}


@run_async
async def test_sealed_archives_enforce_owner_and_room_without_live_session(host, tmp_path):
    from supermonoagent.conversation_manager import ConversationManager
    daemon, fake = host
    daemon.accounts.users = [{'email': 'account@example.test', 'token': 'account-token'}]
    manager = ConversationManager(tmp_path / 'archive-store', default_token=daemon.token, accounts=daemon.accounts)
    daemon.journals = daemon.hub.journals = manager
    scope = manager.owner_scope(daemon.token)
    manifest = manager.store.create(scope, room='room-A', window_id='old-window', session_id='old-session',
        source_epoch='old-epoch', process_generation='old-generation')
    jid = manifest['journal_id']
    event = manager.store.append_raw(jid, b'private archived output')
    manager.store.terminal_end(jid)
    manager.store.seal(jid)
    assert not daemon.hub.sessions
    owner = client_for(daemon, channel='room-A')
    reply, _ = await request(daemon, owner, 'status', {'journal_id': jid})
    assert reply['ok'] and reply['result']['terminal_state'] == 'ended'
    reply, _ = await request(daemon, owner, 'read_content', {'journal_id': jid, 'content_id': event['content_id']})
    assert reply['ok']
    for client in (client_for(daemon, channel='room-B'), client_for(daemon, 'account-token', channel='room-A')):
        reply, _ = await request(daemon, client, 'list')
        assert reply['ok'] and reply['result']['journals'] == []
        for action, params in [('status', {'journal_id': jid}),
                               ('read_page', {'journal_id': jid}),
                               ('read_content', {'journal_id': jid, 'content_id': event['content_id']})]:
            reply, _ = await request(daemon, client, action, params)
            assert not reply['ok'] and 'private archived output' not in str(reply)
    await daemon.close_sidecars()


@run_async
async def test_optional_storage_initialization_failure_leaves_terminal_protocol_available(tmp_path):
    import supermonoagent.conversation_manager as managers
    with patch.object(module, 'STATE', tmp_path), patch.dict(os.environ, {'SUPERMONO_STATE_DIR': str(tmp_path)}), \
            patch.object(managers, 'ConversationManager', side_effect=OSError('private path and token')):
        daemon = module.Daemon('owner-token')
        daemon._start_journals()
    assert daemon.journals is None and daemon.hub.journals is None
    client = client_for(daemon)
    frames, close = await daemon._op(client, OP_PING, b'still-alive')
    assert close is None and unpack(frames[0]) == (OP_PONG, b'still-alive')
    reply, _ = await request(daemon, client, 'list')
    assert reply['error'] == 'unavailable'
    await daemon.close_sidecars()


@run_async
async def test_sidecar_queue_is_bounded_with_journal_busy_envelope(host):
    daemon, manager = host
    client = client_for(daemon)
    entered, release = threading.Event(), threading.Event()
    def blocked(*_args, **_kwargs):
        entered.set()
        assert release.wait(3)
        return {}
    manager.handler = blocked
    async def close(_code):
        raise AssertionError('Owner must remain connected')
    for index in range(6):
        daemon.queue_sidecar_request(client, OP_JOURNAL, json.dumps({
            'request_id': f'request-{index}', 'action': 'list', 'params': {}}).encode(), close)
    assert daemon._sidecar_workers[id(client)][0].maxsize == 4
    busy = []
    while not client.queue.empty():
        op, payload = unpack(client.queue.get_nowait())
        if op == OP_JOURNAL:
            busy.append(json.loads(payload))
    assert busy and all(item['ok'] is False and item['error'] == 'busy' for item in busy)
    assert await asyncio.to_thread(entered.wait, 3)
    release.set()
    await daemon.close_sidecars()
