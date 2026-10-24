"""Windows WebView2 workspace and classic browser launchers sharing one daemon."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import secrets
import subprocess
import sys
import time
import threading
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener, urlopen
import webbrowser

HOST = "127.0.0.1"
PORT = 6769


class NativeUpdateRecovery:
    """Observe the loopback store from Python, even when the renderer JS is hung."""
    def __init__(self, window, url: str, *, interval: float = .5, fetch=None):
        parsed = urlsplit(url)
        query = dict(parse_qsl(parsed.query))
        self.window = window
        self.window_id = query.get("native_window", "")
        query.pop("token", None)
        self.reload_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
        self.health_url = urlunsplit((parsed.scheme, parsed.netloc, "/__updates/health", urlencode({"window": self.window_id}), ""))
        self.interval, self.fetch = interval, fetch or self._fetch
        self.closed = threading.Event()
        self.thread = None
        self.recoveries = 0
        self.interruptions = 0
        self.interruption_error = None
        self.runtime_armed = False
        self.arm_thread = None
        self._arming = threading.Lock()
        self._recovered = set()
        window.events.shown += self.start
        window.events.before_load += self._enable_runtime
        window.events.loaded += self._enable_runtime
        window.events.closed += self.stop

    @staticmethod
    def _fetch(url):
        with build_opener(ProxyHandler({})).open(Request(url, headers={"Cache-Control": "no-store"}), timeout=2) as response:
            return json.load(response)

    def start(self, *_):
        if self.thread is None and self.window_id:
            self._enable_runtime()
            self.thread = threading.Thread(target=self._run, name="native-ui-recovery", daemon=True)
            self.thread.start()

    def _enable_runtime(self, *_):
        # before_load may run on WinForms' UI thread. Never wait there for async CDP.
        if self.runtime_armed or self.closed.is_set() or (self.arm_thread is not None and self.arm_thread.is_alive()):
            return
        self.arm_thread = threading.Thread(target=self._arm_runtime, name="native-ui-runtime", daemon=True)
        self.arm_thread.start()

    def _arm_runtime(self):
        if self.runtime_armed or sys.platform != "win32" or getattr(self.window, "native", None) is None or not self._arming.acquire(False):
            return
        try:
            from System import Action
            pending = []
            control = self.window.native.webview
            control.Invoke(Action(lambda: pending.append(control.CoreWebView2.CallDevToolsProtocolMethodAsync("Runtime.enable", "{}"))))
            self.runtime_armed = bool(pending and pending[0].Wait(2000) and not pending[0].IsFaulted)
        except Exception:
            pass
        finally:
            self._arming.release()

    def _run(self):
        while not self.closed.is_set():
            try:
                status = self.fetch(self.health_url)
                sequence = status.get("document_sequence")
                revision = status.get("document_revision")
                recovery = (sequence, revision)
                if (status.get("ok") is True and status.get("role") == "frontend-ui" and sequence in status.get("failed_sequences", [])
                        and revision != status.get("active_revision") and recovery not in self._recovered):
                    # Native navigation interrupts a wedged renderer; never call evaluate_js here.
                    parsed = urlsplit(self.reload_url)
                    query = dict(parse_qsl(parsed.query))
                    query["ui_recovery"] = secrets.token_hex(8)
                    self._interrupt_renderer()
                    self.window.load_url(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), "")))
                    self._recovered.add(recovery)
                    self.recoveries += 1
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            if self.closed.wait(self.interval):
                return

    def _interrupt_renderer(self):
        """Terminate a failed UI's current JS execution through WebView2, not eval."""
        if sys.platform != "win32" or getattr(self.window, "native", None) is None:
            return
        try:
            from System import Action
            pending = []
            control = self.window.native.webview
            control.Invoke(Action(lambda: pending.append(control.CoreWebView2.CallDevToolsProtocolMethodAsync("Runtime.terminateExecution", "{}"))))
            if pending and pending[0].Wait(2000) and not pending[0].IsFaulted:
                self.interruptions += 1
        except Exception as error:
            # Navigation remains a safe fallback on older native runtimes.
            self.interruption_error = type(error).__name__ + ": " + str(error)[:240]

    def stop(self, *_):
        self.closed.set()
        if self.thread is not None and threading.current_thread() is not self.thread:
            self.thread.join(timeout=3)
        if self.arm_thread is not None and threading.current_thread() is not self.arm_thread:
            self.arm_thread.join(timeout=3)


def port_open(host: str = HOST, port: int = PORT) -> bool:
    try:
        with socket.create_connection((host, port), 0.4):
            return True
    except OSError:
        return False


