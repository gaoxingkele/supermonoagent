"""Dense tablet grids and long history through the real mobile document."""
import asyncio
import json
from pathlib import Path
from urllib.parse import quote

import pytest
from websockets.asyncio.server import serve

from supermonoagent import web
from test_client_integration import isolated_host


ROOT = Path(__file__).resolve().parents[1]
EDGE = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
ARTIFACTS = ROOT / '.omx/artifacts/android-tablet-layout'
PORTRAIT = {'width': 768, 'height': 1200}
LANDSCAPE = {'width': 1200, 'height': 768}

GEOMETRY = '''() => {
    const m=MobileTerminal,t=m.terminal,r=document.getElementById('screen').getBoundingClientRect(),
        cell=t._core._renderService.dimensions.css.cell,
        transform=new DOMMatrixReadOnly(getComputedStyle(t.element).transform);
    return {cols:t.cols,rows:t.rows,owner:m.controls?.owner,
        canonicalCols:m.controls?.cols,canonicalRows:m.controls?.rows,
        screenWidth:r.width,screenHeight:r.height,cellWidth:cell.width,cellHeight:cell.height,
        gridWidth:(t.cols*cell.width+12+t._core.viewport.scrollBarWidth)*transform.a,
        gridHeight:(t.rows*cell.height+5)*transform.d,
        font:t.options.fontSize,effectiveFont:t.options.fontSize*transform.a,
        lineHeight:t.options.lineHeight,letterSpacing:t.options.letterSpacing,
        scaleX:transform.a,scaleY:transform.d,
        overflow:document.documentElement.scrollWidth>innerWidth+1,
        device:document.documentElement.dataset.device};
}'''

READING_DENSITY = '''() => {
    const t=MobileTerminal.terminal,b=t.buffer.active,
        lines=Array.from({length:t.rows},(_,i)=>b.getLine(b.viewportY+i)?.translateToString(true)||''),
        nonempty=lines.map((s,i)=>s.trim()?i:-1).filter(i=>i>=0),
        numbered=lines.filter(s=>s.includes('TABLET_ROW_'));
    return {cols:t.cols,rows:t.rows,bufferLength:b.length,baseY:b.baseY,viewportY:b.viewportY,
        cursorX:b.cursorX,cursorY:b.cursorY,nonemptyVisibleRows:nonempty.length,
        numberedVisibleRows:numbered.length,firstNonemptyRow:nonempty[0]??null,
        lastNonemptyRow:nonempty.at(-1)??null,
        blankTailRows:nonempty.length?t.rows-1-nonempty.at(-1):t.rows,
        firstMarker:numbered[0]?.match(/TABLET_ROW_([0-9]+)/)?.[1]||null,
        lastMarker:numbered.at(-1)?.match(/TABLET_ROW_([0-9]+)/)?.[1]||null};
}'''


@pytest.mark.parametrize('renderer', ['dom', 'webgl'])
def test_tablet_portrait_landscape_fit_and_history(renderer):
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Local Edge required for tablet document checks')
    asyncio.run(exercise_tablet(playwright, renderer))


