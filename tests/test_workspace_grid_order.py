"""A catalog resize must not change the grid of already queued PTY bytes."""
import asyncio
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from test_client_integration import isolated_host


EDGE = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')


@pytest.mark.parametrize('renderer', ['dom', 'webgl'])
def test_queued_output_keeps_its_received_grid(renderer):
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Local Edge required for terminal grid ordering')
    asyncio.run(check_grid_order(playwright, renderer))


async def check_grid_order(playwright, renderer):
    async with isolated_host() as (daemon, _direct, _relay):
        window = daemon.hub.open_window('Grid ordering', owner=daemon.token)
        session = daemon.hub.open_cli(window.id, 'cmd')

        def emit(data):
            session.pump.add(data, asyncio.get_running_loop(),
                lambda chunk, overflow: daemon.hub._flushed(session.id, chunk, overflow))

        desktop = daemon.hub.attach(
            asyncio.Queue(), key='grid-order-owner', token=daemon.token
        )
        desktop.multiplex = True
        desktop.subscriptions = {session.id}
        assert daemon.hub.resize_session(desktop, session.id, 120, 30, True)
        try:
            async with serve(
                daemon.handle_socket, '127.0.0.1', 0,
                process_request=daemon.serve_http
            ) as server:
                async with playwright.async_playwright() as runtime:
                    browser = await runtime.chromium.launch(
                        executable_path=str(EDGE), headless=True
                    )
                    try:
                        page = await browser.new_page(viewport={'width': 1440, 'height': 900})
                        errors = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        await page.goto(
                            f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
                            f'/workspace?direct=1&renderer={renderer}#token={daemon.token}'
                        )
                        await page.wait_for_function('''() => {
                            const workspace=window.smaWorkspace;
                            const pane=workspace?.panes.get(workspace.activeSession);
                            return pane?.ready&&!pane.writing&&pane.term.cols===120;
                        }''')
                        if renderer == 'webgl':
                            assert await page.evaluate('smaWorkspace.effectiveRenderer') == 'webgl'
                        await page.evaluate('''() => {
                            const term=smaWorkspace.terminal, original=term.write.bind(term);
                            window.writeGrids=[];
                            term.write=(data,done)=>{
                                const text=new TextDecoder().decode(data);
                                writeGrids.push({cols:term.cols,rows:term.rows,text});
                                original(data,()=>{
                                    if(text==='BLOCK')window.releaseWrite=done;
                                    else done?.();
                                });
                            };
                        }''')
                        emit(b'BLOCK')
                        await page.wait_for_function('!!window.releaseWrite')
                        emit(b'\x1b[1;100HOLD_GRID')
                        await page.wait_for_function(
                            'smaWorkspace.panes.get(smaWorkspace.activeSession).frames.length===1'
                        )
                        assert daemon.hub.resize_session(desktop, session.id, 60, 20, True)
                        await page.wait_for_function(
                            'smaWorkspace.controls[smaWorkspace.activeSession]?.cols===60'
                        )
                        emit(b'\x1b[2J\x1b[HNEW_GRID')
                        await page.wait_for_function(
                            'smaWorkspace.panes.get(smaWorkspace.activeSession).frames.length===2'
                        )
                        await page.evaluate('window.releaseWrite()')
                        await page.wait_for_function('''() => {
                            const pane=smaWorkspace.panes.get(smaWorkspace.activeSession);
                            return !pane.writing&&!pane.frames.length&&
                                writeGrids.some(write=>write.text.includes('NEW_GRID'));
                        }''')
                        grids = await page.evaluate('writeGrids')
                        assert next(write for write in grids if 'OLD_GRID' in write['text'])['cols'] == 120
                        assert next(write for write in grids if 'NEW_GRID' in write['text'])['cols'] == 60
                        assert await page.evaluate('''() => {
                            const term=smaWorkspace.terminal,buffer=term.buffer.active;
                            return term.cols===60&&term.rows===20&&
                                buffer.getLine(buffer.baseY).translateToString(true)==='NEW_GRID'&&
                                !Array.from({length:term.rows},(_,row)=>buffer.getLine(buffer.baseY+row)
                                    ?.translateToString(true)||'').join('\\n').includes('OLD_GRID');
                        }''')
                        assert not errors, errors
                    finally:
                        await browser.close()
        finally:
            daemon.hub.detach(desktop.key)


