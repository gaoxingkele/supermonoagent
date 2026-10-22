import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from supermonoagent import desktop


class DesktopTest(unittest.TestCase):
    def test_frozen_host_reenters_dispatcher(self):
        with patch.object(desktop.sys, "frozen", True, create=True), patch.object(desktop.sys, "executable", "C:/app/SuperMonoAgent.exe"):
            self.assertEqual(desktop.host_command("127.0.0.1", 9123), ["C:/app/SuperMonoAgent.exe", "host", "--host", "127.0.0.1", "--port", "9123"])

    def test_source_host_uses_daemon_module(self):
        with patch.object(desktop.sys, "frozen", False, create=True):
            self.assertEqual(desktop.host_command("127.0.0.1", 9123)[1:3], ["-m", "supermonoagent"])

    def test_workspace_and_legacy_urls(self):
        url = desktop.page_url("https://relay.example/", renderer="webgl", token="a&b")
        self.assertEqual(urlsplit(url).path, "/workspace")
        self.assertEqual(parse_qs(urlsplit(url).query), {"renderer": ["webgl"], "shell": ["desktop"]})
        self.assertEqual(parse_qs(urlsplit(url).fragment), {"token": ["a&b"]})
        self.assertEqual(urlsplit(desktop.page_url("http://localhost:6769", legacy=True)).path, "/")

    def test_remote_query_preserved_and_unsafe_urls_rejected(self):
        self.assertEqual(parse_qs(urlsplit(desktop.page_url("https://relay.example/workspace?code=abc")).query)["code"], ["abc"])
        for url in ["file:///tmp/x", "javascript:alert(1)", "https://a:b@host", "http://host\\@other", "http://host:99999", "http://host\n/"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                desktop.page_url(url)

    def test_remote_does_not_start_local_host_or_fetch_local_token(self):
        with patch.object(desktop, "start_host") as start, patch.object(desktop, "health") as health:
            args = desktop.argument_parser().parse_args(["--url", "https://relay.example", "--renderer", "dom"])
            self.assertEqual(urlsplit(desktop.connection_url(args)).netloc, "relay.example")
            start.assert_not_called()
            health.assert_not_called()

    def test_browser_workspace_option_keeps_the_shared_workspace_ui(self):
        args = desktop.argument_parser().parse_args(["--url", "https://relay.example", "--workspace"])
        self.assertEqual(urlsplit(desktop.connection_url(args, browser=True)).path, "/workspace")
        self.assertEqual(urlsplit(desktop.page_url("https://relay.example", browser=True)).path, "/")

    def test_local_token_file_bootstrap_without_http_token_exposure(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as directory:
            (Path(directory) / "token").write_text("file-token", encoding="utf-8")
            args = desktop.argument_parser().parse_args(["--state-dir", directory, "--port", "9912"])
            with patch.object(desktop, "port_open", return_value=True), patch.object(desktop, "health", return_value={"ok": True, "role": "daemon"}):
                url = desktop.connection_url(args)
            self.assertNotIn("token", parse_qs(urlsplit(url).query))
            self.assertEqual(parse_qs(urlsplit(url).fragment), {"token": ["file-token"]})

    def test_webview_explicitly_uses_edgechromium(self):
        from types import SimpleNamespace
        import sys
        from unittest.mock import Mock
        webview = SimpleNamespace(create_window=Mock(), start=Mock())
        with patch.dict(sys.modules, {"webview": webview}), patch.object(desktop, "connection_url", return_value="http://localhost:6769/workspace"), patch("supermonoagent.frontend.local_page_url", return_value="http://127.0.0.1:6771/workspace"), patch.object(desktop, "NativeUpdateRecovery") as recovery, patch.object(desktop.sys, "platform", "win32"):
            desktop.main([])
        from pathlib import Path
        icon = Path(desktop.__file__).parent / "static" / "app-icon.ico"
        self.assertTrue(icon.is_file())
        webview.start.assert_called_once_with(webview.create_window.return_value.show, gui="edgechromium", icon=str(icon))
        url = webview.create_window.call_args.args[1]
        self.assertEqual(urlsplit(url)._replace(query="").geturl(), "http://127.0.0.1:6771/workspace")
        self.assertEqual(len(parse_qs(urlsplit(url).query)["native_window"][0]), 32)
        recovery.assert_called_once_with(webview.create_window.return_value, url)


if __name__ == "__main__":
    unittest.main()
