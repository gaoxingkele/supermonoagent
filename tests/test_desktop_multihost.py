"""Desktop identity routing against two isolated daemons on one loopback relay."""
import asyncio
import json
from pathlib import Path
from urllib.parse import urlencode

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from supermonoagent.daemon import relay_uplink
from supermonoagent.protocol import pack_json
from test_client_integration import isolated_host, barrier, receive_op


EDGE = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')


def test_desktop_parallel_hosts_collision_failure_and_captured_actions():
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Local Edge is required')
    asyncio.run(_parallel_hosts(playwright))


async def _native(url, token, session, cols, rows):
    socket = await connect(url, additional_headers={'Authorization': 'Bearer ' + token})
    await socket.send(pack_json(1, {'token': token, 'multiplex': True, 'reliable': True}))
    await receive_op(socket, 2)
    await socket.send(pack_json(16, {'sessions': [session]}))
    await socket.send(pack_json(5, {'session': session, 'cols': cols, 'rows': rows, 'take_control': True}))
    await barrier(socket)
    return socket


async def _parallel_hosts(playwright):
    async with isolated_host() as (local, local_direct, relay), isolated_host() as (remote, remote_direct, _):
        remote.token = remote.hub.default_owner = remote.hub.groups['default'].owner = 'remote-owner-only'
        left = local.hub.open_window('Local terminal', owner=local.token)
        left_session = local.hub.open_cli(left.id, 'cmd')
        right = remote.hub.open_window('Remote terminal', owner=remote.token)
        right_session = remote.hub.open_cli(right.id, 'cmd')
        old = right_session.id
        right_session.id = left_session.id
        remote.hub.sessions[left_session.id] = remote.hub.sessions.pop(old)
        right.sessions[left_session.id] = right.sessions.pop(old)
        right.focused = left_session.id
        left_session.pty.inbox.put(b'LOCAL-ONLY\r\n')
        right_session.pty.inbox.put(b'REMOTE-ONLY\r\n')
        uplink = asyncio.create_task(relay_uplink(remote, relay, 'office-room', 'office'))
        natives = []
        try:
            async with asyncio.timeout(5):
                while relay not in remote.relay_live:
                    await asyncio.sleep(.01)
            natives.append(await _native(local_direct, local.token, left_session.id, 77, 33))
            natives.append(await _native(remote_direct, remote.token, right_session.id, 91, 41))
            owners = (left_session.resize_owner, right_session.resize_owner)
            async with serve(local.handle_socket, '127.0.0.1', 0, process_request=local.serve_http) as server:
                async with playwright.async_playwright() as runtime:
                    browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
                    try:
                        page = await browser.new_page(viewport={'width': 1440, 'height': 900})
                        errors, socket_urls, request_urls = [], [], []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        page.on('websocket', lambda socket: socket_urls.append(socket.url))
                        page.on('request', lambda request: request_urls.append(request.url))
                        saved = [
                            {'id': 'office:machine:one', 'name': 'Office computer', 'code': 'office-room', 'relay': relay},
                            {'id': 'bad:credentials', 'name': 'Bad credentials', 'code': 'office-room', 'relay': relay},
                        ]
                        await page.add_init_script('''(() => {
                          localStorage.setItem('sma.workspace.hosts', %s);
                          sessionStorage.setItem('sma.workspace.host-token.office:machine:one', 'remote-owner-only');
                          sessionStorage.setItem('sma.workspace.host-token.bad:credentials', 'wrong');
                        })()''' % json.dumps(json.dumps(saved)))
                        query = urlencode({'relay': relay, 'code': 'local-test', 'renderer': 'dom'})
                        await page.goto(f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/workspace?{query}#token={local.token}')
                        await page.wait_for_function("smaWorkspace.connections.size===3 && [...smaWorkspace.connections.values()].filter(h=>h.connected).length===2 && smaWorkspace.connections.get('bad:credentials').phase==='auth-failed'")
                        await page.wait_for_function('smaWorkspace.panes.size===1 && smaWorkspace.terminal.cols===77 && smaWorkspace.terminal.rows===33')
                        generations = await page.evaluate('[...smaWorkspace.connections.values()].map(h=>h.generation)')
                        await page.evaluate("smaWorkspace.switchHost('office:machine:one')")
                        await page.wait_for_function('smaWorkspace.panes.size===2 && smaWorkspace.terminal.cols===91 && smaWorkspace.terminal.rows===41')
                        assert await page.evaluate('[...smaWorkspace.connections.values()].map(h=>h.generation)') == generations
                        assert (left_session.resize_owner, right_session.resize_owner) == owners
                        assert not await page.evaluate('location.href.includes("token=")')
                        assert all('token=' not in url for url in socket_urls)
                        keys = await page.evaluate('[...smaWorkspace.panes.keys()]')
                        assert len(set(keys)) == 2
                        assert {tuple(json.loads(key)) for key in keys} == {('local', left_session.id), ('office:machine:one', left_session.id)}
                        await page.evaluate("smaWorkspace.split('horizontal')")
                        left_session.pty.inbox.put(b'LOCAL-LIVE\r\n')
                        right_session.pty.inbox.put(b'REMOTE-LIVE\r\n')
                        await page.wait_for_function("[...smaWorkspace.panes.values()].every(p=>Array.from({length:p.term.buffer.active.length},(_,i)=>p.term.buffer.active.getLine(i).translateToString(true)).join('\\n').includes(p.host.id==='local'?'LOCAL-LIVE':'REMOTE-LIVE'))")
                        screens = await page.evaluate("Object.fromEntries([...smaWorkspace.panes.values()].map(p=>[p.host.id,Array.from({length:p.term.buffer.active.length},(_,i)=>p.term.buffer.active.getLine(i).translateToString(true)).join('\\n')]))")
                        assert 'REMOTE-' not in screens['local']
                        assert 'LOCAL-' not in screens['office:machine:one']
                        await page.evaluate("[...smaWorkspace.panes.values()].forEach(p=>p.term.paste(p.host.id==='local'?'left-input':'right-input'))")
                        async with asyncio.timeout(3):
                            while left_session.pty.written != b'left-input' or right_session.pty.written != b'right-input':
                                await asyncio.sleep(.01)
                        # A dialog opened on local retains that authority after changing selection.
                        await page.evaluate("smaWorkspace.switchHost('local')")
                        await page.locator('.pane').filter(has=page.locator('.pane-title', has_text='Local terminal')).get_by_role('button', name='重命名窗口', exact=True).click()
                        await page.locator('#rename-value').fill('Local renamed')
                        await page.evaluate("smaWorkspace.switchHost('office:machine:one')")
                        await page.locator('#rename-form').evaluate('(form)=>form.requestSubmit()')
                        await page.wait_for_function("smaWorkspace.connections.get('local').catalog[0].title==='Local renamed'")
                        assert left.title == 'Local renamed'
                        assert right.title == 'Remote terminal'
                        # Capture old generation message callback, reconnect only remote, then replay it.
                        await page.evaluate("window.oldRemoteMessage=smaWorkspace.connections.get('office:machine:one').ws.onmessage")
                        local_generation = await page.evaluate("smaWorkspace.connections.get('local').generation")
                        await page.locator('#connect-button').click()
                        await page.locator('#relay-profiles-dialog summary').click()
                        await page.locator('#relay-profiles-advanced').click()
                        await page.locator('#connection-form').evaluate('(form)=>form.requestSubmit()')
                        await page.wait_for_function("smaWorkspace.connections.get('office:machine:one').connected && smaWorkspace.connections.get('office:machine:one').generation>1")
                        await page.evaluate("oldRemoteMessage({data:JSON.stringify({error:'STALE-CALLBACK'})})")
                        assert await page.evaluate("smaWorkspace.connections.get('office:machine:one').error") != 'STALE-CALLBACK'
                        assert await page.evaluate("smaWorkspace.connections.get('local').generation") == local_generation
                        # Dialogs from an old socket generation cannot rename or terminate after a drop.
                        await page.evaluate("smaWorkspace.switchHost('office:machine:one')")
                        await page.locator('.pane').filter(has=page.locator('.pane-title', has_text='Remote terminal')).get_by_role('button', name='重命名窗口', exact=True).click()
                        await page.locator('#rename-value').fill('STALE-RENAME')
                        await page.evaluate("smaWorkspace.connections.get('office:machine:one').ws.close()")
                        await page.wait_for_function("smaWorkspace.connections.get('office:machine:one').connected && smaWorkspace.connections.get('office:machine:one').generation>2")
                        await page.locator('#rename-form').evaluate('(form)=>form.requestSubmit()')
                        await page.evaluate("()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))")
                        assert right.title == 'Remote terminal'
                        assert await page.evaluate("smaWorkspace.connections.get('local').generation") == local_generation
                        await page.locator('.pane').filter(has=page.locator('.pane-title', has_text='Remote terminal')).get_by_role('button', name='结束后台会话', exact=True).click()
                        await page.evaluate("smaWorkspace.connections.get('office:machine:one').ws.close()")
                        await page.wait_for_function("smaWorkspace.connections.get('office:machine:one').connected && smaWorkspace.connections.get('office:machine:one').generation>3")
                        await page.locator('#terminate-form').evaluate('(form)=>form.requestSubmit()')
                        assert right_session.id in remote.hub.sessions
                        # The healthy host still renders and accepts input throughout remote reconnects.
                        left_session.pty.inbox.put(b'LOCAL-AFTER-DROP\r\n')
                        await page.wait_for_function("(()=>{const p=[...smaWorkspace.panes.values()].find(p=>p.host.id==='local');return Array.from({length:p.term.buffer.active.length},(_,i)=>p.term.buffer.active.getLine(i).translateToString(true)).join('\\n').includes('LOCAL-AFTER-DROP');})()")
                        await page.evaluate("[...smaWorkspace.panes.values()].find(p=>p.host.id==='local').term.paste('-after-drop')")
                        async with asyncio.timeout(3):
                            while left_session.pty.written != b'left-input-after-drop':
                                await asyncio.sleep(.01)
                        # Editing room or endpoint clears the saved credential before it can be sent.
                        await page.locator('.computer-tree').filter(has=page.get_by_role('button',name='切换到 Office computer',exact=True)).locator('.workspace-host-edit').click()
                        await page.get_by_role('menuitem', name='编辑工作区 Office computer').click()
                        assert await page.locator('#workspace-token').input_value() == remote.token
                        await page.locator('#workspace-code').fill('other-room')
                        assert await page.locator('#workspace-token').input_value() == ''
                        await page.locator('#workspace-code').fill('office-room')
                        await page.locator('#workspace-token').fill(remote.token)
                        await page.locator('#workspace-relay').fill(relay + '/changed-endpoint')
                        assert await page.locator('#workspace-token').input_value() == ''
                        await page.locator('#workspace-cancel').click()
                        # Directory authorization exposes only enrolled known rooms, and no public health fetch.
                        await page.wait_for_function("[...smaWorkspace.directories.values()].some(d=>d.phase==='connected'&&d.hosts.some(h=>h.code==='office-room'&&h.online))")
                        await page.locator('.computer-tree[data-host="office:machine:one"] .workspace-host-edit').click()
                        await page.get_by_role('menuitem', name='编辑工作区 Office computer').click()
                        await page.locator('#workspace-verify').click()
                        await page.wait_for_function("document.querySelector('#workspace-verification').textContent.includes('已验证在线电脑')")
                        await page.locator('#workspace-cancel').click()
                        # Programmatic form changes also cannot carry an old token to a different room.
                        await page.locator('.computer-tree[data-host="office:machine:one"] .workspace-host-edit').click()
                        await page.get_by_role('menuitem', name='编辑工作区 Office computer').click()
                        await page.locator('#workspace-code').evaluate("input=>input.value='different-room'")
                        await page.locator('#workspace-form').evaluate('(form)=>form.requestSubmit()')
                        await page.wait_for_function("smaWorkspace.connections.get('office:machine:one').config.code==='different-room'")
                        assert await page.evaluate("smaWorkspace.connections.get('office:machine:one').config.token") == ''
                        assert await page.evaluate("sessionStorage.getItem('sma.workspace.host-token.office:machine:one')") is None
                        # Removing a computer releases its views and sockets, never its daemon PTY.
                        await page.locator('.computer-tree[data-host="office:machine:one"] .workspace-host-edit').click()
                        await page.get_by_role('menuitem', name='编辑工作区 Office computer').click()
                        await page.locator('#workspace-remove').click()
                        await page.wait_for_function("!smaWorkspace.connections.has('office:machine:one')&&smaWorkspace.connections.get('local').connected")
                        assert right_session.id in remote.hub.sessions
                        assert left_session.id in local.hub.sessions
                        assert all('/health' not in url for url in request_urls)
                        # An old relay rejecting the watch role cannot disturb its terminal sockets.
                        old_page = await browser.new_page()
                        await old_page.add_init_script('''(() => {
                          localStorage.setItem('sma.workspace.hosts', %s);
                          sessionStorage.setItem('sma.workspace.host-token.office:machine:one', 'remote-owner-only');
                          const NativeSocket=WebSocket;
                          window.WebSocket=class extends NativeSocket {
                            send(message){
                              if(typeof message==='string'&&JSON.parse(message).op==='watch'){this.close(4000,'unsupported role');return;}
                              return super.send(message);
                            }
                          };
                        })()''' % json.dumps(json.dumps(saved[:1])))
                        await old_page.goto(f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/workspace?{query}#token={local.token}')
                        await old_page.wait_for_function("[...smaWorkspace.connections.values()].every(h=>h.connected)&&[...smaWorkspace.directories.values()].every(d=>d.phase==='unsupported')")
                        await old_page.evaluate("smaWorkspace.switchHost('office:machine:one')")
                        await old_page.wait_for_function("smaWorkspace.activeHost.id==='office:machine:one'&&smaWorkspace.terminal")
                        assert await old_page.evaluate('smaWorkspace.connections.size') == 2
                        await old_page.reload()
                        await old_page.wait_for_function("[...smaWorkspace.connections.values()].every(h=>h.connected)&&smaWorkspace.activeHost.id==='office:machine:one'")
                        assert not await old_page.evaluate('location.href.includes("token=")')
                        await old_page.close()
                        assert not errors, errors
                    finally:
                        await browser.close()
        finally:
            for native in natives:
                await native.close()
            uplink.cancel()
            await asyncio.gather(uplink, return_exceptions=True)