async def exercise_tablet(playwright, renderer):
    async with asyncio.timeout(90), isolated_host() as (daemon, _direct, _relay):
        window = daemon.hub.open_window('Tablet layout fixture', owner=daemon.token)
        session = daemon.hub.open_cli(window.id, 'cmd')
        desktop = daemon.hub.attach(asyncio.Queue(), key='tablet-desktop-owner', token=daemon.token)
        desktop.multiplex = True
        desktop.subscriptions = {session.id}
        assert daemon.hub.resize_session(desktop, session.id, 158, 44, True)

        async def http(connection, request):
            path = request.path.split('?', 1)[0]
            if path.startswith('/mobile/'):
                name = path.removeprefix('/mobile/')
                folder = ROOT / 'src/supermonoagent/static' if name.startswith('vendor/') else ROOT / 'android/assets/mobile'
                relative = name.removeprefix('vendor/') if name.startswith('vendor/') else name
                resource = (folder / relative).resolve()
                if resource.is_relative_to(folder.resolve()) and resource.is_file():
                    mime = {'.html': 'text/html', '.css': 'text/css', '.json': 'application/json'}.get(resource.suffix, 'text/javascript')
                    return web.text_response(connection, 200, resource.read_text('utf-8'), mime + '; charset=utf-8')
                return web.text_response(connection, 404, 'Missing fixture asset', 'text/plain')
            return await daemon.serve_http(connection, request)

        try:
            async with serve(daemon.handle_socket, '127.0.0.1', 0, process_request=http) as server:
                origin = 'http://127.0.0.1:' + str(server.sockets[0].getsockname()[1])
                config = quote(json.dumps({'endpoint': origin, 'token': daemon.token, 'renderer': renderer}))
                async with playwright.async_playwright() as runtime:
                    browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
                    try:
                        page = await browser.new_page(viewport=PORTRAIT, screen=PORTRAIT,
                                                      device_scale_factor=2.5, is_mobile=True, has_touch=True)
                        page.set_default_timeout(10000)
                        errors = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        await page.add_init_script('''window.tabletFrames=[];
                            const send=WebSocket.prototype.send;
                            WebSocket.prototype.send=function(value){
                                if(typeof value!=='string'){
                                    const data=new Uint8Array(value);
                                    if(data.length>4&&[4,5,9,17].includes(data[4]))tabletFrames.push(data[4]);
                                }
                                return send.call(this,value);
                            };''')
                        await page.goto(origin + '/mobile/index.html#' + config)
                        await page.wait_for_function('MobileTerminal.ready&&MobileTerminal.controls?.busy')
                        ARTIFACTS.mkdir(parents=True, exist_ok=True)
                        # Save the current product surface before checking the new affordance.
                        await page.screenshot(path=str(ARTIFACTS / (renderer + '-initial.png')))
                        assert await page.locator('#control').text_content() == '适配本屏'
                        assert await page.locator('html').get_attribute('data-device') == 'tablet'

                        for viewport in (LANDSCAPE, PORTRAIT):
                            await page.set_viewport_size(viewport)
                            await page.wait_for_timeout(150)
                            assert (session.cols, session.rows) == (158, 44)
                            assert not await page.evaluate('tabletFrames.some(op=>[4,5,9,17].includes(op))')
                            assert not await page.evaluate('MobileTerminal.controls.owner')
                        assert session.resize_owner == desktop.key
                        assert not session.pty.written

                        # Supply real parser history through the fixture PTY, not DOM text.
                        for first, count in ((0, 2200), (2200, 2200), (4400, 2100)):
                            output = ''.join(f'TABLET_ROW_{number:05d}_' + 'x' * 42 + '\r\n'
                                             for number in range(first, first + count))
                            session.pty.inbox.put(output.encode())
                            await page.wait_for_function(
                                f'MobileTerminal.terminal.buffer.active.length>={first + count + 1}', timeout=20000)
                        await assert_history_retained(page)
                        before_fit_density = await page.evaluate(READING_DENSITY)
                        await page.locator('#control').tap()
                        await page.wait_for_function('MobileTerminal.ready&&MobileTerminal.controls?.owner')
                        assert await page.locator('#control').text_content() == '跟随电脑'
                        await page.locator('#quick-draft').fill('未发送的平板草稿 / tablet draft')
                        evidence = []
                        native_blank_tails = []
                        for label, viewport in (('portrait', PORTRAIT), ('landscape', LANDSCAPE),
                                                ('portrait-return', PORTRAIT)):
                            await page.set_viewport_size(viewport)
                            geometry = await assert_owned_fit(page, session)
                            await assert_history_retained(page)
                            assert await page.locator('#quick-draft').input_value() == '未发送的平板草稿 / tablet draft'
                            await page.locator('#quick-draft').evaluate('e=>e.blur()')
                            await page.locator('#history-top').tap()
                            await page.wait_for_function('MobileTerminal.terminal.buffer.active.viewportY===0')
                            assert await page.evaluate("MobileTerminal.terminal.buffer.active.getLine(0).translateToString(true).includes('TABLET_ROW_00000_')")
                            reading_top = await page.evaluate(READING_DENSITY)
                            assert reading_top['numberedVisibleRows'] >= reading_top['rows'] - 1, reading_top
                            await page.screenshot(path=str(ARTIFACTS / (renderer + '-' + label + '-history-top.png')))
                            await page.evaluate('''()=>{const t=MobileTerminal.terminal;
                                t.scrollToLine(Math.floor(t.buffer.active.baseY/2));}''')
                            reading_middle = await page.evaluate(READING_DENSITY)
                            assert reading_middle['numberedVisibleRows'] >= reading_middle['rows'] - 1, reading_middle
                            await page.screenshot(path=str(ARTIFACTS / (renderer + '-' + label + '-history-middle.png')))
                            await page.locator('#history-bottom').tap()
                            await page.wait_for_function('MobileTerminal.terminal.buffer.active.viewportY===MobileTerminal.terminal.buffer.active.baseY')
                            assert await page.evaluate("(()=>{const t=MobileTerminal.terminal,b=t.buffer.active;return Array.from({length:t.rows},(_,i)=>b.getLine(b.viewportY+i)?.translateToString(true)||'').some(s=>s.includes('TABLET_ROW_06499_'));})()")
                            reading_before = await page.evaluate(READING_DENSITY)
                            # Redraw and settle without adding output. This distinguishes
                            # a stale screenshot from real blank rows in the VT buffer.
                            await page.evaluate('MobileTerminal.terminal.refresh(0,MobileTerminal.terminal.rows-1)')
                            await page.wait_for_timeout(350)
                            reading_after = await page.evaluate(READING_DENSITY)
                            if reading_after['numberedVisibleRows'] < reading_after['rows'] - 3:
                                native_blank_tails.append({'orientation': label, **reading_after})
                            await page.screenshot(path=str(ARTIFACTS / (renderer + '-' + label + '.png')))
                            await assert_dialog_bounds(page)
                            evidence.append({'orientation': label, **geometry,
                                             'readingTop': reading_top,
                                             'readingMiddle': reading_middle,
                                             'readingBeforeRefresh': reading_before,
                                             'readingAfterRefresh': reading_after})
                        assert evidence[0]['rows'] > evidence[1]['rows']
                        assert evidence[1]['cols'] > evidence[0]['cols']
                        assert (evidence[0]['cols'], evidence[0]['rows']) == (evidence[2]['cols'], evidence[2]['rows'])
                        (ARTIFACTS / (renderer + '-density.json')).write_text(json.dumps({
                            'beforeFit': before_fit_density, 'screens': evidence,
                            'nativeConptyBlankTails': native_blank_tails, 'outputAddedAfterRotation': False,
                        }, indent=2), encoding='utf-8')
                        # ConPTY resize preserves the real cursor position and appends
                        # blank rows when its terminal grows. The controlled fixture
                        # intentionally emits no repaint after resize. Do not rewrite
                        # parser state to make its latest-screen screenshot look full;
                        # dense historical reading is verified above and separately.

                        # Model the reduced viewport while an IME is open, then restore.
                        # set_viewport_size also changes the emulated physical screen;
                        # a real keyboard only reduces the available document area.
                        metrics = await page.context.new_cdp_session(page)
                        await metrics.send('Emulation.setDeviceMetricsOverride', {
                            'width': 1200, 'height': 460, 'screenWidth': 1200, 'screenHeight': 768,
                            'deviceScaleFactor': 2.5, 'mobile': True,
                        })
                        keyboard = await assert_owned_fit(page, session)
                        assert await page.locator('html').get_attribute('data-device') == 'tablet'
                        assert keyboard['rows'] < evidence[1]['rows']
                        assert await page.locator('#quick-draft').input_value() == '未发送的平板草稿 / tablet draft'
                        await page.set_viewport_size(LANDSCAPE)
                        await assert_owned_fit(page, session)
                        await metrics.detach()
                        assert not session.pty.written
                        assert not await page.evaluate('tabletFrames.some(op=>[4,9,17].includes(op))')
                        assert not errors, errors
                        (ARTIFACTS / (renderer + '-verification.json')).write_text(json.dumps({
                            'renderer': renderer, 'screens': evidence, 'keyboardViewport': keyboard,
                            'historyLines': 6500, 'draftPreserved': True, 'inputFrames': 0,
                            'physicalAndroid': False, 'errors': errors,
                        }, ensure_ascii=False, indent=2), encoding='utf-8')
                    finally:
                        await browser.close()
        finally:
            daemon.hub.detach(desktop.key)


