"""Owned Edge input events: no operating-system clipboard or real CLI access."""
from pathlib import Path

from test_workspace_profile_rooms import fixture, run, eventually, server_barrier


ROOT = Path(__file__).resolve().parents[1]


def test_claude_plain_paste_survives_sidecar_reinjection_and_ctrl_v_key_path():
    run(check_key_path)


async def check_key_path(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages,
                                       _daemons, _ops, _windows, sessions):
        for session in sessions[0]:
            session.task_detector.app = 'claude-code'
        page = await browser.new_page(viewport={'width': 1200, 'height': 800})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.goto(url)
        await page.wait_for_function('''()=>{const p=smaWorkspace.activePane;return p?.ready
            &&!p.writing&&!p.replayPending&&!p.resizeRequest;}''')
        sid = await page.evaluate('smaWorkspace.activePane.raw')
        selected = next(session for session in sessions[0] if session.id == sid)
        # Mimic a native shell re-injecting bundled modules after the web bundle.
        # Both modules must retain their one-instance guards.
        for _ in range(2):
            for name in ('clipboard-files.js', 'terminal-composer.js'):
                await page.evaluate((ROOT / 'src/supermonoagent/static' / name).read_text(encoding='utf-8'))
        assert await page.locator('#terminal-composer').count() == 1
        assert await page.locator('#terminal-composer-open').count() == 1
        assert await page.locator('#file-transfer-progress').count() == 1
        assert not await page.locator('#terminal-composer-text').evaluate('(e)=>!!e.closest(".terminal-screen")')
        await page.evaluate('''()=>{
            window.bridgeCalls=0;window.supermonoClipboardHasFiles=true;
            window.pywebview={api:{clipboard_has_files:()=>{bridgeCalls++;return new Promise(()=>{});}}};
            const term=smaWorkspace.terminal;term.focus();
            window.keyEvidence=[];window.pasteTargets=0;
            term.textarea.addEventListener('keydown',e=>{
                if(e.ctrlKey&&e.code==='KeyV')keyEvidence.push({phase:'target',prevented:e.defaultPrevented});
            },true);
            term.textarea.addEventListener('paste',()=>pasteTargets++,true);
            // Cancel only at the final bubble boundary, before browser default:
            // physical accelerator delivery is exercised without reading OS data.
            window.addEventListener('keydown',e=>{
                if(e.ctrlKey&&e.code==='KeyV'){
                    keyEvidence.push({phase:'window',prevented:e.defaultPrevented});e.preventDefault();
                }
            });
        }''')
        await page.wait_for_function('document.activeElement===smaWorkspace.terminal.textarea')
        state = await page.evaluate('''()=>({disabled:smaWorkspace.terminal.options.disableStdin,
            readonly:smaWorkspace.terminal.textarea.readOnly})''')
        assert state == {'disabled': False, 'readonly': False}
        await page.keyboard.press('Control+v')
        evidence = await page.evaluate('keyEvidence')
        assert evidence and evidence[0]['phase'] == 'target'
        assert any(item['phase']=='window' and item['prevented'] is False for item in evidence)
        await server_barrier(page, 'local')
        # Compare the same physical key with an unconfigured, standalone xterm.
        # xterm's current Windows key map emits SYN (0x16); this is not a new
        # composer/attachment handler, and does not imply OS paste was tested.
        await page.evaluate('''()=>{const el=document.createElement('div');document.body.append(el);
            window.vanillaTerm=new Terminal();vanillaTerm.open(el);window.vanillaData='';
            vanillaTerm.onData(s=>vanillaData+=s);vanillaTerm.focus();}''')
        await page.keyboard.press('Control+v')
        assert await page.evaluate('vanillaData') == '\x16'
        assert bytes(selected.pty.written) == b''
        await page.evaluate('()=>{vanillaTerm.dispose();smaWorkspace.terminal.focus();}')
        await page.keyboard.type('typed:')
        text = 'owned 中文 "quote" C:\\fixture\\text'
        await page.evaluate('''text=>{const d=new DataTransfer();d.setData('text/plain',text);
            document.activeElement.dispatchEvent(new ClipboardEvent('paste',{
                clipboardData:d,bubbles:true,cancelable:true}));}''', text)
        await server_barrier(page, 'local')
        await eventually(lambda: bytes(selected.pty.written) == ('typed:' + text).encode())
        assert await page.evaluate('bridgeCalls') == 0
        assert await page.evaluate('pasteTargets') == 1
        assert await page.evaluate('document.activeElement===smaWorkspace.terminal.textarea')
        assert not errors
