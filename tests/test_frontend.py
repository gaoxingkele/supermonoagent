"""The current UI attaches to existing PTYs through a loopback-only facade."""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from supermonoagent.frontend import Frontend, local_page_url
from supermonoagent.protocol import CAPABILITIES, OP_GROUP, OP_SESSIONS, pack_json, unpack, unpack_json
from test_client_integration import isolated_host, subscribed, receive_op, barrier


async def wait_ui(page, expression, *, arg=None, timeout=30000):
    # Playwright's in-page RAF poll uses eval, which the production CSP forbids.
    deadline=asyncio.get_running_loop().time()+timeout/1000
    while asyncio.get_running_loop().time()<deadline:
        if await page.evaluate(expression,arg):
            return
        await asyncio.sleep(.02)
    raise AssertionError("UI condition timed out: "+expression)


@pytest.mark.parametrize("modern", [True, False])
def test_facade_preserves_existing_sessions_and_theme_support(modern):
    asyncio.run(_check_facade(modern))


async def _check_facade(modern):
    capabilities = {**CAPABILITIES, "window_themes": modern, "conpty_paste": modern}
    with tempfile.TemporaryDirectory() as directory, patch.dict(CAPABILITIES, capabilities, clear=True):
        async with isolated_host() as (daemon, _direct, _relay):
            window = daemon.hub.open_window("Existing agent", owner=daemon.token)
            session = daemon.hub.open_cli(window.id, "cmd")
            async with serve(daemon.handle_socket, "127.0.0.1", 0, process_request=daemon.serve_http) as backend:
                base = f"http://127.0.0.1:{backend.sockets[0].getsockname()[1]}"
                theme_file = Path(directory) / "themes.json"
                frontend = Frontend(base, theme_file)
                async with serve(frontend.handler, "127.0.0.1", 0, process_request=frontend.serve_http) as server:
                    url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ws"
                    async with subscribed(url, [session.id]) as left, subscribed(url, [session.id]) as right:
                        await left.send(pack_json(OP_GROUP, {"action": "tab-theme", "window": window.id, "theme": "blue"}))
                        state = unpack_json(await receive_op(right, OP_SESSIONS))
                        assert state["windows"][0]["tab_theme"] == "blue"
                        assert window.tab_theme == ("blue" if modern else "")
                        if not modern:
                            assert json.loads(theme_file.read_text())[window.id] == "blue"
                        session.pty.inbox.put(b"unchanged\x1b[31m bytes\x1b[0m\r\n")
                        output = await receive_op(right, 3)
                        assert output == session.id.encode() + b"\0unchanged\x1b[31m bytes\x1b[0m\r\n"
                        await barrier(left)
                        if not modern:
                            from supermonoagent.protocol import pack
                            await left.send(pack(17, session.id.encode()+b"\0\x1b[200~a\rb\x1b[201~"))
                            await barrier(left)
                            import sys
                            assert session.pty.written == (b"a\nb" if sys.platform == "win32" else b"\x1b[200~a\rb\x1b[201~")
                    assert list(daemon.hub.sessions) == [session.id]
                    assert session.pty.inbox.empty()


def test_existing_frontend_is_reused_without_spawning_another_daemon():
    with patch("supermonoagent.frontend.port_open", return_value=True), patch(
        "supermonoagent.frontend.health", return_value={"role": "frontend", "backend": "http://127.0.0.1:6769", "revision": "test-ui"}
    ), patch("supermonoagent.frontend.revision", return_value="test-ui"), patch("supermonoagent.frontend.subprocess.Popen") as spawn:
        assert local_page_url("http://127.0.0.1:6769/workspace") == "http://127.0.0.1:6771/workspace?direct=1"
        spawn.assert_not_called()


def test_facade_rejects_nonlocal_backends(tmp_path):
    for url in ("http://example.com", "https://127.0.0.1", "http://user:password@127.0.0.1", "http://127.0.0.1/ws"):
        with pytest.raises(ValueError):
            Frontend(url, tmp_path / "themes.json")


def test_current_ui_automatically_opens_existing_windows():
    playwright = pytest.importorskip("playwright.async_api")
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    if not edge.exists():
        pytest.skip("Local Edge is required")
    asyncio.run(_check_browser(playwright, edge))


