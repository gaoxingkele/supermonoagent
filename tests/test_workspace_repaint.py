"""Visible WebGL panes receive a full repaint when shown again (stale glyph overlay fix)."""
from test_workspace_profile_rooms import fixture, run


def test_switching_views_repaints_visible_webgl_pane():
    run(check_repaint)


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
