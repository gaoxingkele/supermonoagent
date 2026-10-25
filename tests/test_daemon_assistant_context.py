"""Assistant reads are owner/session bound and cannot follow client-supplied paths."""
import asyncio
import json
import threading
from unittest.mock import patch

from supermonoagent.protocol import OP_ASSISTANT_CONTEXT, pack_json, unpack, unpack_json
from test_client_integration import isolated_host, receive_op, subscribed


def test_assistant_context_uses_session_startup_directory_and_denies_other_account(tmp_path):
    asyncio.run(check_context(tmp_path))


async def check_context(tmp_path):
    async with isolated_host() as (host, *_):
        folder = tmp_path / 'project'
        (folder / '.metarsi/wiki').mkdir(parents=True)
        (folder / '.metarsi/constitution.json').write_text('{}')
        (folder / '.metarsi/wiki/README.md').write_text('fixture evidence ledger', encoding='utf-8')
        (folder / '.metarsi/wiki/nodes.md').write_text('S0001: reserved validation\nG0001: failure signal', encoding='utf-8', newline='\n')
        (folder / '.metarsi/wiki/ideas.md').write_text('I0001: falsifier required', encoding='utf-8')
        win = host.hub.open_window('Assistant fixture', owner=host.token)
        session = host.hub.open_cli(win.id, 'cmd', str(folder))
        client = host.hub.attach(asyncio.Queue(), token=host.token)
        frames, code = await host._op(client, OP_ASSISTANT_CONTEXT, json.dumps({'request': 'selected', 'session': session.id, 'project': 'ignored-client-path'}).encode())
        body = unpack_json(unpack(frames[0])[1])
        assert code is None and body['ok']
        assert body['project'] == str(folder.resolve())
        assert body['ledger']['pages']['README.md']['text'] == 'fixture evidence ledger'
        assert body['ledger']['pages']['nodes.md']['text'] == 'S0001: reserved validation\nG0001: failure signal'
        assert body['ledger']['pages']['ideas.md']['text'] == 'I0001: falsifier required'
        assert not session.pty.written
        other_token = host.accounts.register('assistant-viewer@example.test', 'fixture-password')
        outsider = host.hub.attach(asyncio.Queue(), token=other_token)
        with patch('supermonoagent.assistant_context.read_project_context') as read:
            frames, _ = await host._op(outsider, OP_ASSISTANT_CONTEXT, json.dumps({'session': session.id}).encode())
            assert not unpack_json(unpack(frames[0])[1])['ok']
            read.assert_not_called()
        host.hub.detach(client.key)
        host.hub.detach(outsider.key)


def test_real_context_rpc_returns_fixed_evolution_pages_without_touching_ledger(tmp_path):
    asyncio.run(check_evolution_pages(tmp_path))


async def check_evolution_pages(tmp_path):
    async with isolated_host() as (host, direct, _relay):
        project = tmp_path / 'wire-project'
        wiki = project / '.metarsi' / 'wiki'
        wiki.mkdir(parents=True)
        (project / '.metarsi' / 'constitution.json').write_text('{}', encoding='utf-8')
        (project / '.metarsi' / 'metarsi.db').write_bytes(b'ledger sentinel, not a real database')
        (wiki / 'README.md').write_text('existing ready ledger', encoding='utf-8')
        (wiki / 'nodes.md').write_text('S0012: reserved validation\n' + 'x' * 9000, encoding='utf-8', newline='\n')
        # ideas.md is deliberately absent; old ledgers remain usable.
        (wiki / 'secrets.md').write_text('never return arbitrary pages', encoding='utf-8')
        def snapshot():
            return {path.relative_to(project): (path.read_bytes(), path.stat().st_size,
                    path.stat().st_mtime_ns, path.stat().st_ctime_ns)
                    for path in project.rglob('*') if path.is_file()}
        before = snapshot()
        window = host.hub.open_window('Bounded evolution evidence fixture', owner=host.token)
        session = host.hub.open_cli(window.id, 'cmd', str(project))
        async with subscribed(direct, [session.id]) as socket:
            await socket.send(pack_json(OP_ASSISTANT_CONTEXT, {'request': 'fixed-evidence', 'session': session.id,
                'project': str(tmp_path / 'untrusted-path'), 'pages': ['secrets.md', '../secrets.md']}))
            response = unpack_json(await receive_op(socket, OP_ASSISTANT_CONTEXT))
        assert response['ok'] and response['request'] == 'fixed-evidence'
        assert response['project'] == str(project.resolve())
        pages = response['ledger']['pages']
        assert pages['README.md']['text'] == 'existing ready ledger'
        assert pages['nodes.md']['text'] == ('S0012: reserved validation\n' + 'x' * 9000)[:8192]
        assert pages['nodes.md']['truncated'] is True
        assert pages['ideas.md'] == {'status': 'missing', 'text': '', 'truncated': False}
        assert response['ledger']['status'] == 'ready'
        assert 'never return arbitrary pages' not in str(response)
        assert snapshot() == before
        assert not session.pty.written


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
