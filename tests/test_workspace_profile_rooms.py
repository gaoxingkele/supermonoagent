"""Address/token profiles and captured room actions, using test-owned PTYs only."""
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from websockets.asyncio.server import serve

from supermonoagent.daemon import Daemon, relay_uplink
from supermonoagent.protocol import unpack_json
from supermonoagent.relay import Relay
from test_client_integration import ControlledPty

EDGE = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')


async def eventually(predicate):
    async with asyncio.timeout(8):
        while not predicate():
            await asyncio.sleep(.01)


@asynccontextmanager
async def fixture(playwright):
    with tempfile.TemporaryDirectory(prefix='sma-profile-rooms-') as tmp, patch(
        'supermonoagent.daemon.STATE', Path(tmp)
    ), patch.dict(os.environ, {'SUPERMONO_STATE_DIR': tmp}):
        daemons, ops, windows, sessions = [], [[], []], [], []
        for index in range(2):
            daemon = Daemon('profile-owner-'+str(index))
            daemon.hub.persist = False
            daemon.hub.pty_factory = lambda *_: ControlledPty()
            daemon.hub.attach_loop(asyncio.get_running_loop())
            daemon.hub.set_identity('Profile computer '+str(index), 'Profile project')
            owned_windows = [daemon.hub.open_window('Room '+str(index)+' window '+str(n), owner=daemon.token) for n in range(2)]
            owned_sessions = [daemon.hub.open_cli(win.id, 'cmd') for win in owned_windows]
            original = daemon._op
            async def observe(client, op, payload, original=original, index=index):
                if op in {5, 6, 9, 16}:
                    ops[index].append((op, unpack_json(payload), client.key))
                return await original(client, op, payload)
            daemon._op = observe
            daemons.append(daemon); windows.append(owned_windows); sessions.append(owned_sessions)
        relay = Relay(development=True)
        wrong_endpoint_messages = []
        async def wrong_endpoint(ws):
            try:
                async for message in ws:
                    wrong_endpoint_messages.append(message)
            finally:
                await ws.close()
        jobs = []
        async with AsyncExitStack() as stack:
            server = await stack.enter_async_context(serve(relay.handler, '127.0.0.1', 0))
            page_server = await stack.enter_async_context(serve(daemons[0].handle_socket, '127.0.0.1', 0, process_request=daemons[0].serve_http))
            trap = await stack.enter_async_context(serve(wrong_endpoint, '127.0.0.1', 0))
            endpoint = 'ws://127.0.0.1:'+str(server.sockets[0].getsockname()[1])+'/ws'
            trap_endpoint = 'ws://127.0.0.1:'+str(trap.sockets[0].getsockname()[1])+'/ws'
            url = 'http://127.0.0.1:'+str(page_server.sockets[0].getsockname()[1])+'/workspace?direct=1&renderer=dom#token='+daemons[0].token
            for index, daemon in enumerate(daemons):
                jobs.append(asyncio.create_task(relay_uplink(daemon, endpoint, 'profile-room-'+str(index), 'profile-'+str(index))))
            await eventually(lambda: all(relay.rooms.get('profile-room-'+str(i)) and relay.rooms['profile-room-'+str(i)].host for i in range(2)))
            async with playwright.async_playwright() as runtime:
                browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
                try:
                    yield browser, url, endpoint, trap_endpoint, wrong_endpoint_messages, daemons, ops, windows, sessions
                finally:
                    await browser.close()
                    for job in jobs:
                        job.cancel()
                    await asyncio.gather(*jobs, return_exceptions=True)
                    for daemon in daemons:
                        for wid in list(daemon.hub.windows):
                            daemon.hub.close_window(wid)


async def open_profiles(page):
    if not await page.locator('#relay-profiles-dialog').evaluate('(d)=>d.open'):
        await page.locator('#connect-button').click()
    await page.locator('#relay-profiles-dialog').wait_for(state='visible')


async def add_profile(page, endpoint, token, remember=False):
    await open_profiles(page)
    parts = urlsplit(endpoint)
    await page.locator('#relay-profile-address').fill('127.0.0.1')
    await page.locator('#relay-profile-port').fill(str(parts.port))
    await page.locator('#relay-profile-token').fill(token)
    await page.locator('#relay-profile-remember').set_checked(remember)
    await page.locator('#relay-profile-form button[type=submit]').click()
    await page.locator('#relay-profile-connect').wait_for(state='visible')
    await page.wait_for_function("!document.querySelector('#relay-profile-connect').disabled")
    await page.locator('#relay-profile-connect').click()
    assert await page.locator('#relay-profile-error').inner_text() == ''
    assert await page.locator('#relay-profile-token').input_value() == token


