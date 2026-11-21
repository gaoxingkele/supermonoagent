"""Exercise the actual desktop and Android documents with a synthetic read-only Hub."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path

from websockets.asyncio.server import serve

from android_journal_fixture import Fixture
from test_workspace_profile_rooms import fixture, run


@asynccontextmanager
async def read_service():
    requests = []
    peers = set()

    async def viewer(ws):
        peers.add(ws)
        try:
            hello = json.loads(await ws.recv())
            assert ws.request.path == '/hub/view'
            assert hello == {'op':'hello', 'token':'test-read-only', 'project':'alpha'}
            await ws.send(json.dumps({'op':'ready', 'project':'alpha', 'projects':['alpha','beta']}))
            async for frame in ws:
                msg = json.loads(frame)
                assert 'token' not in msg and msg['op'] == 'request'
                tool, params, scope = msg['tool'], msg['arguments'], msg['project']
                requests.append((tool, params, scope))
                if tool == 'list_tasks':
                    result = {'tasks':[{'project':'beta','task_id':'shared','status':'blocked',
                                        'summary':'等待测试反馈'}], 'next_before':None}
                elif tool == 'get_task_status':
                    assert scope == 'beta' and params == {'task_id':'shared'}
                    result = {'task_id':'shared', 'status':'blocked', 'attempts':[
                        {'host':'machine-two','tool':'codex','window':'项目窗口','attempt_id':'try-1',
                         'role':'primary','summary':'合成的结果','generation':'gen-1','tool_version':'1.0',
                         'wiki':{'entry_id':'wiki1','version':'v1'}}]}
                elif tool == 'get_wiki_entry':
                    assert scope == 'beta'
                    result = {'path':'wiki/result.md','version':'v1','body':'已保存的合成证据：<em>纯文本</em>'}
                elif tool == 'chat':
                    assert params['question'] == '当前结果是什么？'
                    result = {'mode':'recent_events','answer':'合成任务的结论','evidence':['e1']}
                elif tool == 'get_usage_overview':
                    good={'status':'ok','observed_at':'2026-11-21T05:00:00Z',
                          'windows':[{'period':'weekly','unit':'percent','remaining':73,'reset_at':'2026-11-25T00:00:00Z'}]}
                    result = {'accounts':[{'project':'beta','account':'test-account','pool':'shared',
                                           'capacity':{'freshness':'query_failed','eligibility':'unknown',
                                                       'windows':[{'period':'weekly','unit':'percent','remaining':None,'state':'stale'}]},
                                           'latest':{'status':'rate_limited','observed_at':'2026-11-21T06:00:00Z'},
                                           'last_success':good}]}
                elif tool == 'search_skills':
                    result = {'skills':[{'name':'合成技能','id':'sample','revision':'v1'}]}
                elif tool == 'read_skill':
                    result = {'revision':'v1','text':'合成技能正文'}
                elif tool == 'list_evolution':
                    result = {'items':[{'project':'beta','tool':'codex','tool_version':'1.0','skill_id':'sample',
                                        'published_version':'v2','candidate_id':'candidate'}]}
                elif tool == 'get_evolution_status':
                    assert scope == 'beta'
                    result = {'published_version':'v2','pinned_version':'v1','loaded_version':'v1',
                              'loading_state':'loaded','has_update':True,'assessment':{'evaluator':'fixture',
                              'splits':{'holdout':{'verdict':'insufficient_evidence'}}}}
                else:
                    raise AssertionError(tool)
                await ws.send(json.dumps({'op':'response','id':msg['id'],'ok':True,'result':result}))
        finally:
            peers.discard(ws)

    async with serve(viewer,'127.0.0.1',0) as service:
        url='ws://127.0.0.1:'+str(service.sockets[0].getsockname()[1])+'/hub/view'
        yield url,requests,peers


async def exercise(page, *, mobile, url, peers):
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    assert await page.locator('#cockpit-shortcut').is_hidden()
    await page.locator('#options' if mobile else '#app-menu-toggle').click()
    await page.locator('#cockpit-open').click()
    panel = page.locator('#sma-cockpit')
    await panel.get_by_label('驾驶舱服务地址').fill(url)
    await panel.get_by_label('项目', exact=True).fill('alpha')
    await panel.get_by_label('只读访问凭据').fill('test-read-only')
    await panel.get_by_role('button',name='连接项目').click()
    await panel.get_by_label('向总管提问').fill('当前结果是什么？')
    await panel.get_by_role('button',name='发送给总管').click()
    await panel.get_by_text('合成任务的结论',exact=True).wait_for()
    await panel.get_by_role('button',name='待处理',exact=True).click()
    await panel.get_by_role('button',name='查看任务').click()
    await panel.get_by_text('项目窗口',exact=True).wait_for()
    await panel.get_by_role('button',name='查看 wiki 证据').click()
    await panel.get_by_text('已保存的合成证据：<em>纯文本</em>',exact=True).wait_for()
    assert await panel.locator('em').count() == 0
    await page.evaluate('window.SuperMonoCockpit.back()')
    await panel.get_by_text('项目窗口',exact=True).wait_for()
    await panel.get_by_role('button',name='额度',exact=True).click()
    await panel.get_by_text('本周 · 余量未知',exact=True).wait_for()
    await panel.get_by_text('beta · shared · 受限，待重试 · 查询失败 · 可用状态未知',exact=True).wait_for()
    await panel.locator('details summary').click()
    await panel.get_by_text('本周 · 剩余 73 % · 重置',exact=False).last.wait_for()
    assert await panel.evaluate('el=>el.scrollWidth<=el.clientWidth+1 && el.getBoundingClientRect().right<=innerWidth+1')
    artifact_dir = os.environ.get("SMA_COCKPIT_VISUAL_DIR")
    if artifact_dir:
        path = Path(artifact_dir)
        path.mkdir(parents=True,exist_ok=True)
        await page.screenshot(path=str(path / ('android-usage.png' if mobile else 'desktop-usage.png')))
    await panel.get_by_role('button',name='mylib',exact=True).click()
    await panel.get_by_role('button',name='阅读技能').click()
    await panel.get_by_text('合成技能正文',exact=True).wait_for()
    await page.evaluate('window.SuperMonoCockpit.back()')
    await panel.get_by_role('button',name='进化记录').click()
    await panel.get_by_role('button',name='查看版本证据').click()
    await panel.get_by_text('holdout · 证据不足',exact=True).wait_for()
    assert await page.evaluate('!JSON.stringify(localStorage).includes("test-read-only") && !JSON.stringify(sessionStorage).includes("test-read-only")')
    await next(iter(peers)).close(code=1008)
    await panel.get_by_role('button',name='连接中央服务').wait_for()
    assert await panel.get_by_label('选择授权项目').locator('option').count()==0
    assert '合成技能正文' not in await panel.inner_text()
    assert 'holdout · 证据不足' not in await panel.inner_text()
    await panel.get_by_role('button',name='关闭',exact=True).click()
    assert not errors


def test_desktop_cockpit_read_views_do_not_write_or_resize_terminal():
    async def check(playwright):
        async with fixture(playwright) as (browser, url, _endpoint, _trap, _msgs, _daemons, _ops, _wins, sessions):
            async with read_service() as (hub_url, calls, peers):
                page=await browser.new_page(viewport={'width':1280,'height':800})
                await page.goto(url)
                from test_workspace_journal import wait_for_settled_journal_grid
                await wait_for_settled_journal_grid(page)
                await page.evaluate('''() => {
                  const p=smaWorkspace.activePane;
                  window.stageBefore=[p.screen.clientWidth,p.screen.clientHeight];
                  window.hubTerminalWrites=[];
                  const original=p.host.ws.send.bind(p.host.ws);
                  p.host.ws.send=frame=>{
                    if(typeof frame!=='string'){
                      const op=TerminalClient.unpack(new Uint8Array(frame)).op;
                      if([5,17].includes(op))hubTerminalWrites.push(op);
                    }
                    return original(frame);
                  };
                }''')
                await exercise(page,mobile=False,url=hub_url,peers=peers)
                assert calls
                assert await page.evaluate('JSON.stringify(stageBefore)===JSON.stringify([smaWorkspace.activePane.screen.clientWidth,smaWorkspace.activePane.screen.clientHeight])')
                assert not await page.evaluate('hubTerminalWrites')
                assert not any(s.pty.written for owned in sessions for s in owned)
    run(check)


def test_android_cockpit_uses_mobile_document_and_preserves_terminal():
    async def check(playwright):
        from test_workspace_profile_rooms import EDGE
        mobile=Fixture()
        async with serve(mobile.socket,'127.0.0.1',0,process_request=mobile.http) as service:
            mobile.origin='http://127.0.0.1:'+str(service.sockets[0].getsockname()[1])
            async with read_service() as (hub_url,calls,peers), playwright.async_playwright() as runtime:
                browser=await runtime.chromium.launch(executable_path=str(EDGE),headless=True)
                try:
                    page=await browser.new_page(viewport={'width':390,'height':844},is_mobile=True)
                    await page.goto(mobile.origin)
                    await page.locator('a').first.click()
                    await page.wait_for_function('window.MobileTerminal?.ready')
                    baseline=mobile.input_frames,mobile.resize_frames
                    await exercise(page,mobile=True,url=hub_url,peers=peers)
                    assert calls and baseline==(mobile.input_frames,mobile.resize_frames)
                    await page.locator('#cockpit-shortcut').click()
                    for width,height in [(844,390),(768,1200),(1200,768)]:
                        await page.set_viewport_size({'width':width,'height':height})
                        assert await page.locator('#sma-cockpit').evaluate('el=>el.scrollWidth<=el.clientWidth+1 && el.getBoundingClientRect().height<=innerHeight+1')
                    assert baseline==(mobile.input_frames,mobile.resize_frames)
                finally:
                    await browser.close()
    run(check)


def test_local_library_keeps_directory_settings_available_offline():
    async def check(playwright):
        async with fixture(playwright) as (browser,url,*_others):
            page=await browser.new_page(viewport={'width':1100,'height':770})
            await page.add_init_script('''window.pywebview={api:{
              cockpit_library_search: async()=>{throw Error('directory_unavailable');},
              get_assistant_preferences:async()=>({preferences:{library_root:window.testLibrary || 'D:/aicoding/mylib'}}),
              save_assistant_preferences:async p=>{window.testLibrary=p.library_root;}
            }}''')
            await page.goto(url)
            await page.locator('#app-menu-toggle').click()
            await page.locator('#cockpit-open').click()
            panel=page.locator('#sma-cockpit')
            await panel.get_by_role('button',name='mylib',exact=True).click()
            await panel.get_by_role('button',name='目录设置').click()
            field=panel.get_by_label('此桌面电脑的 mylib 目录')
            assert await field.input_value()=='D:/aicoding/mylib'
            await field.fill('C:/test-only/mylib')
            await panel.get_by_role('button',name='保存本机目录').click()
            await panel.get_by_role('button',name='目录设置').click()
            assert await field.input_value()=='C:/test-only/mylib'
    run(check)
