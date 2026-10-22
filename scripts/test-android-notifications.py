"""USB notification probe using fake PTY bytes and an isolated room on the existing relay.

Changes the app's connection form to a disposable room. With --credentials the
bundled native password is left untouched; otherwise a random test token is used.
The desktop daemon is never touched. A locked phone is never unlocked.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import queue
import re
import secrets
import tempfile
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from websockets.asyncio.server import serve

from supermonoagent import daemon, hub
from supermonoagent.notification_bridge import NotificationBridge
from supermonoagent.pty import PtyClosed
from supermonoagent.task_events import TaskEventStore

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("android_ui_probe", ROOT / "scripts/test-android-ui.py")
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)
Device = ui.Device
PACKAGE = "dev.supermonoagent.mobile"


class ControlledFakePty:
    def __init__(self):
        self.chunks = queue.Queue()

    def read(self, _):
        data = self.chunks.get()
        if data is None:
            raise PtyClosed()
        return data

    def write(self, _):
        raise AssertionError("Notification probe must never send terminal input")

    def resize(self, *_):
        pass

    def close(self):
        self.chunks.put(None)

    def emit(self):
        self.chunks.put(b"\x1b]7501;app=codex;state=working\x07")
        self.chunks.put(b"\x1b]7501;app=codex;state=idle\x07")


def fixture_notification(raw: str, title: str) -> dict | None:
    """Discard unrelated notifications in memory; never export their contents."""
    for block in re.split(r"(?=^\s*NotificationRecord\()", raw, flags=re.M):
        if (re.search(r"\bpkg=" + re.escape(PACKAGE) + r"\b", block)
                and title in block and "terminal-results" in block):
            return {"package": PACKAGE, "fixture_title": title, "channel": "terminal-results",
                    "notification_found": True}
    return None


def tap_field(device, node, value):
    bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
    if not bounds:
        raise RuntimeError("Native fixture input has no visible bounds")
    left, top, right, bottom = map(int, bounds.groups())
    device.run("shell", "input", "tap", str((left + right) // 2), str((top + bottom) // 2))
    device.run("shell", "input", "keycombination", "113", "29")  # Ctrl+A
    device.run("shell", "input", "keyevent", "KEYCODE_DEL")
    for character in value:
        device.run("shell", "input", "text", character)


def dismiss_password_vault(device, nodes=None):
    """Reject only HONOR's unsolicited credential-save dialog."""
    nodes = device.native_nodes() if nodes is None else nodes
    for node in nodes:
        if (node.get("package") == "com.hihonor.keychain"
                and (node.get("resource-id") == "android:id/button3"
                     or node.get("text") in {"取消", "Cancel"})):
            bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
            if not bounds or node.get("enabled") == "false":
                continue
            left, top, right, bottom = map(int, bounds.groups())
            device.run("shell", "input", "tap", str((left + right) // 2), str((top + bottom) // 2))
            return True
    return False


def notification_action(device, action):
    """Observe every retry: the vault can appear after the first dialog opens."""
    for _ in range(12):
        nodes = device.native_nodes()
        if dismiss_password_vault(device, nodes):
            time.sleep(.2)
            continue
        app_nodes = [node for node in nodes if node.get("package") == PACKAGE]
        if any(node.get("text") == action and node.get("enabled") != "false" for node in app_nodes):
            if action == "启用通知":
                unchecked = [node for node in app_nodes if node.get("class") == "android.widget.CheckBox"
                             and node.get("checked") != "true"]
                if unchecked:
                    device.native_tap(unchecked, unchecked[0].get("text"))
                    continue
            device.native_tap(app_nodes, action)
            return
        # Open the app's notification dialog only when its own control is visible.
        if any(node.get("text") == "任务完成通知" and node.get("class") == "android.widget.Button" for node in app_nodes):
            device.native_tap(app_nodes, "任务完成通知")
        elif app_nodes:
            bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", nodes[0].get("bounds", ""))
            if bounds:
                left, top, right, bottom = map(int, bounds.groups())
                x = str((left + right) // 2)
                device.run("shell", "input", "swipe", x, str(top + (bottom - top) * 4 // 5),
                           x, str(top + (bottom - top) // 4), "180")
        time.sleep(.2)
    raise RuntimeError("Native notification action unavailable after vault-aware retries")


def configure(device, endpoint, code, token, *, keep_native_token=False):
    device.run("shell", "am", "start", "-f", "0x20000000", "-n", PACKAGE + "/.MainActivity",
               "--ez", "ui_probe", "true", "--ez", "show_settings", "true", "--ez", "hide_keyboard", "true")
    fields = [node for node in device.native_nodes()
              if node.get("package") == PACKAGE and node.get("class") == "android.widget.EditText"]
    if len(fields) != 3:
        raise RuntimeError("Native connection form unavailable; unlock the phone manually")
    if keep_native_token and (fields[2].get("password") != "true" or not fields[2].get("text")):
        raise RuntimeError("Bundled native password field is empty; cannot preserve fixture authentication")
    values = (endpoint, code) if keep_native_token else (endpoint, code, token)
    for index, value in enumerate(values):
        fields = [node for node in device.native_nodes()
                  if node.get("package") == PACKAGE and node.get("class") == "android.widget.EditText"]
        if len(fields) != 3:
            raise RuntimeError("Native connection fields changed during fixture setup")
        tap_field(device, fields[index], value)
        device.run("shell", "input", "keyevent", "KEYCODE_BACK")
    # Current APK defaults legacy compatibility on; assert it when dialog appears.
    notification_action(device, "启用通知")
    # Runtime permission may already be granted. Only touch the OS permission dialog.
    for node in device.native_nodes():
        if "permissioncontroller" in node.get("package", "") and node.get("text") in {"允许", "Allow"}:
            device.native_tap([node], node.get("text"))
            break


async def wait_for(predicate, seconds=25):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.1)


async def probe(args):
    out = args.output or ROOT / ".omx/artifacts" / ("android-notifications-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    device = Device(args.adb, args.serial)
    report = {"passed": False, "checks": [], "errors": [], "restoration_required": True}
    tasks, fake, doze = [], ControlledFakePty(), False
    try:
        await asyncio.to_thread(device.choose)
        with tempfile.TemporaryDirectory() as directory:
            token, code = secrets.token_hex(16), "notify-test-" + secrets.token_hex(5)
            endpoint = args.relay.replace("ws://", "http://").replace("wss://", "https://").removesuffix("/ws")
            if args.credentials:
                credentials = json.loads(args.credentials.read_text(encoding="utf8"))
                if str(credentials.get("endpoint", "")).rstrip("/") != endpoint.rstrip("/"):
                    raise ValueError("Explicit credential endpoint differs from the selected fixture relay")
                token = str(credentials.get("token") or "")
                if not token:
                    raise ValueError("Explicit credential file has no owner token")
            with patch.object(daemon, "STATE", Path(directory)), patch.object(
                    hub, "_desktop_path", return_value=Path(directory) / "desktop.json"):
                service = daemon.Daemon(token)
            service.hub.persist = False
            service.hub.attach_loop(asyncio.get_running_loop())
            service.hub.pty_factory = lambda *_: fake
            window = service.hub.open_window("Notification probe", owner=token)
            service.hub.open_cli(window.id, "fake-notification-probe", directory)
            async with serve(service.handle_socket, "127.0.0.1", 0) as local:
                bridge = NotificationBridge(f"http://127.0.0.1:{local.sockets[0].getsockname()[1]}", token, TaskEventStore())
                tasks = [asyncio.create_task(bridge.observe()), asyncio.create_task(bridge.relay(args.relay, code + "-events"))]
                await wait_for(lambda: bridge.connected and bridge.relay_connected and any(
                    client.subscriptions for client in service.hub.clients.values()))
                await asyncio.to_thread(configure, device, endpoint, code, token,
                                        keep_native_token=bool(args.credentials))
                await wait_for(lambda: bool(bridge.subscribers), 60)
                await asyncio.sleep(.5)  # Allow first-enable baseline cursor to arrive.
                phases = ["background", "locked"] + (["doze"] if args.doze else [])
                for phase in phases:
                    if phase == "background":
                        await asyncio.to_thread(device.run, "shell", "input", "keyevent", "KEYCODE_HOME")
                    elif phase == "locked":
                        await asyncio.to_thread(device.run, "shell", "input", "keyevent", "223")
                    else:
                        doze = True
                        await asyncio.to_thread(device.run, "shell", "dumpsys", "deviceidle", "force-idle")
                    title = "NotifyProbe-" + secrets.token_hex(4) + "-" + phase
                    window.title = title
                    service.hub._catalog()
                    await asyncio.sleep(.2)
                    previous = len(bridge.store.records)
                    fake.emit()
                    await wait_for(lambda: len(bridge.store.records) > previous)
                    event = bridge.store.replay(token)[-1]
                    found = None
                    async with asyncio.timeout(40):
                        while not found:
                            raw = await asyncio.to_thread(device.run, "shell", "dumpsys", "notification", "--noredact")
                            found = fixture_notification(raw, title)
                            if not found:
                                await asyncio.sleep(.5)
                    report["checks"].append({"phase": phase, "event_id": event["event_id"], **found})
                report["passed"] = len(report["checks"]) == len(phases)
    except Exception as error:
        report["errors"].append(ui.safe_error(error) or type(error).__name__)
    finally:
        if doze:
            await asyncio.to_thread(device.run, "shell", "dumpsys", "deviceidle", "unforce", check=False)
        await asyncio.to_thread(device.run, "shell", "input", "keyevent", "224", check=False)
        # Never bypass a keyguard. If locked, leave a precise cleanup instruction.
        try:
            await asyncio.to_thread(device.run, "shell", "am", "start", "-f", "0x20000000", "-n", PACKAGE + "/.MainActivity",
                                    "--ez", "ui_probe", "true", "--ez", "show_settings", "true", "--ez", "hide_keyboard", "true")
            await asyncio.to_thread(notification_action, device, "停止通知")
            report["fixture_monitor_stopped"] = True
        except Exception:
            report["fixture_monitor_stopped"] = False
            report["cleanup_note"] = "Unlock manually, stop the fixture task monitor, and restore live connection settings."
        fake.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        (out / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
        print(json.dumps({"passed": report["passed"], "checks": len(report["checks"]), "errors": report["errors"], "output": str(out)}, ensure_ascii=False))
    return 0 if report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--relay", default="ws://117.50.48.22:8817/ws")
    parser.add_argument("--adb", type=Path, default=ROOT / ".omx/tools/android-sdk/platform-tools/adb.exe")
    parser.add_argument("--serial")
    parser.add_argument("--credentials", type=Path, help="Explicit bundled credential JSON: use token only in fixture memory and leave native password untouched")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--doze", action="store_true")
    return asyncio.run(probe(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
