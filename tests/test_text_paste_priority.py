"""Plain text must never wait for optional native file clipboard classification."""
from pathlib import Path
import pytest
from test_workspace_profile_rooms import fixture, run, eventually, server_barrier


@pytest.mark.parametrize('bridge', ['hung', 'says_files', 'throws'])
def test_plain_text_bypasses_stale_native_file_beacon_and_bridge(bridge):
    run(lambda playwright: exercise(playwright, bridge))


async def exercise(playwright, bridge):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons, _ops, _windows, sessions):
        page = await browser.new_page(viewport={'width':1200,'height':800})
        await page.goto(url)
        await page.wait_for_function('smaWorkspace?.connected&&smaWorkspace.activePane?.ready')
        await page.locator('#agent-mode').select_option('claude-code')
        await page.evaluate('''kind=>{
            window.fileClipboardCalls=0;window.supermonoClipboardHasFiles=true;
            window.pywebview={api:{clipboard_has_files:()=>{
                fileClipboardCalls++;
                if(kind==='hung')return new Promise(()=>{});
                if(kind==='throws')return Promise.reject(Error('owned bridge unavailable'));
                return Promise.resolve(true);
            },clipboard_files:async()=>{fileClipboardCalls++;throw Error('Plain text must not read native files');}}};
        }''', bridge)
        await page.evaluate(Path('src/supermonoagent/static/clipboard-files.js').read_text('utf-8'))
        sid = await page.evaluate('smaWorkspace.activePane.raw')
        receiver = next(s for s in sessions[0] if s.id == sid)
        await page.evaluate('''()=>{
            smaWorkspace.terminal.focus();
            const data=new DataTransfer();data.setData('text/plain','owned plain clipboard');
            smaWorkspace.terminal.textarea.dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));
        }''')
        await server_barrier(page,'local')
        await eventually(lambda: bytes(receiver.pty.written)==b'owned plain clipboard')
        assert await page.evaluate('fileClipboardCalls') == 0
        assert bytes(receiver.pty.written).count(b'owned plain clipboard') == 1
        assert b'\r' not in receiver.pty.written and b'\n' not in receiver.pty.written
