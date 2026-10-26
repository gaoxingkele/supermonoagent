"""Owned loopback parents/broker only; no existing terminal or credentials."""
import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response
from websockets.exceptions import ConnectionClosed

from supermonoagent.file_broker import Broker
from supermonoagent.file_companion import FileCompanion, LegacyInspector, CompanionError, _endpoint
from supermonoagent.protocol import pack_json, pack, unpack, unpack_json


class OldParent:
    def __init__(self, project, token):
        self.project, self.token = project, token
        self.owner = 'missing'
        self.default_token = token
        self.health_present = True
        self.multiplex = True
        self.session_open = True
        self.pid = os.getpid()
        self.sockets, self.operations = set(), []
        self.pty_written = []

    def state(self):
        service = {'pid': self.pid}
        if self.owner != 'missing':
            service['owner_authenticated'] = self.owner
        return {'capabilities': {'multiplex': self.multiplex}, 'service': service,
                'windows': [{'id': 'window1', 'sessions': [{'id': 'session1', 'cwd': str(self.project)}] if self.session_open else []}]}

    async def http(self, connection, request):
        if request.path != '/health':
            return None
        value = {'ok': True, 'pid': self.pid}
        if self.health_present:
            value['default_token'] = self.default_token
        body = json.dumps(value).encode()
        return Response(200, 'OK', Headers({'Content-Type': 'application/json', 'Content-Length': str(len(body))}), body)

    async def handle(self, ws):
        self.sockets.add(ws)
        try:
            async for raw in ws:
                op, payload = unpack(raw)
                self.operations.append(op)
                if op == 1:
                    # Legacy HELLO may accept a viewer: owner proof is separate.
                    assert unpack_json(payload).get('multiplex') is True
                    await ws.send(pack_json(2, self.state()))
                elif op == 10:
                    await ws.send(pack(11, payload))
                else:
                    self.pty_written.append(raw)
                    raise AssertionError('Companion attempted a terminal operation')
        finally:
            self.sockets.discard(ws)

    async def publish(self):
        for ws in list(self.sockets):
            try:
                await ws.send(pack_json(7, self.state()))
            except ConnectionClosed:
                pass


async def eventually(function, timeout=8):
    async with asyncio.timeout(timeout):
        while True:
            result = function()
            if result:
                return result
            await asyncio.sleep(.02)


@asynccontextmanager
async def environment(tmp_path, *, download_mode='http'):
    token = tmp_path / 'viewer-token'
    token.write_text('owned-viewer-token', encoding='utf8')
    key = tmp_path / 'enrollment'
    key.write_text('owned-enrollment-secret-12345678901234567890', encoding='utf8')
    auth = tmp_path / 'auth.json'
    auth.write_text(json.dumps({'schema': 1, 'owners': [{'scope': 'owned',
        'token_sha256': [hashlib.sha256(token.read_bytes()).hexdigest()], 'rooms': ['A', 'B'],
        'enrollment_key': key.read_text()}]}), encoding='utf8')
    broker = Broker(tmp_path / 'broker', auth)
    _, http_address = broker.start_http()
    download_base = f'http://127.0.0.1:{http_address[1]}'
    async with serve(broker.handle_socket, '127.0.0.1', 0, max_size=262144) as broker_server:
        broker_url = f'ws://127.0.0.1:{broker_server.sockets[0].getsockname()[1]}/file/ws'
        parents, companions, tasks, servers = [], [], [], []
        try:
            for label in ['A', 'B']:
                project = tmp_path / ('project-' + label)
                project.mkdir()
                owner = tmp_path / ('owner-' + label)
                owner.write_text('owned-owner-' + label, encoding='utf8')
                parent = OldParent(project, owner.read_text())
                server = await serve(parent.handle, '127.0.0.1', 0, process_request=parent.http)
                servers.append(server)
                backend = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
                companion = FileCompanion(backend, broker_url, label, tmp_path / ('helper-' + label), owner, key,
                    broker_token_file=token, default_mylib_root=str(tmp_path / 'missing-library'),
                    download_mode=download_mode, download_base_url=download_base)
                companions.append(companion);parents.append(parent)
                tasks.append(asyncio.create_task(companion.run()))
                companion._fixture_tasks, companion._fixture_task = tasks, tasks[-1]
            await eventually(lambda: all(item.inspector.valid.is_set() for item in companions))
            yield broker, broker_url, parents, companions
        finally:
            for companion in companions:
                await companion.close()
            await asyncio.wait_for(asyncio.gather(*tasks), 10)
            for server in servers:
                server.close();await server.wait_closed()
            await broker.close()
            assert all(not parent.pty_written for parent in parents)
            assert all(set(parent.operations) <= {1, 10} for parent in parents)


@asynccontextmanager
async def client(url, room):
    async with connect(url, proxy=None) as ws:
        await ws.send(json.dumps({'protocol': 'sma-files-1', 'role': 'client', 'room': room, 'token': 'owned-viewer-token'}))
        assert json.loads(await ws.recv())['type'] == 'ready'
        yield ws


async def request(ws, action, data=None, *, session='session1', ok=True):
    await ws.send(json.dumps({'type': 'request', 'id': 'fixture', 'action': action, 'session': session, 'data': data or {}}))
    body = json.loads(await asyncio.wait_for(ws.recv(), 8))
    if ok:
        assert body.get('ok') is True, body
        return body['result']
    assert body.get('ok') is False, body
    return body


