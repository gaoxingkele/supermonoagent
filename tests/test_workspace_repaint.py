"""Visible WebGL panes receive a full repaint when shown again (stale glyph overlay fix)."""
import pytest

from test_workspace_profile_rooms import fixture, run


def test_switching_views_repaints_visible_webgl_pane():
    run(check_repaint)


@pytest.mark.parametrize('renderer', ['dom', 'webgl'])
def test_switching_tabs_restores_native_scroll_position(renderer):
    run(lambda playwright: check_scroll_position(playwright, renderer))


async def check_scroll_position(playwright, renderer):
    scroll_state = '''() => {
      const pane = smaWorkspace.activePane;
      const buffer = pane.term.buffer.active;
      const viewport = pane.screen.querySelector('.xterm-viewport');
      return {top: viewport.scrollTop, row: buffer.viewportY, follow: pane.followOutput};
    }'''
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons, _ops, _windows, sessions):
        first, second = sessions[0]
        page = await browser.new_page(viewport={'width': 1100, 'height': 760})
        await page.goto(url.replace('renderer=dom', f'renderer={renderer}'))
        await page.wait_for_function('smaWorkspace.activePane?.ready')
        await page.evaluate('id => smaWorkspace.openSession(id, false)', first.id)
        first.pty.inbox.put(('\r\n' + ''.join(f'HISTORY_{i:04d}\r\n' for i in range(400))).encode())
        await page.wait_for_function('smaWorkspace.terminal.buffer.active.baseY > 300 && smaWorkspace.activePane.ready')
        await page.get_by_role('button', name='向上翻动一页历史', exact=True).click()
        await page.wait_for_function('''() => {
          const pane = smaWorkspace.activePane;
          const buffer = pane.term.buffer.active;
          return !pane.followOutput && buffer.viewportY < buffer.baseY
            && pane.screen.querySelector('.xterm-viewport').scrollTop > 0;
        }''')
        await page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        before = await page.evaluate(scroll_state)
        await page.evaluate('id => smaWorkspace.openSession(id, false)', second.id)
        await page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        await page.evaluate('id => smaWorkspace.openSession(id, false)', first.id)
        await page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        assert await page.evaluate(scroll_state) == before
        await page.locator('.pane.active .terminal-screen').hover(timeout=3000)
        await page.mouse.wheel(0, -120)
        await page.wait_for_function('row => smaWorkspace.terminal.buffer.active.viewportY < row', arg=before['row'])
        assert not first.pty.written


async def check_repaint(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons, _ops, _windows, sessions):
        page = await browser.new_page(viewport={'width': 1100, 'height': 850})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.goto(url)
        await page.wait_for_function('window.smaWorkspace?.connected && smaWorkspace.activePane?.ready')
        first, second = sessions[0][0].id, sessions[0][1].id
        await page.evaluate('sid=>smaWorkspace.openSession(sid,false)', first)
        await page.evaluate('sid=>smaWorkspace.openSession(sid,false)', second)
        await page.wait_for_function('smaWorkspace.activePane?.ready')
        baseline = await page.evaluate('''([a,b])=>{
          const counts={};
          for(const sid of [a,b]){
            const pane=smaWorkspace.panes.get(JSON.stringify(['local',sid]));
            counts[sid]=0;const term=pane.term,original=term.refresh.bind(term);
            pane.renderer='webgl';
            term.refresh=(start,end)=>{counts[sid]++;return original(start,end);};
          }
          window.repaintCounts=counts;return counts;
        }''', [first, second])
        # Switching back to the first view must repaint the now-visible pane...
        await page.evaluate('sid=>smaWorkspace.openSession(sid,false)', first)
        await page.wait_for_function(f'window.repaintCounts[{first!r}]>0')
        counts = await page.evaluate('window.repaintCounts')
        assert counts[first] > 0, counts
        assert counts[second] == 0, counts
        # ...and switching onward repaints it in turn without touching hidden panes.
        after_first = await page.evaluate('window.repaintCounts')
        await page.evaluate('sid=>smaWorkspace.openSession(sid,false)', second)
        await page.wait_for_function(f'window.repaintCounts[{second!r}]>0')
        counts = await page.evaluate('window.repaintCounts')
        assert counts[first] == after_first[first], counts
        assert not errors
