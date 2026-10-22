"""The real mobile composer must submit /quit to a new isolated Codex ConPTY.

Only Codex's local /quit command is sent. No model request or user CLI is used.
"""
import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from urllib.parse import quote

import pytest

ROOT = Path(__file__).resolve().parents[1]
EDGE = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")


@pytest.mark.parametrize("action", ["send", "enter", "android-beforeinput", "android-229", "coalesced-send", "lost-mode-send"])
def test_mobile_composer_submits_to_real_codex(action):
    if sys.platform != "win32" or not EDGE.exists() or not shutil.which("codex"):
        pytest.skip("Real Windows Codex ConPTY and hidden Edge are required")
    playwright = pytest.importorskip("playwright.async_api")
    asyncio.run(check_composer(playwright, action))


async def check_composer(playwright, action):
    from websockets.asyncio.server import serve
    from supermonoagent.daemon import Daemon
    from supermonoagent.pty import WinPty
    from supermonoagent import web

    class NegotiatingPty(WinPty):
        def __init__(self, *args, **kwargs):
            self.test_inputs = []
            self.delivery_lock = threading.Lock()
            self.held = None
            self.release_timer = None
            super().__init__(*args, **kwargs)

        def write(self, data):
            self.test_inputs.append((time.monotonic(), bytes(data)))
            if action == "coalesced-send":
                with self.delivery_lock:
                    if self.held is not None:
                        self.held.extend(data)
                        return
                    if b"/quit" in data:
                        self.held = bytearray(data)
                        self.release_timer = threading.Timer(.6, self.release_held)
                        self.release_timer.start()
                        return
            return super().write(data)

        def release_held(self):
            with self.delivery_lock:
                data = bytes(self.held)
                self.held = None
            # Model an application that drains its input after a scheduling stall:
            # writes arriving 200 ms apart can be consumed in the same paste burst.
            super().write(data)

        def read(self, size=4096):
            self.reader = threading.current_thread()
            data = super().read(size)
            # Match the isolated console tests: satisfy ConPTY startup negotiation.
            # These terminal replies are not composer keystrokes or shell commands.
            if b"\x1b[c" in data:
                self.write(b"\x1b[?1;2c")
            if b"\x1b[6n" in data:
                self.write(b"\x1b[1;1R")
            if b"Continue anyway? [y/N]:" in data:
                self.write(b"y\r")
            return data

        def close(self):
            if self.release_timer:
                self.release_timer.cancel()
            super().close()
            # pywinpty's reader socket can stay blocked after a clean /quit.
            # Shut down only this test-owned socket before its event loop exits.
            try:
                self._proc.fileobj.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._proc.fileobj.close()
            self._proc._server.close()

    with tempfile.TemporaryDirectory(prefix="android-composer-only-") as temporary, patch.dict(
        os.environ, {"SUPERMONO_STATE_DIR": temporary}
    ), patch("supermonoagent.daemon.STATE", Path(temporary)):
        command = [shutil.which("codex"), "--no-daemon", "--no-alt-screen", "--disable", "hooks",
                   "-c", "notify=[]", "-c", f"projects.{json.dumps(temporary)}.trust_level=\"trusted\"", "-C", temporary]
        daemon = Daemon("isolated-composer-token")
        daemon.hub.persist = False
        daemon.hub.pty_factory = lambda *_: NegotiatingPty(command, temporary, 100, 28)
        daemon.hub.attach_loop(asyncio.get_running_loop())
        window = daemon.hub.open_window(owner=daemon.token)
        session = daemon.hub.open_cli(window.id, "powershell", temporary)

        async def http(connection, request):
            path = request.path.split("?", 1)[0]
            if path.startswith("/mobile/"):
                name = path[len("/mobile/"):]
                asset = ROOT / "src/supermonoagent/static" / name[7:] if name.startswith("vendor/") else ROOT / "android/assets/mobile" / name
                if asset.is_file():
                    mime = "text/html" if asset.suffix == ".html" else "text/css" if asset.suffix == ".css" else "text/javascript"
                    return web.text_response(connection, 200, asset.read_text(encoding="utf-8"), mime + "; charset=utf-8")
            return await daemon.serve_http(connection, request)

        try:
            async with serve(daemon.handle_socket, "127.0.0.1", 0, process_request=http) as server:
                port = server.sockets[0].getsockname()[1]
                config = {"endpoint": f"http://127.0.0.1:{port}", "code": "", "token": daemon.token, "renderer": "dom"}
                async with playwright.async_playwright() as runtime:
                    browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
                    try:
                        page = await browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
                        await page.add_init_script("localStorage.setItem('sma.mobile.sizing','shared')")
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        await page.goto(f"http://127.0.0.1:{port}/mobile/index.html#" + quote(json.dumps(config)))
                        await page.wait_for_function("MobileTerminal.ready", timeout=10000)
                        await page.wait_for_function(r"""() => {
                          const b=MobileTerminal.terminal.buffer.active;
                          return Array.from({length:b.length},(_,i)=>b.getLine(i)?.translateToString()||'').some(line=>/context.*left/i.test(line));
                        }""", timeout=15000)
                        # The first welcome footer can precede Codex input-loop startup.
                        # Wait for its explicit idle notification, not a fixed startup sleep.
                        async with asyncio.timeout(15):
                            while b"]7501;state=idle" not in session.pump.snapshot():
                                await asyncio.sleep(.03)
                        daemon.hub.rename_window(window.id, "手机端")
                        await page.wait_for_function("document.querySelector('#session-list .session[data-name=\"手机端\"]')")
                        if action == "lost-mode-send":
                            # A bounded reconnect snapshot may omit the old ?2004h mode.
                            await page.evaluate("MobileTerminal.terminal.reset()")
                            assert not await page.evaluate("MobileTerminal.terminal.modes.bracketedPasteMode")
                        await page.locator("#mode").click()
                        # Shift+Enter remains a local newline; composition candidate Enter never submits.
                        await page.locator("#draft").fill("candidate")
                        await page.locator("#draft").dispatch_event("compositionstart")
                        await page.locator("#draft").dispatch_event("keydown", {"key": "Enter", "keyCode": 229, "isComposing": True})
                        await page.locator("#draft").evaluate("e=>e.dispatchEvent(new InputEvent('beforeinput',{inputType:'insertLineBreak',isComposing:true,bubbles:true,cancelable:true}))")
                        assert await page.locator("#draft").input_value() == "candidate"
                        await page.locator("#draft").dispatch_event("compositionend")
                        await page.locator("#draft").fill("line one")
                        await page.locator("#draft").press("Shift+Enter")
                        assert await page.locator("#draft").input_value() == "line one\n"
                        await page.locator("#draft").fill("/quit")
                        if action in ("send", "coalesced-send", "lost-mode-send"):
                            await page.locator("#send").click()
                        elif action == "enter":
                            await page.locator("#draft").press("Enter")
                        elif action == "android-229":
                            await page.locator("#draft").dispatch_event("keydown", {"key": "Enter", "keyCode": 229, "isComposing": False})
                        else:
                            await page.locator("#draft").evaluate("e=>e.dispatchEvent(new InputEvent('beforeinput',{inputType:'insertLineBreak',isComposing:false,bubbles:true,cancelable:true}))")
                        def shutdown_acknowledged():
                            output = session.pump.snapshot()
                            return b"Shutting down..." in output or b"]7501;state=clear" in output

                        async def failure(reason):
                            inputs = session.pty.test_inputs[-5:]
                            baseline = inputs[0][0] if inputs else 0
                            return AssertionError({"reason": reason, "shutdown_acknowledged": shutdown_acknowledged(),
                                "process_alive": session.pty.alive(),
                                "input_writes": [(round(at-baseline, 3), repr(data)) for at, data in inputs],
                                "editor": await page.evaluate("""() => {
                                    const b=MobileTerminal.terminal.buffer.active;
                                    return {draft:document.getElementById('draft').value,ready:MobileTerminal.ready,
                                      sendDisabled:document.getElementById('send').disabled,cursor:[b.cursorX,b.cursorY],
                                      lines:Array.from({length:b.length},(_,i)=>b.getLine(i)?.translateToString()||'')};
                                }""")})

                        # The local command must be consumed promptly. Process
                        # shutdown (including MCP cleanup) has a separate bound;
                        # alive() alone does not mean the editor ignored Enter.
                        deadline = asyncio.get_running_loop().time() + 5
                        while not shutdown_acknowledged() and asyncio.get_running_loop().time() < deadline:
                            await asyncio.sleep(.03)
                        if not shutdown_acknowledged():
                            raise await failure("No shutdown acknowledgement; isolated Codex did not consume /quit")
                        shutdown_confirmed = asyncio.get_running_loop().time()
                        deadline = shutdown_confirmed + 15
                        while session.pty.alive() and asyncio.get_running_loop().time() < deadline:
                            await asyncio.sleep(.03)
                        if session.pty.alive():
                            raise await failure("Shutdown acknowledged but isolated Codex process cleanup exceeded 15 seconds")
                        assert not errors, errors
                        paste = next((at for at, data in session.pty.test_inputs if b"/quit" in data), None)
                        enters = [(at, data) for at, data in session.pty.test_inputs if paste is not None and at >= paste and data == b"\r"]
                        assert len(enters) == 1, "Composer must emit exactly one Enter without retry"
                        if action == "coalesced-send":
                            assert enters[0][0] - paste >= .75, "Enter preceded confirmation of the delayed remote paste"
                        artifact = ROOT / ".omx/artifacts/android-composer-submit"
                        artifact.mkdir(parents=True, exist_ok=True)
                        (artifact / ("frontend-" + action + ".json")).write_text(json.dumps({
                            "action": action, "frontend": "actual packaged Android HTML/JS in hidden Chromium",
                            "transport": "real WebSocket through isolated Daemon and production WinPty input adapter",
                            "command": "/quit", "local_only": True, "codex_exited": True,
                            "composition_not_submitted": True, "shift_enter_inserts_local_newline": True,
                            "frontend_errors": [], "real_user_session_touched": False,
                            "enter_count": len(enters), "paste_to_enter_ms": round((enters[0][0] - paste) * 1000),
                            "opaque_title": "手机端", "shell_metadata": "powershell", "explicit_idle_wait": True,
                            "explicit_shutdown_ack": True, "command_ack_timeout_s": 5, "process_cleanup_timeout_s": 15,
                        }, indent=2), encoding="utf-8")
                    finally:
                        await browser.close()
        finally:
            daemon.hub.close_window(window.id)
            reader = getattr(session.pty, "reader", None)
            if reader:
                # close() shuts down this test's reader socket. Wait for the
                # thread itself, rather than abandoning it after three seconds
                # while it can still schedule close_session on this event loop.
                await asyncio.to_thread(reader.join)
                await asyncio.sleep(0)
