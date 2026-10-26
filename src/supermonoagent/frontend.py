"""Serve current UI assets while existing daemons keep ownership of their PTYs."""

from __future__ import annotations

import argparse
import asyncio
import codecs
import hashlib
import hmac
from html import escape
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from . import web
from .defaults import default_update_feed
from .security import request_allowed, tls_context, transport_options, FIRST_FRAME_S
from .desktop import health, port_open
from .hub import TAB_THEMES
from .protocol import ProtocolError, OP_HELLO, OP_GROUP, OP_SESSIONS, OP_INPUT_SESSION, OP_SUBSCRIBE, OP_PING, OP_PONG, OP_SELECT, OP_FOCUS, pack, pack_json, unpack, unpack_json
from .pty import ConptyInputFilter

NATIVE_UPDATE_API = 2
UPDATE_CHECK_SECONDS = 6 * 60 * 60
REPLAY_DRAIN_TIMEOUT = 5.0
FOOTER_RECOVERY_TIMEOUT = .35
UI_FILES = ("workspace.html", "workspace.js", "workspace.css", "terminal-client.js", "tab-themes.js", "tab-themes.css",
            "xterm.js", "xterm.css", "xterm-fit.js", "xterm-unicode11.js", "xterm-webgl.js",
            "agent-modes/codex.json", "agent-modes/claude-code.json", "agent-modes/grok.json",
            "agent-modes/kimi.json", "agent-modes/pi-dev.json", "agent-modes/cursor-cli.json")
STATIC_ROOT = Path(__file__).parent / "static"
WORKSPACE_CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self' ws: wss:; img-src 'self' data:; base-uri 'none'; frame-src 'none'; frame-ancestors 'none'; object-src 'none'"


class _ReplayDrainTimeout(TimeoutError):
    pass


def numeric_version(value):
    if isinstance(value, str) and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', value):
        return tuple(int(part) for part in value.split('.'))
    return None


def native_bundle_version():
    if not getattr(sys, 'frozen', False):
        return None
    try:
        with (Path(sys.executable).resolve().parent.parent / 'version.txt').open(encoding='utf-8-sig') as source:
            value = source.read(65).strip()
        return value if len(value) <= 64 and numeric_version(value) is not None else None
    except (OSError, UnicodeError, ValueError):
        return None


def bundled_ui_files() -> dict[str, bytes]:
    from .workspace import PAGE, javascript
    return {name: PAGE.encode("utf-8") if name == "workspace.html" else javascript(STATIC_ROOT).encode('utf-8') if name == 'workspace.js' else (STATIC_ROOT / name).read_bytes() for name in UI_FILES}


def bundled_ui_fingerprint(files: dict[str, bytes]) -> str:
    digest=hashlib.sha256()
    for name in sorted(files):
        content=files[name]
        digest.update(name.encode('utf-8')+b'\0'+len(content).to_bytes(8,'big')+content)
    return digest.hexdigest()


def inherited_update_floors(base: Path) -> list[dict]:
    from .ui_updates import MAX_MANIFEST, UpdateError
    paths=[base/'state.json',*sorted((base/'native').glob('*/state.json'))]
    result=[]
    for path in paths:
        if not path.is_file():
            continue
        raw=path.read_bytes()
        if len(raw)>MAX_MANIFEST:
            raise UpdateError('Oversized inherited update state')
        try:
            result.append(json.loads(raw))
        except (ValueError,UnicodeError) as exc:
            raise UpdateError('Invalid inherited update state') from exc
    return result


def revision() -> str:
    from .workspace import PAGE
    digest = hashlib.sha256(("frontend-v2-updates" + web.PAGE + PAGE).encode())
    for name, _content_type in web.STATIC.values():
        digest.update(web.static_text(name).encode())
    for path in [Path(__file__), STATIC_ROOT / "native-updates.js", STATIC_ROOT / "update-trust.json", Path(__file__).with_name("ui_updates.py"), Path(__file__).with_name("token_config.py")]:
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


