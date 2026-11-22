"""Raw input by default: selections never trap Ctrl+C; double-tap blank re-enters it."""
import asyncio
import json
from pathlib import Path
from urllib.parse import quote

import pytest
from websockets.asyncio.server import serve

from supermonoagent import web
from test_client_integration import isolated_host

ROOT = Path(__file__).resolve().parents[1]
EDGE = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')


async def eventually(predicate, timeout=6):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.02)


async def eventually_page(page, expression, timeout=6):
    async with asyncio.timeout(timeout):
        while not await page.evaluate(expression):
            await asyncio.sleep(.05)


def test_ctrl_c_with_selection_and_double_click_blank_stay_raw():
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Local Edge required')
    asyncio.run(check(playwright))


async def check(playwright):
    async with isolated_host() as (daemon, _direct, _relay):
        window = daemon.hub.open_window('Raw input window', owner=daemon.token)
        session = daemon.hub.open_cli(window.id, 'cmd')
        async with serve(daemon.handle_socket, '127.0.0.1', 0, process_request=daemon.serve_http) as server:
            origin = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
            async with playwright.async_playwright() as runtime:
                browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
                try:
                    page = await browser.new_page(viewport={'width': 1100, 'height': 800})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    await page.goto(origin + '/workspace?direct=1&renderer=dom#token=' + daemon.token)
                    await page.wait_for_function('smaWorkspace.activePane?.ready')
                    session.pty.inbox.put(b'\x1b[2J\x1b[Hraw-marker-text-line\r\n')
                    await page.wait_for_function("smaWorkspace.activePane.term.buffer.active.getLine(0)?.translateToString(true).includes('raw-marker-text-line')")
                    assert await page.evaluate('smaWorkspace.activePane.rawInput===true')
                    box = await page.locator('.pane.active .terminal-screen').bounding_box()
                    await page.mouse.move(box['x'] + 20, box['y'] + 14)
                    await page.mouse.down()
                    await page.mouse.move(box['x'] + 220, box['y'] + 14, steps=4)
                    await page.mouse.up()
                    assert await page.evaluate('smaWorkspace.activePane.term.hasSelection()')
                    await page.evaluate('smaWorkspace.activePane.term.focus()')
                    await page.keyboard.press('Control+c')
                    await eventually(lambda: b'\x03' in bytes(session.pty.written))
                    await page.mouse.dblclick(box['x'] + box['width'] - 30, box['y'] + box['height'] - 30)
                    await eventually_page(page, '!smaWorkspace.activePane.term.hasSelection()')
                    assert await page.evaluate('smaWorkspace.activePane.rawInput===true')
                    assert not errors
                finally:
                    await browser.close()


def test_mobile_double_tap_returns_terminal_to_direct_input():
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Local Edge required')
    asyncio.run(check_mobile(playwright))


async def check_mobile(playwright):
    async with isolated_host() as (daemon, _direct, _relay):
        window = daemon.hub.open_window('Raw mobile', owner=daemon.token)
        session = daemon.hub.open_cli(window.id, 'cmd')

        async def http(connection, request):
            if request.path.startswith('/mobile/'):
                name = request.path.split('?')[0][len('/mobile/'):]
                base = ROOT / ('src/supermonoagent/static' if name.startswith('vendor/') else 'android/assets/mobile')
                path = (base / name.removeprefix('vendor/')).resolve()
                if path.is_relative_to(base) and path.is_file():
                    mime = {'.css': 'text/css', '.html': 'text/html', '.json': 'application/json'}.get(path.suffix, 'text/javascript')
                    return web.text_response(connection, 200, path.read_text(encoding='utf-8'), mime+'; charset=utf-8')
            return await daemon.serve_http(connection, request)

        async with serve(daemon.handle_socket, '127.0.0.1', 0, process_request=http) as server:
            origin = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
            url = origin + '/mobile/index.html#' + quote(json.dumps({'endpoint': origin, 'token': daemon.token, 'renderer': 'dom'}))
            async with playwright.async_playwright() as runtime:
                browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
                try:
                    page = await browser.new_page(viewport={'width': 390, 'height': 844}, has_touch=True)
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    await page.goto(url)
                    await page.wait_for_function('MobileTerminal.ready')
                    assert await page.evaluate("MobileTerminal.inputMode") == 'direct'
                    screen = await page.locator('#screen').bounding_box()
                    await page.touchscreen.tap(screen['x'] + screen['width'] / 2, screen['y'] + screen['height'] / 2)
                    await page.wait_for_timeout(120)
                    await page.touchscreen.tap(screen['x'] + screen['width'] / 2, screen['y'] + screen['height'] / 2)
                    await page.wait_for_timeout(300)
                    assert await page.evaluate("MobileTerminal.inputMode") == 'direct'
                    assert await page.evaluate("!document.querySelector('#composer')?.open")
                    assert not session.pty.written
                    assert not errors
                finally:
                    await browser.close()
