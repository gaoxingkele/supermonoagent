"""A CLI owns its suggestion; clients send exactly one right key, never Enter."""
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
SUGGESTION = 'src/app.py 42 "下一步建议"'


@pytest.mark.parametrize('surface', ['windows-keyboard', 'windows-button', 'android-button'])
@pytest.mark.parametrize('application', [False, True])
def test_right_accepts_owned_cli_suggestion_without_submitting(surface, application):
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Local Edge required')
    asyncio.run(check(playwright, surface, application))


async def check(playwright, surface, application):
    async with isolated_host() as (daemon, _direct, _relay):
        window = daemon.hub.open_window('Owned Claude-like prompt', owner=daemon.token)
        session = daemon.hub.open_cli(window.id, 'cmd')
        expected = b'\x1bOC' if application else b'\x1b[C'
        accepted = []
        original = session.pty.write
        def cli_input(data):
            original(data)
            if data == expected:
                accepted.append(True)
                session.pty.inbox.put(('\x1b[H\x1b[2K\x1b[0m> ' + SUGGESTION).encode())
        session.pty.write = cli_input

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
            mobile = surface == 'android-button'
            url = origin + ('/mobile/index.html#' + quote(json.dumps({'endpoint': origin, 'token': daemon.token, 'renderer': 'dom'})) if mobile else '/workspace?direct=1&renderer=dom#token='+daemon.token)
            async with playwright.async_playwright() as runtime:
                browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
                try:
                    page = await browser.new_page(viewport={'width': 390 if surface != 'windows-keyboard' else 1100, 'height': 844})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    await page.goto(url)
                    client = 'MobileTerminal' if mobile else 'smaWorkspace'
                    await page.wait_for_function(f'{client}.ready' if mobile else 'smaWorkspace.activePane?.ready')
                    terminal = client + ('.terminal' if mobile else '.activePane.term')
                    sequence = ('\x1b[?1h' if application else '\x1b[?1l') + '\x1b[2J\x1b[H> \x1b[2m' + SUGGESTION + '\x1b[0m\x1b[3G'
                    session.pty.inbox.put(sequence.encode())
                    await page.wait_for_function(f'{terminal}.buffer.active.getLine(0)?.translateToString(true).includes("下一步建议")&&{terminal}.buffer.active.cursorX===2')
                    assert await page.evaluate(f'!!{terminal}.buffer.active.getLine(0).getCell(2).isDim()')
                    assert not session.pty.written
                    if surface == 'windows-keyboard':
                        await page.evaluate(f'{terminal}.focus()')
                        await page.keyboard.press('ArrowRight')
                    else:
                        await page.locator('[data-key="right"]').click()
                    await page.wait_for_function(f'!{terminal}.buffer.active.getLine(0).getCell(2).isDim()&&{terminal}.buffer.active.cursorX>2')
                    assert accepted == [True]
                    assert bytes(session.pty.written) == expected
                    assert b'\r' not in session.pty.written and b'\n' not in session.pty.written
                    assert await page.evaluate(f'{terminal}.buffer.active.getLine(0).translateToString(true)') == '> ' + SUGGESTION
                    assert not errors
                finally:
                    await browser.close()
