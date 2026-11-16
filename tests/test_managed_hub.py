import asyncio

from supermonoagent.hub import Hub, Session
from supermonoagent.conversation_manager import ConversationManager
from supermonoagent.conversation_contract import JournalError
import pytest
from test_windows import FakePty


def test_managed_raw_input_is_rejected_by_both_hub_paths():
    async def run():
        hub = Hub(pty_factory=lambda *_: FakePty())
        hub.attach_loop(asyncio.get_running_loop())
        window = hub.open_window()
        pty = FakePty()
        target = Session('managed-session', window.id, 'claude', '.', pty,
                         interaction_mode='managed', provider='claude')
        hub.sessions[target.id] = window.sessions[target.id] = target
        window.focused = target.id
        client = hub.attach(asyncio.Queue())
        client.multiplex = True
        client.window_id, client.session_id = window.id, target.id
        client.subscriptions.add(target.id)
        assert not hub.write_session(client, target.id, b'raw input')
        hub.write(client, b'raw input')
        assert not pty.written
        assert hub.resize_session(client, target.id, 100, 30, take_control=True)
        assert not pty.written
        visible = hub.summary()['windows'][0]['sessions'][0]
        assert visible['interaction_mode'] == 'managed'
        assert visible['provider'] == 'claude'
        hub.close_window(window.id)
    asyncio.run(run())


def test_managed_journal_cannot_be_bound_to_another_passive_provider(tmp_path):
    manager = ConversationManager(tmp_path, default_token='owner')
    owner = manager.owner_scope('owner')
    target = Session('managed', 'managed-window', 'claude', '.', FakePty(),
                     interaction_mode='managed', provider='claude')
    manifest = manager.store.create(owner, room='room', window_id=target.window_id,
        session_id=target.id, source_epoch=manager.source_epoch, process_generation=target.process_generation)
    manager.register_managed_session(target, owner_scope=owner, room='room', journal_id=manifest['journal_id'])
    calls = []
    manager.sources.list_sources = lambda *args, **kwargs: calls.append('list') or []
    manager.sources.bind = lambda *args, **kwargs: calls.append('bind') or {'bound': True}
    try:
        for action, params in [('list_sources', {'session_id': target.id, 'provider': 'claude'}),
                               ('bind_source', {'session_id': target.id, 'source_ref': 'foreign',
                                                'terminal_generation': target.process_generation})]:
            with pytest.raises(JournalError, match='managed_source_owned'):
                manager.request(owner, action, params, allow_bind=True)
        assert not calls
        native = Session('native', 'native-window', 'codex', '.', FakePty())
        manager.open_session(native, owner_scope=owner, room='room')
        assert manager.request(owner, 'list_sources', {'session_id': native.id, 'provider': 'codex'},
                               allow_bind=True) == {'sources': []}
        assert calls == ['list']
    finally:
        manager.close()


def test_ended_managed_session_removes_its_empty_independent_window():
    async def run():
        hub = Hub(pty_factory=lambda *_: FakePty())
        hub.attach_loop(asyncio.get_running_loop())
        native_window = hub.open_window()
        native = hub.open_cli(native_window.id, 'cmd')
        managed_window = hub.open_window()
        target = Session('managed-ended', managed_window.id, 'claude', '.', FakePty(),
                         interaction_mode='managed', provider='claude')
        hub.sessions[target.id] = managed_window.sessions[target.id] = target
        hub.close_session(target.id)
        assert managed_window.id not in hub.windows
        assert hub.sessions[native.id] is native
        hub.close_session(native.id)
        assert native_window.id in hub.windows
        hub.close_window(native_window.id)
    asyncio.run(run())
