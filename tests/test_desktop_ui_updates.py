"""Loopback native update boundary, immutable snapshots and startup rollback."""
import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import io
import json
from pathlib import Path
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
from unittest.mock import patch
import zipfile

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import InvalidStatus, ConnectionClosed

from supermonoagent import ui_updates as U
from supermonoagent.frontend import Frontend, bundled_ui_files
from test_client_integration import isolated_host
from test_ui_updates import OPENSSL, sign

EDGE=Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
FEED='https://updates.example/updates/stable/manifest.json'


class NativeRecoveryNavigation:
    """Correlate native completion to recovery's new document, not SDK latches."""
    def __init__(self, window_id):
        self.window_id = window_id
        self.loaded = threading.Event()
        self._lock = threading.Lock()
        self._state = {'navigation_id': None, 'uri': None, 'completed_id': None,
                       'success': False}

    def starting(self, uri, navigation_id):
        query = parse_qs(urlsplit(uri).query)
        if query.get('native_window') != [self.window_id] or not query.get('ui_recovery'):
            return
        with self._lock:
            self._state = {'navigation_id': navigation_id, 'uri': uri,
                           'completed_id': None, 'success': False}
            self.loaded.clear()

    def completed(self, navigation_id, success):
        with self._lock:
            if self._state['navigation_id'] is None or navigation_id != self._state['navigation_id']:
                return
            self._state.update(completed_id=navigation_id, success=bool(success))
            if success:
                self.loaded.set()

    def snapshot(self):
        with self._lock:
            return dict(self._state)


def test_native_recovery_navigation_ignores_old_loaded_latch_and_navigation():
    witness = NativeRecoveryNavigation('owned')
    sdk_loaded = threading.Event()
    sdk_loaded.set()  # Initial load and a late old injection can keep this true.
    witness.completed(1, True)
    assert sdk_loaded.is_set() and not witness.loaded.is_set()
    witness.starting('http://127.0.0.1/workspace?native_window=other&ui_recovery=x', 2)
    witness.completed(2, True)
    assert not witness.loaded.is_set()
    witness.starting('http://127.0.0.1/workspace?native_window=owned&ui_recovery=x', 3)
    witness.completed(1, True)  # Old bad document finally completes after abort.
    assert not witness.loaded.is_set()
    witness.completed(3, False)
    assert not witness.loaded.is_set()
    witness.completed(3, True)
    assert witness.loaded.is_set() and witness.snapshot()['completed_id'] == 3
    witness.starting('http://127.0.0.1/workspace?native_window=owned&ui_recovery=y', 4)
    assert not witness.loaded.is_set()


def test_bundled_update_entry_is_visible_text():
    from html.parser import HTMLParser
    from supermonoagent.workspace import PAGE
    class Entry(HTMLParser):
        def __init__(self):super().__init__();self.ids={}
        def handle_starttag(self,tag,attrs):
            fields=dict(attrs)
            if fields.get('id'):self.ids[fields['id']]=fields
            if fields.get('id')=='check-updates':self.fields=fields
    entry=Entry();entry.feed(PAGE)
    assert 'hidden' not in entry.fields
    assert entry.fields['aria-label']=='检查更新'
    assert '>检查更新</button>' in PAGE
    assert 'hidden' in entry.ids['app-menu']
    assert entry.ids['app-menu-toggle']['aria-expanded']=='false'
    assert PAGE.index('id="sidebar-toggle"')<PAGE.index('id="app-menu-toggle"')<PAGE.index('class="wordmark"')
    assert entry.ids['relay-profile-token']['type']=='password'
    assert entry.ids['relay-profile-port']['type']=='number'
    assert entry.ids['agent-mode']['aria-label']=='Agent \u6e32\u67d3\u6a21\u5f0f'
    assert PAGE.index('id="agent-mode"')<PAGE.index('id="highlight-toggle"')
    for mode in ['auto','codex','claude-code','grok','kimi','pi-dev','cursor-cli']:
        assert f'<option value="{mode}">' in PAGE
    assert {'download-ui-update','menu-connect','relay-profiles-dialog','relay-profile-list','relay-profiles-advanced'}<=entry.ids.keys()


def test_native_bridge_upgrades_legacy_glyph_entry_and_opens_menu(tmp_path,signer):
    playwright=pytest.importorskip('playwright.async_api')
    if not EDGE.exists():pytest.skip('Local Edge required')
    asyncio.run(_check_legacy_entry(tmp_path,signer,playwright))


