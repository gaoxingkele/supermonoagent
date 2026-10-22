"""Saved workspaces keep independent concurrent host views and credentials."""

import asyncio
import os
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from supermonoagent.daemon import relay_uplink
from test_client_integration import isolated_host


def test_add_room_workspace_and_return_to_local_host():
    playwright = pytest.importorskip("playwright.async_api")
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    if not edge.exists():
        pytest.skip("Local Edge is required")
    asyncio.run(_check_hosts(playwright, edge))


async def _check_hosts(playwright, edge):
    async with isolated_host() as (local, _direct, relay), isolated_host() as (remote, _other_direct, _other_relay):
        remote.token = remote.hub.default_owner = remote.hub.groups["default"].owner = "remote-owner-only"
        local_window = local.hub.open_window("Local terminal", owner=local.token)
        local_session = local.hub.open_cli(local_window.id, "cmd")
        remote_window = remote.hub.open_window("Remote terminal", owner=remote.token)
        remote_session = remote.hub.open_cli(remote_window.id, "cmd")
        # IDs are only unique within a host: force a collision to verify owner routing.
        old_id = remote_session.id
        remote_session.id = local_session.id
        remote.hub.sessions[local_session.id] = remote.hub.sessions.pop(old_id)
        remote_window.sessions[local_session.id] = remote_window.sessions.pop(old_id)
        remote_window.focused = local_session.id
        local_session.pty.inbox.put(b"LOCAL-ONLY\r\n")
        remote_session.pty.inbox.put(b"REMOTE-ONLY\r\n")
        local.relay_targets = [{"name": "Test relay", "url": relay, "code": "local-test"}]
        uplink = asyncio.create_task(relay_uplink(remote, relay, "office-room", "office"))
        try:
            async with asyncio.timeout(5):
                while relay not in remote.relay_live:
                    await asyncio.sleep(0.01)
            async with serve(local.handle_socket, "127.0.0.1", 0, process_request=local.serve_http) as server:
                async with playwright.async_playwright() as runtime:
                    browser = await runtime.chromium.launch(executable_path=str(edge), headless=True)
                    try:
                        page = await browser.new_page()
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        await page.goto(f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/workspace?direct=1&renderer=dom#token={local.token}")
                        await page.wait_for_function("window.smaWorkspace?.connected")
                        await page.locator("#new-project").click()
                        await page.locator("#workspace-name").fill("Office computer")
                        await page.locator("#workspace-code").fill("office-room")
                        assert await page.locator("#workspace-relay").input_value() == relay
                        assert await page.locator("#workspace-token").input_value() == ""
                        artifacts = os.environ.get("SUPERMONO_LAYOUT_ARTIFACTS")
                        if artifacts:
                            destination = Path(artifacts)
                            destination.mkdir(parents=True, exist_ok=True)
                            await page.screenshot(path=str(destination / "add-workspace.png"))
                        await page.locator("#workspace-token").fill(remote.token)
                        await page.locator("#workspace-form").get_by_role("button", name="保存并连接").click()
                        await page.wait_for_function("window.smaWorkspace?.connected && smaWorkspace.activePane?.title.textContent.includes('Remote terminal')")
                        await page.wait_for_function("smaWorkspace.terminal.buffer.active.getLine(0)?.translateToString(true).includes('REMOTE-ONLY')")
                        assert "LOCAL-ONLY" not in await page.evaluate("Array.from({length:smaWorkspace.terminal.buffer.active.length},(_,i)=>smaWorkspace.terminal.buffer.active.getLine(i).translateToString(true)).join('\\n')")
                        assert remote.token not in await page.evaluate("localStorage.getItem('sma.workspace.hosts')")
                        await page.locator("#connect-button").click()
                        assert await page.locator("#access-token").input_value() == remote.token
                        assert not await page.locator("#remember-token").is_checked()
                        await page.locator("#connection-close").click()
                        await page.evaluate("smaWorkspace.terminal.paste('remote-input')")
                        async with asyncio.timeout(3):
                            while remote_session.pty.written != b"remote-input":
                                await asyncio.sleep(0.01)
                        assert local_session.pty.written == b""
                        await page.get_by_role("button", name="切换到本机", exact=True).click()
                        await page.wait_for_function("smaWorkspace.connected && smaWorkspace.activePane?.title.textContent.includes('Local terminal')")
                        await page.reload()
                        await page.wait_for_function("window.smaWorkspace?.connected")
                        await page.get_by_role("button", name="切换到 Office computer", exact=True).click()
                        await page.wait_for_function("smaWorkspace.connected && smaWorkspace.activePane?.title.textContent.includes('Remote terminal')")
                        await page.get_by_role("button", name="编辑工作区 Office computer").click()
                        await page.locator("#workspace-remove").click()
                        await page.wait_for_function("smaWorkspace.connected && smaWorkspace.activePane?.title.textContent.includes('Local terminal')")
                        assert await page.get_by_role("button", name="切换到 Office computer", exact=True).count() == 0
                        assert list(local.hub.sessions) == [local_session.id]
                        assert list(remote.hub.sessions) == [remote_session.id]
                        assert not errors, errors
                    finally:
                        await browser.close()
        finally:
            uplink.cancel()
            await asyncio.gather(uplink, return_exceptions=True)
