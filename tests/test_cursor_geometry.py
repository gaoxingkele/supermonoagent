"""Owned cursor/IME geometry probes; no real terminal input or login."""
import pytest
from websockets.asyncio.server import serve

from supermonoagent.pump import OutputPump
from test_client_integration import isolated_host
from test_cursor_input import EDGE, cursor_fixture, http_for, page_for, run


@pytest.mark.parametrize('renderer', ['dom', 'webgl'])
def test_cursor_and_hidden_input_follow_ansi_position_under_windows_scaling(renderer):
    run(lambda playwright: check_geometry(playwright, renderer))


async def check_geometry(playwright, renderer):
    async with isolated_host() as (daemon, _direct, _relay):
        session, owner, _echoed, _replies = cursor_fixture(daemon)
        async with serve(daemon.handle_socket, '127.0.0.1', 0, process_request=http_for(daemon)) as server:
            async with playwright.async_playwright() as runtime:
                browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
                try:
                    page = await page_for(browser, daemon, server)
                    await page.evaluate('value=>smaWorkspace.setRenderer(value)', renderer)
                    await page.wait_for_function('value=>smaWorkspace.activePane.renderer===value', arg=renderer)
                    await page.evaluate('smaWorkspace.terminal.focus()')
                    # Fake PTY output deliberately places the input in the middle.
                    session.pty.inbox.put(b'\x1b[12;19HOWNED_INPUT> \x1b[12;32H')
                    await page.wait_for_function('''()=>{const b=smaWorkspace.terminal.buffer.active;
                        return b.cursorX===31&&b.cursorY===11;}''')
                    await page.wait_for_timeout(100)
                    geometry = await page.evaluate('''()=>{const p=smaWorkspace.activePane,t=p.term;
                        const screen=t.element.querySelector('.xterm-screen').getBoundingClientRect(),
                            rect=t.textarea.getBoundingClientRect(),cell=t._core._renderService.dimensions.css.cell;
                        const scale=t.element.getBoundingClientRect().width/t.element.offsetWidth;
                        return {cursor:[t.buffer.active.cursorX,t.buffer.active.cursorY],scale,
                            x:(rect.left-screen.left)/(cell.width*scale),
                            y:(rect.top-screen.top)/(cell.height*scale),
                            opacity:getComputedStyle(t.textarea).opacity,
                            focused:document.activeElement===t.textarea};}''')
                    assert geometry['cursor'] == [31, 11]
                    assert 0 < geometry['scale'] < 1
                    assert geometry['focused']
                    assert geometry['opacity'] == '0'
                    assert abs(geometry['x'] - 31) < .15, geometry
                    assert abs(geometry['y'] - 11) < .15, geometry
                    assert not session.pty.written
                    # Native IME candidate windows are OS-owned and not proven
                    # by synthetic composition events; this only locks the DOM anchor.
                finally:
                    await browser.close()
        daemon.hub.detach(owner.key)


def test_cropped_history_preserves_early_cursor_hidden_state_without_changing_late_position():
    run(check_cropped_cursor)


async def check_cropped_cursor(playwright):
    # Owned fake CLI hides the native cursor and paints its own input label.
    # The hide command ages out of the ring; the label and final CUP remain.
    raw = (b'\x1b[?25l' + b'x\r\n' * 100000
           + b'\x1b[20;1HAdd an owned follow-up\x1b[42;1H')
    pump = OutputPump()
    pump.remember(raw)
    snapshot = pump.snapshot()
    assert b'\x1b[?25l' in snapshot
    async with isolated_host() as (daemon, _direct, _relay):
        cursor_fixture(daemon)
        async with serve(daemon.handle_socket, '127.0.0.1', 0, process_request=http_for(daemon)) as server:
            async with playwright.async_playwright() as runtime:
                browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
                try:
                    page = await page_for(browser, daemon, server)
                    result = await page.evaluate('''async streams=>{
                        const out=[];
                        for(const stream of streams){
                            const host=document.createElement('div');document.body.append(host);
                            const t=new Terminal({cols:145,rows:42,scrollback:0});t.open(host);t.focus();
                            await new Promise(resolve=>t.write(stream,resolve));
                            await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
                            out.push({hidden:t._core.coreService.isCursorHidden,
                                cursor:[t.buffer.active.cursorX,t.buffer.active.cursorY],
                                input:t.buffer.active.getLine(19).translateToString(true)});
                            t.dispose();host.remove();
                        }
                        return out;
                    }''', [raw.decode('ascii'), snapshot.decode('ascii')])
                    assert result[0]['hidden'] is True
                    assert result[1]['hidden'] is True
                    assert result[0]['cursor'] == result[1]['cursor'] == [0, 41]
                    assert result[0]['input'] == result[1]['input'] == 'Add an owned follow-up'
                finally:
                    await browser.close()