class Frontend:
    def __init__(self, backend: str, theme_file: Path | None = None, *, update_root: Path | None = None,
                 update_feed: str | None = None, update_trust: dict | None = None,
                 default_token_file: Path | None = None, owner_token_file: Path | None = None):
        parsed = urlsplit(backend)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("The local frontend requires a loopback HTTP daemon URL")
        self.backend = backend.rstrip("/")
        self.version = revision()
        self.bundled_version = native_bundle_version()
        key = hashlib.sha256(self.backend.encode()).hexdigest()[:12]
        self.theme_file = theme_file or Path.home() / ".supermonoagent" / f"frontend-themes-{key}.json"
        self.default_token_file = default_token_file
        self.token_config_state = self.theme_file.parent
        self.owner_token_file = owner_token_file or self.token_config_state / "token"
        from .console_snapshot import FooterRecovery
        self.footer_recovery = FooterRecovery(self.theme_file.parent, self.backend)
        try:
            saved = json.loads(self.theme_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        self.themes = {key: value for key, value in saved.items() if isinstance(value, str) and value in TAB_THEMES} if isinstance(saved, dict) else {}
        self.views: dict = {}
        self._footer_slots = asyncio.Semaphore(2)
        self._footer_reads = set()
        from .ui_updates import UpdateStore
        bundled_files = bundled_ui_files()
        self.bundle_fingerprint = bundled_ui_fingerprint(bundled_files)
        native_namespace = update_root is None and theme_file is None
        update_base = Path.home() / ".supermonoagent" / "ui-updates" / "desktop"
        self.update_root = Path(update_root) if update_root is not None else (theme_file.parent / "ui-updates" if theme_file is not None else update_base / "native" / self.bundle_fingerprint)
        self.update_feed = update_feed if update_feed is not None else default_update_feed()
        self.update_store = UpdateStore(self.update_root, "desktop", bundled_files, update_trust, native_api=NATIVE_UPDATE_API)
        if native_namespace:
            self.update_store.inherit_floor(inherited_update_floors(update_base), bundle_fingerprint=self.bundle_fingerprint)
        self.update_store.recover_pending()
        self.update_nonce = secrets.token_urlsafe(32)
        self.update_bridge = (STATIC_ROOT / "native-updates.js").read_bytes()
        self.update_candidate = None
        self.update_envelope = None
        self.update_prepared = None
        self.update_phase = "current"
        self.update_message = "界面已是当前版本。"
        self.update_last_check = 0.0
        self.update_lock = asyncio.Lock()
        self.update_clients = set()
        self.update_windows = {}
        self.update_watchdog = None

    @staticmethod
    def _response(connection, status, body, content_type, *, immutable=False):
        response = connection.respond(status, "")
        response.body = body if isinstance(body, bytes) else body.encode("utf-8")
        for header in ("Content-Length", "Content-Type", "Cache-Control"):
            if header in response.headers:
                del response.headers[header]
        response.headers["Content-Length"] = str(len(response.body))
        response.headers["Content-Type"] = content_type
        response.headers["Cache-Control"] = "public,max-age=31536000,immutable" if immutable else "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @staticmethod
    def _native_origin_allowed(connection, request):
        try:
            if not request_allowed(connection, request):
                return False
            authority = request.headers.get("Host", "")
            parsed = urlsplit("http://" + authority)
            if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
                return False
            scheme = "https" if connection.transport.get_extra_info("ssl_object") else "http"
            return request.headers.get("Origin") == scheme + "://" + authority
        except (ValueError, AttributeError, LookupError):
            return False

    def update_status(self):
        status = self.update_store.status()
        candidate = self.update_candidate
        return {**status, "native_api": NATIVE_UPDATE_API, "phase": self.update_phase, "message": self.update_message,
                "available": {"revision": candidate["revision"], "sequence": candidate["sequence"]} if candidate else None}

    def _start_update_watchdog(self):
        if self.update_watchdog is None or self.update_watchdog.done():
            self.update_watchdog = asyncio.create_task(self._watch_update_boot())

    async def _watch_update_boot(self):
        while True:
            await asyncio.sleep(.25)
            before = await asyncio.to_thread(self.update_store.status)
            if not before.get("pending"):
                continue
            boot = await asyncio.to_thread(self.update_store.begin_boot)
            if boot and time.time() >= boot["deadline"]:
                await asyncio.to_thread(self.update_store.fail, boot["ticket"], "UI startup readiness deadline exceeded")
            after = await asyncio.to_thread(self.update_store.status)
            if before["active_revision"] != after["active_revision"]:
                self.update_phase = "rolled-back"
                self.update_message = "新界面未能启动，已恢复上一版本。"
                await self._update_broadcast({"event": "rollback", "failed_revision": before["active_revision"], **self.update_status()})

    async def close_updates(self):
        if self.update_watchdog is not None:
            self.update_watchdog.cancel()
            await asyncio.gather(self.update_watchdog, return_exceptions=True)

    async def _update_broadcast(self, message):
        if self.update_clients:
            await asyncio.gather(*(client.send(json.dumps(message, ensure_ascii=False)) for client in tuple(self.update_clients)), return_exceptions=True)

    def _workspace_document(self, revision=None, window_id=None):
        from .ui_updates import UpdateError
        requested_revision = revision
        status = self.update_store.status()
        revision = revision or status["active_revision"]
        try:
            snapshot = self.update_store.snapshot(revision)
        except (OSError, UpdateError):
            if requested_revision is not None:
                raise
            self.update_store.recover_pending()
            status = self.update_store.status()
            revision = status["active_revision"]
            snapshot = self.update_store.snapshot(revision)
        boot = self.update_store.begin_boot() if revision == status["active_revision"] else None
        if requested_revision is None and self.update_store.status()["active_revision"] != revision:
            return self._workspace_document(window_id=window_id)
        if boot and boot["revision"] != revision:
            boot = None
        try:
            html = snapshot["workspace.html"].decode("utf-8")
        except UnicodeDecodeError:
            if not boot:
                raise
            self.update_store.fail(boot["ticket"], "Invalid UTF-8 workspace HTML")
            return self._workspace_document(window_id=window_id)
        assets = "|".join(re.escape(name) for name in UI_FILES if name != "workspace.html")
        html = re.sub(r'((?:src|href)\s*=\s*["\'])/?(?:__ui/[A-Za-z0-9._-]+/)?(' + assets + r')((?:[?#][^"\']*)?)(["\'])',
                      lambda match: match[1] + "/__ui/" + revision + "/" + match[2] + self._bundled_asset_query(revision, match[3]) + match[4], html)
        sequence = status.get("active_sequence", 0) if revision == status["active_revision"] else status.get("previous_sequence", 0) if revision == status.get("previous_revision") else 0
        values = {"nonce": self.update_nonce, "revision": revision, "sequence": sequence,
                  "ticket": boot["ticket"] if boot else "", "generation": boot.get("generation", 0) if boot else 0}
        if window_id and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", window_id):
            if window_id not in self.update_windows and len(self.update_windows) >= 128:
                self.update_windows.pop(next(iter(self.update_windows)))
            self.update_windows[window_id] = {"revision": revision, "sequence": values["sequence"], "generation": values["generation"]}
        attributes = " ".join('data-' + name + '="' + escape(str(value), quote=True) + '"' for name, value in values.items())
        bridge = '<script src="/__updates/native-updates.js" ' + attributes + '></script>'
        html = re.sub(r'^\s*<!doctype[^>]*>', '', html, count=1, flags=re.IGNORECASE)
        return "<!doctype html>" + bridge + html

    def _bundled_asset_query(self, revision, suffix):
        if revision != 'bundled':
            return suffix
        path, marker, fragment = suffix.partition('#')
        return path + ('&' if '?' in path else '?') + 'bundle=' + self.bundle_fingerprint + (marker + fragment if marker else '')

    async def _check_update(self, automatic=False):
        from . import ui_updates
        async with self.update_lock:
            if automatic and self.update_last_check and time.monotonic() - self.update_last_check < UPDATE_CHECK_SECONDS:
                return self.update_status()
            self.update_last_check = time.monotonic()
            self.update_phase = "checking"
            self.update_message = "正在检查界面更新…"
            try:
                raw = await asyncio.to_thread(ui_updates.fetch_manifest, self.update_feed)
                candidate = await asyncio.to_thread(ui_updates.verify_envelope, raw, self.update_store.trust, "desktop", native_api=NATIVE_UPDATE_API)
                status = self.update_store.status()
                if candidate["sequence"] in status.get("failed_sequences", []):
                    self.update_candidate = self.update_envelope = None
                    self.update_phase, self.update_message = "current", "失败的界面版本已隔离，等待后续更新。"
                else:
                    bundled_number, candidate_number = numeric_version(self.bundled_version), numeric_version(candidate['revision'])
                    included = candidate_number <= bundled_number if bundled_number is not None and candidate_number is not None else candidate['sequence'] <= status.get('native_bundle_floor', 0)
                    native_current = status.get("active_revision") == "bundled" and included
                    if not native_current or candidate["sequence"] >= status.get("highest_sequence", 0):
                        candidate = await asyncio.to_thread(self.update_store.observe, raw)
                    if native_current or candidate["sequence"] <= status.get("active_sequence", 0):
                        self.update_candidate = self.update_envelope = None
                        self.update_phase, self.update_message = "current", "界面已是当前版本。"
                    else:
                        self.update_candidate, self.update_envelope = candidate, raw
                        self.update_phase, self.update_message = "available", "发现界面更新 " + candidate["revision"] + "；可在方便时下载并更新。"
            except ui_updates.UpdateIncompatible:
                self.update_candidate = self.update_envelope = None
                self.update_phase, self.update_message = "incompatible", "此更新需要新版原生外壳，请先更新 Windows 安装包。"
            except Exception as error:
                self.update_candidate = self.update_envelope = None
                self.update_phase, self.update_message = "offline", "检查更新失败：" + str(error)[:240]
            return self.update_status()

    async def _update_operation(self, message):
        from . import ui_updates
        op = message.get("op")
        if op == "status":
            return self.update_status()
        if op == "check":
            return await self._check_update(message.get("automatic") is True)
        if op == "download":
            async with self.update_lock:
                if not self.update_envelope or not self.update_candidate:
                    raise ValueError("No verified update is available")
                raw, candidate = self.update_envelope, self.update_candidate
                archive = await asyncio.to_thread(ui_updates.fetch_archive, candidate, self.update_feed, "desktop")
                payload = await asyncio.to_thread(ui_updates.verify_envelope, raw, self.update_store.trust, "desktop", native_api=NATIVE_UPDATE_API)
                await asyncio.to_thread(ui_updates.verify_archive, payload, archive, "desktop")
                identifier = secrets.token_urlsafe(24)
                self.update_prepared = {"id": identifier, "envelope": raw, "archive": archive, "revision": candidate["revision"]}
                self.update_phase, self.update_message = "prepared", "更新已验证，等待输入法完成后应用。"
                return {**self.update_status(), "prepared": identifier}
        if op == "activate":
            async with self.update_lock:
                prepared = self.update_prepared
                if not prepared or not hmac.compare_digest(str(message.get("prepared") or ""), prepared["id"]):
                    raise ValueError("Prepared update is no longer available")
                self.update_prepared = None
                await asyncio.to_thread(self.update_store.install, prepared["envelope"], prepared["archive"])
                await asyncio.to_thread(self.update_store.begin_boot)
                self._start_update_watchdog()
                self.update_candidate = self.update_envelope = None
                self.update_phase, self.update_message = "installed", "更新已安装，正在重新加载界面。"
                return {**self.update_status(), "reload": True}
        if op in {"ready", "fail"}:
            boot = await asyncio.to_thread(self.update_store.begin_boot)
            if not boot or boot["ticket"] != message.get("ticket") or boot["revision"] != message.get("revision") or boot.get("generation", 0) != message.get("generation") or time.time() >= boot["deadline"]:
                raise ValueError("Stale UI startup ticket")
            if op == "ready":
                await asyncio.to_thread(self.update_store.acknowledge, boot["ticket"])
                self.update_phase, self.update_message = "current", "界面启动已确认。"
            else:
                await asyncio.to_thread(self.update_store.fail, boot["ticket"], "UI startup failed")
                self.update_phase, self.update_message = "rolled-back", "新界面启动失败，已恢复上一版本。"
                await self._update_broadcast({"event": "rollback", "failed_revision": boot["revision"], **self.update_status()})
            return self.update_status()
        if op == "rollback":
            await asyncio.to_thread(self.update_store.rollback)
            self.update_phase, self.update_message = "rolled-back", "已恢复上一界面版本。"
            return {**self.update_status(), "reload": True}
        raise ValueError("Unknown update operation")

    async def update_handler(self, client):
        self._start_update_watchdog()
        if not self._native_origin_allowed(client, client.request):
            await client.close(code=4001)
            return
        authenticated = False
        try:
            while True:
                raw = await (client.recv() if authenticated else asyncio.wait_for(client.recv(), FIRST_FRAME_S))
                if not isinstance(raw, str) or len(raw) > 16384:
                    await client.close(code=4001)
                    return
                message = json.loads(raw)
                if not isinstance(message, dict) or not hmac.compare_digest(str(message.get("nonce") or ""), self.update_nonce):
                    await client.close(code=4001)
                    return
                authenticated = True
                self.update_clients.add(client)
                try:
                    result = await self._update_operation(message)
                    await client.send(json.dumps({"id": message.get("id"), "ok": True, **result}, ensure_ascii=False))
                except Exception as error:
                    await client.send(json.dumps({"id": message.get("id"), "ok": False, "error": str(error)[:240], **self.update_status()}, ensure_ascii=False))
        except (ConnectionClosed, asyncio.TimeoutError, ValueError, TypeError):
            pass
        finally:
            self.update_clients.discard(client)

    async def serve_http(self, connection, request):
        if not request_allowed(connection, request, getattr(self, 'allowed_origins', ())):
            return web.text_response(connection, 403, 'Forbidden', 'text/plain')
        path = request.path.split("?", 1)[0]
        if path == "/__updates/health":
            state = self.update_store.status()
            window_id = parse_qs(urlsplit(request.path).query).get("window", [""])[0]
            document = self.update_windows.get(window_id, {})
            return self._response(connection, 200, json.dumps({"ok": True, "role": "frontend-ui", "active_revision": state["active_revision"],
                "generation": state.get("generation", 0), "recovery_generation": len(state.get("failed_sequences", [])),
                "failed_sequences": state.get("failed_sequences", []), "document_revision": document.get("revision"), "document_sequence": document.get("sequence")}), "application/json")
        if path == "/__updates/ws":
            if "?" in request.path or not self._native_origin_allowed(connection, request):
                return self._response(connection, 403, "Forbidden", "text/plain")
            return None
        if path == "/__updates/native-updates.js":
            return self._response(connection, 200, self.update_bridge, "text/javascript; charset=utf-8")
        if path in {"/workspace", "/workspace/"}:
            self._start_update_watchdog()
            window_id = parse_qs(urlsplit(request.path).query).get("native_window", [None])[0]
            response = self._response(connection, 200, self._workspace_document(window_id=window_id), "text/html; charset=utf-8")
            response.headers["Content-Security-Policy"] = WORKSPACE_CSP
            return response
        if path.startswith("/__ui/"):
            parts = path.split("/", 3)
            if len(parts) != 4 or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", parts[2]) or parts[3] not in UI_FILES:
                return self._response(connection, 404, "Unknown UI asset", "text/plain")
            try:
                if parts[3] == "workspace.html":
                    body = self._workspace_document(parts[2])
                else:
                    body = self.update_store.snapshot(parts[2])[parts[3]]
            except (KeyError, ValueError, OSError):
                return self._response(connection, 404, "Unknown UI revision", "text/plain")
            content_type = "application/json; charset=utf-8" if parts[3].endswith(".json") else "text/javascript; charset=utf-8" if parts[3].endswith(".js") else "text/css; charset=utf-8" if parts[3].endswith(".css") else "text/html; charset=utf-8"
            response = self._response(connection, 200, body, content_type, immutable=parts[3] != "workspace.html" and parts[2] != "bundled")
            if parts[3] == "workspace.html":
                response.headers["Content-Security-Policy"] = WORKSPACE_CSP
            return response
        if path == "/health":
            try:
                status = await asyncio.to_thread(health, self.backend)
            except (OSError, ValueError):
                return web.text_response(connection, 503, '{"ok":false}', "application/json")
            status = {"ok": bool(status.get("ok")), "role": "frontend", "backend": self.backend, "revision": self.version,
                      "update_feed": self.update_feed, "update_root_id": hashlib.sha256(str(self.update_root.resolve()).encode()).hexdigest()[:16]}
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

    def default_token_state(self, state, authenticated_token, *, owner_authenticated=None):
        """Only the authenticated local owner may read this separate secret."""
        from .token_config import TokenConfigurationError, read_default_token
        service = state.get("service") if isinstance(state.get("service"), dict) else {}
        if "owner_authenticated" in service:
            owner_authenticated = service["owner_authenticated"] is True
        elif owner_authenticated is None:
            try:
                owner = read_default_token(self.owner_token_file)
            except TokenConfigurationError:
                return state
            owner_authenticated = bool(owner) and hmac.compare_digest(authenticated_token.encode("utf-8"), owner.encode("utf-8"))
        if owner_authenticated is not True:
            return state
        try:
            token = read_default_token(self.default_token_file, state_dir=self.token_config_state)
        except TokenConfigurationError as exc:
            return {**state, "service": {**service, "default_relay_token": "", "default_relay_token_error": str(exc),
                                         "default_relay_token_source": "token.txt"}}
        return {**state, "service": {**service, "default_relay_token": token,
                                     "default_relay_token_source": "token.txt"}}

    def _legacy_owner_authenticated(self, authenticated_token):
        """Probe only the configured loopback backend, without redirects/proxies."""
        from .token_config import TokenConfigurationError, read_default_token
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, request, response, code, message, headers, new_url):
                return None
        try:
            opener = build_opener(ProxyHandler({}), NoRedirect())
            with opener.open(self.backend + "/health", timeout=2) as response:
                raw = response.read(16385)
            if len(raw) > 16384:
                raise ValueError("Oversized loopback health response")
            status = json.loads(raw)
            if not isinstance(status, dict) or status.get("ok") is not True:
                raise ValueError("Unhealthy loopback backend")
            if "default_token" in status:
                owner = status["default_token"]
                # A present owner field is authoritative, including malformed/empty denial.
                return (isinstance(owner, str) and 0 < len(owner) <= 4096
                        and hmac.compare_digest(authenticated_token.encode("utf-8"), owner.encode("utf-8")))
        except (OSError, ValueError, UnicodeError):
            pass
        try:
            owner = read_default_token(self.owner_token_file)
        except TokenConfigurationError:
            return False
        return bool(owner) and hmac.compare_digest(authenticated_token.encode("utf-8"), owner.encode("utf-8"))

    async def handler(self, client):
        if client.request.path == "/__updates/ws":
            await self.update_handler(client)
            return
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

                subscribed = set()
                replay_barrier = None
                private_markers = set()
                virtual_view = None
                send_lock = asyncio.Lock()
                frame_generation = {}
                recovery_waiting = {}
                recovery_event = asyncio.Event()

                def catalog_sessions():
                    state = self.views.get(client) or {}
                    return {item['id']: window for window in state.get('windows', [])
                            for item in window.get('sessions', []) if isinstance(item.get('id'), str)}

                async def drain_command(message):
                    nonlocal replay_barrier
                    marker = ('frontend-replay:' + secrets.token_urlsafe(24)).encode('ascii')
                    drained = asyncio.get_running_loop().create_future()
                    replay_barrier = marker, drained
                    private_markers.add(marker)
                    try:
                        async with asyncio.timeout(REPLAY_DRAIN_TIMEOUT):
                            await upstream.send(message)
                            await upstream.send(pack(OP_PING, marker))
                            await drained
                    except asyncio.TimeoutError:
                        raise _ReplayDrainTimeout() from None
                    finally:
                        if replay_barrier is not None and replay_barrier[1] is drained:
                            replay_barrier = None

                async def subscribe_in_order(message, body):
                    nonlocal subscribed, virtual_view
                    sessions = body.get('sessions')
                    if (not isinstance(sessions, list) or len(sessions) > 256
                            or any(not isinstance(sid, str) for sid in sessions)):
                        await upstream.send(message)
                        return
                    visible = catalog_sessions()
                    selected = set(sessions) & visible.keys()
                    if (self.views.get(client) or {}).get('capabilities', {}).get('replay_backpressure') is True:
                        # The updated host schedules replay fairly itself. Its
                        # PONG may precede snapshots which are still deferred.
                        await upstream.send(pack_json(OP_SUBSCRIBE, {**body, 'sessions': [sid for sid in dict.fromkeys(sessions) if sid in selected]}))
                        subscribed = selected
                        return
                    added = list(dict.fromkeys(sid for sid in sessions if sid in selected and sid not in subscribed))
                    # Legacy daemons enqueue an entire replay synchronously.
                    # Grow the replace-style subscription cumulatively, draining
                    # each replay before requesting the next. Retained sessions
                    # stay subscribed and keep their resize leases throughout.
                    retained = subscribed & selected
                    cumulative = [sid for sid in dict.fromkeys(sessions) if sid in retained]
                    clean = {key: value for key, value in body.items() if key != 'refresh'}
                    if not added:
                        await drain_command(pack_json(OP_SUBSCRIBE, {**clean, 'sessions': cumulative}))
                        subscribed = selected
                    for sid in added:
                        if sid not in catalog_sessions():
                            continue
                        cumulative.append(sid)
                        await drain_command(pack_json(OP_SUBSCRIBE, {**clean, 'sessions': list(cumulative)}))
                        subscribed = set(cumulative)
                    if body.get('refresh') is not True:
                        return
                    # SELECT/FOCUS modify only this upstream client's view. They
                    # don't release subscriptions/resize leases or change the
                    # window's shared focused session. Hide temporary navigation.
                    original = self.views.get(client) or {}
                    virtual_view = {key: original.get(key, '') for key in ('selected', 'session')}
                    selected_window = None
                    for sid in dict.fromkeys(sessions):
                        window = catalog_sessions().get(sid)
                        if sid not in subscribed or window is None:
                            continue
                        if window['id'] != selected_window:
                            await drain_command(pack_json(OP_SELECT, {'window': window['id']}))
                            selected_window = window['id']
                        # SELECT returns the default focused session; FOCUS is
                        # needed for other sessions in the same authorized window.
                        focused = window.get('focused') or next((s['id'] for s in window.get('sessions', [])), None)
                        if sid != focused and sid in catalog_sessions():
                            await drain_command(pack_json(OP_FOCUS, {'session': sid}))
                    window = catalog_sessions().get(virtual_view['session'])
                    if window is not None and window['id'] == virtual_view['selected']:
                        await drain_command(pack_json(OP_SELECT, {'window': window['id']}))
                        await drain_command(pack_json(OP_FOCUS, {'session': virtual_view['session']}))
                        virtual_view = None

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
                    owner_checked = False
                    owner_authenticated = False
                    async for message in upstream:
                        op = None
                        if isinstance(message, bytes):
                            op, payload = unpack(message)
                            if op == OP_PONG and payload in private_markers:
                                private_markers.discard(payload)
                                if replay_barrier is not None and payload == replay_barrier[0] and not replay_barrier[1].done():
                                    replay_barrier[1].set_result(None)
                                continue
                            if op in {2, 7}:
                                state = unpack_json(payload)
                                if virtual_view is not None:
                                    state = {**state, **virtual_view}
                                service = state.get("service") if isinstance(state.get("service"), dict) else {}
                                if "owner_authenticated" in service:
                                    owner_checked = True
                                    owner_authenticated = service["owner_authenticated"] is True
                                elif op == 2 and not owner_checked:
                                    # Catalog-only updates cannot establish a legacy owner.
                                    owner_authenticated = await asyncio.to_thread(self._legacy_owner_authenticated, token)
                                    owner_checked = True
                                self.views[client] = state
                                message = pack_json(op, self.default_token_state(self.themed_state(state), token, owner_authenticated=owner_authenticated))
                            elif op in {3, 8}:
                                sid, separator, _raw = payload.partition(b'\0')
                                known = sid.decode('utf-8', 'replace') in catalog_sessions()
                                if separator and known and (sid in frame_generation or len(frame_generation) < 512):
                                    frame_generation[sid] = frame_generation.get(sid, 0) + 1
                                    recovery_waiting.pop(sid, None)
                                    if op == 8 and len(recovery_waiting) < 64:
                                        recovery_waiting[sid] = frame_generation[sid]
                        async with send_lock:
                            await client.send(message)
                        if op in {2, 7, 8}:
                            recovery_event.set()

                def footer_signature(sid):
                    current = self.views.get(client) or {}
                    name = sid.decode('utf-8', 'replace')
                    window = catalog_sessions().get(name)
                    grid = current.get('controls', {}).get(name)
                    if window is None or not isinstance(grid, dict):
                        return None
                    return window['id'], name, grid.get('cols'), grid.get('rows'), token

                async def recover_footer():
                    # Optional native console reads never hold up upstream
                    # draining. At most two reads across this facade may run;
                    # a timed-out read retains its slot until the thread ends.
                    while True:
                        await recovery_event.wait()
                        recovery_event.clear()
                        for sid, generation in tuple(recovery_waiting.items()):
                            signature = footer_signature(sid)
                            if signature is None:
                                if sid.decode('utf-8', 'replace') not in catalog_sessions():
                                    recovery_waiting.pop(sid, None)
                                continue
                            if recovery_waiting.get(sid) != generation:
                                continue
                            recovery_waiting.pop(sid, None)
                            current = self.views.get(client) or {}
                            try:
                                await asyncio.wait_for(self._footer_slots.acquire(), FOOTER_RECOVERY_TIMEOUT)
                            except asyncio.TimeoutError:
                                return
                            job = asyncio.create_task(asyncio.to_thread(
                                self.footer_recovery.supplement, pack(8, sid+b'\0'), current))
                            self._footer_reads.add(job)
                            def finished(done):
                                self._footer_reads.discard(done)
                                self._footer_slots.release()
                                if not done.cancelled():
                                    done.exception()
                            job.add_done_callback(finished)
                            try:
                                supplement = await asyncio.wait_for(asyncio.shield(job), FOOTER_RECOVERY_TIMEOUT)
                            except asyncio.TimeoutError:
                                return  # Don't accumulate threads for a stuck OS reader.
                            except Exception:
                                continue
                            if not isinstance(supplement, bytes):
                                continue
                            try:
                                opcode, data = unpack(supplement)
                                if opcode != 3 or not data.startswith(sid+b'\0'):
                                    continue
                            except ProtocolError:
                                continue
                            async with send_lock:
                                if (frame_generation.get(sid) == generation
                                        and footer_signature(sid) == signature):
                                    await client.send(supplement)

                async def transmit():
                    nonlocal virtual_view
                    async for message in client:
                        state = self.views.get(client)
                        if isinstance(message, bytes) and state:
                            op, payload = unpack(message)
                            if op == OP_SUBSCRIBE:
                                await subscribe_in_order(message, unpack_json(payload))
                                continue
                            if op in {OP_SELECT, OP_FOCUS}:
                                virtual_view = None
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

                tasks = [asyncio.create_task(receive()), asyncio.create_task(transmit()),
                         asyncio.create_task(client.wait_closed())]
                recovery_task = asyncio.create_task(recover_footer())
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
                    recovery_task.cancel()
                    await asyncio.gather(*tasks, recovery_task, return_exceptions=True)
        except _ReplayDrainTimeout:
            await client.close(code=1011, reason='Local replay drain timed out; reconnect to resume')
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


