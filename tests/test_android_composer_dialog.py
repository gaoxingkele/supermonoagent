"""Exercise the packaged mobile editor against isolated, manually echoed input."""
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


def test_floating_editor_verified_send_github_and_f2():
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Local Edge required')
    asyncio.run(check_editor(playwright))


async def check_editor(playwright):
    async with isolated_host() as (daemon, _direct, _relay):
        window = daemon.hub.open_window('Editor fixture', owner=daemon.token)
        session = daemon.hub.open_cli(window.id, 'cmd')

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
                    page = await browser.new_page(viewport={'width':390, 'height':844}, is_mobile=True, has_touch=True)
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    await page.goto(origin+'/mobile/index.html#'+quote(json.dumps(config)))
                    await page.wait_for_function('MobileTerminal.ready')
                    await page.locator('#mode').click()
                    assert await page.locator('dialog#composer').evaluate('d=>d.open')
                    await page.locator('#draft').fill('saved draft')
                    await page.locator('#composer-close').click()
                    await page.locator('#mode').click()
                    assert await page.locator('#draft').input_value() == 'saved draft'
                    # A genuine disconnect retains text and cannot transmit input.
                    await page.evaluate('MobileTerminal.pause()')
                    await page.locator('#send').click()
                    assert await page.locator('#draft').input_value() == 'saved draft'
                    assert not session.pty.written
                    await page.evaluate('MobileTerminal.resume()')
                    await page.wait_for_function('MobileTerminal.ready')
                    if not await page.locator('#composer').evaluate('d=>d.open'):
                        await page.locator('#mode').click()
                    await page.locator('#draft').fill('verified-command')
                    await page.locator('#send').click()
                    await page.wait_for_function("document.querySelector('#send').disabled")
                    async with asyncio.timeout(3):
                        while b'verified-command' not in session.pty.written:
                            await asyncio.sleep(.01)
                    assert b'\r' not in session.pty.written
                    assert await page.locator('#draft').input_value() == 'verified-command'
                    # Unrelated output must never acknowledge the command.
                    session.pty.inbox.put(b'unrelated output\r\n')
                    await page.wait_for_function("MobileTerminal.terminal.buffer.active.getLine(0)?.translateToString().includes('unrelated output')")
                    assert b'\r' not in session.pty.written
                    session.pty.inbox.put(b'verified-command')
                    await page.wait_for_function("document.querySelector('#draft').value === ''")
                    async with asyncio.timeout(3):
                        while b'\r' not in session.pty.written:
                            await asyncio.sleep(.01)
                    assert session.pty.written.count(b'\r') == 1
                    assert not await page.locator('#composer').evaluate('d=>d.open')
                    await page.locator('[data-key="f2"]').click()
                    async with asyncio.timeout(3):
                        while b'\x1bOQ' not in session.pty.written:
                            await asyncio.sleep(.01)
                    before = bytes(session.pty.written)
                    await page.locator('#github').click()
                    assert await page.locator('#composer').evaluate('d=>d.open')
                    assert await page.locator('#draft').input_value() == '提交全部文件到 GitHub'
                    assert bytes(session.pty.written) == before
                    for width,height in [(320,640),(720,340),(390,360),(390,844)]:
                        await page.set_viewport_size({'width':width,'height':height})
                        assert await page.locator('#composer').evaluate('d=>{const r=d.getBoundingClientRect();return r.left>=0&&r.top>=0&&r.right<=innerWidth+1&&r.bottom<=innerHeight+1&&d.scrollWidth<=d.clientWidth+1;}')
                        artifact = ROOT / '.omx/artifacts/android-floating-editor'
                        artifact.mkdir(parents=True, exist_ok=True)
                        await page.screenshot(path=str(artifact / f'editor-{width}x{height}.png'))
                    # Missing echo leaves the draft and prevents an accidental repeat paste.
                    await page.locator('#draft').fill('no-echo-command')
                    await page.locator('#send').click()
                    await page.wait_for_function("document.querySelector('#composer-status').textContent.includes('未收到')", timeout=7000)
                    assert await page.locator('#draft').input_value() == 'no-echo-command'
                    assert await page.locator('#send').is_disabled()
                    assert session.pty.written.count(b'no-echo-command') == 1
                    assert session.pty.written.count(b'\r') == 1
                    await page.locator('#composer-close').click()
                    assert not await page.evaluate("MobileTerminal.openNotification({endpoint:'http://example.invalid',code:'',session:'missing'})")
                    assert not await page.evaluate("v=>MobileTerminal.openNotification(v)", {'endpoint':origin,'code':'','session':'closed-session'})
                    assert await page.evaluate("v=>MobileTerminal.openNotification(v)", {'endpoint':origin,'code':'','session':session.id})
                    assert await page.evaluate('MobileTerminal.active') == session.id
                    assert len(daemon.hub.sessions) == 1
                    assert not errors, errors
                finally:
                    await browser.close()
