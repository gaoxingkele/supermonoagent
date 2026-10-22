"""Window label preferences belong to the shared window, not one browser."""

import asyncio
import os
from pathlib import Path

import pytest

from supermonoagent.protocol import OP_GROUP, pack_json, unpack
from test_client_integration import isolated_host
from websockets.asyncio.server import serve


def test_theme_update_requires_window_owner():
    asyncio.run(_check_owner())


async def _check_owner():
    async with isolated_host() as (daemon, _direct, _relay):
        window = daemon.hub.open_window("Build", owner=daemon.token)
        owner = daemon.hub.attach(asyncio.Queue(), token=daemon.token)
        stranger = daemon.hub.attach(asyncio.Queue(), token="another-owner")
        async def update(client, theme):
            _, payload = unpack(pack_json(OP_GROUP, {"action": "tab-theme", "window": window.id, "theme": theme}))
            await daemon._op(client, OP_GROUP, payload)

        await update(owner, "blue")
        assert daemon.hub.summary(daemon.token)["windows"][0]["tab_theme"] == "blue"
        await update(stranger, "red")
        assert window.tab_theme == "blue"
        await update(owner, "not-a-theme")
        assert window.tab_theme == "blue"
        await update(owner, "")
        assert window.tab_theme == ""


def test_browser_tab_menu_syncs_name_and_color():
    playwright = pytest.importorskip("playwright.async_api")
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    if not edge.exists():
        pytest.skip("Local Edge is required")
    asyncio.run(_check_menu(playwright, edge))


async def _check_menu(playwright, edge):
    async with isolated_host() as (daemon, _direct, _relay):
        window = daemon.hub.open_window("Build", owner=daemon.token)
        daemon.hub.open_cli(window.id, "cmd")
        async with serve(daemon.handle_socket, "127.0.0.1", 0, process_request=daemon.serve_http) as server:
            base = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            async with playwright.async_playwright() as runtime:
                browser = await runtime.chromium.launch(executable_path=str(edge), headless=True)
                try:
                    workspace = await browser.new_page(viewport={"width": 1100, "height": 760})
                    classic = await browser.new_page(viewport={"width": 1100, "height": 760})
                    errors = []
                    for page, route in ((workspace, "workspace"), (classic, "legacy")):
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        await page.goto(f"{base}/{route}?token={daemon.token}&renderer=dom")
                        await page.wait_for_function("window.smaWorkspace?.terminal || window.__lastTerm")

                    await workspace.locator(".tab").click(button="right")
                    menu = workspace.get_by_role("menu", name="窗口标签")
                    assert await menu.get_by_role("menuitemradio").count() == 7
                    artifacts = os.environ.get("SUPERMONO_LAYOUT_ARTIFACTS")
                    if artifacts:
                        destination = Path(artifacts)
                        destination.mkdir(parents=True, exist_ok=True)
                        await workspace.screenshot(path=str(destination / "tab-color-menu.png"))
                    await menu.get_by_role("menuitemradio", name="蓝底黄字").click()
                    for page, selector in ((workspace, ".tab"), (classic, ".card .bar"), (classic, ".row.child")):
                        await page.wait_for_function("selector => getComputedStyle(document.querySelector(selector)).backgroundColor === 'rgb(21, 62, 159)'", arg=selector)
                    assert window.tab_theme == "blue"

                    await workspace.locator(".tab").click(button="right")
                    assert await menu.get_by_role("menuitemradio", name="蓝底黄字").get_attribute("aria-checked") == "true"
                    await menu.get_by_role("menuitem", name="修改名称…").click()
                    await workspace.locator("#rename-value").fill("编译 <任务>")
                    await workspace.locator("#rename-form").get_by_role("button", name="保存").click()
                    await classic.wait_for_function("document.querySelector('.card .bar span').textContent.includes('编译 <任务>')")
                    assert window.title == "编译 <任务>"
                    assert await workspace.locator(".tab img").count() == 0

                    await workspace.reload()
                    await workspace.wait_for_function("window.smaWorkspace?.connected && document.querySelector('.tab.sma-tab-themed')")
                    await classic.locator(".card .bar").click(button="right")
                    classic.once("dialog", lambda dialog: dialog.accept("发布任务"))
                    await classic.get_by_role("menuitem", name="修改名称…").click()
                    await workspace.wait_for_function("document.querySelector('.tab').textContent.includes('发布任务')")

                    await workspace.locator(".tab").click(button="right")
                    await menu.get_by_role("menuitem", name="恢复默认配色").click()
                    await classic.wait_for_function("!document.querySelector('.card .bar').classList.contains('sma-tab-themed')")
                    assert window.tab_theme == ""
                    await workspace.locator(".tab").click(button="right")
                    await workspace.keyboard.press("Escape")
                    assert await menu.count() == 0
                    await workspace.locator(".tab").click(button="right")
                    await workspace.locator(".side-heading").click()
                    assert await menu.count() == 0
                    assert not errors, errors
                finally:
                    await browser.close()
