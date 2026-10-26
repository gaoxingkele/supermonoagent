"""Owned loopback file service fixtures; no terminal/production connections."""
import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import time
import secrets
from contextlib import asynccontextmanager

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from supermonoagent.file_broker import Broker, enrollment_proof


TOKEN = 'owned-viewer-fixture-token'
KEY = 'owned-install-enrollment-fixture-key-12345'
SECRET = 'owned-host-fixture-secret-0123456789'


def auth_file(root, owners=None):
    path = root / 'auth.json'
    path.write_text(json.dumps({'schema': 1, 'owners': owners or [
        {'scope': 'owner', 'token_sha256': [hashlib.sha256(TOKEN.encode()).hexdigest()],
         'rooms': ['*'], 'enrollment_key': KEY}]}))
    return path


def test_canonical_enrollment_proof_does_not_depend_on_json_whitespace():
    payload = json.dumps(['sma-files-1', 'room', 'host', hashlib.sha256(SECRET.encode()).hexdigest(),
                          123, 'nonce-fixture-0123456789'], ensure_ascii=False, separators=(',', ':')).encode()
    assert enrollment_proof(KEY, 'room', 'host', SECRET, 123, 'nonce-fixture-0123456789') == hmac.new(
        KEY.encode(), payload, hashlib.sha256).hexdigest()


@asynccontextmanager
async def running(root, auth, **options):
    broker = Broker(root, auth, **options)
    try:
        async with serve(broker.handle_socket, '127.0.0.1', 0, max_size=262144, max_queue=8,
                         ping_interval=.05, ping_timeout=2) as server:
            yield broker, f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/file/ws'
    finally:
        await broker.close()


def hello(role='client', room='room', token=TOKEN, secret=SECRET, enroll=True, now=None, host_id='host'):
    value = {'protocol': 'sma-files-1', 'role': role, 'room': room, 'token': token}
    if role == 'host':
        value.update(host_id=host_id, host_secret=secret)
        if enroll:
            timestamp, nonce = int(time.time() if now is None else now), secrets.token_hex(16)
            value['enrollment'] = {'timestamp': timestamp, 'nonce': nonce,
                'proof': enrollment_proof(KEY, room, host_id, secret, timestamp, nonce)}
    return value


async def login(url, value):
    ws = await connect(url, max_size=262144)
    await ws.send(json.dumps(value))
    ready = json.loads(await ws.recv())
    assert ready['type'] == 'ready'
    return ws


async def call(ws, action, data, session='session', request_id='request'):
    await ws.send(json.dumps({'type': 'request', 'id': request_id, 'action': action,
                             'session': session, 'data': data}))
    result = json.loads(await asyncio.wait_for(ws.recv(), 3))
    assert result['type'] == 'response' and result['id'] == request_id
    return result


def metadata(data=b'fixture PDF bytes', **changes):
    return {'original_name': '报告.pdf', 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'window_id': 'window', 'daemon_epoch': 'daemon-fixture-epoch', 'workspace_revision': 7,
            'timezone_offset_minutes': 480, 'project_binding': 'b' * 64, **changes}


async def begin(client, host, data=None, session='session', target_changes=None):
    data = metadata() if data is None else data
    future = asyncio.create_task(call(client, 'upload.begin', data, session))
    routed = json.loads(await asyncio.wait_for(host.recv(), 3))
    assert routed['type'] == 'host_request' and routed['action'] == 'workspace.get'
    assert routed['session'] == session and routed['data'] == {}
    target = {'window_id': data['window_id'], 'session_id': session, 'daemon_epoch': data['daemon_epoch'],
              'revision': data['workspace_revision'], 'project_binding': data['project_binding'], **(target_changes or {})}
    await host.send(json.dumps({'type': 'host_response', 'id': routed['id'], 'ok': True, 'result': target}))
    return await future


async def stage(client, host, data=b'fixture PDF bytes', **changes):
    info = metadata(data, **changes)
    started = await begin(client, host, info)
    assert started['ok'], started
    transfer = started['result']['upload_id']
    appended = await call(client, 'upload.chunk', {'upload_id': transfer, 'offset': 0,
                                                  'data': base64.b64encode(data).decode()})
    assert appended['ok']
    finished = await call(client, 'upload.finish', {'upload_id': transfer})
    assert finished['ok'], finished
    delivery = json.loads(await asyncio.wait_for(host.recv(), 3))
    assert delivery['type'] == 'delivery'
    return finished['result'], delivery['job']


@pytest.mark.parametrize('bad', [None, '', 'x'*64, '0'*63, '0'*65, 123])
def test_missing_or_malformed_project_binding_is_rejected_without_host_route(tmp_path, bad):
    async def scenario():
        async with running(tmp_path / 'broker', auth_file(tmp_path)) as (broker, url):
            viewer = await login(url, hello())
            try:
                data = metadata()
                if bad is None: data.pop('project_binding')
                else: data['project_binding'] = bad
                response = await call(viewer, 'upload.begin', data)
                assert response['error'] == 'project_binding_required' and not broker._uploads
            finally: await viewer.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('target', ['', None, 'a'*64])