async def _check_browser(playwright, edge):
    with tempfile.TemporaryDirectory() as directory, patch("supermonoagent.ui_updates.fetch_manifest", side_effect=OSError("Isolated update feed offline")):
        async with isolated_host() as (daemon, _direct, _relay):
            sessions = []
            for title in ("Existing Kimi", "Existing Codex"):
                window = daemon.hub.open_window(title, owner=daemon.token)
                sessions.append(daemon.hub.open_cli(window.id, "cmd"))
            resizes = []
            sessions[1].pty.resize = lambda cols, rows: resizes.append((cols, rows))
            async with serve(daemon.handle_socket, "127.0.0.1", 0, process_request=daemon.serve_http) as backend:
                frontend = Frontend(f"http://127.0.0.1:{backend.sockets[0].getsockname()[1]}", Path(directory) / "themes.json")
                async with serve(frontend.handler, "127.0.0.1", 0, process_request=frontend.serve_http) as server, subscribed(_direct, [sessions[1].id]) as native:
                    await native.send(pack_json(5, {"session": sessions[1].id, "cols": 77, "rows": 44, "take_control": True}))
                    await barrier(native)
                    async with playwright.async_playwright() as runtime:
                        browser = await runtime.chromium.launch(executable_path=str(edge), headless=True)
                        try:
                            page = await browser.new_page()
                            await page.goto(f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/workspace?direct=1#token={daemon.token}")
                            await wait_ui(page,"window.smaWorkspace?.connected && smaWorkspace.panes.size === 2")
                            assert await page.locator(".tab").count() == 2
                            assert json.loads(await page.evaluate("smaWorkspace.activeSession")) == ["local", sessions[0].id]
                            assert {tuple(json.loads(key)) for key in await page.evaluate("[...smaWorkspace.panes.keys()]")} == {("local", session.id) for session in sessions}
                            await page.evaluate("id=>smaWorkspace.openSession(id)", sessions[1].id)
                            await wait_ui(page,"smaWorkspace.controls[smaWorkspace.activeSession]?.owner === true", timeout=2000)
                            await page.set_viewport_size({"width": 1440, "height": 900})
                            await wait_ui(page,"""() => {
                                const term=smaWorkspace.terminal, size=smaWorkspace.controls[smaWorkspace.activeSession],pane=smaWorkspace.panes.get(smaWorkspace.activeSession),desired=pane.fit.proposeDimensions();
                                return innerWidth===1440 && !pane.resizeRequest && size?.owner && term.cols===size.cols && term.rows===size.rows && term.cols===desired.cols && term.rows===desired.rows;
                            }""", timeout=2000)
                            final_size = tuple(await page.evaluate("[smaWorkspace.terminal.cols,smaWorkspace.terminal.rows]"))
                            assert (sessions[1].cols, sessions[1].rows) == final_size

                            def assert_size_bounce(pair):
                                temporary, final = pair
                                assert final == (sessions[1].cols, sessions[1].rows)
                                changes = [temporary[index] != final[index] for index in (0, 1)]
                                assert sum(changes) == 1
                                changed = changes.index(True)
                                assert abs(temporary[changed] - final[changed]) == 1

                            async def wait_for_resize_count(count):
                                async with asyncio.timeout(3):
                                    while len(resizes) < count:
                                        await asyncio.sleep(0.01)

                            await wait_ui(page,"(()=>{const p=smaWorkspace.panes.get(smaWorkspace.activeSession);return !p.redrawTimer&&!p.resizeRequest&&!p.redrawAfterReplay;})()")
                            before = len(resizes)
                            pane = page.locator(".pane").filter(has=page.locator(".pane-title", has_text="Existing Codex"))
                            await pane.locator(".pane-header button").nth(1).click()
                            await wait_for_resize_count(before + 2)
                            await wait_ui(page,"!smaWorkspace.panes.get(smaWorkspace.activeSession)?.resizeRequest")
                            assert len(resizes[before:]) <= 3
                            assert_size_bounce(resizes[before:][-2:])

                            await wait_ui(page,"(()=>{const p=smaWorkspace.panes.get(smaWorkspace.activeSession);return !p.redrawTimer&&!p.resizeRequest&&!p.redrawAfterReplay;})()")
                            await page.evaluate("id=>smaWorkspace.closeView(id)", sessions[1].id)
                            before = len(resizes)
                            await page.evaluate("id=>smaWorkspace.openSession(id)", sessions[1].id)
                            await wait_for_resize_count(before + 2)
                            await wait_ui(page,"!smaWorkspace.panes.get(smaWorkspace.activeSession)?.resizeRequest")
                            assert len(resizes[before:]) <= 3
                            assert_size_bounce(resizes[before:][-2:])
                            await page.close()
                            assert set(daemon.hub.sessions) == {s.id for s in sessions}
                        finally:
                            await browser.close()
