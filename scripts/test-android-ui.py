"""Read-only USB WebView layout probe. Never sends terminal input or closes Chrome.

Use .venv/Scripts/python.exe scripts/test-android-ui.py --rotate. The installed APK
must support the ui_probe launch extra. Connection credentials remain in the app.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PNG = b"\x89PNG\r\n\x1a\n"
GEOMETRY = """() => {
  const m=window.MobileTerminal,t=m?.terminal,s=document.getElementById('screen');
  const rect=e=>{if(!e)return null;const r=e.getBoundingClientRect();return {
    x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom,
    clientWidth:e.clientWidth,clientHeight:e.clientHeight,scrollWidth:e.scrollWidth,
    scrollHeight:e.scrollHeight,transform:getComputedStyle(e).transform};};
  const screen=rect(s),terminal=rect(s?.querySelector('.xterm')),
    keys=rect(document.getElementById('keys')),vv=window.visualViewport;
  const raw=m?.controls,candidate=raw?.[m?.active]||raw;
  const sizing=document.getElementById('sizing')?.value;
  const viewportMode=s?.dataset.viewportMode||'overview',readable=viewportMode==='readable';
  const scale=Number(s?.dataset.scale)||1,fontSize=t?.options.fontSize||null;
  const cell=t?._core?._renderService?.dimensions?.css?.cell;
  const controls=candidate&&typeof candidate==='object'?{
    cols:Number(candidate.cols)||null,rows:Number(candidate.rows)||null,
    owner:!!candidate.owner,busy:!!candidate.busy}:null;
  const dialogs=[...document.querySelectorAll('dialog[open]')].map(d=>({
    id:d.id,...rect(d),clipped:d.getBoundingClientRect().left<0||
    d.getBoundingClientRect().top<0||d.getBoundingClientRect().right>innerWidth+1||
    d.getBoundingClientRect().bottom>innerHeight+1||d.scrollWidth>d.clientWidth+1}));
  return {viewport:{width:innerWidth,height:innerHeight,dpr:devicePixelRatio,
    orientation:window.screen.orientation?.type,
    visual:vv?{width:vv.width,height:vv.height,scale:vv.scale,offsetLeft:vv.offsetLeft,
      offsetTop:vv.offsetTop}:null},documentWidth:document.documentElement.scrollWidth,
    revision:String(m?.revision||'unknown').slice(0,100),sizing,viewportMode,
    fontSize,effectiveFontSize:fontSize?fontSize*scale:null,
    pan:s?{left:s.scrollLeft,top:s.scrollTop,width:s.scrollWidth,height:s.scrollHeight}:null,
    connected:!!m?.connected,ready:!!m?.ready,cols:t?.cols||null,rows:t?.rows||null,
    controls,screen,terminal,keys,dialogs,
    violations:[!m?.connected?'terminal_disconnected':null,!m?.ready?'terminal_not_ready':null,
      document.documentElement.scrollWidth>innerWidth+1?'document_overflow':null,
      keys&&keys.bottom>innerHeight+1?'keys_below_viewport':null,
      screen&&(screen.right>innerWidth+1||screen.bottom>innerHeight+1)?'screen_outside_viewport':null,
      !readable&&terminal&&screen&&terminal.right>screen.right+1?'terminal_horizontal_overflow':null,
      !readable&&terminal&&screen&&terminal.bottom>screen.bottom+1?'terminal_vertical_overflow':null,
      readable&&fontSize&&fontSize*scale<fontSize-.5?'readable_font_shrunk':null,
      controls?.cols&&t&&controls.cols!==t.cols?'canonical_columns_mismatch':null,
      controls?.rows&&t&&controls.rows!==t.rows?'canonical_rows_mismatch':null,
      sizing==='phone'&&controls?.owner&&terminal&&screen&&cell&&
        (screen.right-terminal.right>cell.width+18||screen.bottom-terminal.bottom>cell.height+7)?'phone_view_not_filled':null,
      ...dialogs.filter(d=>d.clipped).map(d=>'dialog_clipped:'+d.id)].filter(Boolean)};
}"""


def safe_error(value: object) -> str:
    """Diagnostic messages must not export URL queries or bearer credentials."""
    text = re.sub(r"(?:https?|wss?)://[^\s\"'<>]+", "[url]", str(value))
    text = re.sub(r"(?i)(token|authorization|bearer)([\s:=\"']+)[^\s,;\"'}]+", r"\1 [redacted]", text)
    return text[:500]


class Device:
    def __init__(self, adb: Path, serial: str | None):
        self.adb = str(adb)
        self.serial = serial

    def run(self, *args: str, binary: bool = False, check: bool = True):
        command = [self.adb] + (["-s", self.serial] if self.serial else []) + list(args)
        result = subprocess.run(command, capture_output=True, timeout=25)
        if check and result.returncode:
            raise RuntimeError(f"adb {args[0]} failed: {safe_error(result.stderr.decode('utf-8', 'replace'))}")
        return result.stdout if binary else result.stdout.decode("utf-8", "replace").strip()

    def choose(self):
        if self.serial:
            if self.run("get-state") != "device":
                raise RuntimeError("Selected Android device is not ready")
            return
        devices = [line.split()[0] for line in self.run("devices").splitlines()
                   if len(line.split()) == 2 and line.split()[1] == "device"]
        if len(devices) != 1:
            raise RuntimeError(f"Expected exactly one authorized Android device; found {len(devices)}. Use --serial.")
        self.serial = devices[0]

    def setting(self, name: str) -> str:
        return self.run("shell", "settings", "get", "system", name).splitlines()[-1]

    def set_setting(self, name: str, value: str):
        if value == "null":
            self.run("shell", "settings", "delete", "system", name)
        else:
            self.run("shell", "settings", "put", "system", name, value)

    def screenshot(self, path: Path):
        import time
        for attempt in range(3):
            data = self.run("exec-out", "screencap", "-p", binary=True)
            start = data.find(PNG)
            if start >= 0:
                path.write_bytes(data[start:])
                return
            if attempt < 2:
                time.sleep(.25)
        raise RuntimeError("Android screenshot contained no PNG signature")

    def native_nodes(self):
        # Parse accessibility XML only in memory. Never persist fields or passwords.
        temporary = "/sdcard/sma-native-probe.xml"
        try:
            self.run("shell", "uiautomator", "dump", temporary)
            raw = self.run("exec-out", "cat", temporary)
        finally:
            self.run("shell", "rm", "-f", temporary, check=False)
        start, end = raw.find("<hierarchy"), raw.rfind("</hierarchy>")
        if start < 0 or end < 0:
            raise RuntimeError("Native accessibility hierarchy unavailable")
        return list(ET.fromstring(raw[start:end + len("</hierarchy>")]).iter("node"))

    def native_tap(self, nodes, text: str):
        for node in nodes:
            if node.get("text") != text or node.get("enabled") == "false":
                continue
            bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
            if bounds:
                left, top, right, bottom = map(int, bounds.groups())
                if right > left and bottom > top:
                    self.run("shell", "input", "tap", str((left + right) // 2), str((top + bottom) // 2))
                    return
        raise RuntimeError("Requested native button is not visible: " + text)

    def native_find_and_tap(self, text: str):
        for attempt in range(7):
            nodes = self.native_nodes()
            try:
                self.native_tap(nodes, text)
                return
            except RuntimeError:
                if attempt == 6:
                    raise
                bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", nodes[0].get("bounds", ""))
                if not bounds:
                    raise
                left, top, right, bottom = map(int, bounds.groups())
                x = str((left + right) // 2)
                self.run("shell", "input", "swipe", x, str(top + (bottom - top) * 4 // 5),
                         x, str(top + (bottom - top) // 4), "180")


async def native_connection_test(device, out, credentials):
    result = {"status": "timeout", "restored_connection": False}
    nodes = device.native_nodes()
    fields=[node for node in nodes if node.get('package')=='dev.supermonoagent.mobile' and node.get('class')=='android.widget.EditText']
    if len(fields)!=3 or fields[0].get('text','').rstrip('/')!=credentials['endpoint'].rstrip('/') or fields[1].get('text','')!=credentials['code']:
        raise RuntimeError('Native connection target differs from the explicitly supplied credential file')
    password=fields[2]
    match=re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]',password.get('bounds',''))
    if not match or password.get('password')!='true':
        raise RuntimeError('Native password field is not available')
    left,top,right,bottom=map(int,match.groups())
    device.run('shell','input','tap',str((left+right)//2),str((top+bottom)//2))
    await asyncio.sleep(.3)
    device.run('shell','input','keyevent','KEYCODE_MOVE_END',*(['KEYCODE_DEL']*256))
    # Bulk ADB input dropped a character in this phone's IME. Commit characters
    # individually; credentials never appear in logs, arguments to this script or reports.
    for char in credentials['token']:
        device.run('shell','input','text',char)
        await asyncio.sleep(.04)
    device.run('shell','input','keyevent','KEYCODE_DPAD_RIGHT')
    device.run('shell','am','start','-f','0x20000000','-n','dev.supermonoagent.mobile/.MainActivity','--ez','ui_probe','true','--ez','hide_keyboard','true')
    nodes=device.native_nodes()
    for node in nodes:
        text = node.get("text", "")
        version = re.fullmatch(r"版本 ([\w.\-]+) · 构建 (\d+)", text)
        if version:
            result.update(versionName=version[1], versionCode=int(version[2]))
        if text.startswith("界面版本："):
            result["ui_revision"] = re.sub(r"[^\w.\-]", "_", text.split("：", 1)[1])[:100]
    try:
        device.native_find_and_tap("测试连接")
        deadline = asyncio.get_running_loop().time() + 15
        while asyncio.get_running_loop().time() < deadline:
            nodes = device.native_nodes()
            statuses = [node.get("text", "") for node in nodes]
            if any(text.startswith("测试成功：") for text in statuses):
                result["status"] = "success"
                break
            if any(text.startswith("测试失败：") for text in statuses):
                result["status"] = "failure"
                break
            await asyncio.sleep(.25)
        device.screenshot(out / "native-connection-test-device.png")
    finally:
        device.native_find_and_tap("连接")
        # Dismiss only the optional OS password-manager save/update prompt.
        for node in device.native_nodes():
            if node.get('package')=='com.hihonor.keychain' and node.get('resource-id')=='android:id/button2':
                device.native_tap([node],node.get('text'))
                break
        result["restored_connection"] = True
    return result


async def stable_geometry(page):
    previous = None
    report = None
    # A 250 ms sampling interval catches insets/layout changes without long waits.
    for _ in range(16):
        report = await page.evaluate(GEOMETRY)
        signature = json.dumps({key: report[key] for key in ("viewport", "screen", "terminal", "cols", "rows", "connected", "ready", "controls")}, sort_keys=True)
        if signature == previous and report['connected'] and report['ready']:
            return report
        previous = signature
        await asyncio.sleep(.25)
    return report


async def toggle_mode_without_focus(page):
    await page.evaluate("""() => {
      const draft=document.getElementById('draft'),term=MobileTerminal.terminal;
      const draftFocus=draft.focus,termFocus=term?.focus;
      draft.focus=()=>{};if(term)term.focus=()=>{};
      try{const editor=document.getElementById('composer');document.getElementById(editor.tagName==='DIALOG'&&editor.open?'composer-close':'mode').click();document.activeElement?.blur();}
      finally{draft.focus=draftFocus;if(term)term.focus=termFocus;}
    }""")


async def probe(args):
    from playwright.async_api import async_playwright

    out = args.output or ROOT / ".omx/artifacts" / ("android-device-probe-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    device = Device(args.adb, args.serial)
    device.choose()
    forward = None
    original_settings = {}
    page = None
    original_dialogs = []
    native_requested = False
    report = {"physical_android": True, "terminal_input_sent": False, "screens": [], "errors": []}
    native_credentials=None
    if args.native_connection_test:
        native_credentials=json.loads(args.native_credentials.read_text(encoding='utf-8-sig'))
        if not all(isinstance(native_credentials.get(key),str) for key in ('endpoint','code','token')) or not re.fullmatch(r'[A-Za-z0-9._-]{1,256}',native_credentials['token']):
            raise ValueError('Native test needs an explicit valid development credential file')
    try:
        device.run("shell", "am", "start", "-f", "0x20000000", "-n", args.package + "/.MainActivity", "--ez", "ui_probe", "true")
        nodes = device.native_nodes()
        if any(n.get("text") == "留在终端" for n in nodes):
            device.native_tap(nodes, "留在终端")
        pid = device.run("shell", "pidof", args.package).split()[0]
        expected = "webview_devtools_remote_" + pid
        sockets = device.run("shell", "cat", "/proc/net/unix")
        available = re.findall(r"@([^\s]*webview_devtools_remote[^\s]*)", sockets)
        if expected not in available:
            alternatives = [name for name in available if name.endswith("_" + pid)]
            if not alternatives:
                raise RuntimeError("App WebView debug socket not found; install an APK supporting ui_probe and open its terminal page")
            expected = alternatives[0]
        forward = device.run("forward", "tcp:0", "localabstract:" + expected).splitlines()[-1]
        if not forward.isdigit():
            raise RuntimeError("adb did not return a forwarded TCP port")
        async with async_playwright() as runtime:
            browser = await runtime.chromium.connect_over_cdp("http://127.0.0.1:" + forward, timeout=12000)
            for context in browser.contexts:
                for candidate in context.pages:
                    if await candidate.evaluate("typeof window.MobileTerminal!=='undefined'"):
                        page = candidate
                        break
                if page:
                    break
            if page is None:
                raise RuntimeError("No terminal WebView is open. Connect from the Android home page first, then rerun.")
            touch = await page.context.new_cdp_session(page)
            async def tap(selector):
                locator = page.locator(selector)
                await locator.scroll_into_view_if_needed()
                bounds = await locator.bounding_box()
                point = {"x": bounds["x"] + bounds["width"] / 2, "y": bounds["y"] + bounds["height"] / 2}
                await touch.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [point]})
                await touch.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
            try:
                page.on("pageerror", lambda error: report["errors"].append(safe_error(error)))
                page.on("console", lambda message: report["errors"].append(safe_error(message.text)) if message.type == "error" else None)
                original_dialogs = await page.evaluate("[...document.querySelectorAll('dialog[open]')].map(d=>d.id)")
                original_composer_hidden = await page.locator("#composer").evaluate("e=>e.tagName==='DIALOG'?!e.open:e.hidden")
                if args.keyboard and 'mInputShown=true' in device.run('shell','dumpsys','input_method'):
                    device.run('shell','am','start','-f','0x20000000','-n',args.package+'/.MainActivity','--ez','ui_probe','true','--ez','hide_keyboard','true')
                    await page.evaluate('document.activeElement?.blur()')
                    await asyncio.sleep(.3)
                if args.rotate:
                    for name in ("accelerometer_rotation", "user_rotation"):
                        original_settings[name] = device.setting(name)
                    device.set_setting("accelerometer_rotation", "0")

                async def capture(label):
                    geometry = await stable_geometry(page)
                    geometry["label"] = label
                    report["screens"].append(geometry)
                    await page.screenshot(path=str(out / (label + "-webview.png")))
                    device.screenshot(out / (label + "-device.png"))

                await page.evaluate("document.querySelectorAll('dialog[open]').forEach(d=>d.close())")
                await capture("current")
                if args.keyboard:
                    baseline = await stable_geometry(page)
                    keyboard = {"opened": False, "terminal_input_sent": False}
                    try:
                        if await page.locator("#composer").evaluate("e=>e.tagName==='DIALOG'?!e.open:e.hidden"):
                            await tap("#mode")
                        # Renderer-only CDP touch does not restore native WebView focus
                        # after hide_keyboard cleared it. Tap the actual Android field.
                        draft_nodes=[n for n in device.native_nodes() if n.get('resource-id')=='draft' and n.get('package')==args.package]
                        if len(draft_nodes)!=1:
                            raise RuntimeError('Could not uniquely locate the native Android editor field')
                        bounds=re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]',draft_nodes[0].get('bounds',''))
                        if not bounds:raise RuntimeError('Android editor bounds are unavailable')
                        left,top,right,bottom=map(int,bounds.groups())
                        device.run('shell','input','tap',str((left+right)//2),str((top+bottom)//2))
                        for _ in range(16):
                            await asyncio.sleep(.25)
                            opened = await page.evaluate(GEOMETRY)
                            before = baseline["viewport"]
                            after = opened["viewport"]
                            reduction = max(before["height"] - after["height"],
                                            (before["visual"] or before)["height"] - (after["visual"] or after)["height"])
                            if reduction > 80:
                                keyboard.update(opened=True, height_reduction=reduction)
                                break
                        await capture("keyboard-open")
                    finally:
                        current = await page.evaluate(GEOMETRY)
                        before, after = baseline["viewport"], current["viewport"]
                        reduction = max(before["height"] - after["height"],
                                        (before["visual"] or before)["height"] - (after["visual"] or after)["height"])
                        if reduction > 80:
                            device.run("shell", "am", "start", "-n", args.package + "/.MainActivity",
                                       "--ez", "ui_probe", "true", "--ez", "hide_keyboard", "true")
                            await asyncio.sleep(.25)
                            remaining = await page.evaluate(GEOMETRY)
                            if baseline['viewport']['height'] - remaining['viewport']['height'] > 80:
                                # Compatibility with the first hot-update shell (0.2.4).
                                device.run("shell", "input", "keyevent", "4")
                                await asyncio.sleep(.25)
                                nodes = device.native_nodes()
                                if any(n.get('text') == '留在终端' for n in nodes):
                                    device.native_tap(nodes, '留在终端')
                        await page.locator("#draft").evaluate("e=>e.blur()")
                        if not await page.locator("#composer").evaluate("e=>e.tagName==='DIALOG'?!e.open:e.hidden"):
                            await toggle_mode_without_focus(page)
                        await page.evaluate("document.activeElement?.blur()")
                        await capture("keyboard-hidden-direct")
                        hidden = await page.locator("#composer").evaluate("e=>e.tagName==='DIALOG'?!e.open:e.hidden")
                        if hidden != original_composer_hidden:
                            await toggle_mode_without_focus(page)
                    report["keyboard"] = keyboard
                    if not keyboard["opened"]:
                        report["errors"].append("Native keyboard did not reduce the viewport")
                for rotation in ([0, 1] if args.rotate else [None]):
                    label = "rotation-" + str(rotation) if rotation is not None else "current"
                    if rotation is not None:
                        device.set_setting("user_rotation", str(rotation))
                        await asyncio.sleep(.25)
                        await capture(label)
                    for dialog in ("session-dialog", "host-dialog", "settings-dialog", "copy-dialog"):
                        exists = await page.evaluate("id=>!!document.getElementById(id)", dialog)
                        if not exists:
                            continue
                        await page.evaluate("id=>{document.querySelectorAll('dialog[open]').forEach(d=>d.close());document.getElementById(id).showModal()}", dialog)
                        await capture(label + "-" + dialog)
                    await page.evaluate("document.querySelectorAll('dialog[open]').forEach(d=>d.close())")
            finally:
                try:
                    await page.evaluate("ids=>{document.querySelectorAll('dialog[open]').forEach(d=>d.close());for(const id of ids)document.getElementById(id)?.showModal()}", original_dialogs)
                except Exception as error:
                    report["errors"].append("dialog restore: " + safe_error(error))
            if args.native_connection_test:
                await page.locator("#settings-dialog").evaluate("e=>{document.querySelectorAll('dialog[open]').forEach(d=>d.close());e.showModal()}")
                native_requested = True
                try:
                    await page.evaluate("location.assign('/settings')")
                    for _ in range(20):
                        if page.is_closed():
                            break
                        await asyncio.sleep(.1)
                except Exception:
                    if not page.is_closed():
                        raise
            # Stopping the Playwright transport detaches CDP; do not browser.close(),
            # which could close the user's actual WebView/application.
        if native_requested:
            report["native_connection_test"] = await native_connection_test(device, out, native_credentials)
            if report["native_connection_test"]["status"] != "success":
                report["errors"].append("Native connection test did not report success")
    except Exception as error:
        report["errors"].append(safe_error(error))
    finally:
        for name, value in original_settings.items():
            try:
                device.set_setting(name, value)
            except Exception as error:
                report["errors"].append("rotation restore: " + safe_error(error))
        if forward and forward.isdigit():
            device.run("forward", "--remove", "tcp:" + forward, check=False)
        report["passed"] = bool(report["screens"]) and not report["errors"] and all(not screen["violations"] for screen in report["screens"])
        (out / "geometry.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (out / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"passed": report["passed"], "screens": len(report["screens"]), "errors": report["errors"], "output": str(out)}, ensure_ascii=False))
    return 0 if report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", type=Path, default=ROOT / ".omx/tools/android-sdk/platform-tools/adb.exe")
    parser.add_argument("--serial")
    parser.add_argument("--package", default="dev.supermonoagent.mobile")
    parser.add_argument("--rotate", action="store_true", help="Probe portrait and landscape; restore rotation settings afterwards")
    parser.add_argument("--keyboard", action="store_true", help="Open native IME without typing, check layout, then restore composer mode")
    parser.add_argument("--native-connection-test", action="store_true", help="Test current native connection settings last, capture version, then reconnect")
    parser.add_argument('--native-credentials',type=Path,help='Explicit local credential JSON for native testing; settings clear in-memory credentials')
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.native_connection_test and not args.native_credentials:
        parser.error('--native-connection-test requires --native-credentials so the working connection can be restored safely')
    return asyncio.run(probe(args))


if __name__ == "__main__":
    raise SystemExit(main())
