"""A live legacy daemon can replay several tabs without an atomic queue burst."""

import asyncio
from contextlib import suppress

import pytest

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from supermonoagent.frontend import Frontend
from supermonoagent.protocol import (
    OP_HELLO, OP_HELLO_OK, OP_OUTPUT, OP_PING, OP_PONG,
    OP_SNAPSHOT, OP_SUBSCRIBE, pack, pack_json, unpack, unpack_json,
)


@pytest.mark.parametrize('rapid_cumulative', [False, True])
def test_six_legacy_replays_are_drained_without_losing_bytes_or_existing_lease(tmp_path, rapid_cumulative):
    asyncio.run(_check_six_replays(tmp_path, rapid_cumulative))


async def _check_six_replays(tmp_path, rapid_cumulative):
    subscriptions = []
    overflowed = []
    lease_released = []
    sids = ['keep', *[f'session-{i}' for i in range(6)]]

    async def backend(socket):
        # Match the legacy daemon: each subscription operation fills a bounded
        # queue synchronously, before its socket sender gets another turn.
        queue = asyncio.Queue(32)
        selected = set()

        def state():
            return pack_json(OP_HELLO_OK, {
                'windows': [{'id': 'window', 'sessions': [{'id': sid} for sid in sids]}],
                'controls': {sid: {'cols': 80, 'rows': 24} for sid in selected},
                'capabilities': {'window_themes': True, 'conpty_paste': True},
                'service': {'owner_authenticated': True},
            })

        async def sender():
            while True:
                await socket.send(await queue.get())
                await asyncio.sleep(0)

        task = asyncio.create_task(sender())
        try:
            async for raw in socket:
                op, payload = unpack(raw)
                if op == OP_HELLO:
                    queue.put_nowait(state())
                elif op == OP_SUBSCRIBE:
                    body = unpack_json(payload)
                    requested = set(body['sessions'])
                    subscriptions.append(list(body['sessions']))
                    if 'keep' in selected and 'keep' not in requested:
                        lease_released.append(True)
                    added = requested - selected
                    selected = requested
                    try:
                        for sid in sorted(added):
                            queue.put_nowait(pack(OP_SNAPSHOT, sid.encode() + b'\0history-0'))
                            for index in range(1, 8):
                                queue.put_nowait(pack(OP_OUTPUT, sid.encode() + b'\0history-' + str(index).encode()))
                        queue.put_nowait(state())
                    except asyncio.QueueFull:
                        overflowed.append(True)
                        await socket.close(code=1013, reason='Slow client output queue overflow')
                        return
                elif op == OP_PING:
                    queue.put_nowait(pack(OP_PONG, payload))
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async with serve(backend, '127.0.0.1', 0, compression=None) as daemon:
        frontend = Frontend(f'http://127.0.0.1:{daemon.sockets[0].getsockname()[1]}', tmp_path / 'themes.json')
        frontend.footer_recovery.supplement = lambda *_: None
        async with serve(frontend.handler, '127.0.0.1', 0, compression=None) as server:
            async with connect(f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ws', proxy=None) as client:
                await client.send(pack_json(OP_HELLO, {'token': 'fixture-owner', 'multiplex': True, 'reliable': True}))
                assert unpack(await asyncio.wait_for(client.recv(), 3))[0] == OP_HELLO_OK
                await client.send(pack_json(OP_SUBSCRIBE, {'sessions': ['keep']}))
                while True:
                    op, _ = unpack(await asyncio.wait_for(client.recv(), 3))
                    if op == OP_HELLO_OK:
                        break
                requests = [sids[:index] for index in range(2, 8)] if rapid_cumulative else [sids]
                for sessions in requests:
                    await client.send(pack_json(OP_SUBSCRIBE, {'sessions': sessions}))
                await client.send(pack(OP_PING, b'client-barrier'))
                output = []
                final_controls = None
                while True:
                    op, payload = unpack(await asyncio.wait_for(client.recv(), 3))
                    if op == OP_PONG:
                        assert payload == b'client-barrier', 'Internal barrier leaked to client'
                        break
                    if op in {OP_OUTPUT, OP_SNAPSHOT}:
                        output.append((op, payload))
                    elif op == OP_HELLO_OK:
                        final_controls = unpack_json(payload)['controls']
                assert output == [
                    (OP_SNAPSHOT if index == 0 else OP_OUTPUT,
                     sid.encode() + b'\0history-' + str(index).encode())
                    for sid in sids[1:] for index in range(8)
                ]
                assert set(final_controls) == set(sids)
    assert not overflowed
    assert not lease_released
    assert subscriptions == [['keep'], *[sids[:index] for index in range(2, 8)]]
