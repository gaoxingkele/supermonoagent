import unittest
from types import SimpleNamespace

from supermonoagent.daemon import Daemon
from supermonoagent.relay import Relay


class Connection:
    remote_address = ("127.0.0.1", 1)

    def respond(self, status, text):
        return SimpleNamespace(status_code=status, body=text, headers={"Content-Type": "text/plain"})


class RoutesTest(unittest.IsolatedAsyncioTestCase):
    async def test_both_servers_offer_legacy_and_workspace(self):
        host = Daemon.__new__(Daemon)
        for serve in (host.serve_http, Relay().serve_page):
            classic = await serve(Connection(), SimpleNamespace(path="/legacy"))
            self.assertEqual(classic.status_code, 200)
            self.assertIn('id="grid"', classic.body)
            upgraded = await serve(Connection(), SimpleNamespace(path="/workspace?renderer=dom"))
            self.assertEqual(upgraded.status_code, 200)
            self.assertIn('id="stage"', upgraded.body)
            for asset in ("/workspace.js", "/workspace.css", "/terminal-client.js"):
                response = await serve(Connection(), SimpleNamespace(path=asset))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.body)
