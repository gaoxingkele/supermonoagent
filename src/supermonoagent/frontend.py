"""Serve current UI assets while existing daemons keep ownership of their PTYs."""

from __future__ import annotations

import argparse
import asyncio
import codecs
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from . import web
from .security import request_allowed, tls_context, transport_options, FIRST_FRAME_S
from .desktop import health, port_open
from .hub import TAB_THEMES
from .protocol import ProtocolError, OP_HELLO, OP_GROUP, OP_SESSIONS, OP_INPUT_SESSION, pack, pack_json, unpack, unpack_json
from .pty import ConptyInputFilter


def revision() -> str:
    from .workspace import PAGE
    digest = hashlib.sha256(("frontend-v1" + web.PAGE + PAGE).encode())
    for name, _content_type in web.STATIC.values():
        digest.update(web.static_text(name).encode())
    return digest.hexdigest()[:16]


class Frontend:
    def __init__(self, backend: str, theme_file: Path | None = None):
        parsed = urlsplit(backend)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("The local frontend requires a loopback HTTP daemon URL")
        self.backend = backend.rstrip("/")
        self.version = revision()
        key = hashlib.sha256(self.backend.encode()).hexdigest()[:12]
        self.theme_file = theme_file or Path.home() / ".supermonoagent" / f"frontend-themes-{key}.json"
        try:
            saved = json.loads(self.theme_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        self.themes = {key: value for key, value in saved.items() if isinstance(value, str) and value in TAB_THEMES} if isinstance(saved, dict) else {}
        self.views: dict = {}

    async def serve_http(self, connection, request):
        if not request_allowed(connection, request, getattr(self, 'allowed_origins', ())):
            return web.text_response(connection, 403, 'Forbidden', 'text/plain')
        path = request.path.split("?", 1)[0]
        if path == "/health":
            try:
                status = await asyncio.to_thread(health, self.backend)
            except (OSError, ValueError):
                return web.text_response(connection, 503, '{"ok":false}', "application/json")
            status = {"ok": bool(status.get("ok")), "role": "frontend", "backend": self.backend, "revision": self.version}
            return web.text_response(connection, 200, json.dumps(status), "application/json")
        content = web.page_content(path)
        if content:
            text, content_type = content
            return web.text_response(connection, 200, text, content_type)
        if path != "/ws":
            return web.text_response(connection, 404, "not found", "text/plain")
        return None

    def themed_state(self, state):
        # New daemons publish themes themselves; old daemons use a local UI overlay.
        if state.get("capabilities", {}).get("window_themes"):
            return state
        return {**state, "windows": [{**window, "tab_theme": self.themes.get(window["id"], window.get("tab_theme", ""))}
                                     for window in state.get("windows", [])]}

    async def handler(self, client):
        parsed = urlsplit(self.backend)
        url = urlunsplit(('ws', parsed.netloc, '/ws', '', ''))
        try:
            if urlsplit(client.request.path).query:
                raise ValueError('WebSocket query credentials are forbidden')
            first = await asyncio.wait_for(client.recv(), FIRST_FRAME_S)
            if isinstance(first, str) and len(first) <= 1024 * 1024:
                account = json.loads(first)
                if not isinstance(account, dict) or account.get('op') not in {'register', 'login', 'rotate'}:
                    raise ValueError('Invalid local account operation')
                async with connect(url, max_size=1024 * 1024, compression=None) as upstream:
                    await upstream.send(first)
                    await client.send(await asyncio.wait_for(upstream.recv(), FIRST_FRAME_S))
                await client.close()
                return
            if not isinstance(first, bytes) or len(first) > 1024 * 1024:
                raise ValueError('Binary HELLO required')
            op, payload = unpack(first)
            body = unpack_json(payload)
            token = body.get('token')
            if op != OP_HELLO or not isinstance(token, str) or not token or len(token) > 4096:
                raise ValueError('Authenticated HELLO required')
            headers = {'Authorization': 'Bearer ' + token}
            async with connect(url, additional_headers=headers, max_size=1024 * 1024, compression=None) as upstream:
                await upstream.send(first)
                self.views[client] = None
                input_filters = {}
                input_timers = {}

                async def forward_input(session, data, flush=False):
                    parser, decoder = input_filters.setdefault(session, (ConptyInputFilter(), codecs.getincrementaldecoder("utf-8")(errors="replace")))
                    previous = input_timers.pop(session, None)
                    if previous is not None and previous is not asyncio.current_task():
                        previous.cancel()
                    text = decoder.decode(parser.flush_escape() if flush else parser.feed(data))
                    if text:
                        await upstream.send(pack(OP_INPUT_SESSION, session + b"\0" + text.encode("utf-8")))
                    if parser._pending and parser._paste is None:
                        async def flush_later():
                            await asyncio.sleep(0.05)
                            await forward_input(session, b"", flush=True)
                        input_timers[session] = asyncio.create_task(flush_later())

                async def receive():
                    async for message in upstream:
                        if isinstance(message, bytes):
                            op, payload = unpack(message)
                            if op in {2, 7}:
                                state = unpack_json(payload)
                                self.views[client] = state
                                message = pack_json(op, self.themed_state(state))
                        await client.send(message)

                async def transmit():
                    async for message in client:
                        state = self.views.get(client)
                        if isinstance(message, bytes) and state and sys.platform == "win32" and not state.get("capabilities", {}).get("conpty_paste"):
                            op, payload = unpack(message)
                            if op == OP_INPUT_SESSION:
                                session, separator, data = payload.partition(b"\0")
                                visible = {item["id"].encode() for window in state.get("windows", []) for item in window.get("sessions", [])}
                                if separator and session in visible:
                                    await forward_input(session, data)
                                continue
                        if isinstance(message, bytes) and state and not state.get("capabilities", {}).get("window_themes"):
                            op, payload = unpack(message)
                            if op == OP_GROUP:
                                body = unpack_json(payload)
                                if body.get("action") == "tab-theme":
                                    window_id, theme = body.get("window"), body.get("theme")
                                    visible = {window["id"] for window in state.get("windows", [])}
                                    if window_id in visible and isinstance(theme, str) and (not theme or theme in TAB_THEMES):
                                        if theme:
                                            self.themes[window_id] = theme
                                        else:
                                            self.themes.pop(window_id, None)
                                        self.theme_file.parent.mkdir(parents=True, exist_ok=True)
                                        temporary = self.theme_file.with_suffix(".tmp")
                                        temporary.write_text(json.dumps(self.themes), encoding="utf-8")
                                        temporary.replace(self.theme_file)
                                        for view, summary in list(self.views.items()):
                                            if summary is not None:
                                                try:
                                                    await view.send(pack_json(OP_SESSIONS, self.themed_state(summary)))
                                                except Exception:
                                                    pass  # A closing viewer must not disconnect the others.
                                    continue
                        await upstream.send(message)

                tasks = [asyncio.create_task(receive()), asyncio.create_task(transmit())]
                try:
                    done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for timer in input_timers.values():
                        timer.cancel()
                    await asyncio.gather(*input_timers.values(), return_exceptions=True)
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (ValueError, ProtocolError, asyncio.TimeoutError):
            await client.close(code=4001, reason='Authenticated first HELLO required')
        except ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd else 1011
            await client.close(code=code if code in {1000, 1001, 4001, 1013} else 1011,
                               reason='Local daemon connection ended; reconnect to resume')
        except Exception:
            await client.close(code=1011, reason="Local daemon connection ended; reconnect to resume")
        finally:
            self.views.pop(client, None)


def local_page_url(url: str) -> str:
    """Reuse a matching local UI service; never replace an existing terminal daemon."""
    parsed = urlsplit(url)
    backend = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    Frontend(backend)  # Validate before launching a helper process.
    version = revision()
    available = None
    for port in range(6771, 6789):
        if not port_open("127.0.0.1", port):
            if available is None:
                available = port
            continue
        try:
            info = health(f"http://127.0.0.1:{port}")
            if info.get("role") == "frontend" and info.get("backend") == backend and info.get("revision") == version:
                return urlunsplit(("http", f"127.0.0.1:{port}", parsed.path, urlencode({**dict(parse_qsl(parsed.query)), "direct": "1"}), parsed.fragment))
        except (OSError, ValueError):
            continue
    if available is None:
        raise ValueError("No local UI port is available in 6771–6788")
    entry = ["frontend"] if getattr(sys, "frozen", False) else ["-m", "supermonoagent.frontend"]
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    process = subprocess.Popen([sys.executable, *entry, "--backend", backend, "--port", str(available)],
                               creationflags=flags, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(75):
        if port_open("127.0.0.1", available):
            info = health(f"http://127.0.0.1:{available}")
            if info.get("role") == "frontend" and info.get("backend") == backend and info.get("revision") == version:
                return urlunsplit(("http", f"127.0.0.1:{available}", parsed.path, urlencode({**dict(parse_qsl(parsed.query)), "direct": "1"}), parsed.fragment))
        if process.poll() is not None:
            break
        time.sleep(0.1)
    raise ValueError("The local UI service did not start; existing terminal sessions are still running")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", required=True)
    parser.add_argument("--port", type=int, required=True)
    transport_options(parser)
    args = parser.parse_args()
    frontend = Frontend(args.backend)
    frontend.allowed_origins = args.allowed_origin
    context = tls_context(args.tls_cert, args.tls_key)

    async def run():
        async with serve(frontend.handler, "127.0.0.1", args.port, process_request=frontend.serve_http, ssl=context, max_size=1024 * 1024):
            await asyncio.Future()
    asyncio.run(run())


if __name__ == "__main__":
    main()