def test_old_host_or_changed_project_binding_fails_preflight(tmp_path, target):
    async def scenario():
        async with running(tmp_path / 'broker', auth_file(tmp_path)) as (broker, url):
            host, viewer = await login(url, hello('host')), await login(url, hello())
            try:
                response = await begin(viewer, host, target_changes={'project_binding': target})
                expected = 'project_binding_changed' if target else 'project_binding_required'
                assert response['error'] == expected and not broker._uploads
            finally: await viewer.close(); await host.close()
    asyncio.run(scenario())


def test_legacy_broker_schema_migrates_unbound_unfinished_jobs_fail_closed(tmp_path):
    import sqlite3
    root = tmp_path / 'broker'; root.mkdir()
    with sqlite3.connect(root/'broker.sqlite3') as db:
        db.execute('CREATE TABLE jobs(file_id TEXT PRIMARY KEY,scope TEXT,room TEXT,session TEXT,window_id TEXT,daemon_epoch TEXT,revision INTEGER,name TEXT,size INTEGER,sha256 TEXT,state TEXT,receipt TEXT,error TEXT)')
        for label, state in [('a', 'queued'), ('b', 'saved')]:
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (label*48, 'owned', 'room', 'session', 'window', 'epoch', 0, 'legacy.pdf', 0, '0'*64, state, '{"legacy":true}' if state=='saved' else None, None))
    async def scenario():
        async with running(root, auth_file(tmp_path)) as (broker, _):
            old = dict(broker._db.execute('SELECT * FROM jobs WHERE file_id=?', ('a'*48,)).fetchone())
            saved = dict(broker._db.execute('SELECT * FROM jobs WHERE file_id=?', ('b'*48,)).fetchone())
            assert old['state'] == 'failed' and old['error'] == 'project_binding_required' and old['project_binding'] is None
            assert saved['state'] == 'saved' and saved['receipt'] == '{"legacy":true}'
    asyncio.run(scenario())


def test_selection_required_preflight_rejects_before_staging(tmp_path):
    async def scenario():
        async with running(tmp_path / 'broker', auth_file(tmp_path)) as (broker, url):
            host, viewer = await login(url, hello('host')), await login(url, hello())
            try:
                result = await begin(viewer, host, target_changes={'project_status': 'selection_required'})
                assert not result['ok'] and result['error'] == 'project_required', result
                assert not broker._uploads
                assert not list((tmp_path / 'broker').rglob('*.part'))
            finally:
                await viewer.close(); await host.close()
    asyncio.run(scenario())


def test_real_loopback_stage_download_ack_duplicate_and_private_storage(tmp_path):
    async def scenario():
        auth = auth_file(tmp_path)
        clock = lambda: 1700000000
        async with running(tmp_path / 'broker', auth, clock=clock) as (broker, url):
            host = await login(url, hello('host', now=clock()))
            client = await login(url, hello())
            try:
                started = await begin(client, host)
                assert started['ok']
                assert started['result']['name'] == '20231115-061320-报告.pdf'
                transfer = started['result']['upload_id']
                payload = b'fixture PDF bytes'
                assert (await call(client, 'upload.chunk', {'upload_id': transfer, 'offset': 0,
                    'data': base64.b64encode(payload).decode()}))['ok']
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(host.recv(), .05)
                done = await call(client, 'upload.finish', {'upload_id': transfer})
                assert done['result']['state'] == 'queued'
                job = json.loads(await host.recv())['job']
                assert job['session_id'] == 'session' and job['workspace_revision'] == 7
                saved_files = list((broker.root / 'file').glob('*/*/*.pdf'))
                assert len(saved_files) == 1 and saved_files[0].read_bytes() == payload
                assert len(saved_files[0].parent.name) == len(saved_files[0].parent.parent.name) == 64
                assert not (await call(client, 'download.chunk', {'file_id': job['file_id'], 'offset': 0}))['ok']
                downloaded = await call(host, 'download.chunk', {'file_id': job['file_id'], 'offset': 0})
                assert base64.b64decode(downloaded['result']['data']) == payload
                status = await call(client, 'file.status', {'file_id': job['file_id']})
                assert status['result']['state'] == 'downloading' and 'receipt' not in status['result']
                receipt = {key: job[key] for key in ('file_id', 'name', 'size', 'sha256', 'daemon_epoch', 'workspace_revision', 'project_binding')}
                receipt['path'] = 'C:/owned/exchange/' + job['name']
                assert (await call(host, 'delivery.ack', receipt))['result']['state'] == 'saved'
                assert (await call(host, 'delivery.ack', receipt))['result']['state'] == 'saved'
                status = await call(client, 'file.status', {'file_id': job['file_id']})
                assert status['result']['receipt'] == receipt
                assert not (await call(host, 'delivery.fail', {'file_id': job['file_id'], 'error': 'bad_hash'}))['ok']
            finally:
                await client.close()
                await host.close()
    asyncio.run(scenario())


