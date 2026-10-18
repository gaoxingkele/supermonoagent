import asyncio
import unittest

from supermonoagent.pty import WinPty, shell_command, default_cwd


class PtyTest(unittest.TestCase):
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
