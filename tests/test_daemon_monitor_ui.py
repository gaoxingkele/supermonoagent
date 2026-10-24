"""One workspace dialog inspects daemon metadata/logs without touching PTYs."""
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from supermonoagent.protocol import pack_json,unpack_json,unpack
from test_workspace_profile_rooms import fixture,run,add_profile,wait_room,server_barrier,eventually


def test_monitor_polls_only_selected_daemon_and_preserves_terminal_view(tmp_path):
    run(lambda playwright: check_monitor(playwright,tmp_path))


async def prepare(browser,url,endpoint,daemons):
    page=await browser.new_page(viewport={'width':1100,'height':850})
    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    await page.goto(url);await page.wait_for_function('window.smaWorkspace?.connected&&smaWorkspace.activePane?.ready')
    await add_profile(page,endpoint,daemons[1].token)
    await wait_room(page,1);await page.locator('#relay-profiles-close').click()
    await page.wait_for_function('[...smaWorkspace.panes.values()].every(p=>!p.resizeRequest&&!p.writing&&!p.frames.length)')
    remote=await page.evaluate("[...smaWorkspace.connections.values()].find(h=>h.config.code==='profile-room-1').id")
    await server_barrier(page,'local');await server_barrier(page,remote)
    await page.evaluate("()=>{window.monitorOriginalPane=smaWorkspace.activePane;window.monitorOriginalSockets=[...smaWorkspace.connections.values()].map(h=>h.ws);window.monitorPopupCount=0;window.open=()=>{monitorPopupCount++;throw new Error('Unexpected separate daemon window');};}")
    return page,remote,errors


def install_observers(daemons,requests,gates=None):
    for index,daemon in enumerate(daemons):
        original=daemon._op
        async def observe(client,op,payload,*,index=index,original=original):
            if op==20:
                requests[index].append(unpack_json(payload))
                if gates and gates[index] is not None:await gates[index].wait()
            return await original(client,op,payload)
        daemon._op=observe


def write_log(daemon,root,label):
    daemon.log_state_dir=root/label;(daemon.log_state_dir/'logs').mkdir(parents=True)
    text='\n'.join(f'{label} line {n:03d}' for n in range(80))+'\n<img src=x onerror=window.monitorInjected=true>\n'
    path=daemon.log_state_dir/'logs/daemon.log';path.write_text(text,encoding='utf8');return path


async def open_monitor(page):
    await page.locator('#app-menu-toggle').click()
    await page.locator('#menu-daemon-monitor').click()
    await page.locator('#daemon-monitor-dialog').wait_for(state='visible')


async def check_monitor(playwright,tmp_path):
    async with fixture(playwright) as (browser,url,endpoint,_trap,_messages,daemons,ops,_windows,sessions):
        paths=[write_log(daemon,tmp_path,'DAEMON-'+str(i)) for i,daemon in enumerate(daemons)]
        requests=[[],[]];remote_gate=asyncio.Event();install_observers(daemons,requests,[None,remote_gate])
        page,remote,errors=await prepare(browser,url,endpoint,daemons)
        for items in ops:items.clear()
        await open_monitor(page)
        await page.wait_for_function("document.querySelector('#daemon-monitor-log').textContent.includes('DAEMON-0')")
        assert await page.locator('#daemon-monitor-list [role=listitem]').count()==2
        row=page.locator('#daemon-monitor-list [data-host="local"]')
        assert '2 个窗口 / 2 个终端' in await row.inner_text()
        assert 'PID ' in await row.inner_text() and '已运行 ' in await row.inner_text()
        assert await page.locator('#daemon-monitor-log img').count()==0
        assert not await page.evaluate('window.monitorInjected===true')
        # A user reading older lines must stay there during the next poll.
        await page.locator('#daemon-monitor-log').evaluate('el=>{el.style.height="80px";el.style.maxHeight="80px";el.scrollTop=0;}')
        paths[0].write_text(paths[0].read_text()+'DAEMON-0 newer line\n',encoding='utf8')
        await page.wait_for_function("document.querySelector('#daemon-monitor-log').textContent.includes('newer line')",timeout=5000)
        assert await page.locator('#daemon-monitor-log').evaluate('el=>el.scrollTop')==0
        assert len(requests[0])>=2 and not requests[1]
        assert all(item['lines']==80 for item in requests[0])
        old_request=requests[0][-1]['request']
        await page.locator('#daemon-monitor-select').select_option(remote)
        await eventually(lambda:bool(requests[1]))
        remote_request=requests[1][-1]['request']
        # Correct request on the wrong host, and old request from prior selection.
        for request in (remote_request,old_request):
            await page.evaluate("args=>smaWorkspace.connections.get('local').ws.onmessage({data:TerminalClient.pack(20,new TextEncoder().encode(JSON.stringify({request:args,text:'WRONG-HOST-LOG',pid:123,uptime_seconds:5}))).buffer})",request)
        assert 'WRONG-HOST-LOG' not in await page.locator('#daemon-monitor-log').inner_text()
        remote_gate.set()
        await page.wait_for_function("document.querySelector('#daemon-monitor-log').textContent.includes('DAEMON-1')")
        local_count=len(requests[0]);await page.wait_for_timeout(2200)
        assert len(requests[0])==local_count and len(requests[1])>=2
        assert await page.evaluate('smaWorkspace.activePane===monitorOriginalPane&&monitorOriginalSockets.every((ws,i)=>[...smaWorkspace.connections.values()][i].ws===ws)&&monitorPopupCount===0')
        assert not any(ops),ops
        assert all(not session.pty.written for host in sessions for session in host)
        artifact=Path('.omx/artifacts/daemon-monitor-ui');artifact.mkdir(parents=True,exist_ok=True)
        await page.screenshot(path=str(artifact/'desktop.png'))
        await page.set_viewport_size({'width':390,'height':844})
        assert await page.locator('#daemon-monitor-dialog').evaluate('el=>el.getBoundingClientRect().right<=innerWidth&&el.scrollWidth<=el.clientWidth')
        await page.screenshot(path=str(artifact/'narrow.png'))
        await page.locator('#daemon-monitor-close').click()
        counts=list(map(len,requests));await page.wait_for_timeout(2300);assert list(map(len,requests))==counts
        await open_monitor(page)
        await page.wait_for_function("document.querySelector('#daemon-monitor-log').textContent.includes('DAEMON-0')")
        await page.evaluate("smaWorkspace.connections.get('local').ws.close(4001,'test-owned disconnect')")
        await page.wait_for_function("!smaWorkspace.connections.get('local').connected")
        assert await page.locator('#daemon-monitor-log').inner_text()==''
        assert not errors,errors


