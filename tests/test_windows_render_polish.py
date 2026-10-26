"""Owned Windows label/font regressions; no production terminals or settings."""
import pytest

from test_workspace_profile_rooms import fixture, run, add_profile, wait_room


def test_manual_mode_labels_are_consistent_persisted_and_room_scoped_with_same_session_id():
    run(check_labels)


async def check_labels(playwright):
    async with fixture(playwright) as (browser, url, endpoint, _trap, _messages,
                                       daemons, _ops, windows, sessions):
        local, remote = sessions[0][0], sessions[1][0]
        old = remote.id
        remote.id = local.id
        daemons[1].hub.sessions[remote.id] = daemons[1].hub.sessions.pop(old)
        windows[1][0].sessions[remote.id] = windows[1][0].sessions.pop(old)
        windows[1][0].focused = remote.id
        for index in range(2): windows[index][0].title = 'Opaque fixture ' + str(index)
        page = await browser.new_page(viewport={'width': 1200, 'height': 800})
        await page.goto(url)
        await page.wait_for_function('smaWorkspace.activePane?.ready')
        await page.evaluate("sid=>smaWorkspace.openHostSession('local',sid,false)", local.id)
        await page.locator('#agent-mode').select_option('kimi')
        await page.wait_for_function("smaWorkspace.activePane.agentProfile==='kimi'")
        await assert_label(page, 'local', local.id, 'Kimi')
        await page.evaluate('smaWorkspace.closeView(smaWorkspace.activeSession)')
        await page.evaluate("sid=>smaWorkspace.openHostSession('local',sid,false)", local.id)
        await assert_label(page, 'local', local.id, 'Kimi')
        await add_profile(page, endpoint, daemons[1].token)
        await wait_room(page, 1)
        await page.locator('#relay-profiles-close').click()
        remote_host = await page.evaluate("[...smaWorkspace.connections.values()].find(h=>h.config.code==='profile-room-1').id")
        await page.evaluate("({host,sid})=>smaWorkspace.openHostSession(host,sid,false)", {'host': remote_host, 'sid': remote.id})
        assert await page.locator('#agent-mode').input_value() == 'auto'
        await page.locator('#agent-mode').select_option('grok')
        await page.wait_for_function("smaWorkspace.activePane.agentProfile==='grok'")
        await assert_label(page, remote_host, remote.id, 'Grok')
        # Also collide on the same relay endpoint: the room remains part of
        # the saved mode scope, rather than treating raw SID alone as identity.
        await add_profile(page, endpoint, daemons[0].token)
        await wait_room(page, 0)
        await page.locator('#relay-profiles-close').click()
        room_zero = await page.evaluate("[...smaWorkspace.connections.values()].find(h=>h.config.code==='profile-room-0').id")
        await page.evaluate("({host,sid})=>smaWorkspace.openHostSession(host,sid,false)", {'host': room_zero, 'sid': local.id})
        assert await page.locator('#agent-mode').input_value() == 'auto'
        await page.locator('#agent-mode').select_option('kimi')
        await assert_label(page, room_zero, local.id, 'Kimi')
        await page.evaluate("({host,sid})=>smaWorkspace.openHostSession(host,sid,false)", {'host': remote_host, 'sid': remote.id})
        await assert_label(page, remote_host, remote.id, 'Grok')
        await page.evaluate("sid=>smaWorkspace.openHostSession('local',sid,false)", local.id)
        await assert_label(page, 'local', local.id, 'Kimi')
        await page.reload()
        await page.wait_for_function('smaWorkspace.activePane?.ready')
        await page.evaluate("sid=>smaWorkspace.openHostSession('local',sid,false)", local.id)
        await assert_label(page, 'local', local.id, 'Kimi')
        await page.evaluate("({host,sid})=>smaWorkspace.openHostSession(host,sid,false)", {'host': remote_host, 'sid': remote.id})
        await assert_label(page, remote_host, remote.id, 'Grok')
        # Explicit auto removes the display preference rather than modifying
        # daemon identity or retaining the previous manual classification.
        await page.locator('#agent-mode').select_option('auto')
        await assert_label(page, remote_host, remote.id, '其他')
        assert not any(s.pty.written for owned in sessions for s in owned)


