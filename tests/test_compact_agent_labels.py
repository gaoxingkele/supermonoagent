"""Both real chooser DOMs keep agent labels on one compact row."""
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


def test_both_choosers_show_compact_programming_agent_labels():
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Local Edge required')
    asyncio.run(_check(playwright))


async def _check(playwright):
    async with isolated_host() as (daemon, _direct, _relay):
        sessions = []
        for title, shell in [('Codex project ' + 'long name ' * 12, 'powershell'), ('Claude Code task', 'cmd'), ('Build task', 'powershell')]:
            window = daemon.hub.open_window(title, owner=daemon.token)
            sessions.append(daemon.hub.open_cli(window.id, shell))
        # Simulate the installed legacy daemon: terminal bytes flow, but catalogs
        # have no agent metadata. Clients must learn identity from the real parser.
        daemon.hub._task_output = lambda *_: None

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
            async with playwright.async_playwright() as runtime:
                browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
                try:
                    desktop = await browser.new_page(viewport={'width':1100, 'height':760})
                    await desktop.goto(origin+'/workspace?direct=1&renderer=dom#token='+daemon.token)
                    await desktop.wait_for_function('document.querySelectorAll("#tree .session-row").length===3')
                    assert await desktop.locator('#tree .session-shell').all_text_contents() == ['（Codex）','（Claude Code）','（其他）']
                    await _single_line(desktop, '#tree .session-open', '.session-label', '.session-shell', 32)
                    phone = await browser.new_page(viewport={'width':280,'height':653}, is_mobile=True, has_touch=True)
                    config = quote(json.dumps({'endpoint':origin,'token':daemon.token,'renderer':'dom'}))
                    await phone.goto(origin+'/mobile/index.html#'+config)
                    await phone.wait_for_function('MobileTerminal.ready')
                    await phone.locator('#sessions').click()
                    assert await phone.locator('#session-list .session-agent').all_text_contents() == ['（Codex）','（Claude Code）','（其他）']
                    await _single_line(phone, '#session-list .session', '.session-name', '.session-agent', 36)
                    output = ROOT/'.omx/artifacts/compact-agent-labels'
                    output.mkdir(parents=True, exist_ok=True)
                    await desktop.screenshot(path=str(output/'desktop.png'))
                    await phone.screenshot(path=str(output/'phone.png'))
                    await phone.locator(f'#session-list .session[data-session="{sessions[2].id}"]').click()
                    sessions[2].pty.inbox.put(b'\x1b]7501;app=codex;state=working\x07')
                    for page, selector in [(desktop,'#tree .session-shell'), (phone,'#session-list .session-agent')]:
                        await page.wait_for_function('(selector)=>document.querySelectorAll(selector)[2].textContent==="（Codex）"', arg=selector)
                    assert '（Codex）' in await phone.locator('#title').inner_text()
                    assert all('powershell' not in text.lower() and 'cmd' not in text.lower() for text in await desktop.locator('.pane-title').all_text_contents())
                    # Repeated catalogs from an older host must preserve learned identity.
                    daemon.hub._catalog()
                    await asyncio.sleep(.05)
                    assert (await desktop.locator('#tree .session-shell').all_text_contents())[2] == '（Codex）'
                    assert (await phone.locator('#session-list .session-agent').all_text_contents())[2] == '（Codex）'
                    sessions[2].pty.inbox.put(b'\x1b]7501;app=unknown-tool;state=working\x07')
                    await asyncio.sleep(.05)
                    assert (await phone.locator('#session-list .session-agent').all_text_contents())[2] == '（Codex）'
                    sessions[2].pty.inbox.put(b'\x1b]7501;state=clear\x07')
                    for page, selector in [(desktop,'#tree .session-shell'), (phone,'#session-list .session-agent')]:
                        await page.wait_for_function('(selector)=>document.querySelectorAll(selector)[2].textContent==="（其他）"', arg=selector)
                    assert not any(session.pty.written for session in sessions)
                finally:
                    await browser.close()


async def _single_line(page, rows, name, agent, max_height):
    geometry = await page.locator(rows).evaluate_all('''(rows, selectors)=>rows.map(row=>{
      const name=row.querySelector(selectors.name),agent=row.querySelector(selectors.agent);
      const a=name.getBoundingClientRect(),b=agent.getBoundingClientRect(),r=row.getBoundingClientRect();
      return {height:r.height,aligned:Math.abs((a.top+a.bottom)-(b.top+b.bottom))<3,
        inside:b.right<=r.right,nowrap:getComputedStyle(name).whiteSpace,
        dot:!!row.querySelector('.session-dot')};
    })''', {'name':name, 'agent':agent})
    assert len(geometry) == 3
    for row in geometry:
        assert row['height'] <= max_height
        assert row['aligned'] and row['inside'] and row['dot']
        assert row['nowrap'] == 'nowrap'