def test_monitor_keeps_old_backend_status_without_unknown_log_opcode(tmp_path):
    run(lambda playwright:check_old_backend(playwright,tmp_path))


async def check_old_backend(playwright,tmp_path):
    async with fixture(playwright) as (browser,url,endpoint,_trap,_messages,daemons,_ops,*_):
        original=daemons[1]._state
        def old_state(*args,**kwargs):
            op,payload=unpack(original(*args,**kwargs));body=unpack_json(payload)
            body['capabilities']={**body['capabilities'],'daemon_logs':False};return pack_json(op,body)
        daemons[1]._state=old_state
        write_log(daemons[0],tmp_path,'OLD-COMPAT-LOCAL')
        requests=[[],[]];install_observers(daemons,requests)
        page,remote,errors=await prepare(browser,url,endpoint,daemons)
        await open_monitor(page)
        await page.locator('#daemon-monitor-select').select_option(remote)
        await page.wait_for_function("document.querySelector('#daemon-monitor-status').textContent.includes('请升级')")
        await page.wait_for_timeout(2200)
        assert not requests[1]
        assert await page.locator('#daemon-monitor-list [role=listitem]').count()==2
        assert await page.evaluate('smaWorkspace.activePane===monitorOriginalPane&&monitorPopupCount===0')
        assert not errors


def test_monitor_displays_permission_error_and_rejects_closed_reply(tmp_path):
    run(lambda playwright:check_error(playwright,tmp_path))


async def check_error(playwright,tmp_path):
    async with fixture(playwright) as (browser,url,endpoint,_trap,_messages,daemons,*_):
        requests=[];original=daemons[0]._op
        async def denied(client,op,payload):
            if op==20:
                body=unpack_json(payload);requests.append(body)
                return [pack_json(20,{'request':body['request'],'error_code':'owner_required','error':'OWNER-ONLY: fixture access denied'})],None
            return await original(client,op,payload)
        daemons[0]._op=denied
        page=await browser.new_page();await page.goto(url)
        await page.wait_for_function('window.smaWorkspace?.connected')
        await open_monitor(page)
        await page.wait_for_function("document.querySelector('#daemon-monitor-status').textContent.includes('OWNER-ONLY')")
        await page.wait_for_timeout(2200);assert len(requests)==1
        await page.locator('#daemon-monitor-close').click()
        await page.evaluate("request=>smaWorkspace.connections.get('local').ws.onmessage({data:TerminalClient.pack(20,new TextEncoder().encode(JSON.stringify({request,text:'LATE-CLOSED-LOG'}))).buffer})",requests[0]['request'])
        assert 'LATE-CLOSED-LOG' not in await page.locator('#daemon-monitor-log').inner_text()


def test_monitor_retries_temporary_read_failure_on_next_poll(tmp_path):
    run(lambda playwright:check_transient(playwright,tmp_path))


async def check_transient(playwright,tmp_path):
    from supermonoagent.daemon_logs import read_log_tail
    async with fixture(playwright) as (browser,url,_endpoint,_trap,_messages,daemons,*_):
        write_log(daemons[0],tmp_path,'RECOVERED-LOG')
        calls=[]
        def temporary_error(*args,**kwargs):
            calls.append(True)
            if len(calls)==1:raise OSError('Test-owned temporary sharing failure')
            return read_log_tail(*args,**kwargs)
        page=await browser.new_page();await page.goto(url)
        await page.wait_for_function('window.smaWorkspace?.connected')
        with patch('supermonoagent.daemon_logs.read_log_tail',side_effect=temporary_error):
            await open_monitor(page)
            await page.wait_for_function("document.querySelector('#daemon-monitor-status').textContent.includes('暂不可用')")
            assert await page.locator('#daemon-monitor-log').inner_text()==''
            await page.wait_for_function("document.querySelector('#daemon-monitor-log').textContent.includes('RECOVERED-LOG')",timeout=5000)
            assert len(calls)>=2
