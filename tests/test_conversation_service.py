import asyncio
import base64
import copy
import hashlib
import json
import time

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from supermonoagent.conversation_contract import PROTOCOL, batch_digest
from supermonoagent.conversation_service import JournalService, source_proof, enrollment_proof


def auth_config(path, tokens=None):
    tokens = tokens or {'ownerA': ('token-A', ['room']), 'ownerB': ('token-B', ['room'])}
    path.write_text(json.dumps({'schema': 1, 'owners': [
        {'scope': scope, 'token_sha256': [hashlib.sha256(token.encode()).hexdigest()],
         'rooms': rooms, 'enrollment_key': scope * 8}
        for scope, (token, rooms) in tokens.items()]}))


def hello(role='viewer', token='token-A', **extra):
    return dict(op='hello', protocol=PROTOCOL, role=role, token=token, room='room', **extra)


def source_hello(nonce='nonce1', host='host1', secret='private-host-secret', **extra):
    stamp = int(time.time())
    return hello('source', host_installation_id=host, host_secret=secret,
                 timestamp=stamp, nonce=nonce,
                 proof=source_proof(secret, 'room', host, stamp, nonce),
                 enrollment_proof=enrollment_proof('ownerA' * 8, 'room', host, secret, stamp, nonce), **extra)


async def request(ws, action, params=None, request_id='r1'):
    await ws.send(json.dumps(dict(op='request', request_id=request_id, action=action, params=params or {})))
    return json.loads(await ws.recv())


def fixture_batch(body=b'hello', **changes):
    digest = hashlib.sha256(body).hexdigest()
    event = dict(event_id='event1', seq=1, kind='assistant_final', actor='assistant', origin='provider',
                 binding_id='binding1', provider_thread_id='thread1', provider_turn_id='turn1',
                 provider_item_id='item1', source_record_id='record1', source_created_at='2026-01-01',
                 observed_at='2026-01-01', state='confirmed', proof='provider_final_phase',
                 supersedes_event_id=None, metadata={}, content_id='content1', content_bytes=len(body),
                 content_sha256=digest, encoding='utf-8', preview='hello')
    batch = dict(destination_id='dest1', owner_scope='ownerA', external_room='room',
                 host_installation_id='host1', journal_id='journal1', source_epoch='epoch1',
                 batch_id='batch1', first_seq=1, last_seq=1, events=[event],
                 contents=[dict(content_id='content1', bytes=len(body), sha256=digest)])
    batch.update(changes)
    batch['batch_sha256'] = batch_digest(batch)
    journal = dict(schema_version=1, journal_id='journal1', owner_scope='ownerA',
                   host_installation_id='host1', source_epoch='epoch1', window_id='window1',
                   session_id='session1', process_generation='generation1', room='room', title='Fixture',
                   agent='fake', created_at='2026-01-01', capture_started_at='2026-01-01', coverage='full',
                   terminal_state='ended', capture_state='sealed', terminal_ended_at='2026-01-01',
                   sealed_at='2026-01-01', last_seq=1, local_error=None, mirror={})
    return batch, journal


async def stage(ws, body, offset=0, chunk=None):
    return await request(ws, 'content.stage', dict(journal_id='journal1', content_id='content1',
        offset=offset, total_bytes=len(body), sha256=hashlib.sha256(body).hexdigest(),
        data_base64=base64.b64encode(body[offset:] if chunk is None else chunk).decode()))


