import unittest
import asyncio
import subprocess
import sys
import json
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from supermonoagent.native_attach import connection_url, load_access_token, session_rows, input_frame, split_feed, run_console, attach, parser, disconnect_input
from supermonoagent.protocol import unpack, pack_feed, pack_json


class NativeAttachTest(unittest.TestCase):
    def test_win32_input_ctrl_bracket_disconnect(self):
        self.assertEqual(disconnect_input(b"abc\x1b[221;27;29;1;8;1_"), (b"abc", True))
        self.assertEqual(disconnect_input(b"\x1b[67;46;3;1;8;1_"), (b"\x1b[67;46;3;1;8;1_", False))
    @unittest.skipUnless(sys.platform == "win32", "Win32 console modes")
    def test_real_hidden_console_output_and_mode_restoration(self):
        script = '''
import ctypes, json, struct, sys
from ctypes import wintypes
from pathlib import Path
from supermonoagent.native_attach import WindowsConsole
with WindowsConsole() as console:
    modes = console.saved[:]
    pages = console.pages
    cols, rows = console.size()
    assert cols > 0 and rows > 0
    console.write("\\x1b[32mShared terminal 中文\\x1b[0m\\r\\n".encode())
    # Inject real console key records: processed Ctrl+C must remain data and
    # ReadFile must encode Chinese through the UTF-8 console code page.
    text = "\\x03中\\x1d"
    records = b"".join(struct.pack("<H2xIHHHHI", 1, 1, 1, 0, 0, ord(char), 0) for char in text)
    console.k.WriteConsoleInputW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    written = wintypes.DWORD()
    assert console.k.WriteConsoleInputW(console.input, records, len(text), ctypes.byref(written))
    received = b""
    while len(received) < len(text.encode()):
        received += console.read()
    assert received == text.encode(), repr(received)
for handle, expected in modes:
    actual = wintypes.DWORD()
    assert console.k.GetConsoleMode(handle, ctypes.byref(actual))
    assert actual.value == expected
assert (console.k.GetConsoleCP(), console.k.GetConsoleOutputCP()) == pages
Path(sys.argv[1]).write_text(json.dumps({"cols": cols, "rows": rows}), encoding="utf-8")
'''
        with TemporaryDirectory() as root:
            result = Path(root) / "console.json"
            startup = subprocess.STARTUPINFO()
            startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
            child = subprocess.run([sys.executable, "-c", script, str(result)],
                                   creationflags=subprocess.CREATE_NEW_CONSOLE,
                                   startupinfo=startup, timeout=10)
            self.assertEqual(child.returncode, 0)
            self.assertTrue(result.is_file())

    def test_targeted_input_and_unicode_output(self):
        op, payload = unpack(input_frame("s1", "中文\x03".encode()))
        self.assertEqual(op, 17)
        self.assertEqual(payload, b"s1\0" + "中文\x03".encode())
        self.assertEqual(split_feed(payload), ("s1", "中文\x03".encode()))

    def test_relay_url_and_reject_credentials_in_url(self):
        self.assertEqual(connection_url("https://example.test/base"), "wss://example.test/base")
        with self.assertRaises(ValueError):
            connection_url("ws://example.test/ws?token=secret")

    def test_root_http_address_and_port_validation(self):
        self.assertEqual(connection_url("http://127.0.0.1:6769/"), "ws://127.0.0.1:6769/ws")
        self.assertEqual(connection_url("https://example.test"), "wss://example.test/ws")
        self.assertEqual(connection_url("wss://example.test/proxy/ws"), "wss://example.test/proxy/ws")
        with self.assertRaises(ValueError):
            connection_url("ws://example.test:invalid/ws")

    def test_token_precedence_and_no_creation(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "token"
            with self.assertRaises(ValueError):
                load_access_token(path, {})
            path.write_text("file-token", encoding="utf-8")
            self.assertEqual(load_access_token(path, {}), "file-token")
            self.assertEqual(load_access_token(path, {"SUPERMONO_TOKEN": "env-token"}), "env-token")

    def test_custom_state_token_path_and_explicit_override(self):
        with TemporaryDirectory() as root:
            state = Path(root) / "state"
            state.mkdir()
            (state / "token").write_text("state-token", encoding="utf-8")
            explicit = Path(root) / "other-token"
            explicit.write_text("explicit-token", encoding="utf-8")
            environ = {"SUPERMONO_STATE_DIR": str(state)}
            with patch("supermonoagent.native_attach.Path.home", return_value=Path(root) / "empty-home"):
                self.assertEqual(load_access_token(environ=environ), "state-token")
                self.assertEqual(load_access_token(explicit, environ), "explicit-token")

    def test_session_listing_includes_window(self):
        rows = session_rows({"windows": [{"id": "w1", "title": "work", "sessions": [{"id": "s1", "shell": "cmd"}]}]})
        self.assertEqual(rows[0]["window"], "w1")
        self.assertEqual(rows[0]["id"], "s1")


class FakeSocket:
    def __init__(self):
        self.sent = []
        self.received = asyncio.Queue()

    async def send(self, frame):
        self.sent.append(unpack(frame))

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self.received.get()
        if frame is None:
            raise StopAsyncIteration
        return frame


class Console:
    def __init__(self, data=b"\x03\x1d"):
        self.data = data
        self.written = []

    def read(self):
        data, self.data = self.data, b""
        return data

    def write(self, data):
        self.written.append(data)

    def size(self):
        return 100, 30


class NativeConsoleTest(unittest.IsolatedAsyncioTestCase):
    async def test_relay_first_phone_hello_requests_reliable_delivery(self):
        from websockets.asyncio.server import serve
        received = []

        async def relay(socket):
            received.append(json.loads(await socket.recv()))
            op, payload = unpack(await socket.recv())
            self.assertEqual(op, 1)
            self.assertTrue(json.loads(payload)["reliable"])
            await socket.send(pack_json(2, {"windows": [], "capabilities": {"multiplex": True}}))

        async with serve(relay, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            args = parser().parse_args(["--url", f"ws://127.0.0.1:{port}/ws", "--relay-code", "test-room", "--list"])
            with patch.dict("os.environ", {"SUPERMONO_TOKEN": "test-owner"}), patch("builtins.print"):
                await asyncio.wait_for(attach(args), 3)
        self.assertEqual(received[0]["op"], "phone")
        self.assertIs(received[0].get("reliable"), True)

    async def test_split_win32_disconnect_and_ctrl_c_record(self):
        ws, console = FakeSocket(), Console()
        ctrl_c = b"\x1b[67;46;3;1;8;1_"
        chunks = iter([ctrl_c + b"\x1b[221;", b"27;29;1;8;1_"])
        console.read = lambda: next(chunks, b"")
        await asyncio.wait_for(run_console(ws, console, "s1"), 2)
        inputs = [payload for op, payload in ws.sent if op == 17]
        self.assertEqual(inputs, [b"s1\0" + ctrl_c])

    async def test_real_websocket_daemon_keeps_session_after_native_disconnect(self):
        from websockets.asyncio.server import serve
        from supermonoagent.daemon import Daemon
        from supermonoagent.hub import Hub
        from test_windows import FakePty
        with TemporaryDirectory() as root, patch("supermonoagent.daemon.STATE", Path(root)), patch.dict(
            "os.environ", {"SUPERMONO_STATE_DIR": root, "SUPERMONO_TOKEN": "owner"}
        ):
            daemon = Daemon("owner")
            daemon.hub = Hub(pty_factory=lambda *_: FakePty())
            daemon.hub.attach_loop(asyncio.get_running_loop())
            daemon.hub.ensure_owner("owner")
            window = daemon.hub.open_window(owner="owner")
            session = daemon.hub.open_cli(window.id, "cmd")
            try:
                async with serve(daemon.handle_socket, "127.0.0.1", 0) as server:
                    port = server.sockets[0].getsockname()[1]
                    args = parser().parse_args(["--url", f"ws://127.0.0.1:{port}/ws", "--session", session.id])
                    with patch("supermonoagent.native_attach.WindowsConsole") as console_type:
                        console_type.return_value.__enter__.return_value = Console()
                        await asyncio.wait_for(attach(args), 3)
                self.assertEqual(bytes(session.pty.written), b"\x03")
                self.assertIn(session.id, daemon.hub.sessions)
                self.assertFalse(session.stop.is_set())
            finally:
                daemon.hub.close_window(window.id)

    async def test_disconnect_keeps_shell_and_ctrl_c_is_forwarded(self):
        ws = FakeSocket()
        await asyncio.wait_for(run_console(ws, Console(), "s1"), 2)
        self.assertIn((17, b"s1\0\x03"), ws.sent)
        self.assertNotIn(9, [op for op, _ in ws.sent])

    async def test_only_subscribed_output_and_one_unchanged_resize(self):
        ws, console = FakeSocket(), Console(b"")
        await ws.received.put(pack_feed(8, "other", b"wrong"))
        await ws.received.put(pack_feed(3, "s1", "中文".encode()))
        task = asyncio.create_task(run_console(ws, console, "s1", True))
        await asyncio.sleep(0.23)
        await ws.received.put(None)
        await asyncio.wait_for(task, 2)
        self.assertEqual(console.written, ["中文".encode()])
        sizes = [payload for op, payload in ws.sent if op == 5]
        self.assertEqual(len(sizes), 1)
        self.assertIn(b'"take_control":true', sizes[0])
        self.assertNotIn(9, [op for op, _ in ws.sent])
