"""Real isolated WSS clients. Test-only trust never alters production TLS policy."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
from unittest.mock import patch
from urllib.parse import quote

import pytest
from websockets.asyncio.server import serve
from supermonoagent.daemon import Daemon, relay_uplink
from supermonoagent.relay import Relay
from supermonoagent import web
from test_client_integration import ControlledPty

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "android/src/dev/supermonoagent/mobile"
OPENSSL = shutil.which("openssl") or next((str(p) for p in (
    Path("D:/Espressif/tools/idf-git/2.44.0/mingw64/bin/openssl.exe"),
    Path("D:/KimiData/daimon-bundle/runtime/git/mingw64/bin/openssl.exe")) if p.exists()), None)


@pytest.fixture(scope="module")
def native_tls(tmp_path_factory):
    javac = shutil.which("javac") or next((str(p) for p in (ROOT / ".omx/tools/jdk").glob("*/bin/javac.exe")), None)
    if not javac or not OPENSSL:
        pytest.skip("Local JDK and OpenSSL TLS fixtures required")
    directory = tmp_path_factory.mktemp("wss-clients")
    cert, key, store = [directory / name for name in ("cert.pem", "key.pem", "trust.p12")]
    subprocess.run([OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                    "-out", str(cert), "-days", "2", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
                   check=True, capture_output=True)
    java = str(Path(javac).with_name("java.exe" if javac.endswith(".exe") else "java"))
    keytool = str(Path(java).with_name("keytool.exe" if java.endswith(".exe") else "keytool"))
    subprocess.run([keytool, "-importcert", "-noprompt", "-alias", "loopback-only", "-file", str(cert),
                    "-keystore", str(store), "-storetype", "PKCS12", "-storepass", "fixturepass"], check=True, capture_output=True)
    runner = directory / "WssClient.java"
    runner.write_text(r'''
import dev.supermonoagent.mobile.*;
import java.nio.charset.StandardCharsets;
public final class WssClient {
 public static void main(String[] args) throws Exception {
  if(args[1].equals("probe")) { System.out.write(ConnectionProbe.test(args[0],args[2],"tls-fixture-owner").getBytes(StandardCharsets.UTF_8)); return; }
  try(EventSocket socket=new EventSocket(args[0])) {
   if(!args[2].isEmpty()) {
    socket.json("{\"op\":\"phone\",\"code\":\""+args[2]+"\",\"token\":\"tls-fixture-owner\"}");
    if(!new String(socket.receive().body,StandardCharsets.UTF_8).contains("ready")) throw new Exception("relay not ready");
   }
   socket.protocol(1,"{\"token\":\"tls-fixture-owner\",\"multiplex\":true}");
   for(int n=0;n<8;n++) { EventSocket.Message result=socket.receive();
    if(result.opcode==2 && result.body.length>4 && result.body[4]==2) {System.out.print("verified hello");return;}
   }
   throw new Exception("no hello");
  } catch(javax.net.ssl.SSLException rejected) {System.out.print("TLS rejected");}
 }
}''', encoding="utf8")
    subprocess.run([javac, "-encoding", "UTF-8", "-d", str(directory), str(PACKAGE / "ConnectionEndpoint.java"),
                    str(PACKAGE / "EventSocket.java"), str(PACKAGE / "ConnectionProbe.java"), str(runner)], check=True, capture_output=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cert, key)
    trust = ssl.create_default_context(cafile=str(cert))
    def run(endpoint, mode="events", code="", trusted=True):
        extra = ["-Djavax.net.ssl.trustStore=" + str(store), "-Djavax.net.ssl.trustStorePassword=fixturepass"] if trusted else []
        return subprocess.run([java, *extra, "-cp", str(directory), "WssClient", endpoint, mode, code],
                              check=True, capture_output=True, timeout=20).stdout.decode("utf8")
    return context, trust, run


def test_native_verified_wss_certificate_rejection_and_local_http(native_tls):
    asyncio.run(_native_check(native_tls))


async def _native_check(native_tls):
    context, _, run = native_tls
    seen = []
    async def socket(ws):
        seen.append(ws.request.path)
        await ws.recv()
        from supermonoagent.protocol import pack_json
        await ws.send(pack_json(2, {"capabilities": {"multiplex": True}}))
        await ws.wait_closed()
    async with serve(socket, "127.0.0.1", 0, ssl=context) as server:
        port = server.sockets[0].getsockname()[1]
        endpoint = f"https://localhost:{port}"
        assert await asyncio.to_thread(run, endpoint) == "verified hello"
        assert "测试成功" in await asyncio.to_thread(run, endpoint, "probe")
        assert await asyncio.to_thread(run, endpoint, trusted=False) == "TLS rejected"
        assert await asyncio.to_thread(run, endpoint.replace("localhost", "127.0.0.1")) == "TLS rejected"
        # Connecting HTTP to the TLS-only port fails; no silent plaintext fallback.
        with pytest.raises(subprocess.CalledProcessError):
            await asyncio.to_thread(run, endpoint.replace("https:", "http:"))
        assert seen == ["/ws", "/ws"]
    async with serve(socket, "127.0.0.1", 0) as server:
        assert await asyncio.to_thread(run, f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}") == "verified hello"
    assert all("token" not in path and "tls-fixture-owner" not in path for path in seen)


@pytest.mark.parametrize("encrypted", [True, False])
def test_actual_windows_and_mobile_over_isolated_relay(native_tls, encrypted):
    playwright = pytest.importorskip("playwright.async_api")
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    if not edge.exists(): pytest.skip("Local hidden Edge required")
    asyncio.run(_browser_check(native_tls, encrypted, playwright, edge))


async def _browser_check(native_tls, encrypted, playwright, edge):
    import websockets
    context, trust, run = native_tls
    with tempfile.TemporaryDirectory() as directory, patch("supermonoagent.daemon.STATE", Path(directory)), patch.dict(os.environ, {"SUPERMONO_STATE_DIR": directory}):
        daemon = Daemon("tls-fixture-owner"); daemon.hub.persist = False
        daemon.hub.pty_factory = lambda *_: ControlledPty(); daemon.hub.attach_loop(asyncio.get_running_loop())
        window = daemon.hub.open_window("WSS fixture", owner=daemon.token); daemon.hub.open_cli(window.id, "cmd")
        relay = Relay(development=True)
        requests = []
        async def http(connection, request):
            requests.append(request.path)
            name = request.path.split("?", 1)[0]
            if name.startswith("/mobile/"):
                relative = name[len("/mobile/"):]
                asset = ROOT / "src/supermonoagent/static" / relative[7:] if relative.startswith("vendor/") else ROOT / "android/assets/mobile" / relative
                if asset.is_file():
                    kind = "text/html" if asset.suffix == ".html" else "text/css" if asset.suffix == ".css" else "text/javascript"
                    return web.text_response(connection, 200, asset.read_text("utf8"), kind + "; charset=utf-8")
            if name == "/ws": return None
            return await daemon.serve_http(connection, request)
        real_connect = websockets.connect
        def fixture_connect(url, **options):
            if url.startswith("wss://localhost:"): options["ssl"] = trust
            return real_connect(url, **options)
        job = None
        try:
            tls_limits = {"ssl_handshake_timeout": 2, "ssl_shutdown_timeout": 1} if encrypted else {}
            server = await serve(relay.handler, "127.0.0.1", 0, ssl=context if encrypted else None,
                                 process_request=http, close_timeout=1, open_timeout=2, **tls_limits)
            try:
                origin = f"{'https' if encrypted else 'http'}://localhost:{server.sockets[0].getsockname()[1]}"
                url = origin.replace("https:", "wss:").replace("http:", "ws:") + "/ws"
                with patch.object(websockets, "connect", fixture_connect):
                    job = asyncio.create_task(relay_uplink(daemon, url, "tls-room", "fixture"))
                    async with asyncio.timeout(8):
                        while url not in daemon.relay_live: await asyncio.sleep(.01)
                    # Actual APK networking classes complete the relay+daemon handshake.
                    assert await asyncio.to_thread(run, origin, "events", "tls-room") == "verified hello"
                    assert "测试成功" in await asyncio.to_thread(run, origin, "probe", "tls-room")
                    async with playwright.async_playwright() as runtime:
                        browser = await runtime.chromium.launch(executable_path=str(edge), headless=True)
                        try:
                            if encrypted:
                                strict = await browser.new_context()
                                strict_page = await strict.new_page()
                                with pytest.raises(Exception, match="CERT_AUTHORITY_INVALID"):
                                    await strict_page.goto(origin)
                                await strict.close()
                            # Only this disposable self-signed loopback fixture bypasses browser CA trust.
                            browser_context = await browser.new_context(ignore_https_errors=encrypted)
                            desktop, mobile = await browser_context.new_page(), await browser_context.new_page()
                            sockets, errors = [], []
                            for page in (desktop, mobile):
                                page.on("websocket", lambda ws: sockets.append(ws.url))
                                page.on("pageerror", lambda error: errors.append(str(error)))
                            await desktop.goto(origin + "/workspace?relay=" + quote(url, safe="") + "&code=tls-room&renderer=dom#token=" + daemon.token)
                            await desktop.wait_for_function("smaWorkspace?.connected && smaWorkspace.terminal")
                            config = {"endpoint": origin, "code": "tls-room", "token": daemon.token, "renderer": "dom"}
                            await mobile.goto(origin + "/mobile/index.html#" + quote(json.dumps(config)))
                            await mobile.wait_for_function("MobileTerminal.ready")
                            assert await desktop.locator('#tabs [role="tab"]').all_text_contents() == ["WSS fixture"]
                            assert sockets and all(item == url for item in sockets)
                            assert not errors, errors
                            assert all(daemon.token not in item for item in requests + sockets)
                            assert session_safe(daemon)
                        finally: await browser.close()
            finally:
                if job:
                    job.cancel(); await asyncio.gather(job, return_exceptions=True); job = None
                server.close()
                # Python 3.14 also tracks browser TLS preconnects without an HTTP handshake.
                # Close these fixture-only transports rather than leaving shutdown waiting.
                if hasattr(server.server, "abort_clients"): server.server.abort_clients()
                await asyncio.wait_for(server.wait_closed(), 5)
        finally:
            if job:
                job.cancel(); await asyncio.gather(job, return_exceptions=True)
            for identifier in list(daemon.hub.windows): daemon.hub.close_window(identifier)


def session_safe(daemon):
    """The fixtures exercise transport only; no terminal command is submitted."""
    return all(not session.pty.written for window in daemon.hub.windows.values() for session in window.sessions.values())