async def assert_label(page, host, sid, label):
    await page.wait_for_function('''({host,sid,label})=>{
        const p=smaWorkspace.activePane;
        const row=[...document.querySelectorAll('.session-row')].find(e=>e.dataset.host===host&&e.dataset.session===sid);
        return p?.host.id===host&&p.raw===sid&&p.title.textContent.endsWith('（'+label+'）')
            &&row?.querySelector('.session-shell')?.textContent==='（'+label+'）';
    }''', arg={'host': host, 'sid': sid, 'label': label}, timeout=3000)


@pytest.mark.parametrize('renderer', ['dom', 'webgl'])
def test_windows_cjk_sans_fallback_keeps_grid_cursor_and_input_bytes(renderer, tmp_path):
    run(lambda playwright: check_font(playwright, renderer, tmp_path))


async def check_font(playwright, renderer, tmp_path):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages,
                                       _daemons, _ops, _windows, sessions):
        page = await browser.new_page(viewport={'width': 1200, 'height': 800})
        await page.goto(url)
        await page.wait_for_function('smaWorkspace.activePane?.ready')
        family = await page.evaluate('smaWorkspace.activePane.term.options.fontFamily')
        assert family.index('Microsoft YaHei UI') < family.index('monospace')
        assert family.index('Consolas') < family.index('Microsoft YaHei UI')
        result = await page.evaluate(r'''async ({family,renderer})=>{
            const box=document.createElement('section');box.id='font-owned';box.style.cssText='position:fixed;inset:0;z-index:3000;background:#11151b;padding:24px';document.body.append(box);
            window.fontTerms=[];const out=[];
            for(const font of ['"Cascadia Mono", "Cascadia Code", Consolas, monospace',family]){
                const label=document.createElement('p');label.textContent=font;box.append(label);
                const el=document.createElement('div');el.style.cssText='width:1000px;height:150px';box.append(el);
                const t=new Terminal({cols:60,rows:4,allowProposedApi:true,fontSize:14,fontFamily:font,cursorBlink:false});
                t.loadAddon(new Unicode11Addon.Unicode11Addon());t.unicode.activeVersion='11';t.open(el);
                if(renderer==='webgl')t.loadAddon(new WebglAddon.WebglAddon());
                let input='';t.onData(value=>input+=value);
                await new Promise(done=>t.write('Latin iW 中文无衬线字体\r\napp.py 42\x1b[2;3H',done));
                await new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(done)));
                const b=t.buffer.active,cell=t._core._renderService.dimensions.css.cell;
                out.push({cols:t.cols,rows:t.rows,cursor:[b.cursorX,b.cursorY],cell:{width:cell.width,height:cell.height},
                    text:b.getLine(0).translateToString(true),wide:b.getLine(0).getCell(9).getWidth(),input});fontTerms.push(t);
            }
            fontTerms[1].focus();window.fontInputs=[];fontTerms[1].onData(value=>fontInputs.push(value));return out;
        }''', {'family': family, 'renderer': renderer})
        assert result[0] == result[1]
        assert result[1]['cursor'] == [2, 1] and result[1]['wide'] == 2
        await page.keyboard.type('owned')
        assert await page.evaluate('fontInputs.join("")') == 'owned'
        await page.locator('#font-owned').screenshot(path=str(tmp_path / f'cjk-{renderer}.png'))
        assert not any(s.pty.written for owned in sessions for s in owned)
        await page.evaluate('()=>{fontTerms.forEach(t=>t.dispose());document.querySelector("#font-owned").remove();}')