async def _check_legacy_entry(tmp_path,signer,playwright):
    import re
    legacy=bundled_ui_files()
    html=legacy['workspace.html'].decode()
    html=re.sub(r'<button id="check-updates"[^>]*>.*?</button>','',html,count=1)
    html=html.replace('<button id="connect-button"','<button id="check-updates" title="检查更新" aria-label="检查更新" hidden>↥</button><button id="connect-button"',1)
    legacy['workspace.html']=html.encode()
    legacy['workspace.css']=legacy['workspace.css'].replace(b'#check-updates{flex:none;width:auto;min-width:64px;padding:4px 7px;font-size:12px;white-space:nowrap}',b'#check-updates{flex:none;width:26px;min-width:26px;padding:3px;font-size:17px}')
    assert 'hidden>↥</button>'.encode() in legacy['workspace.html']
    with patch('supermonoagent.frontend.bundled_ui_files',return_value=legacy):
        async with frontend_fixture(tmp_path,signer) as (_frontend,_feed,daemon,_session,_other,origin):
            with patch.object(U,'fetch_manifest',side_effect=OSError('Isolated entry check')):
                async with playwright.async_playwright() as runtime:
                    browser=await runtime.chromium.launch(executable_path=str(EDGE),headless=True)
                    try:
                        page=await browser.new_page()
                        await page.goto(origin+'/workspace?direct=1&renderer=dom#token='+daemon.token)
                        await wait_ui(page,"window.NativeUpdates&&document.querySelector('#check-updates').textContent==='检查更新'")
                        for width,height in [(1100,760),(390,844)]:
                            await page.set_viewport_size({'width':width,'height':height})
                            trigger=page.locator('#check-updates')
                            assert await trigger.is_visible()
                            assert await trigger.inner_text()=='检查更新'
                            geometry=await trigger.evaluate("button=>{const r=button.getBoundingClientRect();return {width:r.width,right:r.right,textFits:button.scrollWidth<=button.clientWidth,font:getComputedStyle(button).fontSize,pageWidth:document.documentElement.scrollWidth};}")
                            assert geometry['width']>=64 and geometry['textFits'],geometry
                            assert geometry['right']<=width and geometry['pageWidth']<=width,geometry
                            assert geometry['font']=='12px',geometry
                            assert await page.locator('#connect-button').evaluate('button=>button.getBoundingClientRect().right<=innerWidth')
                            await trigger.click()
                            assert await page.locator('#native-updates-host').get_by_role('heading',name='界面更新').is_visible()
                            artifacts=Path(__file__).parents[1]/'.omx'/'artifacts'/'ui-update-entry'
                            artifacts.mkdir(parents=True,exist_ok=True)
                            await page.screenshot(path=str(artifacts/f'legacy-entry-{width}.png'))
                            await page.locator('#native-updates-host').get_by_role('button',name='关闭更新菜单').click()
                    finally:
                        await browser.close()


def test_menu_entries_simple_dialog_and_download_without_candidate(tmp_path,signer):
    playwright=pytest.importorskip('playwright.async_api')
    if not EDGE.exists():pytest.skip('Local Edge required')
    asyncio.run(_check_menu_and_profiles(tmp_path,signer,playwright))


def test_download_api_checks_first_and_reports_no_candidate(tmp_path,signer):
    playwright=pytest.importorskip('playwright.async_api')
    if not EDGE.exists():pytest.skip('Local Edge required')
    asyncio.run(_check_cold_download(tmp_path,signer,playwright))


async def _check_cold_download(tmp_path,signer,playwright):
    async with frontend_fixture(tmp_path,signer) as (_frontend,_feed,daemon,_session,_other,origin):
        with patch.object(U,'fetch_manifest',side_effect=OSError('Isolated cold download')) as checks,patch.object(U,'fetch_archive') as archive:
            async with playwright.async_playwright() as runtime:
                browser=await runtime.chromium.launch(executable_path=str(EDGE),headless=True)
                try:
                    page=await browser.new_page()
                    await page.goto(origin+'/workspace?direct=1&renderer=dom#token='+daemon.token)
                    await wait_ui(page,"document.querySelector('#native-updates-host')?.shadowRoot.querySelector('p').textContent.includes('Isolated cold download')")
                    before=checks.call_count
                    await page.evaluate('NativeUpdates.downloadAndInstall()')
                    assert checks.call_count==before+1
                    archive.assert_not_called()
                    assert await page.locator('#native-updates-host').get_by_role('heading',name='界面更新').is_visible()
                    assert 'Isolated cold download' in await page.locator('#native-updates-host').locator('p').inner_text()
                    assert not await page.locator('#download-ui-update').is_disabled()
                finally:
                    await browser.close()


