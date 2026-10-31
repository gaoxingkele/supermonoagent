"""The local mylib menu uses owned API fixtures and never types into a PTY."""
from test_assistant_evidence_ui import prepare
from test_workspace_profile_rooms import fixture, run


def test_local_directory_git_and_evolution_menu():
    run(check_menu)


async def check_menu(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons, _ops, _windows, sessions):
        page, errors = await prepare(browser, url, sessions)
        await page.evaluate('''()=>{
          window.localCalls={saved:[],status:0,pull:0,preview:0,commit:[],evolve:[],apply:[]};
          window.localPreferences={provider:'unconfigured',model:'',endpoint:'',auto_context:false,max_context_chars:8192,library_root:'D:/owned/mylib'};
          const item={suggestion_id:'c'.repeat(64),title:'Owned candidate',problem:'Owned problem',hypothesis:'Owned hypothesis',falsifier:'Owned falsifier',
            source:{revision:'a'.repeat(64),skill_id:'b'.repeat(64),path:'skills/demo/SKILL.md',sha256:'d'.repeat(64)},
            validation:{scope:'format_and_source_preservation_only',passed:true},candidate_text:'# Candidate\\n'};
          window.localCandidate=item;
          pywebview.api.recommend_skills=async()=>({available:true,indexed:1,library:{kind:'local',root:localPreferences.library_root},skills:[{name:'demo',description:'Owned skill',source:'skills/demo/SKILL.md',reason:'owned'}],evolution:[]});
          pywebview.api.get_assistant_preferences=async(...args)=>{if(args.length)throw Error('Expected a zero-argument native call');return {preferences:{...localPreferences},status:'saved',api_key_present:false};};
          pywebview.api.save_assistant_preferences=async value=>{if(value.library_root.includes('missing'))throw Error('directory_unavailable');localCalls.saved.push(value);localPreferences.library_root=value.library_root;return {preferences:value,status:'saved',api_key_present:false};};
          pywebview.api.library_status=async(...args)=>{if(args.length)throw Error('Expected a zero-argument native call');localCalls.status++;const accessible=localPreferences.library_root==='C:/owned/mylib';return {root:localPreferences.library_root,available:accessible,accessible,git:accessible,origin:'https://example.invalid/mylib.git',revision:'a'.repeat(40),changes:[],excluded_count:0};};
          pywebview.api.pull_library=async(...args)=>{if(args.length)throw Error('Expected a zero-argument native call');localCalls.pull++;return {changed:true,revision:'b'.repeat(40),changes:[]};};
          pywebview.api.commit_library_preview=async(...args)=>{if(args.length)throw Error('Expected a zero-argument native call');localCalls.preview++;return {can_commit:true,fingerprint:'f'.repeat(64),stat:'1 file changed',excluded_count:1,items:[{path:'skills/demo/SKILL.md',code:'M',excluded:false},{path:'.metarsi/private',code:'??',excluded:true}]};};
          pywebview.api.commit_library=async request=>{localCalls.commit.push(request);return {revision:'c'.repeat(40),changes:[]};};
          pywebview.api.run_library_evolution=async request=>{localCalls.evolve.push(request);return {count:1,items:[item],summary:{ok:true},status:{available:true}};};
          pywebview.api.apply_library_evolution=async request=>{localCalls.apply.push(request);return {path:'skills/demo/SKILL.md',sha256:'e'.repeat(64),requires_commit:true};};
        }''')
        await page.locator('#mylib-toggle').click()
        panel=page.locator('#mylib-panel')
        assert 'MCP' not in await panel.inner_text() and '中央' not in await panel.inner_text()
        for selector in ('#mylib-directory','#mylib-pull','#mylib-commit','#mylib-evolve'):
            assert await page.locator(selector).is_visible()

        await page.locator('#mylib-directory').click()
        await page.locator('#mylib-directory-root').fill('C:/missing/mylib')
        await page.locator('#mylib-directory-form button[type=submit]').click()
        await page.wait_for_function("document.querySelector('#mylib-panel').dataset.syncBusy==='false'")
        assert await page.locator('#mylib-directory-dialog').evaluate('e=>e.open')
        assert await page.evaluate('localCalls.saved')==[]
        assert '目录不存在或不可访问' in await page.locator('#mylib-directory-status').inner_text()
        source=page.locator('#mylib-library-source')
        assert await source.get_attribute('data-accessible')=='false'
        assert await source.evaluate("e=>getComputedStyle(e,'::before').backgroundColor")=='rgb(102, 102, 102)'

        await page.locator('#mylib-directory-root').fill('C:/owned/mylib')
        await page.locator('#mylib-directory-form button[type=submit]').click()
        assert (await page.evaluate('localCalls.saved'))[0]['library_root']=='C:/owned/mylib'
        await page.wait_for_function("document.querySelector('#mylib-panel').dataset.syncBusy==='false'")
        assert not await page.locator('#mylib-directory-dialog').evaluate('e=>e.open')
        assert await source.get_attribute('data-accessible')=='true'
        assert await source.evaluate("e=>getComputedStyle(e,'::before').backgroundColor")=='rgb(101, 196, 102)'
        assert '匹配 1 项' in await source.inner_text()

        await page.locator('#mylib-pull').click()
        await page.wait_for_function("document.querySelector('#mylib-panel').dataset.syncBusy==='false'")
        assert await page.evaluate('localCalls.pull')==1
        await page.locator('#mylib-commit').click()
        assert await page.locator('#mylib-commit-dialog').evaluate('e=>e.open')
        assert await page.evaluate('localCalls.commit')==[]
        await page.locator('#mylib-commit-confirm').click()
        await page.wait_for_function("document.querySelector('#mylib-panel').dataset.syncBusy==='false'")
        assert (await page.evaluate('localCalls.commit'))[0]['fingerprint']=='f'*64

        await page.locator('#mylib-evolve').click()
        assert await page.locator('.mylib-candidate').count()==1
        await page.locator('.mylib-candidate button').filter(has_text='预览候选').click()
        assert '# Candidate' in await page.locator('#mylib-skill-preview pre').inner_text()
        await page.locator('#mylib-skill-preview button').filter(has_text='采纳候选').click()
        assert (await page.evaluate('localCalls.apply'))[0]['suggestion_id']=='c'*64
        assert not any(session.pty.written for owned in sessions for session in owned)
        assert not errors


