"""Official MCP v1 client against an owned, authenticated loopback ASGI server."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import socket

import pytest

pytest.importorskip('mcp', reason='Optional SDK belongs in the isolated mylib MCP environment')
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import uvicorn

from supermonoagent.mylib_mcp import create_app


READ = 'owned-private-read-token-937623'
WRITE = 'owned-private-signal-token-28537'
REVISION = 'a' * 40


class Catalog:
    def status(self):
        return {'available': True, 'revision': REVISION, 'skill_count': 1}

    def list_skills(self, cursor=None, limit=50):
        return {'revision': REVISION, 'items': [{'id': 'fixture-skill', 'name': 'Fixture', 'path': 'skills/fixture/SKILL.md'}],
                'next_cursor': None, 'total': 1}

    def search(self, query, limit=10):
        return self.list_skills(limit=limit)

    def read_skill(self, skill_id, revision=None):
        assert skill_id == 'fixture-skill'
        return {'revision': revision or REVISION, 'skill_id': skill_id, 'text': 'Owned fixture skill.', 'references': []}

    def read_resource(self, skill_id, path, revision=None):
        return {'revision': revision or REVISION, 'skill_id': skill_id, 'path': path, 'text': 'Owned resource.'}


class Evolution:
    def __init__(self):
        self.signals = []

    def status(self):
        return {'available': True, 'semantic_efficacy': 'unverified', 'activation': 'manual_review_only'}

    def list_suggestions(self, limit=20):
        return {'items': [], 'count': 0, 'semantic_efficacy': 'unverified'}

    def record_signal(self, kind, name, note, source=None):
        self.signals.append((kind, name, note, source))
        return {'signal_id': 'fixture-signal', 'recorded': True, 'scope': 'mylib-review'}


def auth_file(folder):
    path = folder / 'private-auth.json'
    path.write_text(json.dumps({'schema': 1, 'tokens': [
        {'sha256': hashlib.sha256(READ.encode()).hexdigest(), 'scopes': ['metadata:read']},
        {'sha256': hashlib.sha256(WRITE.encode()).hexdigest(), 'scopes': ['signals:append']}]}), encoding='utf-8')
    path.chmod(0o600)
    return path


@asynccontextmanager
async def server(app):
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen(32)
    port = sock.getsockname()[1]
    service = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level='critical', lifespan='on',
                                           server_header=False, proxy_headers=False))
    task = asyncio.create_task(service.serve(sockets=[sock]))
    try:
        for _ in range(500):
            if service.started:
                break
            if task.done():
                await task
            await asyncio.sleep(.01)
        assert service.started
        yield f'http://127.0.0.1:{port}/mylib/mcp'
    finally:
        service.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


@asynccontextmanager
async def official_client(url, token):
    async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + token}, timeout=10, trust_env=False) as http:
        async with streamable_http_client(url, http_client=http) as (reader, writer, _session):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                yield client


def test_real_sdk_initialize_list_call_resource_and_scopes(tmp_path):
    async def scenario():
        evolution = Evolution()
        app = create_app(Catalog(), evolution, auth_file(tmp_path))
        async with server(app) as url:
            async with official_client(url, READ) as client:
                names = {tool.name for tool in (await client.list_tools()).tools}
                assert names == {'catalog_status', 'list_skills', 'search_skills', 'read_skill', 'read_resource',
                                 'review_status', 'list_suggestions', 'record_signal'}
                result = await client.call_tool('read_skill', {'skill_id': 'fixture-skill'})
                assert not result.isError and 'Owned fixture skill.' in str(result)
                resources = await client.list_resources()
                assert any(str(item.uri) == 'mylib://status' for item in resources.resources)
                content = await client.read_resource('mylib://skills/fixture-skill')
                assert 'Owned fixture skill.' in content.contents[0].text
                denied = await client.call_tool('record_signal', {'kind': 'failure', 'name': 'fixture', 'note': 'bounded metadata'})
                assert denied.isError and not evolution.signals
            async with official_client(url, WRITE) as client:
                accepted = await client.call_tool('record_signal', {'kind': 'failure', 'name': 'fixture', 'note': 'bounded metadata'})
                assert not accepted.isError and len(evolution.signals) == 1
                assert (await client.call_tool('catalog_status', {})).isError
    asyncio.run(scenario())


def test_every_http_method_is_private_host_origin_and_query_are_checked(tmp_path):
    async def scenario():
        app = create_app(Catalog(), Evolution(), auth_file(tmp_path), allowed_origins=('https://console.fixture',))
        async with server(app) as url, httpx.AsyncClient(trust_env=False) as http:
            for method in ('GET', 'POST', 'DELETE', 'OPTIONS', 'HEAD'):
                response = await http.request(method, url)
                assert response.status_code == 401 and response.headers['cache-control'] == 'no-store'
            assert (await http.get(url, headers={'Authorization': 'Bearer incorrect-fixture-token'})).status_code == 401
            headers = {'Authorization': 'Bearer ' + READ}
            response = await http.get(url, headers=headers)
            assert response.status_code == 405 and response.headers['cache-control'] == 'no-store'
            assert (await http.get(url, headers={**headers, 'Host': 'evil.fixture', 'X-Forwarded-Host': 'localhost'})).status_code == 421
            assert (await http.get(url, headers={**headers, 'Origin': 'https://evil.fixture'})).status_code == 403
            assert (await http.get(url, headers={**headers, 'Origin': 'null'})).status_code == 403
            assert (await http.get(url, headers={**headers, 'Origin': 'https://console.fixture'})).status_code == 405
            assert (await http.get(url + '?token=' + READ, headers=headers)).status_code == 400
            assert (await http.get(url.replace('/mylib/mcp', '/.well-known/oauth-authorization-server'), headers=headers)).status_code == 404
    asyncio.run(scenario())


def test_auth_file_revocation_and_invalid_replacement_fail_closed(tmp_path):
    async def scenario():
        path = auth_file(tmp_path)
        app = create_app(Catalog(), Evolution(), path)
        async with server(app) as url, httpx.AsyncClient(trust_env=False) as http:
            headers = {'Authorization': 'Bearer ' + READ}
            assert (await http.get(url, headers=headers)).status_code == 405
            path.write_text('{"schema":1,"tokens":[]}', encoding='utf-8')
            assert (await http.get(url, headers=headers)).status_code == 401
            path.write_text('{broken auth ' + WRITE, encoding='utf-8')
            response = await http.get(url, headers=headers)
            assert response.status_code == 503 and WRITE not in response.text
            auth_file(tmp_path)
            assert (await http.get(url, headers=headers)).status_code == 405
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['plaintext', 'unknown-scope', 'duplicate', 'schema', 'oversize', 'missing'])
def test_private_auth_has_no_implicit_default_or_plaintext_credentials(tmp_path, kind):
    path = auth_file(tmp_path)
    data = json.loads(path.read_text())
    if kind == 'plaintext':
        data['tokens'][0]['token'] = READ
    elif kind == 'unknown-scope':
        data['tokens'][0]['scopes'] = ['admin:promote']
    elif kind == 'duplicate':
        data['tokens'].append(data['tokens'][0])
    elif kind == 'schema':
        data['schema'] = True
    if kind == 'oversize':
        path.write_bytes(b'x' * 65537)
    elif kind == 'missing':
        path.unlink()
    else:
        path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError, match='private_auth_unavailable') as caught:
        create_app(Catalog(), Evolution(), path)
    assert READ not in str(caught.value) and WRITE not in str(caught.value)


def test_body_limit_applies_to_length_and_chunked_requests_before_sdk(tmp_path):
    async def scenario():
        app = create_app(Catalog(), Evolution(), auth_file(tmp_path), max_body_bytes=1024)
        async with server(app) as url, httpx.AsyncClient(trust_env=False) as http:
            headers = {'Authorization': 'Bearer ' + READ, 'Content-Type': 'application/json'}
            response = await http.post(url, headers=headers, content=b'x' * 1025)
            assert response.status_code == 413
            async def chunks():
                yield b'x' * 800
                yield b'x' * 800
            response = await http.post(url, headers=headers, content=chunks())
            assert response.status_code == 413
            assert (await http.post(url, headers={**headers, 'Content-Type': 'text/plain'}, content=b'{}')).status_code == 415
    asyncio.run(scenario())


def test_no_command_project_promotion_or_unbounded_signal_inputs(tmp_path):
    async def scenario():
        evolution = Evolution()
        app = create_app(Catalog(), evolution, auth_file(tmp_path))
        async with server(app) as url:
            async with official_client(url, WRITE) as client:
                for name in ('run_once', 'decide', 'sync', 'promotion', 'run_command'):
                    assert (await client.call_tool(name, {'command': 'never execute'})).isError
                for arguments in [
                    {'kind': 'failure', 'name': 'x', 'note': 'text', 'project_root': 'C:/outside'},
                    {'kind': 'failure', 'name': 'x', 'note': 'x' * 2001},
                    {'kind': 'failure', 'name': 'x', 'note': WRITE},
                    {'kind': 'failure', 'name': 'x', 'note': 'text', 'source': {'path': '/etc/private'}},
                ]:
                    assert (await client.call_tool('record_signal', arguments)).isError
                assert not evolution.signals
            async with official_client(url, READ) as client:
                assert (await client.call_tool('list_skills', {'limit': 101})).isError
                assert (await client.call_tool('list_skills', {'limit': True})).isError
                assert (await client.call_tool('read_resource', {'skill_id': 'fixture-skill', 'path': '../outside'})).isError
                assert (await client.call_tool('read_skill', {'skill_id': 'fixture-skill', 'revision': 'main'})).isError
    asyncio.run(scenario())


def test_backend_errors_and_oversize_results_never_expose_private_paths_or_secrets(tmp_path, caplog):
    class Broken(Catalog):
        def status(self):
            raise OSError('C:/private/runtime/' + READ + '/' + WRITE)

        def read_skill(self, skill_id, revision=None):
            raise RuntimeError('private ' + WRITE)

        def read_resource(self, skill_id, path, revision=None):
            return {'text': 'x' * 262145}

    async def scenario():
        app = create_app(Broken(), Evolution(), auth_file(tmp_path))
        async with server(app) as url, official_client(url, READ) as client:
            for name, arguments in [('catalog_status', {}), ('read_skill', {'skill_id': 'fixture-skill'}),
                ('read_resource', {'skill_id': 'fixture-skill', 'path': 'references/fixture.md'})]:
                result = await client.call_tool(name, arguments)
                assert result.isError and all(secret not in str(result) for secret in (READ, WRITE, 'C:/private'))
            with pytest.raises(Exception) as caught:
                await client.read_resource('mylib://skills/' + READ)
            assert READ not in str(caught.value) and WRITE not in str(caught.value)
    asyncio.run(scenario())
    assert all(secret not in caplog.text for secret in (READ, WRITE, 'C:/private'))


def test_binary_catalog_payload_fits_bounded_official_tool_response(tmp_path):
    class Binary(Catalog):
        def read_resource(self, skill_id, path, revision=None):
            import base64
            return {'revision': REVISION, 'encoding': 'base64', 'data': base64.b64encode(b'x' * 131072).decode()}
    async def scenario():
        app = create_app(Binary(), Evolution(), auth_file(tmp_path))
        async with server(app) as url, official_client(url, READ) as client:
            result = await client.call_tool('read_resource', {'skill_id': 'fixture-skill', 'path': 'fixture.bin'})
            assert not result.isError and 'base64' in str(result)
    asyncio.run(scenario())


def test_timeout_retains_worker_budget_and_new_requests_cannot_grow_queue(tmp_path):
    import threading
    import time
    entered, released = threading.Event(), threading.Event()
    class Slow(Catalog):
        calls = 0
        def status(self):
            self.calls += 1;entered.set()
            assert released.wait(4)
            return {'available': True}

    async def scenario():
        library = Slow()
        app = create_app(library, Evolution(), auth_file(tmp_path), request_timeout=.15, max_concurrency=1)
        try:
            async with server(app) as url, httpx.AsyncClient(trust_env=False) as http:
                headers = {'Authorization': 'Bearer ' + READ, 'Accept': 'application/json, text/event-stream'}
                response = await http.post(url, headers=headers, json={'jsonrpc': '2.0', 'id': 1,
                    'method': 'tools/call', 'params': {'name': 'catalog_status', 'arguments': {}}})
                assert entered.is_set() and response.status_code == 503
                start = time.monotonic()
                for _ in range(8):
                    assert (await http.get(url, headers=headers)).status_code == 503
                assert library.calls == 1 and time.monotonic() - start < 1
                released.set()
                for _ in range(100):
                    if app.pool.running == 0:
                        break
                    await asyncio.sleep(.01)
                assert (await http.get(url, headers=headers)).status_code == 405
        finally:
            released.set()
    asyncio.run(scenario())


def test_concurrent_read_and_signal_credentials_never_share_scopes(tmp_path):
    async def scenario():
        evolution = Evolution()
        app = create_app(Catalog(), evolution, auth_file(tmp_path))
        async with server(app) as url, official_client(url, READ) as reader, official_client(url, WRITE) as writer:
            arguments = {'kind': 'failure', 'name': 'concurrent fixture', 'note': 'explicit metadata'}
            calls = [reader.call_tool('record_signal', arguments) for _ in range(3)]
            calls += [writer.call_tool('record_signal', arguments) for _ in range(3)]
            results = await asyncio.gather(*calls)
            assert all(item.isError for item in results[:3])
            assert all(not item.isError for item in results[3:]) and len(evolution.signals) == 3
    asyncio.run(scenario())


def test_real_verified_git_catalog_is_readable_through_official_client(tmp_path):
    import subprocess
    from supermonoagent.mylib_catalog import CatalogStore, build_snapshot, publish_snapshot
    repo = tmp_path / 'fixture-library'
    (repo / 'skills/fixture/references').mkdir(parents=True)
    (repo / 'README.md').write_text('Owned source readme.', encoding='utf-8')
    (repo / 'WIKI.md').write_text('Owned source index.', encoding='utf-8')
    (repo / 'unrelated.txt').write_text('Not a declared resource.', encoding='utf-8')
    (repo / 'skills/fixture/SKILL.md').write_text('---\nname: Fixture\ndescription: Owned searchable fixture\n---\n'
        '# Fixture\n[Notes](references/notes.md)\n[Readme](../../README.md)\n', encoding='utf-8')
    (repo / 'skills/fixture/references/notes.md').write_text('Owned referenced text.', encoding='utf-8')
    for arguments in [('init', '-q'), ('config', 'user.email', 'fixture@example.invalid'),
        ('config', 'user.name', 'Owned Fixture'), ('add', '.'),
        ('commit', '-qm', 'Keep an immutable fixture source available\n\nScope-risk: narrow\nTested: Local fixture only')]:
        subprocess.run(['git', *arguments], cwd=repo, capture_output=True, check=True)
    candidate = tmp_path / 'candidate'
    build_snapshot(repo, candidate)
    store = CatalogStore(tmp_path / 'store')
    publish_snapshot(store, candidate)
    skill_id = store.list_skills()['items'][0]['id']

    async def scenario():
        app = create_app(store, Evolution(), auth_file(tmp_path))
        async with server(app) as url, official_client(url, READ) as client:
            assert not (await client.call_tool('catalog_status', {})).isError
            listed = await client.call_tool('list_skills', {})
            assert not listed.isError and skill_id in str(listed)
            document = await client.read_resource('mylib://skills/' + skill_id)
            assert 'Owned searchable fixture' in document.contents[0].text
            reference = await client.call_tool('read_resource', {'skill_id': skill_id, 'path': 'references/notes.md'})
            assert not reference.isError and 'Owned referenced text.' in str(reference)
            cross = await client.call_tool('read_resource', {'skill_id': skill_id, 'path': '@/README.md'})
            assert not cross.isError and 'Owned source readme.' in str(cross)
            assert (await client.call_tool('read_resource', {'skill_id': skill_id, 'path': '@/unrelated.txt'})).isError
    asyncio.run(scenario())


def test_incomplete_body_and_http_concurrency_have_a_deadline_without_sdk_work(tmp_path):
    async def scenario():
        import time
        app = create_app(Catalog(), Evolution(), auth_file(tmp_path), request_timeout=.2, max_concurrency=1)
        scope = {'type': 'http', 'method': 'POST', 'path': '/mylib/mcp', 'raw_path': b'/mylib/mcp',
                 'query_string': b'', 'headers': [(b'host', b'127.0.0.1'),
                 (b'authorization', ('Bearer ' + READ).encode()), (b'content-type', b'application/json')]}
        entered = asyncio.Event()
        responses, busy = [], []
        async def blocked():
            entered.set()
            await asyncio.Event().wait()
        async def empty():
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def collect(message):
            responses.append(message)
        async def collect_busy(message):
            busy.append(message)
        try:
            start = time.monotonic()
            task = asyncio.create_task(app(scope, blocked, collect))
            await asyncio.wait_for(entered.wait(), 1)
            await app({**scope, 'method': 'GET'}, empty, collect_busy)
            assert busy[0]['status'] == 503
            await task
            assert responses[0]['status'] == 408 and time.monotonic() - start < .8
            assert app.active == 0
        finally:
            await app.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('settings', [
    {'allowed_hosts': ('*',)}, {'allowed_hosts': 'localhost'}, {'allowed_hosts': ()},
    {'allowed_origins': ('null',)}, {'allowed_origins': ('http://public.fixture',)},
    {'max_concurrency': True}, {'max_body_bytes': 0}, {'request_timeout': float('inf')},
])
def test_invalid_security_configuration_is_not_silently_relaxed(tmp_path, settings):
    with pytest.raises(ValueError):
        create_app(Catalog(), Evolution(), auth_file(tmp_path), **settings)
