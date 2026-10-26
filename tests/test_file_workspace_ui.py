"""File workspace sidecar: owned browser/broker fixtures, never user terminals."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import shutil
import subprocess

import pytest


STATIC = Path(__file__).resolve().parents[1] / 'src/supermonoagent/static'
CLIENT = STATIC / 'file-client.js'
UI = STATIC / 'file-workspace-ui.js'
EDGE = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')


def test_commonjs_file_client_rejects_insecure_urls_without_creating_sockets():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    code = r'''
    const assert=require('node:assert/strict'), api=require(process.argv[1]);
    let opened=0;global.WebSocket=class {constructor(){opened++;}};
    for(const url of ['ws://remote.example/file/ws','ws://127.1/file/ws',
      'wss://user:password@example.test/file/ws','wss://example.test/file/ws?token=bad',
      'wss://example.test/file/ws#bad','wss://example.test/other','wss://example.test/\\file/ws']){
      assert.throws(()=>api.createClient({url,room:'room',token:'fixture-token'}));
    }
    assert.equal(api.brokerURL('https://example.test:443/ws'),'wss://example.test/file/ws');
    assert.equal(api.brokerURL('ws://127.0.0.1:8817/ws'),'ws://127.0.0.1:8817/file/ws');
    const c=api.createClient({url:'ws://127.0.0.1:8819/file/ws',room:'room',token:'fixture-token'});
    assert.equal(opened,0);c.close();
    assert.equal(opened,0);
    '''
    result = subprocess.run([node, '-e', code, str(CLIENT)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def run(check, tmp_path):
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Owned Edge browser unavailable')
    asyncio.run(check(playwright, tmp_path))


@asynccontextmanager
async def browser_page(playwright, url='ws://127.0.0.1:8819/file/ws', *, enabled=True, token='owned-viewer-token', document_url=None):
    async with playwright.async_playwright() as runtime:
        browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
        page = await browser.new_page(viewport={'width': 1250, 'height': 850})
        errors, sockets = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('websocket', lambda socket: sockets.append(socket.url))
        try:
            if document_url:
                await page.goto(document_url)
            await page.set_content('''<!doctype html><html><head><meta charset="utf-8"></head><body>
              <div id="app"><header class="titlebar">Owned file workspace fixture</header><aside id="sidebar"></aside>
              <main><section id="stage">Fixture terminals</section><footer><span id="status">legacy cwd status</span>
              <span class="footer-spacer"></span><select id="agent-mode"><option>自动 Agent</option></select>
              <button id="take-control">尺寸主控</button><span id="dimensions">120 × 40</span></footer></main></div></body></html>''')
            await page.add_style_tag(content=(STATIC / 'workspace.css').read_text(encoding='utf-8'))
            await page.evaluate('''cfg=>{
              window.fixtureInputs=[];window.fixtureEvents=[];
              const hosts=['local','remote'].map((id,index)=>({id,name:'Fixture '+String.fromCharCode(65+index),
                config:{token:cfg.token,relay:cfg.url.replace('/file/ws','/ws'),code:index?'B':'',authMode:'manual'},
                ws:{readyState:1},connected:true,generation:0,
                catalog:[{id:'window1',title:'Window '+String.fromCharCode(65+index),sessions:[{id:'session1'}]}]}));
              const term={write(){fixtureInputs.push('write');throw Error('Forbidden terminal write');},
                paste(){fixtureInputs.push('paste');throw Error('Forbidden terminal paste');},
                input(){fixtureInputs.push('input');throw Error('Forbidden terminal input');}};
              const panes=hosts.map(host=>({host,raw:'session1',sid:JSON.stringify([host.id,'session1']),generation:0,ready:true,term}));
              window.fixtureHosts=hosts;window.fixturePanes=panes;window.fixtureIndex=0;
              window.smaWorkspace={get activePane(){return panes[fixtureIndex];},get activeSession(){return panes[fixtureIndex]?.sid;},
                panes:new Map(panes.map(p=>[p.sid,p])),connections:new Map(hosts.map(h=>[h.id,h]))};
              window.fixtureSelect=index=>{window.fixtureIndex=index;};
              addEventListener('sma-file-workspace-changed',event=>fixtureEvents.push(event.detail));
              if(cfg.enabled)window.SuperMonoFileWorkspaceConfig={enabled:true,broker_url:cfg.url,local_room:'A'};
            }''', {'url': url, 'enabled': enabled, 'token': token})
            await page.evaluate(CLIENT.read_text(encoding='utf-8'))
            await page.evaluate(UI.read_text(encoding='utf-8'))
            yield page, errors, sockets
        finally:
            if not page.is_closed():
                await page.evaluate('window.SuperMonoFileWorkspaces?.destroy()')
            await browser.close()


def test_without_native_configuration_preserves_footer_and_opens_no_network(tmp_path):
    async def exercise(playwright, _tmp_path):
        async with browser_page(playwright, enabled=False) as (page, errors, sockets):
            await page.wait_for_timeout(350)
            assert await page.locator('#file-workspace-footer').count() == 0
            assert not await page.locator('#status').evaluate('(el)=>el.hidden')
            assert await page.locator('#status').inner_text() == 'legacy cwd status'
            assert await page.evaluate('typeof SuperMonoFileWorkspaces') == 'undefined'
            assert not sockets and not errors
    run(exercise, tmp_path)


def test_real_host_default_browse_cas_save_footer_geometry_and_removal(tmp_path):
    async def exercise(playwright, folder):
        from test_file_companion import environment
        async with environment(folder) as (_broker, url, parents, companions):
            for index in range(102):
                (parents[0].project / f'child-{index:03d}').mkdir()
            new_project = folder / 'new-project-A'
            new_project.mkdir()
            async with browser_page(playwright, url) as (page, errors, _sockets):
                await page.wait_for_function('document.querySelector("#file-workspace-project")?.title.includes("project-A")')
                assert await page.locator('#file-workspace-exchange').get_attribute('title') == str(parents[0].project / 'tmp')
                await page.locator('#file-workspace-project').click()
                await page.wait_for_function('!document.querySelector("#file-workspace-save").disabled')
                assert await page.locator('#file-workspace-target').inner_text() == 'Fixture A / Window A'
                await page.locator('#file-browse-project').click()
                await page.wait_for_function('document.querySelectorAll("#file-browse-list button").length===100')
                await page.locator('#file-browse-more').click()
                await page.wait_for_function('document.querySelectorAll("#file-browse-list button").length===2')
                await page.locator('#file-browse-list button').first.click()
                await page.wait_for_function('!document.querySelector("#file-browse-use").disabled')
                await page.locator('#file-browse-use').click()
                chosen = await page.locator('#file-project').input_value()
                assert chosen.endswith('child-100')
                assert await page.locator('#file-exchange').input_value() == str(Path(chosen) / 'tmp')
                await page.locator('#file-project').fill(str(new_project))
                assert await page.locator('#file-exchange').input_value() == str(new_project / 'tmp')
                await page.locator('#file-workspace-save').click()
                await page.wait_for_function('document.querySelector("#file-workspace-state").textContent.includes("已保存")')
                assert await page.locator('#file-workspace-project').get_attribute('title') == str(new_project)
                assert companions[0].workspaces.get('window1', parents[0].project)['project_dir'] == str(new_project)
                assert companions[1].workspaces.get('window1', parents[1].project)['project_dir'] == str(parents[1].project)
                events = await page.evaluate('fixtureEvents')
                assert len(events) == 1 and events[0]['room'] == 'A' and events[0]['revision'] == 1
                assert 'token' not in json.dumps(events)
                await page.locator('#file-workspace-close').click()
                for width in [1250, 760, 390]:
                    await page.set_viewport_size({'width': width, 'height': 850})
                    geometry = await page.evaluate('''()=>{
                      const nodes=[...document.querySelectorAll('#file-workspace-footer,footer')];
                      return nodes.map(el=>{const r=el.getBoundingClientRect();return {left:r.left,right:r.right,width:r.width};});
                    }''')
                    assert all(item['left'] >= -1 and item['right'] <= width + 1 for item in geometry), geometry
                    assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert await page.evaluate('fixtureInputs') == [] and not errors
                await page.evaluate('SuperMonoFileWorkspaces.destroy()')
                assert await page.locator('#file-workspace-footer').count() == 0
                assert not await page.locator('#status').evaluate('(el)=>el.hidden')
            assert all(not parent.pty_written for parent in parents)
    run(exercise, tmp_path)


def test_captured_upload_continues_to_a_after_selecting_b_and_credential_changes_invalidate(tmp_path):
    async def exercise(playwright, folder):
        import base64
        import hashlib
        from test_file_companion import environment
        payload = b'\x89PNG\r\nowned synthetic file fixture'
        async with environment(folder) as (_broker, url, parents, _companions):
            async with browser_page(playwright, url) as (page, errors, _sockets):
                await page.wait_for_function('document.querySelector("#file-workspace-project")?.title.includes("project-A")')
                await page.evaluate('''async data=>{
                  window.capturedA=SuperMonoFileWorkspaces.captureTarget();window.clientA=SuperMonoFileWorkspaces.getClient(capturedA);
                  window.cfgA=await SuperMonoFileWorkspaces.getWorkspace(capturedA);
                  window.uploadA=await SuperMonoFileWorkspaces.request(capturedA,'upload.begin',{
                    original_name:'clipboard.png',size:data.size,sha256:data.sha,window_id:cfgA.window_id,
                    daemon_epoch:cfgA.daemon_epoch,workspace_revision:cfgA.revision,timezone_offset_minutes:0});
                  fixtureSelect(1);
                }''', {'size': len(payload), 'sha': hashlib.sha256(payload).hexdigest()})
                await page.wait_for_function('document.querySelector("#file-workspace-project").title.includes("project-B")')
                assert await page.evaluate('SuperMonoFileWorkspaces.isCurrent(capturedA)&&clientA.connected')
                result = await page.evaluate('''async data=>{
                  await SuperMonoFileWorkspaces.request(capturedA,'upload.chunk',{upload_id:uploadA.upload_id,offset:0,data});
                  const queued=await SuperMonoFileWorkspaces.request(capturedA,'upload.finish',{upload_id:uploadA.upload_id});
                  for(let i=0;i<100;i++){
                    const status=await SuperMonoFileWorkspaces.request(capturedA,'file.status',{file_id:queued.file_id});
                    if(status.state==='saved'||status.state==='failed')return status;
                    await new Promise(resolve=>setTimeout(resolve,30));
                  }throw Error('Fixture delivery timeout');
                }''', base64.b64encode(payload).decode())
                assert result['state'] == 'saved'
                saved = Path(result['receipt']['path'])
                assert saved.parent == parents[0].project / 'tmp' and saved.read_bytes() == payload
                assert not (parents[1].project / 'tmp').exists()
                assert 'project-B' in await page.locator('#file-workspace-project').get_attribute('title')
                await page.evaluate('fixtureHosts[0].config.token="replacement-fixture-token"')
                await page.wait_for_function('!clientA.connected')
                assert not await page.evaluate('SuperMonoFileWorkspaces.isCurrent(capturedA)')
                error = await page.evaluate('SuperMonoFileWorkspaces.request(capturedA,"workspace.get",{}).then(()=>"BAD",e=>e.code)')
                assert error == 'target_changed'
                assert await page.evaluate('SuperMonoFileWorkspaces.isCurrent(SuperMonoFileWorkspaces.captureTarget())')
                assert not errors and await page.evaluate('fixtureInputs') == []
            assert all(not parent.pty_written for parent in parents)
    run(exercise, tmp_path)


def test_late_a_reply_never_replaces_b_footer_or_saves_b_and_cas_conflicts_are_explicit(tmp_path):
    async def exercise(playwright, folder):
        from test_file_companion import environment
        async with environment(folder) as (_broker, url, parents, companions):
            entered, release = asyncio.Event(), asyncio.Event()
            original = companions[0]._workspace
            delayed = [True]
            async def gated(session):
                if delayed[0]:
                    delayed[0] = False;entered.set();await release.wait()
                return await original(session)
            companions[0]._workspace = gated
            async with browser_page(playwright, url) as (page, errors, _sockets):
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    await page.evaluate('fixtureSelect(1)')
                    await page.wait_for_function('document.querySelector("#file-workspace-project").title.includes("project-B")')
                    release.set()
                    await page.wait_for_timeout(350)
                    assert 'project-B' in await page.locator('#file-workspace-project').get_attribute('title')
                    await page.locator('#file-workspace-project').click()
                    await page.wait_for_function('!document.querySelector("#file-workspace-save").disabled')
                    # Another writer advances only B while this dialog holds revision zero.
                    companions[1].workspaces.save('window1', parents[1].project, expected_revision=0)
                    await page.locator('#file-workspace-save').click()
                    await page.wait_for_function('document.querySelector("#file-workspace-state").textContent.includes("重新加载")')
                    assert await page.evaluate('fixtureEvents.length') == 0
                    await page.locator('#file-workspace-reload').click()
                    await page.wait_for_function('!document.querySelector("#file-workspace-save").disabled')
                    # A tab change invalidates the UI dialog, but never remaps its contents to A.
                    await page.evaluate('fixtureSelect(0)')
                    await page.wait_for_function('document.querySelector("#file-workspace-save").disabled')
                    await page.locator('#file-workspace-dialog form').evaluate('(form)=>form.requestSubmit()')
                    assert companions[0].workspaces.get('window1', parents[0].project)['revision'] == 0
                    assert companions[1].workspaces.get('window1', parents[1].project)['revision'] == 1
                    assert not errors and await page.evaluate('fixtureInputs') == []
                finally:
                    release.set()
    run(exercise, tmp_path)


@pytest.mark.parametrize('failure', ['wrong-token', 'missing-helper'])
def test_backend_unavailable_is_explicit_and_never_invents_paths(tmp_path, failure):
    async def exercise(playwright, folder):
        from test_file_companion import environment, eventually
        async with environment(folder) as (broker, url, _parents, companions):
            if failure == 'missing-helper':
                await companions[0].close()
                await eventually(lambda: ('owned', 'A') not in broker._hosts)
            async with browser_page(playwright, url, token='incorrect-fixture-token' if failure == 'wrong-token' else 'owned-viewer-token') as (page, errors, _sockets):
                await page.wait_for_function('''()=>{const value=document.querySelector('#file-workspace-project')?.textContent||'';
                  return value.includes('未安装')||value.includes('验证失败');}''')
                assert 'project-A' not in await page.locator('#file-workspace-project').inner_text()
                await page.locator('#file-workspace-project').click()
                await page.wait_for_function('document.querySelector("#file-workspace-state").textContent.includes("文件")')
                assert await page.locator('#file-workspace-save').is_disabled()
                assert await page.locator('#file-project').input_value() == ''
                assert not errors and await page.evaluate('fixtureInputs') == []
    run(exercise, tmp_path)


def test_commonjs_client_nonce_deadlines_and_old_socket_responses_are_isolated():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    code = r'''
    const assert=require('node:assert/strict');
    const timers=new Map();let timerID=0;
    global.setTimeout=(fn,ms)=>{assert.equal(ms,15000);const id=++timerID;
      timers.set(id,()=>{timers.delete(id);fn();});return id;};
    global.clearTimeout=id=>timers.delete(id);
    const sockets=[];
    global.WebSocket=class{
      constructor(url){this.url=url;this.readyState=0;this.sent=[];sockets.push(this);}
      send(raw){this.sent.push(JSON.parse(raw));}close(){this.readyState=3;}
      open(){this.readyState=1;this.onopen();}
      emit(value){this.onmessage({data:JSON.stringify(value)});}
    };
    const api=require(process.argv[1]),options={url:'ws://127.0.0.1:8821/file/ws',room:'A',token:'private-fixture-token'};
    const tick=async()=>{await Promise.resolve();await Promise.resolve();};
    async function ready(client){const pending=client.connect(),ws=sockets.at(-1);ws.open();
      const hello=ws.sent[0];assert.equal(hello.token,options.token);assert(!ws.url.includes(options.token));
      ws.emit({type:'ready',id:hello.id,role:'client',room:'A',agent_online:true});await pending;return ws;}
    (async()=>{
      const client=api.createClient(options),old=await ready(client);
      const first=client.request('workspace.get','session',{}).catch(e=>e.code);await tick();const oldID=old.sent.at(-1).id;
      old.readyState=3;old.onclose();assert.equal(await first,'disconnected');
      const fresh=await ready(client);let settled=false;
      const second=client.request('workspace.get','session',{}).then(v=>{settled=true;return v;});await tick();
      const id=fresh.sent.at(-1).id;assert.notEqual(id,oldID);
      old.emit({type:'response',id,ok:true,result:{bad:true}});fresh.emit({type:'response',id:oldID,ok:true,result:{bad:true}});
      await tick();assert.equal(settled,false);
      fresh.emit({type:'response',id,ok:true,result:{good:true}});assert.deepEqual(await second,{good:true});
      const late=client.request('workspace.get','session',{}).catch(e=>e.code);await tick();
      const lateID=fresh.sent.at(-1).id;timers.values().next().value();assert.equal(await late,'request_timeout');
      fresh.emit({type:'response',id:lateID,ok:true,result:{late:true}});assert.equal(client.pendingCount,0);
      const count=fresh.sent.length;await assert.rejects(client.request('upload.chunk','session',{x:'x'.repeat(262144)}),e=>e.code==='request_too_large');
      assert.equal(fresh.sent.length,count);
      fresh.bufferedAmount=262144*4+1;await assert.rejects(client.request('workspace.get','session',{}),e=>e.code==='service_busy');
      fresh.bufferedAmount=0;assert.equal(fresh.sent.length,count);
      const pending=client.request('workspace.get','session',{}).catch(e=>e.code);await tick();
      client.close();assert.equal(await pending,'closed');
      const other=api.createClient(options),connecting=other.connect().catch(e=>e.code);
      timers.values().next().value();assert.equal(await connecting,'ready_timeout');other.close();
      const huge=api.createClient(options),incoming=await ready(huge);
      const oversize=huge.request('workspace.get','session',{}).catch(e=>e.code);await tick();
      incoming.emit({type:'response',id:incoming.sent.at(-1).id,ok:true,result:{value:'x'.repeat(262144)}});
      assert.equal(await oversize,'invalid_response');assert.equal(huge.connected,false);huge.close();
      const wrong=api.createClient(options),mismatch=wrong.connect().catch(e=>e.code),last=sockets.at(-1);last.open();
      last.emit({type:'ready',id:'wrong_nonce',role:'client',room:'A',agent_online:true});
      assert.equal(await mismatch,'invalid_response');wrong.close();
      assert.equal(timers.size,0);
    })().then(()=>process.stdout.write('complete')).catch(error=>{console.error(error);process.exitCode=1;});
    '''
    result = subprocess.run([node, '-e', code, str(CLIENT)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'complete'


def test_native_metadata_gate_no_active_generation_change_and_disable_cleanup(tmp_path):
    async def exercise(playwright, folder):
        from test_file_companion import environment
        async with environment(folder) as (_broker, url, _parents, _companions):
            async with browser_page(playwright, url, enabled=False) as (page, errors, sockets):
                assert not sockets
                await page.evaluate('''url=>{
                  window.fixtureNativeCalls=0;window.fixtureFileEnabled=true;
                  window.pywebview={api:{file_exchange_config:async()=>{fixtureNativeCalls++;
                    return {enabled:fixtureFileEnabled,broker_url:url,local_room:'A'};}}};
                  dispatchEvent(new Event('pywebviewready'));
                }''', url)
                await page.wait_for_function('document.querySelector("#file-workspace-project")?.title.includes("project-A")')
                assert await page.evaluate('fixtureNativeCalls') == 1
                await page.evaluate('window.oldTarget=SuperMonoFileWorkspaces.captureTarget();fixtureSelect(-1)')
                await page.wait_for_function('document.querySelector("#file-workspace-project").disabled')
                # No selected UI doesn't invalidate a still-live captured transfer target.
                assert await page.evaluate('SuperMonoFileWorkspaces.isCurrent(oldTarget)')
                await page.evaluate('''()=>{
                  fixtureHosts[0].generation++;fixturePanes[0].generation++;
                  fixtureHosts[0].ws={readyState:1};fixtureSelect(0);
                }''')
                await page.wait_for_function('!document.querySelector("#file-workspace-project").disabled')
                assert not await page.evaluate('SuperMonoFileWorkspaces.isCurrent(oldTarget)')
                assert await page.evaluate('SuperMonoFileWorkspaces.isCurrent(SuperMonoFileWorkspaces.captureTarget())')
                await page.evaluate('fixtureFileEnabled=false;SuperMonoFileWorkspaces.refreshConfig()')
                await page.wait_for_function('!document.querySelector("#file-workspace-footer")')
                assert not await page.locator('#status').evaluate('(el)=>el.hidden')
                assert not errors and await page.evaluate('fixtureInputs') == []
    run(exercise, tmp_path)
