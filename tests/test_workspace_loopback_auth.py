"""The desktop ignores stale saved credentials for direct IPv4 loopback."""

import asyncio
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from supermonoagent.frontend import Frontend
from test_client_integration import isolated_host
from test_frontend import wait_ui


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