def test_viewer_cannot_impersonate_host_or_replay_enrollment_and_pin_survives_restart(tmp_path):
    async def rejected(url, value):
        from websockets.exceptions import ConnectionClosed
        async with connect(url) as ws:
            await ws.send(json.dumps(value))
            with pytest.raises(ConnectionClosed):
                await ws.recv()

    async def scenario():
        auth = auth_file(tmp_path)
        root = tmp_path / 'broker'
        enrolled = hello('host')
        async with running(root, auth) as (_, url):
            await rejected(url, hello('host', enroll=False))
            fake = hello('host')
            fake['enrollment']['proof'] = '0' * 64
            await rejected(url, fake)
            host = await login(url, enrolled)
            await host.close()
            await asyncio.sleep(.02)
            await rejected(url, enrolled)
            await rejected(url, hello('host', enroll=False, secret='different-fixture-secret-0123456789'))
            await rejected(url, hello('host', token='wrong-token'))
        async with running(root, auth) as (_, url):
            await rejected(url, enrolled)
            host = await login(url, hello('host', enroll=False))
            await host.close()
    asyncio.run(scenario())


def test_completed_job_survives_broker_restart_and_replays_only_its_host(tmp_path):
    async def scenario():
        auth, root = auth_file(tmp_path), tmp_path / 'broker'
        async with running(root, auth) as (_, url):
            host, client = await login(url, hello('host')), await login(url, hello())
            finished, job = await stage(client, host)
            await client.close()
            await host.close()
        async with running(root, auth) as (_, url):
            client = await login(url, hello())
            status = await call(client, 'file.status', {'file_id': finished['file_id']})
            assert status['result']['state'] == 'queued'
            host = await login(url, hello('host', enroll=False))
            replay = json.loads(await host.recv())
            assert replay['job'] == job
            response = await call(host, 'download.chunk', {'file_id': job['file_id'], 'offset': 0})
            assert hashlib.sha256(base64.b64decode(response['result']['data'])).hexdigest() == job['sha256']
            await client.close()
            await host.close()
    asyncio.run(scenario())


def test_room_token_and_session_namespaces_do_not_share_jobs(tmp_path):
    async def scenario():
        other_token = 'owned-other-viewer-token'
        auth = auth_file(tmp_path, [
            {'scope': 'owner', 'token_sha256': [hashlib.sha256(TOKEN.encode()).hexdigest()], 'rooms': ['*'], 'enrollment_key': KEY},
            {'scope': 'other', 'token_sha256': [hashlib.sha256(other_token.encode()).hexdigest()], 'rooms': ['*'], 'enrollment_key': KEY}])
        async with running(tmp_path / 'broker', auth) as (_, url):
            host, client = await login(url, hello('host')), await login(url, hello())
            _, job = await stage(client, host)
            others = [await login(url, hello(room='other-room')), await login(url, hello(token=other_token)),
                      await login(url, hello('host', room='other-room'))]
            try:
                for other in others:
                    result = await call(other, 'file.status' if other is not others[-1] else 'download.chunk',
                        {'file_id': job['file_id']} if other is not others[-1] else {'file_id': job['file_id'], 'offset': 0})
                    assert not result['ok']
                assert not (await call(client, 'file.status', {'file_id': job['file_id']}, session='different'))['ok']
            finally:
                for other in others:
                    await other.close()
                await client.close()
                await host.close()
    asyncio.run(scenario())


def test_disconnect_ttl_bad_hash_and_revision_failure_never_publish(tmp_path):
    async def scenario():
        now = [1700000000]
        async with running(tmp_path / 'broker', auth_file(tmp_path), clock=lambda: now[0], upload_ttl=.2) as (broker, url):
            host, client = await login(url, hello('host', now=now[0])), await login(url, hello())
            wrong = await begin(client, host, target_changes={'revision': 8})
            assert wrong['error'] == 'target_changed' and not broker._uploads
            started = await begin(client, host)
            transfer = started['result']['upload_id']
            now[0] += 1
            assert not (await call(client, 'upload.finish', {'upload_id': transfer}))['ok']
            assert not broker._uploads
            started = await begin(client, host)
            transfer = started['result']['upload_id']
            bad = b'X' * len(b'fixture PDF bytes')
            assert (await call(client, 'upload.chunk', {'upload_id': transfer, 'offset': 0,
                                                       'data': base64.b64encode(bad).decode()}))['ok']
            assert not (await call(client, 'upload.finish', {'upload_id': transfer}))['ok']
            assert not list((broker.root / 'file').glob('*/*/*.pdf'))
            await begin(client, host)
            await client.close()
            deadline = time.monotonic() + 2
            while broker._uploads and time.monotonic() < deadline:
                await asyncio.sleep(.01)
            assert not broker._uploads
            assert not list((broker.root / 'file').glob('*/*/.supermono-partials/*.part'))
            await host.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('limit', ['max_active', 'max_files', 'max_total_bytes', 'max_file_size'])
