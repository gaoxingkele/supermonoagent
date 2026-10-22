"""Unified relay login/discovery through the real desktop UI and isolated PTYs."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import tempfile
import time

import pytest
from websockets.asyncio.server import serve

from supermonoagent.daemon import relay_uplink
from supermonoagent.protocol import unpack, unpack_json
from supermonoagent.relay import Relay
from test_client_integration import isolated_host

EDGE = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
PASSWORD = 'fixture-account-password'


@asynccontextmanager
async def account_fixture():
    async with isolated_host() as (local, _, _), isolated_host() as (remote, _, _):
        with tempfile.TemporaryDirectory(prefix='sma-account-ui-') as directory:
            remote.token = remote.hub.default_owner = remote.hub.groups['default'].owner = 'remote-owner-only'
            windows = [local.hub.open_window('Local account terminal', owner=local.token), remote.hub.open_window('Remote account terminal', owner=remote.token)]
            sessions = [local.hub.open_cli(windows[0].id, 'cmd'), remote.hub.open_cli(windows[1].id, 'cmd')]
            old = sessions[1].id
            sessions[1].id = sessions[0].id
            remote.hub.sessions[sessions[0].id] = remote.hub.sessions.pop(old)
            windows[1].sessions[sessions[0].id] = windows[1].sessions.pop(old)
            windows[1].focused = sessions[0].id
            relay = Relay(identity_file=Path(directory)/'accounts.sqlite3', development=True)
            accounts = [relay.identity.create_account(email, PASSWORD) for email in ['one@example.test', 'two@example.test']]
            relay.identity.grant(accounts[0], 'room-local')
            relay.identity.grant(accounts[0], 'room-remote')
            relay.identity.grant(accounts[1], 'room-local')
            async with serve(local.handle_socket, '127.0.0.1', 0, process_request=local.serve_http) as page_server, serve(relay.handler, '127.0.0.1', 0, process_request=relay.serve_page) as relay_server:
                page_url=f'http://127.0.0.1:{page_server.sockets[0].getsockname()[1]}'
                relay_url=f'ws://127.0.0.1:{relay_server.sockets[0].getsockname()[1]}/ws'
                relay.public_url=relay_url
                relay.allowed_origins=(page_url,)
                local.relay_bindings.bind(relay_url, 'room-local', accounts[0], local.token)
                remote.relay_bindings.bind(relay_url, 'room-remote', accounts[0], remote.token)
                jobs=[asyncio.create_task(relay_uplink(local, relay_url, 'room-local', 'account-local')), asyncio.create_task(relay_uplink(remote, relay_url, 'room-remote', 'account-remote'))]
                try:
                    async with asyncio.timeout(5):
                        while not all(code in relay.rooms and relay.rooms[code].host for code in ['room-local','room-remote']):
                            await asyncio.sleep(.01)
                    yield local, remote, relay, relay_url, page_url, accounts, sessions
                finally:
                    for job in jobs:
                        job.cancel()
                    await asyncio.gather(*jobs, return_exceptions=True)
                    if relay._identity_monitor:
                        relay._identity_monitor.cancel()
                        await asyncio.gather(relay._identity_monitor, return_exceptions=True)


def run_browser(check):
    playwright=pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Local Edge required')
    asyncio.run(check(playwright))


def test_login_discover_autofill_directed_io_revoke_logout_and_account_isolation():
    run_browser(_check_account_flow)


async def login(page, relay_url, email='one@example.test', password=PASSWORD):
    await page.locator('#account-relay-url').fill(relay_url)
    await page.locator('#relay-account-email').fill(email)
    await page.locator('#relay-account-password').fill(password)
    await page.locator('#relay-account-login').click()


async def choose_account_host(page, relay_url, code):
    await page.wait_for_function("value=>Array.from(document.querySelector('#workspace-known').options).some(o=>o.value===value)", arg=json.dumps(['account',relay_url,code], separators=(',',':')))
    await page.locator('#workspace-known').select_option(json.dumps(['account',relay_url,code], separators=(',',':')))


async def capture_dialog_layout(page, dialog, label, controls):
    destination=Path(__file__).parents[1]/'.omx'/'artifacts'/'relay-accounts'
    destination.mkdir(parents=True,exist_ok=True)
    records=[]
    for width,height in [(1100,760),(390,844)]:
        await page.set_viewport_size({'width':width,'height':height})
        await page.locator(dialog).evaluate('dialog=>dialog.scrollTop=0')
        geometry=await page.locator(dialog).evaluate("""dialog=>{
          const r=dialog.getBoundingClientRect();return {width:innerWidth,height:innerHeight,left:r.left,right:r.right,top:r.top,bottom:r.bottom,clientWidth:dialog.clientWidth,scrollWidth:dialog.scrollWidth,clientHeight:dialog.clientHeight,scrollHeight:dialog.scrollHeight,overflowY:getComputedStyle(dialog).overflowY,pageWidth:document.documentElement.scrollWidth};
        }""")
        assert geometry['left']>=0 and geometry['right']<=width+1,geometry
        assert geometry['top']>=0 and geometry['bottom']<=height+1,geometry
        assert geometry['scrollWidth']<=geometry['clientWidth']+1,geometry
        assert geometry['pageWidth']<=width+1,geometry
        assert geometry['overflowY']=='auto',geometry
        await page.screenshot(path=str(destination/f'{label}-{width}x{height}-top.png'))
        for selector in controls:
            control=page.locator(selector)
            await control.scroll_into_view_if_needed()
            box=await control.bounding_box()
            assert box and box['x']>=geometry['left'] and box['x']+box['width']<=geometry['right']+1,(selector,box,geometry)
            assert box['y']>=geometry['top'] and box['y']+box['height']<=geometry['bottom']+1,(selector,box,geometry)
        await page.screenshot(path=str(destination/f'{label}-{width}x{height}-bottom.png'))
        records.append(geometry)
    (destination/f'{label}-geometry.json').write_text(json.dumps(records,indent=2),encoding='utf-8')
    await page.set_viewport_size({'width':1100,'height':760})


async def _check_account_flow(playwright):
    async with account_fixture() as (local, remote, relay, relay_url, page_url, accounts, sessions):
        async with playwright.async_playwright() as runtime:
            browser=await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
            try:
                page=await browser.new_page()
                errors, frames, socket_urls=[],[],[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                def socket_opened(socket):
                    socket_urls.append(socket.url)
                    socket.on('framesent',lambda payload:frames.append((socket.url,payload)))
                page.on('websocket',socket_opened)
                await page.goto(page_url+'/workspace?direct=1&renderer=dom#token='+local.token)
                await page.wait_for_function('smaWorkspace?.connected && smaWorkspace.terminal')
                local_generation=await page.evaluate("smaWorkspace.connections.get('local').generation")
                await page.locator('#new-project').click()
                await page.locator('#workspace-relay').fill(relay_url)
                await page.locator('#workspace-account-login').click()
                await login(page, relay_url)
                await page.wait_for_function("document.querySelector('#workspace-dialog').open && smaWorkspace.relayAccounts.size===1")
                await choose_account_host(page, relay_url, 'room-remote')
                assert await page.locator('#workspace-relay').input_value()==relay_url
                assert await page.locator('#workspace-code').input_value()=='room-remote'
                assert await page.locator('#workspace-auth').input_value()=='account'
                assert await page.locator('#workspace-token').get_attribute('type')=='password'
                assert await page.locator('#workspace-token').evaluate('input=>input.readOnly')
                assert await page.locator('#workspace-token').input_value() not in [local.token,remote.token]
                assert await page.locator('#workspace-remember').is_disabled()
                await capture_dialog_layout(page,'#workspace-dialog','authorized-computer',['#workspace-known','#workspace-relay','#workspace-token','#workspace-form button[type=submit]'])
                await page.locator('#workspace-form').evaluate('(form)=>form.requestSubmit()')
                await page.wait_for_function("smaWorkspace.activeHost.config.authMode==='account'&&smaWorkspace.activeHost.config.code==='room-remote'&&smaWorkspace.activeHost.connected&&smaWorkspace.terminal")
                remote_id=await page.evaluate('smaWorkspace.activeHost.id')
                await page.evaluate("smaWorkspace.terminal.paste('account-remote-input')")
                async with asyncio.timeout(4):
                    while sessions[1].pty.written!=b'account-remote-input':
                        await asyncio.sleep(.01)
                assert sessions[0].pty.written==b''
                session=await page.evaluate('[...smaWorkspace.relayAccounts.values()][0].session')
                storage=await page.evaluate('JSON.stringify(localStorage)')
                assert session not in storage and PASSWORD not in storage and remote.token not in storage
                saved=await page.evaluate("JSON.parse(localStorage.getItem('sma.workspace.hosts'))")
                assert saved[0]['authMode']=='account'
                assert set(saved[0])=={'id','name','relay','code','authMode'}
                delegated_hello=[]
                for url,payload in frames:
                    if url==relay_url and isinstance(payload,bytes):
                        op,body=unpack(payload)
                        if op==1:
                            delegated_hello.append(unpack_json(body))
                assert delegated_hello and all(not any(field in hello for field in ['token','session','password','account']) for hello in delegated_hello)
                assert all('token=' not in url and 'session=' not in url for url in socket_urls)
                await page.locator('#new-project').click()
                await choose_account_host(page,relay_url,'room-local')
                await page.locator('#workspace-form').evaluate('(form)=>form.requestSubmit()')
                await page.wait_for_function("smaWorkspace.activeHost.config.code==='room-local'&&smaWorkspace.activeHost.connected")
                local_account_id=await page.evaluate('smaWorkspace.activeHost.id')
                await page.reload()
                await page.wait_for_function("smaWorkspace.connections.size===3&&[...smaWorkspace.connections.values()].every(h=>h.connected)&&[...smaWorkspace.relayAccounts.values()][0]?.phase==='connected'")
                assert await page.evaluate('[...smaWorkspace.relayAccounts.values()][0].session')==session
                await page.evaluate('()=>{window.staleAccountMessage=[...smaWorkspace.relayAccounts.values()][0].ws.onmessage;}')
                local_generation=await page.evaluate("smaWorkspace.connections.get('local').generation")
                relay.identity.revoke(accounts[0],'room-remote')
                await page.wait_for_function("id=>!smaWorkspace.connections.get(id).connected&&smaWorkspace.connections.get(id).catalog.length===0",arg=remote_id)
                assert await page.evaluate("id=>smaWorkspace.connections.get(id).connected",local_account_id)
                assert await page.evaluate("smaWorkspace.connections.get('local').generation")==local_generation
                await page.evaluate("smaWorkspace.terminal.paste('account-local-input')")
                async with asyncio.timeout(4):
                    while sessions[0].pty.written!=b'account-local-input':
                        await asyncio.sleep(.01)
                await page.locator('#connect-button').click()
                await capture_dialog_layout(page,'#connection-dialog','relay-account-panel',['#relay-account-email','#relay-account-password','#relay-account-login','#connection-form button[type=submit]'])
                await page.locator('#relay-account-logout').click()
                await page.wait_for_function("smaWorkspace.relayAccounts.size===0 && [...smaWorkspace.connections.values()].filter(h=>h.config.authMode==='account').every(h=>!h.connected&&h.catalog.length===0)")
                assert await page.evaluate("smaWorkspace.connections.get('local').connected")
                async with asyncio.timeout(3):
                    while relay.identity.authenticate(session):
                        await asyncio.sleep(.01)
                # Bad credentials cannot establish any account state.
                await login(page,relay_url,password=PASSWORD+'-wrong')
                await page.wait_for_function("document.querySelector('#relay-account-status').textContent.includes('Invalid credentials')")
                assert await page.evaluate('smaWorkspace.relayAccounts.size')==0
                # The second account only sees its own grant; no host binding is inferred.
                await login(page,relay_url,'two@example.test')
                await page.wait_for_function("[...smaWorkspace.relayAccounts.values()].some(a=>a.phase==='connected'&&a.account.email==='two@example.test')")
                assert await page.evaluate("[...smaWorkspace.relayAccounts.values()][0].hosts.map(h=>h.code)")==['room-local']
                await page.evaluate("url=>staleAccountMessage({data:JSON.stringify({op:'directory',revision:99999,capabilities:{relay_accounts:true},hosts:[{endpoint:url,code:'room-remote',online:true,machine:'STALE-ID'}]})})",relay_url)
                assert await page.evaluate("[...smaWorkspace.relayAccounts.values()][0].hosts.map(h=>h.code)")==['room-local']
                await page.wait_for_function("id=>smaWorkspace.connections.get(id).phase==='auth-failed'",arg=local_account_id)
                assert await page.evaluate("smaWorkspace.connections.get('local').connected")
                assert all(session.id in daemon.hub.sessions for daemon,session in [(local,sessions[0]),(remote,sessions[1])])
                assert not errors,errors
            finally:
                await browser.close()


def test_public_plaintext_rejects_login_and_does_not_transmit_password_or_session():
    run_browser(_check_plaintext)


async def _check_plaintext(playwright):
    async with isolated_host() as (daemon,_,_):
        async with serve(daemon.handle_socket,'127.0.0.1',0,process_request=daemon.serve_http) as server:
            async with playwright.async_playwright() as runtime:
                browser=await runtime.chromium.launch(executable_path=str(EDGE),headless=True)
                try:
                    page=await browser.new_page();urls=[]
                    page.on('websocket',lambda socket:urls.append(socket.url))
                    await page.goto(f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/workspace?direct=1#token={daemon.token}')
                    await page.wait_for_function('smaWorkspace?.connected')
                    await page.locator('#connect-button').click()
                    generation=await page.evaluate('smaWorkspace.activeHost.generation')
                    await page.locator('#relay-account-email').fill('unfinished-email')
                    await page.locator('#connection-form').evaluate('(form)=>form.requestSubmit()')
                    await page.wait_for_function('generation=>smaWorkspace.connected&&smaWorkspace.activeHost.generation>generation',arg=generation)
                    before=list(urls)
                    await page.locator('#connect-button').click()
                    await login(page,'ws://117.50.48.22:8817/ws')
                    assert 'HTTPS/WSS' in await page.locator('#relay-account-status').inner_text()
                    assert await page.locator('#relay-account-password').input_value()==''
                    assert urls==before
                    assert await page.evaluate('smaWorkspace.relayAccounts.size')==0
                finally:
                    await browser.close()


def test_expiry_stops_only_delegated_connections_and_prevents_retries():
    run_browser(_check_expiry)


async def _check_expiry(playwright):
    async with account_fixture() as (local, remote, relay, relay_url, page_url, _accounts, sessions):
        relay.identity.session_seconds=3
        async with playwright.async_playwright() as runtime:
            browser=await runtime.chromium.launch(executable_path=str(EDGE),headless=True)
            try:
                page=await browser.new_page()
                await page.goto(page_url+'/workspace?direct=1#token='+local.token)
                await page.wait_for_function('smaWorkspace?.connected')
                await page.locator('#new-project').click()
                await page.locator('#workspace-relay').fill(relay_url)
                await page.locator('#workspace-account-login').click()
                await login(page,relay_url)
                await page.wait_for_function("document.querySelector('#workspace-dialog').open")
                await choose_account_host(page,relay_url,'room-remote')
                # Editing either the room or endpoint immediately removes inherited auth.
                await page.locator('#workspace-code').fill('room-changed')
                assert await page.locator('#workspace-auth').input_value()=='manual'
                assert await page.locator('#workspace-token').input_value()==''
                await choose_account_host(page,relay_url,'room-remote')
                await page.locator('#workspace-relay').fill(relay_url+'/other')
                assert await page.locator('#workspace-auth').input_value()=='manual'
                assert await page.locator('#workspace-token').input_value()==''
                await choose_account_host(page,relay_url,'room-remote')
                await page.locator('#workspace-form').evaluate('(form)=>form.requestSubmit()')
                await page.wait_for_function("smaWorkspace.activeHost.config.authMode==='account'&&smaWorkspace.activeHost.connected")
                host_id=await page.evaluate('smaWorkspace.activeHost.id')
                await page.wait_for_function("id=>smaWorkspace.relayAccounts.size===0&&!smaWorkspace.connections.get(id).connected&&smaWorkspace.connections.get(id).catalog.length===0",arg=host_id,timeout=6000)
                opened=await page.evaluate('smaWorkspace.metrics.socketsOpened')
                await page.wait_for_timeout(650)
                assert await page.evaluate('smaWorkspace.metrics.socketsOpened')==opened
                assert await page.evaluate("smaWorkspace.connections.get('local').connected")
                assert await page.evaluate("JSON.parse(sessionStorage.getItem('sma.workspace.relay-accounts')).length")==0
                assert sessions[1].id in remote.hub.sessions
            finally:
                await browser.close()


def test_stale_login_and_plaintext_restored_session_cannot_cross_targets():
    run_browser(_check_stale_login)


async def _check_stale_login(playwright):
    started=asyncio.Event();release=asyncio.Event();other_frames=[]

    async def delayed(socket):
        await socket.recv();started.set();await release.wait()
        try:
            await socket.send(json.dumps({'op':'relay_auth','session':'STALE-SESSION','expires':time.time()+60,'account':{'id':'old-account','email':'one@example.test'}}))
        except Exception:
            pass

    async def other(socket):
        async for frame in socket:
            other_frames.append(frame)

    async with isolated_host() as (daemon,_,_), serve(delayed,'127.0.0.1',0) as first, serve(other,'127.0.0.1',0) as second, serve(daemon.handle_socket,'127.0.0.1',0,process_request=daemon.serve_http) as page_server:
        first_url=f'ws://127.0.0.1:{first.sockets[0].getsockname()[1]}/ws'
        second_url=f'ws://127.0.0.1:{second.sockets[0].getsockname()[1]}/ws'
        async with playwright.async_playwright() as runtime:
            browser=await runtime.chromium.launch(executable_path=str(EDGE),headless=True)
            try:
                page=await browser.new_page();urls=[]
                page.on('websocket',lambda socket:urls.append(socket.url))
                await page.add_init_script("""(() => {
                  const Original=WebSocket;window.testSockets=[];
                  window.WebSocket=class extends Original {constructor(...args){super(...args);testSockets.push(this);}};
                  sessionStorage.setItem('sma.workspace.relay-accounts',JSON.stringify([{url:'ws://117.50.48.22:8817/ws',session:'PUBLIC-SESSION-NEVER-SEND',expires:Date.now()/1000+60,account:{id:'public',email:'one@example.test'}}]));
                  localStorage.setItem('sma.workspace.hosts',JSON.stringify([{id:'public-account-host',name:'Public',relay:'ws://117.50.48.22:8817/ws',code:'room',authMode:'account'}]));
                })()""")
                await page.goto(f'http://127.0.0.1:{page_server.sockets[0].getsockname()[1]}/workspace?direct=1#token={daemon.token}')
                await page.wait_for_function('smaWorkspace?.connected')
                assert all('117.50.48.22' not in url for url in urls)
                assert await page.evaluate('smaWorkspace.relayAccounts.size')==0
                await page.locator('#connect-button').click()
                await login(page,first_url)
                await asyncio.wait_for(started.wait(),3)
                await page.evaluate("url=>{window.staleLoginMessage=testSockets.find(socket=>socket.url===url).onmessage;}",first_url)
                await page.locator('#account-relay-url').fill(second_url)
                await page.evaluate("()=>staleLoginMessage({data:JSON.stringify({op:'relay_auth',session:'STALE-SESSION',expires:Date.now()/1000+60,account:{id:'old-account',email:'one@example.test'}})})")
                release.set()
                assert await page.evaluate('smaWorkspace.relayAccounts.size')==0
                assert 'STALE-SESSION' not in await page.evaluate("sessionStorage.getItem('sma.workspace.relay-accounts')||''")
                assert not other_frames
                assert second_url not in urls
                assert await page.evaluate("smaWorkspace.connections.get('local').connected")
            finally:
                release.set()
                await browser.close()


def test_direct_account_switch_clears_previous_views_before_delayed_authorization():
    run_browser(_check_direct_account_switch)


async def _check_direct_account_switch(playwright):
    async with account_fixture() as (local, remote, _relay, relay_url, page_url, accounts, sessions):
        async with playwright.async_playwright() as runtime:
            browser=await runtime.chromium.launch(executable_path=str(EDGE),headless=True)
            try:
                page=await browser.new_page()
                await page.add_init_script("""(() => {
                  const Original=WebSocket;
                  window.holdAccountResponses=false;window.heldAccountResponses=[];
                  window.WebSocket=class extends Original {
                    constructor(...args){
                      super(...args);
                      this.addEventListener('message',event=>{
                        if(!window.holdAccountResponses||this.url!==window.accountGateURL)return;
                        if(typeof event.data==='string'){
                          try{if(JSON.parse(event.data).op==='relay_auth')return;}catch{}
                        }
                        event.stopImmediatePropagation();heldAccountResponses.push(event.data);
                      },true);
                    }
                  };
                })()""")
                await page.goto(page_url+'/workspace?direct=1&renderer=dom#token='+local.token)
                await page.wait_for_function('smaWorkspace?.connected&&smaWorkspace.terminal')
                local_generation=await page.evaluate("smaWorkspace.connections.get('local').generation")
                await page.locator('#new-project').click()
                await page.locator('#workspace-relay').fill(relay_url)
                await page.locator('#workspace-account-login').click()
                await login(page,relay_url)
                await page.wait_for_function("document.querySelector('#workspace-dialog').open")
                await choose_account_host(page,relay_url,'room-remote')
                await page.locator('#workspace-form').evaluate('(form)=>form.requestSubmit()')
                await page.wait_for_function("smaWorkspace.activeHost.config.code==='room-remote'&&smaWorkspace.activeHost.connected&&smaWorkspace.terminal")
                host_id=await page.evaluate('smaWorkspace.activeHost.id')
                sessions[1].pty.inbox.put(b'ACCOUNT-A-PRIVATE-CONTENT\r\n')
                await page.wait_for_function("(()=>{const t=smaWorkspace.terminal;return Array.from({length:t.buffer.active.length},(_,i)=>t.buffer.active.getLine(i)?.translateToString(true)||'').join('\\n').includes('ACCOUNT-A-PRIVATE-CONTENT');})()")
                await page.evaluate('()=>{window.originalAccountPane=smaWorkspace.activePane;}')
                await page.locator('#connect-button').click()
                # Refreshing the same principal may retain its own terminal instance.
                first_session=await page.evaluate('[...smaWorkspace.relayAccounts.values()][0].session')
                await login(page,relay_url)
                await page.wait_for_function("session=>[...smaWorkspace.relayAccounts.values()][0]?.session!==session&&smaWorkspace.activeHost.connected",arg=first_session)
                assert await page.evaluate('smaWorkspace.activePane===originalAccountPane')
                await page.evaluate('url=>{window.accountGateURL=url;window.holdAccountResponses=true;}',relay_url)
                # No logout: B's successful login must itself remove all of A's view state.
                await login(page,relay_url,'two@example.test')
                await page.wait_for_function("id=>[...smaWorkspace.relayAccounts.values()][0]?.account.id===id",arg=accounts[1])
                state=await page.evaluate("""id=>{
                  const h=smaWorkspace.connections.get(id);
                  return {catalog:h.catalog,groups:h.groups,controls:h.controls,identity:h.identity,
                    panes:[...smaWorkspace.panes.values()].filter(p=>p.host===h).length,
                    credentials:document.querySelector('#access-token').value,
                    choices:Array.from(document.querySelector('#workspace-known').options).map(o=>o.value),
                    directory:[...smaWorkspace.relayAccounts.values()][0].hosts};
                }""",host_id)
                assert state['directory']==[],state  # New directory response is intentionally gated.
                assert state['catalog']==[],state
                assert state['groups']==[],state
                assert state['controls']=={},state
                assert state['identity']=={},state
                assert state['panes']==0,state
                assert state['credentials']=='',state
                assert not any('room-remote' in value for value in state['choices']),state
                assert await page.evaluate("smaWorkspace.connections.get('local').connected")
                assert await page.evaluate("smaWorkspace.connections.get('local').generation")==local_generation
                assert sessions[1].id in remote.hub.sessions
            finally:
                await browser.close()
