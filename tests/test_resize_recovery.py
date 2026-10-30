"""Lost ACKs and reconnects must not strand output or leave a bounce size."""

import asyncio
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from supermonoagent.protocol import unpack, unpack_json
from test_client_integration import isolated_host


def test_resize_timeouts_reconnect_and_composition():
    playwright = pytest.importorskip("playwright.async_api")
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    if not edge.exists():
        pytest.skip("Local Edge required")
    asyncio.run(_exercise(playwright, edge))


async def _exercise(playwright, edge):
    async with isolated_host() as (daemon, _direct, _relay):
        window = daemon.hub.open_window("Recovery editor", owner=daemon.token)
        session = daemon.hub.open_cli(window.id, "cmd")
        resizes = []
        sockets = []
        drop = {"next": False}
        original_op = daemon._op

        async def operation(client, op, payload):
            frames, close = await original_op(client, op, payload)
            if op == 5 and drop["next"]:
                drop["next"] = False
                return [], close
            return frames, close
        daemon._op = operation

        def resize(cols, rows):
            resizes.append((cols, rows))
            session.pty.inbox.put(b"\x1b[2J\x1b[Hdraft-editor\r\n" + f"\x1b[{rows};1HSTATUS {cols}x{rows}".encode())
        session.pty.resize = resize

        async def handler(ws):
            sockets.append(ws)
            await daemon.handle_socket(ws)

        async with serve(handler, "127.0.0.1", 0, process_request=daemon.serve_http) as server:
            async with playwright.async_playwright() as runtime:
                browser = await runtime.chromium.launch(executable_path=str(edge), headless=True)
                try:
                    page = await browser.new_page(viewport={"width": 1100, "height": 760})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.goto(f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/workspace?direct=1&renderer=dom#token={daemon.token}")
                    settled = """() => {
                        const s=window.smaWorkspace;if(!s?.connected||!s.terminal)return false;
                        const p=s.panes.get(s.activeSession),c=s.controls[p.sid],d=p.fit.proposeDimensions();
                        return c?.owner&&!p.resizeRequest&&!p.writing&&!p.redrawAfterReplay&&!p.redrawTimer&&p.term.cols===c.cols&&p.term.rows===c.rows&&c.cols===d.cols&&c.rows===d.rows;
                    }"""
                    await page.wait_for_function(settled, timeout=10000)

                    # Drop the fit ACK while still streaming bytes. Timeout must drain them.
                    drop["next"] = True
                    await page.set_viewport_size({"width": 1280, "height": 800})
                    await page.wait_for_function("!!smaWorkspace.panes.get(smaWorkspace.activeSession).resizeRequest")
                    session.pty.inbox.put(b"\r\nWAITING-OUTPUT\r\n")
                    await page.wait_for_function("smaWorkspace.panes.get(smaWorkspace.activeSession).resizePaused===true", timeout=6000)
                    try:
                        await page.wait_for_function("Array.from({length:smaWorkspace.terminal.buffer.active.length},(_,i)=>smaWorkspace.terminal.buffer.active.getLine(i).translateToString(true)).join('\\n').includes('WAITING-OUTPUT')")
                    except Exception:
                        print('RESIZE_TIMEOUT_STATE', await page.evaluate("""() => {
                          const s=smaWorkspace,p=s.panes.get(s.activeSession),b=p.term.buffer.active;
                          return {connected:s.connected,ready:p.ready,paused:p.resizePaused,resizeRequest:p.resizeRequest,
                            writing:p.writing,queued:p.frames.map(f=>({op:f.op,size:f.bytes.length,grid:f.grid})),
                            controls:s.controls[p.sid],replay:p.replayPending,
                            lines:Array.from({length:b.length},(_,i)=>b.getLine(i).translateToString(true))};
                        }"""))
                        print('RESIZE_BACKEND_STATE', {
                            'input_queue': session.pty.inbox.qsize(), 'stopped': session.stop.is_set(),
                            'position': session.pump.position(), 'flushed': session.pump.flushed_position,
                            'pending': len(session.pump._pending),
                            'clients': [{'multiplex': c.multiplex, 'subscriptions': sorted(c.subscriptions),
                                'pending_replays': sorted(c.pending_replays), 'feed_offsets': c.feed_offsets,
                                'history_context': c.history_context, 'history_resume': c.history_resume,
                                'reliable': c.reliable, 'delegated': c.delegated, 'queue': c.queue.qsize()}
                                for c in daemon.hub.clients.values()]})
                        raise
                    await page.evaluate("smaWorkspace.takeControl()")
                    await page.wait_for_function(settled, timeout=8000)

                    # A lost intermediate redraw ACK still restores the confirmed final size.
                    final = (session.cols, session.rows)
                    before = len(resizes)
                    drop["next"] = True
                    await page.evaluate("smaWorkspace.redraw()")
                    await page.wait_for_function("smaWorkspace.panes.get(smaWorkspace.activeSession).resizeRequest?.stage==='redraw-out'", timeout=5000)
                    await page.wait_for_function(settled, timeout=8000)
                    assert (session.cols, session.rows) == final
                    assert len(resizes[before:]) == 2
                    assert resizes[-1] == final

                    # Only a diff remains in history; reconnect must request one full repaint.
                    with session.pump._lock:
                        session.pump._ring[:] = b"\x1b[1;8HZ"
                        session.pump._epoch_ring[:] = b"\x1b[1;8HZ"
                        session.pump._position += 4096
                    before = len(resizes)
                    previous_snapshots = await page.evaluate("smaWorkspace.metrics.snapshots")
                    await sockets[-1].close(code=1012)
                    try:
                        await page.wait_for_function("count=>smaWorkspace.metrics.snapshots>count", arg=previous_snapshots, timeout=10000)
                    except Exception:
                        print('RECONNECT_SNAPSHOT_STATE', await page.evaluate("""() => {
                          const w=smaWorkspace,h=w.activeHost,p=w.activePane;
                          return {connected:w.connected,phase:h.phase,error:h.error,retry:h.retry,
                            socket:h.ws?.readyState,metrics:w.metrics,cursor:p?.historyCursor,
                            initialized:p?.historyInitialized,replay:p?.replayPending,recovery:p?.recoveryPending,
                            writing:p?.writing,frames:p?.frames.length,follow:p?.followOutput};
                        }"""))
                        print('RECONNECT_BACKEND_STATE', {
                            'position': session.pump.position(), 'epoch_bytes': len(session.pump._epoch_ring),
                            'clients': [{'subscriptions': sorted(c.subscriptions), 'pending_replays': sorted(c.pending_replays),
                                'feed_offsets': c.feed_offsets, 'history_after': c.history_after,
                                'history_context': c.history_context, 'history_resume': c.history_resume,
                                'queue': c.queue.qsize()} for c in daemon.hub.clients.values()]})
                        raise
                    await page.wait_for_function(settled, timeout=10000)
                    # Clearing the timeout notice may first change the available height.
                    assert 2 <= len(resizes[before:]) <= 3
                    assert resizes[-1] == (session.cols, session.rows)
                    assert resizes[-2] == (session.cols - 1, session.rows)
                    await page.wait_for_function("smaWorkspace.terminal.buffer.active.getLine(smaWorkspace.terminal.buffer.active.viewportY)?.translateToString(true).includes('draft-editor')")

                    # DOM composition must not trigger repaint or leak a submit key.
                    before = len(resizes)
                    await page.evaluate("""() => {
                        const t=smaWorkspace.terminal.textarea;t.focus();
                        t.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true,data:''}));
                        t.value='中文';t.dispatchEvent(new CompositionEvent('compositionupdate',{bubbles:true,data:'中文'}));
                        t.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertCompositionText',data:'中文',isComposing:true}));
                        smaWorkspace.redraw();
                    }""")
                    await page.evaluate("()=>new Promise(resolve=>setTimeout(resolve,150))")
                    assert len(resizes) == before
                    assert session.pty.written == b""
                    await page.evaluate("smaWorkspace.terminal.textarea.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true,data:'中文'}))")
                    async with asyncio.timeout(3):
                        while not session.pty.written:
                            await asyncio.sleep(0.01)
                    assert session.pty.written == "中文".encode()
                    await page.wait_for_function(settled, timeout=6000)
                    assert not errors, errors
                finally:
                    await browser.close()
