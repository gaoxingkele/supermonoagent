"""Real desktop discovery, isolated loopback relay and controlled PTYs only."""
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from supermonoagent.daemon import relay_uplink
from supermonoagent.protocol import pack_json, unpack, unpack_json
from supermonoagent.relay import Relay
from test_client_integration import isolated_host


EDGE = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')


async def eventually(predicate):
    async with asyncio.timeout(8):
        while not predicate():
            await asyncio.sleep(.01)


@asynccontextmanager
async def discovery_fixture():
    async with isolated_host() as (local, _, _), isolated_host() as (remote, _, _), isolated_host() as (private, _, _):
        private.token = private.hub.default_owner = private.hub.groups['default'].owner = 'other-owner-fixture'
        local.hub.machine, remote.hub.machine, private.hub.machine = 'Local fixture', 'Remote fixture', 'Private fixture'
        windows = [daemon.hub.open_window(name, owner=daemon.token) for daemon, name in
                   [(local, 'Local terminal'), (remote, 'Remote terminal'), (private, 'Private terminal')]]
        sessions = [daemon.hub.open_cli(window.id, 'cmd') for daemon, window in zip((local, remote, private), windows)]
        relay = Relay(development=True)
        jobs = []
        async with serve(relay.handler, '127.0.0.1', 0) as relay_server, serve(local.handle_socket, '127.0.0.1', 0, process_request=local.serve_http) as page_server:
            url = f'ws://127.0.0.1:{relay_server.sockets[0].getsockname()[1]}/ws'
            page_url = f'http://127.0.0.1:{page_server.sockets[0].getsockname()[1]}/workspace?direct=1&renderer=dom#token={local.token}'
            for daemon, code in [(local, 'home-room'), (remote, 'new-room'), (private, 'private-room')]:
                daemon.relay_targets = [{'url': url, 'code': code}]

            async def start(daemon, code):
                job = asyncio.create_task(relay_uplink(daemon, url, code, code))
                jobs.append(job)
                await eventually(lambda: code in relay.rooms and relay.rooms[code].host)
                return job

            await start(local, 'home-room')
            await start(private, 'private-room')
            try:
                yield local, remote, private, relay, url, page_url, windows, sessions, start
            finally:
                for job in jobs:
                    job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)


def test_direct_workspace_automatically_tracks_same_token_computers_without_stealing_layout():
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Local Edge required')
    asyncio.run(_check_discovery(playwright))


async def _check_discovery(playwright):
    async with discovery_fixture() as (local, remote, private, relay, url, page_url, windows, sessions, start):
        resizes = []
        sessions[0].pty.resize = lambda cols, rows: resizes.append((cols, rows))
        async with playwright.async_playwright() as runtime:
            browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
            try:
                page = await browser.new_page(viewport={'width': 1200, 'height': 800})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.goto(page_url)
                await page.wait_for_function('window.smaWorkspace?.connected && smaWorkspace.terminal')
                await page.wait_for_function('smaWorkspace.directories.size === 1', timeout=5000)
                baseline = await page.evaluate('smaWorkspace.captureUIState()')
                size = (sessions[0].cols, sessions[0].rows)
                count = len(resizes)
                job = await start(remote, 'new-room')
                await page.wait_for_function("[...smaWorkspace.connections.values()].some(h=>h.autoDirectory && h.config.code==='new-room' && h.catalog.length)")
                remote_id = await page.evaluate("[...smaWorkspace.connections.values()].find(h=>h.config.code==='new-room').id")
                assert await page.locator('#tree').locator('.session-label').filter(has_text='Remote terminal').count() == 1
                assert await page.locator('#tree').locator('.session-label').filter(has_text='Private terminal').count() == 0
                assert await page.evaluate('smaWorkspace.captureUIState()') == baseline
                assert (sessions[0].cols, sessions[0].rows) == size
                assert len(resizes) == count
                assert not any(session.pty.written for session in sessions)

                # Open a discovered terminal as a viewer; subsequent removal prunes its pane.
                await page.evaluate('([host,sid])=>smaWorkspace.openHostSession(host,sid,false)', [remote_id, sessions[1].id])
                await page.wait_for_function('smaWorkspace.panes.size === 2')
                remote.hub.close_window(windows[1].id)
                await page.wait_for_function("![...smaWorkspace.connections.values()].find(h=>h.config.code==='new-room').catalog.length")
                await page.wait_for_function('smaWorkspace.panes.size === 1')
                replacement = remote.hub.open_window('Replacement terminal', owner=remote.token)
                replacement_session = remote.hub.open_cli(replacement.id, 'cmd')
                await page.wait_for_function("document.querySelector('#tree').textContent.includes('Replacement terminal')")

                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
                await page.wait_for_function("![...smaWorkspace.connections.values()].some(h=>h.config.code==='new-room')")
                await start(remote, 'new-room')
                await page.wait_for_function("[...smaWorkspace.connections.values()].some(h=>h.config.code==='new-room' && h.connected)")
                assert await page.evaluate("[...smaWorkspace.connections.values()].find(h=>h.config.code==='new-room').id") == remote_id

                # Recover directory sockets without manual add, then discover from a cold page.
                generation = await page.evaluate('[...smaWorkspace.directories.values()][0].generation')
                await page.evaluate('()=>{for(const d of smaWorkspace.directories.values())d.ws.close();}')
                await page.wait_for_function("generation=>[...smaWorkspace.directories.values()].every(d=>d.generation>generation && d.phase==='connected')", arg=generation)
                await page.reload()
                await page.wait_for_function("[...smaWorkspace.connections.values()].some(h=>h.config.code==='new-room' && h.connected)")
                cold = await browser.new_page(viewport={'width': 1100, 'height': 760})
                await cold.goto(page_url)
                await cold.wait_for_function("window.smaWorkspace && [...smaWorkspace.connections.values()].some(h=>h.config.code==='new-room' && h.connected)")

                # Rotation removes metadata and access; no newly discovered credential is stored.
                await remote._relay_sockets[url].send(json.dumps({'op': 'tokens', 'tokens': ['rotated-owner-fixture']}))
                for viewer in [page, cold]:
                    await viewer.wait_for_function("![...smaWorkspace.connections.values()].some(h=>h.config.code==='new-room')")
                    stored = await viewer.evaluate('JSON.stringify({...localStorage})')
                    assert remote_id not in stored
                    assert 'new-room' not in stored
                    assert local.token not in stored
                    assert 'rotated-owner-fixture' not in stored
                    assert await viewer.locator('#tree').locator('.session-label').filter(has_text='Private terminal').count() == 0
                assert not replacement_session.pty.written
                assert not any(session.pty.written for session in sessions)
                assert not errors, errors
            finally:
                await browser.close()


