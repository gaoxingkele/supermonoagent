"""Explicit host bindings and trusted-uplink delegation, without user processes."""

import asyncio
import json
import tempfile
import time
import struct
import unittest
from pathlib import Path
from unittest.mock import patch

from websockets.asyncio.server import serve

from supermonoagent.daemon import Daemon, RelayBindings, main, relay_binding_url, relay_uplink
from supermonoagent.hub import Hub
from supermonoagent.protocol import OP_GROUP, OP_HELLO, OP_INPUT_SESSION, OP_OUTPUT, OP_SUBSCRIBE, OP_PING, pack, pack_json, unpack
from test_windows import FakePty


class BindingTest(unittest.TestCase):
    def test_persisted_exact_endpoint_room_and_account_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.json"
            book = RelayBindings(path)
            book.bind("https://RELAY.example:443/", "room-a", "account-a", "local-owner")
            second = RelayBindings(path)
            self.assertEqual(second.owner("wss://relay.example/ws", "room-a", "account-a"), "local-owner")
            self.assertIsNone(second.owner("wss://other.example/ws", "room-a", "account-a"))
            self.assertIsNone(second.owner("wss://relay.example/ws", "room-b", "account-a"))
            self.assertIsNone(second.owner("wss://relay.example/ws", "room-a", "account-b"))
            book.bind("wss://relay.example/ws", "room-a", "account-a", None)
            self.assertIsNone(second.owner("wss://relay.example/ws", "room-a", "account-a"))
            path.write_text("not-json", encoding="utf8")
            self.assertIsNone(second.owner("wss://relay.example/ws", "room-a", "account-a"))

    def test_public_plaintext_and_credential_urls_are_rejected(self):
        for url in ("ws://public.example/ws", "wss://user:pass@relay.example/ws", "wss://relay.example/ws?session=secret",
                    "wss://relay.example/other", "wss://relay.example/ws#secret"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                relay_binding_url(url)

    def test_cli_binding_is_config_only_and_supports_explicit_local_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with patch("supermonoagent.daemon.STATE", path), patch.dict("os.environ", {"SUPERMONO_STATE_DIR": directory}):
                daemon = Daemon("default-owner")
                local = daemon.accounts.register("local@example.test", "test-password")
                args = ["supermono", "--relay", "wss://relay.example", "--code", "room", "--bind-relay-account", "account-a",
                        "--relay-local-account", "local@example.test"]
                with patch("sys.argv", args), patch("supermonoagent.daemon.serve") as server:
                    main()
                    server.assert_not_called()
                self.assertEqual(RelayBindings(path / "relay-bindings.json").owner("wss://relay.example", "room", "account-a"), local)
                args = ["supermono", "--relay", "wss://relay.example", "--code", "room", "--unbind-relay-account", "account-a"]
                with patch("sys.argv", args), patch("supermonoagent.daemon.serve") as server:
                    main()
                    server.assert_not_called()
                self.assertIsNone(RelayBindings(path / "relay-bindings.json").owner("wss://relay.example", "room", "account-a"))


class DelegationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)
        self.state_patch = patch("supermonoagent.daemon.STATE", self.path)
        self.env_patch = patch.dict("os.environ", {"SUPERMONO_STATE_DIR": str(self.path)})
        self.state_patch.start()
        self.env_patch.start()
        self.daemon = Daemon("local-owner")
        self.daemon.hub = Hub(pty_factory=lambda *_: FakePty())
        self.daemon.hub.default_owner = self.daemon.token
        self.daemon.hub.attach_loop(asyncio.get_running_loop())
        self.url = "ws://127.0.0.1:6770/ws"
        self.code = "test-room"
        self.generation = object()
        self.daemon._relay_generations[self.url] = self.generation
        self.daemon.relay_bindings.bind(self.url, self.code, "account-a", self.daemon.token)
        self.window = self.daemon.hub.open_window(owner=self.daemon.token)
        self.session = self.daemon.hub.open_cli(self.window.id, "fake")
        self.jobs = []

    async def asyncTearDown(self):
        for job in self.jobs:
            job.cancel()
        await asyncio.gather(*self.jobs, return_exceptions=True)
        for window in list(self.daemon.hub.windows):
            self.daemon.hub.close_window(window)
        await asyncio.sleep(.01)
        self.env_patch.stop()
        self.state_patch.stop()
        self.directory.cleanup()

    def client(self, account="account-a", expires=None, key="relay-fixture-1", **extra):
        return self.daemon.delegated_client(self.url, self.code, self.generation, 1,
            {"account": account, "expires": time.time() + 30 if expires is None else expires, **extra}, key)

    async def hello(self, client, body=None):
        return await self.daemon._op(client, OP_HELLO, json.dumps({"multiplex": True, **(body or {})}).encode())

    async def test_explicit_delegation_scopes_to_owner_but_never_becomes_allowed_token(self):
        client = self.client()
        frames, closed = await self.hello(client)
        self.assertIsNone(closed)
        self.assertEqual(client.token, self.daemon.token)
        self.assertFalse(self.daemon.allowed("account-a"))
        self.assertEqual(json.loads(unpack(frames[0])[1])["windows"][0]["id"], self.window.id)
        outsider = self.daemon.hub.attach(asyncio.Queue(), token="account-a")
        self.assertEqual((await self.hello(outsider))[1], 4001)
        self.assertIsNone(self.client(account="unbound", key="relay-fixture-2"))

    async def test_hello_cannot_supply_another_token_or_relay_secret(self):
        for field in ("token", "session", "password", "account"):
            client = self.client(key="relay-" + field)
            self.assertEqual((await self.hello(client, {field: "other-secret"}))[1], 4001)
        self.assertIsNone(self.client(token="local-owner"))
        self.assertIsNone(self.client(session="relay-session"))

    async def test_delegation_cannot_redirect_host_owner_secrets_to_a_new_relay(self):
        client = self.client()
        with patch.object(self.daemon, "set_relays") as change:
            await self.daemon._op(client, OP_GROUP, json.dumps({"action": "relays", "relays": [
                {"url": "wss://attacker.example/ws", "code": "capture"}]}).encode())
            change.assert_not_called()

    async def test_delegated_owner_cannot_edit_global_settings_but_keeps_owner_actions(self):
        client = self.client()
        presets = [dict(item) for item in self.daemon.hub.presets]
        identity = (self.daemon.hub.machine, self.daemon.hub.project)
        for body in ({"action": "presets", "presets": [{"label": "Changed", "command": "changed"}]},
                     {"action": "identity", "project": "Changed"},
                     {"action": "relays", "relays": []}):
            _, close = await self.daemon._op(client, OP_GROUP, json.dumps(body).encode())
            self.assertIsNone(close)
        self.assertEqual(self.daemon.hub.presets, presets)
        self.assertEqual((self.daemon.hub.machine, self.daemon.hub.project), identity)
        self.assertFalse((self.path / "machine.json").exists())
        self.assertFalse((self.path / "relays.json").exists())
        for body in ({"action": "create", "name": "My group", "channel": "my-channel"},
                     {"action": "title", "window": self.window.id, "title": "My terminal"},
                     {"action": "tab-theme", "window": self.window.id, "theme": "red"}):
            _, close = await self.daemon._op(client, OP_GROUP, json.dumps(body).encode())
            self.assertIsNone(close)
        self.assertEqual(self.window.title, "My terminal")
        self.assertEqual(self.window.tab_theme, "red")
        self.assertTrue(any(group.channel == "my-channel" and group.owner == client.token
                            for group in self.daemon.hub.groups.values()))

    async def test_legacy_default_owner_keeps_global_settings_authority(self):
        client = self.daemon.hub.attach(asyncio.Queue(), token=self.daemon.token)
        for body in ({"action": "presets", "presets": [{"label": "Changed", "command": "changed"}]},
                     {"action": "identity", "project": "Legacy project"},
                     {"action": "relays", "relays": []}):
            _, close = await self.daemon._op(client, OP_GROUP, json.dumps(body).encode())
            self.assertIsNone(close)
        self.assertEqual(self.daemon.hub.presets[0]["command"], "changed")
        self.assertEqual(self.daemon.hub.project, "Legacy project")
        self.assertTrue((self.path / "machine.json").exists())
        self.assertTrue((self.path / "relays.json").exists())

    async def test_expiry_generation_and_client_instance_gate_ops_and_queued_output(self):
        client = self.client()
        await self.hello(client)
        while not client.queue.empty():
            client.queue.get_nowait()
        self.daemon._relay_generations[self.url] = object()
        self.daemon.hub._send(client, pack(OP_OUTPUT, b"must-not-queue"))
        self.assertTrue(client.queue.empty())
        self.assertEqual((await self.daemon._op(client, OP_PING, b"stale"))[1], 4001)
        self.daemon._relay_generations[self.url] = self.generation
        replacement = self.client(key=client.key)
        self.assertFalse(self.daemon.client_authorized(client))
        self.assertTrue(self.daemon.client_authorized(replacement))
        self.daemon.forget_relay_client(client)
        self.assertIs(self.daemon.hub.clients[replacement.key], replacement)
        self.daemon._delegations[id(replacement)].expires = time.time() - 1
        self.assertFalse(self.daemon.client_authorized(replacement))
        self.assertIsNone(self.client(expires=time.time() - 1, key="expired"))
        self.assertIsNone(self.client(expires=float("nan"), key="nan"))

    async def test_unbind_prunes_queue_and_preserves_pty_and_other_owner(self):
        client = self.client()
        other_owner = "separate-local-owner"
        self.daemon.relay_bindings.bind(self.url, self.code, "account-b", other_owner)
        other = self.client(account="account-b", key="relay-b")
        client.queue.put_nowait(pack(OP_OUTPUT, b"private"))
        external = RelayBindings(self.path / "relay-bindings.json")
        external.bind(self.url, self.code, "account-a", None)
        self.assertFalse(self.daemon.client_authorized(client))
        self.assertTrue(self.daemon.client_authorized(other))
        self.daemon.forget_relay_client(client)
        self.assertTrue(client.queue.empty())
        self.assertNotIn(client.key, self.daemon.hub.clients)
        self.assertIn(self.session.id, self.daemon.hub.sessions)
        self.assertFalse(self.session.stop.is_set())

    async def test_local_token_rotation_does_not_revoke_independent_delegation(self):
        token = self.daemon.accounts.register("local@example.test", "local-password")
        self.daemon.relay_bindings.bind(self.url, self.code, "account-a", token)
        client = self.client()
        direct = self.daemon.hub.attach(asyncio.Queue(), token=token)
        self.daemon.account_reply({"op": "rotate", "email": "local@example.test", "password": "local-password"})
        self.assertTrue(self.daemon.client_authorized(client))
        self.assertIn(client.key, self.daemon.hub.clients)
        self.assertNotIn(direct.key, self.daemon.hub.clients)

    async def test_real_uplink_ack_io_unbind_and_leave_cleanup(self):
        received = asyncio.Queue()
        connected = asyncio.Queue()

        async def handler(ws):
            initial = json.loads(await ws.recv())
            self.assertEqual(initial["op"], "host")
            await ws.send(json.dumps({"op": "ready"}))
            connected.put_nowait(ws)
            async for message in ws:
                received.put_nowait(message)

        async with serve(handler, "127.0.0.1", 0) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ws"
            self.daemon.relay_bindings.bind(url, self.code, "account-a", self.daemon.token)
            self.daemon.relay_bindings.bind(url, self.code, "account-b", self.daemon.token)
            job = asyncio.create_task(relay_uplink(self.daemon, url, self.code, "fixture"))
            self.jobs.append(job)
            ws = await asyncio.wait_for(connected.get(), 2)

            async def control(op):
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    raw = await asyncio.wait_for(received.get(), deadline - time.monotonic())
                    if isinstance(raw, str):
                        item = json.loads(raw)
                        if item.get("op") == op:
                            return item
                self.fail("Missing host control " + op)

            await ws.send(json.dumps({"op": "join", "id": 1, "account": "unbound", "expires": time.time() + 60}))
            self.assertEqual(await control("joined"), {"op": "joined", "id": 1, "ok": False})
            for phone_id, account in ((1, "account-a"), (2, "account-b")):
                await ws.send(json.dumps({"op": "join", "id": phone_id, "account": account, "expires": time.time() + 60}))
                self.assertTrue((await control("joined"))["ok"])
                await ws.send(struct.pack("<I", phone_id) + pack_json(OP_HELLO, {"multiplex": True}))
                await ws.send(struct.pack("<I", phone_id) + pack_json(OP_SUBSCRIBE, {"sessions": [self.session.id]}))
            await ws.send(struct.pack("<I", 1) + pack(OP_INPUT_SESSION, self.session.id.encode() + b"\0synthetic"))
            deadline = time.monotonic() + 2
            while bytes(self.session.pty.written) != b"synthetic" and time.monotonic() < deadline:
                await asyncio.sleep(.01)
            self.assertEqual(bytes(self.session.pty.written), b"synthetic")
            revoked = self.daemon.hub.clients["relay-fixture-1"]
            RelayBindings(self.path / "relay-bindings.json").bind(url, self.code, "account-a", None)
            closed = await control("disconnect")
            self.assertEqual((closed["id"], closed["code"]), (1, 4001))
            self.assertTrue(revoked.queue.empty())
            self.assertNotIn(revoked.key, self.daemon.hub.clients)
            self.assertIn("relay-fixture-2", self.daemon.hub.clients)
            await ws.send(json.dumps({"op": "leave", "id": 2}))
            deadline = time.monotonic() + 1
            while "relay-fixture-2" in self.daemon.hub.clients and time.monotonic() < deadline:
                await asyncio.sleep(.01)
            self.assertNotIn("relay-fixture-2", self.daemon.hub.clients)
            self.assertFalse(self.session.stop.is_set())
            await ws.send(json.dumps({"op": "join", "id": 3, "account": "account-b", "expires": time.time() + .15}))
            self.assertTrue((await control("joined"))["ok"])
            expired = await control("disconnect")
            self.assertEqual((expired["id"], expired["code"]), (3, 4001))
            self.assertNotIn("relay-fixture-3", self.daemon.hub.clients)
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
            self.assertNotIn(url, self.daemon._relay_generations)


if __name__ == "__main__":
    unittest.main()
