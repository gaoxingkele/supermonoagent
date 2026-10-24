"""Assistant reads are owner/session bound and cannot follow client-supplied paths."""
import asyncio
import json
import threading
from unittest.mock import patch

from supermonoagent.protocol import OP_ASSISTANT_CONTEXT, unpack, unpack_json
from test_client_integration import isolated_host


def test_assistant_context_uses_session_startup_directory_and_denies_other_account(tmp_path):
    asyncio.run(check_context(tmp_path))


async def check_context(tmp_path):
    async with isolated_host() as (host, *_):
        folder = tmp_path / 'project'
        (folder / '.metarsi/wiki').mkdir(parents=True)
        (folder / '.metarsi/constitution.json').write_text('{}')
        (folder / '.metarsi/wiki/README.md').write_text('fixture evidence ledger', encoding='utf-8')
        win = host.hub.open_window('Assistant fixture', owner=host.token)
        session = host.hub.open_cli(win.id, 'cmd', str(folder))
        client = host.hub.attach(asyncio.Queue(), token=host.token)
        frames, code = await host._op(client, OP_ASSISTANT_CONTEXT, json.dumps({'request': 'selected', 'session': session.id, 'project': 'ignored-client-path'}).encode())
        body = unpack_json(unpack(frames[0])[1])
        assert code is None and body['ok']
        assert body['project'] == str(folder.resolve())
        assert body['ledger']['pages']['README.md']['text'] == 'fixture evidence ledger'
        assert not session.pty.written
        other_token = host.accounts.register('assistant-viewer@example.test', 'fixture-password')
        outsider = host.hub.attach(asyncio.Queue(), token=other_token)
        with patch('supermonoagent.assistant_context.read_project_context') as read:
            frames, _ = await host._op(outsider, OP_ASSISTANT_CONTEXT, json.dumps({'session': session.id}).encode())
            assert not unpack_json(unpack(frames[0])[1])['ok']
            read.assert_not_called()
        host.hub.detach(client.key)
        host.hub.detach(outsider.key)


def test_closed_target_cannot_receive_slow_context_result():
    asyncio.run(check_closed_target())


async def check_closed_target():
    async with isolated_host() as (host, *_):
        win = host.hub.open_window('Slow fixture', owner=host.token)
        session = host.hub.open_cli(win.id, 'cmd')
        client = host.hub.attach(asyncio.Queue(), token=host.token)
        entered, release = threading.Event(), threading.Event()
        def slow_read(*_):
            entered.set()
            assert release.wait(3)
            return {'project': 'must-not-arrive'}
        with patch('supermonoagent.assistant_context.read_project_context', slow_read):
            pending = asyncio.create_task(host._op(client, OP_ASSISTANT_CONTEXT, json.dumps({'session': session.id}).encode()))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                host.hub.close_session(session.id)
            finally:
                release.set()
            frames, code = await pending
            assert code is None and not unpack_json(unpack(frames[0])[1])['ok']
        host.hub.detach(client.key)