def test_quotas_include_active_reservations_and_retained_completed_files(tmp_path, limit):
    async def scenario():
        options = {limit: 1 if limit in {'max_active', 'max_files'} else 16}
        async with running(tmp_path / 'broker', auth_file(tmp_path), **options) as (_, url):
            host, client = await login(url, hello('host')), await login(url, hello())
            first = await call(client, 'upload.begin', metadata()) if limit == 'max_file_size' else await begin(client, host)
            if limit in {'max_total_bytes', 'max_file_size'}:
                assert not first['ok']
            else:
                assert first['ok']
                second = await begin(client, host)
                assert second['error'] == 'quota_exceeded'
            await client.close()
            await host.close()
    asyncio.run(scenario())


def test_forwarded_routes_bind_caller_host_epoch_and_cleanup_on_disconnect(tmp_path):
    async def scenario():
        async with running(tmp_path / 'broker', auth_file(tmp_path), route_timeout=.12) as (broker, url):
            host = await login(url, hello('host'))
            wrong_host = await login(url, hello('host', room='other'))
            a, b = await login(url, hello()), await login(url, hello())
            task_a = asyncio.create_task(call(a, 'library.get', {}, request_id='same'))
            route_a = json.loads(await host.recv())
            task_b = asyncio.create_task(call(b, 'workspace.get', {}, request_id='same'))
            route_b = json.loads(await host.recv())
            assert route_a['id'] != route_b['id'] != 'same'
            await wrong_host.send(json.dumps({'type': 'host_response', 'id': route_a['id'], 'ok': True, 'result': {'wrong': True}}))
            await host.send(json.dumps({'type': 'host_response', 'id': route_b['id'], 'ok': True, 'result': {'right': 'b'}}))
            assert (await task_b)['result'] == {'right': 'b'}
            assert (await task_a)['error'] == 'route_timeout'
            await host.send(json.dumps({'type': 'host_response', 'id': route_a['id'], 'ok': True, 'result': {'late': True}}))
            task = asyncio.create_task(call(a, 'workspace.get', {}))
            await host.recv()
            await host.close()
            assert (await task)['error'] == 'host_unavailable'
            assert not broker._routes
            await a.close()
            await b.close()
            await wrong_host.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['extra', 'schema', 'scope', 'digest', 'duplicate-token', 'wildcard', 'key', 'duplicate-field'])
def test_auth_configuration_is_strict_and_fails_before_storage(tmp_path, change):
    path = auth_file(tmp_path)
    value = json.loads(path.read_text())
    owner = value['owners'][0]
    if change == 'extra':
        owner['token'] = TOKEN
    elif change == 'schema':
        value['schema'] = True
    elif change == 'scope':
        owner['scope'] = '../scope'
    elif change == 'digest':
        owner['token_sha256'] = [TOKEN]
    elif change == 'duplicate-token':
        owner['token_sha256'] *= 2
    elif change == 'wildcard':
        owner['rooms'] = ['*', 'room']
    elif change == 'key':
        owner['enrollment_key'] = 'short'
    if change == 'duplicate-field':
        path.write_text('{"schema":1,"schema":1,"owners":[]}')
    else:
        path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='configuration'):
        Broker(tmp_path / 'must-not-exist', path)
    assert not (tmp_path / 'must-not-exist').exists()


def test_auth_room_allowlist_bad_timestamp_url_token_and_oversized_frames(tmp_path):
    async def scenario():
        from websockets.exceptions import ConnectionClosed
        auth = auth_file(tmp_path, [{'scope': 'owner', 'token_sha256': [hashlib.sha256(TOKEN.encode()).hexdigest()],
                                    'rooms': ['allowed'], 'enrollment_key': KEY}])
        async with running(tmp_path / 'broker', auth) as (_, url):
            for target, value in [(url, hello(room='denied')), (url + '?token=' + TOKEN, hello(room='allowed')),
                (url, hello('host', room='allowed', now=int(time.time()) - 301))]:
                async with connect(target) as ws:
                    with pytest.raises(ConnectionClosed):
                        await ws.send(json.dumps(value))
                        await ws.recv()
            ws = await login(url, hello(room='allowed'))
            await ws.send('x' * 262145)
            with pytest.raises(ConnectionClosed):
                await ws.recv()
    asyncio.run(scenario())


def test_receipts_cannot_change_target_revision_epoch_hash_or_name(tmp_path):
    async def scenario():
        async with running(tmp_path / 'broker', auth_file(tmp_path)) as (_, url):
            host, client = await login(url, hello('host')), await login(url, hello())
            _, job = await stage(client, host)
            receipt = {key: job[key] for key in ('file_id', 'name', 'size', 'sha256', 'daemon_epoch', 'workspace_revision', 'project_binding')}
            receipt['path'] = 'C:/owned/' + job['name']
            for key, value in [('workspace_revision', 8), ('daemon_epoch', 'other'), ('sha256', '0' * 64), ('project_binding', '0'*64),
                               ('name', 'different.pdf'), ('path', 'https://example.invalid/' + job['name'])]:
                response = await call(host, 'delivery.ack', {**receipt, key: value})
                assert not response['ok'] and response['error'] == 'receipt_mismatch'
            status = await call(client, 'file.status', {'file_id': job['file_id']})
            assert status['result']['state'] == 'queued'
            assert (await call(host, 'delivery.ack', receipt))['ok']
            assert not (await call(host, 'delivery.ack', {**receipt, 'path': 'C:/other/' + job['name']}))['ok']
            await client.close()
            await host.close()
    asyncio.run(scenario())


