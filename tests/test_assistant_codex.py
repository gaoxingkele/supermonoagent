"""No real authentication or cloud requests; owned CLI capability evidence."""
import json
import base64
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import threading
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from supermonoagent.assistant_codex import (
    CodexCapabilityError, CodexCancelledError, query,
)
from supermonoagent import assistant_codex as engine


def _safe_config():
    return {"config": {"mcp_servers": {}, "plugins": {}, "marketplaces": {},
            "features": {name: False for name in engine._FEATURES},
            "web_search": "disabled", "project_doc_max_bytes": 0,
            "skills": {"include_instructions": False}},
            "layers": [{"name": {"type": "system"}, "config": {}}]}


@pytest.mark.parametrize("mutation", [
    lambda x: x["layers"].append({"name": {"type": "unknown"}}),
    lambda x: x["config"].update(mcp_servers={"unsafe": {}}),
    lambda x: x["config"].update(plugins={"unsafe": {}}),
    lambda x: x["config"]["features"].update(shell_tool=True),
    lambda x: x["config"]["features"].pop("shell_tool"),
    lambda x: x["config"].update(model_catalog_json="unexpected.json"),
    lambda x: x["config"].update(web_search="live"),
])
def test_unverifiable_configuration_rejected(mutation):
    config = _safe_config()
    mutation(config)
    with pytest.raises(CodexCapabilityError):
        engine._check_config(config)


def test_unknown_version_rejected_before_thread(monkeypatch):
    methods = []
    class Client:
        def __init__(self, *args, **kwargs): pass
        def rpc(self, method, params):
            methods.append(method)
            return {"userAgent": "supermono-readonly-assistant/999.0 (unknown)"}
        def close(self): methods.append("close")
    monkeypatch.setattr(engine, "_Client", Client)
    monkeypatch.setattr(shutil, "which", lambda _: sys.executable)
    with pytest.raises(CodexCapabilityError, match="version"):
        query("UNTRUSTED_CONTEXT")
    assert methods == ["initialize", "close"]


@pytest.mark.parametrize("cancelled", [True, False])
def test_owned_process_cancel_timeout_and_cleanup(tmp_path, cancelled):
    cancel = threading.Event()
    client = engine._Client([sys.executable, "-c", "import time; time.sleep(60)"],
                            tmp_path, time.monotonic() + .2, cancel)
    if cancelled:
        cancel.set()
    try:
        with pytest.raises(CodexCancelledError if cancelled else engine.CodexError):
            client.receive()
    finally:
        client.close()
    assert client.process.poll() is not None
    assert not client.reader.is_alive()


def test_owned_output_limit_is_sanitized(tmp_path):
    client = engine._Client([sys.executable, "-c", "print('SECRET_' * 1000000)"],
                            tmp_path, time.monotonic() + 5, None)
    try:
        with pytest.raises(engine.CodexError) as error:
            client.receive()
        assert "SECRET_" not in str(error.value)
    finally:
        client.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows private job cleanup")
def test_private_job_kills_only_owned_child_tree(tmp_path):
    import ctypes
    from ctypes import wintypes
    code = "import subprocess,sys,json,time; child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); print(json.dumps({'pid':child.pid}),flush=True); time.sleep(60)"
    client = engine._Client([sys.executable, "-c", code], tmp_path, time.monotonic() + 5, None)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = None
    try:
        pid = client.receive()["pid"]
        handle = kernel.OpenProcess(0x100000, False, pid)
        assert handle and kernel.WaitForSingleObject(handle, 0) == 258
        client.close()
        assert kernel.WaitForSingleObject(handle, 3000) == 0
    finally:
        if client.process.poll() is None:
            client.close()
        if handle:
            kernel.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows private TLS setup")
@pytest.mark.parametrize("cancelled", [True, False])
def test_tls_setup_respects_owner_timeout_and_cancel(monkeypatch, cancelled):
    monkeypatch.setattr(shutil, "which", lambda _: sys.executable)
    cancel = threading.Event()
    timer = threading.Timer(.05, cancel.set) if cancelled else None
    if timer:
        timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(CodexCancelledError if cancelled else engine.CodexError):
            query("owned fixture", timeout=5 if cancelled else .05, cancel=cancel)
    finally:
        if timer:
            timer.cancel()
            timer.join()
    assert time.monotonic() - started < 3


