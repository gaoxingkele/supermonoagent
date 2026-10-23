"""Actual relay output and replay colors in the desktop and Android documents.

All processes, sockets and PTYs are owned by this test. No production endpoint,
saved credentials, native phone or user terminal is inspected or addressed.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from urllib.parse import quote

import pytest
from websockets.asyncio.server import serve

from supermonoagent import web
from supermonoagent.daemon import Daemon, relay_uplink
from supermonoagent.relay import Relay
from supermonoagent.protocol import pack_json
from test_client_integration import ControlledPty, barrier, receive_op, subscribed

ROOT = Path(__file__).resolve().parents[1]
EDGE = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
ARTIFACTS = ROOT / '.omx/artifacts/remote-terminal-colors'
SAMPLES = [
    ('ANSI16_RED', '31', 1, None),
    ('ANSI16_BRIGHT', '91', 9, None),
    ('ANSI256_RED', '38;5;196', 196, None),
    ('TRUECOLOR_RGB', '38;2;12;34;56', 0x0C2238, None),
    ('ANSI16_BG', '37;44', 7, 4),
    ('ANSI256_BG', '37;48;5;22', 7, 22),
    ('TRUECOLOR_BG', '37;48;2;65;43;21', 7, 0x412B15),
]
STREAM = ('\x1b[0m\x1b[H\x1b[2J' + ''.join(
    '\x1b[' + sgr + 'm' + name + '\x1b[0m\r\n' for name, sgr, _, _ in SAMPLES
) + 'app.py 42 "text"\r\nREMOTE-COLOR-END\r\n').encode()

OBSERVE_DECORATIONS = """window.colorDecorations=[];
Object.defineProperty(window,'Terminal',{configurable:true,set(T){
 const register=T.prototype.registerDecoration;
 T.prototype.registerDecoration=function(options){const d=register.call(this,options);
   if(d)window.colorDecorations.push(d);return d;};
 Object.defineProperty(window,'Terminal',{value:T,writable:true,configurable:true});
}});"""

CELL_REPORT = """platform => {
 const t=platform==='desktop'?smaWorkspace.terminal:MobileTerminal.terminal,b=t.buffer.active;
 const rows=[];
 for(let y=0;y<b.length;y++){
   const l=b.getLine(y),text=l.translateToString(true);
   if(!text)continue;
   rows.push({line:y,text,cells:Array.from({length:Math.min(t.cols,text.length)},(_,x)=>{
     const c=l.getCell(x);return {text:c.getChars(),fg:c.getFgColor(),bg:c.getBgColor(),
       fgMode:c.getFgColorMode(),bgMode:c.getBgColorMode(),defaultFg:!!c.isFgDefault()};
   })});
 }
 return {cols:t.cols,rows:t.rows,renderer:platform==='desktop'?smaWorkspace.effectiveRenderer:
   document.querySelector('#render-status').textContent,lines:rows,
   decorations:colorDecorations.filter(d=>!d.isDisposed).map(d=>({line:d.marker.line,
     x:d.options.x,width:d.options.width,color:d.options.foregroundColor}))};
}"""


@pytest.mark.parametrize('renderer', ['dom', 'webgl'])
def test_relay_preserves_ansi_colors_live_and_snapshot_on_both_documents(renderer):
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Local Edge required for actual xterm renderer validation')
    asyncio.run(_exercise(playwright, renderer))


async def _exercise(playwright, renderer):
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='sma-remote-colors-') as tmp, patch(
        'supermonoagent.daemon.STATE', Path(tmp)
    ), patch.dict(os.environ, {'SUPERMONO_STATE_DIR': tmp}):
        daemon = Daemon('integration-owner')
        daemon.hub.persist = False
        daemon.hub.pty_factory = lambda *_: ControlledPty()
        daemon.hub.attach_loop(asyncio.get_running_loop())
        window = daemon.hub.open_window('Remote colors fixture', owner=daemon.token)
        session = daemon.hub.open_cli(window.id, 'cmd')
        relay = Relay(development=True)

        async def http(connection, request):
            path = request.path.split('?')[0]
            if path.startswith('/mobile/'):
                name = path[len('/mobile/'):]
                resource = (ROOT / 'src/supermonoagent/static' / name[7:]
                            if name.startswith('vendor/') else ROOT / 'android/assets/mobile' / name)
                if resource.is_file():
                    mime = {'.css': 'text/css', '.html': 'text/html'}.get(resource.suffix, 'text/javascript')
                    return web.text_response(connection, 200, resource.read_text(encoding='utf8'), mime+'; charset=utf-8')
            return await relay.serve_page(connection, request)

        async with serve(relay.handler, '127.0.0.1', 0, process_request=http) as server:
            origin = 'http://127.0.0.1:' + str(server.sockets[0].getsockname()[1])
            relay_url = origin.replace('http:', 'ws:') + '/ws'
            uplink = asyncio.create_task(relay_uplink(daemon, relay_url, 'local-test', 'color-fixture'))
            try:
                async with asyncio.timeout(5):
                    while relay_url not in daemon.relay_live:
                        await asyncio.sleep(.01)
                async with playwright.async_playwright() as runtime:
                    browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
                    try:
                        pages, urls, errors = {}, {}, []
                        for platform in ['desktop', 'android']:
                            page = await browser.new_page(viewport={'width': 1200 if platform=='desktop' else 600, 'height': 900})
                            await page.add_init_script(OBSERVE_DECORATIONS)
                            page.on('pageerror', lambda error: errors.append(str(error)))
                            if platform=='desktop':
                                url = origin+'/workspace?relay='+quote(origin, safe='')+'&code=local-test&renderer='+renderer+'#token='+daemon.token
                            else:
                                config = {'endpoint': origin, 'code': 'local-test', 'token': daemon.token, 'renderer': renderer}
                                url = origin+'/mobile/index.html#'+quote(json.dumps(config))
                            pages[platform], urls[platform] = page, url
                            await page.goto(url)
                            await page.wait_for_function('window.smaWorkspace?.terminal&&smaWorkspace.connected' if platform=='desktop' else 'window.MobileTerminal?.ready')

                        async with subscribed(relay_url, [session.id], relay=True) as observer:
                            session.pty.inbox.put(STREAM)
                            live_frame = await receive_op(observer, 3)
                            assert live_frame == session.id.encode()+b'\0'+STREAM
                            await observer.send(pack_json(16, {'sessions': []}))
                            await barrier(observer)
                            await observer.send(pack_json(16, {'sessions': [session.id]}))
                            snapshot_frame = await receive_op(observer, 8)
                            assert snapshot_frame == session.id.encode()+b'\0'+STREAM

                        reports = {'renderer_requested': renderer, 'stream_sha256': hashlib.sha256(STREAM).hexdigest(), 'stream_bytes': len(STREAM), 'wire_live_exact': True, 'wire_snapshot_exact': True}
                        for phase in ['live', 'snapshot']:
                            for platform, page in pages.items():
                                if phase=='snapshot':
                                    # Recreate the real client: only bounded daemon history restores it.
                                    await page.goto('about:blank')
                                    await page.goto(urls[platform])
                                await page.wait_for_function("platform => {const t=platform==='desktop'?window.smaWorkspace?.terminal:window.MobileTerminal?.terminal;return t&&Array.from({length:t.buffer.active.length},(_,i)=>t.buffer.active.getLine(i).translateToString(true)).join('\\n').includes('REMOTE-COLOR-END');}", arg=platform)
                                await page.wait_for_function("colorDecorations.some(d=>!d.isDisposed&&d.options.foregroundColor==='#ffcb6b')", timeout=3000)
                                report = await page.evaluate(CELL_REPORT, platform)
                                assert renderer.upper() in report['renderer'].upper(), report['renderer']
                                _assert_cells(report)
                                plain = next(row for row in report['lines'] if row['text']=='app.py 42 "text"')
                                assert all(cell['defaultFg'] for cell in plain['cells'])
                                marks = [mark for mark in report['decorations'] if mark['line']==plain['line']]
                                assert any(mark['color']=='#ffcb6b' for mark in marks)
                                reports[platform+'_'+phase] = report
                                await page.screenshot(path=str(ARTIFACTS/f'{renderer}-{platform}-{phase}.png'))
                        desktop = pages['desktop']
                        toggle = desktop.locator('#highlight-toggle')
                        assert await toggle.get_attribute('aria-pressed') == 'true'
                        await toggle.click(timeout=3000)
                        await desktop.wait_for_function('colorDecorations.every(d=>d.isDisposed)')
                        assert await toggle.get_attribute('aria-pressed') == 'false'
                        _assert_cells(await desktop.evaluate(CELL_REPORT, 'desktop'))
                        await desktop.goto('about:blank')
                        await desktop.goto(urls['desktop'])
                        await desktop.wait_for_function("window.smaWorkspace?.terminal&&Array.from({length:smaWorkspace.terminal.buffer.active.length},(_,i)=>smaWorkspace.terminal.buffer.active.getLine(i).translateToString(true)).join('\\n').includes('REMOTE-COLOR-END')")
                        assert await toggle.get_attribute('aria-pressed') == 'false'
                        assert await desktop.evaluate('colorDecorations.every(d=>d.isDisposed)')
                        _assert_cells(await desktop.evaluate(CELL_REPORT, 'desktop'))
                        await toggle.click()
                        await desktop.wait_for_function("colorDecorations.some(d=>!d.isDisposed&&d.options.foregroundColor==='#ffcb6b')")
                        _assert_cells(await desktop.evaluate(CELL_REPORT, 'desktop'))
                        await desktop.evaluate("() => {window.closedDecorations=colorDecorations.filter(d=>!d.isDisposed);smaWorkspace.closeView(smaWorkspace.activeSession);}")
                        assert await desktop.evaluate('closedDecorations.length>0&&closedDecorations.every(d=>d.isDisposed)')
                        assert await desktop.evaluate('smaWorkspace.panes.size') == 0
                        assert session.id in daemon.hub.sessions
                        await desktop.evaluate('sid=>smaWorkspace.openSession(sid,false)', session.id)
                        await desktop.wait_for_function("colorDecorations.some(d=>!d.isDisposed&&d.options.foregroundColor==='#ffcb6b')")
                        _assert_cells(await desktop.evaluate(CELL_REPORT, 'desktop'))
                        reports['desktop_toggle_and_reload_preserved'] = True
                        reports['desktop_closed_view_disposed_decorations'] = True
                        assert not errors, errors
                        assert not session.pty.written
                        reports['page_errors'] = errors
                        reports['source_hashes'] = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in [ROOT/'src/supermonoagent/static/workspace.js', ROOT/'src/supermonoagent/static/terminal-client.js', ROOT/'android/assets/mobile/app.js', ROOT/'android/assets/mobile/mobile-core.js']}
                        (ARTIFACTS/f'{renderer}-cells.json').write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding='utf8')
                    finally:
                        await browser.close()
            finally:
                uplink.cancel()
                await asyncio.gather(uplink, return_exceptions=True)
                daemon.hub.close_window(window.id)


def _assert_cells(report):
    for name, sgr, foreground, background in SAMPLES:
        row = next(row for row in report['lines'] if row['text']==name)
        for cell in row['cells']:
            assert cell['fg']==foreground, (name, cell)
            assert not cell['defaultFg'], (name, cell)
            if background is not None:
                assert cell['bg']==background, (name, cell)
        assert not any(mark['line']==row['line'] for mark in report['decorations']), name
