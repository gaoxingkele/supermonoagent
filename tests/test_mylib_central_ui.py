"""Central-library previews use owned native APIs and never type into a PTY."""
from test_assistant_evidence_ui import prepare
from test_workspace_profile_rooms import fixture, run


def test_central_preview_is_pinned_escaped_and_marks_cached_catalog():
    run(check_preview)


async def setup(browser, url, sessions, *, gated=False):
    page, errors = await prepare(browser, url, sessions)
    await page.evaluate('''gated=>{
      window.centralReads=[];window.centralPending=[];
      pywebview.api.recommend_skills=async()=>({available:true,indexed:1855,
        library:{kind:'central',revision:'a'.repeat(40),stale:true},
        skills:[{id:'b'.repeat(64),revision:'a'.repeat(40),name:'central-review',
          source:'skills/review/SKILL.md',description:'<img src=x onerror=window.injected=true>',reason:'fixture'}]});
      pywebview.api.read_assistant_skill=request=>{centralReads.push(request);
        return gated?new Promise(resolve=>centralPending.push(resolve)):Promise.resolve({text:'<script>window.injected=true</script>',truncated:true});};
    }''', gated)
    await page.locator('#mylib-toggle').click()
    await page.locator('.mylib-read-skill').wait_for(state='visible')
    return page, errors


async def check_preview(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons, _ops, _windows, sessions):
        page, errors = await setup(browser, url, sessions)
        assert '中央 mylib' in await page.locator('#mylib-library-source').inner_text()
        assert '缓存目录' in await page.locator('#mylib-library-source').inner_text()
        assert await page.locator('#mylib-results img').count() == 0
        await page.locator('.mylib-read-skill').click()
        await page.wait_for_function('document.querySelector("#mylib-skill-preview pre").textContent.includes("<script>")')
        assert await page.evaluate('centralReads') == [{'skill_id': 'b' * 64, 'revision': 'a' * 40}]
        assert await page.locator('#mylib-skill-preview script').count() == 0
        assert not await page.evaluate('window.injected===true')
        assert '[正文已截断]' in await page.locator('#mylib-skill-preview pre').inner_text()
        await page.locator('#mylib-skill-preview button').click()
        assert not any(session.pty.written for owned in sessions for session in owned)
        assert not errors


def test_late_preview_cannot_show_old_window_content_or_reopen_closed_dialog():
    run(check_stale)


async def check_stale(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons, _ops, _windows, sessions):
        page, errors = await setup(browser, url, sessions, gated=True)
        await page.locator('.mylib-read-skill').click()
        await page.evaluate('()=>smaWorkspace.openSession([...smaWorkspace.panes.keys()].find(id=>id!==smaWorkspace.activeSession),false)')
        await page.evaluate('centralPending.shift()({text:"STALE PREVIEW"})')
        await page.wait_for_function('document.querySelector("#mylib-skill-preview pre").textContent.includes("窗口已切换")')
        assert 'STALE PREVIEW' not in await page.locator('#mylib-skill-preview pre').inner_text()
        await page.locator('#mylib-skill-preview button').click()
        await page.locator('#mylib-refresh').click()
        await page.locator('.mylib-read-skill').click()
        await page.locator('#mylib-skill-preview button').click()
        await page.evaluate('centralPending.shift()({text:"CLOSED PREVIEW"})')
        assert not await page.locator('#mylib-skill-preview').evaluate('(el)=>el.open')
        assert 'CLOSED PREVIEW' not in await page.locator('#mylib-skill-preview pre').text_content()
        assert not any(session.pty.written for owned in sessions for session in owned)
        assert not errors
