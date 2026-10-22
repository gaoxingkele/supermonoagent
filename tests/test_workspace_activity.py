"""Sidebar activity is live output, not history replay or another window's traffic."""
import asyncio
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from test_client_integration import isolated_host


def test_sidebar_activity_colors_indentation_replay_and_disconnect():
    playwright = pytest.importorskip('playwright.async_api')
    edge = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
    if not edge.exists():
        pytest.skip('Local Edge required')
    asyncio.run(_check_activity(playwright, edge))


async def _check_activity(playwright, edge):
    async with isolated_host() as (daemon, _direct, _relay):
        sessions = []
        for title in ('Active window', 'Quiet window'):
            group = daemon.hub.add_group(title + ' group', owner=daemon.token)
            window = daemon.hub.open_window(title, group_id=group.id, owner=daemon.token)
            sessions.append(daemon.hub.open_cli(window.id, 'cmd'))
        sessions[0].pty.inbox.put(b'Historical output\r\n')
        async with serve(daemon.handle_socket, '127.0.0.1', 0, process_request=daemon.serve_http) as server:
            async with playwright.async_playwright() as runtime:
                browser = await runtime.chromium.launch(executable_path=str(edge), headless=True)
                try:
                    page = await browser.new_page(viewport={'width': 1100, 'height': 760})
                    await page.goto(f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/workspace?direct=1&renderer=dom#token={daemon.token}')
                    await page.wait_for_function('window.smaWorkspace?.connected && smaWorkspace.panes.size===2 && smaWorkspace.metrics.snapshots>0')
                    rows = page.locator('#tree .session-row')
                    await page.wait_for_function('[...document.querySelectorAll("#tree .session-row")].every(row=>row.dataset.state==="waiting")')
                    assert await rows.count() == 2
                    assert await rows.first.locator('.session-dot').get_attribute('aria-label') == '状态：等待输入（暂未收到新输出）'
                    assert await rows.first.locator('.session-dot').evaluate('(el)=>getComputedStyle(el).backgroundColor') == 'rgb(244, 207, 85)'
                    assert await page.locator('#tree .project-label, #tree .group-label, #new-group').count() == 0
                    geometry = await rows.first.evaluate('row=>({row:row.getBoundingClientRect().left,machine:row.closest(".computer-tree").querySelector(".workspace-host").getBoundingClientRect().left,dot:row.querySelector(".session-dot").getBoundingClientRect().width,label:row.querySelector(".session-label").getBoundingClientRect().left,right:row.getBoundingClientRect().right,sidebar:document.querySelector("#sidebar").getBoundingClientRect().right})')
                    assert geometry['row'] >= geometry['machine'] + 16
                    assert geometry['dot'] == 8
                    assert geometry['right'] <= geometry['sidebar']
                    out = Path('.omx/artifacts/sidebar-activity')
                    out.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(out / 'waiting-yellow.png'))
                    sessions[0].pty.inbox.put(b'Fresh output\r\n')
                    first = page.locator(f'#tree .session-row[data-session="{sessions[0].id}"]')
                    second = page.locator(f'#tree .session-row[data-session="{sessions[1].id}"]')
                    await page.wait_for_function('id=>document.querySelector(`[data-session="${id}"].session-row`).dataset.state==="refreshing"', arg=sessions[0].id)
                    assert await second.get_attribute('data-state') == 'waiting'
                    assert await first.locator('.session-dot').evaluate('(el)=>({color:getComputedStyle(el).backgroundColor,animation:getComputedStyle(el).animationName})') == {'color':'rgb(94, 220, 139)', 'animation':'workspace-session-pulse'}
                    await page.screenshot(path=str(out / 'live-green-quiet-yellow.png'))
                    await page.emulate_media(reduced_motion='reduce')
                    assert await first.locator('.session-dot').evaluate('(el)=>getComputedStyle(el).animationName') == 'none'
                    await page.wait_for_function('id=>document.querySelector(`[data-session="${id}"].session-row`).dataset.state==="waiting"', arg=sessions[0].id)
                    # A socket failure changes every window of this host immediately.
                    for socket in list(server.connections):
                        await socket.close(code=4001, reason='Isolated test disconnect')
                    await page.wait_for_function('[...document.querySelectorAll("#tree .session-row")].every(row=>row.dataset.state==="disconnected")')
                    assert await first.locator('.session-dot').evaluate('(el)=>getComputedStyle(el).backgroundColor') == 'rgb(237, 107, 104)'
                    await page.screenshot(path=str(out / 'disconnected-red.png'))
                    await page.reload()
                    await page.wait_for_function('window.smaWorkspace?.connected && smaWorkspace.panes.size===2')
                    await page.wait_for_function('[...document.querySelectorAll("#tree .session-row")].every(row=>row.dataset.state==="waiting")')
                    assert all(not session.pty.written for session in sessions)
                finally:
                    await browser.close()
