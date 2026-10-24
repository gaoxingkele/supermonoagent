"""Ephemeral loopback boundary: only text-only, zero-tools Responses traffic.

Authentication headers are forwarded in memory to one fixed HTTPS origin; they
are never logged or persisted. The CLI alone loads its saved authentication.
"""
from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import threading
import time
import os
from pathlib import Path
import base64
import ssl
import subprocess
import tempfile
import socket
from urllib.parse import urlsplit

_UPSTREAMS = {
    "apiKey": "https://api.openai.com/v1/responses",
    "chatgpt": "https://chatgpt.com/backend-api/codex/responses",
}
MAX_BODY = 2 * 1024 * 1024
_EVENTS = frozenset(("response.created response.in_progress response.completed "
    "response.output_item.added response.output_item.done response.content_part.added "
    "response.content_part.done response.output_text.delta response.output_text.done "
    "response.refusal.delta response.refusal.done response.reasoning_summary_part.added "
    "response.reasoning_summary_part.done response.reasoning_summary_text.delta "
    "response.reasoning_summary_text.done response.reasoning_text.delta response.reasoning_text.done").split())


def _tls_context(folder, deadline=None, cancel=None):
    """Generate an hour-lived private CA/server key without changing OS trust."""
    if os.name != "nt":
        raise ValueError("Private gateway TLS currently requires Windows")
    script = r'''
$ErrorActionPreference = 'Stop'
$now = [DateTimeOffset]::UtcNow
$rsa = [System.Security.Cryptography.RSACng]::new(2048)
$sha = [System.Security.Cryptography.HashAlgorithmName]::SHA256
$padding = [System.Security.Cryptography.RSASignaturePadding]::Pkcs1
$rootReq = [System.Security.Cryptography.X509Certificates.CertificateRequest]::new('CN=SuperMono Temporary Assistant CA', $rsa, $sha, $padding)
$rootReq.CertificateExtensions.Add([System.Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($true,$false,0,$true))
$rootReq.CertificateExtensions.Add([System.Security.Cryptography.X509Certificates.X509KeyUsageExtension]::new([System.Security.Cryptography.X509Certificates.X509KeyUsageFlags]::KeyCertSign,$true))
$root = $rootReq.CreateSelfSigned($now.AddMinutes(-1),$now.AddHours(1))
$key = [System.Security.Cryptography.RSACng]::new(2048)
$req = [System.Security.Cryptography.X509Certificates.CertificateRequest]::new('CN=127.0.0.1', $key, $sha, $padding)
$req.CertificateExtensions.Add([System.Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($false,$false,0,$true))
$san = [System.Security.Cryptography.X509Certificates.SubjectAlternativeNameBuilder]::new()
$san.AddIpAddress([System.Net.IPAddress]::Loopback); $req.CertificateExtensions.Add($san.Build())
$serial = New-Object byte[] 16; [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($serial)
$cert = $req.Create($root,$now.AddMinutes(-1),$now.AddMinutes(30),$serial)
$params = $key.ExportParameters($true)
$result = @{ca=[Convert]::ToBase64String($root.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Cert)); cert=[Convert]::ToBase64String($cert.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Cert)); key=@{}}
foreach($name in @('Modulus','Exponent','D','P','Q','DP','DQ','InverseQ')) { $result.key[$name] = [Convert]::ToBase64String($params.$name) }
$result | ConvertTo-Json -Compress
$cert.Dispose(); $root.Dispose(); $key.Dispose(); $rsa.Dispose()
'''
    executable = Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    end = min(deadline or time.monotonic() + 12, time.monotonic() + 12)
    process = subprocess.Popen([str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        while True:
            if time.monotonic() >= end or (cancel is not None and cancel.is_set()):
                raise InterruptedError("Private TLS setup timed out or was cancelled")
            try:
                output, _ = process.communicate(timeout=.05)
                break
            except subprocess.TimeoutExpired:
                pass
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        process.stdout.close()
    if process.returncode or len(output) > 20000:
        raise ValueError("Cannot generate private gateway TLS")
    data = json.loads(output)
    def pem(label, raw):
        value = base64.b64encode(raw).decode()
        return "-----BEGIN " + label + "-----\n" + "\n".join(value[i:i+64] for i in range(0,len(value),64)) + "\n-----END " + label + "-----\n"
    def der(tag, raw):
        size = len(raw)
        length = bytes([size]) if size < 128 else bytes([128 + (size.bit_length()+7)//8]) + size.to_bytes((size.bit_length()+7)//8, "big")
        return bytes([tag]) + length + raw
    values = [b"\0"]
    for name in ("Modulus", "Exponent", "D", "P", "Q", "DP", "DQ", "InverseQ"):
        value = base64.b64decode(data["key"][name]).lstrip(b"\0") or b"\0"
        values.append((b"\0" if value[0] >= 128 else b"") + value)
    private = der(48, b"".join(der(2, value) for value in values))
    ca, cert, key = Path(folder)/"ca.pem", Path(folder)/"server.pem", Path(folder)/"server-key.pem"
    ca.write_text(pem("CERTIFICATE", base64.b64decode(data["ca"])), encoding="ascii")
    cert.write_text(pem("CERTIFICATE", base64.b64decode(data["cert"])), encoding="ascii")
    key.write_text(pem("RSA PRIVATE KEY", private), encoding="ascii")
    os.chmod(key, 0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    key.unlink()
    return context, str(ca)


def _response_is_text_only(body):
    """Validate the entire SSE before any model bytes reach the CLI."""
    completed = False
    try:
        for line in body.decode("utf8").splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                continue
            event = json.loads(data)
            kind = event.get("type", "")
            if kind not in _EVENTS:
                return False
            if kind == "response.completed":
                completed = True
            if any(word in kind.lower() for word in ("tool", "function", "mcp", "computer", "image", "file_search", "web_search")):
                return False
            items = []
            if isinstance(event.get("part"), dict) and event["part"].get("type") not in {"output_text", "refusal", "summary_text"}:
                return False
            if isinstance(event.get("item"), dict):
                items.append(event["item"])
            response = event.get("response")
            if isinstance(response, dict):
                if response.get("tools") not in (None, []):
                    return False
                items.extend(response.get("output", []))
            for item in items:
                if not isinstance(item, dict) or item.get("type") not in {"message", "reasoning"}:
                    return False
                if item.get("type") == "message":
                    if item.get("role") != "assistant":
                        return False
                    if any(part.get("type") not in {"output_text", "refusal"} for part in item.get("content", [])):
                        return False
        return completed
    except (ValueError, TypeError, AttributeError, UnicodeError):
        return False


class ResponsesGateway:
    """A one-query local server. Test override only accepts explicit loopback URLs."""
    def __init__(self, deadline, cancel=None, *, _test_upstream=None, _tls=True):
        self.deadline, self.cancel = deadline, cancel
        self.mode = None
        self.rejected = threading.Event()
        self.forwarded = 0
        self._test_upstream = _test_upstream
        if not _tls and not _test_upstream:
            raise ValueError("Production gateway requires private TLS")
        if _test_upstream is not None:
            parsed = urlsplit(_test_upstream)
            if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.query or parsed.fragment:
                raise ValueError("Test upstream must be loopback")
        self.nonce = secrets.token_urlsafe(32)
        self.marker = "SUPERMONO_ASSISTANT_NONCE:" + self.nonce
        self._forward_lock = threading.Lock()
        self._socket_lock = threading.Lock()
        self._sockets = set()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def setup(self):
                super().setup()
                self.connection.settimeout(max(.1, min(5, owner.deadline - time.monotonic())))
                with owner._socket_lock:
                    owner._sockets.add(self.connection)

            def finish(self):
                try:
                    super().finish()
                finally:
                    with owner._socket_lock:
                        owner._sockets.discard(self.connection)

            def deny(self):
                owner.rejected.set()
                try:
                    self.send_response(502)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except OSError:
                    pass

            def do_GET(self):
                path = urlsplit(self.path)
                if path.path in {f"/{owner.nonce}/backend-api/wham/accounts/check", "/backend-api/wham/accounts/check"} and not path.query:
                    connection = None
                    try:
                        if len(str(self.headers)) > 32768 or not self.headers.get("Authorization") or len(self.headers.get_all("Authorization", [])) != 1:
                            return self.deny()
                        remaining = owner.deadline - time.monotonic()
                        if remaining <= 0:
                            return self.deny()
                        if owner._test_upstream:
                            target = urlsplit(owner._test_upstream)
                            connection = http.client.HTTPConnection(target.hostname, target.port, timeout=min(remaining, 10))
                        else:
                            connection = http.client.HTTPSConnection("chatgpt.com", timeout=min(remaining, 10))
                        headers = {"Authorization": self.headers["Authorization"],
                                   "User-Agent": self.headers.get("User-Agent", "supermono-readonly-assistant/1")}
                        if value := self.headers.get("ChatGPT-Account-ID"):
                            headers["ChatGPT-Account-ID"] = value
                        connection.request("GET", "/backend-api/wham/accounts/check", headers=headers)
                        response = connection.getresponse()
                        body = response.read(MAX_BODY + 1)
                        if response.status != 200 or len(body) > MAX_BODY:
                            return self.deny()
                        metadata = json.loads(body)
                        accounts = metadata.get("accounts")
                        if not isinstance(accounts, list):
                            return self.deny()
                        for account in accounts:
                            if account.get("workspace_backend_origin") != "https://chatgpt.com" or account.get("account_routing_override") not in {None, "NO_CONSTRAINT"}:
                                return self.deny()
                            account["workspace_backend_origin"] = owner.origin
                        body = json.dumps(metadata).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
                        self.deny()
                    finally:
                        if connection is not None:
                            connection.close()
                    return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                connection = None
                transport = None
                try:
                    remaining = owner.deadline - time.monotonic()
                    if remaining <= 0 or (owner.cancel is not None and owner.cancel.is_set()):
                        return self.deny()
                    if self.path not in {f"/{owner.nonce}/v1/responses", f"/{owner.nonce}/backend-api/codex/responses", f"/{owner.nonce}/backend-api/responses", "/backend-api/codex/responses"}:
                        return self.deny()
                    if owner.mode not in _UPSTREAMS:
                        return self.deny()
                    if self.headers.get("Transfer-Encoding") or len(str(self.headers)) > 32768:
                        return self.deny()
                    if len(self.headers.get_all("Content-Length", [])) != 1 or len(self.headers.get_all("Authorization", [])) > 1:
                        return self.deny()
                    if not self.headers.get("Authorization"):
                        return self.deny()
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_BODY:
                        return self.deny()
                    body = self.rfile.read(length)
                    request = json.loads(body)
                    if request.get("tools") not in (None, []) or request.get("tool_choice") not in (None, "none", "auto"):
                        return self.deny()
                    if request.get("previous_response_id") or request.get("conversation") or request.get("background"):
                        return self.deny()
                    if not isinstance(request.get("model"), str) or len(request["model"]) > 256:
                        return self.deny()
                    for item in request.get("input", []):
                        if item.get("type") not in {None, "message"} or item.get("role") not in {"system", "developer", "user", "assistant"}:
                            return self.deny()
                        if any(part.get("type") not in {"input_text", "output_text"} for part in item.get("content", [])):
                            return self.deny()
                    if not any(owner.marker in part.get("text", "") for item in request.get("input", []) if item.get("role") == "user" for part in item.get("content", [])):
                        return self.deny()
                    request["tools"] = []
                    request["tool_choice"] = "none"
                    body = json.dumps(request).encode()
                    upstream = urlsplit(owner._test_upstream or _UPSTREAMS[owner.mode])
                    if owner._test_upstream:
                        connection = http.client.HTTPConnection(upstream.hostname, upstream.port, timeout=remaining)
                    else:
                        connection = http.client.HTTPSConnection(upstream.hostname, upstream.port or 443, timeout=remaining)
                    # No proxy, redirects, cookies or environment-derived endpoints.
                    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
                    for name in ("Authorization", "ChatGPT-Account-ID", "OpenAI-Beta", "OpenAI-Organization", "OpenAI-Project", "User-Agent", "originator", "version"):
                        if value := self.headers.get(name):
                            headers[name] = value
                    with owner._forward_lock:
                        if owner.forwarded:
                            return self.deny()
                        owner.forwarded += 1
                    connection.request("POST", upstream.path, body, headers)
                    transport = connection.sock
                    with owner._socket_lock:
                        owner._sockets.add(transport)
                    response = connection.getresponse()
                    if response.status != 200 or "text/event-stream" not in response.getheader("Content-Type", ""):
                        return self.deny()
                    output = bytearray()
                    while True:
                        if time.monotonic() >= owner.deadline or (owner.cancel is not None and owner.cancel.is_set()):
                            return self.deny()
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        output.extend(chunk)
                        if len(output) > MAX_BODY:
                            return self.deny()
                    if not _response_is_text_only(output):
                        return self.deny()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(output)))
                    self.end_headers()
                    self.wfile.write(output)
                except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
                    self.deny()
                finally:
                    if connection is not None:
                        connection.close()
                    if transport is not None:
                        with owner._socket_lock:
                            owner._sockets.discard(transport)

        self._tls_folder = tempfile.TemporaryDirectory(prefix="supermono-assistant-tls-") if _tls else None
        try:
            context, self.ca_file = _tls_context(self._tls_folder.name, deadline, cancel) if _tls else (None, None)
            class Server(ThreadingHTTPServer):
                def handle_error(self, *_):
                    pass
                def get_request(self):
                    connection, address = super().get_request()
                    connection.settimeout(max(.1, min(1, owner.deadline - time.monotonic())))
                    if context:
                        try:
                            connection = context.wrap_socket(connection, server_side=True)
                        except OSError:
                            connection.close()
                            raise
                    return connection, address
            self.server = Server(("127.0.0.1", 0), Handler)
        except Exception:
            if self._tls_folder:
                self._tls_folder.cleanup()
            raise
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        self.thread.start()
        self.origin = f"{'https' if _tls else 'http'}://127.0.0.1:{self.server.server_port}"
        self.base = self.origin + "/" + self.nonce

    def close(self):
        self.server.shutdown()
        with self._socket_lock:
            sockets = list(self._sockets)
        for connection in sockets:
            try:
                connection.shutdown(socket.SHUT_RDWR)
                connection.close()
            except OSError:
                pass
        self.server.server_close()
        self.thread.join(timeout=1)
        if self._tls_folder:
            self._tls_folder.cleanup()