async def workspace(ws):
    async with asyncio.timeout(8):
        while True:
            await ws.send(json.dumps({'type': 'request', 'id': 'fixture', 'action': 'workspace.get', 'session': 'session1', 'data': {}}))
            body = json.loads(await ws.recv())
            if body.get('ok'):
                return body['result']
            await asyncio.sleep(.05)


async def upload(ws, cfg, content=b'%PDF-1.7 owned fixture', name='document.pdf'):
    begin = await request(ws, 'upload.begin', {'original_name': name, 'size': len(content),
        'sha256': hashlib.sha256(content).hexdigest(), 'window_id': cfg['window_id'],
        'daemon_epoch': cfg['daemon_epoch'], 'workspace_revision': cfg['revision'], 'project_binding': cfg['project_binding'], 'timezone_offset_minutes': 480})
    for offset in range(0, len(content), 98304):
        await request(ws, 'upload.chunk', {'upload_id': begin['upload_id'], 'offset': offset, 'data': base64.b64encode(content[offset:offset+98304]).decode()})
    return await request(ws, 'upload.finish', {'upload_id': begin['upload_id']})


async def saved(ws, file_id):
    async with asyncio.timeout(8):
        while True:
            status = await request(ws, 'file.status', {'file_id': file_id})
            if status['state'] in {'saved', 'failed'}:
                return status
            await asyncio.sleep(.03)


def captured_upload(cfg, content=b'owned binding bytes'):
    value = {'original_name': 'binding.pdf', 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest(),
             'window_id': cfg['window_id'], 'daemon_epoch': cfg['daemon_epoch'],
             'workspace_revision': cfg['revision'], 'timezone_offset_minutes': 0}
    if 'project_binding' in cfg:
        value['project_binding'] = cfg['project_binding']
    return value


@pytest.mark.parametrize('change', ['cwd', 'project_identity', 'exchange_identity'])
def test_preflight_binding_rejects_default_revision_zero_target_changes(tmp_path, change):
    async def exercise():
        async with environment(tmp_path) as (broker, url, parents, companions):
            async with client(url, 'A') as ws:
                if change == 'exchange_identity': (parents[0].project / 'tmp').mkdir()
                cfg = await workspace(ws)
                assert cfg['revision'] == 0
                if change == 'cwd':
                    target = tmp_path / 'other-project'; target.mkdir()
                    parents[0].project = target; await parents[0].publish()
                    await eventually(lambda: companions[0].inspector.valid.is_set()
                                     and companions[0].inspector.snapshot('session1')['cwd'] == str(target))
                elif change == 'project_identity':
                    original = parents[0].project
                    original.rename(original.with_name('retired-project')); original.mkdir()
                else:
                    original = parents[0].project / 'tmp'
                    original.rename(original.with_name('retired-tmp')); original.mkdir()
                await ws.send(json.dumps({'type': 'request', 'id': 'fixture', 'action': 'upload.begin',
                    'session': 'session1', 'data': captured_upload(cfg)}))
                result = json.loads(await ws.recv())
                assert result['ok'] is False and result['error'] == 'project_binding_changed', result
                assert not broker._uploads and not list(tmp_path.rglob('*.part'))
    asyncio.run(exercise())


@pytest.mark.parametrize('phase', ['download', 'prepublish'])
def test_original_binding_never_redirects_to_b_during_download_or_publication(tmp_path, phase):
    async def exercise():
        async with environment(tmp_path) as (_, url, parents, companions):
            helper = companions[0]
            entered, release = threading.Event(), threading.Event()
            original_request, original_finish = helper._request, helper._finish
            async def request_gate(action, session, data, generation):
                result = await original_request(action, session, data, generation)
                if phase == 'download' and action == 'download.ticket':
                    entered.set()
                    while not release.is_set(): await asyncio.sleep(.01)
                return result
            def finish_gate(*args):
                if phase == 'prepublish': entered.set(); assert release.wait(5)
                return original_finish(*args)
            helper._request, helper._finish = request_gate, finish_gate
            try:
                async with client(url, 'A') as ws:
                    cfg = await workspace(ws)
                    queued = await upload(ws, cfg)
                    await eventually(entered.is_set)
                    target = tmp_path / 'other-project'; target.mkdir()
                    parents[0].project = target; await parents[0].publish()
                    await eventually(lambda: helper.inspector.valid.is_set()
                                     and helper.inspector.snapshot('session1')['cwd'] == str(target))
                    release.set()
                    result = await saved(ws, queued['file_id'])
                    assert result['state'] == 'failed', result
                    assert list(target.iterdir()) == [] and not list(tmp_path.rglob('*.part'))
                    assert not list((tmp_path / 'project-A').rglob('*.pdf'))
            finally:
                release.set()
    asyncio.run(exercise())


