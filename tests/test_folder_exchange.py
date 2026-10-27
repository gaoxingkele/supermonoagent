"""Owned real broker/helper folder delivery, with no terminal input."""
import asyncio
import base64
import hashlib
import secrets
from pathlib import Path
import json

import pytest
from websockets.exceptions import ConnectionClosed
from supermonoagent.file_broker import Broker, BrokerError, validate_folder_status_budget, MAX_FRAME
from supermonoagent.file_folders import validate_manifest, manifest_digest
from websockets.asyncio.server import serve
from websockets.asyncio.client import connect

from test_file_companion import environment, client, request, workspace, saved
from test_file_companion import eventually
from supermonoagent.file_companion import FileCompanion, CompanionError


async def send_entry(ws, batch, index, data):
    value = await request(ws, 'folder.file.begin', {'batch_id': batch['batch_id'], 'entry_index': index})
    if data:
        await request(ws, 'upload.chunk', {'upload_id': value['upload_id'], 'offset': 0,
            'data': base64.b64encode(data).decode()})
    return await request(ws, 'upload.finish', {'upload_id': value['upload_id']})


def inventory():
    content = {('nested', 'same.txt'): b'first folder bytes',
               ('other', 'same.txt'): b'second folder bytes',
               ('zero.txt',): b''}
    value = {'version': 1, 'root': 'owned-folder',
             'directories': [['nested'], ['other'], ['empty']],
             'files': [{'path': list(path), 'size': len(data),
                        'sha256': hashlib.sha256(data).hexdigest()}
                       for path, data in content.items()]}
    return value, content


async def begin_folder(ws, cfg, manifest, nonce=None):
    return await request(ws, 'folder.begin', {'nonce': nonce or secrets.token_hex(16),
        'manifest': manifest, 'window_id': cfg['window_id'],
        'daemon_epoch': cfg['daemon_epoch'], 'workspace_revision': cfg['revision'],
        'project_binding': cfg['project_binding'], 'timezone_offset_minutes': 480})


