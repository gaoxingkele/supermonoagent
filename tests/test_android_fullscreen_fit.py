"""The phone keeps readable glyphs and pans locally without resizing the PTY."""
import asyncio
import importlib.util
import json
import mimetypes
from pathlib import Path
from urllib.parse import quote

import pytest
from websockets.asyncio.server import serve

from supermonoagent.web import text_response
from test_client_integration import isolated_host


ROOT = Path(__file__).resolve().parents[1]
EDGE = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
PROBE_SPEC = importlib.util.spec_from_file_location("readable_android_ui_probe", ROOT / "scripts/test-android-ui.py")
UI_PROBE = importlib.util.module_from_spec(PROBE_SPEC)
PROBE_SPEC.loader.exec_module(UI_PROBE)


@pytest.mark.parametrize("renderer", ["dom", "webgl"])
def test_phone_readable_viewport_preserves_font_and_shared_grid(renderer):
    playwright = pytest.importorskip("playwright.async_api")
    if not EDGE.exists():
        pytest.skip("Local Edge required for packaged mobile geometry")
    asyncio.run(check_phone_fit(playwright, renderer))


async def check_phone_fit(playwright, renderer):
    async with isolated_host() as (daemon, _direct, _relay):
        window = daemon.hub.open_window("Fullscreen geometry", owner=daemon.token)
        session = daemon.hub.open_cli(window.id, "cmd")
        desktop = daemon.hub.attach(asyncio.Queue(), key="desktop-grid-owner", token=daemon.token)
        desktop.multiplex = True
        desktop.subscriptions = {session.id}
        assert daemon.hub.resize_session(desktop, session.id, 158, 44, True)

        async def http(connection, request):
            if request.path.startswith("/mobile/"):
                relative = request.path.split("?", 1)[0].removeprefix("/mobile/")
                folder = ROOT / "android/assets/mobile"
                if relative.startswith("vendor/"):
                    relative = relative.removeprefix("vendor/")
                    folder = ROOT / "src/supermonoagent/static"
                folder = folder.resolve()
                asset = (folder / relative).resolve()
                if asset.is_relative_to(folder) and asset.is_file():
                    return text_response(connection, 200, asset.read_text(encoding="utf8"),
                                         mimetypes.guess_type(asset.name)[0] or "text/plain")
                return text_response(connection, 404, "Missing mobile asset", "text/plain")
            return await daemon.serve_http(connection, request)

        async with serve(daemon.handle_socket, "127.0.0.1", 0, process_request=http) as server:
            origin = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            config = quote(json.dumps({"endpoint": origin, "token": daemon.token, "renderer": renderer}))
            async with playwright.async_playwright() as runtime:
                browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
                try:
                    page = await browser.new_page(viewport={"width": 718, "height": 748},
                                                  device_scale_factor=2.75, is_mobile=True, has_touch=True)
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.goto(origin + "/mobile/index.html#" + config)
                    await page.wait_for_function("window.MobileTerminal?.ready")
                    expected_grid = (158, 44)
                    short_grid_evidence = []
                    geometry = """() => {
                        const m=MobileTerminal,t=m.terminal,r=document.getElementById('screen').getBoundingClientRect(),
                        e=t.element.getBoundingClientRect(),cell=t._core._renderService.dimensions.css.cell,
                        transform=getComputedStyle(t.element).transform,scale=new DOMMatrixReadOnly(transform).a;
                        return {cols:t.cols,rows:t.rows,owner:m.controls.owner,screenWidth:r.width,screenHeight:r.height,
                            width:e.width,height:e.height,gridWidth:(t.cols*cell.width+12+t._core.viewport.scrollBarWidth)*scale,
                            gridHeight:(t.rows*cell.height+5)*scale,lineHeight:t.options.lineHeight,
                            letterSpacing:t.options.letterSpacing,scaleX:scale,scaleY:new DOMMatrixReadOnly(transform).d,
                            effectiveFont:t.options.fontSize*scale,font:t.options.fontSize,
                            left:document.getElementById('screen').scrollLeft,top:document.getElementById('screen').scrollTop,
                            maxTop:document.getElementById('screen').scrollHeight-document.getElementById('screen').clientHeight,
                            documentOverflow:document.documentElement.scrollWidth>innerWidth+1};
                    }"""

                    async def assert_filled():
                        try:
                            await page.wait_for_function("""() => {
                                const t=MobileTerminal.terminal,e=t.element.getBoundingClientRect(),r=document.getElementById('screen').getBoundingClientRect(),
                                    gridHeight=t.rows*t._core._renderService.dimensions.css.cell.height+5;
                                // ResizeObserver applies row spacing on the next frame.
                                // An already oversized outer box alone is not proof that
                                // the freshly rotated viewport has been fitted.
                                return e.width>=r.width*.95&&e.height>=r.height*.95&&
                                    gridHeight>=r.height-6&&
                                    (gridHeight<=r.height+24||t.options.lineHeight===1)&&
                                    document.getElementById('screen').dataset.scale==='1';
                            }""", timeout=3000)
                        except Exception:
                            raise AssertionError(await page.evaluate(geometry)) from None
                        value = await page.evaluate(geometry)
                        assert (value["cols"], value["rows"]) == expected_grid and not value["owner"]
                        assert value["effectiveFont"] == value["font"] and value["effectiveFont"] >= 14, value
                        assert not value["documentOverflow"], value
                        assert value["lineHeight"] >= 1 and value["letterSpacing"] == 0, value
                        if value["gridHeight"] > value["screenHeight"] + 24:
                            assert value["lineHeight"] == 1, value
                        assert value["scaleX"] == value["scaleY"], value
                        assert (session.resize_owner, session.cols, session.rows) == (desktop.key, *expected_grid)
                        observed = await page.evaluate(UI_PROBE.GEOMETRY)
                        assert observed["viewportMode"] == "readable"
                        assert observed["effectiveFontSize"] == value["font"]
                        assert not observed["violations"], observed

                    await assert_filled()
                    assert (await page.evaluate(geometry))["top"] == (await page.evaluate(geometry))["maxTop"]
                    # The canonical grid is wider than the phone; a horizontal drag
                    # pans the bounded viewport without sending terminal input.
                    async def drag(dx, dy):
                        await page.evaluate("""({dx,dy}) => {
                            const target=document.getElementById('screen'),r=target.getBoundingClientRect(),x=r.left+200,y=r.top+r.height/2;
                            const touch=(px,py)=>new Touch({identifier:1,target,clientX:px,clientY:py});
                            const fire=(type,px,py,end=false)=>target.dispatchEvent(new TouchEvent(type,{bubbles:true,cancelable:true,
                                touches:end?[]:[touch(px,py)],changedTouches:[touch(px,py)]}));
                            fire('touchstart',x,y);fire('touchmove',x+dx,y+dy);fire('touchend',x+dx,y+dy,true);
                        }""", {"dx": dx, "dy": dy})

                    await drag(-180, 0)
                    assert (await page.evaluate(geometry))["left"] >= 175
                    await drag(0, 150)
                    assert (await page.evaluate(geometry))["top"] < (await page.evaluate(geometry))["maxTop"]
                    await page.locator("#history-bottom").click()
                    # Both normal and fullscreen alternate buffers use the same local grid.
                    for enter, leave in ((b"", b""), (b"\x1b[?1049h", b"\x1b[?1049l")):
                        session.pty.inbox.put(enter + b"\x1b[2J\x1b[HFIT TOP\x1b[44;1HFIT BOTTOM")
                        await page.wait_for_function("MobileTerminal.terminal.buffer.active.getLine(MobileTerminal.terminal.buffer.active.baseY)?.translateToString(true).includes('FIT TOP')")
                        for viewport in ({"width": 390, "height": 844}, {"width": 844, "height": 390},
                                         {"width": 390, "height": 500}, {"width": 718, "height": 748}):
                            await page.set_viewport_size(viewport)
                            await assert_filled()
                        if enter:
                            await drag(0, 160)
                            await drag(-150, 0)
                            assert not session.pty.written  # Alternate-buffer viewing never sends arrows.
                            await page.locator("#history-bottom").click()
                        if leave:
                            session.pty.inbox.put(leave)
                    session.pty.inbox.put(("\r\n".join(f"HISTORY-{i:03}" for i in range(200))+"\r\n").encode())
                    await page.wait_for_function("MobileTerminal.terminal.buffer.active.baseY>100")
                    await page.locator("#history-bottom").click()
                    await drag(0, (await page.evaluate(geometry))["maxTop"] + 65)
                    before = await page.evaluate("MobileTerminal.terminal.buffer.active.viewportY")
                    await page.evaluate("""() => {
                        const target=document.getElementById('screen'),r=target.getBoundingClientRect(),x=100,y=r.top+100;
                        const fire=(type,py,end=false)=>{const t=new Touch({identifier:1,target,clientX:x,clientY:py});
                            target.dispatchEvent(new TouchEvent(type,{bubbles:true,cancelable:true,touches:end?[]:[t],changedTouches:[t]}));};
                        fire('touchstart',y);for(let dy=11;dy<=40;dy++)fire('touchmove',y+dy);fire('touchend',y+40,true);
                    }""")
                    after = await page.evaluate("MobileTerminal.terminal.buffer.active.viewportY")
                    assert 1 <= before-after <= 3  # Fine drags accumulate pixels, not one row per event.
                    await page.locator("#history-bottom").click()
                    # A user-selected font remains its actual on-screen size.
                    await page.locator("#options").click()
                    await page.locator("#font").fill("18")
                    await page.locator("#font").dispatch_event("input")
                    await page.locator("#settings-close").click()
                    await assert_filled()
                    assert (await page.evaluate(geometry))["effectiveFont"] == 18
                    # Shared-original mode intentionally keeps the natural cell aspect/spacing.
                    await page.locator("#options").click()
                    await page.locator("#sizing").select_option("shared")
                    await page.locator("#settings-close").click()
                    await page.wait_for_function("MobileTerminal.terminal.options.lineHeight===1&&MobileTerminal.terminal.options.letterSpacing===0")
                    await page.wait_for_function("Number(document.getElementById('screen').dataset.scale)<1")
                    await page.locator("#options").click()
                    await page.locator("#sizing").select_option("phone")
                    await page.locator("#settings-close").click()
                    await assert_filled()
                    # A short desktop grid must fill the actual rendered row surface,
                    # rather than only enlarging its outer background container.
                    expected_grid = (84, 24)
                    assert daemon.hub.resize_session(desktop, session.id, *expected_grid, False)
                    await page.wait_for_function("MobileTerminal.terminal.rows===24")
                    for viewport in ({"width": 718, "height": 748}, {"width": 390, "height": 844},
                                     {"width": 844, "height": 390}, {"width": 390, "height": 500}):
                        await page.set_viewport_size(viewport)
                        await assert_filled()
                        value = await page.evaluate(geometry)
                        rendered = await page.locator(".xterm-screen").bounding_box()
                        assert rendered["height"] >= value["screenHeight"] - 6, value
                        assert value["effectiveFont"] == 18, value
                        short_grid_evidence.append({"viewport": viewport, **value, "renderedHeight": rendered["height"]})
                    artifact = ROOT / ".omx/artifacts/mobile-short-grid"
                    artifact.mkdir(parents=True, exist_ok=True)
                    (artifact / f"{renderer}.json").write_text(json.dumps(short_grid_evidence, indent=2), encoding="utf8")
                    await page.set_viewport_size({"width": 718, "height": 748})
                    await assert_filled()
                    # Explicit takeover uses unadapted 18px cells, not expanded viewer row gaps.
                    await page.locator("#control").click()
                    await page.wait_for_function("MobileTerminal.controls.owner&&MobileTerminal.terminal.options.lineHeight===1&&MobileTerminal.terminal.options.letterSpacing===0")
                    value = await page.evaluate(geometry)
                    assert session.resize_owner != desktop.key
                    assert 20 < session.cols < 100 and session.rows > 15, value
                    assert not errors, errors
                    assert not session.pty.written
                finally:
                    await browser.close()
        daemon.hub.detach(desktop.key)