async def _check_menu_and_profiles(tmp_path,signer,playwright):
    async with frontend_fixture(tmp_path,signer) as (_frontend,_feed,daemon,_session,_other,origin):
        with patch.object(U,'fetch_manifest',side_effect=OSError('Isolated menu: no release')) as checks,patch.object(U,'fetch_archive') as archive:
            async with playwright.async_playwright() as runtime:
                browser=await runtime.chromium.launch(executable_path=str(EDGE),headless=True)
                try:
                    page=await browser.new_page()
                    await page.goto(origin+'/workspace?direct=1&renderer=dom#token='+daemon.token)
                    await wait_ui(page,'window.NativeUpdates&&window.smaWorkspace?.connected')
                    artifacts=Path(__file__).parents[1]/'.omx'/'artifacts'/'ui-menu-profiles';artifacts.mkdir(parents=True,exist_ok=True)
                    for width,height in [(1100,760),(390,844)]:
                        await page.set_viewport_size({'width':width,'height':height})
                        toggle=page.locator('#app-menu-toggle');await toggle.click()
                        assert await toggle.get_attribute('aria-expanded')=='true'
                        assert await page.locator('#app-menu').is_visible()
                        for selector,text in [('#check-updates','检查更新'),('#download-ui-update','下载并更新'),('#menu-connect','连接工作区')]:
                            assert await page.locator(selector).is_visible()
                            assert await page.locator(selector).inner_text()==text
                        assert await page.evaluate("document.documentElement.scrollWidth<=innerWidth&&document.querySelector('#connect-button').getBoundingClientRect().right<=innerWidth")
                        await page.screenshot(path=str(artifacts/f'menu-{width}.png'))
                        await page.locator('#menu-connect').click()
                        assert await page.locator('#relay-profiles-dialog').is_visible()
                        await page.locator('#relay-profile-address').fill('127.0.0.1')
                        await page.locator('#relay-profile-port').fill('8817')
                        await page.locator('#relay-profile-token').fill('test-credential')
                        assert await page.locator('#relay-profile-token').get_attribute('type')=='password'
                        assert await page.locator('#relay-profiles-dialog').evaluate('dialog=>dialog.scrollWidth<=dialog.clientWidth&&dialog.getBoundingClientRect().right<=innerWidth')
                        await page.evaluate("""() => {const row=document.createElement('div');row.className='relay-profile-row';const address=document.createElement('span');address.textContent='wss://'+ 'long-address.'.repeat(30)+'example/ws';const status=document.createElement('small');status.textContent='Credential '+ 'long-label'.repeat(30);const remove=document.createElement('button');remove.textContent='Remove';row.append(address,status,remove);document.querySelector('#relay-profile-list').append(row);} """)
                        assert await page.locator('#relay-profile-list').evaluate('list=>list.scrollWidth<=list.clientWidth')
                        assert await page.locator('#agent-mode').evaluate('select=>select.getBoundingClientRect().right<=innerWidth')
                        await page.screenshot(path=str(artifacts/f'profiles-{width}.png'))
                        await page.locator('#relay-profiles-close').click()
                    await page.locator('#app-menu-toggle').click()
                    before=checks.call_count
                    await page.locator('#check-updates').click()
                    await wait_ui(page,"document.querySelector('#native-updates-host').shadowRoot.querySelector('p').textContent.includes('Isolated menu: no release')")
                    assert checks.call_count>before
                    await page.locator('#native-updates-host').get_by_role('button',name='关闭更新菜单').click()
                    await page.locator('#app-menu-toggle').click()
                    before=checks.call_count
                    await page.locator('#download-ui-update').click()
                    await wait_ui(page,"document.querySelector('#native-updates-host').shadowRoot.querySelector('p').textContent.includes('Isolated menu: no release')")
                    async with asyncio.timeout(3):
                        while checks.call_count<=before:await asyncio.sleep(.02)
                    archive.assert_not_called()
                    assert not await page.locator('#download-ui-update').is_disabled()
                finally:
                    await browser.close()


@pytest.fixture(scope='module')
def signer(tmp_path_factory):
    if not OPENSSL:
        pytest.skip('Publisher OpenSSL unavailable')
    private=tmp_path_factory.mktemp('desktop-update-signing')/'test-private.pem'
    subprocess.run([OPENSSL,'genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:3072','-out',str(private)],capture_output=True,check=True)
    der=subprocess.run([OPENSSL,'pkey','-in',str(private),'-pubout','-outform','DER'],capture_output=True,check=True).stdout
    return private,{'schema':1,'keys':{hashlib.sha256(der).hexdigest()[:16]:base64.b64encode(der).decode()}}


def candidate(signer, sequence, *, broken=False, missing_ready=False, native_api=U.NATIVE_API):
    private,trust=signer
    files=bundled_ui_files()
    files['workspace.html']=files['workspace.html'].replace(b'/workspace.js\"',b'/workspace.js?v=2#asset\"').replace(b'/xterm.css\"',b'/xterm.css#font\"')
    revision='desktop-ui-'+str(sequence)
    if broken:
        files['workspace.js']=b'throw new Error("BROKEN-UI-STARTUP");'
    elif missing_ready:
        files['workspace.js']=b'window.incompleteUI=true;'
    else:
        files['workspace.js']+=b'\nwindow.updateMarker='+json.dumps(revision).encode()+b';\n'
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as output:
        for name,data in files.items():
            output.writestr(name,data)
    archive=archive.getvalue()
    now=int(time.time())
    payload={'schema':1,'channel':'stable','revision':revision,'sequence':sequence,'created':now-5,'expires':now+3600,
        'artifacts':{'desktop':{'url':'releases/'+revision+'/desktop.zip','sha256':hashlib.sha256(archive).hexdigest(),'size':len(archive),'native_api':native_api,
            'files':{name:{'size':len(data),'sha256':hashlib.sha256(data).hexdigest()} for name,data in files.items()}}}}
    return sign(payload,private,trust),archive,payload,files


@asynccontextmanager
async def frontend_fixture(tmp_path, signer, *, backend_online=True):
    async with isolated_host() as (daemon,_direct,_relay):
        window=daemon.hub.open_window('Update-owned terminal',owner=daemon.token)
        session=daemon.hub.open_cli(window.id,'cmd')
        second=daemon.hub.open_window('Second update tab',owner=daemon.token)
        other=daemon.hub.open_cli(second.id,'cmd')
        async with serve(daemon.handle_socket,'127.0.0.1',0,process_request=daemon.serve_http) as backend:
            backend_url=f'http://127.0.0.1:{backend.sockets[0].getsockname()[1]}' if backend_online else 'http://127.0.0.1:1'
            frontend=Frontend(backend_url,tmp_path/'themes.json',update_root=tmp_path/'ui',update_feed=FEED,update_trust=signer[1])
            feed={'release':candidate(signer,1),'requests':[]}
            def manifest(url):
                feed['requests'].append(('manifest',url))
                assert url==FEED
                return feed['release'][0]
            def archive(payload,url,platform):
                feed['requests'].append(('archive',url,platform))
                assert url==FEED and platform=='desktop'
                assert payload==feed['release'][2]
                return feed['release'][1]
            with patch.object(U,'fetch_manifest',side_effect=manifest),patch.object(U,'fetch_archive',side_effect=archive):
                async with serve(frontend.handler,'127.0.0.1',0,process_request=frontend.serve_http) as server:
                    origin=f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
                    try:
                        yield frontend,feed,daemon,session,other,origin
                    finally:
                        await frontend.close_updates()