async def assert_owned_fit(page, session):
    await page.wait_for_function('''() => {
        const m=MobileTerminal,t=m.terminal,r=document.getElementById('screen'),
            cell=t._core._renderService.dimensions.css.cell;
        const cols=Math.max(2,Math.min(500,Math.floor((r.clientWidth-12-t._core.viewport.scrollBarWidth)/cell.width))),
            rows=Math.max(1,Math.min(200,Math.floor((r.clientHeight-5)/cell.height)));
        return m.ready&&m.controls?.owner&&t.options.lineHeight===1&&
            t.cols===cols&&t.rows===rows&&m.controls.cols===cols&&m.controls.rows===rows;
    }''', timeout=15000)
    geometry = await page.evaluate(GEOMETRY)
    assert (session.cols, session.rows) == (geometry['cols'], geometry['rows'])
    assert geometry['font'] == 14 and geometry['effectiveFont'] >= 13.9, geometry
    assert geometry['lineHeight'] == 1 and geometry['letterSpacing'] == 0, geometry
    assert geometry['scaleX'] == geometry['scaleY'], geometry
    assert geometry['gridWidth'] <= geometry['screenWidth'] + 2, geometry
    assert geometry['gridHeight'] <= geometry['screenHeight'] + 2, geometry
    assert geometry['screenWidth'] - geometry['gridWidth'] < geometry['cellWidth'] * 2 + 18, geometry
    assert geometry['screenHeight'] - geometry['gridHeight'] < geometry['cellHeight'] * 2 + 8, geometry
    assert not geometry['overflow'], geometry
    heights = await page.locator('header button, #terminal-actions button, #tools-toggle, #quick-send').evaluate_all(
        'buttons=>buttons.map(b=>b.getBoundingClientRect().height)')
    assert all(height >= 44 for height in heights), heights
    return geometry


async def assert_history_retained(page):
    assert await page.evaluate("MobileTerminal.terminal.buffer.active.getLine(0)?.translateToString(true).includes('TABLET_ROW_00000_')")
    assert await page.evaluate("(()=>{const b=MobileTerminal.terminal.buffer.active;return Array.from({length:100},(_,i)=>b.getLine(b.length-1-i)?.translateToString(true)||'').some(s=>s.includes('TABLET_ROW_06499_'));})()")


async def assert_dialog_bounds(page):
    for dialog in ('settings-dialog', 'session-dialog', 'composer'):
        await page.locator('#' + dialog).evaluate('d=>d.showModal()')
        bounds = await page.locator('#' + dialog).evaluate('''d=>{
            const r=d.getBoundingClientRect();return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,
                viewportWidth:innerWidth,viewportHeight:innerHeight,overflow:d.scrollWidth>d.clientWidth+1};
        }''')
        assert bounds['left'] >= 0 and bounds['top'] >= 0, bounds
        assert bounds['right'] <= bounds['viewportWidth'] + 1 and bounds['bottom'] <= bounds['viewportHeight'] + 1, bounds
        assert not bounds['overflow'], bounds
        await page.locator('#' + dialog).evaluate('d=>d.close()')
