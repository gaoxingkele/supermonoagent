"""Local-library cards and candidates use owned APIs and never type into a PTY."""
from test_assistant_evidence_ui import prepare
from test_workspace_profile_rooms import fixture, run


async def setup(browser, url, sessions):
    page, errors = await prepare(browser, url, sessions)
    await page.evaluate('''()=>{
      window.localCandidate={suggestion_id:'c'.repeat(64),title:'Owned candidate',problem:'Owned',hypothesis:'Owned',falsifier:'Owned',
        source:{revision:'a'.repeat(64),skill_id:'b'.repeat(64),path:'skills/review/SKILL.md',sha256:'d'.repeat(64)},
        validation:{scope:'format_and_source_preservation_only',passed:true},candidate_text:'# Owned candidate'};
      window.applied=[];window.copied=[];
      Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{copied.push(text);}},configurable:true});
      pywebview.api.recommend_skills=async()=>({available:true,indexed:1,
        library:{kind:'local',root:'D:/owned/mylib'},
        skills:[{name:'local-review',source:'skills/review/SKILL.md',description:'<img src=x onerror=window.injected=true>',reason:'fixture'}],evolution:[]});
      pywebview.api.run_library_evolution=async()=>({count:1,items:[localCandidate],summary:{},status:{}});
      pywebview.api.apply_library_evolution=async request=>{applied.push(request);return {path:'skills/review/SKILL.md',requires_commit:true};};
    }''')
    await page.locator('#mylib-toggle').click()
    return page, errors


def test_local_cards_are_escaped_and_have_no_mcp_calls():
    run(check_cards)


async def check_cards(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons, _ops, _windows, sessions):
        page, errors = await setup(browser, url, sessions)
        await page.wait_for_function("document.querySelector('#mylib-library-source').textContent.includes('本机 mylib')")
        assert '本机 mylib' in await page.locator('#mylib-library-source').inner_text()
        assert await page.locator('#mylib-results img').count() == 0
        assert await page.locator('.mylib-read-skill').count() == 0
        await page.locator('#mylib-results button').filter(has_text='复制调用指令').click()
        copied=await page.evaluate('copied')
        assert len(copied)==1 and 'skills/review/SKILL.md' in copied[0] and 'MCP' not in copied[0] and '中央' not in copied[0]
        assert not any(session.pty.written for owned in sessions for session in owned)
        assert not errors


def test_closing_candidate_preview_does_not_apply_it():
    run(check_closed)


async def check_closed(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons, _ops, _windows, sessions):
        page, errors = await setup(browser, url, sessions)
        await page.locator('#mylib-evolve').click()
        await page.locator('.mylib-candidate button').filter(has_text='预览候选').click()
        assert await page.locator('#mylib-skill-preview').evaluate('(el)=>el.open')
        await page.locator('#mylib-skill-preview button').filter(has_text='关闭').click()
        assert not await page.locator('#mylib-skill-preview').evaluate('(el)=>el.open')
        assert await page.evaluate('applied') == []
        assert not any(session.pty.written for owned in sessions for session in owned)
        assert not errors