def host_command(host: str = HOST, port: int = PORT, state_dir: str | None = None, *,
                 relay: str = "", token: str | None = None, token_file: str | None = None, code: str = "", log_window: bool | None = None) -> list[str]:
    entry = ["host"] if getattr(sys, "frozen", False) else ["-m", "supermonoagent"]
    command = [sys.executable, *entry, "--host", host, "--port", str(port)]
    if state_dir:
        command.extend(["--state-dir", state_dir])
    if relay:
        command.extend(["--relay", relay])
    if code:
        command.extend(["--code", code])
    if log_window is not None:
        command.append('--log-window' if log_window else '--no-log-window')
    if token is not None and token_file is not None:
        raise ValueError("Use either --token or --token-file")
    if token is not None:
        command.extend(["--token", token])
    elif token_file is not None:
        command.extend(["--token-file", token_file])
    return command


def start_host(host: str = HOST, port: int = PORT, state_dir: str | None = None, *,
               relay: str = "", token: str | None = None, token_file: str | None = None, code: str = "", log_window: bool | None = None) -> None:
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen(
        host_command(host, port, state_dir, relay=relay, token=token, token_file=token_file, code=code, log_window=log_window), creationflags=flags, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
    )
    for _ in range(75):
        if port_open(host, port):
            return
        if process.poll() is not None:
            break
        time.sleep(0.2)
    raise SystemExit(f"The local daemon did not start at {host}:{port}. Run the host command in a console to inspect the error.")


def page_url(base: str, *, legacy: bool = False, renderer: str = "auto", token: str = "", browser: bool = False, workspace: bool = False) -> str:
    if any(ord(char) < 32 for char in base) or "\\" in base:
        raise ValueError("The URL contains invalid characters")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use an HTTP(S) URL without embedded credentials")
    if parsed.port == 0 or parsed.fragment:
        raise ValueError("Use a valid HTTP(S) port and omit the fragment")
    if renderer not in {"auto", "dom", "webgl"}:
        raise ValueError("Renderer must be auto, dom, or webgl")
    path = parsed.path
    if not path or path == "/":
        path = "/" if legacy or (browser and not workspace) else "/workspace"
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if not browser:
        query["shell"] = "desktop"
        query["renderer"] = renderer
    token = token or query.pop("token", "")
    query.pop("token", None)
    return urlunsplit((parsed.scheme, parsed.netloc, path, urlencode(query), urlencode({"token": token}) if token else ""))


def health(base: str) -> dict:
    with urlopen(base + "/health", timeout=2) as response:
        body = json.load(response)
    if not isinstance(body, dict) or body.get("ok") is not True:
        raise ValueError("The local port is not a healthy SuperMonoAgent daemon")
    return body


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=HOST, help="Local daemon host (default loopback)")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--state-dir", help="State directory when starting a new local daemon")
    endpoint = parser.add_mutually_exclusive_group()
    endpoint.add_argument("--url", help="Remote HTTP(S) daemon/relay URL; never starts a local daemon")
    endpoint.add_argument("--relay", default="", help="Outbound relay URL when starting a new local daemon; running hosts keep their configuration")
    credentials = parser.add_mutually_exclusive_group()
    credentials.add_argument("--token", default=None, help="Owner token for a new daemon or explicit --url; running local hosts keep their credentials")
    credentials.add_argument("--token-file", default=None, help="Private owner token file for a new daemon or explicit --url")
    parser.add_argument("--code", default="", help="Optional new-host room override; otherwise keep its saved room or use this computer's name")
    log_window = parser.add_mutually_exclusive_group()
    log_window.add_argument('--log-window', dest='log_window', action='store_true', default=None, help='Open daemon logs when starting a new host')
    log_window.add_argument('--no-log-window', dest='log_window', action='store_false', help='Keep daemon file logs without a viewer window')
    page_choice = parser.add_mutually_exclusive_group()
    page_choice.add_argument("--legacy", action="store_true", help="Open the original terminal page")
    page_choice.add_argument("--workspace", action="store_true", help="Open the workspace page in browser mode")
    parser.add_argument("--renderer", choices=["auto", "dom", "webgl"], default="auto")
    parser.add_argument("--update-feed", default=None, help="Independent HTTPS signed UI update manifest")
    parser.add_argument("--ui-update-dir", default=None, help="Verified UI snapshots and rollback state")
    parser.add_argument("--no-start", action="store_true", help="Require an already running daemon")
    parser.add_argument("--print-url", action="store_true", help="Print the connection URL without opening a window (may include local token)")
    parser.add_argument("--smoke-test", action="store_true", help="Load and close a hidden WebView2 window to validate the desktop runtime")
    return parser