def test_post_fsync_boundary_rechecks_project_identity_before_hardlink_publish(tmp_path):
    async def exercise():
        async with environment(tmp_path) as (_, url, parents, companions):
            helper = companions[0]
            entered, release = threading.Event(), threading.Event()
            original_begin = helper._begin
            stores = []
            def capture_begin(*args):
                result = original_begin(*args)
                stores.append(result[0])
                if result[1]:
                    transfer = result[0]._transfers[result[1]]
                    real = transfer.stream
                    class PostFsyncClose:
                        def __getattr__(self, name): return getattr(real, name)
                        def close(self):
                            already_closed = real.closed
                            real.close()
                            # Actual finish order is flush -> fsync -> close ->
                            # can_publish. Gate immediately after real close so
                            # Windows permits the real directory replacement.
                            if not already_closed:
                                entered.set(); assert release.wait(5)
                    transfer.stream = PostFsyncClose()
                return result
            helper._begin = capture_begin
            try:
                async with client(url, 'A') as ws:
                    cfg = await workspace(ws)
                    queued = await upload(ws, cfg)
                    await eventually(entered.is_set)
                    project = parents[0].project
                    retired = tmp_path/'retired-project'
                    project.rename(retired); project.mkdir()
                    (retired/'tmp').rename(project/'tmp')
                    release.set()
                    result = await saved(ws, queued['file_id'])
                    assert result['state'] == 'failed' and result['error'] == 'project_binding_changed', result
                    assert not list(project.rglob('*.pdf')) and not list(retired.rglob('*.pdf'))
                    # Existing TransferStore refuses unlink through a changed
                    # ancestor. It releases the handle/registry, leaving only
                    # the quarantined partial, rather than deleting via a path
                    # which no longer has its original identity.
                    parts = list(tmp_path.rglob('*.part'))
                    assert len(parts) == 1 and parts[0].read_bytes() == b'%PDF-1.7 owned fixture'
                    assert not stores[0]._transfers
            finally:
                release.set()
    asyncio.run(exercise())


def test_saved_binding_replays_after_helper_restart_without_copying_again(tmp_path):
    async def exercise():
        async with environment(tmp_path) as (broker, url, parents, companions):
            helper = companions[0]
            async with client(url, 'A') as ws:
                cfg = await workspace(ws)
                queued = await upload(ws, cfg)
                status = await saved(ws, queued['file_id'])
                path = Path(status['receipt']['path']); before = path.stat()
                await eventually(lambda: queued['file_id'] not in helper._deliveries)
                await helper.close(); await helper._fixture_task
                replacement = FileCompanion(helper.backend, helper.broker_url, helper.room, helper.state_dir,
                    helper.owner_token_file, helper.enrollment_key_file, broker_token_file=helper.broker_token_file,
                    default_mylib_root=str(tmp_path/'missing-library'), download_base_url=helper.download_base_url)
                acknowledgements = []
                original = replacement._request
                async def counted(action, *args):
                    result = await original(action, *args)
                    if action == 'delivery.ack': acknowledgements.append(result)
                    return result
                replacement._request = counted
                task = asyncio.create_task(replacement.run())
                helper._fixture_tasks.append(task)
                replacement._fixture_tasks, replacement._fixture_task = helper._fixture_tasks, task
                companions[0] = replacement
                await workspace(ws)
                row = broker._db.execute('SELECT * FROM jobs WHERE file_id=?', (queued['file_id'],)).fetchone()
                await broker._delivery(broker._hosts[('owned', 'A')], row)
                await eventually(lambda: bool(acknowledgements))
                assert replacement.identity == helper.identity
                assert path.read_bytes() == b'%PDF-1.7 owned fixture' and path.stat().st_mtime_ns == before.st_mtime_ns
                assert len(list((parents[0].project/'tmp').glob('*.pdf'))) == 1
    asyncio.run(exercise())


@pytest.mark.parametrize('unfinished', [False, True])
def test_receiver_legacy_journal_migration_preserves_files_but_cannot_authorize_unbound_job(tmp_path, unfinished):
    import sqlite3
    state = tmp_path/'legacy-state'; state.mkdir()
    target = tmp_path/'legacy.pdf'; target.write_bytes(b'previously saved owned bytes')
    old_job = {'file_id': 'a'*48, 'name': 'legacy.pdf', 'size': target.stat().st_size,
        'sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'window_id': 'window1',
        'session_id': 'session1', 'daemon_epoch': 'b'*64, 'workspace_revision': 0}
    partial_dir = tmp_path/'.supermono-partials'; partial_dir.mkdir()
    owned_partial = partial_dir/('c'*48+'.part')
    neighbor = partial_dir/('d'*48+'.part'); neighbor.write_bytes(b'unrelated owned fixture')
    if unfinished: owned_partial.write_bytes(b'legacy unfinished')
    with sqlite3.connect(state/'deliveries.sqlite3') as db:
        db.execute('CREATE TABLE deliveries(file_id TEXT PRIMARY KEY,binding TEXT,target TEXT,partial TEXT,receipt TEXT)')
        db.execute('INSERT INTO deliveries VALUES(?,?,?,?,?)',
            (old_job['file_id'], json.dumps(old_job), str(target), 'c'*48 if unfinished else None,
             None if unfinished else '{"legacy":true}'))
    db.close()
    async def exercise():
        helper = FileCompanion('http://127.0.0.1:1', 'ws://127.0.0.1:2/file/ws', 'owned',
            state, tmp_path/'unused-owner', tmp_path/'unused-enrollment')
        try:
            helper.identity = helper._initialize()
            row = helper._db.execute('SELECT target_binding,receipt FROM deliveries').fetchone()
            assert row == (None, None if unfinished else '{"legacy":true}')
            with pytest.raises(CompanionError, match='project_binding_required'):
                helper._job(old_job)
            assert target.read_bytes() == b'previously saved owned bytes'
            assert not owned_partial.exists() and neighbor.read_bytes() == b'unrelated owned fixture'
            assert helper._db.execute('SELECT partial FROM deliveries').fetchone()[0] is None
        finally:
            if helper._db: helper._db.close()
            helper._release()
            await helper.inspector.close(); await helper.io.close()
        # A real fresh broker/parent/helper flow remains usable after migration.
        # Reuse the migrated helper directory, including the quarantined row.
        state.rename(tmp_path/'helper-A')
        async with environment(tmp_path) as (_, url, parents, _):
            async with client(url, 'A') as ws:
                result = await saved(ws, (await upload(ws, await workspace(ws)))['file_id'])
                assert result['state'] == 'saved'
                assert Path(result['receipt']['path']).parent == parents[0].project/'tmp'
                assert target.read_bytes() == b'previously saved owned bytes'
    asyncio.run(exercise())


