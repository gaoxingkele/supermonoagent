"""Two isolated PTYs through one relay, running the actual packaged Android document."""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import pytest
from websockets.asyncio.server import serve
from supermonoagent.daemon import Daemon, relay_uplink
from supermonoagent.relay import Relay
from supermonoagent import web
from test_client_integration import ControlledPty

ROOT = Path(__file__).resolve().parents[1]
EDGE = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')


def test_mobile_parallel_directories_and_single_renderer():
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Hidden Edge needed for packaged Android document')
    asyncio.run(check(playwright))


async def check(playwright):
    with tempfile.TemporaryDirectory() as tmp, patch('supermonoagent.daemon.STATE', Path(tmp)), patch.dict(os.environ, {'SUPERMONO_STATE_DIR': tmp}):
        daemons, sessions, jobs = [], [], []
        relay = Relay(development=True)
        for index in range(2):
            daemon = Daemon('mobile-owner-'+str(index))
            daemon.hub.persist = False
            daemon.hub.pty_factory = lambda *_: ControlledPty()
            daemon.hub.attach_loop(asyncio.get_running_loop())
            daemon.hub.set_identity('Computer '+str(index), 'Project '+str(index))
            window = daemon.hub.open_window(owner=daemon.token)
            daemon.hub.groups[window.group_id].name = 'Group '+str(index)
            with patch('supermonoagent.hub.secrets.token_hex', return_value='aabbccdd'):
                session = daemon.hub.open_cli(window.id, 'cmd')
            daemons.append(daemon)
            sessions.append(session)

        async def http(connection, request):
            if request.path.startswith('/mobile/'):
                name = request.path.split('?')[0][len('/mobile/'):]
                file = ROOT / 'src/supermonoagent/static' / name[7:] if name.startswith('vendor/') else ROOT / 'android/assets/mobile' / name
                if file.is_file():
                    mime = 'text/css' if file.suffix == '.css' else 'text/html' if file.suffix == '.html' else 'text/javascript'
                    return web.text_response(connection, 200, file.read_text(encoding='utf-8'), mime+'; charset=utf-8')
            return await relay.serve_page(connection, request)

        try:
            async with serve(relay.handler, '127.0.0.1', 0, process_request=http) as server:
                origin = 'http://127.0.0.1:'+str(server.sockets[0].getsockname()[1])
                url = origin.replace('http:', 'ws:')+'/ws'
                for index, daemon in enumerate(daemons):
                    jobs.append(asyncio.create_task(relay_uplink(daemon, url, 'room-'+str(index), str(index))))
                async with asyncio.timeout(5):
                    while len([r for r in relay.rooms.values() if r.host]) < 2:
                        await asyncio.sleep(.01)
                config = {'endpoint': origin, 'code': 'room-0', 'token': daemons[0].token, 'renderer': 'dom'}
                async with playwright.async_playwright() as runtime:
                    browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
                    try:
                        page = await browser.new_page(viewport={'width':390, 'height':844}, is_mobile=True, has_touch=True)
                        errors = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        await page.add_init_script('localStorage.setItem("sma.mobile.hosts", '+json.dumps(json.dumps([{'id':'host:second', 'name':'Other', 'endpoint':origin, 'code':'room-1'}]))+');')
                        await page.goto(origin+'/mobile/index.html#'+quote(json.dumps(config)))
                        await page.wait_for_function('MobileTerminal.ready && MobileTerminal.hosts[1].status.startsWith(String.fromCodePoint(0x9a8c,0x8bc1,0x5931,0x8d25))')
                        # Failed authentication on another computer leaves the active socket usable.
                        await page.tap('#screen')
                        await page.keyboard.insert_text('home-input')
                        await wait_for(lambda: b'home-input' in sessions[0].pty.written)
                        assert not sessions[1].pty.written
                        await page.tap('#sessions')
                        await page.locator('#host-list .host-row').nth(1).locator('button').nth(1).tap()
                        await page.locator('#host-token').fill(daemons[1].token)
                        await page.locator('#host-form button[type=submit]').tap()
                        await page.wait_for_function('MobileTerminal.ready && MobileTerminal.hosts.every(h=>h.connected)')
                        assert await page.evaluate('MobileTerminal.activeKey') == '["host:second","aabbccdd"]'
                        assert await page.locator('.xterm').count() == 1
                        await page.tap('#screen')
                        await page.keyboard.insert_text('other-input')
                        await wait_for(lambda: b'other-input' in sessions[1].pty.written)
                        assert b'other-input' not in sessions[0].pty.written
                        # TD-006D/I: unopened output streams remain unsubscribed; directories stay connected.
                        await page.wait_for_function('MobileTerminal.hosts[0].subscription==="" && MobileTerminal.hosts[1].subscription==="aabbccdd"')
                        assert all(not client.subscriptions for client in daemons[0].hub.clients.values() if client.multiplex)
                        sessions[0].pty.inbox.put(b'ONLY-HOME\r\n')
                        sessions[1].pty.inbox.put(b'ONLY-OTHER\r\n')
                        await page.wait_for_function('MobileTerminal.terminal.buffer.active.getLine(0).translateToString().includes("ONLY-OTHER")')
                        assert 'ONLY-HOME' not in await screen(page)
                        await page.tap('#mode')
                        await page.locator('#draft').fill('other unsent draft')
                        await page.evaluate('MobileTerminal.chooseHost("home")')
                        await page.wait_for_function('MobileTerminal.ready')
                        assert await page.locator('#composer').is_hidden()
                        await page.tap('#mode')
                        await page.locator('#draft').fill('home unsent draft')
                        await page.evaluate('MobileTerminal.chooseHost("host:second")')
                        await page.wait_for_function('MobileTerminal.ready')
                        assert await page.locator('#draft').input_value() == 'other unsent draft'
                        assert 'ONLY-HOME' not in await screen(page)
                        # Retry only the disconnected computer; preserve the other transport.
                        home_clients = set(daemons[0].hub.clients)
                        before_connections = await page.evaluate('MobileTerminal.metrics.connections')
                        for phone in list(relay.rooms['room-1'].phones.values()):
                            await phone.close(code=4004)
                        await page.wait_for_function('(before)=>MobileTerminal.ready && MobileTerminal.hosts.every(h=>h.connected) && MobileTerminal.metrics.connections>before', arg=before_connections)
                        assert set(daemons[0].hub.clients) == home_clients
                        # Authentication faults stop retrying only the failing computer.
                        for phone in list(relay.rooms['room-1'].phones.values()):
                            await phone.close(code=4001)
                        await page.wait_for_function('MobileTerminal.hosts[1].status.startsWith(String.fromCodePoint(0x9a8c,0x8bc1,0x5931,0x8d25))')
                        assert await page.evaluate('MobileTerminal.hosts[0].connected')
                        assert set(daemons[0].hub.clients) == home_clients
                        await page.evaluate('MobileTerminal.pause()')
                        await wait_for(lambda: all(not room.phones for room in relay.rooms.values()) and not relay.watchers)
                        assert await page.evaluate('MobileTerminal.hosts.every(h=>!h.connected)')
                        await page.evaluate('MobileTerminal.resume()')
                        await page.wait_for_function('MobileTerminal.ready && MobileTerminal.hosts.every(h=>h.connected)')
                        assert await page.locator('#draft').input_value() == 'other unsent draft'
                        assert await page.locator('.xterm').count() == 1
                        await page.tap('#sessions')
                        assert 'Project 0' in await page.locator('#session-list').inner_text()
                        assert 'Project 1' in await page.locator('#session-list').inner_text()
                        assert 'Group 0' in await page.locator('#session-list').inner_text()
                        assert 'Group 1' in await page.locator('#session-list').inner_text()
                        # Endpoint/room edits immediately invalidate the prefilled credential.
                        await page.locator('#host-list .host-row').nth(1).locator('button').nth(1).tap()
                        assert await page.locator('#host-token').input_value() == daemons[1].token
                        await page.locator('#host-code').fill('different-room')
                        assert await page.locator('#host-token').input_value() == ''
                        # Reuse the same saved host ID for a different room with the SAME session ID.
                        # Old drafts, input modes, catalog and control identity must not migrate.
                        await page.locator('#host-code').fill('room-0')
                        await page.locator('#host-token').fill(daemons[0].token)
                        await page.locator('#host-form button[type=submit]').tap()
                        await page.wait_for_function('MobileTerminal.ready')
                        assert await page.evaluate('MobileTerminal.activeKey') == '["host:second","aabbccdd"]'
                        assert await page.locator('#draft').input_value() == ''
                        assert await page.locator('#composer').is_hidden()
                        await page.tap('#sessions')
                        assert 'Project 1' not in await page.locator('#session-list').inner_text()
                        assert 'Group 1' not in await page.locator('#session-list').inner_text()
                        assert not errors
                        await page.evaluate('MobileTerminal.pause()')
                    finally:
                        await browser.close()
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            for daemon in daemons:
                for window_id in list(daemon.hub.windows):
                    daemon.hub.close_window(window_id)


async def wait_for(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.01)


async def screen(page):
    return await page.evaluate('''()=>{const b=MobileTerminal.terminal.buffer.active;return Array.from({length:b.length},(_,i)=>b.getLine(i).translateToString()).join("\\n")}''')
