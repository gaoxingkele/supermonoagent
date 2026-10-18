import asyncio
import json
import struct
import unittest

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedError
from websockets.asyncio.server import serve

from supermonoagent.daemon import clean_relays
from supermonoagent.protocol import OP_INPUT, pack, unpack
from supermonoagent.relay import Relay


class CleanRelayTest(unittest.TestCase):
    def test_bare_public_url_uses_ws_path(self):
        got = clean_relays([{"name": "公网", "url": "http://117.50.48.22:8817/", "code": "abc"}])
        self.assertEqual(got, [{"name": "公网", "url": "ws://117.50.48.22:8817/ws", "code": "abc"}])


class RelayTest(unittest.IsolatedAsyncioTestCase):
    async def test_splice_copies_terminal_frames(self):
        relay = Relay()
        async with serve(relay.handler, "127.0.0.1", 0, max_size=256 * 1024) as server:
            port = server.sockets[0].getsockname()[1]
            url = f"ws://127.0.0.1:{port}"
            async with connect(url) as host:
                await host.send(json.dumps({"op": "host", "code": "abc", "tokens": ["secret"]}))
                self.assertEqual(json.loads(await host.recv())["op"], "ready")
                async with connect(url) as phone, connect(url) as other:
                    await phone.send(json.dumps({"op": "phone", "code": "abc", "token": "secret"}))
                    self.assertEqual(json.loads(await phone.recv())["op"], "ready")
                    joined = json.loads(await host.recv())
                    self.assertEqual(joined["op"], "join")
                    phone_id = joined["id"]
                    await other.send(json.dumps({"op": "phone", "code": "abc", "token": "secret"}))
                    self.assertEqual(json.loads(await other.recv())["op"], "ready")
                    other_join = json.loads(await host.recv())
                    frame = pack(OP_INPUT, b"dir\r")
                    await phone.send(frame)
                    echoed = await asyncio.wait_for(host.recv(), 2)
                    self.assertEqual(struct.unpack_from("<I", echoed)[0], phone_id)
                    self.assertEqual(unpack(echoed[4:]), (OP_INPUT, b"dir\r"))
                    await host.send(struct.pack("<I", phone_id) + pack(3, b"ok"))
                    back = await asyncio.wait_for(phone.recv(), 2)
                    self.assertEqual(unpack(back)[1], b"ok")
                    with self.assertRaises(asyncio.TimeoutError):
                        await asyncio.wait_for(other.recv(), 0.2)
                    self.assertNotEqual(other_join["id"], phone_id)

    async def test_phone_token_is_required(self):
        relay = Relay()
        async with serve(relay.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            url = f"ws://127.0.0.1:{port}"
            async with connect(url) as host:
                await host.send(json.dumps({"op": "host", "code": "abc", "tokens": ["secret"]}))
                await host.recv()
                async with connect(url) as phone:
                    await phone.send(json.dumps({"op": "phone", "code": "abc", "token": ""}))
                    with self.assertRaises(ConnectionClosedError):
                        await asyncio.wait_for(phone.recv(), 1)
                async with connect(url) as phone:
                    await phone.send(json.dumps({"op": "phone", "code": "abc", "token": "secret"}))
                    self.assertEqual(json.loads(await phone.recv())["op"], "ready")
                    joined = json.loads(await host.recv())
                    self.assertEqual(joined["token"], "secret")

    async def test_phone_without_host_is_refused(self):
        relay = Relay()
        async with serve(relay.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}") as phone:
                await phone.send(json.dumps({"op": "phone", "code": "missing"}))
                with self.assertRaises(ConnectionClosedError):
                    await asyncio.wait_for(phone.recv(), 1)


if __name__ == "__main__":
    unittest.main()
