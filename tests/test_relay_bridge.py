import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from supermonoagent.protocol import (OP_HELLO, OP_HELLO_OK, OP_INPUT_SESSION, OP_OUTPUT,
                                     OP_SUBSCRIBE, pack, pack_json, unpack_json)
from supermonoagent.relay import Relay
from supermonoagent.relay_bridge import RelayBridge, socket_url, ensure_running
from supermonoagent.defaults import DEFAULT_RELAY
from supermonoagent.web import text_response
from test_client_integration import isolated_host, receive_op, barrier


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.01)


@asynccontextmanager
async def fixture():
    async with isolated_host() as (daemon, backend, _):
        relay = Relay(development=True)
        async with serve(relay.handler, "127.0.0.1", 0) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ws"
            bridge = RelayBridge(backend, daemon.token, retry_s=.02)
            tasks = [asyncio.create_task(bridge.observe_backend()), asyncio.create_task(bridge.relay(url, "bridge-test"))]
            try:
                await until(lambda: bridge.relay_connected and bridge.backend_connected)
                yield daemon, relay, bridge, url
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


async def join(ws, token="integration-owner"):
    await ws.send(json.dumps({"op": "phone", "code": "bridge-test", "token": token, "reliable": True}))
    assert json.loads(await ws.recv())["op"] == "ready"


