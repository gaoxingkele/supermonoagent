"""Refresh replays the server snapshot; a narrow Codex footer mirrors real cells."""
import asyncio
from unittest.mock import patch

import pytest

from test_android_auto_discovery import EDGE, mobile_page
from test_workspace_auto_discovery import discovery_fixture, eventually


@pytest.mark.parametrize('renderer', ['dom', 'webgl'])
def test_mobile_server_refresh_and_responsive_actual_footer(renderer):
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Local Edge required')
    from supermonoagent.protocol import CAPABILITIES
    with patch.dict(CAPABILITIES, {'snapshot_refresh': True}):
        asyncio.run(check_refresh(playwright, renderer))


async def check_refresh(playwright, renderer):
    async with discovery_fixture() as (local, remote, private, relay, url, page_url, windows, sessions, start):
        session = sessions[0]
        windows[0].title = 'Codex'
        owner = local.hub.attach(asyncio.Queue(), token=local.token)
        owner.multiplex = True
        local.hub.set_subscriptions(owner, [session.id])
        local.hub.resize_session(owner, session.id, 158, 44, True)
        baseline = session.resize_owner, session.cols, session.rows
        calls = []
        original = local.hub.refresh_subscriptions
        local.hub.refresh_subscriptions = lambda client, ids: calls.append((client, ids))
        async with playwright.async_playwright() as runtime:
            browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
            try:
                page, _ = await mobile_page(browser, page_url, url, local.token, renderer)
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                footer = '\x1b[41;1H> UNSENT_PROMPT_DO_NOT_MIRROR\x1b[42;1H? for shortcuts\x1b[43;1HContext left: 67% remaining\x1b[44;1HWeekly limit: 82% remaining'
                session.pty.inbox.put(footer.encode())
                await page.locator('#terminal-status').wait_for(state='visible')
                text = await page.locator('#terminal-status').inner_text()
                assert 'Context left: 67%' in text and 'Weekly limit: 82%' in text
                assert 'UNSENT_PROMPT_DO_NOT_MIRROR' not in text and 'shortcuts' not in text
                for width in (280, 390, 660):
                    await page.set_viewport_size({'width': width, 'height': 844})
                    await page.evaluate('()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
                    assert await page.locator('#terminal-status').evaluate('e=>e.scrollWidth<=e.clientWidth&&e.getBoundingClientRect().right<=innerWidth&&e.getBoundingClientRect().left>=0')
                    assert '82%' in await page.locator('#terminal-status').inner_text()
                await page.locator('#draft').evaluate("e=>{e.value='unsent refresh draft';e.dispatchEvent(new Event('input'));}")
                await page.locator('#refresh-screen').click()
                await eventually(lambda: len(calls) == 1)
                assert await page.locator('#refresh-screen').get_attribute('aria-busy') == 'true'
                assert not await page.locator('#notice').is_visible() or '已刷新' not in await page.locator('#notice').inner_text()
                await page.locator('#refresh-screen').click()
                await asyncio.sleep(.05)
                assert len(calls) == 1, 'Repeated clicks must coalesce until a new snapshot is written'
                original(*calls[0])
                await page.wait_for_function("document.getElementById('refresh-screen').getAttribute('aria-busy')===null")
                assert await page.locator('#refresh-screen').inner_text() == '已刷新'
                assert await page.locator('#draft').input_value() == 'unsent refresh draft'
                assert (session.resize_owner, session.cols, session.rows) == baseline
                assert not session.pty.written

                # Missing actual values must hide the strip rather than invent percentages.
                session.pty.inbox.put(b'\x1b[43;1H\x1b[2KContext left: unknown\x1b[44;1H\x1b[2K')
                await page.locator('#terminal-status').wait_for(state='hidden')
                await page.locator('#refresh-screen').click()
                await eventually(lambda: len(calls) == 2)
                other_window = local.hub.open_window('Claude Code', owner=local.token)
                other = local.hub.open_cli(other_window.id, 'cmd')
                await page.locator('#sessions').click()
                await page.locator(f'#session-list .session[data-session="{other.id}"]').click()
                await page.wait_for_function('sid=>MobileTerminal.active===sid&&MobileTerminal.ready', arg=other.id)
                assert await page.locator('#refresh-screen').get_attribute('aria-busy') is None
                await page.locator('#refresh-screen').click()
                await eventually(lambda: len(calls) == 3)
                local.hub._send(calls[1][0], local.hub._session_snapshot(calls[1][0], session))
                await asyncio.sleep(.1)
                assert await page.locator('#refresh-screen').get_attribute('aria-busy') == 'true', 'An old target snapshot cannot finish the new refresh'
                original(*calls[2])
                await page.wait_for_function("document.getElementById('refresh-screen').getAttribute('aria-busy')===null")
                other.pty.inbox.put(footer.encode())
                await asyncio.sleep(.1)
                assert not await page.locator('#terminal-status').is_visible(), 'Other agents must not inherit the Codex strip'
                await page.locator('#refresh-screen').click()
                await eventually(lambda: len(calls) == 4)
                await page.evaluate('MobileTerminal.pause()')
                assert await page.locator('#refresh-screen').get_attribute('aria-busy') is None
                await page.locator('#refresh-screen').click()
                assert len(calls) == 4 and not session.pty.written and not other.pty.written
                # Late snapshots from a detached connection cannot complete a new refresh.
                original(*calls[-1])
                assert not errors, errors
            finally:
                await browser.close()


def test_legacy_refresh_selects_read_pointer_without_releasing_size_lease():
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Local Edge required')
    asyncio.run(check_legacy_refresh(playwright))


async def check_legacy_refresh(playwright):
    from supermonoagent.protocol import pack_json, unpack, unpack_json
    async with discovery_fixture() as (local, remote, private, relay, url, page_url, windows, sessions, start):
        second_window = local.hub.open_window('Second Codex window', owner=local.token)
        second = local.hub.open_cli(second_window.id, 'cmd')
        original_state, original_op = local._state, local._op
        requests = []

        def old_state(*args, **kwargs):
            op, payload = unpack(original_state(*args, **kwargs))
            body = unpack_json(payload)
            body['capabilities'].pop('snapshot_refresh', None)
            return pack_json(op, body)

        async def old_op(client, op, payload):
            if op in (13, 14, 16):
                body = unpack_json(payload)
                requests.append((op, body))
                if op == 16:
                    # Historical frozen backend ignores the refresh field.
                    body.pop('refresh', None)
                    payload = pack_json(op, body)[5:]
            return await original_op(client, op, payload)

        local._state, local._op = old_state, old_op
        async with playwright.async_playwright() as runtime:
            browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
            try:
                page, _ = await mobile_page(browser, page_url, url, local.token)
                await page.locator('#sessions').click()
                await page.locator(f'#session-list .session[data-session="{second.id}"]').click()
                await page.wait_for_function('sid=>MobileTerminal.active===sid&&MobileTerminal.ready', arg=second.id)
                await page.locator('#control').click()
                await page.wait_for_function('MobileTerminal.controls?.owner===true')
                await page.evaluate('()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
                baseline = second.resize_owner, second.cols, second.rows
                focused = second_window.focused
                for _ in range(2):
                    requests.clear()
                    await page.locator('#refresh-screen').click()
                    await page.wait_for_function("document.getElementById('refresh-screen').textContent==='已刷新'&&document.getElementById('refresh-screen').getAttribute('aria-busy')===null")
                    assert len(requests) == 1 and requests[0][0] in (13, 14)
                    assert (second.resize_owner, second.cols, second.rows) == baseline
                    assert second_window.focused == focused
                    assert not second.pty.written
                assert requests[0][0] == 14, 'Subsequent refresh reuses the correct client window pointer'
            finally:
                await browser.close()
