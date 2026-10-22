"""Windows WebView2 workspace and classic browser launchers sharing one daemon."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import urlopen
import webbrowser

HOST = "127.0.0.1"
PORT = 6769


def port_open(host: str = HOST, port: int = PORT) -> bool:
    try:
        with socket.create_connection((host, port), 0.4):
            return True
    except OSError:
        return False


def host_command(host: str = HOST, port: int = PORT, state_dir: str | None = None) -> list[str]:
    entry = ["host"] if getattr(sys, "frozen", False) else ["-m", "supermonoagent"]
    command = [sys.executable, *entry, "--host", host, "--port", str(port)]
    if state_dir:
        command.extend(["--state-dir", state_dir])
    return command


def start_host(host: str = HOST, port: int = PORT, state_dir: str | None = None) -> None:
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen(
        host_command(host, port, state_dir), creationflags=flags, stdin=subprocess.DEVNULL,
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
    parser.add_argument("--url", help="Remote HTTP(S) daemon/relay URL; never starts a local daemon")
    page_choice = parser.add_mutually_exclusive_group()
    page_choice.add_argument("--legacy", action="store_true", help="Open the original terminal page")
    page_choice.add_argument("--workspace", action="store_true", help="Open the workspace page in browser mode")
    parser.add_argument("--renderer", choices=["auto", "dom", "webgl"], default="auto")
    parser.add_argument("--no-start", action="store_true", help="Require an already running daemon")
    parser.add_argument("--print-url", action="store_true", help="Print the connection URL without opening a window (may include local token)")
    parser.add_argument("--smoke-test", action="store_true", help="Load and close a hidden WebView2 window to validate the desktop runtime")
    return parser


def connection_url(args: argparse.Namespace, *, browser: bool = False) -> str:
    if args.url:
        return page_url(args.url, legacy=args.legacy, renderer=args.renderer, browser=browser, workspace=args.workspace)
    if args.host not in {"127.0.0.1", "localhost", "::1"} or not 1 <= args.port <= 65535:
        raise ValueError("Automatic startup requires a loopback host and a valid port; use --url for remote servers")
    if not port_open(args.host, args.port):
        if args.no_start:
            raise ValueError("No daemon is listening; start the host or omit --no-start")
        start_host(args.host, args.port, args.state_dir)
    authority = f"[{args.host}]" if ":" in args.host else args.host
    base = f"http://{authority}:{args.port}"
    status = health(base)
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
            url = local_page_url(url)
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
    window = webview.create_window("SuperMonoAgent Workspace", url, width=1440, height=900,
                                   min_size=(800, 560), background_color="#12161f", **options)
    if args.smoke_test:
        result = {}

        def verify() -> None:
            try:
                if not window.events.loaded.wait(25):
                    raise TimeoutError("WebView2 did not load the page")
                result.update(window.evaluate_js("({ready:document.readyState,workspace:!!document.querySelector('#app')})"))
            except Exception as exc:
                result["error"] = str(exc)
            finally:
                window.destroy()

        webview.start(verify, gui="edgechromium")
        print(json.dumps(result))
        if result.get("ready") != "complete" or (not args.legacy and not result.get("workspace")):
            raise SystemExit("Hidden WebView2 smoke test failed")
    else:
        # Hidden console launchers can also hide the first native window.
        webview.start(window.show, gui="edgechromium")


if __name__ == "__main__":
    main()
