"""Owned desktop/mobile pages must hand back the same PTY size without input."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from urllib.parse import quote

import pytest
from websockets.asyncio.server import serve
from supermonoagent.daemon import Daemon
from supermonoagent import web
from test_client_integration import ControlledPty

ROOT = Path(__file__).resolve().parents[1]
EDGE = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')


@pytest.mark.parametrize('renderer', ['dom', 'webgl'])
def test_android_takeover_can_return_to_desktop_follow(renderer):
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Local Edge required')
    asyncio.run(exercise(playwright, renderer))


async def exercise(playwright, renderer):
    with tempfile.TemporaryDirectory() as tmp, patch('supermonoagent.daemon.STATE', Path(tmp)), patch.dict(os.environ, {'SUPERMONO_STATE_DIR': tmp}):
        daemon = Daemon('owned-control-test')
        daemon.hub.persist = False
        daemon.hub.pty_factory = lambda *_: ControlledPty()
        daemon.hub.attach_loop(asyncio.get_running_loop())
        window = daemon.hub.open_window('Owned paper fixture', owner=daemon.token)
        session = daemon.hub.open_cli(window.id, 'cmd')

        def repaint(cols, rows):
            session.pty.inbox.put((f'\x1b[2J\x1b[Hpaper src/paper.md 42 ms\x1b[2;{max(1, cols-8)}HRIGHT_EDGE\x1b[{rows};1HPROMPT>').encode())
        session.pty.resize = repaint

        async def http(connection, request):
            path = request.path.split('?')[0]
            if path.startswith('/mobile/'):
                name = path[len('/mobile/'):]
                resource = ROOT/'src/supermonoagent/static'/name[7:] if name.startswith('vendor/') else ROOT/'android/assets/mobile'/name
                if resource.is_file():
                    mime = {'.html':'text/html', '.css':'text/css', '.json':'application/json'}.get(resource.suffix, 'text/javascript')
                    return web.text_response(connection, 200, resource.read_text('utf-8'), mime+'; charset=utf-8')
            return await daemon.serve_http(connection, request)

        try:
            async with serve(daemon.handle_socket, '127.0.0.1', 0, process_request=http) as server:
                origin = 'http://127.0.0.1:'+str(server.sockets[0].getsockname()[1])
                async with playwright.async_playwright() as runtime:
                    browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
                    try:
                        desktop = await browser.new_page(viewport={'width':1450, 'height':900})
                        await desktop.goto(origin+'/workspace?direct=1&renderer=dom#token='+daemon.token)
                        settled = '''()=>{const s=window.smaWorkspace;if(!s?.terminal)return false;const p=s.panes.get(s.activeSession),c=s.controls[p.sid],d=p.fit.proposeDimensions();return c?.owner&&!p.resizeRequest&&!p.writing&&!p.redrawAfterReplay&&!p.redrawTimer&&c.cols===d.cols&&c.rows===d.rows;}'''
                        await desktop.wait_for_function(settled)
                        desktop_size = (session.cols, session.rows)
                        assert desktop_size[0] > 80
                        mobile = await browser.new_page(viewport={'width':390, 'height':844}, is_mobile=True, has_touch=True)
                        errors = []
                        mobile.on('pageerror', lambda e: errors.append(str(e)))
                        await mobile.add_init_script('''window.controlFrames=[];const originalSend=WebSocket.prototype.send;WebSocket.prototype.send=function(value){if(typeof value!=='string'){const bytes=new Uint8Array(value);if(bytes.length>4&&[5,9,16,17].includes(bytes[4]))controlFrames.push({op:bytes[4],body:bytes[4]===17?'input':new TextDecoder().decode(bytes.slice(5))});}return originalSend.call(this,value);};''')
                        config = {'endpoint':origin, 'code':'', 'token':daemon.token, 'renderer':renderer}
                        await mobile.goto(origin+'/mobile/index.html#'+quote(json.dumps(config)))
                        await mobile.wait_for_function('MobileTerminal.ready&&MobileTerminal.controls?.busy')
                        await mobile.wait_for_function('document.getElementById("notice").hidden')
                        # Legacy snapshots have no request IDs: refresh and
                        # handoff must not race to consume the same replay.
                        deferred = []
                        refresh = daemon.hub.refresh_subscriptions
                        daemon.hub.refresh_subscriptions = lambda client, ids: deferred.append((client, ids))
                        await mobile.locator('#refresh-screen').tap()
                        await mobile.wait_for_function('document.getElementById("refresh-screen").getAttribute("aria-busy")==="true"')
                        assert await mobile.locator('#control').is_disabled()
                        await mobile.evaluate('document.getElementById("control").onclick()')
                        assert not await mobile.evaluate('controlFrames.some(f=>f.op===5)')
                        async with asyncio.timeout(3):
                            while not deferred:
                                await asyncio.sleep(.01)
                        refresh(*deferred[0])
                        daemon.hub.refresh_subscriptions = refresh
                        await mobile.wait_for_function('MobileTerminal.ready&&!document.getElementById("control").disabled')
                        await mobile.locator('#control').tap()
                        await mobile.wait_for_function('MobileTerminal.controls?.owner&&MobileTerminal.terminal.cols<80')
                        assert session.cols < 80
                        phone_size = (session.cols, session.rows)
                        evidence = ROOT/'.omx/artifacts/android-follow-0.2.42'
                        evidence.mkdir(parents=True, exist_ok=True)
                        await mobile.screenshot(path=str(evidence/(renderer+'-phone.png')))
                        # The second click currently sends another take_control request.
                        await mobile.locator('#control').tap()
                        await mobile.wait_for_function('MobileTerminal.controls?.owner===false', timeout=4000)
                        await desktop.wait_for_function(settled)
                        await mobile.wait_for_function('MobileTerminal.terminal.cols>80&&document.getElementById("screen").dataset.viewportMode==="readable"')
                        assert (session.cols, session.rows) == desktop_size
                        assert await mobile.evaluate('MobileTerminal.terminal.cols') == desktop_size[0]
                        assert await mobile.locator('#control').text_content() == '接管'
                        assert await mobile.locator('#screen').evaluate('e=>e.scrollWidth>e.clientWidth')
                        await mobile.screenshot(path=str(evidence/(renderer+'-follow.png')))
                        # Identical sizes on another handoff must not be blocked
                        # by the desktop's cache of its completed resize request.
                        await mobile.locator('#control').tap()
                        await mobile.wait_for_function('MobileTerminal.controls?.owner&&MobileTerminal.terminal.cols<80')
                        assert (session.cols, session.rows) == phone_size
                        await mobile.locator('#control').tap()
                        await desktop.wait_for_function(settled, timeout=4000)
                        await mobile.wait_for_function('MobileTerminal.controls?.owner===false&&MobileTerminal.terminal.cols>80')
                        before = await mobile.evaluate('controlFrames.filter(f=>f.op===5).length')
                        assert before == 2
                        await mobile.locator('#screen').dispatch_event('wheel', {'deltaX':120, 'deltaY':0})
                        assert await mobile.locator('#screen').evaluate('e=>e.scrollLeft>0')
                        await mobile.locator('#screen').evaluate('e=>e.scrollLeft=0')
                        box = await mobile.locator('#screen').bounding_box()
                        touch = await mobile.context.new_cdp_session(mobile)
                        y = box['y'] + min(80, box['height']/2)
                        await touch.send('Input.dispatchTouchEvent', {'type':'touchStart', 'touchPoints':[{'x':280, 'y':y}]})
                        await touch.send('Input.dispatchTouchEvent', {'type':'touchMove', 'touchPoints':[{'x':120, 'y':y}]})
                        await touch.send('Input.dispatchTouchEvent', {'type':'touchEnd', 'touchPoints':[]})
                        assert await mobile.locator('#screen').evaluate('e=>e.scrollLeft>0')
                        await touch.detach()
                        await mobile.locator('#options').tap()
                        await mobile.locator('#font').fill('18')
                        await mobile.locator('#font').dispatch_event('input')
                        await mobile.locator('#settings-close').tap()
                        await mobile.set_viewport_size({'width':480, 'height':760})
                        await mobile.wait_for_timeout(120)
                        assert (session.cols, session.rows) == desktop_size
                        assert await mobile.evaluate('controlFrames.filter(f=>f.op===5).length') == before
                        assert bytes(session.pty.written) == b''
                        # Following keeps normal target input, without taking the lease.
                        await mobile.locator('[data-key="right"]').tap()
                        async with asyncio.timeout(3):
                            while not session.pty.written:
                                await asyncio.sleep(.01)
                        assert bytes(session.pty.written) == b'\x1b[C'
                        assert not await mobile.evaluate('MobileTerminal.controls.owner')
                        assert not await mobile.evaluate('controlFrames.some(f=>f.op===9)')
                        # Repeat the round trip; no frozen "lastSize" prevents reacquiring.
                        await mobile.locator('#control').tap()
                        await mobile.wait_for_function('MobileTerminal.controls?.owner&&MobileTerminal.terminal.cols<80')
                        # Owning the grid always fits font changes, even if an old
                        # preference selected the scaled follow-only overview.
                        own_cols = session.cols
                        await mobile.locator('#options').tap()
                        await mobile.locator('#sizing').select_option('shared')
                        await mobile.locator('#font').fill('22')
                        await mobile.locator('#font').dispatch_event('input')
                        await mobile.locator('#settings-close').tap()
                        await mobile.wait_for_function('cols=>MobileTerminal.terminal.cols<cols', arg=own_cols)
                        await mobile.locator('#control').tap()
                        await desktop.wait_for_function(settled)
                        await mobile.wait_for_function('MobileTerminal.controls?.owner===false&&MobileTerminal.terminal.cols>80')
                        # If no desktop is connected, following never invents a grid/owner.
                        await desktop.close()
                        await mobile.locator('#control').tap()
                        await mobile.wait_for_function('MobileTerminal.controls?.owner===true')
                        await mobile.locator('#control').tap()
                        await mobile.wait_for_function('MobileTerminal.ready&&MobileTerminal.controls?.owner===false&&MobileTerminal.controls?.busy===false')
                        assert await mobile.locator('#screen').get_attribute('data-viewport-mode') == 'readable'
                        assert '等待远端接回尺寸控制' in await mobile.locator('#notice').text_content()
                        assert not errors, errors
                    finally:
                        await browser.close()
        finally:
            daemon.hub.close_window(window.id)
