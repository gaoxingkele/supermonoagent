"""Default relay suggestions stay editable and never replace local connections."""

import asyncio
from html.parser import HTMLParser
import importlib
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from supermonoagent.defaults import DEFAULT_RELAY, default_relay
from supermonoagent import workspace
from test_client_integration import isolated_host


class Fields(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.elements = {}
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.elements[attrs["id"]] = (tag, attrs)


def test_default_relay_is_centralized_and_environment_overridable(monkeypatch):
    monkeypatch.delenv("SUPERMONO_DEFAULT_RELAY", raising=False)
    assert default_relay() == DEFAULT_RELAY == "ws://117.50.48.22:8817/ws"
    monkeypatch.setenv("SUPERMONO_DEFAULT_RELAY", "  wss://relay.example/ws  ")
    assert default_relay() == "wss://relay.example/ws"
    monkeypatch.setenv("SUPERMONO_DEFAULT_RELAY", "   ")
    assert default_relay() == DEFAULT_RELAY


def test_both_editable_inputs_have_real_safely_injected_options(monkeypatch):
    value = 'wss://relay.example/ws?x="/><script>alert(1)</script>'
    try:
        with monkeypatch.context() as settings:
            settings.setenv("SUPERMONO_DEFAULT_RELAY", value)
            page = importlib.reload(workspace).PAGE
            fields = Fields(page).elements
            assert fields["default-relay"][1]["value"] == value
            assert fields["default-relay"][0] == "option"
            assert fields["relay-options"][0] == "datalist"
            for field in ("relay-url", "workspace-relay"):
                assert fields[field][0] == "input"
                assert fields[field][1]["list"] == "relay-options"
                assert "readonly" not in fields[field][1]
            assert '<script>alert(1)</script>' not in page
            assert "&lt;script&gt;" in page
    finally:
        importlib.reload(workspace)


@pytest.mark.parametrize("relay", [DEFAULT_RELAY, "wss://custom-default.example/ws"])
def test_default_selection_and_manual_entry_do_not_connect_or_send_tokens(monkeypatch, relay):
    playwright = pytest.importorskip("playwright.async_api")
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    if not edge.exists():
        pytest.skip("Local Edge is required")
    try:
        with monkeypatch.context() as settings:
            settings.setenv("SUPERMONO_DEFAULT_RELAY", relay)
            importlib.reload(workspace)
            asyncio.run(_check_dialogs(playwright, edge, relay))
    finally:
        importlib.reload(workspace)


async def _check_dialogs(playwright, edge, relay):
    async with isolated_host() as (daemon, _direct, _test_relay):
        window = daemon.hub.open_window("Local terminal", owner=daemon.token)
        daemon.hub.open_cli(window.id, "cmd")
        async with serve(daemon.handle_socket, "127.0.0.1", 0, process_request=daemon.serve_http) as server:
            async with playwright.async_playwright() as runtime:
                browser = await runtime.chromium.launch(executable_path=str(edge), headless=True)
                try:
                    page = await browser.new_page()
                    socket_urls, errors = [], []
                    page.on("websocket", lambda socket: socket_urls.append(socket.url))
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    origin = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                    await page.goto(f"{origin}/workspace?direct=1&renderer=dom#token={daemon.token}")
                    await page.wait_for_function("window.smaWorkspace?.connected && smaWorkspace.terminal")
                    sockets_before = list(socket_urls)
                    generation = await page.evaluate("smaWorkspace.activeHost.generation")
                    await page.locator("#connect-button").click()
                    assert await page.locator("#relay-url").input_value() == ""
                    assert await page.locator("#relay-url").evaluate("input=>input.list.options[0].value") == relay
                    await page.locator("#relay-url").evaluate("""input=>{
                        input.value=input.list.options[0].value;
                        input.dispatchEvent(new Event('input',{bubbles:true}));
                    }""")
                    assert await page.locator("#relay-url").input_value() == relay
                    assert await page.locator("#access-token").input_value() == ""
                    await page.locator("#relay-url").fill("wss://hand-entered.example/ws")
                    assert await page.locator("#relay-url").input_value() == "wss://hand-entered.example/ws"
                    await page.locator("#connection-close").click()
                    await page.locator("#new-project").click()
                    assert await page.locator("#workspace-relay").input_value() == relay
                    assert await page.locator("#workspace-token").input_value() == ""
                    await page.locator("#workspace-relay").fill("wss://another.example/ws")
                    assert await page.locator("#workspace-relay").input_value() == "wss://another.example/ws"
                    await page.locator("#workspace-cancel").click()
                    assert socket_urls == sockets_before
                    assert await page.evaluate("smaWorkspace.activeHost.config.relay") == ""
                    assert await page.evaluate("smaWorkspace.activeHost.generation") == generation
                    assert await page.evaluate("smaWorkspace.connected")
                    assert not errors, errors
                finally:
                    await browser.close()
