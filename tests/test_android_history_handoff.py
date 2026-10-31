"""Mobile history must survive large snapshots and shared-grid handoff."""
import asyncio
import json
from pathlib import Path
from urllib.parse import quote

import pytest
from websockets.asyncio.server import serve
from playwright.async_api import async_playwright

from supermonoagent import web
from test_client_integration import isolated_host

ROOT = Path(__file__).resolve().parents[1]
EDGE = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')


def test_mobile_keeps_numbered_history_across_snapshot_overflow_and_takeover():
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Hidden Edge required')
    asyncio.run(check_handoff(playwright))


async def check_handoff(playwright):
    async with isolated_host() as (daemon, _direct, _relay):
        window = daemon.hub.open_window('Mobile history fixture', owner=daemon.token)
        session = daemon.hub.open_cli(window.id, 'cmd')
        desktop = daemon.hub.attach(asyncio.Queue(), key='mobile-history-owner', token=daemon.token)
        desktop.multiplex = True
        desktop.subscriptions = {session.id}
        assert daemon.hub.resize_session(desktop, session.id, 158, 44, True)

        async def http(connection, request):
            path = request.path.split('?')[0]
            if path.startswith('/mobile/'):
                name = path[len('/mobile/'):]
                resource = ROOT / 'src/supermonoagent/static' / name[7:] if name.startswith('vendor/') else ROOT / 'android/assets/mobile' / name
                if resource.is_file():
                    mime = {'.html': 'text/html', '.css': 'text/css', '.json': 'application/json'}.get(resource.suffix, 'text/javascript')
                    return web.text_response(connection, 200, resource.read_text(encoding='utf8'), mime + '; charset=utf-8')
            return await daemon.serve_http(connection, request)

        try:
            async with serve(daemon.handle_socket, '127.0.0.1', 0, process_request=http) as server:
                origin = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
                config = quote(json.dumps({'endpoint': origin, 'code': '', 'token': daemon.token, 'renderer': 'dom'}))
                async with playwright.async_playwright() as runtime:
                    browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
                    try:
                        page = await browser.new_page(viewport={'width': 390, 'height': 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
                        errors = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        await page.goto(origin + '/mobile/index.html#' + config)
                        await page.wait_for_function('window.MobileTerminal?.ready&&MobileTerminal.controls?.busy', timeout=15000)
                        for first, count in ((0, 2200), (2200, 2200), (4400, 2100)):
                            text = ''.join(f'AUDIT_ROW_{index:05d}_' + 'x' * 55 + '\r\n' for index in range(first, first + count))
                            session.pty.inbox.put(text.encode())
                            await page.wait_for_function(f'MobileTerminal.terminal.buffer.active.length>={first + count + 1}', timeout=20000)
                        assert await page.evaluate("MobileTerminal.terminal.buffer.active.getLine(0)?.translateToString(true).includes('AUDIT_ROW_00000_')")
                        assert await page.evaluate("(()=>{const b=MobileTerminal.terminal.buffer.active;return Array.from({length:100},(_,i)=>b.getLine(b.length-1-i)?.translateToString(true)||'').some(row=>row.includes('AUDIT_ROW_06499_'));})()")
                        await page.locator('#control').tap()
                        await page.wait_for_function('MobileTerminal.controls?.owner&&MobileTerminal.terminal.cols<80', timeout=12000)
                        await page.wait_for_timeout(1200)
                        await page.locator('#history-top').tap()
                        await page.wait_for_function("MobileTerminal.terminal.buffer.active.viewportY===0&&MobileTerminal.terminal.buffer.active.getLine(0)?.translateToString(true).includes('AUDIT_ROW_00000_')")
                        await page.locator('#history-bottom').tap()
                        await page.wait_for_function("MobileTerminal.terminal.buffer.active.viewportY===MobileTerminal.terminal.buffer.active.baseY")
                        assert await page.evaluate("(()=>{const t=MobileTerminal.terminal,b=t.buffer.active;return Array.from({length:t.rows},(_,row)=>b.getLine(b.viewportY+row)?.translateToString(true)||'').some(line=>line.includes('AUDIT_ROW_06499_'));})()")
                        assert not session.pty.written
                        assert not errors, errors
                    finally:
                        await browser.close()
        finally:
            daemon.hub.detach(desktop.key)
