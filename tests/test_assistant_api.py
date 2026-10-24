import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time

import pytest

from supermonoagent.assistant_api import AssistantAPIError, MAX_RESPONSE_BYTES, query


MESSAGES = [{"role": "user", "content": "Recommend a skill"}]
ANSWER = {"choices": [{"message": {"role": "assistant", "content": "Try the local skill."}}]}


@contextmanager
def server(body=ANSWER, status=200, headers=None, delay=0, trickle_delay=0,
           trickle_headers=False):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append({"path": self.path, "headers": dict(self.headers),
                             "body": self.rfile.read(int(self.headers["Content-Length"]))})
            if delay:
                time.sleep(delay)
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            try:
                if trickle_headers:
                    head = f"HTTP/1.1 {status} OK\r\nContent-Length: {len(raw)}\r\n\r\n".encode()
                    for byte in head:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(trickle_delay)
                else:
                    self.send_response(status)
                    for key, value in (headers or {}).items():
                        self.send_header(key, value)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                if trickle_delay:
                    for byte in raw:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(trickle_delay)
                else:
                    self.wfile.write(raw)
            except OSError:
                pass

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/v1", received
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("suffix", ["", "/", "/chat/completions", "/chat/completions/"])
def test_success_payload_and_explicit_credentials_only(monkeypatch, suffix):
    monkeypatch.setenv("OPENAI_API_KEY", "environment-secret")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    with server() as (url, received):
        assert query(url + suffix, "test-model", MESSAGES, "fixture-secret") == "Try the local skill."
        assert query(url, "test-model", MESSAGES) == "Try the local skill."
    first, second = received
    assert first["path"] == "/v1/chat/completions"
    assert first["headers"]["Authorization"] == "Bearer fixture-secret"
    assert "Authorization" not in second["headers"]
    assert "Cookie" not in first["headers"]
    assert b"fixture-secret" not in first["body"]
    assert b"environment-secret" not in first["body"]
    assert json.loads(first["body"]) == {
        "model": "test-model", "messages": MESSAGES, "stream": False, "store": False,
        "max_completion_tokens": 1024, "tools": [], "tool_choice": "none"}


@pytest.mark.parametrize("url", [
    "http://example.test/v1", "http://localhost.evil.test/v1", "http://127.1/v1",
    "http://2130706433/v1", "http://[::1%25zone]/v1", "ftp://localhost/v1",
    "https://user:secret@example.test/v1", "https://@example.test/v1",
    "https://example.test/v1?", "https://example.test/v1#", "https://example.test/\\evil",
    "https://example.test/\nfoo", "https://example.test/ foo", "https://example.test:99999/v1",
    "https:///v1", "", None,
])
def test_reject_unsafe_url_before_network(url):
    with pytest.raises(AssistantAPIError, match="Invalid model endpoint"):
        query(url, "model", MESSAGES)


@pytest.mark.parametrize("changes", [
    {"model": ""}, {"model": "x" * 129}, {"model": "bad\nname"},
    {"messages": []}, {"messages": MESSAGES * 15},
    {"messages": [{"role": "tool", "content": "bad"}]},
    {"messages": [{"role": "user", "content": "x" * 32769}]},
    {"messages": [{"role": "user", "content": "\ud800"}]},
    {"messages": [{"role": "user", "content": "x", "tool_calls": []}]},
    {"messages": [{"role": "user", "content": "界" * 32768}] * 2},
    {"api_key": "bad key"}, {"api_key": "secret\nheader"}, {"api_key": "非ascii"},
    {"api_key": "s" * 8193}, {"timeout": True}, {"timeout": 0},
    {"timeout": float("inf")}, {"timeout": 91}, {"timeout": 10 ** 1000},
    {"api_key": "secret", "messages": [{"role": "user", "content": "my secret"}]},
])
def test_invalid_inputs_never_send(changes):
    with server() as (url, received):
        args = {"endpoint": url, "model": "model", "messages": MESSAGES, **changes}
        with pytest.raises(AssistantAPIError):
            query(**args)
        assert received == []


