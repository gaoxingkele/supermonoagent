"""The phone discovers authorized relay computers without manual profiles."""
import asyncio
import json
from pathlib import Path
from urllib.parse import quote, urlsplit

import pytest

from test_workspace_auto_discovery import discovery_fixture

ROOT = Path(__file__).resolve().parents[1]
EDGE = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')


async def mobile_page(browser, page_url, relay_url, token, renderer='dom'):
    # Serve over a real loopback socket: synthetic fulfilled navigation has no
    # address space and Chromium correctly blocks its private-network sockets.
    from websockets.asyncio.server import serve
    from supermonoagent.web import text_response
    async def http(connection, request):
        name = urlsplit(request.path).path.removeprefix('/mobile/')
        path = ROOT / ('src/supermonoagent/static/' + name[7:] if name.startswith('vendor/') else 'android/assets/mobile/' + name)
        if not path.is_file():
            return text_response(connection, 404, 'Missing fixture asset', 'text/plain')
        mime = {'.css':'text/css','.html':'text/html'}.get(path.suffix,'text/javascript')
        return text_response(connection, 200, path.read_text('utf-8'), mime+'; charset=utf-8')
    async def reject(ws):
        await ws.close()
    server = await serve(reject, '127.0.0.1', 0, process_request=http)
    browser.on('disconnected', lambda: server.close())
    page = await browser.new_page(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
    page.set_default_timeout(8000)
    origin = 'http://127.0.0.1:'+str(server.sockets[0].getsockname()[1])
    config = {'endpoint': relay_url.replace('ws://', 'http://').removesuffix('/ws'), 'code': 'home-room', 'token': token, 'renderer': renderer}
    url = origin + '/mobile/index.html#' + quote(json.dumps(config))
    await page.goto(url)
    await page.wait_for_function('window.MobileTerminal?.ready')
    return page, url


@pytest.mark.parametrize('renderer', ['dom', 'webgl'])
def test_phone_popup_automatically_tracks_same_token_computers_and_windows(renderer):
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Local Edge required')
    asyncio.run(check_lifecycle(playwright, renderer))


async def check_lifecycle(playwright, renderer):
    async with discovery_fixture() as (local, remote, private, relay, relay_url, page_url, windows, sessions, start):
        await start(remote, 'new-room')
        baseline_sizes = [(session.cols, session.rows) for session in sessions]
        async with playwright.async_playwright() as runtime:
            browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
            try:
                page, url = await mobile_page(browser, page_url, relay_url, local.token, renderer)
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                baseline = await page.evaluate('MobileTerminal.activeKey')
                await page.locator('#draft').evaluate("e=>{e.value='unsent local draft';e.dispatchEvent(new Event('input'));}")
                await page.locator('#sessions').tap()
                remote_row = page.locator(f'#session-list .session[data-session="{sessions[1].id}"]')
                await remote_row.wait_for()
                assert await page.locator('#session-list .session').count() == 2
                assert not await page.locator('#session-list').get_by_text('Private fixture', exact=False).count()
                assert await page.evaluate('MobileTerminal.activeKey') == baseline
                assert await page.locator('#draft').input_value() == 'unsent local draft'
                assert not any(session.pty.written for session in sessions)
                assert [(session.cols, session.rows) for session in sessions] == baseline_sizes
                assert not [h for h in json.loads(await page.evaluate("localStorage.getItem('sma.mobile.hosts')||'[]'")) if h.get('code') == 'new-room']
                assert 'new-room' not in await page.evaluate("sessionStorage.getItem('sma.mobile.credentials')||''")

                extra = remote.hub.open_window('Newly opened remote window', owner=remote.token)
                extra_session = remote.hub.open_cli(extra.id, 'cmd')
                extra_row = page.locator(f'#session-list .session[data-session="{extra_session.id}"]')
                await extra_row.wait_for()
                remote.hub.close_window(extra.id)
                await extra_row.wait_for(state='detached')

                await remote_row.tap()
                await page.wait_for_function('sid=>MobileTerminal.active===sid&&MobileTerminal.ready', arg=sessions[1].id)
                remote_key = await page.evaluate('MobileTerminal.activeKey')
                host_id = json.loads(remote_key)[0]
                await page.wait_for_function("id=>MobileTerminal.hosts.find(h=>h.id==='home')?.subscription===''&&MobileTerminal.hosts.find(h=>h.id===id)?.subscription===MobileTerminal.active", arg=host_id)
                state = await page.evaluate('MobileTerminal.captureUIState()')
                assert local.token not in json.dumps(state)
                await page.goto(url)
                await page.evaluate('state=>MobileTerminal.restoreUIState(state)', state)
                await page.wait_for_function('key=>MobileTerminal.activeKey===key&&MobileTerminal.ready', arg=remote_key)
                await page.evaluate('MobileTerminal.pause()')
                assert all(not h['connected'] for h in await page.evaluate('MobileTerminal.hosts'))
                await page.evaluate('MobileTerminal.resume()')
                await page.wait_for_function('key=>MobileTerminal.activeKey===key&&MobileTerminal.ready', arg=remote_key)

                await relay._replace_tokens(relay.rooms['new-room'], ['rotated-fixture-token'])
                await page.wait_for_function("id=>!MobileTerminal.hosts.some(h=>h.id===id)", arg=host_id)
                await page.wait_for_function("MobileTerminal.connected&&MobileTerminal.ready&&JSON.parse(MobileTerminal.activeKey)[0]==='home'")
                assert await page.locator('#draft').input_value() == 'unsent local draft'
                await page.locator('#sessions').tap()
                assert not await remote_row.count()
                assert not any(session.pty.written for session in sessions)
                assert [(session.cols, session.rows) for session in sessions] == baseline_sizes
                assert not errors, errors
                artifacts = ROOT / '.omx/artifacts/android-auto-discovery'
                artifacts.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(artifacts / f'{renderer}.png'))
            finally:
                await browser.close()


def test_phone_does_not_trust_unknown_rooms_without_discovery_capability():
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Local Edge required')
    asyncio.run(check_old_server(playwright))


async def check_old_server(playwright):
    async with discovery_fixture() as (local, remote, private, relay, relay_url, page_url, windows, sessions, start):
        await start(remote, 'new-room')
        snapshot = relay._snapshot
        def old_snapshot(rooms, tokens=()):
            value = json.loads(snapshot(rooms, tokens))
            value['capabilities'].pop('token_discovery', None)
            return json.dumps(value)
        relay._snapshot = old_snapshot
        async with playwright.async_playwright() as runtime:
            browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
            try:
                page, _ = await mobile_page(browser, page_url, relay_url, local.token)
                await page.locator('#sessions').tap()
                await page.wait_for_timeout(300)
                assert [h['id'] for h in await page.evaluate('MobileTerminal.hosts')] == ['home']
                assert await page.locator('#session-list .session').count() == 1
                assert not any(session.pty.written for session in sessions)
            finally:
                await browser.close()