def test_staged_default_revision_zero_job_cannot_follow_cwd_to_other_project(tmp_path):
    async def exercise():
        async with environment(tmp_path) as (_, url, parents, companions):
            companion = companions[0]; original = companion._deliver
            entered, release = asyncio.Event(), asyncio.Event()
            async def gated(job, generation):
                entered.set(); await release.wait(); return await original(job, generation)
            companion._deliver = gated
            try:
                async with client(url, 'A') as ws:
                    queued = await upload(ws, await workspace(ws))
                    await asyncio.wait_for(entered.wait(), 5)
                    target = tmp_path / 'other-project'; target.mkdir()
                    parents[0].project = target; await parents[0].publish()
                    await eventually(lambda: companion.inspector.valid.is_set()
                                     and companion.inspector.snapshot('session1')['cwd'] == str(target))
                    release.set()
                    result = await saved(ws, queued['file_id'])
                    assert result['state'] == 'failed' and result['error'] == 'project_binding_changed', result
                    assert list(target.iterdir()) == [] and not list(tmp_path.rglob('*.part'))
                    assert not list((tmp_path / 'project-A').rglob('*.pdf'))
            finally:
                release.set()
    asyncio.run(exercise())


@pytest.mark.parametrize('mode', ['http', 'ws'])
def test_two_owned_parents_route_and_save_verified_bytes(tmp_path, mode):
    async def exercise():
        async with environment(tmp_path, download_mode=mode) as (_, url, parents, companions):
            async with client(url, 'A') as a, client(url, 'B') as b:
                cfg_a, cfg_b = await workspace(a), await workspace(b)
                assert cfg_a['project_dir'] == str(parents[0].project)
                assert cfg_a['daemon_epoch'] != cfg_b['daemon_epoch']
                assert cfg_a['mylib_status'] == 'missing'
                assert not Path(cfg_a['exchange_dir']).exists()
                data = b'%PDF-1.7\nowned binary\x00\xff'
                queued = await upload(a, cfg_a, data)
                result = await saved(a, queued['file_id'])
                assert result['state'] == 'saved', result
                path = Path(result['receipt']['path'])
                assert path.parent == parents[0].project / 'tmp'
                assert re_name(path.name) and path.read_bytes() == data
                assert not (parents[1].project / 'tmp').exists()
                assert not list(path.parent.glob('.supermono-partials/*.part'))
    asyncio.run(exercise())


def test_home_startup_rejects_upload_until_project_selected_then_saves_to_project_tmp(tmp_path, monkeypatch):
    home = tmp_path / 'synthetic-home'; home.mkdir()
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    async def exercise():
        async with environment(tmp_path) as (broker, url, parents, companions):
            parents[0].project = home
            await parents[0].publish()
            await eventually(lambda: companions[0].inspector.valid.is_set()
                             and companions[0].inspector.snapshot('session1')['cwd'] == str(home))
            async with client(url, 'A') as ws:
                cfg = await workspace(ws)
                assert cfg['project_status'] == 'selection_required' and cfg['exchange_dir'] == str(home / 'tmp')
                content = b'%PDF-1.7 selected owned project'
                denied = await request(ws, 'upload.begin', {'original_name': 'owned.pdf', 'size': len(content),
                    'sha256': hashlib.sha256(content).hexdigest(), 'window_id': cfg['window_id'],
                    'daemon_epoch': cfg['daemon_epoch'], 'workspace_revision': cfg['revision'],
                    'project_binding': cfg['project_binding'], 'timezone_offset_minutes': 0}, ok=False)
                assert denied['error'] == 'project_required' and not broker._uploads
                denied = await request(ws, 'workspace.set', {'project_dir': str(home),
                    'exchange_dir': None, 'expected_revision': 0}, ok=False)
                assert denied['error'] == 'project_required'
                assert list(home.iterdir()) == []
                project = tmp_path / 'selected-project'; project.mkdir()
                cfg = await request(ws, 'workspace.set', {'project_dir': str(project),
                    'exchange_dir': None, 'expected_revision': 0})
                assert cfg['revision'] == 1 and cfg['exchange_dir'] == str(project / 'tmp')
                assert not (project / 'tmp').exists()
                result = await saved(ws, (await upload(ws, cfg, content))['file_id'])
                assert result['state'] == 'saved', result
                target = Path(result['receipt']['path'])
                assert target.parent == project / 'tmp' and target.read_bytes() == content
                assert result['receipt']['sha256'] == hashlib.sha256(content).hexdigest()
                assert list(home.iterdir()) == [] and not (parents[1].project / 'tmp').exists()
    asyncio.run(exercise())


