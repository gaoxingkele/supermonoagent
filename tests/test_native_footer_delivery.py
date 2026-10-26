"""Session-owned read-only footer recovery on direct and native relay outputs."""
import asyncio
import json
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from websockets.asyncio.client import connect

from supermonoagent.protocol import (MAX_FRAME, OP_HELLO, OP_HELLO_OK, OP_OUTPUT,
                                    OP_SNAPSHOT, OP_SUBSCRIBE, pack, pack_feed, pack_json, unpack)
from supermonoagent.pty import WinPty
from test_client_integration import isolated_host, receive_op


FOOTER = b'\x1b7\x1b[27;1Hcontext 52% weekly 81%\x1b8'


@pytest.mark.parametrize('relay', [False, True])
def test_native_direct_and_relay_full_snapshot_then_footer_before_live(relay):
    async def check():
        async with isolated_host() as (daemon, direct, uplink):
            window = daemon.hub.open_window(owner=daemon.token)
            session = daemon.hub.open_cli(window.id, 'cmd')
            session.pump.remember(b'x' * MAX_FRAME)
            session.pty.footer_patch = Mock(return_value=FOOTER)
            session.pty.resize = Mock(wraps=session.pty.resize)
            expected = pack_feed(OP_SNAPSHOT, session.id, session.pump.snapshot())
            assert len(expected) == MAX_FRAME + 4
            headers = {} if relay else {'Authorization': 'Bearer ' + daemon.token}
            async with connect(uplink if relay else direct, additional_headers=headers,
                               compression=None, max_size=1024 * 1024) as socket:
                if relay:
                    await socket.send(json.dumps({'op': 'phone', 'code': 'local-test',
                                                   'token': daemon.token, 'reliable': True}))
                    assert json.loads(await socket.recv())['op'] == 'ready'
                await socket.send(pack_json(OP_HELLO, {'token': daemon.token, 'multiplex': True, 'reliable': True}))
                await receive_op(socket, OP_HELLO_OK)
                await socket.send(pack_json(OP_SUBSCRIBE, {'sessions': [session.id]}))
                async with asyncio.timeout(3):
                    while True:
                        frame = await socket.recv()
                        if isinstance(frame, bytes) and unpack(frame)[0] == OP_SNAPSHOT:
                            break
                    assert frame == expected
                    # Control/catalog updates remain responsive during optional recovery.
                    assert await receive_op(socket, OP_OUTPUT) == session.id.encode() + b'\0' + FOOTER
                # Queue fresh output after replay; the recovery path never
                # replaces, truncates or injects anything into the PTY input.
                session.pty.inbox.put(b'fresh native output')
                assert await receive_op(socket, OP_OUTPUT) == session.id.encode() + b'\0fresh native output'
                session.pty.footer_patch.assert_called_once_with(session.cols, session.rows)
                assert session.pty.written == b''
                session.pty.resize.assert_not_called()
    asyncio.run(check())


def test_unsubscribed_foreign_unknown_and_non_snapshot_never_read_console():
    async def check():
        async with isolated_host() as (daemon, _, _):
            window = daemon.hub.open_window(owner=daemon.token)
            session = daemon.hub.open_cli(window.id, 'cmd')
            foreign = daemon.hub.open_window(owner='another-owner')
            other = daemon.hub.open_cli(foreign.id, 'cmd')
            session.pty.footer_patch = Mock(return_value=FOOTER)
            other.pty.footer_patch = Mock(return_value=FOOTER)
            client = daemon.hub.attach(asyncio.Queue(), token=daemon.token)
            client.multiplex = True
            client.subscriptions = {other.id}
            for frame in (pack_feed(OP_SNAPSHOT, session.id, b'raw'),
                          pack_feed(OP_SNAPSHOT, other.id, b'raw'),
                          pack_feed(OP_SNAPSHOT, 'unknown', b'raw'),
                          pack_feed(OP_OUTPUT, session.id, b'raw')):
                assert await daemon.snapshot_supplement(client, frame) is None
            session.pty.footer_patch.assert_not_called()
            other.pty.footer_patch.assert_not_called()
    asyncio.run(check())


@pytest.mark.parametrize('race', ['dimensions', 'revoked', 'unsubscribed', 'removed', 'pty'])
def test_recovery_revalidates_session_auth_and_dimensions_after_hidden_read(race):
    async def check():
        async with isolated_host() as (daemon, _, _):
            window = daemon.hub.open_window(owner=daemon.token)
            session = daemon.hub.open_cli(window.id, 'cmd')
            client = daemon.hub.attach(asyncio.Queue(), token=daemon.token)
            client.multiplex = True
            client.subscriptions = {session.id}
            started, finish = threading.Event(), threading.Event()
            def read(*_):
                started.set()
                assert finish.wait(3)
                return FOOTER
            original_pty = session.pty
            original_pty.footer_patch = read
            task = asyncio.create_task(daemon.snapshot_supplement(client, pack_feed(OP_SNAPSHOT, session.id, b'raw')))
            try:
                assert await asyncio.to_thread(started.wait, 3)
                if race == 'dimensions':
                    session.cols += 1
                elif race == 'revoked':
                    client.token = ''
                elif race == 'unsubscribed':
                    client.subscriptions.clear()
                elif race == 'removed':
                    daemon.hub.sessions.pop(session.id)
                else:
                    session.pty = object()
                finish.set()
                assert await task is None
                assert original_pty.written == b''
            finally:
                finish.set()
                session.pty = original_pty
                daemon.hub.sessions[session.id] = session
    asyncio.run(check())


def test_plain_direct_pane_footer_is_not_session_prefixed():
    async def check():
        async with isolated_host() as (daemon, _, _):
            window = daemon.hub.open_window(owner=daemon.token)
            session = daemon.hub.open_cli(window.id, 'cmd')
            client = daemon.hub.attach(asyncio.Queue(), token=daemon.token)
            daemon.hub.select(client, window.id)
            session.pty.footer_patch = Mock(return_value=FOOTER)
            assert await daemon.snapshot_supplement(client, pack(OP_SNAPSHOT, b'raw')) == pack(OP_OUTPUT, FOOTER)
    asyncio.run(check())


def test_spawn_generation_actual_parent_and_per_pty_cache_are_bound_without_io():
    process = Mock(pid=123)
    fake = SimpleNamespace(PtyProcess=SimpleNamespace(spawn=Mock(return_value=process)))
    identity = (987, 456789)
    with patch.dict(sys.modules, {'winpty': fake}), \
            patch('supermonoagent.console_snapshot._process_identity', return_value=identity) as generation, \
            patch('supermonoagent.console_snapshot.read_console', return_value={'safe': 'fixture'}) as read, \
            patch('supermonoagent.console_snapshot.footer_patch', return_value=FOOTER), \
            patch('supermonoagent.pty.time.monotonic', side_effect=[10, 10.2, 11.1]):
        pty = WinPty(['fixture'], '.', cols=100, rows=28)
        assert pty.footer_patch(100, 28) == FOOTER
        assert pty.footer_patch(100, 28) == FOOTER
        read.assert_called_once_with(123, expected_created=456789, expected_parent=987)
        assert pty.footer_patch(100, 28) == FOOTER
        assert read.call_count == 2
        generation.return_value = (987, 456790)
        assert pty.footer_patch(100, 28) == b''
        assert pty._footer_cache is None
        process.write.assert_not_called()
        process.setwinsize.assert_not_called()
