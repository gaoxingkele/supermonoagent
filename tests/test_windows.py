import asyncio
import queue
import unittest

from supermonoagent.hub import Hub
from supermonoagent.protocol import OP_OUTPUT, OP_SNAPSHOT, unpack
from supermonoagent.pty import PtyClosed


class FakePty:
    def __init__(self):
        self.written = bytearray()
        self.inbox = queue.Queue()

    def read(self, n=4096):
        item = self.inbox.get()
        if item is None:
            raise PtyClosed()
        return item

    def write(self, data):
        self.written.extend(data)

    def resize(self, cols, rows):
        pass

    def close(self):
        self.inbox.put(None)

    def emit(self, data: bytes):
        self.inbox.put(data)


class WindowForwardTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ptys = []

        def factory(shell, cwd):
            pty = FakePty()
            self.ptys.append(pty)
            return pty

        self.hub = Hub(pty_factory=factory)
        self.hub.attach_loop(asyncio.get_running_loop())

    async def asyncTearDown(self):
        for window_id in list(self.hub.windows):
            self.hub.close_window(window_id)

    async def test_only_selected_window_is_forwarded(self):
        first = self.hub.open_window("一")
        second = self.hub.open_window("二")
        left = self.hub.open_cli(first.id, "powershell")
        right = self.hub.open_cli(second.id, "cmd")
        queue_a = asyncio.Queue()
        queue_b = asyncio.Queue()
        client_a = self.hub.attach(queue_a)
        client_b = self.hub.attach(queue_b)
        self.hub.select(client_a, first.id)
        self.hub.select(client_b, second.id)
        await asyncio.sleep(0.05)
        left.pty.emit(b"from-a")
        right.pty.emit(b"from-b")
        got_a = await self._collect(queue_a)
        got_b = await self._collect(queue_b)
        self.assertIn(b"from-a", got_a)
        self.assertNotIn(left.id.encode(), got_a)
        self.assertNotIn(b"from-b", got_a)
        self.assertIn(b"from-b", got_b)
        self.assertNotIn(right.id.encode(), got_b)
        self.assertNotIn(b"from-a", got_b)
        self.hub.write(client_a, b"hi")
        self.assertEqual(left.pty.written, b"hi")
        self.assertEqual(right.pty.written, b"")

    async def test_relay_receives_every_open_window(self):
        first = self.hub.open_window("一")
        second = self.hub.open_window("二")
        left = self.hub.open_cli(first.id, "powershell")
        right = self.hub.open_cli(second.id, "cmd")
        queue_r = asyncio.Queue()
        client_r = self.hub.attach(queue_r, key="relay-1")
        self.hub.select(client_r, first.id)
        await self._drain(queue_r)
        left.pty.emit(b"from-a")
        right.pty.emit(b"from-b")
        got = await self._collect(queue_r)
        self.assertIn(left.id.encode() + b"\0from-a", got)
        self.assertIn(right.id.encode() + b"\0from-b", got)
        self.hub.write(client_r, b"only-a")
        self.assertEqual(bytes(left.pty.written), b"only-a")
        self.assertEqual(bytes(right.pty.written), b"")

    async def test_window_stays_on_its_computer_inside_a_project(self):
        office = self.hub
        office.set_identity("OFFICE", "施工")
        window = office.open_window("一")
        left = office.open_cli(window.id, "powershell")
        office.set_identity("OFFICE", "施工")
        self.assertEqual(office.project, "施工")
        self.assertIn(window.id, office.windows)

        field_ptys = []

        def factory(shell, cwd):
            pty = FakePty()
            field_ptys.append(pty)
            return pty

        field = Hub(pty_factory=factory)
        field.attach_loop(asyncio.get_running_loop())
        field.set_identity("FIELD", "施工")
        other = field.open_window("二")
        right = field.open_cli(other.id, "cmd")
        self.assertNotIn(window.id, field.windows)
        self.assertNotIn(other.id, office.windows)
        queue_office = asyncio.Queue()
        client = office.attach(queue_office, key="relay-office")
        office.select(client, window.id)
        await self._drain(queue_office)
        left.pty.emit(b"from-office")
        right.pty.emit(b"from-field")
        got = await self._collect(queue_office)
        self.assertIn(b"from-office", got)
        self.assertNotIn(b"from-field", got)
        field.close_window(other.id)

    async def test_other_token_cannot_see_windows(self):
        self.hub.default_owner = "owner"
        self.hub.groups["default"].owner = "owner"
        own = self.hub.open_window("自己的", owner="owner")
        guest_group = self.hub.ensure_owner("guest")
        guest_win = self.hub.open_window("别人的", guest_group.id, "guest")
        left = self.hub.open_cli(own.id, "powershell")
        right = self.hub.open_cli(guest_win.id, "cmd")
        queue_guest = asyncio.Queue()
        guest = self.hub.attach(queue_guest, key="relay-2", token="guest")
        self.hub.select(guest, own.id)
        self.assertIsNone(guest.window_id)
        self.hub.select(guest, guest_win.id)
        self.hub.subscribe(guest, "default")
        self.assertEqual(guest.channel, "")
        await self._drain(queue_guest)
        left.pty.emit(b"secret-owner")
        right.pty.emit(b"guest-ok")
        got = await self._collect(queue_guest)
        self.assertNotIn(b"secret-owner", got)
        self.assertIn(b"guest-ok", got)
        self.assertEqual([item["id"] for item in self.hub.summary("guest")["windows"]], [guest_win.id])
        self.assertEqual([item["id"] for item in self.hub.summary("owner")["windows"]], [own.id])

    def test_window_titles_keep_increasing(self):
        first = self.hub.open_window()
        second = self.hub.open_window()
        self.hub.close_window(first.id)
        third = self.hub.open_window()
        self.assertEqual(first.title, "窗口 1")
        self.assertEqual(second.title, "窗口 2")
        self.assertEqual(third.title, "窗口 3")
        self.hub.rename_window(third.id, "手工标题")
        self.assertEqual(self.hub.windows[third.id].title, "手工标题")

    def test_short_title_uses_shell_and_latest_command(self):
        from supermonoagent.hub import short_title

        raw = "Microsoft Windows\r\nC:\\Users\\iamaf> dir\r\nC:\\Users\\iamaf>\r\n".encode()
        self.assertEqual(short_title("cmd", raw), "cmd · dir")

    def test_custom_command_keeps_args(self):
        from supermonoagent.pty import shell_command

        argv = shell_command("cmd.exe /K echo hi")
        self.assertTrue(argv[0].lower().endswith("cmd.exe"))
        self.assertEqual(argv[1:], ["/K", "echo", "hi"])

    async def test_two_clients_keep_different_clis_in_one_window(self):
        window = self.hub.open_window("同窗")
        left = self.hub.open_cli(window.id, "powershell")
        right = self.hub.open_cli(window.id, "cmd")
        queue_a = asyncio.Queue()
        queue_b = asyncio.Queue()
        client_a = self.hub.attach(queue_a)
        client_b = self.hub.attach(queue_b)
        self.hub.select(client_a, window.id)
        self.hub.select(client_b, window.id)
        self.hub.focus(client_a, left.id)
        self.assertEqual(client_a.session_id, left.id)
        self.assertEqual(client_b.session_id, right.id)
        self.assertEqual(window.focused, right.id)
        await self._drain(queue_a)
        await self._drain(queue_b)
        left.pty.emit(b"AAA")
        right.pty.emit(b"BBB")
        got_a = await self._collect(queue_a)
        got_b = await self._collect(queue_b)
        self.assertIn(b"AAA", got_a)
        self.assertNotIn(b"BBB", got_a)
        self.assertIn(b"BBB", got_b)
        self.assertNotIn(b"AAA", got_b)
        self.hub.write(client_a, b"aa")
        self.hub.write(client_b, b"bb")
        self.assertEqual(bytes(left.pty.written), b"aa")
        self.assertEqual(bytes(right.pty.written), b"bb")
        self.hub.close_session(left.id)
        self.assertEqual(client_a.session_id, right.id)
        self.assertEqual(client_b.session_id, right.id)
        self.hub.close_window(window.id)
        self.assertIsNone(client_a.window_id)
        self.assertIsNone(client_b.session_id)

    async def _drain(self, items: asyncio.Queue) -> None:
        while not items.empty():
            items.get_nowait()

    async def _collect(self, items: asyncio.Queue) -> bytes:
        blob = b""
        deadline = asyncio.get_running_loop().time() + 0.4
        while asyncio.get_running_loop().time() < deadline:
            try:
                frame = await asyncio.wait_for(items.get(), 0.1)
            except asyncio.TimeoutError:
                continue
            op, payload = unpack(frame)
            if op in {OP_OUTPUT, OP_SNAPSHOT}:
                blob += payload
        return blob


if __name__ == "__main__":
    unittest.main()