def test_authenticated_durable_offline_archive(tmp_path):
    async def scenario():
        config = tmp_path / 'auth.json'
        auth_config(config)
        root = tmp_path / 'central'
        body = ('中文😀\n' * 18000).encode()
        service = JournalService(root, config)
        async with serve(service.handle_socket, '127.0.0.1', 0, max_size=196608) as server:
            url = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/journals/ws'
            async with connect(url) as source:
                await source.send(json.dumps(source_hello()))
                assert json.loads(await source.recv())['ok']
                batch, journal = fixture_batch(body)
                missing = await request(source, 'append_batch', dict(batch=batch, journal=journal))
                assert missing['error'] == 'content_incomplete'
                for offset in range(0, len(body), 49152):
                    result = await stage(source, body, offset, body[offset:offset + 49152])
                    assert result['ok'] and 'receipt' not in result['result']
                ack = await request(source, 'append_batch', dict(batch=batch, journal=journal))
                assert ack['ok']
                repeat = await request(source, 'append_batch', dict(batch=batch, journal=journal))
                assert repeat == ack
        service.close()
        service = JournalService(root, config)
        async with serve(service.handle_socket, '127.0.0.1', 0, max_size=196608) as server:
            url = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/journals/ws'
            async with connect(url) as viewer:
                await viewer.send(json.dumps(hello()))
                assert json.loads(await viewer.recv())['ok']
                assert (await request(viewer, 'list'))['result']['journals'][0]['journal_id'] == 'journal1'
                page = await request(viewer, 'read_page', dict(journal_id='journal1', projection='conversation'))
                assert page['result']['events'][0]['event_id'] == 'event1'
                result, offset = bytearray(), 0
                while offset is not None:
                    response = await request(viewer, 'read_content', dict(journal_id='journal1', content_id='content1', offset=offset))
                    content = response['result']
                    result.extend(base64.b64decode(content['data_base64']))
                    offset = content['next_offset']
                assert bytes(result) == body
                assert (await request(viewer, 'append_batch', {}))['error'] == 'permission_denied'
            async with connect(url) as other:
                await other.send(json.dumps(hello(token='token-B')))
                assert json.loads(await other.recv())['ok']
                assert (await request(other, 'list'))['result']['journals'] == []
                assert (await request(other, 'status', dict(journal_id='journal1')))['error'] == 'not_found'
        service.close()
    asyncio.run(scenario())


def test_source_pin_nonce_replay_restart_and_viewer_impersonation(tmp_path):
    async def scenario():
        config = tmp_path / 'auth.json'
        auth_config(config)
        root = tmp_path / 'central'
        first = source_hello()
        for restart in range(2):
            service = JournalService(root, config)
            with pytest.raises(ValueError, match='already in use'):
                JournalService(root, config)
            async with serve(service.handle_socket, '127.0.0.1', 0) as server:
                url = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/journals/ws'
                async with connect(url) as ws:
                    await ws.send(json.dumps(first))
                    result = json.loads(await ws.recv())
                    assert result['ok'] is (restart == 0)
                    if restart:
                        assert result['error'] == 'replay_denied'
                async with connect(url) as ws:
                    await ws.send(json.dumps(source_hello(nonce=f'changed{restart}', secret='different-host-secret')))
                    assert json.loads(await ws.recv())['error'] == 'pin_conflict'
                async with connect(url) as ws:
                    forged = source_hello(nonce=f'fake{restart}', host='untrusted')
                    del forged['enrollment_proof']
                    await ws.send(json.dumps(forged))
                    assert json.loads(await ws.recv())['error'] == 'unauthorized'
            service.close()
    asyncio.run(scenario())