class EndpointTest(unittest.TestCase):
    def test_tls_and_loopback_only_without_secret_urls(self):
        self.assertEqual(socket_url("https://117.50.48.22"), "wss://117.50.48.22/ws")
        self.assertEqual(socket_url("http://127.0.0.1:6769", local=True), "ws://127.0.0.1:6769/ws")
        for value in ("ws://117.50.48.22:8817/ws", "http://example.com/ws", "wss://user:secret@example.com/ws",
                      "wss://@example.com/ws", "wss://example.com/ws?token=secret", "wss://example.com/ws#secret",
                      "wss://example.com:bad/ws", "wss://example.com/custom", "ws://[invalid"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                socket_url(value)
        for value in ("https://117.50.48.22/ws", "ws://192.168.1.2:6769/ws"):
            with self.assertRaises(ValueError):
                socket_url(value, local=True)

    def test_owner_credential_required_and_health_contains_no_secrets(self):
        with self.assertRaises(ValueError):
            RelayBridge("http://127.0.0.1:6769", "")
        bridge = RelayBridge("http://127.0.0.1:6769", "secret-owner")
        self.assertNotIn("secret-owner", json.dumps(bridge.health()))
        self.assertFalse(bridge.authorized("different"))
        for body in ({"token": "different"}, {"token": "secret-owner", "account": "other-account"},
                     {"token": "secret-owner", "session": "delegated"}):
            with self.assertRaises(ValueError):
                bridge._validate_phone_frame(pack_json(OP_HELLO, body), True)


class StartupTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        (self.state / "token").write_text("never-argv-owner", encoding="utf8")
        (self.state / "relay-code").write_text("never-argv-room", encoding="utf8")

    def config(self, **changes):
        value = {"enabled": True, "relay": DEFAULT_RELAY, **changes}
        (self.state / "secure-relay.json").write_text(json.dumps(value), encoding="utf8")

    def test_no_opt_in_or_disabled_never_probes_or_spawns(self):
        with patch("supermonoagent.relay_bridge._local_health") as health, patch("subprocess.Popen") as process:
            self.assertIsNone(ensure_running("http://127.0.0.1:6769", self.state))
            self.config(enabled=False)
            self.assertIsNone(ensure_running("http://127.0.0.1:6769", self.state))
            health.assert_not_called()
            process.assert_not_called()

    def test_invalid_targets_or_missing_credentials_never_probes_or_spawns(self):
        with patch("supermonoagent.relay_bridge._local_health") as health, patch("subprocess.Popen") as process:
            for changes in ({"relay": "wss://other-host.example/ws"}, {"relay": "ws://117.50.48.22:8817/ws"},
                            {"backend": "http://192.168.1.2:6769"}, {"backend": "http://127.0.0.1:7777"},
                            {"backend": 123}, {"port": 8888}):
                self.config(**changes)
                self.assertIsNone(ensure_running("http://127.0.0.1:6769", self.state))
            self.config()
            (self.state / "token").unlink()
            self.assertIsNone(ensure_running("http://127.0.0.1:6769", self.state))
            health.assert_not_called()
            process.assert_not_called()

    def test_existing_matching_bridge_returns_health_even_when_relay_offline(self):
        self.config()
        value = {"ok": True, "role": "relay-bridge", "backend": "ws://127.0.0.1:6769/ws",
                 "relay": DEFAULT_RELAY, "relay_connected": False}
        with patch("supermonoagent.relay_bridge._local_health", return_value=(True, value)), patch("subprocess.Popen") as process:
            self.assertEqual(ensure_running("http://127.0.0.1:6769", self.state), value)
            process.assert_not_called()

    def test_foreign_service_is_never_killed_or_replaced(self):
        self.config()
        with patch("supermonoagent.relay_bridge._local_health", return_value=(True, {"role": "other"})), patch("subprocess.Popen") as process:
            with self.assertLogs("supermonoagent.relay_bridge", "WARNING"):
                self.assertIsNone(ensure_running("http://127.0.0.1:6769", self.state))
            process.assert_not_called()

    def test_frozen_start_is_hidden_bounded_and_keeps_tokens_and_code_out_of_argv(self):
        self.config()
        with patch("supermonoagent.relay_bridge._local_health", return_value=(False, None)), \
                patch("subprocess.Popen") as process, patch.object(sys, "frozen", True, create=True), \
                patch("supermonoagent.relay_bridge._STARTED", {}):
            ensure_running("http://127.0.0.1:6769", self.state)
            ensure_running("http://127.0.0.1:6769", self.state)
            process.assert_called_once()
            args, kwargs = process.call_args
            self.assertEqual(args[0][1], "relay-bridge")
            self.assertNotIn("never-argv-owner", " ".join(args[0]))
            self.assertNotIn("never-argv-room", " ".join(args[0]))
            self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            if sys.platform == "win32":
                self.assertTrue(kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW)

    def test_source_start_uses_pythonw_module_and_same_fixed_relay(self):
        self.config()
        python = self.state / "python.exe"
        (self.state / "pythonw.exe").touch()
        with patch("supermonoagent.relay_bridge._local_health", return_value=(False, None)), \
                patch("subprocess.Popen") as process, patch.object(sys, "frozen", False, create=True), \
                patch.object(sys, "executable", str(python)), patch("supermonoagent.relay_bridge._STARTED", {}):
            ensure_running("http://127.0.0.1:6769", self.state)
            command = process.call_args.args[0]
            self.assertEqual(command[1:3], ["-m", "supermonoagent.relay_bridge"])
            if sys.platform == "win32":
                self.assertEqual(Path(command[0]).name, "pythonw.exe")
            self.assertEqual(command[command.index("--relay") + 1], DEFAULT_RELAY)


class BridgeTest(unittest.IsolatedAsyncioTestCase):
    async def test_plaintext_redirect_upgrades_existing_default_client_to_verified_tls(self):
        """An existing connect(wsURL) can upgrade without a new daemon process."""
        fixture = Path(__file__).parent / "fixtures"
        certificate = fixture / "tls-loopback-cert.pem"
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(certificate, fixture / "tls-loopback-key.pem")
        relay = Relay(development=True)
        async with serve(relay.handler, "127.0.0.1", 0, ssl=server_context) as tls:
            target = f"wss://127.0.0.1:{tls.sockets[0].getsockname()[1]}/ws"

            async def redirect(connection, request):
                reply = text_response(connection, 308, "TLS required", "text/plain")
                reply.headers["Location"] = target
                return reply

            async with serve(relay.handler, "127.0.0.1", 0, process_request=redirect) as old:
                source = f"ws://127.0.0.1:{old.sockets[0].getsockname()[1]}/ws"
                # Default trust rejects the fixture certificate after redirect.
                with self.assertRaises(ssl.SSLCertVerificationError):
                    async with connect(source):
                        pass
                original = ssl.create_default_context
                def trusted_default(*args, **kwargs):
                    context = original(*args, **kwargs)
                    context.load_verify_locations(certificate)
                    return context
                # Add this test CA to the default context; verification stays on.
                with patch("ssl.create_default_context", side_effect=trusted_default):
                    async with connect(source) as host:
                        self.assertIsNotNone(host.transport.get_extra_info("ssl_object"))
                        await host.send(json.dumps({"op": "host", "code": "redirect-proof", "tokens": ["fixture-owner"]}))
                        self.assertEqual(json.loads(await host.recv())["op"], "ready")
                    with self.assertRaisesRegex(ValueError, "ssl=None"):
                        async with connect(source, ssl=None):
                            pass

    async def test_real_catalog_input_output_preserve_sessions_without_extra_pty(self):
        async with fixture() as (daemon, _, bridge, url):
            a = daemon.hub.open_window("first", owner=daemon.token)
            b = daemon.hub.open_window("second", owner=daemon.token)
            first = daemon.hub.open_cli(a.id, "cmd")
            second = daemon.hub.open_cli(b.id, "cmd")
            clients_before = len(daemon.hub.clients)
            async with connect(url) as phone:
                await join(phone)
                await phone.send(pack_json(OP_HELLO, {"token": daemon.token, "multiplex": True, "reliable": True}))
                catalog = unpack_json(await receive_op(phone, OP_HELLO_OK))
                self.assertEqual({window["id"] for window in catalog["windows"]}, {a.id, b.id})
                self.assertEqual(len(daemon.hub.sessions), 2)
                await phone.send(pack_json(OP_SUBSCRIBE, {"sessions": [second.id]}))
                await barrier(phone)
                await phone.send(pack(OP_INPUT_SESSION, second.id.encode() + b"\0echo bridge-input\r"))
                await barrier(phone)
                self.assertEqual(first.pty.written, b"")
                self.assertEqual(second.pty.written, b"echo bridge-input\r")
                second.pty.inbox.put(b"unchanged \x1b[31mterminal\x1b[0m output")
                self.assertEqual(await receive_op(phone, OP_OUTPUT), second.id.encode() + b"\0unchanged \x1b[31mterminal\x1b[0m output")
                self.assertEqual(len(bridge.phones), 1)
            await until(lambda: not bridge.phones and len(daemon.hub.clients) == clients_before)
            self.assertEqual(len(daemon.hub.sessions), 2)

    async def test_wrong_token_and_hello_reauthentication_are_rejected(self):
        async with fixture() as (daemon, _, bridge, url):
            async with connect(url) as phone:
                await phone.send(json.dumps({"op": "phone", "code": "bridge-test", "token": "wrong"}))
                with self.assertRaises(ConnectionClosed):
                    await phone.recv()
            async with connect(url) as phone:
                await join(phone)
                await phone.send(pack_json(OP_HELLO, {"token": "wrong", "multiplex": True}))
                with self.assertRaises(ConnectionClosed):
                    await phone.recv()
            await until(lambda: not bridge.phones)
            self.assertEqual(len(daemon.hub.sessions), 0)

    async def test_uplink_disconnect_cleans_routes_and_retries_without_terminal_restart(self):
        async with fixture() as (daemon, relay, bridge, url):
            window = daemon.hub.open_window("keep", owner=daemon.token)
            session = daemon.hub.open_cli(window.id, "cmd")
            async with connect(url) as phone:
                await join(phone)
                await phone.send(pack_json(OP_HELLO, {"token": daemon.token, "multiplex": True}))
                await receive_op(phone, OP_HELLO_OK)
                old = relay.rooms["bridge-test"].host
                await old.close(code=1012)
                with self.assertRaises(ConnectionClosed):
                    await phone.recv()
            await until(lambda: bridge.relay_connected and relay.rooms.get("bridge-test")
                        and relay.rooms["bridge-test"].host is not old)
            self.assertFalse(bridge.phones)
            self.assertIs(daemon.hub.sessions[session.id], session)

    async def test_existing_host_conflict_retries_until_old_host_leaves(self):
        async with isolated_host() as (daemon, backend, _):
            relay = Relay(development=True)
            async with serve(relay.handler, "127.0.0.1", 0) as server:
                url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ws"
                async with connect(url) as old:
                    await old.send(json.dumps({"op": "host", "code": "occupied", "tokens": [daemon.token]}))
                    self.assertEqual(json.loads(await old.recv())["op"], "ready")
                    bridge = RelayBridge(backend, daemon.token, retry_s=.02)
                    task = asyncio.create_task(bridge.relay(url, "occupied"))
                    try:
                        await asyncio.sleep(.1)
                        self.assertFalse(bridge.relay_connected)
                        await old.close()
                        await until(lambda: bridge.relay_connected)
                        self.assertEqual(len(daemon.hub.sessions), 0)
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
