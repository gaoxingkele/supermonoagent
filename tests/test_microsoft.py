import unittest
from unittest.mock import patch

from supermonoagent.microsoft import find_terminal, launch_command


class MicrosoftTest(unittest.TestCase):
    def test_no_silent_fallback(self):
        with patch("supermonoagent.microsoft.shutil.which", return_value=None):
            self.assertIsNone(find_terminal(environ={}))

    def test_command_targets_shared_session_without_secrets(self):
        args = launch_command("C:/apps/wtai.exe", ["--session", "s1"], python="C:/python.exe")
        self.assertEqual(args[:5], ["C:/apps/wtai.exe", "-w", "SuperMonoAgent", "new-tab", "--title"])
        self.assertIn("supermonoagent.native_attach", args)
        self.assertEqual(args[-2:], ["--session", "s1"])
        self.assertNotIn("--token", args)

    def test_reject_secret_and_launcher_injection(self):
        with self.assertRaises(ValueError):
            launch_command("wtai.exe", ["--token", "secret"])
        with self.assertRaises(ValueError):
            launch_command("wtai.exe", ["--session", "s1;split-pane"])

    def test_frozen_launcher_dispatches_native_adapter(self):
        with patch("supermonoagent.microsoft.sys.frozen", True, create=True):
            args = launch_command("wtai.exe", ["--session", "s1"], python="C:/SuperMonoAgent.exe")
        self.assertIn("native-attach", args)
        self.assertNotIn("-m", args)
        self.assertNotIn("supermonoagent.native_attach", args)
