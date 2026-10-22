"""Replay existing cursor-addressed output only after learning its PTY grid."""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import pytest
from websockets.asyncio.server import serve

from supermonoagent import web
from supermonoagent.daemon import Daemon
from test_client_integration import ControlledPty


ROOT = Path(__file__).resolve().parents[1]
EDGE = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')


def test_mobile_replay_preserves_server_grid_on_connect_reconnect_and_switch():
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Hidden Edge needed for packaged mobile replay checks')
    asyncio.run(check_replay(playwright))


async def check_replay(playwright):
    with tempfile.TemporaryDirectory() as tmp, patch(
        'supermonoagent.daemon.STATE', Path(tmp)
    ), patch.dict(os.environ, {'SUPERMONO_STATE_DIR': tmp}):
        daemon = Daemon('mobile-replay-test-only')
        daemon.hub.persist = False
        daemon.hub.pty_factory = lambda *_: ControlledPty()
        daemon.hub.attach_loop(asyncio.get_running_loop())
        window = daemon.hub.open_window(owner=daemon.token)
        first = daemon.hub.open_cli(window.id, 'cmd')
        second = daemon.hub.open_cli(window.id, 'powershell')
        desktop = daemon.hub.attach(
            asyncio.Queue(), key='desktop-replay-test', token=daemon.token
        )
        desktop.multiplex = True
        desktop.subscriptions = {first.id, second.id}
        for session, cols, rows, label in (
            (first, 120, 30, 'FIRST'), (second, 132, 32, 'SECOND')
        ):
            assert daemon.hub.resize_session(desktop, session.id, cols, rows, True)
            # The real subscription path sends this snapshot before its controls
            # reply. Its cursor positions exceed xterm's initial 80 x 24 grid.
            session.pump.remember((
                '\x1b[H\x1b[2J'
                f'\x1b[1;100HRIGHT_{label}'
                f'\x1b[10;30HMIDDLE_{label}'
                f'\x1b[28;1HBOTTOM_{label}'
            ).encode())

        async def http(connection, request):
            path = request.path.split('?')[0]
            if path.startswith('/mobile/'):
                name = path[len('/mobile/'):]
                resource = (
                    ROOT / 'src/supermonoagent/static' / name[len('vendor/'):]
                    if name.startswith('vendor/') else ROOT / 'android/assets/mobile' / name
                )
                if resource.is_file():
                    mime = {
                        '.css': 'text/css', '.html': 'text/html'
                    }.get(resource.suffix, 'text/javascript')
                    return web.text_response(
                        connection, 200, resource.read_text(encoding='utf-8'),
                        mime + '; charset=utf-8'
                    )
            return await daemon.serve_http(connection, request)

        try:
            async with serve(
                daemon.handle_socket, '127.0.0.1', 0, process_request=http
            ) as server:
                port = server.sockets[0].getsockname()[1]
                config = {
                    'endpoint': f'http://127.0.0.1:{port}',
                    'token': daemon.token, 'renderer': 'dom'
                }
                async with playwright.async_playwright() as runtime:
                    browser = await runtime.chromium.launch(
                        executable_path=str(EDGE), headless=True
                    )
                    try:
                        page = await browser.new_page(
                            viewport={'width': 390, 'height': 844},
                            is_mobile=True, has_touch=True
                        )
                        errors = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        await page.add_init_script(
                            "localStorage.setItem('sma.mobile.sizing', 'shared')"
                        )
                        await page.goto(
                            f'http://127.0.0.1:{port}/mobile/index.html#'
                            + quote(json.dumps(config))
                        )
                        await assert_replay(page, first.id, 120, 30, 'FIRST')

                        connections = await page.evaluate('MobileTerminal.metrics.connections')
                        await page.evaluate('MobileTerminal.pause()')
                        await page.wait_for_function('!MobileTerminal.connected')
                        await page.evaluate('MobileTerminal.resume()')
                        await assert_replay(page, first.id, 120, 30, 'FIRST')
                        assert await page.evaluate('MobileTerminal.metrics.connections') == connections + 1

                        for session, cols, rows, label in (
                            (second, 132, 32, 'SECOND'), (first, 120, 30, 'FIRST')
                        ):
                            await page.locator('#sessions').tap()
                            await page.locator('#session-list .session').nth(
                                1 if session is second else 0
                            ).tap()
                            await assert_replay(page, session.id, cols, rows, label)
                        assert not errors, errors
                    finally:
                        await browser.close()
        finally:
            daemon.hub.detach(desktop.key)
            daemon.hub.close_window(window.id)


async def assert_replay(page, session_id, cols, rows, label):
    await page.wait_for_function('''expected => {
        const mobile = window.MobileTerminal, term = mobile?.terminal;
        if (!mobile?.ready || mobile.active !== expected.id ||
            term.cols !== expected.cols || term.rows !== expected.rows) return false;
        const buffer = term.buffer.active;
        const line = row => buffer.getLine(buffer.baseY + row)?.translateToString() || '';
        return line(0).slice(99).startsWith('RIGHT_' + expected.label) &&
            line(9).slice(29).startsWith('MIDDLE_' + expected.label) &&
            line(27).startsWith('BOTTOM_' + expected.label);
    }''', arg={'id': session_id, 'cols': cols, 'rows': rows, 'label': label}, timeout=5000)