async def rpc(socket,frontend,op,**fields):
    await socket.send(json.dumps({'id':op,'op':op,'nonce':frontend.update_nonce,**fields}))
    while True:
        message=json.loads(await socket.recv())
        if message.get('id')==op:
            return message


async def wait_ui(page, expression, *, arg=None, timeout=30000):
    deadline=asyncio.get_running_loop().time()+timeout/1000
    while asyncio.get_running_loop().time()<deadline:
        try:
            if await page.evaluate(expression,arg):
                return
        except Exception as error:
            if not any(text in str(error) for text in ['Execution context was destroyed','Cannot find context','is not defined']):
                raise
        await asyncio.sleep(.02)
    raise AssertionError("UI condition timed out: "+expression)


async def get(url,headers=None):
    def read():
        with urlopen(Request(url,headers=headers or {}),timeout=5) as response:
            return response.read(),dict(response.headers)
    return await asyncio.to_thread(read)


def test_native_api_nonce_exact_origin_pinning_and_late_ack(tmp_path,signer):
    asyncio.run(_check_api(tmp_path,signer))


async def _check_api(tmp_path,signer):
    async with frontend_fixture(tmp_path,signer) as (frontend,feed,_daemon,_session,_other,origin):
        url=origin.replace('http:','ws:')+'/__updates/ws'
        with pytest.raises(InvalidStatus):
            async with connect(url,origin='https://hostile.example'):
                pass
        with pytest.raises(InvalidStatus):
            async with connect(url):
                pass
        async with connect(url,origin=origin) as socket:
            await socket.send(json.dumps({'id':1,'op':'activate','nonce':'wrong','prepared':'forged'}))
            with pytest.raises(ConnectionClosed):
                await socket.recv()
        assert frontend.update_store.status()['active_revision']=='bundled'
        html,headers=await get(origin+'/workspace')
        assert "script-src 'self'" in headers['Content-Security-Policy']
        assert "base-uri 'none'" in headers['Content-Security-Policy']
        assert 'unsafe-eval' not in headers['Content-Security-Policy']
        assert 'Content-Security-Policy' not in (await get(origin+'/'))[1]
        assert html.index(b'/__updates/native-updates.js')<html.index(b'/__ui/bundled/xterm.js')
        assert b'/__ui/bundled/workspace.js' in html
        bridge,_=await get(origin+'/__updates/native-updates.js')
        async with connect(url,origin=origin) as socket:
            result=await rpc(socket,frontend,'check')
            assert result['phase']=='available'
            requests=len(feed['requests'])
            await rpc(socket,frontend,'check',automatic=True)
            assert len(feed['requests'])==requests
            first=await rpc(socket,frontend,'download')
            assert first['ok'] and frontend.update_store.status()['active_revision']=='bundled'
            installed=await rpc(socket,frontend,'activate',prepared=first['prepared'])
            boot=installed['pending']
            assert installed['active_revision']=='desktop-ui-1'
            candidate_html,_=await get(origin+'/workspace')
            assert b'/__ui/desktop-ui-1/workspace.js' in candidate_html
            assert (await get(origin+'/__updates/native-updates.js'))[0]==bridge
            assert (await get(origin+'/__ui/bundled/workspace.js'))[0]==bundled_ui_files()['workspace.js']
            denied=await rpc(socket,frontend,'ready',ticket=boot['ticket'],revision='bundled',generation=boot['generation'])
            assert not denied['ok'] and frontend.update_store.status()['pending']
            good=await rpc(socket,frontend,'ready',ticket=boot['ticket'],revision=boot['revision'],generation=boot['generation'])
            assert good['ok'] and not good['pending']
            feed['release']=candidate(signer,2)
            await rpc(socket,frontend,'check')
            second=await rpc(socket,frontend,'download')
            await rpc(socket,frontend,'activate',prepared=second['prepared'])
            stale=await rpc(socket,frontend,'ready',ticket=boot['ticket'],revision=boot['revision'],generation=boot['generation'])
            assert not stale['ok']
            assert frontend.update_store.status()['active_revision']=='desktop-ui-2'
            rolled=await rpc(socket,frontend,'rollback')
            assert rolled['active_revision']=='desktop-ui-1'
            assert rolled['highest_sequence']==2
            feed['release']=candidate(signer,3,native_api=U.NATIVE_API+1)
            incompatible=await rpc(socket,frontend,'check')
            assert incompatible['phase']=='incompatible' and incompatible['available'] is None
            assert frontend.update_store.status()['active_revision']=='desktop-ui-1'