async def server_barrier(page, host):
    await page.evaluate("""id=>new Promise(resolve=>{
      const ws=smaWorkspace.connections.get(id).ws,stamp='profile-test-barrier';
      const receive=event=>{if(typeof event.data==='string')return;
        const f=TerminalClient.unpack(new Uint8Array(event.data));
        if(f.op===11&&new TextDecoder().decode(f.payload)===stamp){ws.removeEventListener('message',receive);resolve();}};
      ws.addEventListener('message',receive);ws.send(TerminalClient.pack(10,new TextEncoder().encode(stamp)));
    })""", host)


async def wait_room(page, index):
    await page.wait_for_function("code=>[...smaWorkspace.connections.values()].some(h=>h.config.code===code&&h.connected&&h.catalog.length===2)", arg='profile-room-'+str(index))


async def room_menu(page, index):
    code = 'profile-room-'+str(index)
    host = await page.evaluate("code=>[...smaWorkspace.connections.values()].find(h=>h.config.code===code).id", code)
    await page.locator('#tree section').filter(has=page.locator('.workspace-host-edit')).evaluate_all("(sections,id)=>sections.find(s=>s.dataset.host===id).querySelector('.workspace-host-edit').click()", host)
    await page.locator('.room-menu').wait_for(state='visible')
    return host


def run(check):
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Local Edge required')
    asyncio.run(check(playwright))


def test_profiles_discover_multiple_tokens_deduplicate_and_remove_only_own_rooms():
    run(_check_profiles)


async def _check_profiles(playwright):
    async with fixture(playwright) as (browser, url, endpoint, _trap, _messages, daemons, _ops, _windows, sessions):
        page = await browser.new_page(viewport={'width': 1200, 'height': 800})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.goto(url)
        await page.wait_for_function('window.smaWorkspace?.connected')
        for index in range(2):
            await add_profile(page, endpoint, daemons[index].token)
            await wait_room(page, index)
        await add_profile(page, endpoint, daemons[1].token)
        assert await page.locator('.relay-profile-row').count() == 2
        assert await page.locator('#relay-profiles-dialog #relay-code').count() == 0
        stored = await page.evaluate('JSON.stringify({...localStorage})')
        assert all(daemon.token not in stored for daemon in daemons)
        assert 'profile-room-' not in await page.evaluate('localStorage.getItem("sma.workspace.relay-profiles")')
        assert await page.evaluate('Object.keys(sessionStorage).filter(k=>k.startsWith("sma.workspace.profile-token.")&&!k.endsWith(".target")).length') == 2
        await page.locator('#relay-profiles-close').click()
        await page.locator('#new-project').click()
        assert await page.locator('#relay-profiles-dialog').evaluate('(d)=>d.open')
        await page.locator('#relay-profiles-close').click()
        await page.locator('#app-menu-toggle').click()
        await page.locator('#menu-connect').click()
        await page.locator('#relay-profiles-dialog summary').click()
        await page.locator('#relay-profiles-advanced').click()
        assert await page.locator('#connection-dialog').evaluate('(d)=>d.open')
        await page.locator('#connection-close').click()
        await page.reload()
        for index in range(2):
            await wait_room(page, index)
        await page.evaluate("sid=>{const h=[...smaWorkspace.connections.values()].find(h=>h.config.code==='profile-room-1');window.healthyProfileHost=h;smaWorkspace.openHostSession(h.id,sid,false);window.healthyProfilePane=smaWorkspace.panes.get(smaWorkspace.activeSession);window.staleProfileCallback=smaWorkspace.directories.values().next().value.ws.onmessage;}", sessions[1][0].id)
        await page.wait_for_function('!!healthyProfilePane')
        await open_profiles(page)
        await page.locator('.relay-profile-row').nth(0).locator('button').click()
        await page.wait_for_function("![...smaWorkspace.connections.values()].some(h=>h.config.code==='profile-room-0')")
        await page.wait_for_function("[...smaWorkspace.connections.values()].some(h=>h===healthyProfileHost&&h.connected)&&smaWorkspace.panes.get(healthyProfilePane.sid)===healthyProfilePane", timeout=4000)
        await page.evaluate("staleProfileCallback({data:JSON.stringify({op:'directory',revision:99999,capabilities:{token_discovery:true},hosts:[{code:'profile-room-0',online:true,token_index:0,machine:'Stale room'}]})})")
        assert not await page.evaluate("[...smaWorkspace.connections.values()].some(h=>h.config.code==='profile-room-0')")
        await add_profile(page, endpoint, daemons[1].token, remember=True)
        assert daemons[1].token in await page.evaluate('JSON.stringify({...localStorage})')
        await page.locator('.relay-profile-row').locator('button').click()
        stored = await page.evaluate('JSON.stringify({...localStorage})')
        assert all(daemon.token not in stored for daemon in daemons)
        assert all(session.id in daemon.hub.sessions for daemon, owned in zip(daemons, sessions) for session in owned)
        assert not errors


