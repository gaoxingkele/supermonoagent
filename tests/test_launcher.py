import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from supermonoagent import launcher


class LauncherTest(unittest.TestCase):
    def test_mode_dispatch_preserves_arguments_and_restores_argv(self):
        original = sys.argv
        seen = []
        module = SimpleNamespace(main=lambda: seen.append(sys.argv[1:]))
        with patch.object(launcher.importlib, "import_module", return_value=module) as load:
            launcher.main(["host", "--port", "9123"])
        load.assert_called_once_with("supermonoagent.daemon")
        self.assertEqual(seen, [["--port", "9123"]])
        self.assertIs(sys.argv, original)

    def test_browser_and_workspace_dispatch(self):
        module = SimpleNamespace(main=Mock())
        with patch.object(launcher.importlib, "import_module", return_value=module):
            launcher.main(["browser", "--no-start"])
            module.main.assert_called_with(["--no-start"], browser=True)
            launcher.main(["workspace", "--renderer", "webgl"])
            module.main.assert_called_with(["--renderer", "webgl"])

    def test_invalid_mode_rejected(self):
        with self.assertRaises(SystemExit) as context:
            launcher.main(["unknown"])
        self.assertEqual(context.exception.code, 2)

    def test_microsoft_diagnostics_never_start_daemon(self):
        module = SimpleNamespace(main=Mock())
        with patch.object(launcher.importlib, "import_module", return_value=module) as load:
            launcher.main(["microsoft", "--diagnose"])
        load.assert_called_once_with("supermonoagent.microsoft")

    def test_microsoft_default_ensures_shared_host(self):
        module = SimpleNamespace(main=Mock())
        desktop = SimpleNamespace(argument_parser=Mock(), connection_url=Mock())
        with patch.dict(launcher.os.environ, {}, clear=True), patch.object(launcher.importlib, "import_module", side_effect=[module, desktop]):
            launcher.main(["microsoft", "--shell", "cmd"])
        desktop.connection_url.assert_called_once()