def test_actual_http_folder_preserves_hierarchy_and_empty_directories(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (broker, url, parents, helpers):
            async with client(url, 'A') as ws:
                cfg = await workspace(ws)
                capability = await request(ws, 'folder.capabilities')
                assert capability['version'] == 1
                manifest, content = inventory()
                nonce = secrets.token_hex(16)
                batch = await begin_folder(ws, cfg, manifest, nonce)
                again = await begin_folder(ws, cfg, manifest, nonce)
                assert again['batch_id'] == batch['batch_id']
                root = Path(batch['receipt']['root_path'])
                assert root.parent == parents[0].project / 'tmp'
                assert (root / 'empty').is_dir()
                for index, entry in enumerate(batch['manifest']['files']):
                    data = content[tuple(entry['path'])]
                    opening = await request(ws, 'folder.file.begin', {'batch_id': batch['batch_id'], 'entry_index': index})
                    assert (await request(ws, 'folder.file.begin', {'batch_id': batch['batch_id'], 'entry_index': index})) == opening
                    if data:
                        await request(ws, 'upload.chunk', {'upload_id': opening['upload_id'], 'offset': 0,
                            'data': base64.b64encode(data).decode()})
                    queued = await request(ws, 'upload.finish', {'upload_id': opening['upload_id']})
                    assert (await saved(ws, queued['file_id']))['state'] == 'saved'
                    committed = await request(ws, 'folder.file.begin', {'batch_id': batch['batch_id'], 'entry_index': index})
                    assert committed['file_id'] == queued['file_id'] and 'upload_id' not in committed
                    assert root.joinpath(*entry['path']).read_bytes() == data
                complete = await request(ws, 'folder.finish', {'batch_id': batch['batch_id']})
                assert complete['state'] == 'complete'
                assert (await request(ws, 'folder.lookup', {'nonce': nonce}))['batch_id'] == batch['batch_id']
                assert not parents[0].pty_written
                assert helpers[0].download_mode == 'http'
    asyncio.run(scenario())


def test_actual_http_folder_supports_chinese_and_emoji_names(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (_, url, parents, _):
            async with client(url, 'A') as ws:
                data = b'owned Unicode folder HTTP bytes'
                manifest = {'version': 1, 'root': '资料😀',
                    'directories': [['论文😀'], ['空目录']],
                    'files': [{'path': ['论文😀', '报告😀.pdf'], 'size': len(data),
                               'sha256': hashlib.sha256(data).hexdigest()}]}
                batch = await begin_folder(ws, await workspace(ws), manifest)
                queued = await send_entry(ws, batch, 0, data)
                status = await saved(ws, queued['file_id'])
                assert status['state'] == 'saved'
                root = Path(batch['receipt']['root_path'])
                assert root.parent == parents[0].project/'tmp'
                assert root.name.endswith('-资料😀')
                assert (root/'论文😀'/'报告😀.pdf').read_bytes() == data
                assert (root/'空目录').is_dir()
                assert (await request(ws, 'folder.finish', {'batch_id': batch['batch_id']}))['state'] == 'complete'
    asyncio.run(scenario())


def test_nonce_conflict_cross_room_and_target_change_are_rejected(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (_, url, parents, helpers):
            async with client(url, 'A') as a, client(url, 'B') as b:
                cfg = await workspace(a)
                manifest, _ = inventory(); nonce = secrets.token_hex(16)
                batch = await begin_folder(a, cfg, manifest, nonce)
                other = {**manifest, 'root': 'changed-root'}
                data = {'nonce': nonce, 'manifest': other, 'window_id': cfg['window_id'],
                    'daemon_epoch': cfg['daemon_epoch'], 'workspace_revision': cfg['revision'],
                    'project_binding': cfg['project_binding'], 'timezone_offset_minutes': 480}
                assert (await request(a, 'folder.begin', data, ok=False))['error'] == 'folder_conflict'
                assert (await request(b, 'folder.status', {'batch_id': batch['batch_id']}, ok=False))['error'] == 'unknown_folder'
                assert (await request(a, 'folder.prepare', {}, ok=False))['error'] == 'action_forbidden'
                changed = tmp_path / 'other-project'; changed.mkdir()
                parents[0].project = changed; await parents[0].publish()
                await eventually(lambda: helpers[0].inspector.valid.is_set()
                    and helpers[0].inspector.snapshot('session1')['cwd'] == str(changed))
                assert (await request(a, 'folder.file.begin', {'batch_id': batch['batch_id'], 'entry_index': 0}, ok=False))['error'] in {'unavailable', 'target_changed'}
                assert not list(changed.iterdir())
    asyncio.run(scenario())


def test_host_nonce_cannot_be_reused_for_a_different_batch_id(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (broker, url, parents, helpers):
            (parents[0].project/'tmp').mkdir()
            async with client(url, 'A') as ws:
                manifest, _ = inventory()
                batch = await begin_folder(ws, await workspace(ws), manifest)
                row = broker._db.execute('SELECT request,digest,host_id FROM folder_batches WHERE batch_id=?', (batch['batch_id'],)).fetchone()
                repeated = {**json.loads(row['request']), 'batch_id': secrets.token_hex(24),
                    'manifest_digest': row['digest'], 'host_id': row['host_id']}
                binding = await helpers[0].inspector.fresh('session1')
                with pytest.raises(CompanionError, match='folder_conflict'):
                    await helpers[0].io(helpers[0]._folder_prepare, binding, repeated)
                assert len(list((parents[0].project/'tmp').glob('*owned-folder*'))) == 1
    asyncio.run(scenario())


def test_empty_folder_cannot_complete_without_reconciled_prepare_receipt(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (broker, url, _, _):
            original = broker._route; dropped = False
            async def route(caller, action, session, data):
                nonlocal dropped
                result = await original(caller, action, session, data)
                if action == 'folder.prepare' and not dropped:
                    dropped = True
                    raise BrokerError('route_timeout')
                return result
            broker._route = route
            async with client(url, 'A') as ws:
                cfg = await workspace(ws)
                data = {'nonce': secrets.token_hex(16),
                    'manifest': {'version': 1, 'root': 'empty-folder', 'directories': [], 'files': []},
                    'window_id': cfg['window_id'], 'daemon_epoch': cfg['daemon_epoch'],
                    'workspace_revision': cfg['revision'], 'project_binding': cfg['project_binding'],
                    'timezone_offset_minutes': 480}
                await request(ws, 'folder.begin', data, ok=False)
                uncertain = await request(ws, 'folder.lookup', {'nonce': data['nonce']})
                assert uncertain['state'] == 'preparing' and 'receipt' not in uncertain
                await request(ws, 'folder.finish', {'batch_id': uncertain['batch_id']}, ok=False)
                assert (await request(ws, 'folder.status', {'batch_id': uncertain['batch_id']}))['state'] == 'preparing'
                recovered = await request(ws, 'folder.begin', data)
                assert recovered['state'] == 'ready' and recovered['receipt']['root_path']
                complete = await request(ws, 'folder.finish', {'batch_id': uncertain['batch_id']})
                assert complete['state'] == 'complete' and complete['receipt'] == recovered['receipt']
    asyncio.run(scenario())


def test_host_journal_reopens_same_batch_after_helper_restart(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (broker, url, parents, helpers):
            async with client(url, 'A') as ws:
                manifest, content = inventory(); nonce = secrets.token_hex(16)
                cfg = await workspace(ws)
                batch = await begin_folder(ws, cfg, manifest, nonce)
                first = batch['manifest']['files'][0]
                queued = await send_entry(ws, batch, 0, content[tuple(first['path'])])
                assert (await saved(ws, queued['file_id']))['state'] == 'saved'
                old = helpers[0]
                await old.close(); await old._fixture_task
                await eventually(lambda: ('owned', 'A') not in broker._hosts)
                replacement = FileCompanion(old.backend, old.broker_url, old.room, old.state_dir,
                    old.owner_token_file, old.enrollment_key_file, broker_token_file=old.broker_token_file,
                    default_mylib_root=tmp_path / 'missing-library', download_base_url=old.download_base_url)
                helpers[0] = replacement
                task = asyncio.create_task(replacement.run())
                old._fixture_tasks.append(task)
                replacement._fixture_tasks, replacement._fixture_task = old._fixture_tasks, task
                await eventually(lambda: replacement.inspector.valid.is_set() and ('owned', 'A') in broker._hosts)
                resumed = await begin_folder(ws, cfg, manifest, nonce)
                assert resumed['batch_id'] == batch['batch_id']
                assert resumed['receipt'] == batch['receipt']
                for index in range(1, len(batch['manifest']['files'])):
                    entry = batch['manifest']['files'][index]
                    queued = await send_entry(ws, batch, index, content[tuple(entry['path'])])
                    assert (await saved(ws, queued['file_id']))['state'] == 'saved'
                assert (await request(ws, 'folder.finish', {'batch_id': batch['batch_id']}))['state'] == 'complete'
                assert len(list((parents[0].project / 'tmp').glob('*owned-folder*'))) == 1
    asyncio.run(scenario())


def test_restart_recovers_receipt_after_publication_before_journal_ack(tmp_path):
    import threading
    async def scenario():
        async with environment(tmp_path) as (broker, url, _, helpers):
            old = helpers[0]; original = old._finish; published = threading.Event()
            def interrupted(store, transfer, job, binding, generation):
                receipt = original(store, transfer, job, binding, generation)
                if 'folder' in job:
                    with old._db_lock, old._db:
                        old._db.execute('UPDATE deliveries SET receipt=NULL,partial=? WHERE file_id=?', (transfer, job['file_id']))
                    published.set()
                    # Model a process stopping after hard-link publication but
                    # before receipt/ACK persistence, without marking relay failed.
                    raise asyncio.CancelledError()
                return receipt
            old._finish = interrupted
            async with client(url, 'A') as ws:
                data = b'owned publication recovery bytes'
                manifest = {'version': 1, 'root': 'recover-folder', 'directories': [],
                    'files': [{'path': ['saved.txt'], 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}]}
                batch = await begin_folder(ws, await workspace(ws), manifest)
                queued = await send_entry(ws, batch, 0, data)
                await eventually(lambda: published.is_set() and not old._deliveries)
                target = Path(batch['receipt']['root_path'])/'saved.txt'
                info = target.stat()
                original_identity = info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
                await old.close(); await old._fixture_task
                await eventually(lambda: ('owned', 'A') not in broker._hosts)
                replacement = FileCompanion(old.backend, old.broker_url, old.room, old.state_dir,
                    old.owner_token_file, old.enrollment_key_file, broker_token_file=old.broker_token_file,
                    default_mylib_root=tmp_path/'missing-library', download_base_url=old.download_base_url)
                helpers[0] = replacement
                task = asyncio.create_task(replacement.run()); old._fixture_tasks.append(task)
                replacement._fixture_tasks, replacement._fixture_task = old._fixture_tasks, task
                await eventually(lambda: replacement.inspector.valid.is_set() and ('owned', 'A') in broker._hosts)
                assert (await saved(ws, queued['file_id']))['state'] == 'saved'
                complete = await request(ws, 'folder.finish', {'batch_id': batch['batch_id']})
                assert complete['state'] == 'complete'
                assert target.read_bytes() == data
                info = target.stat()
                assert (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) == original_identity
                assert list(target.parent.glob('saved*.txt')) == [target]
    asyncio.run(scenario())


def test_folder_reservations_count_against_ordinary_upload_quota(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (broker, url, _, _):
            async with client(url, 'A') as ws:
                manifest, _ = inventory()
                total = sum(item['size'] for item in manifest['files'])
                broker.max_total_bytes = total
                batch = await begin_folder(ws, await workspace(ws), manifest)
                assert broker._folder_reserved() == (total, len(manifest['files']))
                cfg = await workspace(ws)
                result = await request(ws, 'upload.begin', {'original_name': 'extra.txt', 'size': 1,
                    'sha256': hashlib.sha256(b'x').hexdigest(), 'window_id': cfg['window_id'],
                    'daemon_epoch': cfg['daemon_epoch'], 'workspace_revision': cfg['revision'],
                    'project_binding': cfg['project_binding'], 'timezone_offset_minutes': 0}, ok=False)
                assert result['error'] == 'quota_exceeded'
                opening = await request(ws, 'folder.file.begin', {'batch_id': batch['batch_id'], 'entry_index': 0})
                assert 'upload_id' in opening
                size = batch['manifest']['files'][0]['size']
                assert broker._folder_reserved() == (total - size, len(manifest['files']) - 1)
                await request(ws, 'upload.cancel', {'upload_id': opening['upload_id']})
                assert broker._folder_reserved() == (total, len(manifest['files']))
    asyncio.run(scenario())


def test_lost_prepare_ack_is_queryable_and_reuses_host_reservation(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (broker, url, parents, _):
            original = broker._route; dropped = False
            async def route(caller, action, session, data):
                nonlocal dropped
                result = await original(caller, action, session, data)
                if action == 'folder.prepare' and not dropped:
                    dropped = True
                    raise BrokerError('route_timeout')
                return result
            broker._route = route
            async with client(url, 'A') as ws:
                cfg = await workspace(ws); manifest, _ = inventory(); nonce = secrets.token_hex(16)
                data = {'nonce': nonce, 'manifest': manifest, 'window_id': cfg['window_id'],
                    'daemon_epoch': cfg['daemon_epoch'], 'workspace_revision': cfg['revision'],
                    'project_binding': cfg['project_binding'], 'timezone_offset_minutes': 480}
                assert (await request(ws, 'folder.begin', data, ok=False))['error'] == 'route_timeout'
                unknown = await request(ws, 'folder.lookup', {'nonce': nonce})
                assert unknown['state'] == 'preparing'
                resumed = await request(ws, 'folder.begin', data)
                assert resumed['batch_id'] == unknown['batch_id'] and resumed['state'] == 'ready'
                assert len(list((parents[0].project / 'tmp').glob('*owned-folder*'))) == 1
    asyncio.run(scenario())


def test_lost_upload_finish_ack_reconciles_entry_without_second_job(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (broker, url, _, _):
            original = broker._send
            async def send(connection, body):
                if connection.role == 'client' and body.get('id') == 'owned-lost-finish':
                    await connection.ws.close(1012, 'owned lost acknowledgment')
                    return
                await original(connection, body)
            broker._send = send
            manifest, content = inventory(); nonce = secrets.token_hex(16)
            async with client(url, 'A') as ws:
                batch = await begin_folder(ws, await workspace(ws), manifest, nonce)
                entry = batch['manifest']['files'][0]
                opening = await request(ws, 'folder.file.begin', {'batch_id': batch['batch_id'], 'entry_index': 0})
                await request(ws, 'upload.chunk', {'upload_id': opening['upload_id'], 'offset': 0,
                    'data': base64.b64encode(content[tuple(entry['path'])]).decode()})
                await ws.send(json.dumps({'type': 'request', 'id': 'owned-lost-finish', 'action': 'upload.finish',
                    'session': 'session1', 'data': {'upload_id': opening['upload_id']}}))
                with pytest.raises(ConnectionClosed):
                    await ws.recv()
            async with client(url, 'A') as ws:
                status = await request(ws, 'folder.lookup', {'nonce': nonce})
                file_id = status['entries'][0]['file_id']
                assert (await saved(ws, file_id))['state'] == 'saved'
                replay = await request(ws, 'folder.file.begin', {'batch_id': batch['batch_id'], 'entry_index': 0})
                assert replay['file_id'] == file_id and 'upload_id' not in replay
                assert broker._db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
    asyncio.run(scenario())


def test_unknown_preparation_can_be_cancelled_without_touching_foreign_root(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (broker, url, parents, helpers):
            original = helpers[0]._folder_prepare
            def prepare(binding, data):
                result = original(binding, data)
                with helpers[0]._db_lock, helpers[0]._db:
                    helpers[0]._db.execute("UPDATE folders SET state='preparing',snapshot=NULL WHERE batch_id=?", (data['batch_id'],))
                helpers[0]._folders.pop(data['batch_id']).close()
                raise ValueError('owned simulated interrupted preparation')
            helpers[0]._folder_prepare = prepare
            async with client(url, 'A') as ws:
                cfg = await workspace(ws); manifest, _ = inventory(); nonce = secrets.token_hex(16)
                data = {'nonce': nonce, 'manifest': manifest, 'window_id': cfg['window_id'],
                    'daemon_epoch': cfg['daemon_epoch'], 'workspace_revision': cfg['revision'],
                    'project_binding': cfg['project_binding'], 'timezone_offset_minutes': 480}
                await request(ws, 'folder.begin', data, ok=False)
                unknown = await request(ws, 'folder.lookup', {'nonce': nonce})
                roots = list((parents[0].project / 'tmp').glob('*owned-folder*'))
                assert len(roots) == 1
                (roots[0] / 'sentinel').write_text('retain')
                await request(ws, 'folder.begin', data, ok=False)
                assert len(list((parents[0].project / 'tmp').glob('*owned-folder*'))) == 1
                cancelled = await request(ws, 'folder.cancel', {'batch_id': unknown['batch_id']})
                assert cancelled['state'] == 'cancelled' and broker._folder_reserved() == (0, 0)
                assert (roots[0] / 'sentinel').read_text() == 'retain'
    asyncio.run(scenario())


def test_full_saved_status_budget_handles_actual_maximum_entry_amplification():
    manifest = validate_manifest({'version': 1, 'root': 'folder',
        'directories': [['nested']],
        'files': [{'path': ['nested', 'file-'+str(index)+'.txt'], 'size': 1,
                   'sha256': hashlib.sha256(b'x').hexdigest()} for index in range(32)]})
    target = {'daemon_epoch': 'e'*64, 'workspace_revision': 0, 'project_binding': 'b'*64}
    digest = manifest_digest(manifest)
    def receipt(units):
        # An absolute Windows path with bounded individual components, without
        # allocating a real directory or exceeding the OS's own path settings.
        prefix = 'C:\\' + ('a'*100+'\\') * (units//101) + 'a'*(units%101)
        return {'batch_id': 'f'*48, 'manifest_digest': digest, 'root_path': prefix+'\\root', 'name': 'root'}
    assert validate_folder_status_budget(manifest, target, receipt(100)) < MAX_FRAME-2048
    low, high = 0, 4080
    while low+1 < high:
        middle = (low+high)//2
        try:
            validate_folder_status_budget(manifest, target, receipt(middle))
        except ValueError:
            high = middle
        else:
            low = middle
    size = validate_folder_status_budget(manifest, target, receipt(low))
    assert size <= MAX_FRAME-2048
    with pytest.raises(ValueError):
        validate_folder_status_budget(manifest, target, receipt(high))


def test_budget_rejection_precedes_host_reservation_and_directory_creation(tmp_path, monkeypatch):
    import supermonoagent.file_companion as module
    def too_large(*_):
        raise ValueError('Owned budget failure')
    async def scenario():
        async with environment(tmp_path) as (_, url, parents, helpers):
            monkeypatch.setattr(module, 'validate_folder_status_budget', too_large)
            async with client(url, 'A') as ws:
                cfg = await workspace(ws); manifest, _ = inventory()
                data = {'nonce': secrets.token_hex(16), 'manifest': manifest, 'window_id': cfg['window_id'],
                    'daemon_epoch': cfg['daemon_epoch'], 'workspace_revision': cfg['revision'],
                    'project_binding': cfg['project_binding'], 'timezone_offset_minutes': 480}
                await request(ws, 'folder.begin', data, ok=False)
                assert not (parents[0].project / 'tmp').exists()
                assert helpers[0]._db.execute('SELECT COUNT(*) FROM folders').fetchone()[0] == 0
    asyncio.run(scenario())


def test_broker_and_helper_restart_preserve_batch_and_committed_entry(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (broker, url, parents, helpers):
            manifest, content = inventory(); nonce = secrets.token_hex(16)
            async with client(url, 'A') as ws:
                cfg = await workspace(ws)
                batch = await begin_folder(ws, cfg, manifest, nonce)
                first = batch['manifest']['files'][0]
                committed = await send_entry(ws, batch, 0, content[tuple(first['path'])])
                assert (await saved(ws, committed['file_id']))['state'] == 'saved'
                await request(ws, 'folder.file.begin', {'batch_id': batch['batch_id'], 'entry_index': 1})
            await eventually(lambda: not broker._uploads)
            old = helpers[0]
            await old.close(); await old._fixture_task
            await broker.close()
            restarted = Broker(broker.root, broker._auth_file)
            _, address = restarted.start_http()
            try:
                async with serve(restarted.handle_socket, '127.0.0.1', 0, max_size=262144) as server:
                    new_url = 'ws://127.0.0.1:'+str(server.sockets[0].getsockname()[1])+'/file/ws'
                    replacement = FileCompanion(old.backend, new_url, old.room, old.state_dir,
                        old.owner_token_file, old.enrollment_key_file, broker_token_file=old.broker_token_file,
                        default_mylib_root=tmp_path/'missing-library',
                        download_base_url='http://127.0.0.1:'+str(address[1]))
                    helpers[0] = replacement
                    task = asyncio.create_task(replacement.run()); old._fixture_tasks.append(task)
                    replacement._fixture_tasks, replacement._fixture_task = old._fixture_tasks, task
                    await eventually(lambda: replacement.inspector.valid.is_set() and ('owned', 'A') in restarted._hosts)
                    async with client(new_url, 'A') as ws:
                        resumed = await begin_folder(ws, cfg, manifest, nonce)
                        assert resumed['batch_id'] == batch['batch_id'] and resumed['receipt'] == batch['receipt']
                        old_entry = await request(ws, 'folder.file.begin', {'batch_id': batch['batch_id'], 'entry_index': 0})
                        assert old_entry['file_id'] == committed['file_id'] and 'upload_id' not in old_entry
                        for index in range(1, len(batch['manifest']['files'])):
                            entry = batch['manifest']['files'][index]
                            queued = await send_entry(ws, batch, index, content[tuple(entry['path'])])
                            assert (await saved(ws, queued['file_id']))['state'] == 'saved'
                        assert (await request(ws, 'folder.finish', {'batch_id': batch['batch_id']}))['state'] == 'complete'
                        assert restarted._db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 3
                        assert len(list((parents[0].project/'tmp').glob('*owned-folder*'))) == 1
            finally:
                await restarted.close()
    asyncio.run(scenario())


def test_owner_scope_and_pinned_host_identity_cannot_be_substituted(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (broker, url, _, _):
            async with client(url, 'A') as ws:
                manifest, _ = inventory()
                batch = await begin_folder(ws, await workspace(ws), manifest)
                other_token = 'owned-other-token'
                digest = hashlib.sha256(other_token.encode()).hexdigest()
                broker.tokens[digest] = 'other-owner'
                broker.owners['other-owner'] = {'rooms': frozenset({'A'}), 'key': 'other-enrollment-secret'}
                async with connect(url, proxy=None) as other:
                    await other.send(json.dumps({'protocol': 'sma-files-1', 'file_protocol': 2, 'role': 'client',
                        'room': 'A', 'token': other_token}))
                    assert json.loads(await other.recv())['type'] == 'ready'
                    result = await request(other, 'folder.status', {'batch_id': batch['batch_id']}, ok=False)
                    assert result['error'] == 'unknown_folder'
                with broker._lock, broker._db:
                    broker._db.execute("UPDATE folder_batches SET host_id=? WHERE batch_id=?", ('f'*32, batch['batch_id']))
                result = await request(ws, 'folder.file.begin', {'batch_id': batch['batch_id'], 'entry_index': 0}, ok=False)
                assert result['error'] == 'target_changed'
                assert not broker._uploads
    asyncio.run(scenario())


@pytest.mark.parametrize('limit,value', [('max_folder_batches', True), ('max_folder_batches', 1025),
    ('max_room_folder_batches', 0), ('max_room_folder_batches', 65),
    ('max_active_folder_batches', 0), ('max_active_folder_batches', 9)])
def test_folder_constructor_limits_are_bounded(tmp_path, limit, value):
    with pytest.raises(ValueError):
        Broker(tmp_path/'unused', tmp_path/'unused-auth', **{limit: value})


def test_active_and_retained_batch_limits_are_distinct(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (broker, url, parents, _):
            broker.max_active_folder_batches = 1
            broker.max_room_folder_batches = 2
            empty = {'version': 1, 'root': 'empty-folder', 'directories': [], 'files': []}
            async with client(url, 'A') as ws:
                batch = await begin_folder(ws, await workspace(ws), empty)
                cfg = await workspace(ws)
                data = {'nonce': secrets.token_hex(16), 'manifest': empty, 'window_id': cfg['window_id'],
                    'daemon_epoch': cfg['daemon_epoch'], 'workspace_revision': cfg['revision'],
                    'project_binding': cfg['project_binding'], 'timezone_offset_minutes': 480}
                assert (await request(ws, 'folder.begin', data, ok=False))['error'] == 'quota_exceeded'
                await request(ws, 'folder.cancel', {'batch_id': batch['batch_id']})
                next_batch = await request(ws, 'folder.begin', data)
                assert next_batch['state'] == 'ready'
                await request(ws, 'folder.cancel', {'batch_id': next_batch['batch_id']})
                data['nonce'] = secrets.token_hex(16)
                assert (await request(ws, 'folder.begin', data, ok=False))['error'] == 'quota_exceeded'
                assert len(list((parents[0].project/'tmp').glob('*empty-folder*'))) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize('phase', ['ticket', 'ack'])
def test_folder_reference_is_bound_to_download_ticket_and_ack(tmp_path, phase):
    async def scenario():
        async with environment(tmp_path) as (_, url, _, helpers):
            helper = helpers[0]; original = helper._request
            async def intercept(action, session, data, generation):
                if phase == 'ack' and action == 'delivery.ack' and 'folder' in data:
                    data = {**data, 'folder': {**data['folder'], 'manifest_digest': 'f'*64}}
                result = await original(action, session, data, generation)
                if phase == 'ticket' and action == 'download.ticket' and 'folder' in result:
                    result = {**result, 'folder': {**result['folder'], 'manifest_digest': 'f'*64}}
                return result
            helper._request = intercept
            async with client(url, 'A') as ws:
                manifest, content = inventory()
                batch = await begin_folder(ws, await workspace(ws), manifest)
                entry = batch['manifest']['files'][0]
                queued = await send_entry(ws, batch, 0, content[tuple(entry['path'])])
                result = await saved(ws, queued['file_id'])
                assert result['state'] == 'failed'
                target = Path(batch['receipt']['root_path']).joinpath(*entry['path'])
                if phase == 'ticket':
                    assert not target.exists()
                else:
                    assert target.read_bytes() == content[tuple(entry['path'])]
                replay = await request(ws, 'folder.file.begin', {'batch_id': batch['batch_id'], 'entry_index': 0})
                assert replay['file_id'] == queued['file_id'] and replay['state'] == 'failed'
                assert 'upload_id' not in replay
    asyncio.run(scenario())


def test_cancel_preserves_saved_files_stops_uncommitted_and_releases_reservations(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (broker, url, _, _):
            async with client(url, 'A') as ws:
                manifest, content = inventory()
                batch = await begin_folder(ws, await workspace(ws), manifest)
                first = batch['manifest']['files'][0]
                queued = await send_entry(ws, batch, 0, content[tuple(first['path'])])
                status = await saved(ws, queued['file_id']); assert status['state'] == 'saved'
                existing = Path(status['receipt']['path'])
                opening = await request(ws, 'folder.file.begin', {'batch_id': batch['batch_id'], 'entry_index': 1})
                cancelled = await request(ws, 'folder.cancel', {'batch_id': batch['batch_id']})
                assert cancelled['state'] == 'cancelled'
                assert not broker._uploads
                result = await request(ws, 'upload.finish', {'upload_id': opening['upload_id']}, ok=False)
                assert result['error'] == 'unknown_upload'
                assert (await request(ws, 'folder.file.begin', {'batch_id': batch['batch_id'], 'entry_index': 2}, ok=False))['error'] == 'folder_unavailable'
                assert existing.read_bytes() == content[tuple(first['path'])]
                assert broker._folder_reserved() == (0, 0)
                assert not broker._uploads
    asyncio.run(scenario())


def test_reconnect_does_not_duplicate_committed_or_incomplete_uploads(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (broker, url, _, _):
            manifest, content = inventory(); nonce = secrets.token_hex(16)
            async with client(url, 'A') as ws:
                cfg = await workspace(ws)
                batch = await begin_folder(ws, cfg, manifest, nonce)
                first = batch['manifest']['files'][0]
                queued = await send_entry(ws, batch, 0, content[tuple(first['path'])])
                await saved(ws, queued['file_id'])
                await request(ws, 'folder.file.begin', {'batch_id': batch['batch_id'], 'entry_index': 1})
            await eventually(lambda: not broker._uploads)
            async with client(url, 'A') as ws:
                resumed = await begin_folder(ws, cfg, manifest, nonce)
                assert resumed['batch_id'] == batch['batch_id']
                old = await request(ws, 'folder.file.begin', {'batch_id': batch['batch_id'], 'entry_index': 0})
                assert old['file_id'] == queued['file_id'] and 'upload_id' not in old
                fresh = await request(ws, 'folder.file.begin', {'batch_id': batch['batch_id'], 'entry_index': 1})
                assert 'upload_id' in fresh
                assert broker._db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
                assert len(list(Path(batch['receipt']['root_path']).parent.glob('*owned-folder*'))) == 1
    asyncio.run(scenario())


def test_prepublication_folder_replacement_cannot_redirect_file(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (_, url, _, helpers):
            helper = helpers[0]; original = helper._http_open
            def replace(job, ticket, binding, generation):
                if 'folder' in job:
                    store, _ = helper._folder_store(job['folder']['batch_id'])
                    empty = store.root / 'empty'
                    empty.rename(empty.with_name('retired-directory')); empty.mkdir()
                    (empty / 'foreign').write_text('retain')
                return original(job, ticket, binding, generation)
            helper._http_open = replace
            async with client(url, 'A') as ws:
                manifest, content = inventory()
                batch = await begin_folder(ws, await workspace(ws), manifest)
                entry = batch['manifest']['files'][0]
                queued = await send_entry(ws, batch, 0, content[tuple(entry['path'])])
                status = await saved(ws, queued['file_id'])
                assert status['state'] == 'failed'
                root = Path(batch['receipt']['root_path'])
                assert not root.joinpath(*entry['path']).exists()
                assert (root / 'empty' / 'foreign').read_text() == 'retain'
    asyncio.run(scenario())


def test_concurrent_file_begins_persist_a_complete_partial_identity_inventory(tmp_path):
    import threading
    async def scenario():
        async with environment(tmp_path) as (_, url, _, helpers):
            helper = helpers[0]; original = helper._begin_folder_locked
            entered, release, second = threading.Event(), threading.Event(), threading.Event()
            def gated(job, binding):
                if job['folder']['entry_index'] == 0:
                    entered.set(); assert release.wait(5)
                elif job['folder']['entry_index'] == 1:
                    second.set()
                return original(job, binding)
            helper._begin_folder_locked = gated
            try:
                async with client(url, 'A') as ws:
                    manifest, content = inventory()
                    batch = await begin_folder(ws, await workspace(ws), manifest)
                    uploads = []
                    for index, entry in enumerate(batch['manifest']['files'][:2]):
                        opening = await request(ws, 'folder.file.begin', {'batch_id': batch['batch_id'], 'entry_index': index})
                        await request(ws, 'upload.chunk', {'upload_id': opening['upload_id'], 'offset': 0,
                            'data': base64.b64encode(content[tuple(entry['path'])]).decode()})
                        uploads.append(opening['upload_id'])
                    one = await request(ws, 'upload.finish', {'upload_id': uploads[0]})
                    await eventually(entered.is_set)
                    two = await request(ws, 'upload.finish', {'upload_id': uploads[1]})
                    await asyncio.sleep(.05)
                    assert not second.is_set()
                    release.set()
                    assert (await saved(ws, one['file_id']))['state'] == 'saved'
                    assert (await saved(ws, two['file_id']))['state'] == 'saved'
                    with helper._db_lock:
                        snapshot = json.loads(helper._db.execute('SELECT snapshot FROM folders WHERE batch_id=?', (batch['batch_id'],)).fetchone()[0])
                    assert {tuple(item['path']) for item in snapshot['partials']} == {('nested',), ('other',)}
                    helper._folders.pop(batch['batch_id']).close()
                    reopened, _ = helper._folder_store(batch['batch_id'])
                    assert reopened.check()
            finally:
                release.set()
    asyncio.run(scenario())


def test_persisted_partial_directory_replacement_blocks_next_entry(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (broker, url, _, _):
            async with client(url, 'A') as ws:
                content = b'owned partial identity bytes'
                manifest = {'version': 1, 'root': 'folder', 'directories': [['same']],
                    'files': [{'path': ['same', name], 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
                              for name in ('first.txt', 'second.txt')]}
                batch = await begin_folder(ws, await workspace(ws), manifest)
                queued = await send_entry(ws, batch, 0, content)
                assert (await saved(ws, queued['file_id']))['state'] == 'saved'
                partials = Path(batch['receipt']['root_path'])/'same'/'.supermono-partials'
                # The completed file has no open receiving handle. This is the
                # real Windows reconnect boundary at which replacement is possible.
                partials.rename(partials.with_name('retired-partials')); partials.mkdir()
                (partials/'foreign').write_text('retain')
                result = await request(ws, 'folder.file.begin', {'batch_id': batch['batch_id'], 'entry_index': 1}, ok=False)
                assert result['error'] == 'directory_unavailable'
                assert (partials/'foreign').read_text() == 'retain'
                assert not partials.parent.joinpath('second.txt').exists()
                assert broker._db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
    asyncio.run(scenario())


def test_cancel_before_host_reservation_prevents_late_root_and_quota_leak(tmp_path):
    import threading
    async def scenario():
        async with environment(tmp_path) as (broker, url, parents, helpers):
            helper = helpers[0]; original = helper._folder_prepare
            entered, release = threading.Event(), threading.Event()
            def held(binding, data):
                entered.set(); assert release.wait(10)
                return original(binding, data)
            helper._folder_prepare = held
            pending = None
            try:
                async with client(url, 'A') as first, client(url, 'A') as second:
                    cfg = await workspace(first); manifest, _ = inventory()
                    pending = asyncio.create_task(begin_folder(first, cfg, manifest))
                    await eventually(entered.is_set)
                    batch_id = broker._db.execute('SELECT batch_id FROM folder_batches').fetchone()[0]
                    cancelled = await request(second, 'folder.cancel', {'batch_id': batch_id})
                    assert cancelled['state'] == 'cancelled'
                    assert helper._db.execute('SELECT COUNT(*) FROM folder_cancellations').fetchone()[0] == 1
                    release.set()
                    assert (await pending)['state'] == 'cancelled'
                    assert helper._db.execute("SELECT COUNT(*) FROM folders WHERE state IN ('preparing','ready')").fetchone()[0] == 0
                    assert not (parents[0].project/'tmp').exists()
                    assert broker._db.execute('SELECT cancel_pending FROM folder_batches WHERE batch_id=?', (batch_id,)).fetchone()[0] == 0
            finally:
                release.set()
                if pending is not None and not pending.done():
                    await asyncio.gather(pending, return_exceptions=True)
    asyncio.run(scenario())


def test_second_connection_cancel_retires_only_selected_batch_uploads(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (broker, url, _, _):
            async with client(url, 'A') as first, client(url, 'A') as second:
                manifest, content = inventory()
                one = await begin_folder(first, await workspace(first), manifest)
                two = await begin_folder(first, await workspace(first), {**manifest, 'root': 'other-folder'})
                opening_one = await request(first, 'folder.file.begin', {'batch_id': one['batch_id'], 'entry_index': 0})
                opening_two = await request(first, 'folder.file.begin', {'batch_id': two['batch_id'], 'entry_index': 0})
                await request(second, 'folder.cancel', {'batch_id': one['batch_id']})
                assert opening_one['upload_id'] not in broker._uploads
                assert opening_two['upload_id'] in broker._uploads
                assert not broker._store('owned', 'A')._transfers.get(opening_one['upload_id'])
                entry = two['manifest']['files'][0]
                await request(first, 'upload.chunk', {'upload_id': opening_two['upload_id'], 'offset': 0,
                    'data': base64.b64encode(content[tuple(entry['path'])]).decode()})
                job = await request(first, 'upload.finish', {'upload_id': opening_two['upload_id']})
                assert (await saved(first, job['file_id']))['state'] == 'saved'
                assert Path(one['receipt']['root_path']).is_dir()
    asyncio.run(scenario())


@pytest.mark.parametrize('offline', [False, True])
def test_pending_cancel_replays_after_reconnect_even_with_closed_session(tmp_path, offline):
    async def scenario():
        async with environment(tmp_path) as (broker, url, parents, helpers):
            old = helpers[0]; original = broker._route; dropped = False
            async def route(caller, action, session, data):
                nonlocal dropped
                result = await original(caller, action, session, data)
                if action == 'folder.cancel' and not dropped and not offline:
                    dropped = True
                    raise BrokerError('route_timeout')
                return result
            broker._route = route
            async with client(url, 'A') as ws:
                manifest, _ = inventory()
                batch = await begin_folder(ws, await workspace(ws), manifest)
                if offline:
                    await old.close(); await old._fixture_task
                    await eventually(lambda: ('owned', 'A') not in broker._hosts)
                assert (await request(ws, 'folder.cancel', {'batch_id': batch['batch_id']}))['state'] == 'cancelled'
                assert broker._db.execute('SELECT cancel_pending FROM folder_batches WHERE batch_id=?', (batch['batch_id'],)).fetchone()[0] == 1
                parents[0].session_open = False
                await parents[0].publish()
                if not offline:
                    await old.close(); await old._fixture_task
                    await eventually(lambda: ('owned', 'A') not in broker._hosts)
                replacement = FileCompanion(old.backend, old.broker_url, old.room, old.state_dir,
                    old.owner_token_file, old.enrollment_key_file, broker_token_file=old.broker_token_file,
                    default_mylib_root=tmp_path/'missing-library', download_base_url=old.download_base_url)
                helpers[0] = replacement
                task = asyncio.create_task(replacement.run()); old._fixture_tasks.append(task)
                replacement._fixture_tasks, replacement._fixture_task = old._fixture_tasks, task
                await eventually(lambda: ('owned', 'A') in broker._hosts)
                await eventually(lambda: broker._db.execute('SELECT cancel_pending FROM folder_batches WHERE batch_id=?', (batch['batch_id'],)).fetchone()[0] == 0)
                assert replacement._db.execute('SELECT state FROM folders WHERE batch_id=?', (batch['batch_id'],)).fetchone()[0] == 'cancelled'
                assert replacement._db.execute('SELECT COUNT(*) FROM folder_cancellations WHERE batch_id=?', (batch['batch_id'],)).fetchone()[0] == 1
                assert Path(batch['receipt']['root_path']).is_dir()
    asyncio.run(scenario())


def test_cancel_keeps_previously_committed_delivery_eligible(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (_, url, _, helpers):
            helper = helpers[0]; original = helper._deliver
            entered, release = asyncio.Event(), asyncio.Event()
            async def held(job, generation):
                entered.set(); await release.wait()
                await original(job, generation)
            helper._deliver = held
            try:
                async with client(url, 'A') as ws:
                    manifest, content = inventory()
                    batch = await begin_folder(ws, await workspace(ws), manifest)
                    entry = batch['manifest']['files'][0]
                    queued = await send_entry(ws, batch, 0, content[tuple(entry['path'])])
                    await asyncio.wait_for(entered.wait(), 5)
                    await request(ws, 'folder.cancel', {'batch_id': batch['batch_id']})
                    release.set()
                    status = await saved(ws, queued['file_id'])
                    assert status['state'] == 'saved'
                    assert Path(status['receipt']['path']).read_bytes() == content[tuple(entry['path'])]
                    assert (await request(ws, 'folder.status', {'batch_id': batch['batch_id']}))['state'] == 'cancelled'
            finally:
                release.set()
    asyncio.run(scenario())


def test_full_tombstone_journal_keeps_authoritative_cancel_pending(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (broker, url, _, helpers):
            async with client(url, 'A') as ws:
                manifest, _ = inventory()
                batch = await begin_folder(ws, await workspace(ws), manifest)
                helper = helpers[0]
                with helper._db_lock, helper._db:
                    helper._db.executemany('INSERT INTO folder_cancellations VALUES(?,?,?)',
                        [(format(index, '048x'), 'session1', 'f'*64) for index in range(64)])
                assert (await request(ws, 'folder.cancel', {'batch_id': batch['batch_id']}))['state'] == 'cancelled'
                assert broker._db.execute('SELECT cancel_pending FROM folder_batches WHERE batch_id=?', (batch['batch_id'],)).fetchone()[0] == 1
                assert helper._db.execute('SELECT COUNT(*) FROM folder_cancellations').fetchone()[0] == 64
                assert helper._db.execute('SELECT state FROM folders WHERE batch_id=?', (batch['batch_id'],)).fetchone()[0] == 'ready'
                assert Path(batch['receipt']['root_path']).is_dir()
    asyncio.run(scenario())