def test_profile_tokens_bound_to_endpoint_and_public_plaintext_refused():
    run(_check_binding)


async def _check_binding(playwright):
    async with fixture(playwright) as (browser, url, endpoint, trap, messages, daemons, *_):
        page = await browser.new_page()
        values = {'id': 'test-bound-profile', 'endpoint': trap}
        await page.add_init_script("localStorage.setItem('sma.workspace.relay-profiles',"+json.dumps(json.dumps([values]))+");sessionStorage.setItem('sma.workspace.profile-token.test-bound-profile',"+json.dumps(daemons[1].token)+");sessionStorage.setItem('sma.workspace.profile-token.test-bound-profile.target',"+json.dumps(endpoint)+");")
        await page.goto(url)
        await page.wait_for_function('window.smaWorkspace?.connected')
        await open_profiles(page)
        assert '需要填写 Token' in await page.locator('.relay-profile-row').inner_text()
        assert not messages
        await page.locator('#relay-profile-address').fill('http://203.0.113.9')
        await page.locator('#relay-profile-port').fill('8817')
        await page.locator('#relay-profile-token').fill(daemons[1].token)
        await page.locator('#relay-profile-form button[type=submit]').click()
        assert await page.locator('#relay-profile-error').inner_text()
        assert await page.locator('.relay-profile-row').count() == 1
        assert not messages


def test_room_actions_capture_target_windows_and_ignore_stale_generations():
    run(_check_room_actions)


async def _check_room_actions(playwright):
    async with fixture(playwright) as (browser, url, endpoint, _trap, _messages, daemons, ops, _windows, sessions):
        page = await browser.new_page(viewport={'width': 1200, 'height': 800})
        await page.goto(url)
        await page.wait_for_function('window.smaWorkspace?.connected')
        for index in range(2):
            await add_profile(page, endpoint, daemons[index].token)
            await wait_room(page, index)
        await page.locator('#relay-profiles-close').click()
        host = await room_menu(page, 0)
        await page.get_by_role('menuitem', name='新增窗口', exact=True).click()
        await eventually(lambda: len(daemons[0].hub.windows) == 3)
        assert len(daemons[1].hub.windows) == 2
        await page.wait_for_function('id=>smaWorkspace.connections.get(id).catalog.length===3', arg=host)
        await page.evaluate('([id,sid])=>smaWorkspace.openHostSession(id,sid,false)', [host, sessions[0][0].id])
        before = await page.evaluate('smaWorkspace.metrics.snapshots')
        await room_menu(page, 0)
        await page.get_by_role('menuitem', name='刷新所有窗口', exact=True).click()
        await page.wait_for_function('n=>smaWorkspace.metrics.snapshots>n', arg=before, timeout=4000)
        assert any(op==16 and body.get('refresh') is True for op, body, _ in ops[0])
        closed_before = [len([op for op, _, _ in log if op==9]) for log in ops]
        page.once('dialog', lambda dialog: dialog.dismiss())
        await room_menu(page, 0)
        await page.get_by_role('menuitem', name='关闭所有窗口', exact=True).click()
        await server_barrier(page, host)
        assert [len([op for op, _, _ in log if op==9]) for log in ops] == closed_before
        assert len(daemons[0].hub.windows) == 3
        await room_menu(page, 0)
        await page.evaluate('id=>smaWorkspace.connections.get(id).generation++', host)
        await page.get_by_role('menuitem', name='新增窗口', exact=True).click()
        await server_barrier(page, host)
        assert len(daemons[0].hub.windows) == 3
        # Restore validity through a real reconnect, then capture only current windows.
        await page.evaluate('id=>{const h=smaWorkspace.connections.get(id);h.generation--;window.beforeRoomReconnect=h.generation;h.ws.close();}', host)
        await page.wait_for_function('id=>{const h=smaWorkspace.connections.get(id);return h?.connected&&h.generation>beforeRoomReconnect;}', arg=host)
        await room_menu(page, 0)
        captured = set(daemons[0].hub.windows)
        extra = daemons[0].hub.open_window('Created after menu opened', owner=daemons[0].token)
        daemons[0].hub.open_cli(extra.id, 'cmd')
        page.once('dialog', lambda dialog: dialog.accept())
        await page.get_by_role('menuitem', name='关闭所有窗口', exact=True).click()
        await eventually(lambda: not captured.intersection(daemons[0].hub.windows))
        assert set(daemons[0].hub.windows) == {extra.id}
        assert len(daemons[1].hub.windows) == 2
        assert not any(op==9 for op, _, _ in ops[1])
        assert not any(session.pty.written for owned in sessions for session in owned)
