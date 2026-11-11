import asyncio
import base64
import threading
import time
from types import SimpleNamespace

import pytest

from supermonoagent.conversation_contract import JournalError
from supermonoagent.conversation_manager import ConversationManager


def session(sid='session-one'):
    return SimpleNamespace(id=sid, window_id='window-one', shell='codex', cwd='C:/project',
                           process_generation='process-one')


def test_owner_rotation_and_closed_archive_acl(tmp_path):
    accounts = SimpleNamespace(users=[{'email': 'one@example.test', 'token': 'old'}])
    manager = ConversationManager(tmp_path, default_token='root', accounts=accounts)
    owner = manager.owner_scope('old')
    accounts.users[0]['token'] = 'new'
    assert manager.owner_scope('new') == owner
    with pytest.raises(JournalError):
        manager.owner_scope('old')
    sid = session()
    jid = manager.open_session(sid, owner_scope=owner, room='room-one', title='title')
    manager.record_output(sid, b'\x1b[31mexact\x00bytes\xff')
    assert manager.capture.flush()
    manager.store.terminal_end(jid)
    manager.store.seal(jid)
    assert manager.request(owner, 'status', {'journal_id': jid}, rooms=['room-one'])['journal_id'] == jid
    for scope, rooms in [('other', ['room-one']), (owner, ['room-two'])]:
        with pytest.raises(JournalError):
            manager.request(scope, 'read_page', {'journal_id': jid}, rooms=rooms)
    page = manager.request(owner, 'read_page', {'journal_id': jid})
    raw = next(row for row in page['events'] if row['kind'] == 'raw_output')
    body = manager.request(owner, 'read_content', {'journal_id': jid, 'content_id': raw['content_id']})
    assert base64.b64decode(body['data_base64']) == b'\x1b[31mexact\x00bytes\xff'
    manager.close()


def test_storage_shutdown_suspends_live_cli_and_keeps_identity(tmp_path):
    manager = ConversationManager(tmp_path, default_token='root')
    scope = manager.owner_scope('root')
    jid = manager.open_session(session(), owner_scope=scope, room='room-one')
    host = manager.store.host_installation_id
    manager.close()
    reopened = ConversationManager(tmp_path, default_token='changed')
    assert reopened.store.host_installation_id == host
    assert reopened.owner_scope('changed') == scope
    manifest = reopened.request(scope, 'status', {'journal_id': jid})
    assert manifest['terminal_state'] != 'ended'
    assert manifest['capture_state'] == 'suspended'
    assert not manifest['sealed_at']
    reopened.close()


def test_no_binding_or_private_params_without_native_owner(tmp_path):
    manager = ConversationManager(tmp_path, default_token='root')
    scope = manager.owner_scope('root')
    manager.open_session(session(), owner_scope=scope, room='room-one')
    for action, params in [('list_sources', {'session_id': 'session-one', 'provider': 'codex'}),
                           ('status', {'session_id': 'session-one', 'owner_scope': 'other'}),
                           ('read_content', {'journal_id': '../escape', 'content_id': 'x'})]:
        with pytest.raises(JournalError):
            manager.request(scope, action, params)
    assert manager.request(scope, 'status', {'session_id': 'session-one'})['source']['bound'] is False
    manager.close()


def test_reader_eof_flushes_and_seals_without_viewer(tmp_path):
    async def run():
        manager = ConversationManager(tmp_path, default_token='root', drain_seconds=0)
        manager.start_background(asyncio.get_running_loop())
        scope = manager.owner_scope('root')
        target = session()
        jid = manager.open_session(target, owner_scope=scope, room='room-one')
        manager.record_output(target, b'last chunk')
        manager.end_session(target, outcome='exited')
        for _ in range(200):
            await asyncio.sleep(.01)
            if manager.store.status(scope, jid)['capture_state'] == 'sealed':
                break
        manifest = manager.store.status(scope, jid)
        assert manifest['terminal_state'] == 'ended'
        assert manifest['capture_state'] == 'sealed'
        assert manager.request(scope, 'status', {'session_id': target.id})['journal_id'] == jid
        await manager.aclose()
    asyncio.run(run())


