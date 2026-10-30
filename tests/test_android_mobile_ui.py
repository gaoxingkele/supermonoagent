"""Android packaged document in hidden Chromium; not physical WebView/IME validation."""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import pytest
from websockets.asyncio.server import serve
from supermonoagent.daemon import Daemon
from supermonoagent import web
from test_client_integration import ControlledPty

ROOT=Path(__file__).resolve().parents[1]
EDGE=Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')


class EchoPty(ControlledPty):
    def write(self,data):
        super().write(data)
        self.inbox.put(data.replace(b'\x1b[200~',b'').replace(b'\x1b[201~',b''))


@pytest.mark.parametrize('intercepted',[False,True])
def test_packaged_mobile_document(intercepted):
    playwright=pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Hidden Edge needed for mobile document checks')
    asyncio.run(check_mobile(playwright,intercepted))


async def check_mobile(playwright,intercepted):
    with tempfile.TemporaryDirectory() as tmp, patch('supermonoagent.daemon.STATE',Path(tmp)),patch.dict(os.environ,{'SUPERMONO_STATE_DIR':tmp}):
        daemon=Daemon('mobile-test-only');daemon.hub.persist=False
        daemon.hub.pty_factory=lambda *_:EchoPty();daemon.hub.attach_loop(asyncio.get_running_loop())
        window=daemon.hub.open_window(owner=daemon.token)
        first=daemon.hub.open_cli(window.id,'cmd');second=daemon.hub.open_cli(window.id,'powershell')
        async def http(connection,request):
            path=request.path.split('?')[0]
            if path.startswith('/mobile/'):
                name=path[len('/mobile/'):]
                file=(ROOT/'src/supermonoagent/static'/name[7:]) if name.startswith('vendor/') else ROOT/'android/assets/mobile'/name
                if file.is_file():
                    mime='text/css' if file.suffix=='.css' else 'text/html' if file.suffix=='.html' else 'text/javascript'
                    return web.text_response(connection,200,file.read_text(encoding='utf-8'),mime+'; charset=utf-8')
            return await daemon.serve_http(connection,request)
        try:
            async with serve(daemon.handle_socket,'127.0.0.1',0,process_request=http) as server:
                port=server.sockets[0].getsockname()[1]
                config={'endpoint':f'http://127.0.0.1:{port}','code':'','token':daemon.token,'renderer':'dom'}
                async with playwright.async_playwright() as runtime:
                    browser=await runtime.chromium.launch(executable_path=str(EDGE),headless=True)
                    try:
                        page=await browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
                        errors=[];logs=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.on('console',lambda m:logs.append(m.text) if m.type=='error' else None)
                        origin=f'http://127.0.0.1:{port}'
                        if intercepted:
                            # Desktop route fulfillment has no private-network response address.
                            # Emulate the host permission Android WebView inherits from INTERNET.
                            await page.context.grant_permissions(['local-network-access'],origin=origin)
                            async def packaged_resource(route):
                                from urllib.parse import urlsplit
                                response=await route.fetch(url=origin+urlsplit(route.request.url).path)
                                await route.fulfill(response=response)
                            await page.route(origin+'/mobile/**',packaged_resource)
                        await page.goto(origin+'/mobile/index.html#'+quote(json.dumps(config)))
                        try:
                            await page.wait_for_function('window.MobileTerminal?.ready',timeout=7000)
                        except Exception:
                            print({'intercepted':intercepted,'errors':errors,'console':logs,'status':await page.locator('#status').text_content()})
                            raise
                        assert await page.evaluate('location.hash')==''
                        # Responsive PTY ownership is now an explicit user action.
                        await page.locator('#control').tap()
                        second.pty.inbox.put(b'background activity\r\n')
                        await page.locator('#sessions').tap()
                        # Unopened sessions stay unsubscribed even when their PTY is busy.
                        assert await page.locator('#session-list .session').nth(1).get_attribute('data-state')=='waiting'
                        assert all(client.subscriptions=={first.id} for client in daemon.hub.clients.values() if client.multiplex)
                        first.pty.inbox.put(b'visible activity\r\n')
                        await page.wait_for_function("document.querySelectorAll('#session-list .session')[0]?.dataset.state==='refreshing'")
                        await page.wait_for_function("document.querySelectorAll('#session-list .session')[0]?.dataset.state==='waiting'")
                        await page.locator('#sessions-close').tap()
                        assert await page.evaluate('document.activeElement.tagName') not in ('TEXTAREA','INPUT')
                        height_before=await page.locator('#screen').evaluate('e=>e.clientHeight')
                        await page.locator('#tools-toggle').tap()
                        assert await page.locator('#terminal-actions').is_hidden() and await page.locator('#keys').is_hidden()
                        assert await page.locator('#quick-composer').is_visible() and await page.locator('#quick-send').is_visible()
                        assert await page.locator('#tools-toggle').inner_text()=='展开'
                        await page.evaluate('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
                        assert await page.locator('#screen').evaluate('e=>e.clientHeight')>height_before
                        assert await page.evaluate("localStorage.getItem('sma.mobile.tools-collapsed')")=='1'
                        await page.locator('#tools-toggle').tap()
                        assert await page.locator('#terminal-actions').is_visible() and await page.locator('#keys').is_visible()
                        assert await page.locator('#quick-composer').is_visible()
                        assert await page.evaluate("localStorage.getItem('sma.mobile.tools-collapsed')")=='0'
                        before_tap = bytes(first.pty.written)
                        await page.tap('#screen')
                        await page.keyboard.insert_text('中文')
                        await asyncio.sleep(.05)
                        assert bytes(first.pty.written) == before_tap, 'The viewport must not become an IME input field'
                        assert await page.locator('#screen .xterm-helper-textarea').evaluate('e=>e.readOnly&&e.inputMode==="none"')
                        await page.locator('#quick-draft').fill('快捷中文')
                        assert await page.locator('#draft').input_value() == '快捷中文'
                        assert not await page.locator('#quick-draft').evaluate('e=>e.readOnly')
                        await page.locator('#quick-draft').press('Enter')
                        await wait_bytes(first.pty,'快捷中文'.encode()+b'\r')
                        await page.wait_for_function('document.getElementById("quick-draft").value===""&&document.getElementById("draft").value===""')
                        await page.locator('#mode').tap()
                        await page.locator('#draft').fill('中文')
                        await page.locator('#draft').press('Enter')
                        assert not await page.locator('#composer').evaluate('e=>e.open'), 'Editor must close as soon as input is accepted'
                        await wait_bytes(first.pty,'中文'.encode()+b'\r')
                        await page.wait_for_function('!document.getElementById("composer").open')
                        # An explicitly focused hardware-key path still supports Ctrl.
                        await page.locator('#screen .xterm-helper-textarea').focus()
                        await page.locator('#ctrl').tap()
                        await page.keyboard.press('c')
                        await wait_bytes(first.pty,b'\x03')
                        assert '中文'.encode() in first.pty.written
                        await page.locator('#mode').tap()
                        before=bytes(first.pty.written)
                        await page.locator('#draft').fill('echo drafted')
                        assert bytes(first.pty.written)==before
                        # Enter during preedit must not submit; final Enter submits exactly once.
                        await page.locator('#draft').dispatch_event('keydown',{'key':'Enter','isComposing':True})
                        assert bytes(first.pty.written)==before
                        await page.locator('#draft').press('Enter')
                        await wait_bytes(first.pty,b'echo drafted\r')
                        assert bytes(first.pty.written).count(b'echo drafted')==1
                        first.pty.inbox.put(('\r\n'.join('history '+str(i) for i in range(100))+'\r\n中文\r\n').encode())
                        await page.wait_for_function('MobileTerminal.terminal.buffer.active.baseY>0')
                        await page.locator('#screen').hover();await page.mouse.wheel(0,-300)
                        await page.wait_for_function('!document.getElementById("resume").hidden')
                        await page.locator('#bottom').tap()
                        await page.wait_for_function('document.getElementById("resume").hidden')
                        await page.locator('#options').tap();await page.locator('#renderer').select_option('webgl');await page.locator('#settings-close').tap()
                        artifacts=ROOT/'.omx/artifacts/android-v021';artifacts.mkdir(parents=True,exist_ok=True)
                        # Preserve the actual document/session/draft through fold and rotation.
                        await page.locator('#mode').tap()
                        await page.locator('#draft').fill('unsent fold draft')
                        await page.locator('#composer-close').tap()
                        await page.evaluate('window.layoutDocument = document')
                        connections = await page.evaluate('MobileTerminal.metrics.connections')
                        geometry_reports=[]
                        for width,height in ((390,844),(844,390),(280,653),(320,420),(673,841),(841,673),(1024,768),(360,640),(280,320),(390,844)):
                            await page.set_viewport_size({'width':width,'height':height})
                            await page.wait_for_function('''() => {
                                const t=MobileTerminal.terminal, s=document.getElementById('screen');
                                const cell=t._core._renderService.dimensions.css.cell;
                                return Math.abs(t.cols-Math.floor((s.clientWidth-12-t._core.viewport.scrollBarWidth)/cell.width))<=1;
                            }''')
                            await page.evaluate('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
                            assert await page.evaluate('document.documentElement.scrollWidth===innerWidth')
                            assert await page.locator('#keys').is_visible()
                            geometry=await page.evaluate('''() => {
                                const t=MobileTerminal.terminal, s=document.getElementById('screen');
                                const cell=t._core._renderService.dimensions.css.cell;
                                const rect=s.getBoundingClientRect(),keys=document.getElementById('keys').getBoundingClientRect();
                                return {width:innerWidth,height:innerHeight,cols:t.cols,rows:t.rows,
                                    terminalWidth:t.cols*cell.width,terminalHeight:t.rows*cell.height,
                                    availableWidth:s.clientWidth-12-t._core.viewport.scrollBarWidth,
                                    availableHeight:s.clientHeight-5,screenWidth:rect.width,keysBottom:keys.bottom};
                            }''')
                            assert geometry['terminalWidth']<=geometry['availableWidth']+1,geometry
                            assert geometry['availableWidth']-geometry['terminalWidth']<20,geometry
                            assert geometry['terminalHeight']<=geometry['availableHeight']+1,geometry
                            assert geometry['availableHeight']-geometry['terminalHeight']<20,geometry
                            assert geometry['keysBottom']<=height,geometry
                            assert geometry['screenWidth']==width,geometry
                            async with asyncio.timeout(3):
                                while (first.cols,first.rows)!=(geometry['cols'],geometry['rows']):
                                    await asyncio.sleep(.01)
                            assert await page.evaluate('document===window.layoutDocument&&MobileTerminal.ready')
                            assert await page.evaluate('MobileTerminal.metrics.connections')==connections
                            assert await page.locator('#draft').input_value()=='unsent fold draft'
                            geometry_reports.append(geometry)
                            await page.screenshot(path=str(artifacts/f'mobile-{width}x{height}.png'))
                            # Long host titles/paths and dialogs must fit the cover screen too.
                            await page.locator('#sessions').tap()
                            assert await page.locator('#session-list .session').first.evaluate("e=>e.getBoundingClientRect().height<=36")
                            assert await page.locator('#session-list .session-name').first.evaluate("e=>getComputedStyle(e).whiteSpace==='nowrap'")
                            assert await page.locator('#session-list .session-dot').count()==2
                            await page.locator('#session-list .session span').first.evaluate("e=>e.textContent='long-host-'.repeat(50)")
                            for dialog_id in ('session-dialog','settings-dialog','copy-dialog'):
                                await page.evaluate('''id=>{for(const d of document.querySelectorAll('dialog[open]'))d.close();document.getElementById(id).showModal()}''',dialog_id)
                                assert await page.locator('#'+dialog_id).evaluate('''e=>{
                                    const r=e.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight&&e.scrollWidth<=e.clientWidth;
                                }'''),(width,height,dialog_id)
                                await page.screenshot(path=str(artifacts/f'{dialog_id}-{width}x{height}.png'))
                            await page.evaluate("document.getElementById('copy-dialog').close()")
                        (artifacts/'responsive-geometry.json').write_text(json.dumps(geometry_reports,indent=2),encoding='utf-8')
                        # A desktop holds the lease: shared mode must parse the exact same grid.
                        await page.locator('#options').tap()
                        await page.locator('#sizing').select_option('shared')
                        await page.locator('#settings-close').tap()
                        desktop=daemon.hub.attach(asyncio.Queue(),key='desktop-layout-test',token=daemon.token)
                        desktop.multiplex=True;desktop.subscriptions={first.id}
                        assert daemon.hub.resize_session(desktop,first.id,120,30,True)
                        await page.wait_for_function('MobileTerminal.terminal.cols===120&&MobileTerminal.terminal.rows===30',timeout=5000)
                        history_samples_before=await page.evaluate('MobileTerminal.metrics.historySamples')
                        for i in range(300):
                            first.pty.inbox.put((f'\x1b[H\x1b[2J\x1b[1;100HRIGHT_{i:03}\x1b[10;30HMIDDLE_{i:03}\x1b[28;1HBOTTOM_{i:03}').encode())
                        await page.wait_for_function("MobileTerminal.terminal.buffer.active.getLine(MobileTerminal.terminal.buffer.active.baseY)?.translateToString().slice(99).startsWith('RIGHT_299')")
                        history_samples=await page.evaluate('MobileTerminal.metrics.historySamples')-history_samples_before
                        assert history_samples<=20, f'Full-screen history must be sampled, not rebuilt for every output: {history_samples}'
                        for width,height in ((280,653),(841,673),(390,844)):
                            await page.set_viewport_size({'width':width,'height':height})
                            await page.evaluate('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
                            assert await page.evaluate('MobileTerminal.terminal.cols===120&&MobileTerminal.terminal.rows===30')
                            assert (first.cols,first.rows)==(120,30)
                            assert await page.evaluate("MobileTerminal.terminal.buffer.active.getLine(MobileTerminal.terminal.buffer.active.baseY+9).translateToString().slice(29).startsWith('MIDDLE_299')")
                            assert await page.evaluate("MobileTerminal.terminal.buffer.active.getLine(MobileTerminal.terminal.buffer.active.baseY+27).translateToString().startsWith('BOTTOM_299')")
                            assert await page.locator('#screen .xterm').evaluate('e=>e.getBoundingClientRect().right<=innerWidth+1')
                        await page.locator('#control').tap()
                        await page.wait_for_function('MobileTerminal.terminal.cols<80')
                        async with asyncio.timeout(3):
                            while first.cols!=await page.evaluate('MobileTerminal.terminal.cols'):
                                await asyncio.sleep(.01)
                        daemon.hub.detach(desktop.key)
                        first.pty.inbox.put(('\r\n'.join(f'history-check-{i:04}' for i in range(6000))+'\r\n').encode())
                        await page.wait_for_function('MobileTerminal.terminal.buffer.normal.baseY>5000')
                        retained=await page.evaluate('MobileTerminal.terminal.buffer.normal.baseY')
                        first.pty.inbox.put(b'\x1b[3J')
                        await page.wait_for_timeout(100)
                        assert await page.evaluate('MobileTerminal.terminal.buffer.normal.baseY')>=retained
                        await page.evaluate('MobileTerminal.terminal.scrollToLine(0)')
                        assert await page.evaluate('MobileTerminal.terminal.buffer.active.viewportY')==0
                        await page.locator('#ctrl').tap()
                        await page.locator('#sessions').tap();await page.locator('#session-list .session').nth(1).tap()
                        await page.wait_for_function('(sid)=>MobileTerminal.active===sid&&MobileTerminal.ready',arg=second.id)
                        assert await page.evaluate('MobileTerminal.terminal.buffer.active.viewportY===MobileTerminal.terminal.buffer.active.baseY')
                        assert await page.locator('#ctrl').get_attribute('aria-pressed')=='false'
                        await page.locator('#mode').tap();await page.locator('#draft').fill('second');await page.locator('#draft').press('Enter')
                        await wait_bytes(second.pty,b'second');assert b'second' not in first.pty.written
                        await page.wait_for_function('!document.getElementById("composer").open')
                        await page.evaluate('MobileTerminal.pause()')
                        await page.wait_for_function('!MobileTerminal.connected')
                        await page.evaluate('MobileTerminal.resume()')
                        await page.wait_for_function('MobileTerminal.connected&&MobileTerminal.ready')
                        await page.locator('#mode').tap();await page.locator('#draft').fill('resumed');await page.locator('#draft').press('Enter')
                        await wait_bytes(second.pty,b'resumed')
                        daemon.hub.close_session(second.id)
                        await page.wait_for_function('(sid)=>MobileTerminal.active===sid&&MobileTerminal.ready',arg=first.id)
                        await page.locator('#sessions').tap()
                        assert await page.locator('#session-list .session').count()==1
                        await page.locator('#sessions-close').tap()
                        daemon.hub.close_window(window.id)
                        await page.wait_for_function("MobileTerminal.active===''&&MobileTerminal.terminal===null")
                        await page.locator('#sessions').tap()
                        assert await page.locator('#session-list .session').count()==0
                        await page.locator('#sessions-close').tap()
                        report=await page.evaluate('({metrics:MobileTerminal.metrics,status:document.getElementById("render-status").textContent})')
                        report.update(errors=errors,physical_android=False,packaged_interception=intercepted,desktop_local_network_permission_granted=intercepted,document_origin_bound_to_endpoint=True,checks=['direct_unicode_ctrl','buffered_preedit','scroll_history','viewports','fold_rotation_preserves_session_draft_connection','terminal_geometry_and_server_dimensions','dialog_bounds','targeted_session','background_resume'])
                        (artifacts/'verification.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
                        assert not errors,errors
                    finally:
                        await browser.close()
        finally:
            for wid in list(daemon.hub.windows):daemon.hub.close_window(wid)


async def wait_bytes(pty,expected):
    async with asyncio.timeout(5):
        while expected not in pty.written:
            await asyncio.sleep(.01)
