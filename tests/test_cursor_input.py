"""Read-only diagnosis through owned Cursor-like TUI fixtures, never real login."""
import asyncio
import json
import mimetypes
from pathlib import Path
import re
from urllib.parse import quote

import pytest
from websockets.asyncio.server import serve

from supermonoagent import web
from supermonoagent.pty import ConptyInputFilter
from test_client_integration import isolated_host


ROOT = Path(__file__).resolve().parents[1]
EDGE = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')


def run(test):
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Owned Edge fixture requires local browser')
    asyncio.run(test(playwright))


async def page_for(browser, daemon, server, mobile=False):
    origin = 'http://127.0.0.1:' + str(server.sockets[0].getsockname()[1])
    url = (origin + '/mobile/index.html#' + quote(json.dumps({'endpoint': origin, 'token': daemon.token, 'renderer': 'dom'}))
           if mobile else origin + '/workspace?direct=1&renderer=dom#token=' + daemon.token)
    page = await browser.new_page(viewport={'width': 390 if mobile else 1200, 'height': 844})
    await page.goto(url)
    await page.wait_for_function('MobileTerminal.ready' if mobile else 'smaWorkspace.activePane?.ready&&!smaWorkspace.activePane.writing&&!smaWorkspace.activePane.replayPending&&!smaWorkspace.activePane.resizeRequest')
    return page


def http_for(daemon):
    async def http(connection, request):
        if request.path.startswith('/mobile/'):
            name = request.path.split('?', 1)[0].removeprefix('/mobile/')
            base = ROOT / ('src/supermonoagent/static' if name.startswith('vendor/') else 'android/assets/mobile')
            path = (base / name.removeprefix('vendor/')).resolve()
            if path.is_relative_to(base) and path.is_file():
                return web.text_response(connection, 200, path.read_text(encoding='utf-8'),
                                         (mimetypes.guess_type(path.name)[0] or 'text/javascript') + '; charset=utf-8')
        return await daemon.serve_http(connection, request)
    return http


def cursor_fixture(daemon, *, waits_for_query=False):
    window = daemon.hub.open_window('Owned Cursor CLI input fixture', owner=daemon.token)
    session = daemon.hub.open_cli(window.id, 'cmd')
    session.task_detector.app = 'cursor-cli'
    owner = daemon.hub.attach(asyncio.Queue(), key='owned-cursor-grid', token=daemon.token)
    owner.multiplex = True
    daemon.hub.set_subscriptions(owner, [session.id])
    assert daemon.hub.resize_session(owner, session.id, 144, 100, True)
    startup = b'\x1b[?1049h\x1b[?2004h\x1b[2J\x1b[HOWNED_CURSOR_FIXTURE\x1b[100;1H> '
    session.pump.remember(startup)
    filtered, echoed, replies = ConptyInputFilter(), bytearray(), []
    original = session.pty.write
    def receive(data):
        original(data)
        value = filtered.feed(data)
        if re.fullmatch(rb'\x1b\[\d+;\d+R', value):
            replies.append(True)
            return
        if waits_for_query and not replies:
            return
        echoed.extend(value)
        session.pty.inbox.put(('\x1b[100;1H\x1b[2K> ' + echoed.decode('utf-8', 'replace')).encode())
    session.pty.write = receive
    return session, owner, echoed, replies


@pytest.mark.parametrize('mobile', [False, True], ids=['windows', 'android'])
def test_focused_cursor_like_tui_receives_keys_and_plain_paste_with_echo(mobile):
    run(lambda playwright: check_focused(playwright, mobile))


async def check_focused(playwright, mobile):
    async with isolated_host() as (daemon, _direct, _relay):
        session, owner, echoed, _replies = cursor_fixture(daemon)
        async with serve(daemon.handle_socket, '127.0.0.1', 0, process_request=http_for(daemon)) as server:
            async with playwright.async_playwright() as runtime:
                browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
                try:
                    page = await page_for(browser, daemon, server, mobile)
                    term = 'MobileTerminal.terminal' if mobile else 'smaWorkspace.terminal'
                    if not mobile:
                        await page.evaluate('()=>{window.supermonoClipboardHasFiles=false;window.pywebview={api:{clipboard_has_files:async()=>false}};}')
                        await page.evaluate((ROOT / 'src/supermonoagent/static/clipboard-files.js').read_text(encoding='utf-8'))
                    await page.evaluate(term + '.focus()')
                    await page.wait_for_function('document.activeElement===' + term + '.textarea')
                    assert not await page.evaluate(term + '.options.disableStdin')
                    await page.keyboard.type('typed')
                    await page.wait_for_function('()=>{const b=' + term + '.buffer.active;return b.getLine(b.baseY+b.cursorY)?.translateToString(true).includes("> typed");}')
                    await page.evaluate('()=>{const d=new DataTransfer();d.setData("text/plain","-paste");' + term + '.textarea.dispatchEvent(new ClipboardEvent("paste",{clipboardData:d,bubbles:true,cancelable:true}));}')
                    await page.wait_for_function('()=>{const b=' + term + '.buffer.active;return b.getLine(b.baseY+b.cursorY)?.translateToString(true).includes("> typed-paste");}')
                    assert bytes(echoed) == b'typed-paste'
                    assert b'typed' in session.pty.written
                    assert await page.evaluate(term + '.buffer.active.type') == 'alternate'
                    assert await page.evaluate(term + '.buffer.active.cursorX') == 13
                    assert (session.cols, session.rows, session.resize_owner) == (144, 100, owner.key)
                finally:
                    await browser.close()
        daemon.hub.detach(owner.key)