def test_source_catalog_io_does_not_block_reader_or_return_after_end(tmp_path):
    manager = ConversationManager(tmp_path, default_token='root')
    scope, target = manager.owner_scope('root'), session()
    manager.open_session(target, owner_scope=scope, room='room-one')
    entered, release = threading.Event(), threading.Event()
    def slow(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        return []
    manager.sources.list_sources = slow
    errors = []
    def catalog():
        try:
            manager.request(scope, 'list_sources', {'session_id': target.id, 'provider': 'codex'}, allow_bind=True)
        except JournalError as error:
            errors.append(error.code)
    worker = threading.Thread(target=catalog)
    worker.start()
    assert entered.wait(2)
    before = time.monotonic()
    assert manager.record_output(target, b'live output')
    elapsed = time.monotonic() - before
    with manager._lock:
        manager._sessions[target.id]['active'] = False
    release.set()
    worker.join(5)
    assert elapsed < .05
    assert errors == ['terminal_ended']
    manager.close()


def test_pending_outcome_settles_before_seal(tmp_path):
    async def run():
        manager = ConversationManager(tmp_path, default_token='root', drain_seconds=.01)
        target, scope = session(), manager.owner_scope('root')
        jid = manager.open_session(target, owner_scope=scope, room='room-one')
        pending = [1]
        calls = [0]
        manager.sources.status = lambda _jid: dict(binding_id='binding', pending_turns=pending[0], reason=None)
        async def reconcile(_binding):
            calls[0] += 1
            if calls[0] > 1:
                pending[0] = 0
        manager.sources.reconcile = reconcile
        manager.sources.terminal_end = lambda _jid: None
        await manager._finish(jid, 'exited')
        state = manager.store.status(scope, jid)
        assert state['capture_state'] == 'sealed'
        assert state['terminal_state'] == 'ended'
        assert not manager._sync_errors
        await manager.aclose()
    asyncio.run(run())


def test_immediate_shutdown_keeps_accepted_reader_eof_and_releases_writer(tmp_path):
    async def run():
        manager = ConversationManager(tmp_path, default_token='root', drain_seconds=0)
        manager.start_background(asyncio.get_running_loop())
        scope, target = manager.owner_scope('root'), session()
        jid = manager.open_session(target, owner_scope=scope, room='room-one')
        manager.record_output(target, b'final bytes before shutdown')
        manager.end_session(target)
        await manager.aclose()
        reopened = ConversationManager(tmp_path, default_token='root')
        manifest = reopened.store.status(scope, jid)
        assert manifest['terminal_state'] == 'ended'
        assert manifest['capture_state'] in {'sealed', 'incomplete'}
        events = reopened.store.read_page(scope, jid)['events']
        assert b''.join(reopened.store.content_bytes(jid, event['content_id']) for event in events
                        if event['kind'] == 'raw_output') == b'final bytes before shutdown'
        await reopened.aclose()
    asyncio.run(run())


def test_shutdown_joins_registry_disk_work_before_closing_storage(tmp_path):
    async def run():
        manager = ConversationManager(tmp_path, default_token='root', drain_seconds=0)
        manager.start_background(asyncio.get_running_loop())
        target = session()
        manager.open_session(target, owner_scope=manager.owner_scope('root'), room='room-one')
        entered, release = threading.Event(), threading.Event()
        original = manager.sources.terminal_end
        active = [0]
        def blocked(jid):
            active[0] += 1
            entered.set()
            try:
                assert release.wait(5)
                return original(jid)
            finally:
                active[0] -= 1
        manager.sources.terminal_end = blocked
        manager.end_session(target)
        assert await asyncio.to_thread(entered.wait, 2)
        closing = asyncio.create_task(manager.aclose())
        await asyncio.sleep(.05)
        try:
            assert not closing.done()
            assert active[0] == 1
        finally:
            release.set()
            await closing
    asyncio.run(run())
