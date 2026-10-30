"""Observable status, confirmed grid and lease behavior through real WebSockets."""

import asyncio
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from websockets.asyncio.server import serve

from supermonoagent.protocol import CAPABILITIES, OP_RESIZE, pack_json, unpack, unpack_json
from test_client_integration import isolated_host, subscribed, barrier


def test_resize_ack_identifies_acceptance_and_refusal():
    asyncio.run(_check_ack())


async def _check_ack():
    async with isolated_host() as (daemon, _direct, _relay):
        window = daemon.hub.open_window(owner=daemon.token)
        session = daemon.hub.open_cli(window.id, "cmd")
        clients = [daemon.hub.attach(asyncio.Queue(), token=daemon.token) for _ in range(2)]
        for client in clients:
            client.multiplex = True
            daemon.hub.set_subscriptions(client, [session.id])
        for index, accepted in ((0, True), (1, False)):
            _, payload = unpack(pack_json(OP_RESIZE, {"session": session.id, "cols": 8, "rows": 3, "request": index + 10}))
            frames, _ = await daemon._op(clients[index], OP_RESIZE, payload)
            state = unpack_json(unpack(frames[0])[1])
            assert state["resize_ack"] == {"session": session.id, "request": index + 10, "accepted": accepted}
            assert (state["controls"][session.id]["cols"], state["controls"][session.id]["rows"]) == (8, 3)


@pytest.mark.parametrize("legacy", [False, True])
def test_workspace_status_and_shared_grid_without_lease_fights(legacy):
    playwright = pytest.importorskip("playwright.async_api")
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    if not edge.exists():
        pytest.skip("Local Edge required")
    with patch.dict(CAPABILITIES, {**CAPABILITIES, "resize_ack": not legacy}, clear=True):
        asyncio.run(_check_browser(playwright, edge, legacy))


