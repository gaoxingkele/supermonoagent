"""Manual directory/catalog refresh uses real sockets and fixture-owned PTYs."""
import asyncio

from websockets.asyncio.server import serve
from supermonoagent.relay import Relay

from supermonoagent.daemon import Daemon, relay_uplink
from test_client_integration import ControlledPty
from test_workspace_profile_rooms import fixture, add_profile, wait_room, eventually, run


def test_menu_refresh_discovers_rooms_and_windows_without_restarting_live_views(tmp_path):
    run(lambda playwright: check_refresh(playwright, tmp_path))


async def check_refresh(playwright, artifacts):
    async with fixture(playwright) as (browser, url, endpoint, _trap, _messages, daemons, ops, _windows, sessions):
        async with serve(Relay(development=True).handler, '127.0.0.1', 0) as other_server:
            other_endpoint = f'ws://127.0.0.1:{other_server.sockets[0].getsockname()[1]}/ws'
            page = await browser.new_page(viewport={'width': 1100, 'height': 760})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto(url)
            await page.wait_for_function('window.smaWorkspace?.connected && smaWorkspace.panes.size > 0')
            await add_profile(page, endpoint, daemons[0].token)
            await wait_room(page, 0)
            await add_profile(page, other_endpoint, daemons[0].token)
            await page.wait_for_function("smaWorkspace.directories.size===2 && [...smaWorkspace.directories.values()].every(d=>d.phase==='connected')")
            await page.locator('#relay-profiles-close').click()
            # Simulate a missed push while preserving the actual server state/socket.
            await page.evaluate('''() => {
                window.refreshProbe = {drop: true, watches: 0, active: smaWorkspace.activeSession,
                    pane: smaWorkspace.activePane, socket: smaWorkspace.connections.get('local').ws};
                for (const d of smaWorkspace.directories.values()) {
                    const receive = d.ws.onmessage, send = d.ws.send.bind(d.ws);
                    d.ws.onmessage = e => {if (!refreshProbe.drop) receive(e);};
                    d.ws.send = data => {if (JSON.parse(data).op === 'watch') refreshProbe.watches++; send(data);};
                }
                for (const h of smaWorkspace.connections.values()) {
                    const receive = h.ws.onmessage;
                    h.ws.onmessage = e => {
                        if (refreshProbe.drop && typeof e.data !== 'string' &&
                            TerminalClient.unpack(new Uint8Array(e.data)).op === 7) return;
                        receive(e);
                    };
                }
            }''')
            new_window = daemons[0].hub.open_window('Refreshed catalog window', owner=daemons[0].token)
            extra = Daemon(daemons[0].token)
            extra.hub.persist = False
            extra.hub.pty_factory = lambda *_: ControlledPty()
            extra.hub.attach_loop(asyncio.get_running_loop())
            extra.hub.set_identity('Newly discovered computer', 'Refresh fixture')
            extra_window = extra.hub.open_window('Newly discovered window', owner=extra.token)
            extra.hub.open_cli(extra_window.id, 'cmd')
            job = asyncio.create_task(relay_uplink(extra, other_endpoint, 'refresh-new-room', 'refresh-extra'))
            try:
                await eventually(lambda: other_endpoint in extra.relay_live)
                await asyncio.sleep(.1)
                assert not await page.evaluate("[...smaWorkspace.connections.values()].some(h=>h.config.code==='refresh-new-room')")
                assert not await page.evaluate("id=>smaWorkspace.connections.get('local').catalog.some(w=>w.id===id)", new_window.id)
                ops[0].clear()
                ops[1].clear()
                await page.evaluate('refreshProbe.drop = false')
                await page.locator('#app-menu-toggle').click()
                item = page.get_by_role('menuitem', name='刷新中继', exact=True)
                await item.wait_for(state='visible')
                bounds = await item.bounding_box()
                assert bounds and bounds['x'] >= 0 and bounds['y'] + bounds['height'] <= 760
                await page.screenshot(path=str(artifacts / 'relay-menu.png'))
                # Double activation must not flood the relay's watch rate limit.
                await item.evaluate('(button)=>{button.click();button.click();}')
                await page.wait_for_function("id=>smaWorkspace.connections.get('local').catalog.some(w=>w.id===id)", arg=new_window.id)
                await page.wait_for_function("[...smaWorkspace.connections.values()].some(h=>h.config.code==='refresh-new-room'&&h.catalog.length===1)")
                await asyncio.sleep(.15)
                assert await page.locator('#app-menu').is_hidden()
                assert await page.evaluate('refreshProbe.watches') == 2
                assert await page.evaluate("smaWorkspace.activeSession===refreshProbe.active && smaWorkspace.activePane===refreshProbe.pane && smaWorkspace.connections.get('local').ws===refreshProbe.socket")
                assert not await page.evaluate("[...smaWorkspace.connections.values()].some(h=>h.config.code==='profile-room-1')")
                assert all(op == 16 and body.get('refresh') is not True for op, body, _ in ops[0])
                assert ops[1] == []
                assert all(not session.pty.written for session in sessions[0] + sessions[1])
                # An explicitly disconnected directory can be retried from the same menu.
                await page.evaluate('''() => {
                    for (const d of smaWorkspace.directories.values()) d.ws.close();
                }''')
                await page.wait_for_function("[...smaWorkspace.directories.values()].every(d=>d.phase==='offline')")
                await page.evaluate('() => {for(const d of smaWorkspace.directories.values()) clearTimeout(d.retryTimer);}')
                await asyncio.sleep(1.05)
                await page.locator('#app-menu-toggle').click()
                await page.get_by_role('menuitem', name='刷新中继', exact=True).click()
                await page.wait_for_function("[...smaWorkspace.directories.values()].every(d=>d.phase==='connected')")
                await page.wait_for_function("[...smaWorkspace.connections.values()].some(h=>h.config.code==='refresh-new-room'&&h.connected)")
                assert await page.evaluate('smaWorkspace.activeSession===refreshProbe.active && smaWorkspace.activePane===refreshProbe.pane')
                assert not errors
            finally:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
                for wid in list(extra.hub.windows):
                    extra.hub.close_window(wid)