def test_chunked_file_collision_names_long_unicode_extension_and_retained_quota(tmp_path):
    async def scenario():
        payload = bytes(range(256)) * 800
        async with running(tmp_path / 'broker', auth_file(tmp_path), max_files=2) as (_, url):
            host, client = await login(url, hello('host')), await login(url, hello())
            info = metadata(payload, original_name='长' * 240 + '.docx')
            started = await begin(client, host, info)
            name = started['result']['name']
            assert len(name.encode('utf-16-le')) // 2 <= 200 and name.endswith('.docx')
            transfer = started['result']['upload_id']
            offset = 0
            while offset < len(payload):
                chunk = payload[offset:offset + 98304]
                response = await call(client, 'upload.chunk', {'upload_id': transfer, 'offset': offset,
                    'data': base64.b64encode(chunk).decode()})
                assert response['result']['received'] == offset + len(chunk)
                offset += len(chunk)
            done = await call(client, 'upload.finish', {'upload_id': transfer})
            job = json.loads(await host.recv())['job']
            downloaded, offset = bytearray(), 0
            while offset < len(payload):
                response = await call(host, 'download.chunk', {'file_id': job['file_id'], 'offset': offset})
                downloaded.extend(base64.b64decode(response['result']['data']))
                offset = response['result']['next']
            assert bytes(downloaded) == payload
            second = await begin(client, host, info)
            assert second['result']['name'] != name and second['result']['name'].endswith('.docx')
            assert (await begin(client, host, info))['error'] == 'quota_exceeded'
            await call(client, 'upload.cancel', {'upload_id': second['result']['upload_id']})
            assert done['result']['state'] == 'queued'
            await client.close()
            await host.close()
    asyncio.run(scenario())


def test_file_io_executor_does_not_block_socket_ping(tmp_path, monkeypatch):
    async def scenario():
        import threading
        entered, release = threading.Event(), threading.Event()
        async with running(tmp_path / 'broker', auth_file(tmp_path), io_workers=1) as (broker, url):
            host, client = await login(url, hello('host')), await login(url, hello())
            started = await begin(client, host)
            original = next(iter(broker._stores.values())).append

            def slow(*args):
                entered.set()
                assert release.wait(2)
                return original(*args)

            monkeypatch.setattr(next(iter(broker._stores.values())), 'append', slow)
            pending = asyncio.create_task(call(client, 'upload.chunk', {'upload_id': started['result']['upload_id'],
                'offset': 0, 'data': base64.b64encode(b'fixture PDF bytes').decode()}))
            deadline = time.monotonic() + 1
            while not entered.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(.005)
            assert entered.is_set()
            try:
                pong = await client.ping()
                await asyncio.wait_for(pong, .2)
            finally:
                release.set()
            assert (await pending)['ok']
            await client.close()
            await host.close()
    asyncio.run(scenario())


def test_exclusive_registry_lock_is_released_on_close(tmp_path):
    async def scenario():
        auth = auth_file(tmp_path)
        root = tmp_path / 'broker'
        async with running(root, auth):
            with pytest.raises(ValueError, match='already in use'):
                Broker(root, auth)
        # The OS lock is released by close; a clean restart is valid.
        async with running(root, auth):
            pass
    asyncio.run(scenario())


