"""Paging controls must survive reconnect and application screen cleanup."""
import asyncio
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from test_client_integration import isolated_host

EDGE=Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')


@pytest.mark.parametrize('boundary',['reconnect','erase-scrollback'])
def test_history_buttons_keep_working(boundary):
    playwright=pytest.importorskip('playwright.async_api')
    if not EDGE.exists():pytest.skip('Hidden Edge required')
    asyncio.run(check_paging(playwright,boundary))


async def check_paging(playwright,boundary):
    async with isolated_host() as (daemon,_direct,_relay):
        window=daemon.hub.open_window('Paging fixture',owner=daemon.token)
        session=daemon.hub.open_cli(window.id,'cmd')
        async with serve(daemon.handle_socket,'127.0.0.1',0,process_request=daemon.serve_http) as server:
            async with playwright.async_playwright() as runtime:
                browser=await runtime.chromium.launch(executable_path=str(EDGE),headless=True)
                try:
                    page=await browser.new_page(viewport={'width':1100,'height':760})
                    await page.goto(f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/workspace?direct=1&renderer=dom#token={daemon.token}')
                    await page.wait_for_function('window.smaWorkspace?.connected&&smaWorkspace.activePane?.ready')
                    session.pty.inbox.put(('\r\n'+''.join(f'PAGING_ROW_{i:03d}\r\n' for i in range(200))).encode())
                    await page.wait_for_function('smaWorkspace.terminal.buffer.active.baseY>150&&!smaWorkspace.activePane.writing&&!smaWorkspace.activePane.frames.length')
                    await page.evaluate('window.originalPagingPane=smaWorkspace.activePane;window.originalPagingGeneration=smaWorkspace.activePane.host.generation')
                    if boundary=='reconnect':
                        for socket in list(server.connections):await socket.close(code=1012,reason='Test-owned reconnect')
                        await page.wait_for_function('smaWorkspace.connected&&smaWorkspace.activePane===originalPagingPane&&smaWorkspace.activePane.host.generation>originalPagingGeneration&&smaWorkspace.activePane.ready&&!smaWorkspace.activePane.replayPending&&!smaWorkspace.activePane.writing')
                    else:
                        session.pty.inbox.put(b'\x1b[3J\x1b[H\x1b[2JAFTER_SCREEN_CLEANUP')
                        await page.wait_for_function("smaWorkspace.terminal.buffer.active.getLine(smaWorkspace.terminal.buffer.active.baseY)?.translateToString(true).includes('AFTER_SCREEN_CLEANUP')")
                    assert await page.evaluate('smaWorkspace.terminal.buffer.active.baseY>100'), 'Application cleanup discarded every history row'
                    await page.get_by_role('button',name='向上翻动一页历史',exact=True).click()
                    await page.wait_for_function('smaWorkspace.terminal.buffer.active.viewportY<smaWorkspace.terminal.buffer.active.baseY',timeout=2000)
                    before=await page.evaluate('smaWorkspace.terminal.buffer.active.viewportY')
                    session.pty.inbox.put(b'\r\nFRESH_WHILE_READING')
                    await page.wait_for_function('!smaWorkspace.activePane.writing&&!smaWorkspace.activePane.frames.length')
                    await asyncio.sleep(.1)
                    assert await page.evaluate('smaWorkspace.terminal.buffer.active.viewportY')==before
                    await page.get_by_role('button',name='翻到最早的历史输出',exact=True).click()
                    await page.wait_for_function('smaWorkspace.terminal.buffer.active.viewportY===0')
                    await page.get_by_role('button',name='翻到最新输出并恢复自动跟随',exact=True).click()
                    await page.wait_for_function('smaWorkspace.activePane.followOutput&&smaWorkspace.terminal.buffer.active.viewportY===smaWorkspace.terminal.buffer.active.baseY')
                    assert not session.pty.written
                finally:
                    await browser.close()
