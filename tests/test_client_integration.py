"""Real loopback WebSocket tests. Never load saved relay or desktop settings."""

import asyncio
import json
import os
import queue
import sys
import tempfile
import unittest
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from supermonoagent.daemon import Daemon, relay_uplink
from supermonoagent.protocol import (
    OP_HELLO, OP_HELLO_OK, OP_INPUT_SESSION, OP_OUTPUT, OP_PING, OP_PONG,
    OP_SNAPSHOT, OP_SUBSCRIBE, pack, pack_json, unpack,
)
from supermonoagent.pty import PtyClosed
from supermonoagent.relay import Relay


class ControlledPty:
    def __init__(self):
        self.written = bytearray()
        self.inbox = queue.Queue()

    def read(self, n=4096):
        value = self.inbox.get()
        if value is None:
            raise PtyClosed()
        return value

    def write(self, data):
        self.written.extend(data)

    def resize(self, cols, rows):
        pass

    def close(self):
        self.inbox.put(None)


@asynccontextmanager
async def isolated_host(real_shell=False):
    with tempfile.TemporaryDirectory(prefix="supermono-integration-") as directory, patch(
        "supermonoagent.daemon.STATE", Path(directory)
    ), patch.dict(os.environ, {"SUPERMONO_STATE_DIR": directory}):
        daemon = Daemon("integration-owner")
        daemon.hub.persist = False
        if not real_shell:
            daemon.hub.pty_factory = lambda *_: ControlledPty()
        daemon.hub.attach_loop(asyncio.get_running_loop())
        jobs = []
        async with AsyncExitStack() as stack:
            direct = await stack.enter_async_context(serve(daemon.handle_socket, "127.0.0.1", 0))
            relay = Relay(development=True)
            relay_server = await stack.enter_async_context(serve(relay.handler, "127.0.0.1", 0))
            direct_url = f"ws://127.0.0.1:{direct.sockets[0].getsockname()[1]}/ws"
            relay_url = f"ws://127.0.0.1:{relay_server.sockets[0].getsockname()[1]}/ws"
            job = asyncio.create_task(relay_uplink(daemon, relay_url, "local-test", "test"))
            jobs.append(job)
            try:
                async with asyncio.timeout(5):
                    while relay_url not in daemon.relay_live:
                        await asyncio.sleep(0.01)
                yield daemon, direct_url, relay_url
            finally:
                for ws in list(daemon._relay_sockets.values()):
                    await ws.close(code=1000)
                for window_id in list(daemon.hub.windows):
                    daemon.hub.close_window(window_id)
                for task in jobs:
                    task.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
                await asyncio.sleep(0.03)


async def receive_op(ws, wanted, timeout=5):
    async with asyncio.timeout(timeout):
        while True:
            frame = await ws.recv()
            if isinstance(frame, bytes):
                op, payload = unpack(frame)
                if op == wanted:
                    return payload


async def barrier(ws):
    await ws.send(pack(OP_PING, b"barrier"))
    assert await receive_op(ws, OP_PONG) == b"barrier"


@asynccontextmanager
async def subscribed(url, sessions, relay=False):
    headers = {} if relay else {"Authorization": "Bearer integration-owner"}
    async with connect(url, additional_headers=headers, compression=None) as ws:
        if relay:
            await ws.send(json.dumps({"op": "phone", "code": "local-test", "token": "integration-owner", "reliable": True}))
            assert json.loads(await ws.recv())["op"] == "ready"
        await ws.send(pack_json(OP_HELLO, {"token": "integration-owner", "multiplex": True, "reliable": True}))
        await receive_op(ws, OP_HELLO_OK)
        await ws.send(pack_json(OP_SUBSCRIBE, {"sessions": sessions}))
        await barrier(ws)
        yield ws


class ClientIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_subscription_snapshot_does_not_replay_pending_output(self):
        async with isolated_host() as (daemon, direct, _):
            window = daemon.hub.open_window(owner="integration-owner")
            session = daemon.hub.open_cli(window.id, "cmd")
            # Leave a deterministic gap between reader history and batched broadcast.
            session.pump.coalesce_s = 0.3
            session.pty.inbox.put(b"pending-cutover")
            async with asyncio.timeout(2):
                while b"pending-cutover" not in session.pump.snapshot():
                    await asyncio.sleep(0.001)
            async with connect(direct, additional_headers={"Authorization": "Bearer integration-owner"}) as ws:
                await ws.send(pack_json(OP_HELLO, {"token": "integration-owner", "multiplex": True, "reliable": True}))
                await receive_op(ws, OP_HELLO_OK)
                await ws.send(pack_json(OP_SUBSCRIBE, {"sessions": [session.id]}))
                snapshot = await receive_op(ws, OP_SNAPSHOT)
                self.assertEqual(snapshot, session.id.encode() + b"\0pending-cutover")
                await asyncio.sleep(0.35)
                await ws.send(pack(OP_PING, b"cutover-complete"))
                while True:
                    op, payload = unpack(await asyncio.wait_for(ws.recv(), 2))
                    self.assertNotEqual(op, OP_OUTPUT, "snapshot bytes were emitted again by the pending pump batch")
                    if op == OP_PONG:
                        self.assertEqual(payload, b"cutover-complete")
                        break

    async def test_bearer_auth_works_before_hello(self):
        async with isolated_host() as (daemon, direct, _):
            async with connect(direct, additional_headers={"Authorization": "Bearer integration-owner"}) as ws:
                # PING before HELLO proves authorization comes from the HTTP header.
                await barrier(ws)
                self.assertEqual(next(iter(daemon.hub.clients.values())).token, "integration-owner")

    async def test_direct_and_relay_multiplex_once_and_targeted_input(self):
        async with isolated_host() as (daemon, direct, relay):
            window = daemon.hub.open_window(owner="integration-owner")
            sessions = [daemon.hub.open_cli(window.id, shell) for shell in ("powershell", "cmd")]
            ids = [session.id for session in sessions]
            for url, relayed in ((direct, False), (relay, True)):
                async with subscribed(url, ids, relayed) as all_ws, subscribed(url, [ids[1]], relayed) as right_ws:
                    # Duplicate subscription IDs must not duplicate a session's stream.
                    await all_ws.send(pack_json(OP_SUBSCRIBE, {"sessions": ids + ids}))
                    await barrier(all_ws)
                    for index, session in enumerate(sessions):
                        daemon.hub._flushed(session.id, f"stream-{index}".encode(), False)
                    seen = [await receive_op(all_ws, OP_OUTPUT) for _ in sessions]
                    self.assertCountEqual(seen, [sid.encode() + b"\0" + f"stream-{i}".encode() for i, sid in enumerate(ids)])
                    self.assertEqual(await receive_op(right_ws, OP_OUTPUT), ids[1].encode() + b"\0stream-1")
                    await all_ws.send(pack(OP_INPUT_SESSION, ids[0].encode() + b"\0left"))
                    await right_ws.send(pack(OP_INPUT_SESSION, ids[1].encode() + b"\0right"))
                    await right_ws.send(pack(OP_INPUT_SESSION, ids[0].encode() + b"\0forbidden"))
                    await barrier(all_ws)
                    await barrier(right_ws)
                    # No queued extra output may be hidden by the routing checks.
                    for ws in (all_ws, right_ws):
                        await ws.send(pack(OP_PING, b"end"))
                        while True:
                            op, payload = unpack(await asyncio.wait_for(ws.recv(), 5))
                            self.assertNotEqual(op, OP_OUTPUT)
                            if op == OP_PONG:
                                self.assertEqual(payload, b"end")
                                break
            self.assertEqual(bytes(sessions[0].pty.written), b"leftleft")
            self.assertEqual(bytes(sessions[1].pty.written), b"rightright")

    @unittest.skipUnless(sys.platform == "win32", "requires Windows ConPTY")
    async def test_real_powershell_cmd_share_direct_and_relay_sessions(self):
        async with isolated_host(real_shell=True) as (daemon, direct, relay):
            window = daemon.hub.open_window(owner="integration-owner")
            for shell in ("powershell", "cmd"):
                session = daemon.hub.open_cli(window.id, shell)
                async with subscribed(direct, [session.id]) as desktop, subscribed(relay, [session.id], True) as phone:
                    # Split marker in command so the echoed command itself cannot satisfy it.
                    command = "Write-Output ('SMA_' + 'PS_OK')\r" if shell == "powershell" else "set SMA_MARK=CMD_OK\r\necho SMA_%SMA_MARK%\r\n"
                    expected = b"SMA_PS_OK" if shell == "powershell" else b"SMA_CMD_OK"
                    await phone.send(pack(OP_INPUT_SESSION, session.id.encode() + b"\0" + command.encode()))

                    async def marker(ws):
                        output = bytearray()
                        async with asyncio.timeout(15):
                            while expected not in output:
                                op, payload = unpack(await ws.recv())
                                if op in (OP_OUTPUT, OP_SNAPSHOT):
                                    sid, data = payload.split(b"\0", 1)
                                    self.assertEqual(sid.decode(), session.id)
                                    output.extend(data)
                        return output

                    await asyncio.gather(marker(desktop), marker(phone))
                self.assertFalse(session.stop.is_set(), "disconnecting views must preserve the daemon shell")


if __name__ == "__main__":
    unittest.main()
