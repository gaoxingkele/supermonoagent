"""Exercise the physical Android renderer through USB against an isolated fake PTY.

No shell process is created. Only the temporary fake PTY receives test input.
The temporary phone host profile and ADB mappings are removed after the run.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import queue
import re
import sys
import tempfile
from datetime import datetime
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
SPEC = importlib.util.spec_from_file_location("android_ui_probe", ROOT / "scripts/test-android-ui.py")
UI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UI)


class FakePty:
    """An in-memory transport with the PTY interface; never starts a process."""
    def __init__(self):
        self.written = bytearray()
        self.inbox = queue.Queue()

    def read(self, n=4096):
        from supermonoagent.pty import PtyClosed
        data = self.inbox.get()
        if data is None:
            raise PtyClosed()
        return data

    def write(self, data):
        self.written.extend(data)
        # This memory PTY models an interactive editor acknowledging received text.
        self.inbox.put(data.replace(b'\x1b[200~',b'').replace(b'\x1b[201~',b''))

    def resize(self, cols, rows):
        pass

    def close(self):
        self.inbox.put(None)


async def wait_bytes(pty, expected):
    async with asyncio.timeout(6):
        while expected not in pty.written:
            await asyncio.sleep(.02)


async def wait_page(page, expression, *, arg=None, timeout=30000):
    # Runtime evaluation works with the app's strict CSP; Playwright's string
    # wait_for_function uses page eval(), which correctly remains forbidden.
    deadline = asyncio.get_running_loop().time() + timeout / 1000
    while asyncio.get_running_loop().time() < deadline:
        result = await page.evaluate(expression, arg)
        if result:
            return result
        await asyncio.sleep(.05)
    raise TimeoutError('Android page condition timed out')


async def exercise(page, device, daemon, first, second, window, output, report):
    """All input-producing actions happen after the fake session ID is confirmed."""
    await wait_page(page, "sid=>MobileTerminal.ready&&MobileTerminal.active===sid", arg=first.id, timeout=12000)
    report["fake_host_authenticated"] = True
    if await page.locator('#tools-toggle').get_attribute('aria-expanded') == 'false':
        await page.locator('#tools-toggle').click()
    assert await page.locator('#terminal-actions').is_visible() and await page.locator('#keys').is_visible()
    fixture_host=json.loads(await page.evaluate('MobileTerminal.activeKey'))[0]
    fixture_rows=f'#session-list .session[data-host="{fixture_host}"]'
    await page.locator("#options").click()
    await page.locator("#sizing").select_option("shared")
    await page.locator("#settings-close").click()
    desktop = daemon.hub.attach(asyncio.Queue(), key="usb-renderer-desktop", token=daemon.token)
    desktop.multiplex = True
    desktop.subscriptions = {first.id}
    try:
        if not daemon.hub.resize_session(desktop, first.id, 120, 30, True):
            raise AssertionError("Fake desktop could not acquire the resize lease")
        await wait_page(page, "MobileTerminal.terminal?.cols===120&&MobileTerminal.terminal.rows===30", timeout=8000)
        profiler=await page.context.new_cdp_session(page)
        await profiler.send('Profiler.enable');await profiler.send('Profiler.setSamplingInterval',{'interval':1000})
        await profiler.send('Profiler.start')
        history_samples_before=await page.evaluate('MobileTerminal.metrics.historySamples')
        for index in range(300):
            first.pty.inbox.put((f"\x1b[H\x1b[2J\x1b[1;100HRIGHT_{index:03}"
                                 f"\x1b[10;30HMIDDLE_{index:03}\x1b[28;1HBOTTOM_{index:03}").encode())
        await wait_page(page, """() => {
          const b=MobileTerminal.terminal?.buffer.active;if(!b)return false;
          return b.getLine(b.baseY)?.translateToString().slice(99).startsWith('RIGHT_299')&&
            b.getLine(b.baseY+9)?.translateToString().slice(29).startsWith('MIDDLE_299')&&
            b.getLine(b.baseY+27)?.translateToString().startsWith('BOTTOM_299');
        }""", timeout=15000)
        profile=(await profiler.send('Profiler.stop'))['profile']
        if (first.cols, first.rows) != (120, 30):
            raise AssertionError("Shared server grid changed during renderer stress")
        geometry = await UI.stable_geometry(page)
        if geometry["violations"]:
            raise AssertionError("Shared renderer geometry: " + ", ".join(geometry["violations"]))
        history_samples=await page.evaluate('MobileTerminal.metrics.historySamples')-history_samples_before
        if history_samples>20:
            raise AssertionError(f"Full-screen history was rebuilt for every output: {history_samples}")
        hits=[]
        for node in profile['nodes']:
            if node.get('hitCount'):
                frame=node['callFrame']
                hits.append((node['hitCount'],frame.get('functionName') or '(anonymous)',frame.get('url','').rsplit('/',1)[-1],frame.get('lineNumber',-1)+1))
        top=[{'samples':count,'function':function,'asset':asset,'line':line} for count,function,asset,line in sorted(hits,reverse=True)[:15]]
        report["render_stress"] = {"updates": 300, "grid": {"cols": first.cols, "rows": first.rows},
                                   "right_marker": True, "middle_marker": True, "bottom_marker": True,
                                   "history_samples": history_samples, "top_self_samples": top, "geometry": geometry}
        await page.screenshot(path=str(output / "fake-stream-webview.png"))
        device.screenshot(output / "fake-stream-device.png")

        # Composer events exercise UTF-8 transport; this memory object is the only recipient.
        expected = "USB_ASCII_123 中文输入验证"
        first.pty.inbox.put(('\r\n'.join(f'USB-HISTORY-{i:04}' for i in range(6500))+'\r\n').encode())
        await wait_page(page, 'MobileTerminal.terminal.buffer.normal.baseY>6000')
        before_history=await page.evaluate('MobileTerminal.terminal.buffer.normal.baseY')
        first.pty.inbox.put(b'\x1b[3J')
        await asyncio.sleep(.2)
        if await page.evaluate('MobileTerminal.terminal.buffer.normal.baseY')<before_history:
            raise AssertionError('Screen repaint erased mobile scrollback')
        await page.evaluate('MobileTerminal.terminal.scrollToBottom()')
        bounds=await page.locator('#screen').bounding_box()
        touch=await page.context.new_cdp_session(page)
        x=bounds['x']+bounds['width']/2;y=bounds['y']+30
        await touch.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':x,'y':y}]})
        for step in range(1,7):
            await touch.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':x,'y':y+step*25}]})
            await asyncio.sleep(.02)
        await touch.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]})
        await asyncio.sleep(.1)
        history=await page.evaluate('({baseY:MobileTerminal.terminal.buffer.active.baseY,viewportY:MobileTerminal.terminal.buffer.active.viewportY,limit:MobileTerminal.terminal.options.scrollback})')
        if history['viewportY']>=history['baseY']:
            raise AssertionError('Real touch drag did not move into terminal history')
        report['history']={**history,'touch_scroll':True,'clear_scrollback_preserved':True}
        await page.screenshot(path=str(output/'history-touch-webview.png'))
        device.screenshot(output/'history-touch-device.png')
        before_buttons = bytes(first.pty.written)
        for selector in ('#page-up', '#page-down', '#history-top', '#history-bottom'):
            await page.locator(selector).click()
        await page.locator('#history-top').click()
        if await page.evaluate('MobileTerminal.terminal.buffer.active.viewportY') != 0:
            raise AssertionError('Top button did not reach the local history start')
        await page.locator('#history-bottom').click()
        if not await page.evaluate('MobileTerminal.terminal.buffer.active.viewportY===MobileTerminal.terminal.buffer.active.baseY'):
            raise AssertionError('Bottom button did not reach latest output')
        grid_before = (first.cols, first.rows, first.resize_owner)
        await page.locator('#refresh-screen').click()
        if bytes(first.pty.written) != before_buttons or (first.cols, first.rows, first.resize_owner) != grid_before:
            raise AssertionError('Local toolbar changed remote input or desktop dimensions')
        await page.locator('[data-key="enter"]').click()
        await wait_bytes(first.pty, before_buttons + b'\r')
        if bytes(first.pty.written) != before_buttons + b'\r':
            raise AssertionError('Enter button sent more than one carriage return')
        report['navigation_buttons']={'page_up_down':True,'top_bottom':True,'refresh_local_only':True,
                                      'desktop_grid_preserved':True,'enter_exactly_once':True}
        first.task_detector.app='codex';daemon.hub._catalog()
        first.pty.inbox.put(b'\x1b[?1049h')
        await wait_page(page,"MobileTerminal.terminal.buffer.active.type==='alternate'")
        before=bytes(first.pty.written)
        await page.locator('#page-up').click()
        await wait_bytes(first.pty,before+b'\x1b[5~')
        before=bytes(first.pty.written)
        x=bounds['x']+bounds['width']/2;y=bounds['y']+bounds['height']/2-40
        await touch.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':x,'y':y}]})
        await touch.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':x,'y':y+80}]})
        await touch.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]})
        await wait_bytes(first.pty,before+b'\x1b[5~')
        report['codex_paging']={'button_exactly_once':True,'touch_swipe_exactly_once':True}
        first.pty.inbox.put(b'\x1b[?1049l')
        await wait_page(page,"MobileTerminal.terminal.buffer.active.type==='normal'")
        first.task_detector.app='';daemon.hub._catalog()
        await page.locator('#history-bottom').click()
        await page.locator('#tools-toggle').click()
        assert await page.locator('#terminal-actions').is_hidden() and await page.locator('#keys').is_hidden()
        assert await page.locator('#quick-composer').is_visible() and await page.locator('#quick-send').is_visible()
        await page.locator('#tools-toggle').click()
        assert await page.locator('#terminal-actions').is_visible() and await page.locator('#keys').is_visible()
        report['collapsible_toolbars']={'collapse':True,'expand':True,'quick_input_always_visible':True}
        quick='12345'
        before = bytes(first.pty.written)
        fields=[node for node in device.native_nodes() if node.get('resource-id')=='quick-draft']
        if len(fields)!=1:
            raise AssertionError('Could not uniquely locate the native quick input field')
        package=fields[0].get('package')
        bounds=re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]',fields[0].get('bounds',''))
        left,top,right,bottom=map(int,bounds.groups())
        device.run('shell','input','tap',str((left+right)//2),str((top+bottom)//2))
        await asyncio.sleep(.4)
        for char in quick:
            device.run('shell','input','text',char)
            await asyncio.sleep(.04)
        await asyncio.sleep(.2)
        if bytes(first.pty.written) != before:
            raise AssertionError('Quick composer sent its unfinished draft')
        report['native_ime_field']=await page.locator('#quick-draft').input_value()
        if report['native_ime_field']!=quick:
            raise AssertionError('Native IME did not commit the expected text: '+repr(report['native_ime_field']))
        device.native_find_and_tap('发送')
        await asyncio.sleep(.25)
        report['native_ime_written_tail']=bytes(first.pty.written)[len(before):].decode('utf-8','replace')
        await wait_bytes(first.pty, quick.encode())
        await wait_page(page,"document.getElementById('quick-draft').value===''&&document.getElementById('draft').value===''",timeout=6000)
        actual=bytes(first.pty.written)[len(before):]
        assert actual in (quick.encode()+b'\r', b'\x1b[200~'+quick.encode()+b'\x1b[201~\r'), repr(actual)
        report['stage']='native_ime_sent'
        device.run('shell','am','start','-n',package+'/.MainActivity','--ez','ui_probe','true','--ez','hide_keyboard','true')
        await page.evaluate('document.activeElement?.blur()')
        quick='USB_QUICK_中文'
        before = bytes(first.pty.written)
        await page.locator('#quick-draft').fill(quick)
        if bytes(first.pty.written) != before:
            raise AssertionError('Quick composer sent its unfinished Unicode draft')
        await page.locator('#quick-draft').press('Enter')
        await wait_bytes(first.pty, quick.encode())
        await wait_page(page,"document.getElementById('quick-draft').value===''&&document.getElementById('draft').value===''",timeout=6000)
        actual=bytes(first.pty.written)[len(before):]
        assert actual in (quick.encode()+b'\r', b'\x1b[200~'+quick.encode()+b'\x1b[201~\r'), repr(actual)
        report['stage']='unicode_sent'
        if await page.locator('#quick-draft').input_value() or await page.locator('#draft').input_value():
            raise AssertionError('Quick composer did not clear both draft fields')
        before = bytes(first.pty.written)
        await page.locator("#mode").click()
        await page.locator("#draft").fill(expected)
        if bytes(first.pty.written) != before:
            raise AssertionError("Composer sent its unfinished draft")
        await page.locator("#send").click()
        if await page.locator('#composer').evaluate('e=>e.open'):
            raise AssertionError('Editor did not close after accepting input')
        await wait_bytes(first.pty, expected.encode() + b"\r")
        report['stage']='modal_sent'
        if bytes(first.pty.written)[len(before):] != expected.encode() + b"\r":
            raise AssertionError("Composer output was duplicated or corrupted")
        if await page.locator("#draft").input_value():
            raise AssertionError("Composer did not clear the submitted draft")
        bounds=await page.locator('#screen').bounding_box()
        x=bounds['x']+bounds['width']/2;y=bounds['y']+bounds['height']/2
        await touch.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':x,'y':y}]})
        await touch.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]})
        await page.locator('.xterm-helper-textarea').focus()
        await page.locator("#ctrl").click()
        await page.keyboard.press("c")
        await wait_bytes(first.pty, b"\x03")
        report['stage']='ctrl_c_sent'
        report["fake_input"] = {"ascii_utf8_exact": True, "quick_direct_input": True,
                                "native_ime_tap_and_send": True, "unicode_input": True,
                                "editor_closed_on_accept": True, "unfinished_draft_not_sent": True,
                                "submitted_once": True, "ctrl_c_received": True,
                                "real_shell_process_started": False}

        # Select the second session, then verify server-side closure returns to the first.
        await page.locator("#sessions").click()
        await page.locator(f'{fixture_rows}[data-session="{second.id}"]').click()
        await wait_page(page, "sid=>MobileTerminal.active===sid&&MobileTerminal.ready", arg=second.id, timeout=6000)
        daemon.hub.close_session(second.id)
        await wait_page(page, "sid=>MobileTerminal.active===sid&&MobileTerminal.ready", arg=first.id, timeout=6000)
        await page.locator("#sessions").click()
        if await page.locator(fixture_rows).count() != 1:
            raise AssertionError("Closed session remains in Android navigation")
        await page.locator("#sessions-close").click()
        daemon.hub.close_window(window.id)
        await wait_page(page, "MobileTerminal.active===''&&MobileTerminal.terminal===null", timeout=6000)
        await page.locator("#sessions").click()
        if await page.locator(fixture_rows).count():
            raise AssertionError("Closed window sessions remain in Android navigation")
        await page.locator("#sessions-close").click()
        report["closure"] = {"active_session_removal_selects_remaining": True, "window_removal_clears_terminal": True}
        report["metrics"] = await page.evaluate("({...MobileTerminal.metrics})")
    finally:
        daemon.hub.detach(desktop.key)


async def probe(args):
    from playwright.async_api import async_playwright
    from websockets.asyncio.server import serve
    from supermonoagent.daemon import Daemon

    output = args.output or ROOT / ".omx/artifacts" / ("android-stream-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    output.mkdir(parents=True, exist_ok=True)
    device = UI.Device(args.adb, args.serial)
    report = {"physical_android": True, "isolated_fake_backend": True, "errors": [], "cleanup": {}}
    forward = reverse = None
    daemon = None
    page = None
    temporary_host = "USB 渲染测试 " + datetime.now().strftime("%H%M%S%f")
    saved = {}
    profile_added = False
    try:
        device.choose()
        with tempfile.TemporaryDirectory(prefix="android-stream-only-") as temporary, patch.dict(
            os.environ, {"SUPERMONO_STATE_DIR": temporary}
        ), patch("supermonoagent.daemon.STATE", Path(temporary)):
            daemon = Daemon("android-stream-test-only")
            daemon.hub.persist = False
            daemon.hub.pty_factory = lambda *_: FakePty()
            daemon.hub.attach_loop(asyncio.get_running_loop())
            window = daemon.hub.open_window(owner=daemon.token)
            first = daemon.hub.open_cli(window.id, "cmd")
            second = daemon.hub.open_cli(window.id, "powershell")
            async with serve(daemon.handle_socket, "127.0.0.1", 0, process_request=daemon.serve_http) as server:
                port = server.sockets[0].getsockname()[1]
                reverse = "tcp:" + str(port)
                device.run("reverse", reverse, reverse)
                device.run("shell", "am", "start", "-f", "0x20000000", "-n", args.package + "/.MainActivity", "--ez", "ui_probe", "true")
                pid = device.run("shell", "pidof", args.package).split()[0]
                names = re.findall(r"@([^\s]*webview_devtools_remote[^\s]*)", device.run("shell", "cat", "/proc/net/unix"))
                names = [name for name in names if name.endswith("_" + pid)]
                if len(names) != 1:
                    raise RuntimeError("Could not uniquely identify this app's WebView debugging socket")
                forward = device.run("forward", "tcp:0", "localabstract:" + names[0]).splitlines()[-1]
                if not forward.isdigit():
                    raise RuntimeError("ADB forward did not return a numeric port")
                async with async_playwright() as runtime:
                    browser = await runtime.chromium.connect_over_cdp("http://127.0.0.1:" + forward, timeout=12000)
                    for context in browser.contexts:
                        for candidate in context.pages:
                            if await candidate.evaluate("typeof MobileTerminal!=='undefined'"):
                                page = candidate
                                break
                        if page:
                            break
                    if page is None:
                        raise RuntimeError("Open the Android terminal page before running this test")
                    # Permit only this actual packaged document's origin on the isolated
                    # loopback fixture; retain the daemon's normal WebSocket origin checks.
                    daemon.allowed_origins=(await page.evaluate('location.origin'),)
                    saved = await page.evaluate("""() => ({
                      sizing:localStorage.getItem('sma.mobile.sizing'),
                      effectiveSizing:document.getElementById('sizing').value,
                      draft:document.getElementById('draft').value,
                      composerHidden:document.getElementById('composer').tagName==='DIALOG'?!document.getElementById('composer').open:document.getElementById('composer').hidden,
                      dialogs:[...document.querySelectorAll('dialog[open]')].map(d=>d.id)
                    })""")
                    try:
                        await page.evaluate("document.querySelectorAll('dialog[open]').forEach(d=>d.close())")
                        await page.locator("#sessions").click()
                        await page.locator("#host-add").click()
                        await page.locator("#host-name").fill(temporary_host)
                        await page.locator("#host-endpoint").fill(f"http://127.0.0.1:{port}")
                        await page.locator("#host-code").fill("")
                        await page.locator("#host-token").fill(daemon.token)
                        await page.locator('#host-form button[type="submit"]').click()
                        profile_added = await page.get_by_role("button", name="移除电脑 " + temporary_host, exact=True, include_hidden=True).count() == 1
                        if not profile_added:
                            raise AssertionError("Android did not create the isolated test host profile")
                        def record_error(error):
                            report["errors"].append(UI.safe_error(error))
                        page.on("pageerror", record_error)
                        try:
                            await exercise(page, device, daemon, first, second, window, output, report)
                        except Exception:
                            report['state_at_failure']=await page.evaluate("({active:MobileTerminal.active,ready:MobileTerminal.ready,hosts:MobileTerminal.hosts,status:document.getElementById('status').textContent,notice:document.getElementById('notice').textContent})")
                            raise
                        finally:
                            page.remove_listener("pageerror", record_error)
                    finally:
                        # Restore settings while still on the fake host, before returning home.
                        try:
                            await page.evaluate("document.querySelectorAll('dialog[open]').forEach(d=>d.close())")
                            await page.locator("#options").click()
                            await page.locator("#sizing").select_option(saved["effectiveSizing"])
                            await page.locator("#settings-close").click()
                            if saved["sizing"] is None:
                                await page.evaluate("localStorage.removeItem('sma.mobile.sizing')")
                            report["cleanup"]["sizing_restored"] = True
                        except Exception as error:
                            report["errors"].append("sizing restore: " + UI.safe_error(error))
                        try:
                            if await page.get_by_role("button", name="移除电脑 " + temporary_host, exact=True, include_hidden=True).count():
                                await page.evaluate("document.querySelectorAll('dialog[open]').forEach(d=>d.close())")
                                await page.locator("#sessions").click()
                                # Match the generated name exactly; preserve all pre-existing profiles.
                                remove = page.get_by_role("button", name="移除电脑 " + temporary_host, exact=True)
                                await remove.click()
                                await page.locator("#sessions-close").click()
                                report["cleanup"]["temporary_profile_removed"] = True
                                await wait_page(page, "MobileTerminal.connected", timeout=15000)
                                report["cleanup"]["home_reconnected"] = True
                                if not saved["composerHidden"] and await page.locator("#composer").evaluate("e=>e.tagName==='DIALOG'?!e.open:e.hidden"):
                                    await UI.toggle_mode_without_focus(page)
                                await page.evaluate("draft=>{document.getElementById('draft').value=draft}", saved["draft"])
                        except Exception as error:
                            report["errors"].append("profile restore: " + UI.safe_error(error))
                        try:
                            for dialog in saved["dialogs"]:
                                await page.evaluate("id=>{const d=document.getElementById(id);if(d&&!d.open)d.showModal()}", dialog)
                        except Exception as error:
                            report["errors"].append("dialog restore: " + UI.safe_error(error))
                    # Do not call browser.close(): this is the user's physical app.
    except Exception as error:
        report["errors"].append(UI.safe_error(error))
    finally:
        if daemon:
            for window_id in list(daemon.hub.windows):
                daemon.hub.close_window(window_id)
        if reverse:
            device.run("reverse", "--remove", reverse, check=False)
        if forward and forward.isdigit():
            device.run("forward", "--remove", "tcp:" + forward, check=False)
        report["cleanup"]["adb_mappings_removed"] = True
        report["passed"] = bool(report.get("closure")) and not report["errors"] and all(report["cleanup"].values())
        (output / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"passed": report["passed"], "errors": report["errors"], "output": str(output)}, ensure_ascii=False))
    return 0 if report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", type=Path, default=ROOT / ".omx/tools/android-sdk/platform-tools/adb.exe")
    parser.add_argument("--serial")
    parser.add_argument("--package", default="dev.supermonoagent.mobile")
    parser.add_argument("--output", type=Path)
    return asyncio.run(probe(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