def local_page_url(url: str, *, update_feed: str | None = None, update_root: str | None = None) -> str:
    """Reuse a matching local UI service; never replace an existing terminal daemon."""
    parsed = urlsplit(url)
    backend = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    parsed_backend = urlsplit(backend)
    if parsed_backend.scheme != "http" or parsed_backend.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed_backend.username or parsed_backend.password:
        raise ValueError("The local frontend requires a loopback HTTP daemon URL")
    version = revision()
    available = None
    for port in range(6771, 6789):
        if not port_open("127.0.0.1", port):
            if available is None:
                available = port
            continue
        try:
            info = health(f"http://127.0.0.1:{port}")
            if (info.get("role") == "frontend" and info.get("backend") == backend and info.get("revision") == version
                    and (update_feed is None or info.get("update_feed") == update_feed)
                    and (update_root is None or info.get("update_root_id") == hashlib.sha256(str(Path(update_root).resolve()).encode()).hexdigest()[:16])):
                return urlunsplit(("http", f"127.0.0.1:{port}", parsed.path, urlencode({**dict(parse_qsl(parsed.query)), "direct": "1"}), parsed.fragment))
        except (OSError, ValueError):
            continue
    if available is None:
        raise ValueError("No local UI port is available in 6771–6788")
    entry = ["frontend"] if getattr(sys, "frozen", False) else ["-m", "supermonoagent.frontend"]
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    command = [sys.executable, *entry, "--backend", backend, "--port", str(available)]
    if update_feed is not None:
        command.extend(["--update-feed", update_feed])
    if update_root is not None:
        command.extend(["--ui-update-dir", update_root])
    process = subprocess.Popen(command,
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
    parser.add_argument("--update-feed", default=None, help="Independent HTTPS signed UI update manifest")
    parser.add_argument("--ui-update-dir", default=None, help="Verified UI snapshots and rollback state")
    transport_options(parser)
    args = parser.parse_args()
    frontend = Frontend(args.backend, update_root=Path(args.ui_update_dir) if args.ui_update_dir else None, update_feed=args.update_feed)
    frontend.allowed_origins = args.allowed_origin
    context = tls_context(args.tls_cert, args.tls_key)

    async def run():
        try:
            async with serve(frontend.handler, "127.0.0.1", args.port, process_request=frontend.serve_http, ssl=context, max_size=1024 * 1024):
                await asyncio.Future()
        finally:
            await frontend.close_updates()
    asyncio.run(run())


if __name__ == "__main__":
    main()
