"""Isolated resize leases and Windows console input; never attach user sessions."""

import asyncio
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from supermonoagent.hub import Hub
from supermonoagent.protocol import OP_SESSIONS, unpack
from supermonoagent.pty import WinPty
from test_windows import FakePty


class SizedPty(FakePty):
    def __init__(self):
        super().__init__()
        self.sizes = []

    def resize(self, cols, rows):
        self.sizes.append((cols, rows))


class ResizeBoundsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = Hub(pty_factory=lambda *_: SizedPty())
        self.hub.attach_loop(asyncio.get_running_loop())
        self.window = self.hub.open_window()
        self.session = self.hub.open_cli(self.window.id, "fake")
        self.a = self.hub.attach(asyncio.Queue(100), key="a")
        self.b = self.hub.attach(asyncio.Queue(100), key="b")
        for client in (self.a, self.b):
            client.multiplex = True
            self.hub.set_subscriptions(client, [self.session.id])
            self.hub.select(client, self.window.id)
            self.drain(client)

    async def asyncTearDown(self):
        self.hub.close_window(self.window.id)
        await asyncio.sleep(0.01)

    def drain(self, client):
        frames = []
        while not client.queue.empty():
            frames.append(client.queue.get_nowait())
        return frames

    async def test_both_resize_routes_accept_identical_narrow_and_large_sizes(self):
        for wanted, effective in [((8, 3), (8, 3)), ((19, 4), (19, 4)),
                                  ((20, 5), (20, 5)), ((21, 6), (21, 6)),
                                  ((-1, 0), (2, 1)), ((900, 900), (500, 200))]:
            self.hub.resize(self.a, *wanted)
            self.assertEqual((self.session.cols, self.session.rows), effective)
            count = len(self.session.pty.sizes)
            self.assertTrue(self.hub.resize_session(self.a, self.session.id, *wanted))
            self.assertEqual(len(self.session.pty.sizes), count)
            self.assertEqual(self.hub.controls(self.a)[self.session.id]["cols"], effective[0])

    async def test_observer_learns_size_owner_and_release_without_stealing(self):
        sid = self.session.id
        self.assertTrue(self.hub.resize_session(self.a, sid, 8, 3))
        catalogs = [json.loads(data) for op, data in map(unpack, self.drain(self.b)) if op == OP_SESSIONS]
        self.assertEqual(catalogs[-1]["controls"][sid], {"cols": 8, "rows": 3, "owner": False, "busy": True})
        self.assertFalse(self.hub.resize_session(self.b, sid, 70, 20))
        self.hub.resize(self.b, 70, 20)
        self.assertEqual(self.session.pty.sizes, [(8, 3)])
        self.drain(self.a)
        self.assertTrue(self.hub.resize_session(self.a, sid, 8, 3))
        self.assertEqual(self.drain(self.a), [])
        self.hub.set_subscriptions(self.a, [])
        catalogs = [json.loads(data) for op, data in map(unpack, self.drain(self.b)) if op == OP_SESSIONS]
        self.assertFalse(catalogs[-1]["controls"][sid]["busy"])
        self.assertTrue(self.hub.resize_session(self.b, sid, 8, 3))
        self.assertEqual(self.session.pty.sizes, [(8, 3)])

    async def test_explicit_takeover_same_size_changes_owner_without_pty_resize(self):
        sid = self.session.id
        self.hub.resize_session(self.a, sid, 30, 10)
        self.assertTrue(self.hub.resize_session(self.b, sid, 30, 10, take_control=True))
        self.assertEqual(self.session.pty.sizes, [(30, 10)])
        self.assertTrue(self.hub.controls(self.a)[sid]["busy"])
        self.hub.detach(self.b.key)
        self.assertFalse(self.hub.controls(self.a)[sid]["busy"])

    async def test_hub_forwards_exact_fragmented_input_to_pty_adapter(self):
        data = "\x1b[200~中文\r\nsecond\x1b[201~\x1b[A\x03".encode()
        for byte in data:
            self.assertTrue(self.hub.write_session(self.a, self.session.id, bytes([byte])))
        self.assertEqual(bytes(self.session.pty.written), data)


@unittest.skipUnless(sys.platform == "win32", "requires real Windows ConPTY")
class ConptyCaptureTest(unittest.TestCase):
    def test_multiline_unicode_paste_arrives_without_wrappers_or_submission(self):
        # ReadConsole's raw characters are captured in a new temporary process.
        # No PowerShell, Kimi, Codex or daemon owned by the user is addressed.
        child = '''import ctypes,json,msvcrt,sys,time
from pathlib import Path
k=ctypes.windll.kernel32
k.SetConsoleMode(k.GetStdHandle(-10), 0)
p=Path(sys.argv[1]); (p/'ready').touch()
chars=[]; deadline=time.monotonic()+5
while time.monotonic()<deadline:
 if msvcrt.kbhit():
  chars.append(msvcrt.getwch())
  if len(chars)>=int(sys.argv[2]): break
 else: time.sleep(.002)
(p/'capture.json').write_text(json.dumps(''.join(chars)), encoding='utf8')
'''
        with tempfile.TemporaryDirectory(prefix="supermono-console-") as directory:
            path = Path(directory)
            script = path / "capture.py"
            script.write_text(child, encoding="utf8")
            body = "first中文\nsecond_line_0123456789"
            pty = WinPty([sys.executable, str(script), str(path), str(len(body))], directory)

            def drain():
                try:
                    while True:
                        output = pty.read()
                        # ConPTY waits for the frontend's device attributes
                        # before allowing startup; emulate that frontend here.
                        if b"\x1b[c" in output:
                            pty.write(b"\x1b[?1;2c")
                except Exception:
                    pass

            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            try:
                deadline = time.monotonic() + 8
                while not (path / "ready").exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue((path / "ready").exists(), "isolated console did not start")
                for byte in ("\x1b[200~" + body.replace("\n", "\r\n") + "\x1b[201~").encode():
                    pty.write(bytes([byte]))
                while not (path / "capture.json").exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(json.loads((path / "capture.json").read_text(encoding="utf8")), body)
            finally:
                pty.close()
                reader.join(timeout=1)


if __name__ == "__main__":
    unittest.main()
