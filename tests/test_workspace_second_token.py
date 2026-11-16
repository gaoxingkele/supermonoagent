"""A second registered token sees and controls only its own desktop windows."""
from pathlib import Path
import json

import pytest

from test_workspace_profile_rooms import fixture, run


def test_second_token_has_an_isolated_desktop():
    run(check)


async def check(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, ops, windows, sessions):
        daemon = daemons[0]
        # The second user registers their own account on this daemon.
        reply = daemon.account_reply({'op': 'register', 'email': 'peer@example.com', 'password': 'secret1', 'id': 't1'})
        second = reply['token']
        assert second and second != daemon.token
        assert second in daemon.access_tokens(), 'second token must be published to the relay room'
        # The HTTP register endpoint republishes room tokens; direct calls must too.
        daemon.publish_tokens()

        page_owner = await browser.new_page(viewport={'width': 1536, 'height': 960})
        page_peer = await browser.new_page(viewport={'width': 1536, 'height': 960})
        errors = []
        page_owner.on('pageerror', lambda error: errors.append(str(error)))
        page_peer.on('pageerror', lambda error: errors.append(str(error)))
        await page_owner.goto(url)
        await page_owner.wait_for_function('smaWorkspace?.activePane!==undefined&&smaWorkspace.connections.size>0&&[...smaWorkspace.connections.values()].some(h=>h.catalog.length)')
        # The second user connects the way a remote computer does: through the
        # relay room with their own token.
        peer_url = url.replace('?direct=1', f'?relay={_endpoint}&code=profile-room-0').replace(f'#token={daemon.token}', f'#token={second}')
        await page_peer.goto(peer_url)
        await page_peer.wait_for_function("smaWorkspace?.connections?.size>0&&[...smaWorkspace.connections.values()].some(h=>h.connected&&h.config.code==='profile-room-0')")

        owner_windows = await page_owner.evaluate("()=>[...smaWorkspace.connections.values()].flatMap(h=>h.catalog.map(w=>w.title))")
        peer_windows = await page_peer.evaluate("()=>[...smaWorkspace.connections.values()].flatMap(h=>h.catalog.map(w=>w.title))")
        assert len(owner_windows) >= 2, owner_windows
        assert peer_windows == [], f'second token must not see the owner desktop: {peer_windows}'

        # A window opened under the second token appears only in that desktop.
        peer_window = daemon.hub.open_window('Peer desktop', owner=second)
        peer_session = daemon.hub.open_cli(peer_window.id, 'cmd')
        peer_session.pty.inbox.put(b'peer-only output\r\n')
        await page_peer.wait_for_function("()=>[...smaWorkspace.connections.values()].some(h=>h.catalog.some(w=>w.title==='Peer desktop'))")
        owner_titles = await page_owner.evaluate("()=>[...smaWorkspace.connections.values()].flatMap(h=>h.catalog.map(w=>w.title))")
        assert 'Peer desktop' not in owner_titles, f'owner desktop must not see the peer window: {owner_titles}'

        # The peer page subscribes and receives only its own session output.
        await page_peer.evaluate("""()=>{
          const host=[...smaWorkspace.connections.values()].find(h=>h.catalog.some(w=>w.title==='Peer desktop'));
          const win=host.catalog.find(w=>w.title==='Peer desktop');
          const session=(win.sessions||[])[0];
          smaWorkspace.openHostSession(host.id,session.id);
        }""")
        await page_peer.wait_for_function("()=>{const p=smaWorkspace.activePane;return p&&p.term.buffer.active.getLine(0)?.translateToString(true).includes('peer-only output')}")

        # The owner page never receives the peer session's bytes.
        assert not await page_owner.evaluate("document.body.innerText.includes('peer-only output')"), 'peer output leaked into the owner page'

        artifacts = Path('.omx/artifacts/workspace-second-token')
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / 'verification.json').write_text(json.dumps({
            'owner_windows': owner_windows, 'peer_windows_before': peer_windows,
            'peer_window_visible_to_peer': True, 'peer_window_hidden_from_owner': True,
            'relay_tokens': len(daemon.access_tokens()), 'verdict': 'pass',
        }, indent=2), encoding='utf-8')
        assert not errors
