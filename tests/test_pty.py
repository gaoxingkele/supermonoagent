import asyncio
import threading
import unittest
from unittest.mock import patch

from supermonoagent.pty import ConptyInputFilter, WinPty, shell_command, default_cwd


class PtyTest(unittest.TestCase):
    def test_conpty_bracketed_paste_sends_only_content(self):
        filter = ConptyInputFilter()
        self.assertEqual(filter.feed(b"\x1b[200~first line\r\nsecond \xc3"), b"")
        self.assertEqual(filter.feed(b"\xa9 line\x1b[201~"), b"first line\nsecond \xc3\xa9 line")
        self.assertEqual(filter.feed(b"next"), b"next")

    def test_conpty_bracketed_paste_marker_can_cross_writes(self):
        filter = ConptyInputFilter()
        self.assertEqual(filter.feed("abc\x1b[20".encode()), b"abc")
        self.assertEqual(filter.feed("0~中文\x1b[20".encode()), b"")
        self.assertEqual(filter.feed(b"1~done"), "中文done".encode())

    def test_conpty_filter_preserves_escape_and_other_csi(self):
        filter = ConptyInputFilter()
        self.assertEqual(filter.feed(b"\x1b"), b"")
        self.assertEqual(filter.flush_escape(), b"\x1b")
        self.assertEqual(filter.feed(b"\x1b[A\x03"), b"\x1b[A\x03")
        self.assertEqual(filter.feed(b"\x1b[201~"), b"")

    def test_short_pasted_cr_is_newline_but_explicit_enter_is_unchanged(self):
        filter = ConptyInputFilter()
        self.assertEqual(filter.feed(b"\x1b[200~a\r\nb\rc\x1b[201~"), b"a\nb\nc")
        self.assertEqual(filter.feed(b"\r"), b"\r")

    def test_split_utf8_and_envelope_emit_one_complete_paste_without_enter(self):
        with patch("winpty.PtyProcess.spawn") as spawn:
            pty = WinPty(["unused"], default_cwd())
            try:
                for byte in "\x1b[200~第一行\nsecond\x1b[201~".encode():
                    pty.write(bytes([byte]))
                spawn.return_value.write.assert_called_once_with("第一行\nsecond")
                spawn.return_value.write.reset_mock()
                for byte in "中".encode():
                    pty.write(bytes([byte]))
                pty.write(b"\x1b[A\x03\x7f")
                self.assertEqual([call.args[0] for call in spawn.return_value.write.call_args_list],
                                 ["中", "\x1b[A\x03\x7f"])
            finally:
                pty.close()

    def test_isolated_escape_key_reaches_application_after_grace_period(self):
        with patch("winpty.PtyProcess.spawn") as spawn:
            arrived = threading.Event()
            spawn.return_value.write.side_effect = lambda _: arrived.set()
            pty = WinPty(["unused"], default_cwd())
            try:
                pty.write(b"\x1b")
                self.assertTrue(arrived.wait(1), "Escape key remained buffered")
                spawn.return_value.write.assert_called_once_with("\x1b")
            finally:
                pty.close()

    def test_powershell_echo(self):
        pty = WinPty(shell_command("powershell"), default_cwd(), cols=100, rows=24)
        try:
            pty.write(b"Write-Output SUPERMONO_OK\r")
            text = ""
            async def collect():
                nonlocal text
                loop = asyncio.get_running_loop()
                while "SUPERMONO_OK" not in text:
                    chunk = await loop.run_in_executor(None, pty.read, 4096)
                    text += chunk.decode("utf-8", errors="replace")
            asyncio.run(asyncio.wait_for(collect(), 15))
            self.assertIn("SUPERMONO_OK", text)
        finally:
            pty.close()


if __name__ == "__main__":
    unittest.main()