def test_link_storage_fails_closed(tmp_path):
    auth_file(tmp_path)
    external = tmp_path / 'external'
    external.mkdir()
    linked = tmp_path / 'linked-root'
    junction = False
    try:
        linked.symlink_to(external, target_is_directory=True)
    except OSError:
        import os
        import subprocess
        if os.name != 'nt':
            pytest.skip('Directory symlinks unavailable to fixture account')
        created = subprocess.run(['cmd', '/c', 'mklink', '/J', str(linked), str(external)],
                                 capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if created.returncode:
            pytest.skip('Directory junction unavailable to fixture account')
        junction = True
    try:
        with pytest.raises(ValueError):
            Broker(linked, tmp_path / 'auth.json')
        assert list(external.iterdir()) == []
    finally:
        if junction:
            linked.rmdir()  # Remove this test junction only, never its target.


def test_saved_receipt_persists_without_replay_and_ack_stays_idempotent(tmp_path):
    async def scenario():
        auth, root = auth_file(tmp_path), tmp_path / 'broker'
        async with running(root, auth) as (broker, url):
            host, client = await login(url, hello('host')), await login(url, hello())
            _, job = await stage(client, host)
            receipt = {key: job[key] for key in ('file_id', 'name', 'size', 'sha256', 'daemon_epoch', 'workspace_revision', 'project_binding')}
            receipt['path'] = 'C:/owned/' + job['name']
            assert (await call(host, 'delivery.ack', receipt))['ok']
            await client.close()
            await host.close()
        async with running(root, auth) as (broker, url):
            host, client = await login(url, hello('host', enroll=False)), await login(url, hello())
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(host.recv(), .05)
            assert (await call(client, 'file.status', {'file_id': job['file_id']}))['result']['receipt'] == receipt
            assert (await call(host, 'delivery.ack', receipt))['ok']
            await client.close()
            await host.close()
    asyncio.run(scenario())


def test_invalid_upload_paths_chunk_limits_and_credentials_not_persisted(tmp_path):
    async def scenario():
        root, auth = tmp_path / 'broker', auth_file(tmp_path)
        async with running(root, auth) as (broker, url):
            host, client = await login(url, hello('host')), await login(url, hello())
            for bad in [metadata(original_name='../outside.pdf'), metadata(original_name='NUL.pdf'),
                        metadata(timezone_offset_minutes=841), {**metadata(), 'path': 'C:/outside'},
                        {**metadata(), 'url': 'https://outside.invalid/file'}]:
                if bad.get('original_name', '').startswith('../') or bad.get('original_name') == 'NUL.pdf':
                    response = await begin(client, host, bad)
                else:
                    response = await call(client, 'upload.begin', bad)
                assert not response['ok']
            started = await begin(client, host)
            upload_id = started['result']['upload_id']
            assert not (await call(client, 'upload.chunk', {'upload_id': upload_id, 'offset': True, 'data': ''}))['ok']
            assert not (await call(client, 'upload.chunk', {'upload_id': upload_id, 'offset': 0,
                'data': base64.b64encode(b'x' * 98305).decode()}))['ok']
            assert not (await call(client, 'upload.chunk', {'upload_id': upload_id, 'offset': 0, 'data': '%%%'}))['ok']
            assert (await call(client, 'upload.cancel', {'upload_id': upload_id}))['ok']
            assert not broker._uploads
            await client.close()
            await host.close()
        for path in root.iterdir():
            if path.is_file():
                raw = path.read_bytes()
                assert all(secret.encode() not in raw for secret in (TOKEN, KEY, SECRET))
    asyncio.run(scenario())


def test_enrollment_replay_registry_has_a_finite_nonce_quota(tmp_path):
    broker = Broker(tmp_path / 'broker', auth_file(tmp_path), max_hosts=1)
    try:
        for _ in range(4):
            assert broker._authenticate(hello('host')) == ('owner', 'room', 'host')
        with pytest.raises(ValueError, match='authentication_failed'):
            broker._authenticate(hello('host'))
        assert broker._db.execute('SELECT COUNT(*) FROM nonces').fetchone()[0] == 4
        # A pinned reconnect doesn't require another enrollment record.
        assert broker._authenticate(hello('host', enroll=False)) == ('owner', 'room', 'host')
    finally:
        asyncio.run(broker.close())


def test_host_routed_errors_are_safe_codes_and_never_exception_text(tmp_path):
    async def scenario():
        async with running(tmp_path / 'broker', auth_file(tmp_path)) as (_, url):
            host, client = await login(url, hello('host')), await login(url, hello())
            for raw, expected in [('revision_conflict', 'revision_conflict'),
                                  ('permission denied C:/private/' + KEY, 'host_rejected')]:
                pending = asyncio.create_task(call(client, 'workspace.set', {}))
                route = json.loads(await host.recv())
                await host.send(json.dumps({'type': 'host_response', 'id': route['id'], 'ok': False, 'error': raw}))
                assert (await pending)['error'] == expected
            await client.close()
            await host.close()
    asyncio.run(scenario())


def test_http_ticket_stream_range_requires_header_and_is_not_a_public_alias(tmp_path, capsys, monkeypatch):
    # The standalone relay runtime is Python 3.10, without file_digest.
    monkeypatch.delattr(hashlib, 'file_digest', raising=False)
    async def scenario():
        import urllib.request
        import urllib.error
        async with running(tmp_path / 'broker', auth_file(tmp_path)) as (broker, url):
            server, address = broker.start_http()
            base = f'http://127.0.0.1:{address[1]}'
            host, client = await login(url, hello('host')), await login(url, hello())
            _, job = await stage(client, host)
            result = await call(host, 'download.ticket', {'file_id': job['file_id']})
            assert result['ok'], result
            ticket = result['result']
            assert ticket['download_path'] == '/file/download/' + job['file_id']

            def fetch(headers=None, path=None):
                request = urllib.request.Request(base + (path or ticket['download_path']), headers=headers or {})
                try:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open(request, timeout=2) as response:
                        return response.status, dict(response.headers), response.read()
                except urllib.error.HTTPError as error:
                    return error.code, dict(error.headers), error.read()

            assert (await asyncio.to_thread(fetch))[0] == 401
            auth = {'Authorization': 'Bearer ' + ticket['ticket']}
            status, headers, content = await asyncio.to_thread(fetch, auth)
            assert status == 200 and content == b'fixture PDF bytes'
            assert headers['Cache-Control'] == 'no-store' and headers['X-Content-SHA256'] == job['sha256']
            status, headers, content = await asyncio.to_thread(fetch, {**auth, 'Range': 'bytes=2-5'})
            assert status == 206 and content == b'xtur' and headers['Content-Range'] == 'bytes 2-5/17'
            assert (await asyncio.to_thread(fetch, auth, ticket['download_path'] + '?token=' + ticket['ticket']))[0] == 401
            assert (await asyncio.to_thread(fetch, auth, '/file/download/' + '0' * 48))[0] == 401
            assert (await call(host, 'download.chunk', {'file_id': job['file_id'], 'offset': 0}))['ok']
            await client.close()
            await host.close()
            return ticket['ticket']
    raw = asyncio.run(scenario())
    captured = capsys.readouterr()
    assert raw not in captured.out + captured.err


def http_fetch(base, path, ticket='', range_value=None):
    import urllib.request
    import urllib.error
    headers = {'Authorization': 'Bearer ' + ticket} if ticket else {}
    if range_value is not None:
        headers['Range'] = range_value
    request = urllib.request.Request(base + path, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=3) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def test_http_ticket_expiry_revocation_scope_session_and_job_state(tmp_path):
    async def scenario():
        now = [1700000000]
        auth = auth_file(tmp_path)
        async with running(tmp_path / 'broker', auth, clock=lambda: now[0], ticket_ttl=10) as (broker, url):
            _, address = broker.start_http()
            base = f'http://127.0.0.1:{address[1]}'
            host, client = await login(url, hello('host', now=now[0])), await login(url, hello())
            _, job = await stage(client, host)
            other = await login(url, hello('host', room='other', now=now[0]))
            try:
                assert not (await call(client, 'download.ticket', {'file_id': job['file_id']}))['ok']
                assert not (await call(other, 'download.ticket', {'file_id': job['file_id']}))['ok']
                assert not (await call(host, 'download.ticket', {'file_id': job['file_id']}, session='other'))['ok']
                ticket = (await call(host, 'download.ticket', {'file_id': job['file_id']}))['result']
                path, raw = ticket['download_path'], ticket['ticket']
                assert (await asyncio.to_thread(http_fetch, base, path, 'wrong-ticket'))[0] == 401
                now[0] += 11
                assert (await asyncio.to_thread(http_fetch, base, path, raw))[0] == 401
                ticket = (await call(host, 'download.ticket', {'file_id': job['file_id']}))['result']
                raw = ticket['ticket']
                cfg = json.loads(auth.read_text())
                cfg['owners'][0]['token_sha256'] = [hashlib.sha256(b'revoked-fixture-replacement').hexdigest()]
                auth.write_text(json.dumps(cfg))
                assert (await asyncio.to_thread(http_fetch, base, path, raw))[0] == 401
                auth_file(tmp_path)
                ticket = (await call(host, 'download.ticket', {'file_id': job['file_id']}))['result']
                await call(host, 'delivery.fail', {'file_id': job['file_id'], 'error': 'target_changed'})
                assert (await asyncio.to_thread(http_fetch, base, path, ticket['ticket']))[0] == 401
            finally:
                await host.close()
                await client.close()
                await other.close()
    asyncio.run(scenario())


def test_http_ticket_dies_with_host_epoch_and_close_stops_http_threads(tmp_path):
    async def scenario():
        broker_ref, thread, address = None, None, None
        async with running(tmp_path / 'broker', auth_file(tmp_path)) as (broker, url):
            broker_ref = broker
            server, address = broker.start_http()
            thread = broker._http_thread
            host, client = await login(url, hello('host')), await login(url, hello())
            _, job = await stage(client, host)
            ticket = (await call(host, 'download.ticket', {'file_id': job['file_id']}))['result']
            await host.close()
            base = f'http://127.0.0.1:{address[1]}'
            assert (await asyncio.to_thread(http_fetch, base, ticket['download_path'], ticket['ticket']))[0] == 401
            await asyncio.sleep(.02)
            replacement = await login(url, hello('host', enroll=False))
            assert json.loads(await replacement.recv())['type'] == 'delivery'
            assert (await asyncio.to_thread(http_fetch, base, ticket['download_path'], ticket['ticket']))[0] == 401
            assert (await call(replacement, 'download.ticket', {'file_id': job['file_id']}))['ok']
            await replacement.close()
            await client.close()
        assert not thread.is_alive() and broker_ref._http_stopping.is_set()
        import socket
        with socket.socket() as sock:
            assert sock.connect_ex(('127.0.0.1', address[1])) != 0
    asyncio.run(scenario())


@pytest.mark.parametrize('value,expected,body', [
    ('bytes=0-', 206, b'fixture PDF bytes'), ('bytes=-5', 206, b'bytes'),
    ('bytes=15-999', 206, b'es'), ('bytes=17-', 416, b''), ('bytes=5-2', 416, b''),
    ('bytes=-0', 416, b''), ('bytes=0-1,4-5', 416, b''), ('items=0-1', 416, b''),
])
def test_http_ranges_are_single_bounded_and_hash_metadata_is_whole_object(tmp_path, value, expected, body):
    async def scenario():
        async with running(tmp_path / 'broker', auth_file(tmp_path)) as (broker, url):
            _, address = broker.start_http()
            host, client = await login(url, hello('host')), await login(url, hello())
            _, job = await stage(client, host)
            ticket = (await call(host, 'download.ticket', {'file_id': job['file_id']}))['result']
            status, headers, received = await asyncio.to_thread(http_fetch, f'http://127.0.0.1:{address[1]}',
                ticket['download_path'], ticket['ticket'], value)
            assert status == expected and received == body
            assert headers['Cache-Control'] == 'no-store'
            if expected == 416:
                assert headers['Content-Range'] == 'bytes */17'
            else:
                assert headers['X-Content-SHA256'] == job['sha256']
            await host.close()
            await client.close()
    asyncio.run(scenario())


def test_http_streams_two_megabytes_with_bounded_memory_and_live_websocket_ping(tmp_path, capsys):
    async def scenario():
        import urllib.request
        import tracemalloc
        payload = bytes(range(256)) * 9000
        async with running(tmp_path / 'broker', auth_file(tmp_path)) as (broker, url):
            _, address = broker.start_http()
            host, client = await login(url, hello('host')), await login(url, hello())
            started = await begin(client, host, metadata(payload))
            upload = started['result']['upload_id']
            for offset in range(0, len(payload), 98304):
                assert (await call(client, 'upload.chunk', {'upload_id': upload, 'offset': offset,
                    'data': base64.b64encode(payload[offset:offset + 98304]).decode()}))['ok']
            finished = await call(client, 'upload.finish', {'upload_id': upload})
            job = json.loads(await host.recv())['job']
            ticket = (await call(host, 'download.ticket', {'file_id': job['file_id']}))['result']

            def streaming_get():
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                request = urllib.request.Request(f'http://127.0.0.1:{address[1]}' + ticket['download_path'],
                    headers={'Authorization': 'Bearer ' + ticket['ticket']})
                digest, total = hashlib.sha256(), 0
                with opener.open(request, timeout=3) as response:
                    while chunk := response.read(32768):
                        digest.update(chunk)
                        total += len(chunk)
                return digest.hexdigest(), total

            tracemalloc.start()
            try:
                pending = asyncio.create_task(asyncio.to_thread(streaming_get))
                pong = await client.ping()
                await asyncio.wait_for(pong, .5)
                digest, total = await pending
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            assert digest == job['sha256'] and total == len(payload)
            assert peak < 2 * 1024**2  # Neither server nor client reads the full 2.3 MB object.
            assert finished['result']['state'] == 'queued'
            await host.close()
            await client.close()
            return ticket['ticket']
    ticket = asyncio.run(scenario())
    captured = capsys.readouterr()
    assert ticket not in captured.out + captured.err


@pytest.mark.parametrize('field', ['revision', 'project_binding'])
def test_http_ticket_quota_and_binding_cannot_be_changed_after_issue(tmp_path, field):
    async def scenario():
        now = [1700000000]
        async with running(tmp_path / 'broker', auth_file(tmp_path), clock=lambda: now[0],
                           ticket_ttl=2, max_tickets=1) as (broker, url):
            with pytest.raises(ValueError, match='loopback'):
                broker.start_http('0.0.0.0', 0)
            _, address = broker.start_http()
            host, client = await login(url, hello('host', now=now[0])), await login(url, hello())
            _, job = await stage(client, host)
            first = (await call(host, 'download.ticket', {'file_id': job['file_id']}))['result']
            assert (await call(host, 'download.ticket', {'file_id': job['file_id']}))['error'] == 'quota_exceeded'
            now[0] += 3
            ticket = (await call(host, 'download.ticket', {'file_id': job['file_id']}))['result']
            assert ticket['ticket'] != first['ticket'] and len(broker._tickets) == 1
            with broker._lock, broker._db:
                if field == 'revision':
                    broker._db.execute('UPDATE jobs SET revision=revision+1 WHERE file_id=?', (job['file_id'],))
                else:
                    broker._db.execute('UPDATE jobs SET project_binding=? WHERE file_id=?', ('0'*64, job['file_id']))
            response = await asyncio.to_thread(http_fetch, f'http://127.0.0.1:{address[1]}',
                ticket['download_path'], ticket['ticket'])
            assert response[0] == 401
            await host.close()
            await client.close()
    asyncio.run(scenario())


def test_http_shutdown_interrupts_incomplete_header_reader_without_blocking_ws(tmp_path):
    async def scenario():
        import socket
        active = None
        try:
            async with running(tmp_path / 'broker', auth_file(tmp_path), http_workers=1) as (broker, url):
                server, address = broker.start_http()
                client = await login(url, hello())
                active = socket.create_connection(('127.0.0.1', address[1]), timeout=2)
                active.sendall(b'GET /file/download/' + b'0' * 48 + b' HTTP/1.1\r\nAuthorization: Bearer unfinished')
                deadline = time.monotonic() + 1
                while not server.sockets and time.monotonic() < deadline:
                    await asyncio.sleep(.005)
                assert server.sockets
                pong = await client.ping()
                await asyncio.wait_for(pong, .3)
                await client.close()
                started = time.monotonic()
                await broker.close()
                assert time.monotonic() - started < 1.5
            assert not broker._http_thread.is_alive() and not server.sockets
        finally:
            if active:
                active.close()
    asyncio.run(scenario())
