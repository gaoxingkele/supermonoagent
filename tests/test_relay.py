import asyncio
import json
import struct
import unittest

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedError
from websockets.asyncio.server import serve

from supermonoagent.daemon import clean_relays
from supermonoagent.protocol import OP_INPUT, pack, unpack
from supermonoagent.relay import Relay, group_hosts


class GroupHostsTest(unittest.TestCase):
    def test_one_project_holds_two_rooms(self):
        grouped = group_hosts([
            {"code": "aaa111", "machine": "OFFICE", "project": "施工"},
            {"code": "bbb222", "machine": "FIELD", "project": "施工"},
        ])
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["project"], "施工")
        self.assertEqual(
            sorted(room["code"] for room in grouped[0]["rooms"]),
            ["aaa111", "bbb222"],
        )


class CleanRelayTest(unittest.TestCase):
    def test_bare_public_url_uses_ws_path(self):
        got = clean_relays([{"name": "公网", "url": "http://117.50.48.22:8817/", "code": "abc"}])
        self.assertEqual(got, [{"name": "公网", "url": "wss://117.50.48.22/ws", "code": "abc"}])


class RelayTest(unittest.IsolatedAsyncioTestCase):
    async def test_splice_copies_terminal_frames(self):
        relay = Relay(development=True)
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
        relay = Relay(development=True)
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

    async def _health(self, relay: Relay) -> dict:
        class Response:
            def __init__(self):
                self.headers = {"Content-Type": "text/plain"}
                self.body = ""

        class Conn:
            def respond(self, status, text):
                res = Response()
                res.body = text
                return res

        class Req:
            path = "/health"

        res = await relay.serve_page(Conn(), Req())
        return json.loads(res.body)

    async def test_two_computers_share_one_port(self):
        relay = Relay(development=True)
        async with serve(relay.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            url = f"ws://127.0.0.1:{port}"
            async with connect(url) as office, connect(url) as field:
                await office.send(json.dumps({
                    "op": "host", "code": "aaa111", "tokens": ["tok-a"],
                    "machine": "OFFICE", "project": "施工",
                }))
                await field.send(json.dumps({
                    "op": "host", "code": "bbb222", "tokens": ["tok-b"],
                    "machine": "FIELD", "project": "施工",
                }))
                self.assertEqual(json.loads(await office.recv())["op"], "ready")
                self.assertEqual(json.loads(await field.recv())["op"], "ready")
                async with connect(url) as extra:
                    await extra.send(json.dumps({
                        "op": "host", "code": "aaa111", "tokens": ["other"],
                        "machine": "OTHER", "project": "施工",
                    }))
                    with self.assertRaises(ConnectionClosedError):
                        await asyncio.wait_for(extra.recv(), 1)
                self.assertIsNotNone(relay.rooms["aaa111"].host)
                self.assertIsNotNone(relay.rooms["bbb222"].host)
                health = await self._health(relay)
                self.assertEqual(health, {'ok': True, 'role': 'relay'})
                async with connect(url) as phone:
                    await phone.send(json.dumps({"op": "phone", "code": "bbb222", "token": "tok-a"}))
                    with self.assertRaises(ConnectionClosedError):
                        await asyncio.wait_for(phone.recv(), 1)
                async with connect(url) as phone:
                    await phone.send(json.dumps({"op": "phone", "code": "bbb222", "token": "tok-b"}))
                    self.assertEqual(json.loads(await phone.recv())["op"], "ready")

    async def test_phone_without_host_is_refused(self):
        relay = Relay(development=True)
        async with serve(relay.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}") as phone:
                await phone.send(json.dumps({"op": "phone", "code": "missing"}))
                with self.assertRaises(ConnectionClosedError):
                    await asyncio.wait_for(phone.recv(), 1)


if __name__ == "__main__":
    unittest.main()
