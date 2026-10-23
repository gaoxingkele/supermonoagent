"""Isolated Windows workspace agent profiles through actual browser asset routes."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from websockets.asyncio.server import serve

from supermonoagent import ui_updates
from supermonoagent.daemon import relay_uplink
from supermonoagent.frontend import Frontend
from test_client_integration import isolated_host
from test_desktop_ui_updates import wait_ui

EDGE = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
MODES = ['codex', 'claude-code', 'grok', 'kimi', 'pi-dev', 'cursor-cli']
FIXTURE = b'\x1b[38;2;240;180;210mNATIVE Error 123\x1b[0m\r\nError "quoted" sample.py 123 Thinking Working Plan Codex Claude Grok Kimi\r\n'
ARTIFACTS = Path(__file__).parents[1] / '.omx/artifacts/windows-agent-modes'
SCREEN = "p=>Array.from({length:p.term.buffer.active.length},(_,i)=>p.term.buffer.active.getLine(i).translateToString(true)).join('\\n')"


@asynccontextmanager
async def surface(tmp_path, frontend):
    async with isolated_host() as (daemon, _direct, relay):
        sessions=[]
        for title in ['Fixture terminal one', 'Fixture terminal two']:
            window=daemon.hub.open_window(title,owner=daemon.token)
            session=daemon.hub.open_cli(window.id,'cmd')
            sessions.append(session)
            session.pty.inbox.put(FIXTURE)
        async with serve(daemon.handle_socket,'127.0.0.1',0,process_request=daemon.serve_http) as backend:
            origin=f'http://127.0.0.1:{backend.sockets[0].getsockname()[1]}'
            if not frontend:
                yield daemon,sessions,origin,relay
                return
            app=Frontend(origin,tmp_path/'themes.json',update_root=tmp_path/'updates',update_feed='https://isolated.invalid/manifest.json')
            with patch.object(ui_updates,'fetch_manifest',side_effect=OSError('Isolated fixture: no network')):
                async with serve(app.handler,'127.0.0.1',0,process_request=app.serve_http) as server:
                    try:
                        yield daemon,sessions,f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}',relay
                    finally:
                        await app.close_updates()


def browser_api():
    module=pytest.importorskip('playwright.async_api')
    if not EDGE.exists():pytest.skip('Local Edge required')
    return module


@pytest.mark.parametrize('frontend',[False,True],ids=['source','revision-namespace'])
def test_windows_six_real_profiles_theme_highlight_ansi_and_persistence(tmp_path,frontend):
    asyncio.run(check_profiles(tmp_path,frontend,browser_api()))


async def check_profiles(tmp_path,frontend,playwright):
    async with surface(tmp_path,frontend) as (daemon,sessions,origin,_relay):
        resizes=[]
        for session in sessions:
            session.pty.resize=lambda cols,rows,sid=session.id:resizes.append((sid,cols,rows))
        async with playwright.async_playwright() as runtime:
            browser=await runtime.chromium.launch(executable_path=str(EDGE),headless=True)
            try:
                page=await browser.new_page(viewport={'width':1100,'height':760})
                responses=[]
                page.on('response',lambda response: responses.append((response.url,response.status)) if '/agent-modes/' in response.url else None)
                await page.goto(origin+'/workspace?direct=1&renderer=dom#token='+daemon.token)
                await wait_ui(page,'window.smaWorkspace?.connected&&smaWorkspace.panes.size===2')
                await page.evaluate('id=>smaWorkspace.openSession(id,false)',sessions[0].id)
                await wait_ui(page,"smaWorkspace.activePane.term.buffer.active.getLine(1)?.translateToString(true).includes('sample.py')")
                assert await page.locator('#agent-mode').input_value()=='auto'
                # Capture real decoration allocations while retaining the actual xterm implementation.
                await page.evaluate("""() => {window.profileDecos=[];const t=smaWorkspace.terminal,register=t.registerDecoration.bind(t);t.registerDecoration=options=>{profileDecos.push({x:options.x,width:options.width,color:options.foregroundColor,line:options.marker.line});return register(options);};window.originalScreen=("""+SCREEN+""")(smaWorkspace.activePane);} """)
                await wait_ui(page,"[...smaWorkspace.panes.values()].every(p=>!p.writing&&!p.resizeRequest&&!p.replayPending&&!p.frames.length&&!p.redrawAfterReplay&&!p.redrawRestore)")
                await asyncio.sleep(.1)
                await page.evaluate('originalScreen=('+SCREEN+')(smaWorkspace.activePane)')
                baseline=list(resizes)
                themes=[];decoration_sets=[]
                for mode in MODES:
                    await page.evaluate('profileDecos=[]')
                    await page.locator('#agent-mode').select_option(mode)
                    await wait_ui(page,'mode=>smaWorkspace.activePane.agentProfile===mode',arg=mode)
                    await wait_ui(page,'profileDecos.length>0&&document.querySelectorAll(".xterm-decoration").length>0')
                    value=await page.evaluate("""() => {const p=smaWorkspace.activePane;return {theme:p.term.options.theme,screen:("""+SCREEN+""")(p),ansi:p.term.buffer.active.getLine(0).getCell(0).getFgColor(),decos:profileDecos,domColors:[...document.querySelectorAll('.xterm-decoration')].map(d=>getComputedStyle(d).color),nativeDOM:[...p.el.querySelectorAll('.xterm-rows span')].filter(el=>el.textContent.includes('NATIVE')).map(el=>getComputedStyle(el).color),background:getComputedStyle(p.el.querySelector('.xterm-viewport')).backgroundColor};}""")
                    profile=json.loads((Path(__file__).parents[1]/'src/supermonoagent/static/agent-modes'/f'{mode}.json').read_text(encoding='utf-8'))
                    assert value['theme']['background']==profile['theme']['background']
                    assert value['screen']==await page.evaluate('originalScreen')
                    assert value['ansi']==0xF0B4D2
                    assert 'rgb(240, 180, 210)' in value['nativeDOM'],value
                    rgb=tuple(int(profile['theme']['background'][i:i+2],16) for i in (1,3,5))
                    assert value['background']=='rgb'+str(rgb),value['background']
                    assert not any(d['line']==0 for d in value['decos']),value['decos']
                    assert any(color!='rgb(0, 0, 0)' for color in value['domColors'])
                    themes.append(value['theme']['background'])
                    decoration_sets.append(tuple(sorted({d['color'] for d in value['decos']})))
                    ARTIFACTS.mkdir(parents=True,exist_ok=True)
                    await page.screenshot(path=str(ARTIFACTS/f'{"frontend" if frontend else "source"}-{mode}.png'))
                assert len(set(themes))==6
                assert len(set(decoration_sets))>=4,decoration_sets
                assert resizes==baseline
                assert all(not session.pty.written for session in sessions)
                # Another window starts automatic, and its explicit selection remains independent.
                await page.evaluate('id=>smaWorkspace.openSession(id,false)',sessions[1].id)
                await wait_ui(page,"document.querySelector('#agent-mode').value==='auto'")
                await page.locator('#agent-mode').select_option('kimi')
                await wait_ui(page,"smaWorkspace.activePane.agentProfile==='kimi'")
                await page.evaluate('id=>smaWorkspace.openSession(id,false)',sessions[0].id)
                await wait_ui(page,"document.querySelector('#agent-mode').value==='cursor-cli'")
                await page.reload()
                await wait_ui(page,'window.smaWorkspace?.connected&&smaWorkspace.panes.size===2')
                await page.evaluate('id=>smaWorkspace.openSession(id,false)',sessions[0].id)
                await wait_ui(page,"smaWorkspace.activePane.agentProfile==='cursor-cli'")
                await page.evaluate('id=>smaWorkspace.openSession(id,false)',sessions[1].id)
                await wait_ui(page,"smaWorkspace.activePane.agentProfile==='kimi'")
                for mode in MODES:
                    hits=[url for url,status in responses if '/agent-modes/'+mode+'.json' in url and status==200]
                    assert hits,(mode,responses)
                    if frontend:assert all('/__ui/bundled/agent-modes/' in url and '?bundle=' in url for url in hits),hits
            finally:
                await browser.close()


def test_windows_same_session_different_hosts_and_late_profile_does_not_reach_reopened_view(tmp_path):
    asyncio.run(check_isolation(tmp_path,browser_api()))


async def check_isolation(tmp_path,playwright):
    async with surface(tmp_path,False) as (local,sessions,origin,relay),isolated_host() as (remote,_direct,_relay):
        window=remote.hub.open_window('Remote fixture',owner=remote.token)
        other=remote.hub.open_cli(window.id,'cmd')
        old=other.id;other.id=sessions[0].id
        remote.hub.sessions[other.id]=remote.hub.sessions.pop(old)
        window.sessions[other.id]=window.sessions.pop(old);window.focused=other.id
        other.pty.inbox.put(FIXTURE)
        uplink=asyncio.create_task(relay_uplink(remote,relay,'profile-remote','fixture'))
        try:
            async with asyncio.timeout(5):
                while relay not in remote.relay_live:await asyncio.sleep(.01)
            async with playwright.async_playwright() as runtime:
                browser=await runtime.chromium.launch(executable_path=str(EDGE),headless=True)
                try:
                    page=await browser.new_page()
                    saved=[{'id':'remote:fixture','name':'Remote fixture','code':'profile-remote','relay':relay}]
                    await page.add_init_script("localStorage.setItem('sma.workspace.hosts',"+json.dumps(json.dumps(saved))+");sessionStorage.setItem('sma.workspace.host-token.remote:fixture','integration-owner');")
                    await page.goto(origin+'/workspace?direct=1&renderer=dom#token='+local.token)
                    await wait_ui(page,'window.smaWorkspace?.connected&&[...smaWorkspace.connections.values()].every(h=>h.connected)')
                    await page.evaluate('id=>smaWorkspace.openSession(id,false)',sessions[0].id)
                    await page.locator('#agent-mode').select_option('codex')
                    await wait_ui(page,"smaWorkspace.activePane.agentProfile==='codex'")
                    await page.evaluate("smaWorkspace.switchHost('remote:fixture')")
                    await wait_ui(page,"smaWorkspace.activePane?.host.id==='remote:fixture'")
                    assert await page.locator('#agent-mode').input_value()=='auto'
                    await page.locator('#agent-mode').select_option('grok')
                    await wait_ui(page,"smaWorkspace.activePane.agentProfile==='grok'")
                    await page.evaluate("smaWorkspace.switchHost('local')")
                    await page.evaluate('id=>smaWorkspace.openSession(id,false)',sessions[0].id)
                    assert await page.locator('#agent-mode').input_value()=='codex'
                    # Delay a real fetched profile after receiving bytes; close/reopen with a new choice.
                    await page.evaluate("""() => {const native=window.fetch;window.pendingProfiles=[];window.fetch=async(...args)=>{const response=await native(...args);if(String(args[0]).includes('/claude-code.json')){window.delayedProfile=true;await new Promise(resolve=>window.pendingProfiles.push(resolve));}return response;};window.closedProfilePane=smaWorkspace.activePane;}""")
                    await page.locator('#agent-mode').select_option('claude-code')
                    await wait_ui(page,'window.delayedProfile===true')
                    await page.evaluate('smaWorkspace.closeView(smaWorkspace.activeSession)')
                    await page.evaluate("id=>smaWorkspace.openHostSession('local',id,false)",sessions[0].id)
                    await page.locator('#agent-mode').select_option('kimi')
                    await wait_ui(page,"smaWorkspace.activePane.agentProfile==='kimi'")
                    assert await page.evaluate('pendingProfiles.length')==2
                    await page.evaluate('pendingProfiles.forEach(resolve=>resolve())')
                    await asyncio.sleep(.15)
                    assert await page.evaluate("smaWorkspace.activePane!==closedProfilePane&&smaWorkspace.activePane.agentProfile==='kimi'&&closedProfilePane.agentProfile==='codex'")
                    await page.evaluate("smaWorkspace.switchHost('remote:fixture')")
                    details=await page.evaluate("({active:smaWorkspace.activeSession,field:document.querySelector('#agent-mode').value,panes:[...smaWorkspace.panes.values()].map(p=>({sid:p.sid,host:p.host.id,mode:p.agentMode,profile:p.agentProfile})),stored:Object.fromEntries(Object.entries(localStorage).filter(([k])=>k.includes('agent-mode')))})")
                    ARTIFACTS.mkdir(parents=True,exist_ok=True)
                    (ARTIFACTS/'isolation-state.json').write_text(json.dumps(details,indent=2),encoding='utf-8')
                    assert details['field']=='grok',details
                    assert all(not session.pty.written for session in [*sessions,other])
                finally:
                    await browser.close()
        finally:
            uplink.cancel();await asyncio.gather(uplink,return_exceptions=True)