async def _check_browser(playwright, edge, legacy):
    async with isolated_host() as (daemon, direct, _relay):
        window = daemon.hub.open_window("Runtime probe", owner=daemon.token)
        session = daemon.hub.open_cli(window.id, "cmd")
        if legacy:
            daemon.hub._terminal_size = lambda cols, rows: (max(20, min(cols, 500)), max(5, min(rows, 200)))
            original = daemon.hub.controls
            daemon.hub.controls = lambda client: {sid: {key: value for key, value in info.items() if key != "busy"} for sid, info in original(client).items()}
        resizes = []
        session.pty.resize = lambda cols, rows: resizes.append((cols, rows))
        async with serve(daemon.handle_socket, "127.0.0.1", 0, process_request=daemon.serve_http) as server, subscribed(direct, [session.id]) as native:
            await native.send(pack_json(5, {"session": session.id, "cols": 77, "rows": 44, "take_control": True}))
            await barrier(native)
            native_owner = session.resize_owner
            async with playwright.async_playwright() as runtime:
                browser = await runtime.chromium.launch(executable_path=str(edge), headless=True)
                try:
                    page = await browser.new_page(viewport={"width": 390, "height": 844})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.goto(f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/workspace?direct=1&renderer=dom#token={daemon.token}")
                    await page.wait_for_function("window.smaWorkspace?.terminal && smaWorkspace.metrics.lastPongMs !== null")
                    assert await page.locator('#tabs [role="tab"]').all_text_contents() == ["Runtime probe"]
                    assert await page.locator(".pane-title").inner_text() == "Runtime probe（其他）"
                    assert await page.locator("#tree .workspace-host-name").inner_text()
                    assert await page.locator('a[href="/legacy"]').count() == 0
                    await page.wait_for_function("smaWorkspace.terminal.cols === 77 && smaWorkspace.terminal.rows === 44")
                    await page.locator(".pane-header").click()
                    await page.set_viewport_size({"width": 1440, "height": 900})
                    await page.wait_for_function("innerWidth === 1440")
                    assert session.resize_owner == native_owner
                    assert (session.cols, session.rows) == (77, 44)
                    await page.wait_for_function("document.querySelector('#connection-quality').dataset.bars !== '0'")
                    await page.wait_for_function("document.querySelector('#connection-tx').textContent !== '↑ 0 B/s' && document.querySelector('#connection-rx').textContent !== '↓ 0 B/s'", timeout=5000)
                    await page.wait_for_function("document.querySelector('#connection-tx').textContent === '↑ 0 B/s' && document.querySelector('#connection-rx').textContent === '↓ 0 B/s'", timeout=5000)
                    assert await page.locator("#connection-label").inner_text() == "本机已连接"
                    assert int(await page.locator("#connection-quality").get_attribute("data-bars")) > 0
                    # Selecting a tab is local viewing; only Take Control reclaims dimensions.
                    await page.locator('#tabs [role="tab"]').first.click()
                    assert session.resize_owner == native_owner
                    assert (session.cols, session.rows) == (77, 44)
                    await page.locator('#take-control').click()
                    await page.wait_for_function("(()=>{const p=smaWorkspace.panes.get(smaWorkspace.activeSession),s=smaWorkspace.controls[p.sid],d=p.fit.proposeDimensions();return s?.owner&&!p.resizeRequest&&!p.redrawAfterReplay&&!p.redrawTimer&&s.cols===d.cols&&s.rows===d.rows;})()", timeout=6000)
                    count = len(resizes)
                    await page.locator('[role="tab"]').click()
                    await page.evaluate("()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))")
                    assert len(resizes) == count
                    assert session.resize_owner != native_owner

                    # Exercise a genuinely narrow grid, not CSS word-breaking.
                    await page.evaluate("""() => {
                        const p=smaWorkspace.panes.get(smaWorkspace.activeSession),c=p.term._core._renderService.dimensions.css.cell,s=getComputedStyle(p.term.element);
                        p.screen.style.flex='none';p.screen.style.width=(8*c.width+parseFloat(s.paddingLeft)+parseFloat(s.paddingRight)+p.term._core.viewport.scrollBarWidth+.5)+'px';
                        p.screen.style.height=(3*c.height+parseFloat(s.paddingTop)+parseFloat(s.paddingBottom)+.5)+'px';
                    }""")
                    await page.wait_for_function("size=>smaWorkspace.terminal.cols===size[0] && smaWorkspace.terminal.rows===size[1] && !smaWorkspace.panes.get(smaWorkspace.activeSession).resizeRequest", arg=[20,5] if legacy else [8,3])
                    assert await page.evaluate("(()=>{const p=smaWorkspace.panes.get(smaWorkspace.activeSession);return p.term.element.getBoundingClientRect().right<=p.screen.getBoundingClientRect().right+1;})()")
                    session.pty.inbox.put(b"abcdefghijklmnopqrstuvwxyz0123456789")
                    await page.wait_for_function("smaWorkspace.terminal.buffer.active.getLine(1)?.isWrapped === true")
                    await page.evaluate("smaWorkspace.terminal.paste('long-input-without-enter')")
                    async with asyncio.timeout(3):
                        while not session.pty.written:
                            await asyncio.sleep(0.01)
                    assert session.pty.written == b"long-input-without-enter"
                    await page.evaluate("()=>{const p=smaWorkspace.panes.get(smaWorkspace.activeSession);p.screen.style.flex='';p.screen.style.width='';p.screen.style.height='';}")
                    for width, height in ((390, 844), (850, 700), (1440, 900), (1920, 1080)):
                        await page.set_viewport_size({"width": width, "height": height})
                        await page.wait_for_function("()=>document.querySelector('#connect-button').getBoundingClientRect().right<=innerWidth && document.documentElement.scrollWidth<=innerWidth")
                        artifacts = os.environ.get("SUPERMONO_LAYOUT_ARTIFACTS")
                        if artifacts:
                            path = Path(artifacts)
                            path.mkdir(parents=True, exist_ok=True)
                            await page.screenshot(path=str(path / f"status-{width}.png"))

                    async def reject(ws):
                        await ws.recv()
                        await ws.close(code=4001)
                    async with serve(reject, "127.0.0.1", 0) as rejected:
                        await page.locator("#connect-button").click()
                        await page.locator('#relay-profiles-dialog summary').click()
                        await page.locator('#relay-profiles-advanced').click()
                        print('ADVANCED_DIALOG_STATE', await page.evaluate("""() => ({
                          dialogs:[...document.querySelectorAll('dialog[open]')].map(d=>d.id),
                          relayCode:{display:getComputedStyle(document.getElementById('relay-code')).display,visibility:getComputedStyle(document.getElementById('relay-code')).visibility,labelHidden:document.getElementById('relay-code').closest('label').hidden,rect:document.getElementById('relay-code').getBoundingClientRect().toJSON()},
                          connectionDisplay:getComputedStyle(document.getElementById('connection-dialog')).display,
                          auth:document.getElementById('connection-auth').value,
                          error:document.getElementById('connection-error').textContent
                        })"""), 'PAGE_ERRORS', errors)
                        await page.locator("#relay-url").fill(f"ws://127.0.0.1:{rejected.sockets[0].getsockname()[1]}/ws")
                        await page.locator("#relay-code").fill("bad-room")
                        await page.locator("#connection-form").get_by_role("button", name="连接", exact=True).click()
                        await page.wait_for_function("document.querySelector('#connection-label').textContent==='验证失败'")
                        assert await page.locator("#connection-quality").get_attribute("data-bars") == "0"
                        assert await page.locator("#connection-tx").inner_text() == "↑ 0 B/s"
                        assert await page.locator("#connect-button").is_enabled()
                    assert not errors, errors
                finally:
                    await browser.close()


def test_workspace_resize_preserves_follow_bottom_and_intentional_history():
    playwright = pytest.importorskip("playwright.async_api")
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    if not edge.exists():
        pytest.skip("Local Edge required")
    asyncio.run(_check_viewport_follow(playwright, edge))


async def _check_viewport_follow(playwright, edge):
    async with isolated_host() as (daemon, _direct, _relay):
        window = daemon.hub.open_window("Viewport regression", owner=daemon.token)
        session = daemon.hub.open_cli(window.id, "cmd")

        def redraw(cols, rows):
            text = "\x1b[2J\x1b[HISOLATED FAKE PTY\r\nLONG INPUT: " + "abcdefghij" * 12 + "\r\n" + "中文输入" * 10
            text += f"\x1b[{rows};1HSTATUS {cols}x{rows} CODEX_STYLE_FOOTER_END"
            session.pty.inbox.put(text.encode())

        session.pty.resize = redraw
        async with serve(daemon.handle_socket, "127.0.0.1", 0, process_request=daemon.serve_http) as server:
            async with playwright.async_playwright() as runtime:
                browser = await runtime.chromium.launch(executable_path=str(edge), headless=True)
                try:
                    page = await browser.new_page(viewport={"width": 1100, "height": 760}, has_touch=True)
                    await page.goto(f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/workspace?direct=1&renderer=dom#token={daemon.token}")
                    await page.wait_for_function("smaWorkspace?.terminal && smaWorkspace.connected")
                    await page.evaluate("""() => {
                      window.nanScrolls=[];
                      const service=smaWorkspace.terminal._core._bufferService,scroll=service.scrollLines;
                      service.scrollLines=function(...args){
                        if(!Number.isFinite(args[0]))nanScrolls.push({amount:String(args[0]),stack:new Error().stack});
                        return scroll.apply(this,args);
                      };
                    }""")
                    await page.evaluate("smaWorkspace.takeControl()")
                    settled = "!smaWorkspace.activePane.resizeRequest && !smaWorkspace.activePane.writing && !smaWorkspace.activePane.frames.length && !smaWorkspace.activePane.redrawTimer && !smaWorkspace.activePane.redrawAfterReplay && smaWorkspace.controls[smaWorkspace.activeSession]?.cols===smaWorkspace.terminal.cols"
                    for width, height in [(1100, 760), (880, 608), (733, 507), (1920, 1080), (1280, 720), (430, 760), (344, 608), (320, 507)]:
                        await page.set_viewport_size({"width": width, "height": height})
                        await page.wait_for_function(settled)
                        try:
                            await page.wait_for_function("(()=>{const t=smaWorkspace.terminal,b=t.buffer.active;return Array.from({length:t.rows},(_,i)=>b.getLine(b.viewportY+i)?.translateToString(true)||'').some(line=>line.includes('STATUS '+t.cols+'x'+t.rows));})()", timeout=3000)
                        except Exception:
                            print('VIEWPORT_STATE', width, height, await page.evaluate("""() => {
                              const s=smaWorkspace,p=s.activePane,t=p.term,b=t.buffer.active;
                              return {cols:t.cols,rows:t.rows,controls:s.controls[p.sid],request:p.resizeRequest,
                                paused:p.resizePaused,follow:p.followOutput,cursor:p.historyCursor,
                                baseY:b.baseY,viewportY:b.viewportY,frames:p.frames.length,
                                cell:t._core._renderService.dimensions.css.cell,
                                scrollTop:t.element.querySelector('.xterm-viewport').scrollTop,
                                followFrame:p.followFrame,followGeometry:p.followGeometry,nanScrolls,
                                lines:Array.from({length:b.length},(_,i)=>b.getLine(i).translateToString(true))};
                            }"""))
                            print('VIEWPORT_AFTER_BOTTOM', await page.evaluate("""() => {
                              const t=smaWorkspace.terminal;t.scrollToBottom();
                              return {viewportY:t.buffer.active.viewportY,baseY:t.buffer.active.baseY,
                                follow:smaWorkspace.activePane.followOutput};
                            }"""))
                            raise
                        assert await page.evaluate("smaWorkspace.terminal.buffer.active.viewportY===smaWorkspace.terminal.buffer.active.baseY"), (width, height)
                    # Native layout may scroll the old viewport before fit observes new dimensions.
                    await page.evaluate("""() => {
                        const p=smaWorkspace.activePane,t=p.term;t.scrollToBottom();
                        p.screen.style.width=Math.max(100,p.screen.clientWidth-35)+'px';
                        const viewport=t.element.querySelector('.xterm-viewport');viewport.scrollTop=0;
                        viewport.dispatchEvent(new Event('scroll'));
                    }""")
                    await page.wait_for_function(settled)
                    await page.wait_for_function("smaWorkspace.terminal.buffer.active.viewportY===smaWorkspace.terminal.buffer.active.baseY")
                    await page.evaluate("smaWorkspace.activePane.screen.style.width=''")
                    await page.wait_for_function(settled)
                    # A focus tap and no-op downward scroll at bottom must keep follow intent.
                    await page.locator('.terminal-screen').tap(force=True)
                    await page.wait_for_function("smaWorkspace.activePane.followOutput && !smaWorkspace.activePane.scrollIntentFrame")
                    await page.locator('.terminal-screen').hover()
                    await page.mouse.wheel(0, 240)
                    await page.wait_for_function("smaWorkspace.activePane.followOutput && !smaWorkspace.activePane.scrollIntentFrame")
                    session.pty.inbox.put(("\r\n" + "".join(f"HISTORY_ROW_{index:03d}\r\n" for index in range(100))).encode())
                    await page.wait_for_function("smaWorkspace.terminal.buffer.active.baseY>100 && !smaWorkspace.activePane.writing && !smaWorkspace.activePane.frames.length")
                    await page.evaluate("smaWorkspace.terminal.scrollToBottom();smaWorkspace.terminal.scrollLines(-12)")
                    assert await page.evaluate("smaWorkspace.terminal.buffer.active.viewportY<smaWorkspace.terminal.buffer.active.baseY")
                    await page.set_viewport_size({"width": 390, "height": 610})
                    await page.wait_for_function(settled)
                    assert await page.evaluate("smaWorkspace.terminal.buffer.active.viewportY<smaWorkspace.terminal.buffer.active.baseY")
                    assert await page.evaluate("(()=>{const t=smaWorkspace.terminal,b=t.buffer.active;return Array.from({length:t.rows},(_,i)=>b.getLine(b.viewportY+i)?.translateToString(true)||'').some(line=>line.includes('HISTORY_ROW_'));})()")
                finally:
                    await browser.close()


def test_zero_height_viewport_scroll_keeps_finite_position():
    playwright = pytest.importorskip("playwright.async_api")
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    if not edge.exists():
        pytest.skip("Local Edge required")
    asyncio.run(_check_zero_height_scroll(playwright, edge))


async def _check_zero_height_scroll(playwright, edge):
    async with isolated_host() as (daemon, _direct, _relay):
        window = daemon.hub.open_window("Viewport zero-height probe", owner=daemon.token)
        session = daemon.hub.open_cli(window.id, "cmd")
        async with serve(daemon.handle_socket, "127.0.0.1", 0, process_request=daemon.serve_http) as server:
            async with playwright.async_playwright() as runtime:
                browser = await runtime.chromium.launch(executable_path=str(edge), headless=True)
                try:
                    page = await browser.new_page(viewport={"width": 1100, "height": 760})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.goto(f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/workspace?direct=1&renderer=dom#token={daemon.token}")
                    await page.wait_for_function("smaWorkspace?.terminal && smaWorkspace.connected")
                    assert await page.evaluate("""() => {
                      const term=smaWorkspace.terminal,viewport=term._core.viewport;
                      Object.defineProperty(viewport,'_currentRowHeight',{value:0,configurable:true});
                      viewport._ignoreNextScrollEvent=false;
                      viewport._viewportElement.scrollTop=0;
                      viewport._handleScroll(new Event('scroll'));
                      return Number.isFinite(term.buffer.active.viewportY)&&term.buffer.active.viewportY===0;
                    }""")
                    assert not errors, errors
                finally:
                    await browser.close()
