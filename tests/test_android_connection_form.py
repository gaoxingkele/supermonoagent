"""Connection input UI only; real auth/crypto/room scope are exercised by JVM and relay suites."""
import asyncio
import json
from pathlib import Path
from urllib.parse import quote,urlsplit
import pytest
ROOT=Path(__file__).resolve().parents[1]
EDGE=Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')


def test_mobile_advanced_fields_accept_ip_port_and_clear_cross_endpoint_token():
    playwright=pytest.importorskip('playwright.async_api')
    if not EDGE.exists():pytest.skip('Local Edge required')
    async def check():
        async with playwright.async_playwright() as runtime:
            browser=await runtime.chromium.launch(executable_path=str(EDGE),headless=True)
            try:
                page=await browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
                errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                await page.add_init_script('''window.formSockets=[];window.WebSocket=class {constructor(url){this.url=url;this.readyState=0;formSockets.push(this);queueMicrotask(()=>{this.readyState=3;this.onclose?.({code:4001});});}send(){}close(){this.readyState=3;}};''')
                origin='http://127.0.0.1:54321'
                async def asset(route):
                    name=urlsplit(route.request.url).path.removeprefix('/mobile/')
                    path=ROOT/('src/supermonoagent/static/'+name[7:] if name.startswith('vendor/') else 'android/assets/mobile/'+name)
                    await route.fulfill(status=200,body=path.read_text('utf-8'),content_type={'.css':'text/css','.html':'text/html','.json':'application/json'}.get(path.suffix,'text/javascript'))
                await page.route(origin+'/mobile/**',asset)
                config={'endpoint':'https://192.0.2.1','token':'initial-fixture'}
                await page.goto(origin+'/mobile/index.html#'+quote(json.dumps(config)));await page.wait_for_function('window.MobileTerminal')
                async def add(address,port,name):
                    await page.locator('#sessions').click();await page.locator('#host-add').click();await page.locator('#host-name').fill(name)
                    await page.locator('#host-port').fill(port);await page.locator('#host-endpoint').fill(address)
                await add('192.0.2.10','443','First')
                await page.locator('#host-token').fill('field-token-A');await page.locator('#host-endpoint').fill('192.0.2.20');assert await page.locator('#host-token').input_value()==''
                await page.locator('#host-token').fill('field-token-B');await page.locator('#host-port').fill('8443');assert await page.locator('#host-token').input_value()==''
                await page.locator('#host-token').fill('field-token-C');await page.locator('#host-form button[type=submit]').click()
                saved=await page.evaluate('JSON.parse(localStorage.getItem("sma.mobile.hosts"))');assert saved[0]['endpoint']=='https://192.0.2.20:8443' and 'field-token' not in json.dumps(saved)
                await add('https://192.0.2.30:9443/relay','443','Full URL');assert await page.locator('#host-port').input_value()=='9443' and await page.locator('#host-port').is_disabled()
                await page.locator('#host-token').fill('url-fixture');await page.locator('#host-form button[type=submit]').click()
                saved=await page.evaluate('JSON.parse(localStorage.getItem("sma.mobile.hosts"))');assert saved[1]['endpoint']=='https://192.0.2.30:9443/relay'
                await add('2001:db8::1','443','IPv6');assert not await page.locator('#host-port').is_disabled()
                await page.locator('#host-form button[type=submit]').click();saved=await page.evaluate('JSON.parse(localStorage.getItem("sma.mobile.hosts"))');assert saved[2]['endpoint']=='https://[2001:db8::1]'
                await add('https://user:secret@192.0.2.40','443','Rejected');count=await page.evaluate('formSockets.length');await page.locator('#host-form button[type=submit]').click()
                assert await page.evaluate('formSockets.length')==count and await page.locator('#host-error').is_visible()
                assert not errors,errors
            finally:await browser.close()
    asyncio.run(check())


def test_native_start_and_web_settings_share_the_same_unfiltered_connection_flow():
    source=(ROOT/'android/src/dev/supermonoagent/mobile/MainActivity.java').read_text('utf-8')
    assert 'if ("/settings".equals(target.getPath())) { showConnection(); return true; }' in source
    assert 'ConnectionEndpoint.displayAddress(savedEndpoint)' in source and 'ConnectionEndpoint.displayPort(savedEndpoint)' in source
    assert 'EditText code=input(advanced,"手动房间（高级，可留空）","",false)' in source
    assert 'ConnectionProbe.discover(endpoint,accessToken)' in source
    assert 'check.setTextColor(Color.WHITE);check.setChecked(true)' in source
    assert 'connection.put("discoverAllRooms",true)' in source and 'connection.put("excludedRoomCodes",excludedRoomCodes)' in source
    assert 'persistCredentials.run();credentialScope[0]=next' in source
    assert 'connection.optBoolean("discoverAllRooms",false)' in source
    assert '(!dynamic||source.contains("SMA_ROOM_SELECTION_V2"))' in source
