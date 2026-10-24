"""Slow file durability must not stall a terminal sharing the same WebSocket.

All consoles, files and sockets belong to isolated_host; no real shell is used.
"""
import asyncio
import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from websockets.exceptions import ConnectionClosed

from supermonoagent.file_transfer import TransferStore
from supermonoagent.protocol import (
    OP_FILE_TRANSFER, OP_INPUT_SESSION, OP_PING, OP_PONG, pack, pack_json, unpack_json,
)
from test_client_integration import isolated_host, receive_op, subscribed


async def start_upload(ws, session):
    async def request(body):
        await ws.send(pack_json(OP_FILE_TRANSFER, {
            'request': 'responsive-upload', 'session': session.id, **body,
        }))
        response = unpack_json(await receive_op(ws, OP_FILE_TRANSFER))
        assert response['ok'], response
        return response

    first = await request({'action': 'begin', 'name': 'responsive.pdf', 'size': 1,
                           'sha256': hashlib.sha256(b'x').hexdigest()})
    await request({'action': 'chunk', 'id': first['id'], 'offset': 0,
                   'data': base64.b64encode(b'x').decode()})
    return first['id']


def blocked_fsync(entered, release):
    def sync(_fd):
        entered.set()
        if not release.wait(5):
            raise TimeoutError('Fixture fsync gate was not released')
    return sync


async def wait_for_thread_gate(entered):
    # Never spend a default-executor worker waiting for a filesystem worker.
    async with asyncio.timeout(2):
        while not entered.is_set():
            await asyncio.sleep(.005)


@pytest.mark.parametrize('relay', [False, True], ids=['direct', 'relay-phone'])
def test_input_and_ping_on_same_socket_remain_responsive_during_file_fsync(relay):
    asyncio.run(check_responsiveness(relay))


async def check_responsiveness(relay):
    # A blocked upload must leave even a sole PTY worker available for input.
    # asyncio.run shuts down this executor after the isolated fixture exits.
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
    async with isolated_host() as (host, direct, remote):
        window = host.hub.open_window('Slow disk fixture', owner=host.token)
        session = host.hub.open_cli(window.id, 'cmd')
        entered, release = threading.Event(), threading.Event()
        async with subscribed(remote if relay else direct, [session.id], relay=relay) as ws:
            upload = await start_upload(ws, session)
            with patch('supermonoagent.file_transfer.os.fsync', blocked_fsync(entered, release)):
                try:
                    await ws.send(pack_json(OP_FILE_TRANSFER, {
                        'request': 'finish-gated', 'session': session.id,
                        'action': 'finish', 'id': upload,
                    }))
                    await wait_for_thread_gate(entered)
                    # Both messages use the SAME direct socket or relay phone.
                    await ws.send(pack(OP_INPUT_SESSION, session.id.encode() + b'\0typing-remains-responsive'))
                    await ws.send(pack(OP_PING, b'while-fsync-is-blocked'))
                    async with asyncio.timeout(.75):
                        assert await receive_op(ws, OP_PONG) == b'while-fsync-is-blocked'
                    assert bytes(session.pty.written) == b'typing-remains-responsive'
                    assert not release.is_set()
                    assert not (host.receive_dir / 'responsive.pdf').exists()
                finally:
                    release.set()
                finished = unpack_json(await receive_op(ws, OP_FILE_TRANSFER))
                assert finished['request'] == 'finish-gated' and finished['ok']
            assert (host.receive_dir / 'responsive.pdf').read_bytes() == b'x'


@pytest.mark.parametrize('relay', [False, True], ids=['direct', 'relay-phone'])
@pytest.mark.parametrize('boundary', ['session-close', 'authorization-revoked'])
def test_closed_or_revoked_target_cannot_publish_after_blocked_fsync(relay, boundary):
    asyncio.run(check_revocation(relay, boundary))