def test_scaled_windows_gutter_click_restores_native_textarea_for_keys_and_text_paste():
    run(check_gutter)


async def check_gutter(playwright):
    async with isolated_host() as (daemon, _direct, _relay):
        session, owner, echoed, _replies = cursor_fixture(daemon)
        async with serve(daemon.handle_socket, '127.0.0.1', 0, process_request=http_for(daemon)) as server:
            async with playwright.async_playwright() as runtime:
                browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
                try:
                    page = await page_for(browser, daemon, server)
                    geometry = await page.evaluate('()=>{const p=smaWorkspace.activePane;return {screen:p.screen.getBoundingClientRect().toJSON(),term:p.term.element.getBoundingClientRect().toJSON()};}')
                    assert geometry['screen']['right'] - geometry['term']['right'] > 80
                    await page.evaluate('()=>{window.supermonoClipboardHasFiles=true;window.pywebview={api:{clipboard_has_files:async()=>false}};}')
                    await page.evaluate((ROOT / 'src/supermonoagent/static/clipboard-files.js').read_text(encoding='utf-8'))
                    await page.locator('#app-menu-toggle').focus()
                    screen = page.locator('.pane.active .terminal-screen')
                    await screen.click(position={'x': geometry['screen']['width'] - 8, 'y': geometry['screen']['height'] / 2})
                    await page.wait_for_function('document.activeElement===smaWorkspace.terminal.textarea')
                    await page.keyboard.type('focused')
                    await page.wait_for_function('()=>{const b=smaWorkspace.terminal.buffer.active;return b.getLine(b.baseY+b.cursorY)?.translateToString(true).includes("> focused");}')
                    assert bytes(session.pty.written) == b'focused'
                    # Own clipboard data avoids reading/modifying the OS clipboard.
                    # A stale format beacon must still deliver plain text to xterm.
                    await page.evaluate('()=>{const d=new DataTransfer();d.setData("text/plain","-paste");document.activeElement.dispatchEvent(new ClipboardEvent("paste",{clipboardData:d,bubbles:true,cancelable:true}));}')
                    await page.wait_for_function('()=>{const b=smaWorkspace.terminal.buffer.active;return b.getLine(b.baseY+b.cursorY)?.translateToString(true).includes("> focused-paste");}')
                    assert bytes(echoed) == b'focused-paste'
                    # The narrow screen-only fix must not steal header focus.
                    await page.locator('#app-menu-toggle').click()
                    assert not await page.evaluate('document.activeElement===smaWorkspace.terminal.textarea')
                    await page.keyboard.press('Escape')
                    # Synthetic pointerdown on child scroll/selection surfaces
                    # must not run the new explicit term.focus() branch.
                    await page.evaluate('()=>{const p=smaWorkspace.activePane,focus=p.term.focus.bind(p.term);window.cursorExplicitFocus=0;p.term.focus=(...args)=>{cursorExplicitFocus++;return focus(...args);};}')
                    await page.locator('.pane.active .xterm-viewport').dispatch_event('pointerdown', {'bubbles':True,'pointerType':'mouse'})
                    await page.locator('.pane.active .xterm-rows').dispatch_event('pointerdown', {'bubbles':True,'pointerType':'mouse'})
                    assert await page.evaluate('cursorExplicitFocus') == 0
                    assert bytes(echoed) == b'focused-paste'
                    assert (session.cols, session.rows, session.resize_owner) == (144, 100, owner.key)
                finally:
                    await browser.close()
        daemon.hub.detach(owner.key)


@pytest.mark.parametrize('mobile', [False, True], ids=['windows', 'android'])
def test_query_waiting_fixture_demonstrates_shared_reply_filter_gap_not_actual_cursor_diagnosis(mobile):
    run(lambda playwright: check_query_wait(playwright, mobile))


async def check_query_wait(playwright, mobile):
    async with isolated_host() as (daemon, _direct, _relay):
        session, owner, echoed, replies = cursor_fixture(daemon, waits_for_query=True)
        async with serve(daemon.handle_socket, '127.0.0.1', 0, process_request=http_for(daemon)) as server:
            async with playwright.async_playwright() as runtime:
                browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
                try:
                    page = await page_for(browser, daemon, server, mobile)
                    term = 'MobileTerminal.terminal' if mobile else 'smaWorkspace.terminal'
                    await page.evaluate('()=>{window.ownedDsrReports=0;' + term + '.onData(data=>{if(/^\\x1b\\[\\d+;\\d+R$/.test(data))ownedDsrReports++;});}')
                    session.pty.inbox.put(b'\x1b[6n')
                    await page.wait_for_function('ownedDsrReports===1')
                    assert not replies and session.pty.written == b''
                    await page.evaluate(term + '.focus()')
                    await page.keyboard.type('owned-input')
                    async with asyncio.timeout(3):
                        while bytes(session.pty.written) != b'owned-input':
                            await asyncio.sleep(.01)
                    assert not echoed and not replies
                    assert await page.evaluate(term + '.buffer.active.cursorX') == 2
                    assert await page.evaluate(term + '.buffer.active.cursorY') == 99
                finally:
                    await browser.close()
        daemon.hub.detach(owner.key)
