"""Hostile model requests/responses never cross the loopback tools boundary."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

import pytest

from supermonoagent.assistant_codex_gateway import ResponsesGateway, _response_is_text_only


def sse(item=None):
    return ("data: " + json.dumps({"type": "response.completed", "response": {
        "status": "completed", "output": [] if item is None else [item]}}) + "\n\n").encode()


@pytest.mark.parametrize("item", [
    {"type": "function_call", "name": "exec_command", "arguments": "{}"},
    {"type": "custom_tool_call", "name": "apply_patch", "input": "x"},
    {"type": "mcp_call", "name": "send_email"},
    {"type": "web_search_call"},
    {"type": "computer_call"},
    {"type": "message", "role": "assistant", "content": [{"type": "output_image"}]},
])
def test_tool_response_rejected(item):
    assert not _response_is_text_only(sse(item))


@pytest.mark.parametrize("attack", ["request_tool", "response_tool", "redirect", "wrong_nonce", "missing_auth", "missing_marker", "valid"])
def test_gateway_forwards_only_empty_tools_and_buffers_response(attack):
    received = []
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = sse({"type": "function_call", "name": "shell", "arguments": "{}"}) if attack == "response_tool" else sse()
            self.send_response(302 if attack == "redirect" else 200)
            self.send_header("Location", "https://untrusted.invalid/collect")
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    gateway = ResponsesGateway(time.monotonic() + 5, _test_upstream=f"http://127.0.0.1:{upstream.server_port}/responses", _tls=False)
    gateway.mode = "apiKey"
    connection = http.client.HTTPConnection("127.0.0.1", gateway.server.server_port, timeout=3)
    try:
        request = {"model": "owned-fixture", "input": [{"role": "user", "content": [{"type": "input_text", "text": "owned" if attack == "missing_marker" else gateway.marker + " owned"}]}],
                   "tools": [{"type": "function", "name": "shell"}] if attack == "request_tool" else []}
        path = "/wrong/v1/responses" if attack == "wrong_nonce" else f"/{gateway.nonce}/v1/responses"
        headers = {"Content-Type": "application/json"}
        if attack != "missing_auth":
            headers["Authorization"] = "Bearer OWNED_FAKE_ONLY"
        connection.request("POST", path, json.dumps(request), headers)
        response = connection.getresponse()
        body = response.read()
        if attack == "valid":
            assert response.status == 200 and body == sse()
            assert received[0]["tools"] == []
            assert received[0]["tool_choice"] == "none"
            connection.request("POST", path, json.dumps(request), headers)
            replay = connection.getresponse()
            assert replay.status == 502 and replay.read() == b""
        else:
            assert response.status == 502 and body == b""
            assert gateway.rejected.is_set()
        assert len(received) == (0 if attack in {"request_tool", "wrong_nonce", "missing_auth", "missing_marker"} else 1)
    finally:
        connection.close()
        gateway.close()
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)


def test_test_upstream_cannot_be_remote():
    with pytest.raises(ValueError):
        ResponsesGateway(time.monotonic() + 1, _test_upstream="http://remote.invalid/responses")


def test_metadata_auth_is_request_local_and_origin_rewrite_is_ephemeral():
    auth = []
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            auth.append(self.headers.get("Authorization"))
            body = json.dumps({"accounts": [{"id": "owned", "workspace_backend_origin": "https://chatgpt.com"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    gateway = ResponsesGateway(time.monotonic() + 5, _test_upstream=f"http://127.0.0.1:{upstream.server_port}/responses", _tls=False)
    try:
        for caller in ("OWNED_A", "OWNED_B"):
            connection = http.client.HTTPConnection("127.0.0.1", gateway.server.server_port, timeout=3)
            connection.request("GET", "/backend-api/wham/accounts/check", headers={"Authorization": "Bearer " + caller})
            response = connection.getresponse()
            assert response.status == 200
            assert json.loads(response.read())["accounts"][0]["workspace_backend_origin"] == gateway.origin
            connection.close()
        assert auth == ["Bearer OWNED_A", "Bearer OWNED_B"]
        assert gateway.forwarded == 0
    finally:
        gateway.close()
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)