def test_missing_native_cli_fails_closed_without_accessing_credentials(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Fail-closed query must not create processes or inspect credentials")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(CodexCapabilityError, match="Native Codex") as error:
        query("UNTRUSTED_SECRET_WINDOW_TEXT", model="user-selected-model")
    assert "UNTRUSTED_SECRET_WINDOW_TEXT" not in str(error.value)


def test_owner_cancellation():
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(CodexCancelledError):
        query("window context", cancel=cancel)


@pytest.mark.parametrize("kwargs", [
    {"prompt": ""}, {"prompt": None}, {"prompt": "x" * 65537},
    {"model": "bad\nmodel"}, {"model": 123}, {"timeout": 91},
    {"timeout": 0}, {"timeout": float("inf")}, {"timeout": float("nan")},
    {"timeout": True}, {"cancel": object()},
])
def test_invalid_arguments(kwargs):
    with pytest.raises(ValueError):
        query(**({"prompt": "context"} | kwargs))


@pytest.mark.parametrize("mode", ["inventory_empty", "inventory_mcp", "query_empty", "query_mcp", "query_oauth", "query_race"])
def test_real_cli_owned_profile_tool_inventory(tmp_path, mode, monkeypatch):
    """An empty MCP override does not remove a configured server.

    All profiles, provider responses, prompt and subprocesses belong to this
    test. Proxy configuration rejects any attempt to reach an external origin.
    This evidence intentionally does NOT authorize production inference.
    """
    cli = shutil.which("codex")
    configured_mcp = mode.endswith("mcp")
    if not cli or Path(cli).suffix.lower() in {".cmd", ".bat", ".ps1"}:
        pytest.skip("Native Codex executable required")
    requests = []
    external = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_CONNECT(self):
            external.append(True)
            self.send_error(503)

        def do_GET(self):
            if self.path == "/backend-api/wham/accounts/check":
                body = json.dumps({"accounts": [{"id": "OWNED_ACCOUNT", "plan_type": "plus", "structure": "personal",
                    "workspace_backend_origin": "https://chatgpt.com", "account_routing_override": "NO_CONSTRAINT"}],
                    "account_ordering": ["OWNED_ACCOUNT"], "default_account_id": "OWNED_ACCOUNT"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                return
            if not self.path.startswith("/"):
                external.append(True)
                self.send_error(503)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"data":[]}')

        def do_POST(self):
            assert self.path == "/v1/responses", self.path
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            events = [
                {"type": "response.created", "response": {"id": "owned", "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0, "item": {"type": "message", "id": "owned-message", "role": "assistant", "content": []}},
                {"type": "response.content_part.added", "item_id": "owned-message", "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": ""}},
                {"type": "response.output_text.delta", "item_id": "owned-message", "output_index": 0, "content_index": 0, "delta": "OWNED_LOCAL_ANSWER"},
                {"type": "response.output_item.done", "output_index": 0, "item": {"type": "message", "id": "owned-message", "role": "assistant", "content": [{"type": "output_text", "text": "OWNED_LOCAL_ANSWER"}]}},
                {"type": "response.completed", "response": {"id": "owned", "status": "completed", "output": [{"type": "message", "id": "owned-message", "role": "assistant", "content": [{"type": "output_text", "text": "OWNED_LOCAL_ANSWER"}]}],
                    "usage": {"input_tokens": 1, "output_tokens": 0, "total_tokens": 1}}},
            ]
            for event in events:
                self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home = tmp_path / "codex-home"
    work = tmp_path / "empty-work"
    home.mkdir()
    work.mkdir()
    url = f"http://127.0.0.1:{server.server_port}"
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "SYSTEMDRIVE", "PROGRAMFILES", "PROGRAMFILES(X86)"}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    env.update(CODEX_HOME=str(home), HOME=str(tmp_path), USERPROFILE=str(tmp_path),
               APPDATA=str(tmp_path), LOCALAPPDATA=str(tmp_path), HTTP_PROXY=url,
               HTTPS_PROXY=url, ALL_PROXY=url, NO_PROXY="127.0.0.1,localhost")
    run_options = {"env": env, "cwd": work, "capture_output": True, "text": True, "timeout": 30}
    if os.name == "nt":
        run_options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        version = subprocess.run([cli, "--version"], **run_options)
        if version.stdout.strip() != "codex-cli 0.164.0":
            pytest.skip("Capability evidence is pinned to installed CLI 0.164.0")
        features = subprocess.run([cli, "features", "list"], **run_options)
        assert features.returncode == 0
        if mode.startswith("query"):
            gateway_type = engine.ResponsesGateway
            gateways = []
            def owned_gateway(deadline, cancel):
                instance = gateway_type(deadline, cancel, _test_upstream=url + "/v1/responses")
                gateways.append((instance, instance._tls_folder.name))
                assert not (Path(instance._tls_folder.name) / "server-key.pem").exists()
                return instance
            monkeypatch.setattr(engine, "ResponsesGateway", owned_gateway)
            config = ('model="gpt-5.4"\nmodel_provider="fixture"\n'
                      '[model_providers.fixture]\nname="Owned fixture"\n'
                      'base_url=' + json.dumps(url + '/v1') + '\n'
                      'wire_api="responses"\nrequires_openai_auth=false\n')
            if configured_mcp:
                config += '[mcp_servers.owned]\ncommand="python"\nargs=["-c", "print(123)"]\n'
            (home / "config.toml").write_text(config, encoding="utf8")
            (home / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "OWNED_FAKE_AUTH_NO_CLOUD"}), encoding="utf8")
            if mode == "query_oauth":
                def encode(value):
                    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
                token = encode({"alg": "none"}) + "." + encode({"exp": int(time.time()) + 86400,
                    "email": "owned@example.invalid", "https://api.openai.com/auth": {
                    "chatgpt_account_id": "OWNED_ACCOUNT", "chatgpt_plan_type": "plus"}}) + ".owned"
                (home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {
                    "id_token": token, "access_token": token, "refresh_token": "OWNED_FAKE_REFRESH",
                    "account_id": "OWNED_ACCOUNT"}, "last_refresh": "2099-01-01T00:00:00Z"}), encoding="utf8")
            original_auth = (home / "auth.json").read_bytes()
            original_config = (home / "config.toml").read_bytes()
            startup_marker = tmp_path / "owned-mcp-started"
            if mode == "query_race":
                original_rpc = engine._Client.rpc
                def racing_rpc(client, method, params):
                    if method != "thread/start":
                        return original_rpc(client, method, params)
                    # Deterministic ABA race: thread creation sees an MCP,
                    # both configuration snapshots see the original empty map.
                    command = "from pathlib import Path; Path(" + repr(str(startup_marker)) + ").write_text('owned'); print(123)"
                    replacement = original_config.decode() + '\n[mcp_servers.owned_race]\ncommand=' + json.dumps(sys.executable) + '\nargs=["-c",' + json.dumps(command) + ']\n'
                    (home / "config.toml").write_bytes(replacement.encode())
                    try:
                        return original_rpc(client, method, params)
                    finally:
                        (home / "config.toml").write_bytes(original_config)
                monkeypatch.setattr(engine._Client, "rpc", racing_rpc)
            with patch.dict(os.environ, env, clear=True):
                if mode == "query_race":
                    with pytest.raises(CodexCapabilityError, match="gateway rejected"):
                        query("OWNED_LOCAL_FIXTURE", model="gpt-5.4", timeout=20)
                    assert requests == []
                    assert startup_marker.read_text() == "owned"
                elif configured_mcp:
                    with pytest.raises(CodexCapabilityError, match="MCP/plugins"):
                        query("OWNED_LOCAL_FIXTURE", model="gpt-5.4", timeout=20)
                    assert requests == []
                else:
                    assert query("OWNED_LOCAL_FIXTURE", model="gpt-5.4", timeout=20) == "OWNED_LOCAL_ANSWER"
                    assert len(requests) == 1
                    assert requests[0].get("tools", []) == []
                assert "CODEX_CA_CERTIFICATE" not in os.environ
            assert (home / "auth.json").read_bytes() == original_auth
            assert (home / "config.toml").read_bytes() == original_config
            assert all(not Path(folder).exists() for _, folder in gateways)
            return
        command = [cli, "--no-daemon", "exec", "--ignore-rules", "--ephemeral",
                   "--skip-git-repo-check", "--sandbox", "read-only", "--json", "-C", str(work), "-m", "gpt-5.4"]
        if configured_mcp:
            # Harmless server exits immediately; only resource-list tools remain.
            (home / "config.toml").write_text('[mcp_servers.owned]\ncommand="python"\nargs=["-c", "print(123)"]\n', encoding="utf8")
        else:
            command.append("--ignore-user-config")
        for line in features.stdout.splitlines():
            parts = line.split()
            if parts and "removed" not in parts and parts[-1] == "true":
                command.extend(["--disable", parts[0]])
        for setting in [
            'mcp_servers={}', 'model_provider="fixture"',
            'model_providers.fixture.name="Owned fixture"',
            'model_providers.fixture.base_url=' + json.dumps(url + '/v1'),
            'model_providers.fixture.wire_api="responses"',
            'model_providers.fixture.requires_openai_auth=false',
            'web_search="disabled"', 'tools.update_plan.enabled=false',
            'tools.experimental_request_user_input.enabled=false',
            'skills.include_instructions=false', 'project_doc_max_bytes=0',
            'check_for_update_on_startup=false', 'analytics.enabled=false',
        ]:
            command.extend(["-c", setting])
        result = subprocess.run(command, input="OWNED_LOCAL_FIXTURE", **run_options)
        assert result.returncode == 0, "Owned Codex capability fixture failed"
        assert len(requests) == 1
        # CLI telemetry/update probes, if attempted, were rejected by our proxy;
        # the only permitted HTTP request body was the owned local Responses call.
        tools = requests[0].get("tools", [])
        names = [tool.get("name", tool.get("type")) for tool in tools]
        if configured_mcp:
            assert "read_mcp_resource" in names
        else:
            assert tools == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()
