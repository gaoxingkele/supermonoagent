"""Native browser clipboard accelerators, owned text and fake PTYs only."""
import pytest
from test_workspace_profile_rooms import fixture, run, eventually, server_barrier


@pytest.mark.parametrize('mode', ['codex', 'kimi', 'grok', 'claude-code', 'pi-dev', 'cursor-cli'])
def test_six_modes_paste_real_browser_clipboard_and_copy_selection_without_interrupt(mode):
    run(lambda playwright: exercise(playwright, mode))


async def exercise(playwright, mode):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages,
                                       _daemons, _ops, _windows, sessions):
        page = await browser.new_page()
        await page.context.grant_permissions(['clipboard-read', 'clipboard-write'])
        await page.goto(url)
        await page.wait_for_function('smaWorkspace.activePane?.ready&&!smaWorkspace.activePane.writing&&!smaWorkspace.activePane.replayPending&&!smaWorkspace.activePane.resizeRequest')
        await page.locator('#agent-mode').select_option(mode)
        sid = await page.evaluate('smaWorkspace.activePane.raw')
        receiver = next(session for session in sessions[0] if session.id == sid)
        text = 'OWNED_PASTE_' + mode
        await page.evaluate('''async text=>{
          window.clipboardFileReads=0;window.supermonoClipboardHasFiles=true;
          window.pywebview={api:{clipboard_files:async()=>{clipboardFileReads++;throw Error('Not a file paste');}}};
          await navigator.clipboard.writeText(text);smaWorkspace.terminal.focus();
        }''', text)
        await page.keyboard.press('Control+v')
        await server_barrier(page, 'local')
        await eventually(lambda: bytes(receiver.pty.written) == text.encode())
        assert await page.evaluate('clipboardFileReads') == 0
        receiver.pty.written.clear()
        copied = 'OWNED_COPY_' + mode
        await page.evaluate(r'''async text=>{
          const t=smaWorkspace.terminal;await new Promise(done=>t.write('\x1b[2J\x1b[H'+text+'\r\n',done));
          t.select(0,0,text.length);t.focus();
        }''', copied)
        await page.keyboard.press('Control+c')
        await server_barrier(page, 'local')
        assert not receiver.pty.written, 'Copying selected output must not send Ctrl+C to the CLI'
        await page.evaluate('''()=>{const e=document.createElement('textarea');e.id='owned-copy-result';document.body.append(e);e.focus();}''')
        await page.keyboard.press('Control+v')
        await page.wait_for_function('expected=>document.querySelector("#owned-copy-result").value===expected', arg=copied)
        await page.evaluate('()=>{smaWorkspace.terminal.clearSelection();smaWorkspace.terminal.focus();}')
        await page.keyboard.press('Control+c')
        await server_barrier(page, 'local')
        await eventually(lambda: bytes(receiver.pty.written) == b'\x03')
