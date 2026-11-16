"""Claude alternate-screen windows must scroll through the agent itself, not the plain-text archive."""
from pathlib import Path
import json

import pytest

from test_workspace_profile_rooms import fixture, run

CAPTURE = Path('.omx/artifacts/claude-scroll-repro/claude-tui-capture.bin')


@pytest.mark.parametrize('renderer', ['dom', 'webgl'])
def test_claude_window_wheel_reaches_agent(renderer):
    run(lambda playwright: check(playwright, renderer))


async def check(playwright, renderer):
    if not CAPTURE.exists():
        pytest.skip('real Claude capture missing')
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, ops, _windows, sessions):
        pty = sessions[0][0].pty
        pty.inbox.put(b'\x1b]7501;app=claude-code\x07')
        pty.inbox.put(CAPTURE.read_bytes())
        page = await browser.new_page(viewport={'width': 1536, 'height': 960})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.goto(url.replace('renderer=dom', 'renderer=' + renderer))
        await page.wait_for_function("smaWorkspace?.activePane?.ready")
        await page.wait_for_function("smaWorkspace.activePane.term.buffer.active.type==='alternate'")
        assert await page.evaluate("smaWorkspace.activePane.host.agents.get(smaWorkspace.activePane.raw)") in ('claude', 'claude-code')
        pane_state = '''()=>{
          const p=smaWorkspace.activePane;
          const buffer=p.term.buffer.active;
          return {historyMode:p.historyMode,archive:p.screenArchive.length,
                  viewportY:buffer.viewportY,baseY:buffer.baseY};
        }'''
        before = await page.evaluate(pane_state)
        assert not before['historyMode']

        box = await page.evaluate("()=>{const el=smaWorkspace.activePane.screen;const r=el.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};}")
        await page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)

        # The real Claude welcome screen enables SGR mouse tracking from the start,
        # so the wheel must reach Claude as xterm-translated mouse button 64/65.
        written_before = len(pty.written)
        await page.mouse.wheel(0, -360)
        await page.wait_for_timeout(600)
        after = await page.evaluate(pane_state)
        written = bytes(pty.written[written_before:])
        assert b'\x1b[<64' in written, f'wheel never reached Claude as SGR mouse: {written[:80]!r}'
        assert not after['historyMode'], 'plain-text archive hijacked the Claude window'
        written_before = len(pty.written)
        await page.mouse.wheel(0, 240)
        await page.wait_for_timeout(400)
        written = bytes(pty.written[written_before:])
        assert b'\x1b[<65' in written, f'wheel down not forwarded: {written[:80]!r}'

        # Without mouse tracking (agent exited its scroll mode) the wheel pages
        # through the agent itself instead of the local plain-text archive.
        pty.inbox.put(b'\x1b[?1003l\x1b[?1002l\x1b[?1000l')
        await page.wait_for_timeout(400)
        written_before = len(pty.written)
        await page.mouse.wheel(0, -360)
        await page.wait_for_timeout(600)
        after_keys = await page.evaluate(pane_state)
        written = bytes(pty.written[written_before:])
        assert b'\x1b[5~' in written, f'wheel without mouse mode never paged Claude: {written[:80]!r}'
        assert not after_keys['historyMode'], 'archive hijack without mouse tracking'

        # The page buttons keep using the same native paging route.
        written_before = len(pty.written)
        clicked = await page.evaluate("()=>{const b=[...smaWorkspace.activePane.el.querySelectorAll('.pane-scroll button')].find(b=>b.textContent.includes('上翻屏'));if(b)b.click();return b?b.title:null;}")
        assert clicked, 'page-up button missing'
        await page.wait_for_timeout(400)
        written = bytes(pty.written[written_before:])
        assert b'\x1b[5~' in written, f'page-up button bypassed the agent: {written[:80]!r}'

        artifacts = Path('.omx/artifacts/claude-scroll-repro')
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / f'wheel-{renderer}-result.json').write_text(json.dumps({
            'renderer': renderer, 'before': before, 'after': after, 'after_keys': after_keys,
            'verdict': 'pass',
        }, indent=2), encoding='utf-8')
        await page.screenshot(path=str(artifacts / f'wheel-{renderer}-after.png'))
        assert not errors

        await server_barrier_close(page)


async def server_barrier_close(page):
    # Let pending writes settle before teardown.
    await page.wait_for_timeout(200)
