"""Packaged mobile UI must not steal a desktop terminal's canonical grid."""
import asyncio
import json
import mimetypes
from pathlib import Path
from urllib.parse import quote

import pytest
from websockets.asyncio.server import serve

from supermonoagent.web import text_response
from test_client_integration import isolated_host


ROOT = Path(__file__).resolve().parents[1]
EDGE = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")


def test_phone_navigation_preserves_desktop_grid_until_explicit_takeover():
    playwright = pytest.importorskip("playwright.async_api")
    if not EDGE.exists():
        pytest.skip("Local Edge required for cross-device layout regression")
    asyncio.run(check_cross_device_layout(playwright))


async def check_cross_device_layout(playwright):
    async with isolated_host() as (daemon, _direct, _relay):
        sessions = []
        for title in ("Desktop Alpha", "Desktop Beta"):
            window = daemon.hub.open_window(title, owner=daemon.token)
            sessions.append(daemon.hub.open_cli(window.id, "cmd"))

        async def http(connection, request):
            if request.path.startswith("/mobile/"):
                relative = request.path.split("?", 1)[0].removeprefix("/mobile/")
                folder = (ROOT / "android/assets/mobile").resolve()
                if relative.startswith("vendor/"):
                    relative = relative.removeprefix("vendor/")
                    folder = (ROOT / "src/supermonoagent/static").resolve()
                asset = (folder / relative).resolve()
                if asset.is_relative_to(folder) and asset.is_file():
                    return text_response(connection, 200, asset.read_text(encoding="utf8"),
                                         mimetypes.guess_type(asset.name)[0] or "text/plain")
                return text_response(connection, 404, "Missing mobile asset", "text/plain")
            return await daemon.serve_http(connection, request)

        async with serve(daemon.handle_socket, "127.0.0.1", 0, process_request=http) as server:
            origin = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            async with playwright.async_playwright() as runtime:
                browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
                try:
                    desktop = await browser.new_page(viewport={"width": 1440, "height": 900})
                    mobile = await browser.new_page(viewport={"width": 390, "height": 844},
                                                    is_mobile=True, has_touch=True)
                    errors = []
                    desktop.on("pageerror", lambda error: errors.append(str(error)))
                    mobile.on("pageerror", lambda error: errors.append(str(error)))
                    await desktop.goto(origin + "/workspace?direct=1&renderer=dom#token=" + daemon.token)
                    await desktop.wait_for_function("window.smaWorkspace?.terminal && smaWorkspace.connected")

                    async def desktop_claim(session):
                        await desktop.evaluate("sid=>smaWorkspace.openSession(sid,true)", session.id)
                        await desktop.wait_for_function("""() => {
                            const w=smaWorkspace,p=w.activePane,d=p?.fit.proposeDimensions(),c=w.controls[w.activeSession];
                            return c?.owner&&!p.resizeRequest&&d&&c.cols===d.cols&&c.rows===d.rows&&
                                   p.term.cols===d.cols&&p.term.rows===d.rows;
                        }""")

                    for session in sessions:
                        await desktop_claim(session)
                    baseline = {session.id: (session.resize_owner, session.cols, session.rows) for session in sessions}
                    assert all(cols > 100 and rows > 30 for _, cols, rows in baseline.values())
                    layout = await desktop.evaluate("smaWorkspace.layout")

                    config = quote(json.dumps({"endpoint": origin, "code": "", "token": daemon.token,
                                               "renderer": "dom"}))
                    await mobile.goto(origin + "/mobile/index.html#" + config)
                    await mobile.wait_for_function("window.MobileTerminal?.connected && MobileTerminal.ready")

                    async def assert_desktop_preserved():
                        # Wait past mobile's animation-frame fit and actual WebSocket round trip.
                        await mobile.evaluate("()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
                        await asyncio.sleep(.15)
                        assert {session.id: (session.resize_owner, session.cols, session.rows) for session in sessions} == baseline
                        assert await desktop.evaluate("smaWorkspace.layout") == layout
                        assert await desktop.evaluate("""() => {
                            const p=smaWorkspace.activePane,r=p.screen.getBoundingClientRect(),t=p.term.element.getBoundingClientRect();
                            return innerWidth===1440&&innerHeight===900&&r.width>1000&&r.height>600&&
                                   t.width>=r.width*.95&&t.right<=r.right+2;
                        }""")

                    await assert_desktop_preserved()
                    for session in (sessions[1], sessions[0], sessions[1]):
                        await mobile.locator("#sessions").click()
                        await mobile.locator(f'#session-list [data-session="{session.id}"]').click()
                        await mobile.wait_for_function("sid=>MobileTerminal.ready&&MobileTerminal.active===sid", arg=session.id)
                        await assert_desktop_preserved()
                    for viewport in ({"width": 844, "height": 390}, {"width": 390, "height": 844}):
                        await mobile.set_viewport_size(viewport)
                        await assert_desktop_preserved()
                    await mobile.locator("#mode").click()
                    await mobile.locator("#composer").wait_for(state="visible")
                    await mobile.locator("#draft").fill("unsent draft must not resize another client's terminal")
                    await assert_desktop_preserved()
                    await mobile.locator("#composer-close").click()

                    # The explicit phone control button may claim and fit this one session.
                    target = sessions[1]
                    old_owner = target.resize_owner
                    await mobile.locator("#control").click()
                    await mobile.wait_for_function("MobileTerminal.controls?.owner === true")
                    assert target.resize_owner != old_owner
                    assert target.cols < baseline[target.id][1]
                    assert (sessions[0].resize_owner, sessions[0].cols, sessions[0].rows) == baseline[sessions[0].id]
                    portrait_grid = (target.cols, target.rows)
                    await mobile.set_viewport_size({"width": 844, "height": 390})
                    await mobile.wait_for_function("MobileTerminal.terminal.cols > 60")
                    assert (target.cols, target.rows) != portrait_grid
                    phone_owner, phone_grid = target.resize_owner, (target.cols, target.rows)
                    for title in ("Desktop Alpha", "Desktop Beta"):
                        await desktop.locator('#tabs [role="tab"]').filter(has_text=title).click()
                        await desktop.evaluate("()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
                        await asyncio.sleep(.15)
                        assert target.resize_owner == phone_owner
                        assert (target.cols, target.rows) == phone_grid
                    # Explicit desktop takeover restores the full desktop grid.
                    await desktop_claim(target)
                    await mobile.wait_for_function("MobileTerminal.controls?.owner === false")
                    # xterm's scrollbar can consume one cell after a remote-grid replay;
                    # desktop_claim already proves equality with its current fit result.
                    assert target.cols >= baseline[target.id][1] - 1
                    assert target.rows == baseline[target.id][2]
                    assert target.resize_owner == baseline[target.id][0]
                    assert not errors, errors
                    assert all(not session.pty.written for session in sessions)
                finally:
                    await browser.close()
