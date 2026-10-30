"""Exercise display-only highlights on the real mobile document and xterm renderers."""
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


@pytest.mark.parametrize('renderer', ['dom', 'webgl', 'auto'])
def test_mobile_highlights_preserve_terminal_bytes_and_native_colors(renderer):
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Hidden Edge needed for mobile rendering checks')
    asyncio.run(check_highlights(playwright, renderer))


async def check_highlights(playwright, renderer):
    with tempfile.TemporaryDirectory() as tmp, patch(
        'supermonoagent.daemon.STATE', Path(tmp)
    ), patch.dict(os.environ, {'SUPERMONO_STATE_DIR': tmp}):
        daemon = Daemon('mobile-highlight-test-only')
        daemon.hub.persist = False
        daemon.hub.pty_factory = lambda *_: ControlledPty()
        daemon.hub.attach_loop(asyncio.get_running_loop())
        window = daemon.hub.open_window(owner=daemon.token)
        first = daemon.hub.open_cli(window.id, 'cmd')
        second = daemon.hub.open_cli(window.id, 'cmd')
        first.pump.remember(b'\x1b[H\x1b[2Japp.py 42 "text"\r\n\x1b[31mError 7\x1b[0m\r\n')

        async def http(connection, request):
            path = request.path.split('?')[0]
            if path.startswith('/mobile/'):
                name = path[len('/mobile/'):]
                resource = (ROOT / 'src/supermonoagent/static' / name[7:]
                            if name.startswith('vendor/') else ROOT / 'android/assets/mobile' / name)
                if resource.is_file():
                    mime = {'.css': 'text/css', '.html': 'text/html'}.get(resource.suffix, 'text/javascript')
                    return web.text_response(connection, 200, resource.read_text(encoding='utf-8'), mime+'; charset=utf-8')
            return await daemon.serve_http(connection, request)

        try:
            async with serve(daemon.handle_socket, '127.0.0.1', 0, process_request=http) as server:
                port = server.sockets[0].getsockname()[1]
                config = {'endpoint': f'http://127.0.0.1:{port}', 'token': daemon.token, 'renderer': renderer}
                async with playwright.async_playwright() as runtime:
                    browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
                    try:
                        page = await browser.new_page(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
                        errors = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        # Observe public decorations without changing their rendering.
                        await page.add_init_script("""window.highlightDecorations=[];
                          Object.defineProperty(window,'Terminal',{configurable:true,set(T){
                            const register=T.prototype.registerDecoration;
                            T.prototype.registerDecoration=function(options){const d=register.call(this,options);
                              if(d)window.highlightDecorations.push(d);return d;};
                            Object.defineProperty(window,'Terminal',{value:T,writable:true,configurable:true});
                          }});""")
                        url = f'http://127.0.0.1:{port}/mobile/index.html#'+quote(json.dumps(config))
                        await page.goto(url)
                        await page.wait_for_function('window.MobileTerminal?.ready')
                        await page.wait_for_function("highlightDecorations.some(d=>!d.isDisposed&&d.options.foregroundColor===TerminalClient.commonAgentProfile.tokens[2].color)")
                        assert await page.evaluate('MobileTerminal.terminal.buffer.active.getLine(0).getCell(0).isFgDefault()')
                        assert await page.evaluate('MobileTerminal.terminal.buffer.active.getLine(1).getCell(0).getFgColor()') == 1
                        assert not await page.evaluate('highlightDecorations.some(d=>!d.isDisposed&&d.marker.line===1)')
                        await page.evaluate('MobileTerminal.pause();MobileTerminal.resume()')
                        await page.wait_for_function('window.MobileTerminal?.ready')
                        await page.wait_for_function("highlightDecorations.some(d=>!d.isDisposed&&d.options.foregroundColor===TerminalClient.commonAgentProfile.tokens[2].color)")

                        # Every byte boundary, including UTF-8, OSC and SGR fragments.
                        await page.evaluate(r"""async () => {
                          const t=MobileTerminal.terminal;
                          const bytes=new TextEncoder().encode('\x1b[H\x1b[2J中文😀 app.py 42 "text"\r\n\x1b]0;Error 55\x07\x1b[38;2;12;34;56mError 7\x1b[0m');
                          for(const byte of bytes)await new Promise(done=>t.write(Uint8Array.of(byte),done));
                        }""")
                        await page.wait_for_function("highlightDecorations.some(d=>!d.isDisposed&&d.marker.line===0&&d.options.x===7&&d.options.width===6)")
                        assert await page.evaluate("MobileTerminal.terminal.buffer.active.getLine(0).translateToString(true)") == '中文😀 app.py 42 "text"'
                        assert await page.evaluate('MobileTerminal.terminal.buffer.active.getLine(1).getCell(0).getFgColor()') == 0x0c2238
                        assert not await page.evaluate('highlightDecorations.some(d=>!d.isDisposed&&d.marker.line===1)')
                        if renderer != 'webgl':
                            await page.wait_for_function("""() => [...document.querySelector('.xterm-rows > div').querySelectorAll('span')]
                              .filter(s=>getComputedStyle(s).color==='rgb(229, 192, 123)').map(s=>s.textContent).join('').includes('app.py')""")
                            assert 'DOM' in await page.locator('#render-status').text_content()
                        else:
                            assert await page.locator('#renderer').input_value() == 'webgl'
                            assert 'WEBGL' in await page.locator('#render-status').text_content()

                        # Settings disable immediately and persist across page reloads.
                        await page.locator('#options').tap()
                        await page.locator('#highlight').uncheck()
                        await page.wait_for_function('highlightDecorations.every(d=>d.isDisposed)')
                        # Reopening needs the transient connection config again.
                        await page.goto('about:blank')
                        await page.goto(url)
                        await page.wait_for_function('window.MobileTerminal?.ready')
                        assert not await page.locator('#highlight').is_checked()
                        assert not await page.evaluate('highlightDecorations.some(d=>!d.isDisposed)')
                        await page.locator('#options').tap()
                        await page.locator('#highlight').check()
                        await page.locator('#settings-close').tap()
                        await page.wait_for_function('highlightDecorations.some(d=>!d.isDisposed)')

                        # Cursor overwrite removes old marks; native colors stay native.
                        await write(page, '\x1b[H\x1b[2Japp.py\r\n')
                        await page.wait_for_function('highlightDecorations.some(d=>!d.isDisposed)')
                        await write(page, '\x1b[H\x1b[2Kplain\x1b[2;1H')
                        await page.wait_for_function('highlightDecorations.every(d=>d.isDisposed)')
                        await write(page, '\x1b[H\x1b[2K\x1b[32mapp.py\x1b[0m\x1b[2;1H')
                        assert not await page.evaluate('highlightDecorations.some(d=>!d.isDisposed)')
                        for style in ('7', '8', '44'):
                            await write(page, f'\x1b[H\x1b[2K\x1b[0;{style}mapp.py\x1b[0m\x1b[2;1H')
                            await page.wait_for_function('highlightDecorations.every(d=>d.isDisposed)')

                        # Alternate screen and normal scrollback use separate markers.
                        await write(page, '\x1b[?1049h\x1b[Happ.py 42\r\n')
                        await page.wait_for_function('highlightDecorations.some(d=>!d.isDisposed)')
                        await write(page, '\x1b[?1049l')
                        await page.wait_for_function('highlightDecorations.every(d=>d.isDisposed)')
                        await write(page, '\x1b[H\x1b[2J'+''.join(f'app.py {i}\r\n' for i in range(120)))
                        await page.evaluate('MobileTerminal.terminal.scrollToTop()')
                        await page.wait_for_function('highlightDecorations.some(d=>!d.isDisposed&&d.marker.line===0)')
                        assert await page.evaluate('MobileTerminal.terminal.markers.length') <= await page.evaluate('MobileTerminal.terminal.rows')
                        await page.set_viewport_size({'width': 844, 'height': 390})
                        await page.wait_for_function('highlightDecorations.some(d=>!d.isDisposed)')

                        artifacts = ROOT / '.omx/artifacts/mobile-highlighting'
                        artifacts.mkdir(parents=True, exist_ok=True)
                        await page.screenshot(path=str(artifacts / f'{renderer}.png'))
                        await page.locator('#sessions').tap()
                        await page.locator('#session-list .session').nth(1).tap()
                        await page.wait_for_function('expected=>MobileTerminal.active===expected&&MobileTerminal.ready', arg=second.id)
                        await page.wait_for_function('highlightDecorations.every(d=>d.isDisposed)')
                        assert not first.pty.written and not second.pty.written
                        assert not errors, errors
                    finally:
                        await browser.close()
        finally:
            daemon.hub.close_window(window.id)


async def write(page, text):
    await page.evaluate('text=>new Promise(done=>MobileTerminal.terminal.write(new TextEncoder().encode(text),done))', text)