@pytest.mark.parametrize("body", [
    b"not-json", [], {}, {"choices": []}, {"choices": [None]},
    {"choices": [{"message": {"role": "assistant", "content": ""}}]},
    {"choices": [{"message": {"role": "assistant", "content": []}}]},
    {"choices": [{"message": {"role": "assistant", "content": "\ud800"}}]},
    {"choices": [{"message": {"role": "assistant", "content": "x" * 16385}}]},
    {"choices": [{"message": {"role": "user", "content": "bad"}}]},
    {"choices": [{"message": {"role": "assistant", "content": "secret"}}]},
    {"choices": [{"message": {"role": "assistant", "content": "x", "tool_calls": [{}]}}]},
    {"choices": [{"message": {"role": "assistant", "content": "x", "function_call": {"name": "run"}}}]},
    {"choices": [{"message": {"role": "assistant", "content": "x", "refusal": "no"}}]},
    {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": "x"}}]},
])
def test_response_shape_and_tools_rejected(body):
    with server(body) as (url, _):
        with pytest.raises(AssistantAPIError, match="Invalid or unsupported model response"):
            query(url, "model", MESSAGES, "secret")


def test_response_bytes_bounded():
    with server(b"x" * (MAX_RESPONSE_BYTES + 1)) as (url, _):
        with pytest.raises(AssistantAPIError, match="response exceeds size"):
            query(url, "model", MESSAGES)


def test_redirect_never_forwards_authorization():
    with server() as (destination, received):
        with server(b"secret", 307, {"Location": destination}) as (url, _):
            with pytest.raises(AssistantAPIError, match=r"^Model request failed \(HTTP 307\)$"):
                query(url, "model", MESSAGES, "secret")
        assert received == []


def test_errors_do_not_expose_server_body_or_credentials():
    with server(b"secret-provider-internal", 401) as (url, _):
        with pytest.raises(AssistantAPIError) as caught:
            query(url, "model", MESSAGES, "secret")
    assert str(caught.value) == "Model request failed (HTTP 401)"
    assert caught.value.__suppress_context__


def test_timeout_is_bounded_and_sanitized():
    with server(delay=0.4) as (url, _):
        start = time.monotonic()
        with pytest.raises(AssistantAPIError, match="failed or timed out"):
            query(url, "model", MESSAGES, "secret", timeout=0.05)
        assert time.monotonic() - start < 0.35


def test_trickle_cannot_extend_overall_body_deadline():
    with server(trickle_delay=0.015) as (url, received):
        start = time.monotonic()
        with pytest.raises(AssistantAPIError, match="failed or timed out"):
            query(url, "model", MESSAGES, timeout=0.12)
        elapsed = time.monotonic() - start
        assert received
        assert 0.08 <= elapsed < 0.4


def test_header_trickle_cannot_extend_open_deadline():
    with server(trickle_delay=0.015, trickle_headers=True) as (url, received):
        start = time.monotonic()
        with pytest.raises(AssistantAPIError, match="failed or timed out"):
            query(url, "model", MESSAGES, timeout=0.12)
        assert received
        assert 0.08 <= time.monotonic() - start < 0.4


def test_cancel_before_open_sends_nothing():
    cancelled = threading.Event()
    cancelled.set()
    with server() as (url, received):
        with pytest.raises(AssistantAPIError, match="cancelled"):
            query(url, "model", MESSAGES, cancel_event=cancelled)
        assert received == []


def test_cancel_during_trickle_stops_between_chunks():
    cancelled = threading.Event()
    timer = threading.Timer(0.08, cancelled.set)
    with server(trickle_delay=0.015) as (url, _):
        timer.start()
        start = time.monotonic()
        try:
            with pytest.raises(AssistantAPIError, match="cancelled"):
                query(url, "model", MESSAGES, timeout=1, cancel_event=cancelled)
        finally:
            timer.cancel()
        assert time.monotonic() - start < 0.4


def test_verified_tls_and_no_proxy_handler(monkeypatch):
    import ssl
    import supermonoagent.assistant_api as api

    captured = []
    original = api.build_opener

    def checked(*handlers):
        captured.extend(handlers)
        return original(*handlers)

    monkeypatch.setattr(api, "build_opener", checked)
    with server() as (url, _):
        query(url, "model", MESSAGES)
    tls = next(handler for handler in captured if isinstance(handler, api.HTTPSHandler))
    proxy = next(handler for handler in captured if isinstance(handler, api.ProxyHandler))
    assert tls._context.verify_mode == ssl.CERT_REQUIRED
    assert tls._context.check_hostname
    assert proxy.proxies == {}
