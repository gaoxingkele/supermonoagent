import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from supermonoagent.daemon import Daemon
from supermonoagent.hub import Hub
from supermonoagent.protocol import OP_OUTPUT, OP_SNAPSHOT, pack, pack_json, unpack
from supermonoagent.relay import Relay, Room
from test_windows import FakePty


class MultiplexTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = Hub(pty_factory=lambda *_: FakePty())
        self.hub.attach_loop(asyncio.get_running_loop())
        self.window = self.hub.open_window()
        self.left = self.hub.open_cli(self.window.id, "powershell")
        self.right = self.hub.open_cli(self.window.id, "cmd")

    async def asyncTearDown(self):
        self.hub.close_window(self.window.id)

    def client(self, key="modern", capacity=32):
        client = self.hub.attach(asyncio.Queue(capacity), key=key)
        client.multiplex = True
        client.reliable = True
        return client

    def drain(self, client):
        items = []
        while not client.queue.empty():
            items.append(client.queue.get_nowait())
        return items

    async def test_one_connection_routes_subscribed_sessions_once(self):
        client = self.client()
        self.hub.set_subscriptions(client, [self.left.id])
        self.drain(client)
        self.hub._flushed(self.left.id, b"left", False)
        self.hub._flushed(self.right.id, b"right", False)
        self.assertEqual([unpack(f) for f in self.drain(client)], [
            (OP_OUTPUT, self.left.id.encode() + b"\0left")])
        self.hub.set_subscriptions(client, [self.left.id, self.right.id])
        snapshots = [unpack(f) for f in self.drain(client)]
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0][0], OP_SNAPSHOT)

    async def test_subscription_cannot_cross_owner_or_channel(self):
        guest_group = self.hub.ensure_owner("guest")
        guest_window = self.hub.open_window(group_id=guest_group.id, owner="guest")
        try:
            guest = self.hub.open_cli(guest_window.id, "cmd")
            client = self.client("relay-modern")
            self.hub.set_subscriptions(client, [self.left.id, guest.id, "missing"])
            self.assertEqual(client.subscriptions, {self.left.id})
            other_group = self.hub.add_group("other")
            self.hub.move_window(self.window.id, other_group.id)
            client.channel = self.hub.groups["default"].channel
            self.assertFalse(self.hub.write_session(client, self.left.id, b"no"))
            self.hub._flushed(self.left.id, b"hidden", False)
            self.assertNotIn(b"hidden", b"".join(self.drain(client)))
        finally:
            self.hub.close_window(guest_window.id)

    async def test_targeted_keys_ignore_stale_focus_and_unsubscribed_sessions(self):
        a, b = self.client("a"), self.client("b")
        self.hub.set_subscriptions(a, [self.left.id])
        self.hub.set_subscriptions(b, [self.right.id])
        self.hub.select(a, self.window.id)
        self.assertEqual(a.session_id, self.right.id)
        self.assertTrue(self.hub.write_session(a, self.left.id, b"a"))
        self.assertTrue(self.hub.write_session(b, self.right.id, b"b"))
        self.assertFalse(self.hub.write_session(a, self.right.id, b"wrong"))
        self.assertEqual(bytes(self.left.pty.written), b"a")
        self.assertEqual(bytes(self.right.pty.written), b"b")
        self.hub.close_session(self.left.id)
        self.assertFalse(self.hub.write_session(a, self.left.id, b"stale"))
        self.assertEqual(bytes(self.right.pty.written), b"b")

    async def test_resize_lease_release_and_explicit_takeover(self):
        a, b = self.client("a"), self.client("b")
        for client in (a, b):
            self.hub.set_subscriptions(client, [self.left.id])
        self.assertTrue(self.hub.resize_session(a, self.left.id, 120, 40))
        self.assertFalse(self.hub.resize_session(b, self.left.id, 50, 20))
        self.assertEqual((self.left.cols, self.left.rows), (120, 40))
        self.assertTrue(self.hub.resize_session(b, self.left.id, 50, 20, take_control=True))
        self.hub.detach(b.key)
        self.assertTrue(self.hub.resize_session(a, self.left.id, 100, 30))
        self.assertEqual(self.left.resize_owner, a.key)

    async def test_legacy_and_multiplex_resize_share_terminal_limits(self):
        legacy = self.client("legacy")
        legacy.multiplex = False
        self.hub.select(legacy, self.window.id)
        self.hub.focus(legacy, self.left.id)
        self.hub.resize(legacy, 700, 300)
        self.assertEqual((self.left.cols, self.left.rows), (500, 200))
        modern = self.client()
        self.hub.set_subscriptions(modern, [self.left.id])
        self.assertTrue(self.hub.resize_session(modern, self.left.id, 1, 0, take_control=True))
        self.assertEqual((self.left.cols, self.left.rows), (2, 1))

    async def test_slow_reliable_client_is_closed_instead_of_losing_delta(self):
        client = self.client(capacity=1)
        self.hub._send(client, pack(OP_OUTPUT, b"first"))
        self.hub._send(client, pack(OP_OUTPUT, b"second"))
        self.assertNotIn(client.key, self.hub.clients)
        self.assertIn("slow", client.failure.lower())
        self.assertIsNone(client.queue.get_nowait())
        self.assertFalse(self.left.stop.is_set())

    async def test_pump_overflow_is_explicit_for_reliable_client(self):
        client = self.client()
        self.hub.set_subscriptions(client, [self.left.id])
        self.drain(client)
        self.hub._flushed(self.left.id, b"partial-history", True)
        self.assertNotIn(client.key, self.hub.clients)
        self.assertIn("overflow", client.failure.lower())

    async def test_relay_overflow_disconnects_only_reliable_phone(self):
        class Socket:
            closed = None
            async def close(self, code=1000, reason=""):
                self.closed = (code, reason)
        relay, room, ws = Relay(), Room(), Socket()
        room.phones[1] = ws
        room.queues[1] = asyncio.Queue(1)
        room.reliable.add(1)
        room.queues[1].put_nowait(b"first")
        relay._enqueue(room, 1, b"second")
        await asyncio.sleep(0)
        self.assertEqual(ws.closed[0], 1013)


class NegotiationTest(unittest.IsolatedAsyncioTestCase):
    async def test_capabilities_and_atomic_targeted_input(self):
        with tempfile.TemporaryDirectory() as path, patch("supermonoagent.daemon.STATE", Path(path)), patch.dict(
            "os.environ", {"SUPERMONO_STATE_DIR": path}
        ):
            daemon = Daemon("owner")
            daemon.hub = Hub(pty_factory=lambda *_: FakePty())
            daemon.hub.attach_loop(asyncio.get_running_loop())
            client = daemon.hub.attach(asyncio.Queue(32), token="owner")
            frames, _ = await daemon._op(client, 1, unpack(pack_json(1, {
                "token": "owner", "multiplex": True, "reliable": True}))[1])
            import json
            self.assertTrue(json.loads(unpack(frames[0])[1])["capabilities"]["multiplex"])
            window = daemon.hub.open_window(owner="owner")
            session = daemon.hub.open_cli(window.id, "cmd")
            try:
                await daemon._op(client, 16, unpack(pack_json(16, {"sessions": [session.id]}))[1])
                await daemon._op(client, 17, session.id.encode() + b"\0targeted")
                self.assertEqual(bytes(session.pty.written), b"targeted")
            finally:
                daemon.hub.close_window(window.id)
