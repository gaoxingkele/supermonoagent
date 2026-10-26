import contextlib
import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import supermonoagent.mylib_remote_client as module
from supermonoagent.mylib_remote_client import MylibRemoteClient, MylibRemoteError

TOKEN = 'owned-fixture-private-token'
SKILL = 'a' * 64
REVISION = 'b' * 40


@contextlib.contextmanager
def server():
    state = {'requests': [], 'mode': 'json', 'result': {'revision': REVISION, 'items': []}, 'started': threading.Event()}
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            request = json.loads(body)
            state['requests'].append((request, dict(self.headers), body, self.path))
            if state['mode'] == 'delayed-handshake':
                time.sleep(.04)
            method = request['method']
            if method == 'notifications/initialized':
                self.send_response(202)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if method == 'initialize':
                result = {'protocolVersion': module.PROTOCOL, 'capabilities': {}, 'serverInfo': {'name': 'fixture', 'version': '1'}}
                if state['mode'] == 'protocol':
                    result['protocolVersion'] = '1999-01-01'
            else:
                result = {'structuredContent': {'result': state['result']}, 'content': []}
                if state['mode'] == 'direct':
                    result['structuredContent'] = state['result']
                if state['mode'] == 'fallback':
                    result = {'content': [{'type': 'text', 'text': json.dumps({'result': state['result']})}]}
                if state['mode'] == 'tool-error':
                    result = {'isError': True, 'content': [{'type': 'text', 'text': TOKEN}]}
            response = {'jsonrpc': '2.0', 'id': request['id'], 'result': result}
            if method == 'tools/call':
                mode = state['mode']
                if mode == 'wrong-id':
                    response['id'] += 1
                elif mode == 'rpc-error':
                    response = {'jsonrpc': '2.0', 'id': request['id'], 'error': {'message': TOKEN}}
                elif mode == 'server-request':
                    response = {'jsonrpc': '2.0', 'id': 100, 'method': 'sampling/createMessage', 'params': {'secret': TOKEN}}
                elif mode == 'bad-content':
                    response['result'] = {'content': [1]}
            raw = json.dumps(response).encode()
            kind = 'application/json'
            if state['mode'] == 'sse':
                raw = b': keepalive\r\nevent: message\r\nid: owned\r\ndata: ' + raw + b'\r\n\r\n'
                kind = 'text/event-stream'
            if state['mode'] == 'bad-json':
                raw = b'{"secret": "' + TOKEN.encode() + b'"'
            if state['mode'] == 'oversize':
                raw = b' ' * (module.MAX_RESPONSE + 1)
            if state['mode'] == 'duplicate':
                raw = b'{"jsonrpc":"2.0","id":1,"id":1,"result":{}}'
            if state['mode'] == 'multi-sse':
                kind = 'text/event-stream'
                raw = b'data: ' + raw + b'\n\ndata: ' + raw + b'\n\n'
            status = 302 if state['mode'] == 'redirect' else 401 if state['mode'] == 'unauthorized' else 200
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(raw)))
            if status == 302:
                self.send_header('Location', 'http://127.0.0.1:1/leak')
            if state['mode'] in ('slow-close', 'stalled-close'):
                self.send_header('Connection', 'close')
            self.end_headers()
            try:
                if state['mode'] == 'stalled-close':
                    state['started'].set()
                    time.sleep(.8)
                elif state['mode'] in ('slow', 'slow-close'):
                    state['started'].set()
                    for byte in raw:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(.025)
                else:
                    self.wfile.write(raw)
            except (OSError, ValueError):
                pass
    class OwnedServer(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            # Cancellation deliberately resets only this fixture's connections.
            pass
    http = OwnedServer(('127.0.0.1', 0), Handler)
    http.daemon_threads = True
    thread = threading.Thread(target=http.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    thread.start()
    state['url'] = f'http://127.0.0.1:{http.server_port}/mylib/mcp'
    try:
        yield state
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=1)


def client(state, **kwargs):
    return MylibRemoteClient._for_test(lambda: TOKEN, state['url'], **kwargs)


def test_fixed_endpoint_and_close():
    value = MylibRemoteClient(lambda: TOKEN)
    assert value._endpoint == module.ENDPOINT
    assert not hasattr(value, 'record_signal')
    value.close()
    with pytest.raises(MylibRemoteError, match='closed'):
        value.catalog_status()


@pytest.mark.parametrize('mode', ['json', 'sse', 'direct', 'fallback'])
def test_standard_handshake_and_authorization_only_header(mode, monkeypatch):
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:1')
    with server() as state:
        state['mode'] = mode
        value = client(state)
        assert value.catalog_status() == state['result']
        requests = state['requests']
        assert [item[0]['method'] for item in requests] == ['initialize', 'notifications/initialized', 'tools/call']
        assert requests[0][0]['params']['protocolVersion'] == module.PROTOCOL
        for i, (body, headers, raw, path) in enumerate(requests):
            assert headers['Authorization'] == 'Bearer ' + TOKEN
            assert TOKEN.encode() not in raw and TOKEN not in path
            assert headers['Accept'] == 'application/json, text/event-stream'
            assert path == '/mylib/mcp'
            assert 'Cookie' not in headers
            if i:
                assert headers['MCP-Protocol-Version'] == module.PROTOCOL
        value.close()


def test_all_readonly_methods_and_revision_forwarding():
    with server() as state:
        value = client(state)
        value.list_skills('eyJpIjoxfQ==', 100)
        value.search_skills('design query', 50)
        value.read_skill(SKILL, REVISION)
        value.read_resource(SKILL, '@/README.md', REVISION)
        value.review_status()
        value.list_suggestions(100)
        calls = [r[0]['params'] for r in state['requests'] if r[0]['method'] == 'tools/call']
        assert [c['name'] for c in calls] == ['list_skills', 'search_skills', 'read_skill', 'read_resource', 'review_status', 'list_suggestions']
        assert calls[0]['arguments'] == {'cursor': 'eyJpIjoxfQ==', 'limit': 100}
        assert calls[2]['arguments'] == {'skill_id': SKILL, 'revision': REVISION}
        assert calls[3]['arguments'] == {'skill_id': SKILL, 'path': '@/README.md', 'revision': REVISION}


@pytest.mark.parametrize('mode,code', [('wrong-id','invalid_response'), ('server-request','invalid_response'),
 ('rpc-error','rpc_error'), ('tool-error','tool_error'), ('protocol','unsupported_protocol'),
 ('bad-json','invalid_response'), ('duplicate','invalid_response'), ('oversize','response_too_large'),
 ('redirect','http_error'), ('unauthorized','http_error'), ('bad-content','invalid_tool_result'),
 ('multi-sse','invalid_response')])
def test_sanitized_failures(mode, code):
    with server() as state:
        state['mode'] = mode
        with pytest.raises(MylibRemoteError) as error:
            client(state).catalog_status()
        assert str(error.value) == code
        assert TOKEN not in str(error.value)
        if mode == 'redirect':
            assert len(state['requests']) == 1


@pytest.mark.parametrize('action', [lambda c:c.list_skills(limit=101), lambda c:c.list_skills(cursor='../'),
 lambda c:c.search_skills('x',True), lambda c:c.search_skills('x'*513), lambda c:c.search_skills('\x00'),
 lambda c:c.search_skills('a\x85b'),
 lambda c:c.read_skill('x'), lambda c:c.read_skill(SKILL, 'HEAD'), lambda c:c.list_suggestions(0),
 *[lambda c,p=p:c.read_resource(SKILL,p,REVISION) for p in ('../x','/tmp/x','a/../x','C:/x','a\\b','a\x00b','a//b','a\x85b')]])
def test_invalid_arguments_never_connect(action):
    with server() as state:
        with pytest.raises(MylibRemoteError):
            action(client(state))
        assert not state['requests']


@pytest.mark.parametrize('url', ['http://example.com/mylib/mcp','https://127.0.0.1/mylib/mcp',
 'http://user@127.0.0.1:1/mylib/mcp','http://127.0.0.1:1/mylib/mcp?token=secret',
 'http://127.0.0.1:1/other','http://127.0.0.1:1/mylib/mcp#secret'])
def test_test_connector_rejects_remote_or_unsafe(url):
    with pytest.raises(MylibRemoteError, match='invalid_test_endpoint'):
        MylibRemoteClient._for_test(lambda: TOKEN,url)


def test_request_bound_and_credential_in_content(monkeypatch):
    with server() as state:
        with pytest.raises(MylibRemoteError, match='credential_in_content'):
            client(state).search_skills(TOKEN)
        assert len(state['requests']) == 2
        state['requests'].clear()
        monkeypatch.setattr(module, 'MAX_REQUEST', 80)
        with pytest.raises(MylibRemoteError, match='request_too_large'):
            client(state).catalog_status()
        assert not state['requests']


@pytest.mark.parametrize('mode', ['slow','slow-close'])
def test_trickle_total_deadline(mode):
    with server() as state:
        state['mode'] = mode
        started = time.monotonic()
        with pytest.raises(MylibRemoteError, match='timeout'):
            client(state,timeout=.18).catalog_status()
        assert time.monotonic() - started < .65


@pytest.mark.parametrize('operation', ['cancel','close'])
@pytest.mark.parametrize('mode', ['slow','slow-close', 'stalled-close'])
def test_cancel_and_close_interrupt_owned_connection(operation, mode):
    with server() as state:
        state['mode'] = mode
        cancel = threading.Event()
        value = client(state,timeout=3)
        result = []
        def run():
            try:
                value.catalog_status(cancel_event=cancel)
            except MylibRemoteError as error:
                result.append(str(error))
        thread = threading.Thread(target=run)
        thread.start()
        assert state['started'].wait(1)
        started = time.monotonic()
        cancel.set() if operation == 'cancel' else value.close()
        thread.join(timeout=.7)
        assert not thread.is_alive()
        assert result == ['cancelled' if operation == 'cancel' else 'closed']
        assert time.monotonic()-started < .7


def test_provider_reread_and_no_response_credential_leak():
    with server() as state:
        current = [TOKEN]
        value = MylibRemoteClient._for_test(lambda:current[0],state['url'])
        value.catalog_status()
        current[0] = TOKEN+'-new'
        value.review_status()
        assert state['requests'][-1][1]['Authorization'] == 'Bearer '+current[0]
        assert [r[0]['method'] for r in state['requests']].count('initialize') == 2
        state['result'] = {'name': current[0]}
        with pytest.raises(MylibRemoteError, match='credential_in_response'):
            value.catalog_status()


@pytest.mark.parametrize('secret', ['owned-quote-"-fixture-secret', 'owned-backslash-\\-fixture-secret'])
@pytest.mark.parametrize('as_key', [False, True])
def test_escaped_credentials_never_reach_request_content_or_returned_result(secret, as_key):
    with server() as state:
        value = MylibRemoteClient._for_test(lambda: secret, state['url'])
        with pytest.raises(MylibRemoteError, match='credential_in_content'):
            value.search_skills(secret)
        assert len(state['requests']) == 2
        state['result'] = {secret: 'value'} if as_key else {'nested': [{'text': secret}]}
        with pytest.raises(MylibRemoteError, match='credential_in_response'):
            value.read_skill(SKILL, REVISION)
        assert state['requests'][-1][1]['Authorization'] == 'Bearer ' + secret


def test_twenty_pages_initialize_once_and_reread_provider_each_operation():
    with server() as state:
        state['mode'] = 'delayed-handshake'
        calls = []
        def credential():
            calls.append(1)
            return TOKEN
        value = MylibRemoteClient._for_test(credential, state['url'], timeout=1)
        started = time.monotonic()
        for _ in range(20):
            assert value.list_skills(limit=100)['revision'] == REVISION
        assert time.monotonic() - started < 2
        assert len(calls) == 20
        methods = [r[0]['method'] for r in state['requests']]
        assert len(methods) == 22
        assert methods.count('initialize') == methods.count('notifications/initialized') == 1
        assert methods.count('tools/call') == 20
        value.close()


def test_cancel_before_connect_and_private_write_rejected():
    with server() as state:
        cancel = threading.Event()
        cancel.set()
        value = client(state)
        with pytest.raises(MylibRemoteError, match='cancelled'):
            value.catalog_status(cancel_event=cancel)
        with pytest.raises(MylibRemoteError, match='unsupported_tool'):
            value._call('record_signal', {}, None)
        assert not state['requests']


def test_one_deadline_covers_initialize_notification_and_tool(monkeypatch):
    from types import SimpleNamespace
    clock = [100.0]
    phases = []
    # Replace this module's reference only, leaving real server/thread clocks
    # untouched. Exact phase count must not depend on host scheduling load.
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    value = MylibRemoteClient(lambda: TOKEN, timeout=.10)
    def post(message, token, deadline, cancel, **kwargs):
        phases.append((message['method'], deadline))
        clock[0] += .04
        value._check(deadline, cancel)
        if message['method'] == 'initialize':
            return {'protocolVersion': module.PROTOCOL, 'capabilities': {}}
        return None
    monkeypatch.setattr(value, '_post', post)
    with pytest.raises(MylibRemoteError, match='timeout'):
        value.catalog_status()
    assert [phase for phase, _ in phases] == ['initialize', 'notifications/initialized', 'tools/call']
    assert [deadline for _, deadline in phases] == [100.10] * 3


def test_production_uses_verified_tls_and_sanitizes_transport(monkeypatch):
    seen = []
    class FailedTLS:
        sock = None
        def __init__(self, host, port, *, context, timeout):
            seen.append((host, port, context.check_hostname, context.verify_mode))
        def request(self, *args, **kwargs):
            raise OSError(TOKEN)
        def close(self):
            pass
    monkeypatch.setattr(module, '_HTTPSConnection', FailedTLS)
    with pytest.raises(MylibRemoteError) as error:
        MylibRemoteClient(lambda: TOKEN).catalog_status()
    assert str(error.value) == 'transport_failed'
    assert seen == [('117.50.48.22', None, True, module.ssl.CERT_REQUIRED)]


def test_against_official_sdk_stateless_service(tmp_path):
    pytest.importorskip('mcp', reason='SDK interoperability uses optional isolated environment')
    import asyncio
    from test_mylib_mcp import Catalog, Evolution, auth_file, server as sdk_server, READ
    from supermonoagent.mylib_mcp import create_app
    class OwnedCatalog(Catalog):
        def read_skill(self, skill_id, revision=None):
            return {'skill_id': skill_id, 'revision': revision, 'text': 'Owned skill.'}
    async def scenario():
        app = create_app(OwnedCatalog(), Evolution(), auth_file(tmp_path))
        async with sdk_server(app) as url:
            value = MylibRemoteClient._for_test(lambda: READ, url)
            try:
                assert (await asyncio.to_thread(value.catalog_status))['available']
                assert (await asyncio.to_thread(value.list_skills))['total'] == 1
                assert (await asyncio.to_thread(value.read_skill, SKILL, REVISION))['revision'] == REVISION
                assert (await asyncio.to_thread(value.read_resource, SKILL, '@/README.md', REVISION))['text'] == 'Owned resource.'
                assert (await asyncio.to_thread(value.review_status))['semantic_efficacy'] == 'unverified'
                assert (await asyncio.to_thread(value.list_suggestions))['count'] == 0
            finally:
                value.close()
    asyncio.run(scenario())