def test_partial_replay_repaints_unchanged_grid_and_renderer_switch():
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Local Edge required for terminal repaint checks')
    asyncio.run(check_partial_repaint(playwright))


async def check_partial_repaint(playwright):
    async with isolated_host() as (daemon, _direct, _relay):
        window = daemon.hub.open_window('Partial repaint', owner=daemon.token)
        session = daemon.hub.open_cli(window.id, 'cmd')
        session.pty.resize = lambda cols, rows: None

        def emit(data):
            session.pump.add(data, asyncio.get_running_loop(),
                lambda chunk, overflow: daemon.hub._flushed(session.id, chunk, overflow))

        resizes = []

        def resize(cols, rows):
            resizes.append((cols, rows))
            session.pty.inbox.put(f'\x1b[2J\x1b[HCLEAN_REPAINT {cols}x{rows}'.encode())

        async with serve(daemon.handle_socket, '127.0.0.1', 0,
                         process_request=daemon.serve_http) as server:
            async with playwright.async_playwright() as runtime:
                browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
                try:
                    page = await browser.new_page(viewport={'width': 1440, 'height': 900})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    await page.goto(
                        f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
                        f'/workspace?direct=1&renderer=dom#token={daemon.token}')
                    await page.wait_for_function('''() => {
                        const p=smaWorkspace.activePane,c=smaWorkspace.controls[p.sid];
                        return p?.ready&&c?.owner&&!p.writing&&!p.resizeRequest&&!p.replayPending;
                    }''')
                    raw, expected = await page.evaluate('''() => {
                        const p=smaWorkspace.activePane;smaWorkspace.closeView(p.raw);
                        return [p.raw,{cols:p.term.cols,rows:p.term.rows}];
                    }''')
                    async with asyncio.timeout(3):
                        while session.resize_owner is not None:
                            await asyncio.sleep(.01)
                    session.pty.resize = resize
                    session.pump.begin_resize()
                    emit(b'\x1b[2J\x1b[HSTALE_REPLAY')
                    await page.evaluate('raw=>smaWorkspace.openSession(raw,false)', raw)
                    await page.wait_for_function('''() => {
                        const p=smaWorkspace.activePane,c=smaWorkspace.controls[p.sid];
                        return p?.historyInitialized&&c?.owner&&!p.writing&&!p.resizeRequest
                            &&!p.replayPending&&!p.redrawAfterReplay&&!p.redrawTimer
                            &&Array.from({length:p.term.rows},(_,row)=>p.term.buffer.active.getLine(p.term.buffer.active.baseY+row)?.translateToString(true)||'').some(line=>line.includes('CLEAN_REPAINT'));
                    }''', timeout=12000)
                    assert resizes == [(expected['cols'] - 1, expected['rows']),
                                       (expected['cols'], expected['rows'])]
                    await page.wait_for_timeout(2400)
                    assert resizes == [(expected['cols'] - 1, expected['rows']),
                                       (expected['cols'], expected['rows'])]
                    assert await page.evaluate('''() => {
                        const t=smaWorkspace.terminal,b=t.buffer.active;
                        const snapshot=()=>({cols:t.cols,rows:t.rows,baseY:b.baseY,viewportY:b.viewportY,
                          lines:Array.from({length:t.rows},(_,row)=>b.getLine(b.viewportY+row)?.translateToString(true)||'')});
                        const before=snapshot();window.repaintRanges=[];
                        const refresh=t.refresh.bind(t);
                        t.refresh=(start,end)=>{repaintRanges.push([start,end]);return refresh(start,end);};
                        smaWorkspace.setRenderer('webgl');smaWorkspace.setRenderer('dom');
                        return JSON.stringify({repaintRanges,unchanged:JSON.stringify(before)===JSON.stringify(snapshot())});
                    }''') == f'{{"repaintRanges":[[0,{expected["rows"] - 1}],[0,{expected["rows"] - 1}]],"unchanged":true}}'
                    await page.wait_for_timeout(500)
                    assert resizes == [(expected['cols'] - 1, expected['rows']),
                                       (expected['cols'], expected['rows'])]
                    assert await page.evaluate('smaWorkspace.terminal.cols') == expected['cols']
                    assert await page.evaluate('smaWorkspace.terminal.rows') == expected['rows']
                    assert not errors, errors
                finally:
                    await browser.close()
