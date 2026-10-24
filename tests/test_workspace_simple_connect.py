"""The simple relay form discovers all rooms before committing a bound choice."""
import asyncio
from urllib.parse import urlsplit

from supermonoagent.daemon import Daemon, relay_uplink
from test_client_integration import ControlledPty
from test_workspace_profile_rooms import fixture, eventually, run


def test_test_connection_selects_all_rooms_remembers_token_and_applies_choices(tmp_path):
    run(lambda playwright: check_form(playwright, tmp_path))


def test_explicit_room_choices_override_implicit_native_relay_scope(tmp_path):
    run(lambda playwright: check_form(playwright, tmp_path, native=True))


def test_native_configured_relay_prefills_its_owner_token_only_at_that_endpoint():
    run(check_native_default)


async def check_native_default(playwright):
    async with fixture(playwright) as (browser, url, endpoint, trap, trap_messages, daemons, *_):
        # Represents the authenticated state produced by --relay URL --token TOKEN.
        daemons[0].relay_targets = [{'url': endpoint, 'code': 'profile-room-0'}]
        page = await browser.new_page()
        await page.goto(url)
        await page.wait_for_function('window.smaWorkspace?.connected')
        await page.locator('#connect-button').click()
        assert await page.locator('#relay-profile-port').input_value() == str(urlsplit(endpoint).port)
        assert await page.locator('#relay-profile-token').input_value() == daemons[0].token
        await page.locator('#relay-profile-port').fill(str(urlsplit(trap).port))
        assert await page.locator('#relay-profile-token').input_value() == ''
        assert not trap_messages


async def check_form(playwright, artifacts, native=False):
    async with fixture(playwright) as (browser, url, endpoint, trap, trap_messages, daemons, ops, _windows, _sessions):
        extra_daemons, jobs = [], []

        async def extra_room(code):
            daemon = Daemon(daemons[0].token)
            daemon.hub.persist = False
            daemon.hub.pty_factory = lambda *_: ControlledPty()
            daemon.hub.attach_loop(asyncio.get_running_loop())
            daemon.hub.set_identity(code, 'Simple connection')
            window = daemon.hub.open_window(code + ' window', owner=daemon.token)
            daemon.hub.open_cli(window.id, 'cmd')
            extra_daemons.append(daemon)
            jobs.append(asyncio.create_task(relay_uplink(daemon, endpoint, code, code)))
            await eventually(lambda: endpoint in daemon.relay_live)
            return daemon

        try:
            await extra_room('simple-extra')
            if native:
                daemons[0].relay_targets = [{'url': endpoint, 'code': 'profile-room-0'}]
            page = await browser.new_page(viewport={'width': 1100, 'height': 850})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto(url)
            await page.wait_for_function('window.smaWorkspace?.connected && smaWorkspace.activePane')
            await page.evaluate('() => {window.originalSimplePane=smaWorkspace.activePane;}')
            await page.locator('#connect-button').click()
            assert await page.locator('#relay-profile-remember').is_checked()
            assert not await page.locator('#relay-profile-room').is_visible()
            await page.locator('#relay-profile-address').fill('127.0.0.1')
            await page.locator('#relay-profile-port').fill(str(urlsplit(endpoint).port))
            await page.locator('#relay-profile-token').fill(daemons[0].token)
            for values in ops:
                values.clear()
            await page.get_by_role('button', name='测试连接', exact=True).click()
            choices = page.locator('#relay-probe-rooms input[type=checkbox]')
            await page.wait_for_function("document.querySelectorAll('#relay-probe-rooms input').length===2")
            assert all([await choices.nth(n).is_checked() for n in range(2)])
            if not native:
                assert not await page.evaluate("[...smaWorkspace.connections.values()].some(h=>h.autoDirectory)")
            if not native:
                assert not any(ops)
            await page.screenshot(path=str(artifacts / 'simple-connection.png'))
            await page.locator('#relay-probe-rooms label').filter(has_text='simple-extra').locator('input').uncheck()
            primary = page.locator('#relay-probe-rooms label').filter(has_text='profile-room-0').locator('input')
            await primary.uncheck()
            assert await page.locator('#relay-profile-connect').is_disabled()
            await primary.check()
            await page.get_by_role('button', name='连接所选房间', exact=True).click()
            if not native:
                await page.wait_for_function("[...smaWorkspace.connections.values()].some(h=>h.config.code==='profile-room-0'&&h.connected)")
            await page.wait_for_function("![...smaWorkspace.connections.values()].some(h=>h.config.code==='simple-extra')")
            assert not await page.evaluate("[...smaWorkspace.connections.values()].some(h=>h.config.code==='simple-extra'||h.config.code==='profile-room-1')")
            assert daemons[0].token in await page.evaluate('JSON.stringify(localStorage)')
            await extra_room('simple-new-room')
            await page.wait_for_function("[...smaWorkspace.connections.values()].some(h=>h.config.code==='simple-new-room'&&h.connected)")
            row = page.locator('.relay-profile-row label').filter(has_text='simple-extra').locator('input')
            assert not await row.is_checked()
            await row.check()
            await page.wait_for_function("[...smaWorkspace.connections.values()].some(h=>h.config.code==='simple-extra'&&h.connected)")
            await row.uncheck()
            await page.wait_for_function("![...smaWorkspace.connections.values()].some(h=>h.config.code==='simple-extra')")
            assert await page.evaluate('smaWorkspace.activePane===originalSimplePane')
            assert all(not session.pty.written for daemon in daemons + extra_daemons for session in daemon.hub.sessions.values())
            await page.reload()
            await page.wait_for_function("smaWorkspace.connected && [...smaWorkspace.connections.values()].some(h=>h.config.code==='simple-new-room'&&h.connected)")
            assert not await page.evaluate("[...smaWorkspace.connections.values()].some(h=>h.config.code==='simple-extra')")
            await page.locator('#connect-button').click()
            assert await page.locator('#relay-profile-token').input_value() == daemons[0].token
            assert await page.locator('#relay-profile-remember').is_checked()
            # Target edits invalidate the previous choice and never forward a saved token.
            await page.get_by_role('button', name='测试连接', exact=True).click()
            await page.wait_for_function("!document.querySelector('#relay-profile-connect').disabled")
            assert not await page.locator('#relay-probe-rooms label').filter(has_text='simple-extra').locator('input').is_checked()
            await page.locator('#relay-profile-port').fill(str(urlsplit(trap).port))
            assert await page.locator('#relay-profile-token').input_value() == ''
            assert await page.locator('#relay-profile-connect').is_disabled()
            assert not await page.locator('#relay-probe-rooms input').count()
            await asyncio.sleep(.05)
            assert not trap_messages
            await page.evaluate("() => {const profiles=JSON.parse(localStorage.getItem('sma.workspace.relay-profiles'));profiles[0].excludedRooms=[];localStorage.setItem('sma.workspace.relay-profiles',JSON.stringify(profiles));}")
            await page.reload()
            await page.wait_for_function('smaWorkspace.connected')
            await asyncio.sleep(.1)
            assert not await page.evaluate("[...smaWorkspace.connections.values()].some(h=>h.autoDirectory)")
            assert not errors
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            for daemon in extra_daemons:
                for window in list(daemon.hub.windows):
                    daemon.hub.close_window(window)
