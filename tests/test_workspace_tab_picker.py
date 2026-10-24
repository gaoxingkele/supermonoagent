"""Navigation uses only fixture-owned terminals and preserves terminal bytes."""
from pathlib import Path

from test_workspace_profile_rooms import fixture, run, server_barrier


def test_window_actions_start_in_header_before_javascript_runs():
    from supermonoagent.workspace import PAGE

    header = PAGE.split('<header class="titlebar">', 1)[1].split('</header>', 1)[0]
    tab_row = PAGE.split('<div class="workspace-toolbar">', 1)[1].split('<div id="notice"', 1)[0]
    for control in ('preset', 'new-session', 'split-horizontal', 'split-vertical', 'maximize'):
        assert f'id="{control}"' in header
        assert f'id="{control}"' not in tab_row
    assert 'id="tabs"' in tab_row


def test_all_tabs_menu_reaches_overflow_and_keeps_terminal_input_untouched(tmp_path):
    run(lambda playwright: check_picker(playwright, tmp_path))


async def check_picker(playwright, tmp_path):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, ops, _windows, sessions):
        for i in range(16):
            win = daemons[0].hub.open_window('Extra window ' + str(i), owner=daemons[0].token)
            sessions[0].append(daemons[0].hub.open_cli(win.id, 'cmd'))
        page = await browser.new_page(viewport={'width': 1000, 'height': 750})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.goto(url)
        await page.wait_for_function('smaWorkspace?.connected&&smaWorkspace.panes.size===18')
        await server_barrier(page, 'local')
        toggle = page.locator('#tabs-overflow-toggle')
        assert await toggle.is_visible()
        for name in ['new-session', 'split-horizontal', 'split-vertical', 'maximize']:
            assert await page.locator('.titlebar #' + name).count() == 1
            assert await page.locator('.workspace-toolbar #' + name).count() == 0
        assert await page.locator('.workspace-toolbar .tab-scroll').count() == 2
        assert await page.locator('#new-session').evaluate('(el)=>el.getBoundingClientRect().bottom<=document.querySelector(".workspace-toolbar").getBoundingClientRect().top')
        await page.screenshot(path=str(tmp_path / 'tab-toolbar-desktop.png'))
        await toggle.click()
        assert await page.locator('#tabs-overflow-menu [role=menuitemradio]').count() == 18
        assert await page.locator('#tabs-overflow-menu [aria-checked=true]').count() == 1
        await page.get_by_role('menuitemradio', name='切换到 Extra window 15', exact=True).click()
        assert await page.locator('#tabs-overflow-menu').is_hidden()
        assert await page.locator('#tabs [aria-selected=true]').inner_text() == 'Extra window 15'
        assert await page.evaluate('()=>{const t=document.querySelector("#tabs"),c=t.querySelector("[aria-selected=true]");const a=t.getBoundingClientRect(),b=c.getBoundingClientRect();return b.left>=a.left-1&&b.right<=a.right+1;}')
        before = await page.locator('#tabs').evaluate('(el)=>el.scrollLeft')
        await page.locator('#tabs-scroll-left').click()
        await page.wait_for_function('before=>document.querySelector("#tabs").scrollLeft<before', arg=before)
        before_right = await page.locator('#tabs').evaluate('(el)=>el.scrollLeft')
        await page.locator('#tabs-scroll-right').click()
        await page.wait_for_function('before=>document.querySelector("#tabs").scrollLeft>before', arg=before_right)
        await toggle.click()
        await page.keyboard.press('Escape')
        assert await toggle.evaluate('(el)=>el===document.activeElement')
        await page.set_viewport_size({'width': 430, 'height': 800})
        await toggle.click()
        assert await page.evaluate('()=>{const r=document.querySelector("#tabs-overflow-menu").getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.bottom<=innerHeight;}')
        assert await page.locator('#tabs-scroll-right').is_visible()
        await page.locator('#tabs-overflow-menu').screenshot(path=str(tmp_path / 'tab-picker-mobile.png'))
        await page.screenshot(path=str(tmp_path / 'tab-toolbar-mobile.png'))
        await page.keyboard.press('Escape')
        await page.locator('#sidebar-toggle').click()
        await page.wait_for_function('document.querySelector("#app").classList.contains("mobile-sidebar")')
        assert await page.locator('#sidebar').evaluate('(el)=>el.getBoundingClientRect().top>=document.querySelector(".titlebar").getBoundingClientRect().bottom-1')
        assert not errors
        assert not any(session.pty.written for owned in sessions for session in owned)


def test_assistant_script_is_readonly_and_rejects_stale_results(tmp_path):
    run(lambda playwright: check_assistant(playwright, tmp_path))


async def check_assistant(playwright, tmp_path):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons, _ops, _windows, sessions):
        for index, session in enumerate(sessions[0]):
            root = tmp_path / ('project-' + str(index))
            (root / '.metarsi/wiki').mkdir(parents=True)
            (root / '.metarsi/constitution.json').write_text('{}')
            (root / '.metarsi/wiki/README.md').write_text('LEDGER-' + str(index))
            session.cwd = str(root)
        page = await browser.new_page(viewport={'width': 1400, 'height': 850})
        await page.goto(url)
        await page.wait_for_function('smaWorkspace?.connected&&smaWorkspace.panes.size===2')
        await page.evaluate('sid=>smaWorkspace.openHostSession("local",sid,false)', sessions[0][0].id)
        await page.evaluate('()=>{window.assistantRequests=[];window.pywebview={api:{recommend_skills:context=>new Promise(resolve=>assistantRequests.push({context,resolve}))}};}')
        script = Path('src/supermonoagent/static/mylib-assistant.js').read_text(encoding='utf-8')
        await page.evaluate(script)
        await page.locator('#mylib-toggle').click()
        await page.wait_for_function('assistantRequests.length===1')
        original = await page.evaluate('smaWorkspace.activeSession')
        await page.evaluate('()=>smaWorkspace.openSession([...smaWorkspace.panes.keys()].find(id=>id!==smaWorkspace.activeSession),false)')
        await page.locator('#mylib-refresh').click()
        await page.wait_for_function('assistantRequests.length===2')
        await page.evaluate('()=>assistantRequests[0].resolve({available:true,indexed:1,skills:[{name:"STALE",description:"old",source:"old",reason:"old"}]})')
        assert 'STALE' not in await page.locator('#mylib-results').inner_text()
        await page.evaluate('()=>assistantRequests[1].resolve({available:true,indexed:1,skills:[{name:"<img src=x>",description:"safe",source:"skills/test/SKILL.md",reason:"test"}],evolution:[{name:"验证线索",evidence:["<img src=x>"],next_step:"先查账",falsifier:"独立验证失败则否决"}]})')
        await page.wait_for_function('document.querySelector("#mylib-results").textContent.includes("<img src=x>")')
        assert await page.locator('#mylib-results img').count() == 0
        assert 'LEDGER-1' in await page.locator('#mylib-project').text_content()
        assert 'LEDGER-0' not in await page.locator('#mylib-project').text_content()
        assert '待验证' in await page.locator('#mylib-evolution').inner_text()
        assert await page.locator('#mylib-evolution img').count() == 0
        await page.locator('#mylib-toggle').click()
        assert not await page.locator('body').evaluate('(el)=>el.classList.contains("mylib-open")')
        assert await page.evaluate('smaWorkspace.activeSession') != original
        assert not any(session.pty.written for owned in sessions for session in owned)
