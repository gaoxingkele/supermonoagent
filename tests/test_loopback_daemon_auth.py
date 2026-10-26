"""Owner access is implicit only on the IPv4 loopback direct transport."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from supermonoagent.protocol import OP_HELLO, OP_HELLO_OK, pack_json, unpack_json
from test_client_integration import isolated_host, receive_op


@pytest.mark.parametrize("hello", [{"multiplex": True}, {"token": "", "multiplex": True}])
def test_loopback_without_token_sees_owner_windows(hello):
    async def check():
        async with isolated_host() as (daemon, direct, _relay):
            window = daemon.hub.open_window("sCodex", owner=daemon.token)
            async with connect(direct, proxy=None) as ws:
                await ws.send(pack_json(OP_HELLO, hello))
                state = unpack_json(await receive_op(ws, OP_HELLO_OK))
                assert state["service"]["owner_authenticated"] is True
                assert any(item["id"] == window.id for item in state["windows"])
    asyncio.run(check())


def test_loopback_explicit_invalid_token_still_rejected():
    async def check():
        async with isolated_host() as (_daemon, direct, _relay):
            async with connect(direct, proxy=None) as ws:
                await ws.send(pack_json(OP_HELLO, {"token": "invalid"}))
                with pytest.raises(ConnectionClosed) as closed:
                    await ws.recv()
                assert closed.value.rcvd.code == 4001
    asyncio.run(check())


def test_loopback_explicit_account_token_keeps_account_scope():
    async def check():
        async with isolated_host() as (daemon, direct, _relay):
            owner_window = daemon.hub.open_window("Owner only", owner=daemon.token)
            token = daemon.accounts.register("local-account@example.test", "test-password")
            async with connect(direct, proxy=None) as ws:
                await ws.send(pack_json(OP_HELLO, {"token": token, "multiplex": True}))
                state = unpack_json(await receive_op(ws, OP_HELLO_OK))
                assert state["service"]["owner_authenticated"] is False
                assert not any(item["id"] == owner_window.id for item in state["windows"])
    asyncio.run(check())


def test_nonlocal_peer_cannot_omit_token():
    class RemotePeer:
        remote_address = ("192.0.2.20", 5000)

        def __init__(self, ws):
            self.ws = ws

        def __getattr__(self, name):
            return getattr(self.ws, name)

    async def check():
        async with isolated_host() as (daemon, _direct, _relay):
            async def handler(ws):
                await daemon.handle_socket(RemotePeer(ws))
            async with serve(handler, "127.0.0.1", 0) as server:
                url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ws"
                async with connect(url, proxy=None) as ws:
                    await ws.send(pack_json(OP_HELLO, {}))
                    with pytest.raises(ConnectionClosed) as closed:
                        await ws.recv()
                    assert closed.value.rcvd.code == 4001
    asyncio.run(check())


@pytest.mark.parametrize("host,origin", [
    ("attacker.example:6769", None),
    ("127.0.0.1:6769", "https://attacker.example"),
    ("127.0.0.1:6769", "null"),
    ("127.0.0.1:9999", None),
])
def test_loopback_owner_checks_host_and_origin(host, origin):
    async def check():
        async with isolated_host() as (daemon, _direct, _relay):
            headers = {"Host": host}
            if origin is not None:
                headers["Origin"] = origin
            ws = SimpleNamespace(remote_address=("127.0.0.1", 5000), local_address=("127.0.0.1", 6769),
                                 request=SimpleNamespace(headers=headers), transport=None)
            assert not daemon._local_owner_peer(ws)
    asyncio.run(check())


def test_relay_phone_still_requires_token():
    async def check():
        async with isolated_host() as (_daemon, _direct, relay):
            async with connect(relay, proxy=None) as ws:
                await ws.send(json.dumps({"op": "phone", "code": "local-test"}))
                with pytest.raises(ConnectionClosed) as closed:
                    await ws.recv()
                assert closed.value.rcvd.code == 4001
    asyncio.run(check())
