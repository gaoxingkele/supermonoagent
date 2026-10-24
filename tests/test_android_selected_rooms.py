"""Native checked-room selection bounds mobile relay connections."""
import asyncio
import json
from pathlib import Path
from urllib.parse import quote, urlsplit

import pytest
from websockets.asyncio.server import serve

from supermonoagent.web import text_response
from test_workspace_auto_discovery import discovery_fixture

ROOT = Path(__file__).resolve().parents[1]
EDGE = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')


async def selected_page(browser, relay_url, token, rooms, saved=None, credentials=None, selection=None):
    async def http(connection, request):
        name = urlsplit(request.path).path.removeprefix('/mobile/')
        path = ROOT / ('src/supermonoagent/static/' + name[7:] if name.startswith('vendor/') else 'android/assets/mobile/' + name)
        if not path.is_file():
            return text_response(connection, 404, 'Missing fixture asset', 'text/plain')
        mime = {'.css': 'text/css', '.html': 'text/html'}.get(path.suffix, 'text/javascript')
        return text_response(connection, 200, path.read_text('utf-8'), mime + '; charset=utf-8')
    async def reject(ws):
        await ws.close()
    server = await serve(reject, '127.0.0.1', 0, process_request=http)
    browser.on('disconnected', lambda: server.close())
    page = await browser.new_page(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
    page.set_default_timeout(8000)
    await page.add_init_script("""((value)=>{
      if(!localStorage.getItem('selected-room-test-initialized')){
        localStorage.setItem('sma.mobile.hosts',JSON.stringify(value.saved));
        sessionStorage.setItem('sma.mobile.credentials',JSON.stringify(value.credentials));
        localStorage.setItem('selected-room-test-initialized','1');
      }
    })(""" + json.dumps({'saved': saved or [], 'credentials': credentials or []}) + ");")
    endpoint = relay_url.replace('ws://', 'http://').removesuffix('/ws')
    config = {'endpoint': endpoint, 'token': token, 'code': 'private-room', 'renderer': 'dom', 'selectedRooms': rooms}
    config.update(selection or {})
    url = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/mobile/index.html#' + quote(json.dumps(config))
    return page, url


@pytest.mark.parametrize('all_checked', [False, True])
def test_checked_rooms_connect_and_unchecked_rooms_stay_excluded(all_checked):
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Local Edge required')
    asyncio.run(check_selection(playwright, all_checked))


async def check_selection(playwright, all_checked):
    async with discovery_fixture() as (local, remote, third, relay, relay_url, page_url, windows, sessions, start):
        await start(remote, 'new-room')
        third.token = third.hub.default_owner = third.hub.groups['default'].owner = local.token
        await relay._replace_tokens(relay.rooms['private-room'], [local.token])
        selected = ['home-room', 'new-room'] + (['private-room'] if all_checked else [])
        rooms = [{'code': code, 'machine': 'Selected ' + code, 'project': 'Fixture'} for code in selected]
        relay_endpoint = relay_url.replace('ws://', 'http://').removesuffix('/ws')
        local_endpoint = page_url.split('/workspace')[0]
        saved = [
            {'id': 'saved-unchecked', 'name': 'Saved relay profile', 'endpoint': relay_endpoint, 'code': 'private-room'},
            {'id': 'other-explicit', 'name': 'Other endpoint', 'endpoint': local_endpoint, 'code': ''},
        ]
        target = lambda h: json.dumps([h['endpoint'], h['code']], separators=(',', ':'))
        credentials = [[h['id'], target(h), local.token] for h in saved]
        sizes = [(s.cols, s.rows) for s in sessions]
        async with playwright.async_playwright() as runtime:
            browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
            try:
                page, url = await selected_page(browser, relay_url, local.token, rooms, saved, credentials)
                local.allowed_origins = (url.split('/mobile/')[0],)
                errors, phone_codes = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                def socket_created(ws):
                    def sent(payload):
                        if isinstance(payload, str):
                            value = json.loads(payload)
                            if value.get('op') == 'phone':
                                phone_codes.append(value['code'])
                    ws.on('framesent', sent)
                page.on('websocket', socket_created)
                await page.goto(url)
                await page.wait_for_function('MobileTerminal?.ready')
                assert await page.evaluate('MobileTerminal.roomSelectionVersion') == 1
                expected = len(selected) + 1  # The explicit different endpoint is retained.
                await page.wait_for_function('n=>MobileTerminal.hosts.length===n&&MobileTerminal.hosts.every(h=>h.connected&&h.sessions.length)', arg=expected)
                assert set(phone_codes) == set(selected)
                assert not any(h['id'] == 'saved-unchecked' for h in await page.evaluate('MobileTerminal.hosts'))
                assert any(h['id'] == 'other-explicit' for h in await page.evaluate('MobileTerminal.hosts'))
                assert all(not h['subscription'] for h in await page.evaluate('MobileTerminal.hosts') if h['id'] != 'home')
                assert await page.evaluate('JSON.parse(MobileTerminal.activeKey)[0]') == 'home'
                await page.locator('#draft').evaluate("e=>{e.value='unsent checked-room draft';e.dispatchEvent(new Event('input'));}")
                await page.locator('#sessions').click()
                await page.get_by_role('button', name='移除电脑 Other endpoint', exact=True).click()
                await page.locator('#sessions-close').click()
                assert json.loads(await page.evaluate("localStorage.getItem('sma.mobile.hosts')")) == [saved[0]]
                state = await page.evaluate('MobileTerminal.captureUIState()')
                assert local.token not in json.dumps(state)
                assert 'new-room' not in await page.evaluate("sessionStorage.getItem('sma.mobile.credentials')||''")
                await page.goto(url)
                await page.evaluate('value=>MobileTerminal.restoreUIState(value)', state)
                await page.wait_for_function('n=>MobileTerminal.ready&&MobileTerminal.hosts.length===n&&MobileTerminal.hosts.every(h=>h.connected)', arg=len(selected))
                assert await page.locator('#draft').input_value() == 'unsent checked-room draft'
                await page.evaluate('MobileTerminal.pause();MobileTerminal.resume()')
                await page.wait_for_function('n=>MobileTerminal.ready&&MobileTerminal.hosts.length===n&&MobileTerminal.hosts.every(h=>h.connected)', arg=len(selected))
                relay._publish()
                await page.locator('#sessions').click()
                await page.wait_for_timeout(200)
                assert await page.locator('#session-list .session').count() == len(selected)
                assert set(phone_codes) == set(selected)
                assert [(s.cols, s.rows) for s in sessions] == sizes
                assert not any(s.pty.written for s in sessions)
                assert not errors, errors
            finally:
                await browser.close()


@pytest.mark.parametrize('rooms', [[], [{'code': 'home-room'}, {'code': 'home-room'}], [{'code': 'x' * 129}]])
def test_invalid_native_selection_never_opens_a_transport(rooms):
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Local Edge required')
    asyncio.run(check_invalid(playwright, rooms))


async def check_invalid(playwright, rooms):
    async with playwright.async_playwright() as runtime:
        browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
        try:
            page, url = await selected_page(browser, 'ws://127.0.0.1:9/ws', 'fixture-only-token', rooms)
            sockets = []
            page.on('websocket', lambda socket: sockets.append(socket.url))
            await page.goto(url)
            assert await page.evaluate('typeof MobileTerminal') == 'undefined'
            assert not sockets
            assert '房间选择无效' in await page.locator('#status').text_content()
        finally:
            await browser.close()


@pytest.mark.parametrize('exclude_private',[False,True])
def test_native_dynamic_discovery_includes_new_rooms_but_never_reopens_excluded(exclude_private):
    playwright=pytest.importorskip('playwright.async_api')
    if not EDGE.exists():pytest.skip('Local Edge required')
    async def check():
        async with discovery_fixture() as (local,remote,private,relay,relay_url,page_url,windows,sessions,start):
            private.token=private.hub.default_owner=private.hub.groups['default'].owner=local.token
            await relay._replace_tokens(relay.rooms['private-room'],[local.token])
            async with playwright.async_playwright() as runtime:
                browser=await runtime.chromium.launch(executable_path=str(EDGE),headless=True)
                try:
                    selection={'discoverAllRooms':True,'excludedRoomCodes':['private-room'] if exclude_private else []}
                    page,url=await selected_page(browser,relay_url,local.token,[{'code':'home-room','machine':'Home'}],selection=selection)
                    phone_codes=[]
                    def socket_created(ws):
                        def sent(value):
                            if isinstance(value,str):
                                frame=json.loads(value)
                                if frame.get('op')=='phone':phone_codes.append(frame['code'])
                        ws.on('framesent',sent)
                    page.on('websocket',socket_created);await page.goto(url)
                    expected=1 if exclude_private else 2
                    await page.wait_for_function('count=>MobileTerminal?.ready&&MobileTerminal.hosts.length===count&&MobileTerminal.hosts.every(h=>h.connected)',arg=expected)
                    await start(remote,'new-room')
                    await page.wait_for_function('count=>MobileTerminal.hosts.length===count&&MobileTerminal.hosts.every(h=>h.connected)',arg=expected+1)
                    assert 'new-room' in phone_codes
                    assert ('private-room' in phone_codes) is not exclude_private
                    await page.evaluate('MobileTerminal.pause();MobileTerminal.resume()')
                    await page.wait_for_function('count=>MobileTerminal.ready&&MobileTerminal.hosts.length===count&&MobileTerminal.hosts.every(h=>h.connected)',arg=expected+1)
                    relay._publish();await page.wait_for_timeout(100)
                    assert ('private-room' in phone_codes) is not exclude_private
                    assert all(not h['subscription'] for h in await page.evaluate('MobileTerminal.hosts') if h['id']!='home')
                    assert not any(s.pty.written for s in sessions)
                finally:await browser.close()
    asyncio.run(check())


@pytest.mark.parametrize('selection',[
    {'discoverAllRooms':'true','excludedRoomCodes':[]},
    {'discoverAllRooms':True},
    {'discoverAllRooms':True,'excludedRoomCodes':['home-room']},
    {'discoverAllRooms':True,'excludedRoomCodes':['other','other']},
])
def test_invalid_dynamic_selection_cannot_bypass_home_or_open_transports(selection):
    playwright=pytest.importorskip('playwright.async_api')
    if not EDGE.exists():pytest.skip('Local Edge required')
    async def check():
        async with playwright.async_playwright() as runtime:
            browser=await runtime.chromium.launch(executable_path=str(EDGE),headless=True)
            try:
                page,url=await selected_page(browser,'ws://127.0.0.1:9/ws','fixture',[{'code':'home-room'}],selection=selection)
                sockets=[];page.on('websocket',lambda ws:sockets.append(ws.url));await page.goto(url)
                assert await page.evaluate('typeof MobileTerminal')=='undefined' and not sockets
            finally:await browser.close()
    asyncio.run(check())
