"""The desktop ignores stale saved credentials for direct IPv4 loopback."""

import asyncio
import sys
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from supermonoagent.frontend import Frontend
from test_client_integration import isolated_host
from test_frontend import wait_ui


def test_close_denial_from_source_preserves_visible_session():
    from supermonoagent.pty import PtyCloseError
    from test_workspace_profile_rooms import EDGE, run
    from unittest.mock import patch

    async def check(playwright):
        async with isolated_host() as (daemon, _direct, _relay):
            window = daemon.hub.open_window('Protected fixture', owner=daemon.token)
            session = daemon.hub.open_cli(window.id, 'cmd')
            async with serve(daemon.handle_socket, '127.0.0.1', 0, process_request=daemon.serve_http) as server:
                async with playwright.async_playwright() as runtime:
                    browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
                    try:
                        page = await browser.new_page()
                        await page.goto(f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/workspace?direct=1#token={daemon.token}')
                        await wait_ui(page, 'smaWorkspace?.activeHost?.capabilities?.close_ack===1')
                        with patch.object(session.pty, 'close', side_effect=PtyCloseError('process_still_alive')):
                            await page.get_by_role('button', name='结束后台会话', exact=True).click()
                            await page.locator('#terminate-form .danger').click()
                            await page.get_by_text('后台尚未确认进程退出，会话仍保留。',exact=True).wait_for(timeout=3000)
                            assert session.id in daemon.hub.sessions
                            assert await page.locator('#terminate-dialog').is_visible()
                    finally:
                        await browser.close()
    run(check)


@pytest.mark.skipif(sys.platform != 'win32', reason='Requires a Windows process handle')
def test_end_button_exits_only_the_confirmed_powershell(tmp_path):
    import _winapi
    import shutil
    from contextlib import ExitStack
    from supermonoagent.pty import WinPty
    from test_workspace_profile_rooms import EDGE, run

    async def check(playwright):
        async with isolated_host(real_shell=True) as (daemon, _direct, _relay):
            executable = shutil.which('powershell.exe')
            assert executable
            daemon.hub.pty_factory = lambda shell, cwd: WinPty([executable, '-NoLogo', '-NoProfile'], cwd)
            with ExitStack() as handles:
                sessions, process_handles = [], []
                for name in ('Selected PowerShell', 'Other PowerShell'):
                    win = daemon.hub.open_window(name, owner=daemon.token)
                    session = daemon.hub.open_cli(win.id, 'powershell', str(tmp_path))
                    handle = _winapi.OpenProcess(_winapi.SYNCHRONIZE, False, session.pty._proc.pid)
                    handles.callback(_winapi.CloseHandle, handle)
                    sessions.append(session)
                    process_handles.append(handle)
                async with serve(daemon.handle_socket, '127.0.0.1', 0, process_request=daemon.serve_http) as server:
                    async with playwright.async_playwright() as runtime:
                        browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
                        try:
                            page = await browser.new_page()
                            await page.goto(f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/workspace?direct=1#token={daemon.token}')
                            await wait_ui(page, 'smaWorkspace?.connected && smaWorkspace.panes.size===2')
                            await page.evaluate('sid=>smaWorkspace.openSession(sid,false)', sessions[0].id)
                            end = page.locator('.pane.active').get_by_role('button', name='结束后台会话', exact=True)
                            await end.click()
                            assert 'Selected PowerShell' in await page.locator('#terminate-dialog p').inner_text()
                            await page.locator('#terminate-cancel').click()
                            assert all(_winapi.WaitForSingleObject(h, 0) == _winapi.WAIT_TIMEOUT for h in process_handles)
                            await end.click()
                            await page.locator('#terminate-form .danger').click()
                            assert await asyncio.to_thread(_winapi.WaitForSingleObject, process_handles[0], 5000) == _winapi.WAIT_OBJECT_0
                            await wait_ui(page, '!document.querySelector("#terminate-dialog").open && smaWorkspace.panes.size===1')
                            assert _winapi.WaitForSingleObject(process_handles[1], 0) == _winapi.WAIT_TIMEOUT
                            assert sessions[0].id not in daemon.hub.sessions and sessions[1].id in daemon.hub.sessions
                        finally:
                            await browser.close()

    run(check)


def test_workspace_direct_ignores_stale_token(tmp_path):
    playwright = pytest.importorskip('playwright.async_api')
    edge = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
    if not edge.exists():
        pytest.skip('Edge is unavailable')

    async def check():
        async with isolated_host() as (daemon, _direct, _relay):
            window = daemon.hub.open_window('sCodex', owner=daemon.token)
            session = daemon.hub.open_cli(window.id, 'cmd')
            owner = tmp_path / 'token'
            owner.write_text(daemon.token, encoding='utf-8')
            async with serve(daemon.handle_socket, '127.0.0.1', 0, process_request=daemon.serve_http) as backend:
                frontend = Frontend(f'http://127.0.0.1:{backend.sockets[0].getsockname()[1]}',
                                    tmp_path / 'themes.json', owner_token_file=owner)
                async with serve(frontend.handler, '127.0.0.1', 0, process_request=frontend.serve_http) as server:
                    async with playwright.async_playwright() as runtime:
                        browser = await runtime.chromium.launch(executable_path=str(edge), headless=True)
                        try:
                            page = await browser.new_page()
                            await page.add_init_script('''
                                window.sentHellos=[];
                                const original=WebSocket.prototype.send;
                                WebSocket.prototype.send=function(value){
                                    if(value instanceof Uint8Array && value[4]===1)
                                        sentHellos.push(JSON.parse(new TextDecoder().decode(value.slice(5))));
                                    return original.call(this,value);
                                };
                            ''')
                            await page.goto(f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/workspace?direct=1#token=stale-token')
                            await wait_ui(page, 'window.smaWorkspace?.connected && smaWorkspace.panes.size===1')
                            assert await page.evaluate('sentHellos.length') >= 1
                            assert await page.evaluate('sentHellos.every(hello=>!("token" in hello))')
                            assert await page.evaluate('smaWorkspace.activeHost.config.code') == ''
                            assert session.id in await page.evaluate('[...smaWorkspace.panes.values()].map(pane=>pane.raw)')
                            # A disconnected view must not pretend a kill request
                            # was sent. Once online, await the actual catalog ack.
                            await page.evaluate('smaWorkspace.activeHost.connected=false')
                            await page.get_by_role('button', name='结束后台会话', exact=True).click()
                            await page.locator('#terminate-form .danger').click()
                            assert await page.locator('#terminate-dialog').is_visible()
                            assert '结束指令未发送' in await page.locator('#terminate-dialog p').inner_text()
                            assert session.id in daemon.hub.sessions
                            await page.evaluate('smaWorkspace.activeHost.connected=true')
                            await page.locator('#terminate-form .danger').click()
                            await wait_ui(page, '!document.querySelector("#terminate-dialog").open && smaWorkspace.panes.size===0')
                            assert session.id not in daemon.hub.sessions
                        finally:
                            await browser.close()
    asyncio.run(check())