def test_staged_delivery_rechecks_home_target_before_creating_receiver(tmp_path, monkeypatch):
    home = tmp_path / 'synthetic-home'; home.mkdir()
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    async def exercise():
        async with environment(tmp_path) as (_, url, parents, companions):
            companion = companions[0]
            entered, release = asyncio.Event(), asyncio.Event()
            original = companion._deliver
            async def gated(job, generation):
                entered.set()
                await release.wait()
                return await original(job, generation)
            companion._deliver = gated
            try:
                async with client(url, 'A') as ws:
                    queued = await upload(ws, await workspace(ws))
                    await asyncio.wait_for(entered.wait(), 5)
                    parents[0].project = home
                    await parents[0].publish()
                    await eventually(lambda: companion.inspector.valid.is_set()
                                     and companion.inspector.snapshot('session1')['cwd'] == str(home))
                    release.set()
                    result = await saved(ws, queued['file_id'])
                    assert result['state'] == 'failed' and result['error'] == 'project_required', result
                    assert companion.progress[queued['file_id']]['error'] == 'project_required'
                    assert list(home.iterdir()) == []
                    assert not list(tmp_path.rglob('*.part'))
                    assert not list((tmp_path / 'project-A').rglob('*.pdf'))
            finally:
                release.set()
    asyncio.run(exercise())


def re_name(name):
    import re
    return bool(re.fullmatch(r'\d{8}-\d{6}-document\.pdf', name))


@pytest.mark.parametrize('value', ['http://example.com:1234', 'http://127.0.0.1:1?token=x',
    'http://x@127.0.0.1:1', 'http://127.0.0.1:1/path', 'http://localhost', 'https://127.0.0.1:1'])
def test_parent_endpoint_rejects_remote_credentials_or_ambiguous_paths(value):
    with pytest.raises(CompanionError):
        _endpoint(value, backend=True)


@pytest.mark.parametrize('denial', [False, '', None, 'true'])
def test_modern_owner_denial_never_falls_back(tmp_path, denial):
    async def exercise():
        async with environment(tmp_path) as (_, _, parents, companions):
            parents[0].owner = denial
            await parents[0].publish()
            await eventually(lambda: not companions[0].inspector.valid.is_set())
            with pytest.raises(CompanionError):
                await companions[0].inspector.fresh('session1')
    asyncio.run(exercise())


@pytest.mark.parametrize('denial', ['', None, 123, 'different-owner'])
def test_present_bad_legacy_health_owner_never_uses_file_fallback(tmp_path, denial):
    async def exercise():
        async with environment(tmp_path) as (_, _, parents, companions):
            parents[0].default_token = denial
            with pytest.raises(CompanionError):
                await companions[0].inspector.fresh('session1')
            assert not companions[0].inspector.valid.is_set()
    asyncio.run(exercise())


def test_legacy_absent_health_field_uses_configured_owner_file(tmp_path):
    async def exercise():
        async with environment(tmp_path) as (_, url, parents, companions):
            parents[0].health_present = False
            async with client(url, 'A') as ws:
                assert (await workspace(ws))['session_id'] == 'session1'
    asyncio.run(exercise())


def test_hello_without_multiplex_is_never_accepted(tmp_path):
    async def exercise():
        async with environment(tmp_path) as (_, _, parents, companions):
            parents[0].multiplex = False
            await parents[0].publish()
            await eventually(lambda: not companions[0].inspector.valid.is_set())
    asyncio.run(exercise())


def test_workspace_and_library_changes_are_cas_config_only(tmp_path):
    async def exercise():
        async with environment(tmp_path) as (_, url, parents, _):
            project = tmp_path / 'explicit-project'; project.mkdir()
            library = tmp_path / 'explicit-mylib'; library.mkdir()
            async with client(url, 'A') as ws:
                cfg = await workspace(ws)
                result = await request(ws, 'workspace.set', {'project_dir': str(project), 'exchange_dir': None, 'expected_revision': 0})
                assert result['revision'] == 1 and result['project_dir'] == str(project)
                assert result['exchange_dir'] == str(project / 'tmp')
                assert not (project / 'tmp').exists()
                assert parents[0].project != project  # No terminal cd.
                denied = await request(ws, 'workspace.set', {'project_dir': str(project), 'exchange_dir': None, 'expected_revision': 0}, ok=False)
                assert denied['error'] == 'revision_conflict'
                await request(ws, 'library.set', {'mylib_root': str(library), 'expected_revision': 0})
                assert (await request(ws, 'workspace.get'))['mylib_root'] == str(library)
                denied = await request(ws, 'library.set', {'mylib_root': str(tmp_path / 'absent'), 'expected_revision': 1}, ok=False)
                assert denied['error'] == 'directory_unavailable'
                await request(ws, 'workspace.get', session='not-open', ok=False)
                assert not cfg['exchange_dir'] == result['exchange_dir']
    asyncio.run(exercise())


def test_wrong_room_binding_and_revision_rejected_before_upload(tmp_path):
    async def exercise():
        async with environment(tmp_path) as (_, url, parents, _):
            async with client(url, 'A') as a, client(url, 'B') as b:
                cfg_a, cfg_b = await workspace(a), await workspace(b)
                data = {'original_name': 'wrong.pdf', 'size': 1, 'sha256': hashlib.sha256(b'x').hexdigest(),
                        'window_id': 'window1', 'daemon_epoch': cfg_a['daemon_epoch'],
                        'workspace_revision': cfg_a['revision'], 'timezone_offset_minutes': 0}
                await request(b, 'upload.begin', data, ok=False)
                data['daemon_epoch'] = cfg_b['daemon_epoch']; data['workspace_revision'] = 99
                await request(b, 'upload.begin', data, ok=False)
                assert not (parents[0].project/'tmp').exists() and not (parents[1].project/'tmp').exists()
    asyncio.run(exercise())