async def check_revocation(relay, boundary):
    async with isolated_host() as (host, direct, remote):
        window = host.hub.open_window('Revocation fixture', owner=host.token)
        session = host.hub.open_cli(window.id, 'cmd')
        entered, release = threading.Event(), threading.Event()
        async with subscribed(remote if relay else direct, [session.id], relay=relay) as ws:
            upload = await start_upload(ws, session)
            client = next(client for client in host.hub.clients.values()
                          if client.token == host.token and client.multiplex)
            with patch('supermonoagent.file_transfer.os.fsync', blocked_fsync(entered, release)):
                try:
                    await ws.send(pack_json(OP_FILE_TRANSFER, {
                        'request': 'revoked-finish', 'session': session.id,
                        'action': 'finish', 'id': upload,
                    }))
                    await wait_for_thread_gate(entered)
                    if boundary == 'session-close':
                        host.hub.close_session(session.id)
                    else:
                        host.hub.detach(client.key)
                finally:
                    release.set()
                try:
                    result = unpack_json(await receive_op(ws, OP_FILE_TRANSFER, timeout=2))
                    assert not result['ok']
                except ConnectionClosed:
                    pass
            async with asyncio.timeout(3):
                while list((host.receive_dir / '.supermono-partials').glob('*.part')):
                    await asyncio.sleep(.01)
            assert not (host.receive_dir / 'responsive.pdf').exists()
            assert not session.pty.written


def test_published_file_stays_successful_when_only_partial_unlink_fails(tmp_path):
    store = TransferStore(tmp_path, max_active=1)
    transfer = store.begin('owner', 'session', 'published.pdf', 1, hashlib.sha256(b'x').hexdigest())
    store.append('owner', transfer, 0, b'x')
    unlink = Path.unlink

    def denied_partial(path, *args, **kwargs):
        if path.parent.name == '.supermono-partials':
            raise PermissionError('Fixture scanner temporarily prevents delete')
        return unlink(path, *args, **kwargs)

    with patch.object(Path, 'unlink', denied_partial):
        saved = store.finish('owner', transfer)
    assert Path(saved).read_bytes() == b'x'
    # Publication must release the active-upload quota despite cleanup failure.
    another = store.begin('owner', 'session', 'next.pdf', 0, hashlib.sha256(b'').hexdigest())
    store.cancel('owner', another)


def test_native_close_callback_does_not_wait_for_source_snapshot_lock():
    from supermonoagent.desktop_files import DesktopFiles
    from supermonoagent.windows_clipboard import ClipboardFiles

    class Event:
        def __iadd__(self, callback):
            return self

    window = SimpleNamespace(expose=lambda *_: None,
        get_current_url=lambda: 'http://127.0.0.1:6771/workspace',
        events=SimpleNamespace(loaded=Event(), closed=Event()))
    bridge = DesktopFiles(window, 'http://127.0.0.1:6771/workspace')
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()

    def source_getter():
        entered.set()
        assert release.wait(3)
        return []

    bridge._files = ClipboardFiles(getter=source_getter)
    outcomes = []

    def reading():
        try:
            bridge.clipboard_files()
        except Exception as error:
            outcomes.append(type(error).__name__)
        else:
            outcomes.append('returned')

    reader = threading.Thread(target=reading)
    reader.start()
    assert entered.wait(1)

    def closing():
        bridge._close()
        returned.set()

    closer = threading.Thread(target=closing)
    closer.start()
    try:
        assert returned.wait(.2), 'Native close event blocked behind clipboard source I/O'
    finally:
        release.set()
        reader.join(1)
        closer.join(1)
        cleanup = getattr(bridge, '_retire_thread', None)
        if cleanup is not None:
            cleanup.join(1)
    assert not reader.is_alive() and not closer.is_alive()
    assert cleanup is None or not cleanup.is_alive()
    assert outcomes == ['ValueError'], 'A closed native page must not receive a completed source snapshot'
