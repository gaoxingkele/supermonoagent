"""Old daemons send replay before its size metadata: recover after both."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import struct
from unittest.mock import Mock, patch

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from supermonoagent.console_snapshot import FooterRecovery
from supermonoagent.frontend import Frontend
from supermonoagent.protocol import (MAX_FRAME, OP_HELLO, OP_HELLO_OK, OP_OUTPUT,
                                    OP_PING, OP_PONG, OP_SNAPSHOT, OP_SUBSCRIBE, pack, pack_json, unpack, unpack_json)
from supermonoagent.relay_bridge import Phone, RelayBridge


OWNER = 'footer-order-fixture-owner'
HELLO = pack_json(OP_HELLO, {'token': OWNER, 'multiplex': True, 'reliable': True})
SUBSCRIBE = pack_json(OP_SUBSCRIBE, {'sessions': ['sid']})
FOOTER = b'\x1b7\x1b[19;1Hcontext 52% weekly 81%\x1b8'
LIVE = pack(OP_OUTPUT, b'sid\0later live bytes')
FULL_SNAPSHOT = pack(OP_SNAPSHOT, b'sid\0' + b'x' * (MAX_FRAME - 5))
UNKNOWN_SNAPSHOT = pack(OP_SNAPSHOT, b'unknown\0private replay')


def state(ready=False):
    return {'windows': [{'id': 'window', 'sessions': [{'id': 'sid', 'shell': 'cmd'}]}],
            'capabilities': {'window_themes': True, 'conpty_paste': True},
            # Even a control entry cannot authorize a session absent from the
            # caller's catalog. This must never reach the console reader.
            'controls': {'sid': {'cols': 100, 'rows': 20},
                         'unknown': {'cols': 100, 'rows': 20}} if ready else {}}


@asynccontextmanager
async def old_backend():
    received = []
    closed = asyncio.Event()

    async def handler(socket):
        try:
            assert socket.request.headers['Authorization'] == 'Bearer ' + OWNER
            received.append(await socket.recv())
            assert received[-1] == HELLO
            await socket.send(pack_json(OP_HELLO_OK, state()))
            received.append(await socket.recv())
            assert received[-1] == SUBSCRIBE
            await socket.send(FULL_SNAPSHOT)
            await socket.send(UNKNOWN_SNAPSHOT)
            # Existing daemon behavior: subscription replay arrives first,
            # followed by a second HELLO_OK containing its canonical grid.
            await socket.send(pack_json(OP_HELLO_OK, state(True)))
            await socket.send(LIVE)
            async for frame in socket:
                op, payload = unpack(frame)
                if op == OP_PING and payload.startswith(b'frontend-replay:'):
                    await socket.send(pack(OP_PONG, payload))
                    continue
                received.append(frame)
        finally:
            closed.set()

    async with serve(handler, '127.0.0.1', 0, compression=None) as server:
        yield f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}', received, closed


@asynccontextmanager
async def proxy_client(kind, backend, tmp_path, recovery):
    if kind == 'frontend':
        frontend = Frontend(backend, tmp_path / 'themes.json', update_root=tmp_path / 'updates')
        frontend.footer_recovery = recovery
        async with serve(frontend.handler, '127.0.0.1', 0, compression=None) as server:
            url = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ws'
            try:
                async with connect(url, max_size=1024 * 1024, compression=None) as client:
                    yield client.send, client.recv
            finally:
                await frontend.close_updates()
    else:
        bridge = RelayBridge(backend, OWNER)
        bridge.footer_recovery = recovery
        route = Phone()
        bridge.phones[23] = route
        outbound = asyncio.Queue()
        job = asyncio.create_task(bridge._phone(23, route, outbound))

        async def send(frame):
            await route.queue.put(frame)

        async def receive():
            phone_id, actual_route, wrapped = await outbound.get()
            assert phone_id == 23 and actual_route is route
            assert struct.unpack_from('<I', wrapped)[0] == 23
            return wrapped[4:]

        try:
            yield send, receive
        finally:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)


@pytest.mark.parametrize('kind', ['frontend', 'relay-bridge'])
@pytest.mark.parametrize('enabled', [False, True])
def test_full_replay_before_controls_recovers_in_order_without_input(kind, enabled, tmp_path):
    async def check():
        async with old_backend() as (backend, received, closed):
            recovery = FooterRecovery(tmp_path, backend)
            if enabled:
                # Keep the production authorization/dimension validator; only
                # replace its OS read boundary with a deterministic footer.
                recovery.patch = Mock(return_value=FOOTER)
            with patch('supermonoagent.console_snapshot.read_console') as read, \
                    patch('supermonoagent.console_snapshot._process_identity') as identity:
                async with proxy_client(kind, backend, tmp_path, recovery) as (send, receive):
                    await send(HELLO)
                    initial = await asyncio.wait_for(receive(), 3)
                    assert unpack(initial)[0] == OP_HELLO_OK
                    assert unpack_json(unpack(initial)[1])['controls'] == {}
                    await send(SUBSCRIBE)
                    # Neither unknown IDs nor a full protocol frame may be
                    # trimmed, reordered or replaced by recovery machinery.
                    assert await asyncio.wait_for(receive(), 3) == FULL_SNAPSHOT
                    assert await asyncio.wait_for(receive(), 3) == UNKNOWN_SNAPSHOT
                    metadata = await asyncio.wait_for(receive(), 3)
                    assert unpack(metadata)[0] == OP_HELLO_OK
                    assert unpack_json(unpack(metadata)[1])['controls']['sid'] == {'cols': 100, 'rows': 20}
                    if enabled:
                        assert await asyncio.wait_for(receive(), 3) == pack(OP_OUTPUT, b'sid\0' + FOOTER)
                        recovery.patch.assert_called_once_with('sid', 100, 20)
                    assert await asyncio.wait_for(receive(), 3) == LIVE
                await asyncio.wait_for(closed.wait(), 3)
                # Recovery performs no open/input/resize or other backend RPC.
                assert received == [HELLO, SUBSCRIBE]
                read.assert_not_called()
                identity.assert_not_called()

    asyncio.run(check())