@pytest.mark.parametrize('change', ['directory', 'session', 'owner', 'disconnect', 'epoch'])
def test_mid_download_target_change_never_publishes(tmp_path, change):
    async def exercise():
        import subprocess
        import sys
        child = None
        async with environment(tmp_path) as (_, url, parents, companions):
            companion = companions[0]
            entered, release = asyncio.Event(), asyncio.Event()
            original = companion._request
            async def gated(action, session, data, generation):
                value = await original(action, session, data, generation)
                if action in {'download.chunk', 'download.ticket'} and not entered.is_set():
                    entered.set(); await release.wait()
                return value
            companion._request = gated
            try:
                async with client(url, 'A') as ws:
                    cfg = await workspace(ws)
                    queued = await upload(ws, cfg)
                    await asyncio.wait_for(entered.wait(), 5)
                    if change == 'directory':
                        target = tmp_path / 'changed'; target.mkdir()
                        await request(ws, 'workspace.set', {'project_dir': str(target), 'exchange_dir': None, 'expected_revision': 0})
                    elif change == 'session':
                        parents[0].session_open = False; await parents[0].publish()
                    elif change == 'owner':
                        parents[0].owner = False; await parents[0].publish()
                    elif change == 'disconnect':
                        for socket in list(parents[0].sockets): await socket.close()
                        await eventually(lambda: not companion.inspector.valid.is_set())
                    else:
                        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
                        child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'], **options)
                        parents[0].pid = child.pid; await parents[0].publish()
                    if change in {'owner', 'session', 'epoch'}:
                        await asyncio.sleep(.1)
                    release.set()
                    result = await saved(ws, queued['file_id'])
                    assert result['state'] == 'failed', result
                    assert all(not list(parent.project.rglob('*.pdf')) for parent in parents)
                    if change == 'directory':
                        assert not list(target.rglob('*.pdf'))
                    assert not list(tmp_path.rglob('*.part'))
            finally:
                release.set()
                if child:
                    child.terminate();child.wait(timeout=3)
    asyncio.run(exercise())


def test_owner_failure_is_not_terminal_until_partial_cleanup_finishes(tmp_path):
    async def exercise():
        async with environment(tmp_path) as (_, url, parents, companions):
            helper = companions[0]
            ticket_entered, release_ticket = asyncio.Event(), asyncio.Event()
            cleanup_entered, release_cleanup = threading.Event(), threading.Event()
            original_request, original_begin = helper._request, helper._begin
            async def ticket(action, session, data, generation):
                result = await original_request(action, session, data, generation)
                if action == 'download.ticket':
                    ticket_entered.set(); await release_ticket.wait()
                return result
            def begin(*args):
                store, transfer, saved_path = original_begin(*args)
                cancel = store.cancel_owner
                def controlled_cleanup(owner):
                    cleanup_entered.set()
                    assert release_cleanup.wait(5)
                    return cancel(owner)
                store.cancel_owner = controlled_cleanup
                return store, transfer, saved_path
            helper._request, helper._begin = ticket, begin
            try:
                async with client(url, 'A') as ws:
                    queued = await upload(ws, await workspace(ws))
                    await asyncio.wait_for(ticket_entered.wait(), 5)
                    parents[0].owner = False; await parents[0].publish()
                    await eventually(lambda: not helper.inspector.valid.is_set())
                    release_ticket.set()
                    await eventually(cleanup_entered.is_set)
                    # A blocked real receiver cleanup makes the ordering observable.
                    # No timing sleep and no eventual assertion after terminal failure.
                    assert list(parents[0].project.rglob('*.part'))
                    result = await request(ws, 'file.status', {'file_id': queued['file_id']})
                    assert result['state'] not in {'failed', 'saved'}
                    assert helper.progress[queued['file_id']]['state'] != 'failed'
                    release_cleanup.set()
                    result = await saved(ws, queued['file_id'])
                    assert result['state'] == 'failed'
                    assert not list(parents[0].project.rglob('*.part'))
                    assert not list(parents[0].project.rglob('*.pdf'))
            finally:
                release_ticket.set();release_cleanup.set()
    asyncio.run(exercise())


def test_duplicate_delivery_revalidates_and_acks_without_copy(tmp_path):
    async def exercise():
        async with environment(tmp_path) as (broker, url, parents, companions):
            helper = companions[0]
            original = helper._request
            acknowledgements = []
            async def count(action, session, data, generation):
                result = await original(action, session, data, generation)
                if action == 'delivery.ack': acknowledgements.append(data)
                return result
            helper._request = count
            async with client(url, 'A') as ws:
                cfg = await workspace(ws)
                queued = await upload(ws, cfg)
                status = await saved(ws, queued['file_id'])
                path = Path(status['receipt']['path']); before = path.stat()
                await eventually(lambda: len(acknowledgements) == 1 and queued['file_id'] not in helper._deliveries)
                row = broker._db.execute('SELECT * FROM jobs WHERE file_id=?', (queued['file_id'],)).fetchone()
                await broker._delivery(broker._hosts[('owned', 'A')], dict(row))
                await eventually(lambda: len(acknowledgements) == 2)
                assert acknowledgements[0] == acknowledgements[1]
                assert path.stat().st_ino == before.st_ino
                assert len(list(parents[0].project.rglob('*.pdf'))) == 1
    asyncio.run(exercise())


