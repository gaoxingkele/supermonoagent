"""The real mobile toolbar scrolls locally and sends only explicit Enter input."""
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


def test_mobile_history_enter_and_refresh_buttons():
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Local Edge required')
    asyncio.run(check_buttons(playwright))


async def check_buttons(playwright):
    async with isolated_host() as (daemon, _direct, _relay):
        window = daemon.hub.open_window('Navigation probe', owner=daemon.token)
        session = daemon.hub.open_cli(window.id, 'cmd')
        desktop = daemon.hub.attach(asyncio.Queue(), token=daemon.token)
        desktop.multiplex = True
        daemon.hub.set_subscriptions(desktop, [session.id])
        daemon.hub.resize_session(desktop, session.id, 100, 28, True)
        baseline = (session.resize_owner, session.cols, session.rows)

        async def http(connection, request):
            if request.path.startswith('/mobile/'):
                name = request.path.split('?')[0][len('/mobile/'):]
                file = ROOT / 'src/supermonoagent/static' / name[7:] if name.startswith('vendor/') else ROOT / 'android/assets/mobile' / name
                if file.is_file():
                    mime = {'.css':'text/css', '.html':'text/html'}.get(file.suffix, 'text/javascript')
                    return web.text_response(connection, 200, file.read_text(encoding='utf-8'), mime+'; charset=utf-8')
            return await daemon.serve_http(connection, request)

        async with serve(daemon.handle_socket, '127.0.0.1', 0, process_request=http) as server:
            origin = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
            config = {'endpoint':origin, 'token':daemon.token, 'renderer':'dom'}
            async with playwright.async_playwright() as runtime:
                browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
                try:
                    page = await browser.new_page(viewport={'width':390,'height':844}, is_mobile=True, has_touch=True)
                    page.set_default_timeout(3000)
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    await page.goto(origin+'/mobile/index.html#'+quote(json.dumps(config)))
                    await page.wait_for_function('MobileTerminal.ready')
                    assert await page.locator('#terminal-actions button').all_text_contents() == ['上翻','下翻','顶部','底部','回车','刷新']
                    session.pty.inbox.put(('\r\n'.join(f'HISTORY-{i:03}' for i in range(120))+'\r\n').encode())
                    await page.wait_for_function('MobileTerminal.terminal.buffer.active.baseY>80')
                    read = "({y:MobileTerminal.terminal.buffer.active.viewportY,base:MobileTerminal.terminal.buffer.active.baseY,rows:MobileTerminal.terminal.rows})"
                    start = await page.evaluate(read)
                    await page.locator('#page-up').click()
                    up = await page.evaluate(read)
                    assert up['y'] == start['y']-(start['rows']-1)
                    await page.locator('#page-down').click()
                    assert (await page.evaluate(read))['y'] == start['base']
                    await page.locator('#history-top').click()
                    assert (await page.evaluate(read))['y'] == 0
                    await page.locator('#history-bottom').click()
                    assert (await page.evaluate(read))['y'] == start['base']
                    await page.locator('#page-up').click()
                    before = await page.evaluate(read)
                    await page.evaluate("""() => {
                        const t=MobileTerminal.terminal,original=t.refresh.bind(t);window.refreshCalls=0;
                        t.refresh=(...args)=>{window.refreshCalls++;return original(...args);};
                    }""")
                    await page.locator('#refresh-screen').click()
                    assert await page.evaluate('refreshCalls') > 0
                    assert await page.evaluate(read) == before
                    assert not session.pty.written
                    assert (session.resize_owner, session.cols, session.rows) == baseline
                    await page.locator('[data-key="enter"]').click()
                    async with asyncio.timeout(3):
                        while not session.pty.written:
                            await asyncio.sleep(.01)
                    assert session.pty.written == b'\r'
                    await page.locator('.xterm-helper-textarea').dispatch_event('compositionstart')
                    await page.locator('[data-key="enter"]').click()
                    assert session.pty.written == b'\r'
                    await page.locator('.xterm-helper-textarea').dispatch_event('compositionend')
                    async with asyncio.timeout(3):
                        while len(session.pty.written)<2:
                            await asyncio.sleep(.01)
                    assert session.pty.written == b'\r\r'
                    session.pty.inbox.put(b'\x1b[?1049h')
                    await page.wait_for_function("MobileTerminal.terminal.buffer.active.type==='alternate'")
                    for selector in ['#page-up','#page-down','#history-top','#history-bottom','#refresh-screen']:
                        await page.locator(selector).click()
                    assert session.pty.written == b'\r\r', 'History buttons must not become remote TUI input'
                    assert (session.resize_owner, session.cols, session.rows) == baseline
                    session.pty.inbox.put(b'\x1b[?1049l')
                    await page.wait_for_function("MobileTerminal.terminal.buffer.active.type==='normal'")
                    # Even a size owner must not resize the PTY just by showing a history hint.
                    await page.locator('#control').click()
                    await page.wait_for_function('MobileTerminal.controls?.owner === true')
                    await page.evaluate('()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
                    owned = (session.resize_owner, session.cols, session.rows)
                    for selector in ['#history-bottom','#page-up','#history-top','#refresh-screen','#history-bottom']:
                        await page.locator(selector).click()
                        await page.evaluate('()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
                        assert (session.resize_owner, session.cols, session.rows) == owned
                    artifacts = ROOT / '.omx/artifacts/android-navigation-buttons'
                    artifacts.mkdir(parents=True, exist_ok=True)
                    for width,height in [(280,653),(390,844),(844,390),(320,360)]:
                        await page.set_viewport_size({'width':width,'height':height})
                        assert await page.evaluate("""() => [...document.querySelectorAll('#terminal-actions button,#keys button')].every(b=>{
                            const r=b.getBoundingClientRect();return r.left>=0&&r.top>=0&&r.right<=innerWidth+1&&r.bottom<=innerHeight+1;
                        }) && document.documentElement.scrollWidth<=innerWidth""")
                        await page.screenshot(path=str(artifacts/f'toolbar-{width}x{height}.png'))
                    await page.evaluate('MobileTerminal.pause()')
                    await page.locator('[data-key="enter"]').click()
                    assert session.pty.written == b'\r\r'
                    assert not errors, errors
                finally:
                    await browser.close()