def test_good_update_ime_preserves_pty_then_broken_startup_rolls_back(tmp_path,signer):
    playwright=pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Local Edge required')
    asyncio.run(_check_browser(tmp_path,signer,playwright))


async def _check_browser(tmp_path,signer,playwright):
    async with frontend_fixture(tmp_path,signer) as (frontend,feed,daemon,session,other,origin):
        async with playwright.async_playwright() as runtime:
            browser=await runtime.chromium.launch(executable_path=str(EDGE),headless=True)
            try:
                page=await browser.new_page(viewport={'width':1100,'height':760})
                await page.goto(origin+'/workspace?direct=1&renderer=dom#token='+daemon.token)
                await wait_ui(page,'window.smaWorkspace?.connected&&smaWorkspace.panes.size===2&&window.NativeUpdates')
                await page.evaluate('id=>smaWorkspace.openSession(id,false)',session.id)
                await page.evaluate("smaWorkspace.split('horizontal')")
                active=await page.evaluate('smaWorkspace.activeSession')
                await page.evaluate("smaWorkspace.terminal.paste('draft-already-owned-by-PTY')")
                active_session=session if json.loads(active)[1]==session.id else other
                async with asyncio.timeout(3):
                    while active_session.pty.written!=b'draft-already-owned-by-PTY':
                        await asyncio.sleep(.01)
                await page.evaluate("smaWorkspace.terminal.textarea.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true,data:'pending'}))")
                await page.locator('#app-menu-toggle').click()
                await page.locator('#check-updates').click()
                menu=page.locator('#native-updates-host')
                artifacts=Path(__file__).parents[1]/'.omx'/'artifacts'/'ui-updates'
                artifacts.mkdir(parents=True,exist_ok=True)
                await menu.get_by_role('button',name='下载并更新',exact=True).wait_for(state='visible')
                await wait_ui(page,"!document.querySelector('#native-updates-host').shadowRoot.querySelector('#download').disabled")
                await page.screenshot(path=str(artifacts/'desktop-available-1100.png'))
                await menu.get_by_role('button',name='下载并更新',exact=True).click()
                async with asyncio.timeout(5):
                    while frontend.update_phase!='prepared':
                        await asyncio.sleep(.01)
                assert frontend.update_store.status()['active_revision']=='bundled'
                await page.wait_for_timeout(250)
                assert frontend.update_store.status()['active_revision']=='bundled'
                await page.evaluate("smaWorkspace.terminal.textarea.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true,data:'pending'}))")
                await wait_ui(page,"window.updateMarker==='desktop-ui-1'&&smaWorkspace.connected&&smaWorkspace.panes.size===2",timeout=15000)
                async with asyncio.timeout(5):
                    while frontend.update_store.status()['pending']:
                        await asyncio.sleep(.01)
                assert frontend.update_store.status()['active_revision']=='desktop-ui-1'
                assert await page.evaluate('smaWorkspace.activeSession')==active
                assert isinstance(await page.evaluate('smaWorkspace.layout'),dict)
                assert active_session.pty.written==b'draft-already-owned-by-PTY'
                assert set(daemon.hub.sessions)=={session.id,other.id}
                for width,height in [(390,844),(600,760),(1100,760)]:
                    await page.set_viewport_size({'width':width,'height':height})
                    assert await page.evaluate("document.querySelector('#connect-button').getBoundingClientRect().right<=innerWidth&&document.querySelector('#app-menu-toggle').getBoundingClientRect().right<=innerWidth&&document.documentElement.scrollWidth<=innerWidth")
                    await page.screenshot(path=str(artifacts/f'desktop-updated-{width}.png'))
                feed['release']=candidate(signer,2,broken=True)
                await page.evaluate('NativeUpdates.check()')
                await page.locator('#native-updates-host').get_by_role('button',name='下载并更新',exact=True).click()
                async with asyncio.timeout(12):
                    while 2 not in frontend.update_store.status()['failed_sequences']:
                        await asyncio.sleep(.02)
                await wait_ui(page,"window.updateMarker==='desktop-ui-1'&&smaWorkspace.connected",timeout=10000)
                assert frontend.update_store.status()['active_revision']=='desktop-ui-1'
                assert frontend.update_store.status()['highest_sequence']==2
                assert not frontend.update_candidate
                assert set(daemon.hub.sessions)=={session.id,other.id}
                assert active_session.pty.written==b'draft-already-owned-by-PTY'
                # Missing exports cannot acknowledge: immutable bridge + server deadline recover.
                feed['release']=candidate(signer,3,missing_ready=True)
                await page.evaluate('NativeUpdates.check()')
                await page.locator('#native-updates-host').get_by_role('button',name='下载并更新',exact=True).click()
                async with asyncio.timeout(25):
                    while 3 not in frontend.update_store.status()['failed_sequences']:
                        await asyncio.sleep(.05)
                await wait_ui(page,"window.updateMarker==='desktop-ui-1'&&smaWorkspace.connected",timeout=8000)
                assert frontend.update_store.status()['active_revision']=='desktop-ui-1'
                assert frontend.update_store.status()['highest_sequence']==3
                assert set(daemon.hub.sessions)=={session.id,other.id}
            finally:
                await browser.close()


def test_watchdog_without_page_and_offline_daemon_is_healthy_ui(tmp_path,signer):
    playwright=pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Local Edge required')
    asyncio.run(_check_offline_and_watchdog(tmp_path,signer,playwright))


