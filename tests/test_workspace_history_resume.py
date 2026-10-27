import asyncio
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from test_client_integration import isolated_host


def test_desktop_reconnect_resumes_parsed_history_without_a_full_snapshot():
    playwright = pytest.importorskip('playwright.async_api')
    edge = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
    if not edge.exists():
        pytest.skip('Local Edge required')
    asyncio.run(_check(playwright, edge))


async def _check(playwright, edge):
    async with isolated_host() as (daemon, _direct, _relay):
        window = daemon.hub.open_window('Desktop resume', owner=daemon.token)
        session = daemon.hub.open_cli(window.id, 'cmd')
        native = daemon.hub.attach(asyncio.Queue(), token=daemon.token)
        native.multiplex = True
        daemon.hub.set_subscriptions(native, [session.id])
        daemon.hub.resize_session(native, session.id, 100, 28, True)
        session.pty.inbox.put(b'DESKTOP_RESUME_START\r\n')
        async with serve(daemon.handle_socket, '127.0.0.1', 0, process_request=daemon.serve_http) as server:
            async with playwright.async_playwright() as runtime:
                browser = await runtime.chromium.launch(executable_path=str(edge), headless=True)
                try:
                    page = await browser.new_page(viewport={'width': 1100, 'height': 760})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    port = server.sockets[0].getsockname()[1]
                    await page.goto(f'http://127.0.0.1:{port}/workspace?direct=1&renderer=dom#token={daemon.token}')
                    settled = '''()=>{const p=smaWorkspace.activePane;return p?.historyInitialized&&p.historyCursor
                        &&smaWorkspace.connected&&!p.writing&&!p.resizeRequest&&!p.frames.length&&!p.replayPending;}'''
                    await page.wait_for_function(settled)
                    before = await page.evaluate('''()=>({position:smaWorkspace.activePane.historyCursor.position,
                        snapshots:smaWorkspace.metrics.snapshots,parsed:smaWorkspace.metrics.parsedBytes})''')
                    sockets = [item[0] for item in list(daemon._direct_sockets.values())]
                    await asyncio.gather(*(socket.close(code=1012, reason='owned reconnect probe') for socket in sockets))
                    await page.wait_for_function('!smaWorkspace.connected')
                    session.pty.inbox.put(b'DESKTOP_RESUME_DELTA\r\n')
                    await page.wait_for_function(settled, timeout=10000)
                    await page.wait_for_function('''()=>{const b=smaWorkspace.terminal.buffer.active;
                        for(let i=0;i<b.length;i++)if(b.getLine(i)?.translateToString(true).includes('DESKTOP_RESUME_DELTA'))return true;return false;}''')
                    after = await page.evaluate('''()=>({position:smaWorkspace.activePane.historyCursor.position,
                        snapshots:smaWorkspace.metrics.snapshots,parsed:smaWorkspace.metrics.parsedBytes,
                        resumes:smaWorkspace.metrics.historyResumes,recoveries:smaWorkspace.metrics.historyRecoveries,
                        start:(()=>{const b=smaWorkspace.terminal.buffer.active;for(let i=0;i<b.length;i++)if(b.getLine(i)?.translateToString(true).includes('DESKTOP_RESUME_START'))return true;return false;})()})''')
                    assert after['position'] > before['position']
                    assert after['snapshots'] == before['snapshots']
                    assert after['resumes'] == 1
                    assert after['recoveries'] == 0
                    assert after['start'] is True
                    assert after['parsed'] - before['parsed'] < 4096
                    assert not errors, errors
                finally:
                    await browser.close()