@pytest.mark.parametrize('mutated', [False, True])
def test_helper_restart_replays_journal_when_saved_ack_was_lost(tmp_path, mutated):
    async def exercise():
        async with environment(tmp_path) as (_, url, parents, companions):
            old = companions[0]
            original = old._request
            entered = asyncio.Event()
            async def lose_ack(action, session, data, generation):
                if action == 'delivery.ack':
                    entered.set(); await asyncio.Future()
                return await original(action, session, data, generation)
            old._request = lose_ack
            async with client(url, 'A') as ws:
                cfg = await workspace(ws)
                queued = await upload(ws, cfg)
                await asyncio.wait_for(entered.wait(), 5)
                files = list(parents[0].project.rglob('*.pdf')); assert len(files) == 1
                before = files[0].stat()
                host_id, host_secret = old.identity['host_id'], old.identity['host_secret']
                await old.close(); await asyncio.wait_for(old._fixture_task, 8)
                if mutated: files[0].write_bytes(b'changed by owned user')
                new = FileCompanion(old.backend, url, 'A', old.state_dir, old.owner_token_file,
                    old.enrollment_key_file, broker_token_file=old.broker_token_file,
                    default_mylib_root=old.workspaces.default_mylib_root,
                    download_mode=old.download_mode, download_base_url=old.download_base_url)
                companions[0] = new
                task = asyncio.create_task(new.run()); old._fixture_tasks.append(task)
                result = await saved(ws, queued['file_id'])
                assert result['state'] == ('failed' if mutated else 'saved'), result
                assert new.identity['host_id'] == host_id and new.identity['host_secret'] == host_secret
                assert len(list(parents[0].project.rglob('*.pdf'))) == 1
                if not mutated: assert files[0].stat().st_ino == before.st_ino
                assert not list(parents[0].project.rglob('*.part'))
    asyncio.run(exercise())


def test_broker_disconnect_cleans_partial_and_replays_same_staged_job(tmp_path):
    async def exercise():
        async with environment(tmp_path) as (_, url, parents, companions):
            helper = companions[0]
            original = helper._request
            entered = asyncio.Event()
            async def interrupt(action, session, data, generation):
                value = await original(action, session, data, generation)
                if action in {'download.chunk', 'download.ticket'} and not entered.is_set():
                    entered.set(); await helper._ws.close(); await asyncio.Future()
                return value
            helper._request = interrupt
            async with client(url, 'A') as ws:
                cfg = await workspace(ws)
                queued = await upload(ws, cfg)
                result = await saved(ws, queued['file_id'])
                assert result['state'] == 'saved', result
                assert entered.is_set()
                assert len(list(parents[0].project.rglob('*.pdf'))) == 1
                assert not list(parents[0].project.rglob('*.part'))
    asyncio.run(exercise())


@pytest.mark.parametrize('content', [b'', b'PK\x03\x04' + bytes(range(256)) * 1000], ids=['empty', 'multichunk'])
def test_empty_and_multichunk_office_bytes_preserve_named_extension(tmp_path, content):
    async def exercise():
        async with environment(tmp_path) as (_, url, parents, _):
            async with client(url, 'A') as ws:
                cfg = await workspace(ws)
                queued = await upload(ws, cfg, content, name='\u62a5\u544a.docx')
                status = await saved(ws, queued['file_id'])
                assert status['state'] == 'saved', status
                path = Path(status['receipt']['path'])
                assert path.name.endswith('-\u62a5\u544a.docx') and path.read_bytes() == content
                assert status['receipt']['sha256'] == hashlib.sha256(content).hexdigest()
    asyncio.run(exercise())


def test_corrupt_staged_bytes_rejected_by_target_sha(tmp_path):
    async def exercise():
        async with environment(tmp_path) as (broker, url, parents, companions):
            helper = companions[0]; original = helper._request
            entered = False
            async def corrupt(action, session, data, generation):
                nonlocal entered
                if action in {'download.chunk', 'download.ticket'} and not entered:
                    entered = True
                    row = broker._db.execute('SELECT * FROM jobs WHERE file_id=?', (data['file_id'],)).fetchone()
                    path = broker._job_path(dict(row)); path.write_bytes(b'x' * path.stat().st_size)
                return await original(action, session, data, generation)
            helper._request = corrupt
            async with client(url, 'A') as ws:
                queued = await upload(ws, await workspace(ws))
                assert (await saved(ws, queued['file_id']))['state'] == 'failed'
                assert not list(parents[0].project.rglob('*.pdf'))
                assert not list(parents[0].project.rglob('*.part'))
    asyncio.run(exercise())


def test_lifetime_singleton_and_startup_failure_release_owned_resources(tmp_path):
    async def exercise():
        async with environment(tmp_path) as (_, url, _, companions):
            old = companions[0]
            duplicate = FileCompanion(old.backend, url, 'A', old.state_dir,
                old.owner_token_file, old.enrollment_key_file, broker_token_file=old.broker_token_file)
            with pytest.raises(CompanionError, match='helper_already_running'):
                await duplicate.run()
            assert duplicate.io.pool._shutdown
    asyncio.run(exercise())


