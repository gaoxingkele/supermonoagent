"""Exercise actual terminal geometry in Edge without touching user sessions."""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from supermonoagent.daemon import Daemon
from test_client_integration import ControlledPty
from websockets.asyncio.server import serve

EDGE = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")


class LayoutPty(ControlledPty):
    dimensions = None

    def resize(self, cols, rows):
        self.dimensions = (cols, rows)


@pytest.mark.parametrize("route", ["/workspace", "/legacy"])
def test_terminal_tracks_viewport(route):
    playwright = pytest.importorskip("playwright.async_api")
    if not EDGE.exists():
        pytest.skip("Local Edge is required for layout verification")
    asyncio.run(_check_layout(playwright, route))


async def _check_layout(playwright, route):
    with tempfile.TemporaryDirectory(prefix="supermono-layout-") as directory, patch(
        "supermonoagent.daemon.STATE", Path(directory)
    ), patch.dict(os.environ, {"SUPERMONO_STATE_DIR": directory}):
        daemon = Daemon("layout-test-only")
        daemon.hub.persist = False
        daemon.hub.pty_factory = lambda *_: LayoutPty()
        daemon.hub.attach_loop(asyncio.get_running_loop())
        window = daemon.hub.open_window(owner=daemon.token)
        daemon.hub.open_cli(window.id, "cmd")
        try:
            async with serve(daemon.handle_socket, "127.0.0.1", 0, process_request=daemon.serve_http) as server:
                port = server.sockets[0].getsockname()[1]
                async with playwright.async_playwright() as runtime:
                    browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
                    try:
                        page = await browser.new_page(viewport={"width": 1100, "height": 760})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        await page.goto(f"http://127.0.0.1:{port}{route}?token=layout-test-only&renderer=dom")
                        await page.wait_for_function("window.smaWorkspace?.terminal || window.__lastTerm", timeout=5000)
                        for width, height in ((1100, 760), (1920, 1080), (850, 700), (390, 844), (1440, 900)):
                            await page.set_viewport_size({"width": width, "height": height})
                            # Classic layout debounces viewport events for 150 ms.
                            await page.wait_for_timeout(250)
                            geometry = await page.evaluate("""() => {
                                const workspace=!!window.smaWorkspace;
                                const screen=document.querySelector(workspace?'.terminal-screen':'.screen');
                                const stage=document.querySelector(workspace?'#stage':'#grid');
                                const term=workspace?smaWorkspace.terminal:__lastTerm;
                                const cell=term._core._renderService.dimensions.css.cell;
                                const rect=screen.getBoundingClientRect(), style=getComputedStyle(screen);
                                const terminalStyle=getComputedStyle(term.element);
                                const scrollBar=term._core.viewport.scrollBarWidth;
                                const contentWidth=screen.clientWidth-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight)
                                    -parseFloat(terminalStyle.paddingLeft)-parseFloat(terminalStyle.paddingRight)-scrollBar;
                                return {viewport:innerWidth, stage:stage.clientWidth, screen:rect.width,
                                    right:rect.right, contentWidth, terminal:term.cols*cell.width,
                                    cols:term.cols, rows:term.rows, cellWidth:cell.width,
                                    document:document.documentElement.scrollWidth};
                            }""")
                            print(route, width, height, geometry)
                            artifacts = os.environ.get("SUPERMONO_LAYOUT_ARTIFACTS")
                            if artifacts:
                                destination = Path(artifacts)
                                destination.mkdir(parents=True, exist_ok=True)
                                stem = f"{route[1:]}-{width}x{height}"
                                (destination / f"{stem}.json").write_text(json.dumps(geometry), encoding="utf-8")
                                await page.screenshot(path=str(destination / f"{stem}.png"))
                            assert geometry["document"] <= width, geometry
                            assert abs(geometry["screen"] - geometry["stage"]) <= 2, geometry
                            assert geometry["terminal"] <= geometry["contentWidth"] + 1, geometry
                            assert geometry["contentWidth"] - geometry["terminal"] <= geometry["cellWidth"] + 1, geometry
                            session = next(iter(daemon.hub.sessions.values()))
                            assert session.pty.dimensions == (geometry["cols"], geometry["rows"])
                        if route == "/legacy":
                            await page.get_by_role("button", name="最大化", exact=True).click()
                            await page.set_viewport_size({"width": 1920, "height": 1080})
                            await page.wait_for_timeout(250)
                            await page.get_by_role("button", name="还原", exact=True).click()
                            await page.wait_for_function("Math.abs(document.querySelector('.card').getBoundingClientRect().width - document.querySelector('#grid').clientWidth) <= 2")
                            # A sidebar change resizes the grid without a browser resize event.
                            await page.locator("#side-toggle").click()
                            await page.wait_for_function("Math.abs(document.querySelector('.card').getBoundingClientRect().width - document.querySelector('#grid').clientWidth) <= 2")
                        else:
                            await page.locator("#sidebar-toggle").click()
                            await page.wait_for_function("document.querySelector('#stage').clientWidth === innerWidth")
                            await page.locator("#sidebar-toggle").click()
                            await page.wait_for_function("document.querySelector('#stage').clientWidth < innerWidth")
                        assert not errors, errors
                    finally:
                        await browser.close()
        finally:
            for window_id in list(daemon.hub.windows):
                daemon.hub.close_window(window_id)
