"""Runtime private defaults and explicit room scopes on the actual workspace."""
import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from websockets.asyncio.server import serve

from supermonoagent import ui_updates
from supermonoagent.frontend import Frontend
from supermonoagent.daemon import Daemon,relay_uplink
from test_client_integration import isolated_host,ControlledPty
from test_workspace_profile_rooms import fixture,add_profile,wait_room,eventually

EDGE=Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')


def test_owner_default_prefills_only_trusted_target_and_refreshes_file(tmp_path):
    playwright=pytest.importorskip('playwright.async_api')
    if not EDGE.exists():pytest.skip('Hidden Edge required')
    asyncio.run(check_default(playwright,tmp_path))


async def check_default(playwright,tmp_path):
    async with isolated_host() as (daemon,_direct,_relay):
        source=tmp_path/'token.txt';source.write_text('private-default-fixture-one')
        owner=tmp_path/'owner-token';owner.write_text(daemon.token)
        async with serve(daemon.handle_socket,'127.0.0.1',0,process_request=daemon.serve_http) as backend:
            app=Frontend(f'http://127.0.0.1:{backend.sockets[0].getsockname()[1]}',tmp_path/'themes.json',default_token_file=source,owner_token_file=owner)
            with patch.object(ui_updates,'fetch_manifest',side_effect=OSError('Fixture: no network')):
                async with serve(app.handler,'127.0.0.1',0,process_request=app.serve_http) as server:
                    async with playwright.async_playwright() as runtime:
                        browser=await runtime.chromium.launch(executable_path=str(EDGE),headless=True)
                        try:
                            page=await browser.new_page()
                            await page.goto(f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/workspace?direct=1#token={daemon.token}')
                            async def wait(expression):
                                async with asyncio.timeout(5):
                                    while not await page.evaluate(expression):await asyncio.sleep(.03)
                            await wait('window.smaWorkspace?.connected')
                            await page.locator('#connect-button').click()
                            await wait("document.querySelector('#relay-profile-token').value==='private-default-fixture-one'")
                            assert 'private-default-fixture' not in await page.evaluate('JSON.stringify(localStorage)+JSON.stringify(sessionStorage)')
                            await page.locator('#relay-profile-address').fill('untrusted.invalid')
                            assert await page.locator('#relay-profile-token').input_value()==''
                            await page.locator('#relay-profiles-close').click()
                            source.write_text('private-default-fixture-two')
                            await page.locator('#connect-button').click()
                            await wait("document.querySelector('#relay-profile-token').value==='private-default-fixture-two'")
                            await page.locator('#relay-profile-token').fill('manual-fixture-value')
                            source.write_text('private-default-fixture-three');daemon.hub._catalog()
                            await asyncio.sleep(.15)
                            assert await page.locator('#relay-profile-token').input_value()=='manual-fixture-value'
                            await page.locator('#relay-profiles-close').click()
                            source.unlink()
                            await page.locator('#connect-button').click()
                            await wait("document.querySelector('#relay-profile-token').value===''")
                        finally:
                            await browser.close();await app.close_updates()


def test_explicit_room_limits_discovery_and_credential_binding():
    playwright=pytest.importorskip('playwright.async_api')
    if not EDGE.exists():pytest.skip('Hidden Edge required')
    asyncio.run(check_scope(playwright))


async def check_scope(playwright):
    async with fixture(playwright) as (browser,url,endpoint,_trap,_messages,daemons,_ops,_windows,_sessions):
        extra=Daemon(daemons[0].token);extra.hub.persist=False;extra.hub.pty_factory=lambda *_:ControlledPty();extra.hub.attach_loop(asyncio.get_running_loop())
        win=extra.hub.open_window('Scoped extra fixture',owner=extra.token);extra.hub.open_cli(win.id,'cmd')
        job=asyncio.create_task(relay_uplink(extra,endpoint,'profile-room-extra','extra'))
        try:
            await eventually(lambda:endpoint in extra.relay_live)
            page=await browser.new_page();await page.goto(url)
            await page.wait_for_function('window.smaWorkspace?.connected')
            await page.locator('#connect-button').click()
            await page.locator('#relay-profile-room').fill('profile-room-0')
            await add_profile(page,endpoint,daemons[0].token)
            await wait_room(page,0)
            await page.wait_for_function("[...smaWorkspace.directories.values()].some(d=>d.phase==='connected')")
            await asyncio.sleep(.15)
            assert not await page.evaluate("[...smaWorkspace.connections.values()].some(h=>h.config.code==='profile-room-extra')")
            await page.evaluate("() => {const p=JSON.parse(localStorage.getItem('sma.workspace.relay-profiles'));p[0].code='profile-room-extra';localStorage.setItem('sma.workspace.relay-profiles',JSON.stringify(p));}")
            await page.reload();await page.wait_for_function('window.smaWorkspace?.connected')
            assert not await page.evaluate('[...smaWorkspace.connections.values()].some(h=>h.autoDirectory)')
        finally:
            job.cancel();await asyncio.gather(job,return_exceptions=True);extra.hub.close_window(win.id)