async def _check_offline_and_watchdog(tmp_path,signer,playwright):
    async with frontend_fixture(tmp_path,signer,backend_online=False) as (frontend,feed,daemon,_session,_other,origin):
        # No document or JavaScript needed for timeout enforcement.
        frontend.update_store.install(feed['release'][0],feed['release'][1])
        boot=frontend.update_store.begin_boot()
        frontend._start_update_watchdog()
        with patch.object(U.time,'time',return_value=boot['deadline']+1):
            async with asyncio.timeout(2):
                while frontend.update_store.status()['pending']:
                    await asyncio.sleep(.02)
        assert frontend.update_store.status()['active_revision']=='bundled'
        feed['release']=candidate(signer,2)
        frontend.update_store.install(feed['release'][0],feed['release'][1])
        async with playwright.async_playwright() as runtime:
            browser=await runtime.chromium.launch(executable_path=str(EDGE),headless=True)
            try:
                page=await browser.new_page()
                await page.goto(origin+'/workspace?direct=1&renderer=dom#token='+daemon.token)
                await wait_ui(page,"window.updateMarker==='desktop-ui-2'&&window.NativeUpdates")
                async with asyncio.timeout(5):
                    while frontend.update_store.status()['pending']:
                        await asyncio.sleep(.01)
                assert frontend.update_store.status()['active_revision']=='desktop-ui-2'
                assert not await page.evaluate('smaWorkspace.connected')
            finally:
                await browser.close()


def test_native_recovery_uses_no_page_javascript_and_drops_url_credentials():
    from types import SimpleNamespace
    from supermonoagent.desktop import NativeUpdateRecovery
    import threading
    class Event:
        def __init__(self): self.handlers=[]
        def __iadd__(self,callback): self.handlers.append(callback);return self
    navigated=threading.Event();urls=[]
    window=SimpleNamespace(events=SimpleNamespace(shown=Event(),before_load=Event(),loaded=Event(),closed=Event()),load_url=lambda url:(urls.append(url),navigated.set()))
    state={'ok':True,'role':'frontend-ui','active_revision':'old','document_revision':'bad','document_sequence':2,'failed_sequences':[2]}
    watch=NativeUpdateRecovery(window,'http://127.0.0.1:1234/workspace?native_window=test&token=forbidden#token=also-forbidden',interval=.01,fetch=lambda _:state)
    watch.start()
    assert navigated.wait(1)
    watch.stop()
    from urllib.parse import parse_qs,urlsplit
    assert len(urls)==1 and urlsplit(urls[0]).fragment==''
    assert parse_qs(urlsplit(urls[0]).query).keys()=={'native_window','ui_recovery'}
    assert parse_qs(urlsplit(urls[0]).query)['native_window']==['test']
    assert watch.recoveries==1 and not watch.thread.is_alive()