def test_unknown_rooms_require_explicit_token_discovery_capability():
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Local Edge required')
    asyncio.run(_check_legacy_directory(playwright))


async def _check_legacy_directory(playwright):
    async with discovery_fixture() as (local, remote, private, relay, url, page_url, windows, sessions, start):
        snapshot = relay._snapshot

        def unsupported_snapshot(rooms, tokens=()):
            value = json.loads(snapshot(rooms, tokens))
            value['capabilities'].pop('token_discovery')
            return json.dumps(value)

        relay._snapshot = unsupported_snapshot
        async with playwright.async_playwright() as runtime:
            browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
            try:
                page = await browser.new_page()
                await page.goto(page_url)
                await page.wait_for_function("window.smaWorkspace && [...smaWorkspace.directories.values()].some(d=>d.phase==='connected')")
                await start(remote, 'new-room')
                revision = relay.revision
                await page.wait_for_function('revision=>[...smaWorkspace.directories.values()].some(d=>d.revision>=revision)', arg=revision)
                assert await page.evaluate("[...smaWorkspace.connections.keys()]") == ['local']
                assert await page.evaluate("[...smaWorkspace.directories.values()][0].hosts.map(h=>h.code)") == ['home-room']
                assert await page.locator('#tree').locator('.session-label').filter(has_text='Remote terminal').count() == 0
                assert not any(session.pty.written for session in sessions)
            finally:
                await browser.close()


@pytest.mark.parametrize('legacy_service', [False, True])
def test_local_relay_configuration_changes_refresh_discovery_without_duplicate_own_computer(legacy_service):
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Local Edge required')
    asyncio.run(_check_live_configuration(playwright, legacy_service))


async def _check_live_configuration(playwright, legacy_service):
    async with discovery_fixture() as (local, remote, private, relay, url, page_url, windows, sessions, start):
        # Equal display names are not proof of the same computer: inventories differ.
        remote.hub.machine = local.hub.machine
        remote.hub.project = local.hub.project
        if legacy_service:
            state = local._state

            def old_service_state(*args, **kwargs):
                op, payload = unpack(state(*args, **kwargs))
                value = unpack_json(payload)
                for item in value['service']['relays']:
                    item.pop('code', None)
                return pack_json(op, value)

            local._state = old_service_state
        await start(remote, 'new-room')
        async with playwright.async_playwright() as runtime:
            browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
            try:
                page = await browser.new_page()
                await page.goto(page_url)
                await page.wait_for_function("window.smaWorkspace && [...smaWorkspace.connections.values()].some(h=>h.config.code==='new-room' && h.connected)")
                await page.wait_for_function("[...smaWorkspace.directories.values()].every(d=>d.phase==='connected')")
                await page.wait_for_function("[...smaWorkspace.connections.values()].filter(h=>h.id==='local'||h.config.code==='home-room').length===1", timeout=5000)
                assert await page.evaluate("[...smaWorkspace.connections.values()].filter(h=>h.id==='local'||h.config.code==='home-room').length") == 1
                assert await page.locator('#tree').locator('.session-label').filter(has_text='Local terminal').count() == 1
                baseline = await page.evaluate('smaWorkspace.captureUIState()')
                local.relay_targets = []
                local.publish_status()
                await page.wait_for_function('smaWorkspace.directories.size===0 && smaWorkspace.connections.size===1')
                local.relay_targets = [{'url': url, 'code': 'home-room'}]
                local.publish_status()
                await page.wait_for_function("[...smaWorkspace.connections.values()].some(h=>h.config.code==='new-room' && h.connected)")
                assert await page.evaluate('smaWorkspace.captureUIState()') == baseline
                assert await page.evaluate("[...smaWorkspace.connections.values()].filter(h=>h.id==='local'||h.config.code==='home-room').length") == 1
                assert not any(session.pty.written for session in sessions)
            finally:
                await browser.close()