def test_content_integrity_commit_conflicts_and_scope_forgery(tmp_path):
    async def scenario():
        config = tmp_path / 'auth.json'
        auth_config(config)
        service = JournalService(tmp_path / 'central', config)
        async with serve(service.handle_socket, '127.0.0.1', 0) as server:
            url = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/journals/ws'
            async with connect(url) as ws:
                await ws.send(json.dumps(source_hello()))
                assert json.loads(await ws.recv())['ok']
                body = b'printed token-A stays private and exact'
                batch, manifest = fixture_batch(body)
                wrong = dict(journal_id='journal1', content_id='content1', offset=0, total_bytes=3,
                    sha256=hashlib.sha256(b'bad').hexdigest(), data_base64=base64.b64encode(b'BAD').decode())
                assert (await request(ws, 'content.stage', wrong))['error'] == 'content_hash_mismatch'
                assert (await stage(ws, body, chunk=body[:5]))['result']['complete'] is False
                assert (await request(ws, 'append_batch', dict(batch=batch, journal=manifest)))['error'] == 'content_incomplete'
                assert (await stage(ws, body, offset=5, chunk=body[5:]))['result']['complete']
                for field, value in [('owner_scope', 'ownerB'), ('external_room', 'other'),
                                     ('host_installation_id', 'other'), ('source_epoch', 'other')]:
                    forged = copy.deepcopy(batch)
                    forged[field] = value
                    forged['batch_sha256'] = batch_digest(forged)
                    result = await request(ws, 'append_batch', dict(batch=forged, journal=manifest))
                    assert result['error'] == 'permission_denied'
                invalid = copy.deepcopy(batch)
                invalid['events'][0]['metadata'] = {'token': 'token-A'}
                invalid['batch_sha256'] = batch_digest(invalid)
                result = await request(ws, 'append_batch', dict(batch=invalid, journal=manifest))
                assert not result['ok'] and 'token-A' not in json.dumps(result)
                # Two references to one content ID must agree individually, not
                # merely let the final dictionary entry overwrite a wrong hash.
                inconsistent = copy.deepcopy(batch)
                inconsistent['events'][0]['content_sha256'] = '0' * 64
                second_event = dict(batch['events'][0], seq=2, event_id='event2')
                inconsistent['events'].append(second_event)
                inconsistent['last_seq'] = 2
                inconsistent['batch_sha256'] = batch_digest(inconsistent)
                bad_manifest = dict(manifest, last_seq=2)
                assert (await request(ws, 'append_batch', dict(batch=inconsistent, journal=bad_manifest)))['error'] == 'invalid_batch'
                first_ack = await request(ws, 'append_batch', dict(batch=batch, journal=manifest))
                assert first_ack['ok']
                changed = copy.deepcopy(batch)
                changed['events'][0]['preview'] = 'changed'
                changed['batch_sha256'] = batch_digest(changed)
                assert (await request(ws, 'append_batch', dict(batch=changed, journal=manifest)))['error'] == 'batch_conflict'
                changed['batch_id'] = 'batch2'
                changed['first_seq'] = changed['last_seq'] = 3
                changed['events'][0]['seq'] = 3
                changed['batch_sha256'] = batch_digest(changed)
                manifest['last_seq'] = 3
                assert (await request(ws, 'append_batch', dict(batch=changed, journal=manifest)))['error'] == 'sequence_conflict'
                changed['first_seq'] = changed['last_seq'] = 2
                changed['events'][0]['seq'] = 2
                changed['batch_sha256'] = batch_digest(changed)
                assert (await request(ws, 'append_batch', dict(batch=changed, journal=manifest)))['error'] == 'event_conflict'
                assert (await request(ws, 'status', dict(journal_id='journal1')))['result']['last_seq'] == 1
                assert (await request(ws, 'read_page', dict(journal_id='../private')))['error'] == 'invalid_request'
                assert (await request(ws, 'list', dict(owner_scope='ownerB')))['error'] == 'invalid_request'
        service.close()
    asyncio.run(scenario())