def connection_url(args: argparse.Namespace, *, browser: bool = False) -> str:
    if args.url:
        token = getattr(args, "token", None) or ""
        if getattr(args, "token_file", None) is not None:
            from .token_config import read_default_token
            token = read_default_token(args.token_file)
            if not token:
                raise ValueError("Token file does not exist or contains no token")
        return page_url(args.url, legacy=args.legacy, renderer=args.renderer, token=token, browser=browser, workspace=args.workspace)
    if args.host not in {"127.0.0.1", "localhost", "::1"} or not 1 <= args.port <= 65535:
        raise ValueError("Automatic startup requires a loopback host and a valid port; use --url for remote servers")
    if not port_open(args.host, args.port):
        if args.no_start:
            raise ValueError("No daemon is listening; start the host or omit --no-start")
        startup = {}
        for name in ("relay", "token", "token_file", "code", "log_window"):
            value = getattr(args, name, None)
            if value is not None and (name in {"token", "token_file"} or value != ""):
                startup[name] = value
        start_host(args.host, args.port, args.state_dir, **startup)
    authority = f"[{args.host}]" if ":" in args.host else args.host
    base = f"http://{authority}:{args.port}"
    status = health(base)
    if args.state_dir or os.environ.get("SUPERMONO_STATE_DIR") or args.port == PORT:
        from .relay_bridge import ensure_running
        bridge_state = Path(args.state_dir or os.environ.get("SUPERMONO_STATE_DIR") or Path.home() / ".supermonoagent")
        ensure_running(base, bridge_state)
    token = str(status.get("default_token") or "")  # Older loopback daemons only.
    if not token and (args.state_dir or os.environ.get("SUPERMONO_STATE_DIR") or args.port == PORT):
        state = Path(args.state_dir or os.environ.get("SUPERMONO_STATE_DIR") or Path.home() / ".supermonoagent")
        try:
            token = (state / "token").read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
    return page_url(base, legacy=args.legacy, renderer=args.renderer,
                    token=token, browser=browser, workspace=args.workspace)


def main(argv: list[str] | None = None, *, browser: bool = False) -> None:
    parser = argument_parser()
    args = parser.parse_args(argv)
    try:
        url = connection_url(args, browser=browser)
        if not args.url:
            from .frontend import local_page_url
            options = {}
            if args.update_feed is not None:
                options["update_feed"] = args.update_feed
            if args.ui_update_dir is not None:
                options["update_root"] = args.ui_update_dir
            url = local_page_url(url, **options)
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from None
    if args.print_url:
        print(url)
        return
    if browser:
        if not webbrowser.open(url):
            raise SystemExit("No default browser could be opened")
        return
    if sys.platform != "win32":
        raise SystemExit("The desktop window requires Windows. Use the browser launcher on this system.")
    try:
        import webview
    except ImportError:
        raise SystemExit('pywebview is missing; install the desktop extra: pip install -e ".[desktop]"') from None
    options = {"hidden": True, "focus": False} if args.smoke_test else {}
    icon = Path(__file__).parent / "static" / "app-icon.ico"
    native_icon = str(icon) if icon.is_file() else None
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SuperMonoAgent.Workspace")
        except (AttributeError, OSError):
            pass
    if not args.url and urlsplit(url).path.rstrip("/") == "/workspace":
        parsed = urlsplit(url)
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode({**dict(parse_qsl(parsed.query)), "native_window": secrets.token_hex(16)}), parsed.fragment))
    window = webview.create_window("SuperMonoAgent Workspace", url, width=1440, height=900,
                                   min_size=(800, 560), background_color="#12161f", **options)
    if not args.url and urlsplit(url).path.rstrip("/") == "/workspace":
        from .desktop_assistant import DesktopAssistant
        from .desktop_files import DesktopFiles
        try:
            assistant = DesktopAssistant(window, url, os.environ.get("SUPERMONO_MYLIB", "D:/aicoding/mylib"))
        except (OSError, ValueError):
            pass
        try:
            clipboard = DesktopFiles(window, url)
        except (OSError, ValueError):
            pass
    recovery = NativeUpdateRecovery(window, url) if not args.url and urlsplit(url).path.rstrip("/") == "/workspace" else None
    if args.smoke_test:
        result = {}

        def verify() -> None:
            try:
                if recovery:
                    recovery.start()
                if not window.events.loaded.wait(25):
                    raise TimeoutError("WebView2 did not load the page")
                state = window.run_js("({ready:document.readyState,workspace:!!document.querySelector('#app')})")
                if isinstance(state, str):
                    state = json.loads(state)
                if not isinstance(state, dict):
                    raise ValueError("WebView2 did not return its native document state")
                result.update(state)
            except Exception as exc:
                result["error"] = str(exc)
            finally:
                if recovery:
                    recovery.stop()
                window.destroy()

        webview.start(verify, gui="edgechromium", icon=native_icon)
        print(json.dumps(result))
        if result.get("ready") != "complete" or (not args.legacy and not result.get("workspace")):
            raise SystemExit("Hidden WebView2 smoke test failed")
    else:
        # Hidden console launchers can also hide the first native window.
        webview.start(window.show, gui="edgechromium", icon=native_icon)


if __name__ == "__main__":
    main()