def test_directory_browse_and_untrusted_window_override_are_config_only(tmp_path):
    async def exercise():
        async with environment(tmp_path) as (_, url, parents, _):
            child = parents[0].project / 'child';child.mkdir()
            async with client(url, 'A') as ws:
                cfg = await workspace(ws)
                listing = await request(ws, 'workspace.browse', {'path': str(parents[0].project), 'offset': 0, 'limit': 10})
                assert listing['directories'] == [{'name': 'child', 'path': str(child)}]
                result = await request(ws, 'workspace.set', {'window_id': 'another', 'project_dir': str(child), 'exchange_dir': None, 'expected_revision': 0}, ok=False)
                assert result['error'] == 'invalid_request'
                assert (await workspace(ws)) == cfg
    asyncio.run(exercise())


def test_parent_and_health_never_use_environment_proxy(tmp_path, monkeypatch):
    async def exercise():
        contacts = []
        async def unwanted(reader, writer):
            contacts.append(True);writer.close();await writer.wait_closed()
        async with await asyncio.start_server(unwanted, '127.0.0.1', 0) as proxy:
            address = f'http://127.0.0.1:{proxy.sockets[0].getsockname()[1]}'
            for name in ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy']:
                monkeypatch.setenv(name, address)
            monkeypatch.setenv('NO_PROXY', '');monkeypatch.setenv('no_proxy', '')
            async with environment(tmp_path) as (_, url, _, _):
                async with client(url, 'A') as ws:
                    assert (await workspace(ws))['session_id'] == 'session1'
            assert contacts == []
    asyncio.run(exercise())


def test_http_midstream_owner_loss_cancels_verified_partial(tmp_path):
    async def exercise():
        async with environment(tmp_path) as (_, url, parents, companions):
            helper = companions[0]
            original = helper._http_read
            entered, release = threading.Event(), threading.Event()
            calls = 0
            def gated(response, binding, generation):
                nonlocal calls
                calls += 1
                if calls == 2:
                    entered.set(); assert release.wait(8)
                return original(response, binding, generation)
            helper._http_read = gated
            try:
                async with client(url, 'A') as ws:
                    queued = await upload(ws, await workspace(ws), b'%PDF-' + b'fixture-byte' * 20000)
                    await eventually(entered.is_set)
                    assert helper.progress[queued['file_id']]['received'] == 98304
                    parents[0].owner = False; await parents[0].publish()
                    await eventually(lambda: not helper.inspector.valid.is_set())
                    release.set()
                    assert (await saved(ws, queued['file_id']))['state'] == 'failed'
                    assert not list(parents[0].project.rglob('*.pdf'))
                    assert not list(parents[0].project.rglob('*.part'))
            finally:
                release.set()
    asyncio.run(exercise())


@pytest.mark.parametrize('fault', ['redirect', 'arbitrary_path', 'close_while_blocked'])
def test_http_rejects_redirect_and_untrusted_path_and_closes_blocked_read(tmp_path, fault):
    async def exercise():
        async with environment(tmp_path) as (broker, url, parents, companions):
            helper = companions[0]
            contacts = []
            release = threading.Event()
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_): pass
                def do_GET(self):
                    contacts.append(self.path)
                    assert '?' not in self.path and self.headers['Authorization'].startswith('Bearer ')
                    if fault == 'redirect':
                        self.send_response(302);self.send_header('Location', 'http://untrusted.invalid/leak')
                        self.send_header('Content-Length', '0');self.end_headers();return
                    file_id = self.path.rsplit('/', 1)[-1]
                    row = dict(broker._db.execute('SELECT * FROM jobs WHERE file_id=?', (file_id,)).fetchone())
                    data = broker._job_path(row).read_bytes()
                    self.send_response(200);self.send_header('Content-Length', str(len(data)))
                    self.send_header('X-Content-SHA256', row['sha256']);self.end_headers()
                    self.wfile.write(data[:98304]);self.wfile.flush()
                    release.wait(8)
                    try: self.wfile.write(data[98304:])
                    except OSError: pass
            class Server(ThreadingHTTPServer):
                daemon_threads = True
                def handle_error(self, *_): pass
            server = Server(('127.0.0.1', 0), Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True);worker.start()
            helper.download_base_url = f'http://127.0.0.1:{server.server_port}'
            if fault == 'arbitrary_path':
                original = helper._request
                async def corrupt(action, session, data, generation):
                    result = await original(action, session, data, generation)
                    if action == 'download.ticket': result['download_path'] = 'http://untrusted.invalid/leak'
                    return result
                helper._request = corrupt
            try:
                async with client(url, 'A') as ws:
                    queued = await upload(ws, await workspace(ws), b'%PDF-' + b'x' * 250000)
                    if fault == 'close_while_blocked':
                        await eventually(lambda: helper.progress.get(queued['file_id'], {}).get('received') == 98304)
                        await helper.close()
                        await asyncio.wait_for(helper._fixture_task, 3)
                        assert not helper._streams and helper.io.pool._shutdown
                    else:
                        assert (await saved(ws, queued['file_id']))['state'] == 'failed'
                    assert len(contacts) == (0 if fault == 'arbitrary_path' else 1)
                    assert not list(parents[0].project.rglob('*.pdf'))
                    assert not list(parents[0].project.rglob('*.part'))
            finally:
                release.set();server.shutdown();server.server_close();worker.join(timeout=2)
    asyncio.run(exercise())