def test_revocation_rotation_and_recheck_after_io(tmp_path):
    async def scenario():
        config = tmp_path / 'auth.json'
        auth_config(config)
        service = JournalService(tmp_path / 'central', config)
        async with serve(service.handle_socket, '127.0.0.1', 0) as server:
            url = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/journals/ws'
            async with connect(url) as source, connect(url) as viewer:
                await source.send(json.dumps(source_hello()))
                await source.recv()
                body = b'closed archive'
                batch, manifest = fixture_batch(body)
                await stage(source, body)
                assert (await request(source, 'append_batch', dict(batch=batch, journal=manifest)))['ok']
                await viewer.send(json.dumps(hello()))
                await viewer.recv()
                original = service._dispatch
                def rotate_during_io(p, action, params):
                    result = original(p, action, params)
                    auth_config(config, {'ownerA': ('rotated-A', ['room']), 'ownerB': ('token-B', ['room'])})
                    return result
                service._dispatch = rotate_during_io
                result = await request(viewer, 'status', dict(journal_id='journal1'))
                assert result['error'] == 'unauthorized' and 'result' not in result
                service._dispatch = original
                assert (await request(source, 'list'))['error'] == 'unauthorized'
            async with connect(url) as viewer:
                await viewer.send(json.dumps(hello(token='rotated-A')))
                assert json.loads(await viewer.recv())['ok']
                assert (await request(viewer, 'status', dict(journal_id='journal1')))['ok']
                auth_config(config, {'ownerA': ('rotated-A', ['other']), 'ownerB': ('token-B', ['room'])})
                assert (await request(viewer, 'read_content', dict(journal_id='journal1', content_id='content1')))['error'] == 'unauthorized'
        service.close()
    asyncio.run(scenario())


def test_origin_and_url_auth_boundaries(tmp_path):
    async def scenario():
        config = tmp_path / 'auth.json'
        auth_config(config)
        service = JournalService(tmp_path / 'central', config, origins=['https://relay.example'])
        async with serve(service.handle_socket, '127.0.0.1', 0) as server:
            url = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/journals/ws'
            for address, origin in [(url, 'https://evil.example'), (url + '?token=token-A', None)]:
                async with connect(address, origin=origin) as ws:
                    with pytest.raises(ConnectionClosed):
                        await ws.recv()
            async with connect(url, origin='https://relay.example') as ws:
                await ws.send(json.dumps(hello()))
                assert json.loads(await ws.recv())['ok']
        service.close()
    asyncio.run(scenario())


def test_staging_crash_cannot_create_journal_receipt(tmp_path):
    async def scenario():
        config = tmp_path / 'auth.json'
        auth_config(config)
        root = tmp_path / 'central'
        body = b'before crash\x00after crash'
        batch, manifest = fixture_batch(body)
        for iteration in range(2):
            service = JournalService(root, config)
            async with serve(service.handle_socket, '127.0.0.1', 0) as server:
                url = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/journals/ws'
                async with connect(url) as source:
                    await source.send(json.dumps(source_hello(nonce=f'restart{iteration}')))
                    assert json.loads(await source.recv())['ok']
                    if iteration == 0:
                        assert (await stage(source, body, chunk=body[:5]))['result']['complete'] is False
                    assert (await request(source, 'append_batch', dict(batch=batch, journal=manifest)))['error'] == 'content_incomplete'
                    assert (await request(source, 'list'))['result']['journals'] == []
                    if iteration:
                        assert (await stage(source, body, offset=5, chunk=body[5:]))['result']['complete']
                        assert (await request(source, 'append_batch', dict(batch=batch, journal=manifest)))['ok']
            service.close()
    asyncio.run(scenario())


def test_storage_worker_does_not_block_event_loop_and_connection_count_is_bounded(tmp_path):
    async def scenario():
        config = tmp_path / 'auth.json'
        auth_config(config)
        service = JournalService(tmp_path / 'central', config, max_connections=1)
        original = service._dispatch
        def slow(p, action, params):
            time.sleep(.15)
            return original(p, action, params)
        service._dispatch = slow
        async with serve(service.handle_socket, '127.0.0.1', 0) as server:
            url = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/journals/ws'
            async with connect(url) as first:
                await first.send(json.dumps(hello()))
                await first.recv()
                async with connect(url) as extra:
                    with pytest.raises(ConnectionClosed):
                        await extra.recv()
                pending = asyncio.create_task(request(first, 'list'))
                await asyncio.sleep(.02)
                assert not pending.done()
                assert (await pending)['ok']
        service.close()
    asyncio.run(scenario())