def test_remote_directory_git_actions_use_the_selected_host_not_native_mylib():
    run(check_remote_menu)


async def check_remote_menu(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons, _ops, _windows, sessions):
        page, errors = await prepare(browser, url, sessions)
        await page.evaluate('''()=>{
          window.localCalls={saved:[],pull:0};
          window.localPreferences={provider:'unconfigured',model:'',endpoint:'',auto_context:false,max_context_chars:8192,library_root:'D:/native/mylib'};
          pywebview.api.get_assistant_preferences=async()=>({preferences:{...localPreferences},status:'saved',api_key_present:false});
          pywebview.api.save_assistant_preferences=async value=>{localCalls.saved.push(value);return {preferences:value,status:'saved'};};
          pywebview.api.pull_library=async()=>{localCalls.pull++;throw Error('Must not pull native mylib');};
          pywebview.api.library_status=async()=>({root:localPreferences.library_root,accessible:true,git:true,revision:'a'.repeat(40),changes:[]});
          pywebview.api.recommend_skills=async()=>({available:true,indexed:0,skills:[],evolution:[],library:{root:'D:/native/mylib'}});
          const pane=smaWorkspace.activePane;pane.host.id='remote-windows';pane.host.name='Remote Windows';
          window.remoteCalls=[];window.remoteRoot='D:/native/mylib';
          const status=()=>({root:remoteRoot,accessible:true,git:true,revision:'a'.repeat(40),changes:[]});
          window.SuperMonoFileWorkspaces={
            captureTarget:()=>({pane,host:pane.host,machine:pane.host.name,session:pane.raw,generation:pane.host.generation}),
            isCurrent:target=>smaWorkspace.activePane===target.pane&&target.host.connected,
            request:async(target,action,data)=>{
              remoteCalls.push({action,data,host:target.host.id});
              if(action==='library.get')return {mylib_root:remoteRoot,revision:remoteCalls.filter(call=>call.action==='library.set').length};
              if(action==='library.set'){remoteRoot=data.mylib_root;return {mylib_root:remoteRoot,revision:1};}
              if(action==='library.status')return status();
              if(action==='library.pull')return {...status(),changed:false};
              if(action==='library.recommend')return {available:true,indexed:1,skills:[],evolution:[],library:{kind:'local',root:remoteRoot}};
              if(action==='library.commit_preview')return {can_commit:true,fingerprint:'f'.repeat(64),items:[],stat:'',excluded_count:0};
              if(action==='library.commit')return {...status(),revision:'c'.repeat(40)};
              throw Error('Unexpected action: '+action);
            }};
        }''')
        await page.locator('#mylib-toggle').click()
        await page.locator('#mylib-directory').click()
        await page.locator('#mylib-directory-root').fill('C:/remote/mylib')
        await page.locator('#mylib-directory-form button[type=submit]').click()
        await page.wait_for_function("document.querySelector('#mylib-panel').dataset.syncBusy==='false'")
        assert not await page.locator('#mylib-directory-dialog').evaluate('e=>e.open')
        assert await page.locator('#mylib-library-source').get_attribute('data-accessible')=='true'
        assert 'Remote Windows 电脑 mylib · C:/remote/mylib' in await page.locator('#mylib-library-source').inner_text()
        assert await page.evaluate('localCalls.saved')==[]
        assert any(call['action']=='library.set' and call['host']=='remote-windows' and call['data']['mylib_root']=='C:/remote/mylib' for call in await page.evaluate('remoteCalls'))
        await page.locator('#mylib-pull').click()
        await page.wait_for_function("document.querySelector('#mylib-panel').dataset.syncBusy==='false'")
        assert await page.evaluate('localCalls.pull')==0
        assert any(call['action']=='library.pull' and call['host']=='remote-windows' for call in await page.evaluate('remoteCalls'))
        await page.locator('#mylib-commit').click()
        await page.locator('#mylib-commit-confirm').click()
        await page.wait_for_function("document.querySelector('#mylib-panel').dataset.syncBusy==='false'")
        assert any(call['action']=='library.commit' and call['host']=='remote-windows' for call in await page.evaluate('remoteCalls'))
        await page.evaluate("smaWorkspace.activePane.host.id='local'")
        await page.locator('#mylib-refresh').click()
        await page.wait_for_function("document.querySelector('#mylib-library-source').dataset.accessible==='true'")
        assert '本机 mylib' in await page.locator('#mylib-library-source').inner_text()
        assert not any(session.pty.written for owned in sessions for session in owned)
        assert not errors