def test_real_webview2_hung_app_recovers_by_native_navigation(tmp_path,signer):
    import os
    import sys
    if sys.platform!='win32':
        pytest.skip('Native WebView2 requires Windows')
    pytest.importorskip('webview')
    good=candidate(signer,1)
    bad_files=bundled_ui_files();bad_files['workspace.js']=b'while(true){}'
    bad=candidate(signer,2)
    # Keep the signed complete catalog, replacing just its application with the actual hang.
    private,trust=signer
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,data in bad_files.items():archive.writestr(name,data)
    bad_zip=output.getvalue();payload=bad[2]
    payload['artifacts']['desktop'].update(sha256=hashlib.sha256(bad_zip).hexdigest(),size=len(bad_zip),files={name:{'sha256':hashlib.sha256(data).hexdigest(),'size':len(data)} for name,data in bad_files.items()})
    for name,data in [('good.json',good[0]),('good.zip',good[1]),('bad.json',sign(payload,private,trust)),('bad.zip',bad_zip),('trust.json',json.dumps(trust).encode())]:(tmp_path/name).write_bytes(data)
    probe=tmp_path/'native-hang-probe.py'
    probe.write_text(NATIVE_HANG_PROBE,encoding='utf-8')
    root=Path(__file__).parents[1]
    command=[sys.executable,str(probe),str(root),str(tmp_path)]
    result=subprocess.run(command,cwd=root,capture_output=True,text=True,timeout=75,creationflags=subprocess.CREATE_NO_WINDOW)
    report=json.loads((tmp_path/'native-result.json').read_text(encoding='utf-8')) if (tmp_path/'native-result.json').exists() else {}
    artifacts=root/'.omx'/'artifacts'/'ui-updates';artifacts.mkdir(parents=True,exist_ok=True)
    (artifacts/'native-hung-renderer-recovery.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    assert result.returncode==0,(result.stdout,result.stderr,report)
    assert report.get('passed'),report
    assert report['bridge_clients_during_hang']==0,report
    assert report['native_recoveries']==1 and report['native_interruptions']==1 and report['recovery_seconds']>=19,report
    assert report['stable_url_has_credentials'] is False,report
    assert report['active_revision']=='desktop-ui-1' and report['connected_after_recovery'],report
    assert report['icon_big'] and report['icon_small'],report


NATIVE_HANG_PROBE=r'''
import asyncio,ctypes,json,os,sys,threading,time,traceback
from pathlib import Path
from unittest.mock import patch
ROOT=Path(sys.argv[1]);WORK=Path(sys.argv[2]);sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
import webview
from websockets.asyncio.server import serve
from supermonoagent.daemon import Daemon
from supermonoagent.frontend import Frontend
from supermonoagent.desktop import NativeUpdateRecovery
from supermonoagent import ui_updates as U
from test_client_integration import ControlledPty
from test_desktop_ui_updates import NativeRecoveryNavigation
ready=threading.Event();done=threading.Event();state={};result={'passed':False}
window_id='owned-native-update-test'
navigation=NativeRecoveryNavigation(window_id)

async def service():
  try:
    with patch('supermonoagent.daemon.STATE',WORK/'state'),patch.dict(os.environ,{'SUPERMONO_STATE_DIR':str(WORK/'state')}):
      daemon=Daemon('native-update-fixture-owner');daemon.hub.persist=False;daemon.hub.pty_factory=lambda *_:ControlledPty();daemon.hub.attach_loop(asyncio.get_running_loop())
      win=daemon.hub.open_window('Owned native update PTY',owner=daemon.token);session=daemon.hub.open_cli(win.id,'cmd')
      async with serve(daemon.handle_socket,'127.0.0.1',0,process_request=daemon.serve_http) as backend:
        frontend=Frontend('http://127.0.0.1:'+str(backend.sockets[0].getsockname()[1]),WORK/'themes.json',update_root=WORK/'ui',update_trust=json.loads((WORK/'trust.json').read_text()),update_feed='https://updates.example/manifest.json')
        frontend.update_store.install((WORK/'good.json').read_bytes(),(WORK/'good.zip').read_bytes())
        with patch.object(U,'fetch_manifest',return_value=(WORK/'bad.json').read_bytes()),patch.object(U,'fetch_archive',return_value=(WORK/'bad.zip').read_bytes()):
          async with serve(frontend.handler,'127.0.0.1',0,process_request=frontend.serve_http) as server:
            state.update(frontend=frontend,daemon=daemon,session=session,url='http://127.0.0.1:'+str(server.sockets[0].getsockname()[1])+'/workspace?direct=1&renderer=dom&native_window='+window_id)
            ready.set()
            while not done.is_set():await asyncio.sleep(.05)
        await frontend.close_updates();daemon.hub.close_window(win.id)
  except BaseException as error:
    state['error']=repr(error);ready.set()
thread=threading.Thread(target=lambda:asyncio.run(service()),daemon=True);thread.start()
assert ready.wait(10), 'owned frontend failed to start'
assert 'error' not in state,state
icon=ROOT/'src'/'supermonoagent'/'static'/'app-icon.ico'
window=webview.create_window('Owned update recovery fixture',state['url']+'#token=native-update-fixture-owner',width=900,height=650,focus=False)
recovery=NativeUpdateRecovery(window,state['url'])

def check():
  try:
    assert window.events.loaded.wait(15),'initial UI did not load'
    # loaded is a reusable, unversioned SDK event. Native navigation IDs are
    # needed because late injection for the interrupted document can set it.
    from System import Action
    def starting(_sender,args):
      navigation.starting(str(args.Uri),int(args.NavigationId))
    def completed(_sender,args):
      navigation.completed(int(args.NavigationId),bool(args.IsSuccess))
    def observe_native_navigation():
      core=window.native.webview.CoreWebView2
      core.NavigationStarting += starting
      core.NavigationCompleted += completed
    window.native.webview.Invoke(Action(observe_native_navigation))
    deadline=time.monotonic()+10
    while (state['frontend'].update_store.status()['pending'] or not state['frontend'].update_candidate) and time.monotonic()<deadline:time.sleep(.03)
    assert state['frontend'].update_store.status()['pending'] is None,'initial ready was not committed'
    assert state['frontend'].update_candidate['sequence']==2
    initial=window.run_js('({marker:window.updateMarker,connected:smaWorkspace.connected,agent:navigator.userAgent})')
    assert initial['marker']=='desktop-ui-1' and initial['connected'],initial
    window.run_js('setTimeout(()=>NativeUpdates.downloadAndInstall(),0);')
    deadline=time.monotonic()+10
    while state['frontend'].update_windows.get(window_id,{}).get('sequence')!=2 and time.monotonic()<deadline:time.sleep(.03)
    assert state['frontend'].update_windows.get(window_id,{}).get('sequence')==2,'hung snapshot document never loaded'
    started=time.monotonic();time.sleep(.3)
    result['bridge_clients_during_hang']=len(state['frontend'].update_clients)
    deadline=time.monotonic()+30
    while (recovery.recoveries<1 or not navigation.loaded.is_set()) and time.monotonic()<deadline:time.sleep(.05)
    assert recovery.recoveries==1,'native recovery did not navigate'
    assert navigation.loaded.is_set(),('recovery navigation did not complete successfully',navigation.snapshot())
    result['recovery_navigation']=navigation.snapshot()
    # Only a successful completion of the ui_recovery navigation permits JS
    # observation. No evaluate/run_js is attempted during the while(true).
    observations=[];current=None
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
      assert navigation.loaded.is_set(),'a new recovery navigation began during readiness checks'
      current=window.run_js('({marker:window.updateMarker,ready:document.readyState,connected:!!window.smaWorkspace?.connected,agent:navigator.userAgent,href:location.href})')
      observations.append({'kind':type(current).__name__,'marker':current.get('marker') if isinstance(current,dict) else None,'connected':current.get('connected') if isinstance(current,dict) else False})
      if isinstance(current,dict) and current.get('marker')=='desktop-ui-1' and current.get('ready')=='complete' and current.get('connected'):
        break
      time.sleep(.05)
    result['recovered_document_observations']=observations
    assert isinstance(current,dict) and current.get('marker')=='desktop-ui-1' and current.get('ready')=='complete' and current.get('connected'),('recovered document did not become ready',observations,navigation.snapshot())
    assert current['href']==navigation.snapshot()['uri'],('observed a different document',current['href'],navigation.snapshot())
    status=state['frontend'].update_store.status()
    send=ctypes.windll.user32.SendMessageW;send.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_size_t,ctypes.c_ssize_t];send.restype=ctypes.c_void_p
    handle=int(window.native.Handle.ToInt64())
    result.update(passed=True,active_revision=status['active_revision'],failed_sequences=status['failed_sequences'],native_recoveries=recovery.recoveries,native_interruptions=recovery.interruptions,recovery_seconds=time.monotonic()-started,connected_after_recovery=current['connected'],runtime=current['agent'],stable_url_has_credentials=('#' in recovery.reload_url or 'token=' in recovery.reload_url),icon_big=bool(send(handle,0x7f,1,0)),icon_small=bool(send(handle,0x7f,0,0) or send(handle,0x7f,2,0)),pty_input_bytes=len(state['session'].pty.written))
  except BaseException as error:
    result.update(error=repr(error),traceback=traceback.format_exc())
  finally:
    result.update(native_recoveries=recovery.recoveries,native_interruptions=recovery.interruptions,interrupt_error=recovery.interruption_error,runtime_armed=recovery.runtime_armed,recovery_navigation=navigation.snapshot(),served_document=state['frontend'].update_windows.get(window_id),final_store=state['frontend'].update_store.status()['active_revision'])
    recovery.stop();done.set();(WORK/'native-result.json').write_text(json.dumps(result,indent=2),encoding='utf-8');window.destroy()
webview.start(check,gui='edgechromium',icon=str(icon));done.set();thread.join(timeout=5)
raise SystemExit(0 if result['passed'] else 1)
'''


def test_snapshot_pointer_race_never_rebases_old_html_to_new_assets(tmp_path,signer):
    asyncio.run(_check_snapshot_race(tmp_path,signer))


async def _check_snapshot_race(tmp_path,signer):
    async with frontend_fixture(tmp_path,signer) as (frontend,feed,_daemon,_session,_other,_origin):
        original=frontend.update_store.snapshot
        switched=False
        calls=[]
        def race(revision=None):
            nonlocal switched
            calls.append(revision)
            files=original(revision)
            if not switched:
                switched=True
                frontend.update_store.install(feed['release'][0],feed['release'][1])
            return files
        with patch.object(frontend.update_store,'snapshot',side_effect=race):
            html=frontend._workspace_document()
        assert all(revision is not None for revision in calls)
        assert 'data-revision="desktop-ui-1"' in html
        assert '/__ui/desktop-ui-1/workspace.js?v=2#asset' in html
        assert '/__ui/desktop-ui-1/xterm.css#font' in html
        assert '/__ui/bundled/' not in html


def test_two_ready_windows_stop_polling_after_only_one_ticket_commit(tmp_path,signer):
    playwright=pytest.importorskip('playwright.async_api')
    if not EDGE.exists():pytest.skip('Local Edge required')
    asyncio.run(_check_two_windows(tmp_path,signer,playwright))


async def _check_two_windows(tmp_path,signer,playwright):
    async with frontend_fixture(tmp_path,signer) as (frontend,feed,daemon,_session,_other,origin):
        frontend.update_store.install(feed['release'][0],feed['release'][1])
        original=frontend._update_operation
        ready_calls=0;ready_results=[];status_calls=0;gate=asyncio.Event()
        async def operation(message):
            nonlocal ready_calls,status_calls
            if message.get('op')=='status':status_calls+=1
            if message.get('op')=='ready':
                ready_calls+=1
                if ready_calls==2:gate.set()
                await asyncio.wait_for(gate.wait(),5)
                try:
                    value=await original(message);ready_results.append(True);return value
                except ValueError:
                    ready_results.append(False);raise
            return await original(message)
        frontend._update_operation=operation
        async with playwright.async_playwright() as runtime:
            browser=await runtime.chromium.launch(executable_path=str(EDGE),headless=True)
            try:
                context=await browser.new_context()
                pages=[await context.new_page(),await context.new_page()]
                await asyncio.gather(*(page.goto(origin+'/workspace?direct=1&renderer=dom#token='+daemon.token) for page in pages))
                async with asyncio.timeout(6):
                    while len(ready_results)<2:await asyncio.sleep(.02)
                assert sorted(ready_results)==[False,True]
                assert frontend.update_store.status()['pending'] is None
                await asyncio.sleep(1.25)
                count=status_calls
                await asyncio.sleep(1.25)
                assert status_calls==count
                for page in pages:
                    assert await page.evaluate("smaWorkspace.connected&&NativeUpdates.revision==='desktop-ui-1'")
            finally:
                gate.set();await browser.close()
