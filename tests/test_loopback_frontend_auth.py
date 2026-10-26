"""Credential-free IPv4 loopback clients can reuse an existing daemon safely."""

import asyncio
from types import SimpleNamespace

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from supermonoagent.frontend import Frontend
from supermonoagent.protocol import OP_HELLO, pack_json, unpack, unpack_json


@pytest.mark.parametrize('body', [{}, {'token': ''}])
def test_local_empty_token_uses_private_owner_only_upstream(tmp_path, body):
    asyncio.run(_check_local_owner(tmp_path, body))


async def _check_local_owner(tmp_path, body):
    owner = tmp_path / 'token'
    owner.write_text('private-daemon-owner', encoding='utf-8')
    observed = []

    async def legacy_backend(ws):
        first = await ws.recv()
        op, payload = unpack(first)
        observed.append((ws.request.headers.get('Authorization'), unpack_json(payload)))
        assert op == OP_HELLO
        assert observed[-1] == ('Bearer private-daemon-owner', {**body, 'token': 'private-daemon-owner'})
        await ws.send(pack_json(2, {'windows': [], 'service': {'owner_authenticated': True}}))
        await ws.wait_closed()

    async with serve(legacy_backend, '127.0.0.1', 0) as backend:
        app = Frontend(f'http://127.0.0.1:{backend.sockets[0].getsockname()[1]}', tmp_path / 'themes.json', owner_token_file=owner)
        async with serve(app.handler, '127.0.0.1', 0, process_request=app.serve_http) as server:
            async with connect(f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ws', proxy=None) as client:
                await client.send(pack_json(OP_HELLO, body))
                op, payload = unpack(await asyncio.wait_for(client.recv(), 2))
                assert op == 2
                assert 'private-daemon-owner' not in payload.decode()
    assert len(observed) == 1


@pytest.mark.parametrize('peer,local,authority,origin,expected', [
    ('127.0.0.1', '127.0.0.1', '127.0.0.1:6771', 'http://127.0.0.1:6771', True),
    ('127.0.0.1', '127.0.0.1', '127.0.0.1:6771', None, True),
    ('192.168.1.2', '127.0.0.1', '127.0.0.1:6771', None, False),
    ('::1', '::1', '[::1]:6771', None, False),
    ('127.0.0.1', '127.0.0.1', 'attacker.example:6771', None, False),
    ('127.0.0.1', '127.0.0.1', '127.0.0.1:6771', 'http://attacker.example', False),
    ('127.0.0.1', '127.0.0.1', '127.0.0.1:6771', 'null', False),
    ('127.0.0.1', '0.0.0.0', '127.0.0.1:6771', None, False),
])
def test_owner_bypass_requires_actual_local_peer_and_local_origin(peer, local, authority, origin, expected):
    headers = {'Host': authority}
    if origin is not None:
        headers['Origin'] = origin
    client = SimpleNamespace(remote_address=(peer, 1234), local_address=(local, 6771), request=SimpleNamespace(headers=headers))
    assert Frontend._local_owner_connection(client) is expected


def test_nonlocal_client_cannot_use_private_owner_file(tmp_path):
    asyncio.run(_check_nonlocal_denied(tmp_path))


async def _check_nonlocal_denied(tmp_path):
    owner = tmp_path / 'token'
    owner.write_text('private-daemon-owner', encoding='utf-8')
    app = Frontend('http://127.0.0.1:1', tmp_path / 'themes.json', owner_token_file=owner)

    async def nonlocal_handler(client):
        # Simulate a nonlocal transport while retaining a real WebSocket exchange.
        client.remote_address_override = ('192.168.1.2', 1234)
        original = app._local_owner_connection
        app._local_owner_connection = lambda connection: original(SimpleNamespace(
            remote_address=connection.remote_address_override,
            local_address=connection.local_address,
            request=connection.request))
        await app.handler(client)

    async with serve(nonlocal_handler, '127.0.0.1', 0) as server:
        async with connect(f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ws', proxy=None) as client:
            await client.send(pack_json(OP_HELLO, {}))
            with pytest.raises(ConnectionClosed) as closed:
                await asyncio.wait_for(client.recv(), 2)
            assert closed.value.rcvd.code == 4001
