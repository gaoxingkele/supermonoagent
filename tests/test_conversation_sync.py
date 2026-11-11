import asyncio
import base64
import hashlib
import json
from pathlib import Path
import ssl

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from supermonoagent.conversation_contract import BATCH_IDENTITY, JournalError
from supermonoagent.conversation_service import JournalService
from supermonoagent.conversation_store import JournalStore
from supermonoagent.conversation_sync import MirrorClient, SourceCredentials
from test_conversation_service import auth_config, fixture_batch, hello, request


class Store:
    def __init__(self):
        self.body = b'faithful body'
        self.batch, self.journal = fixture_batch(self.body)
        self.accepted = []

    def pin_mirror(self, journal_id, destination):
        return self.journal

    def status(self, owner, journal_id):
        return self.journal

    def mirror_batch(self, journal_id):
        return self.batch

    def content_bytes(self, journal_id, content_id):
        return self.body

    def accept_receipt(self, journal_id, receipt):
        self.accepted.append(receipt)


def destination():
    return dict(destination_id='dest1', authority='http://127.0.0.1:8794', service_path='/journals/ws',
                owner_scope='ownerA', external_room='room', host_installation_id='host1', source_epoch='epoch1')


def test_sync_rejects_forged_ack_without_advancing_source():
    async def scenario():
        store = Store()
        client = MirrorClient(store, destination(), SourceCredentials('token-A', 'private-host-secret', 'ownerA' * 8))
        async def forged(ws, action, params):
            if action == 'append_batch':
                batch = params['batch']
                receipt = {key: batch[key] for key in BATCH_IDENTITY}
                receipt.update(through_seq=1, last_event_id='event1', commit_id='commit1', owner_scope='other')
                return dict(receipt=receipt)
            return dict(content_id=params['content_id'], received_bytes=params['total_bytes'], complete=True)
        class Socket:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def send(self, raw): pass
            async def recv(self): return '{"op":"hello","protocol":"sma-journal-1","ok":true,"role":"source"}'
        client._connect = lambda *args, **kwargs: Socket()
        client._request = forged
        with pytest.raises(JournalError, match='invalid_receipt'):
            await client.sync_once('journal1')
        assert not store.accepted
    asyncio.run(scenario())


def test_real_store_lost_ack_identical_retry_and_offline_read(tmp_path):
    async def scenario():
        config = tmp_path / 'auth.json'
        auth_config(config)
        service = JournalService(tmp_path / 'central', config)
        local = JournalStore(tmp_path / 'source', host_installation_id='host1')
        journal = local.create('ownerA', room='local-room', window_id='window1', session_id='session1',
            source_epoch='epoch1', process_generation='generation1')['journal_id']
        body = ('中文😀\n```python\nprint("token-A")\n```\n' * 5000).encode()
        event = local.append_event(journal, dict(kind='assistant_final', actor='assistant', state='confirmed',
            proof='provider_final_phase', source_record_id='final1'), body)
        # Force a pending first batch before lifecycle events exist.
        async with serve(service.handle_socket, '127.0.0.1', 0, max_size=196608) as server:
            target = destination()
            target['authority'] = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
            credentials = SourceCredentials('token-A', 'private-host-secret', 'ownerA' * 8)
            local.pin_mirror(journal, target)
            pending = local.mirror_batch(journal)
            local.terminal_end(journal)
            local.seal(journal)
            client = MirrorClient(local, target, credentials)
            original = client._request
            async def lose_ack(ws, action, params):
                result = await original(ws, action, params)
                if action == 'append_batch':
                    raise JournalError('mirror_unavailable')
                return result
            client._request = lose_ack
            with pytest.raises(JournalError, match='mirror_unavailable'):
                await client.sync_once(journal)
            assert local.status('ownerA', journal)['mirror']['through_seq'] == 0
            assert local.mirror_batch(journal) == pending
            # Server must not advertise the future sealed wrapper after only first batch.
            url = target['authority'].replace('http:', 'ws:') + '/journals/ws'
            async with connect(url) as viewer:
                await viewer.send(json.dumps(hello()))
                await viewer.recv()
                first_status = (await request(viewer, 'status', dict(journal_id=journal)))['result']
                assert first_status['capture_state'] == 'capturing'
                assert first_status['last_seq'] == pending['last_seq']
            local.close()
            local = JournalStore(tmp_path / 'source')
            assert local.mirror_batch(journal) == pending
            client = MirrorClient(local, target, credentials)
            assert await client.sync_once(journal)
            assert local.status('ownerA', journal)['mirror']['through_seq'] == pending['last_seq']
            assert await client.sync_once(journal)
            assert not await client.sync_once(journal)
            local.close()  # No source process or store is involved in the following read.
            async with connect(url) as viewer:
                await viewer.send(json.dumps(hello()))
                await viewer.recv()
                status = (await request(viewer, 'status', dict(journal_id=journal)))['result']
                assert status['capture_state'] == 'sealed'
                content, offset = bytearray(), 0
                while offset is not None:
                    result = (await request(viewer, 'read_content', dict(journal_id=journal,
                        content_id=event['content_id'], offset=offset)))['result']
                    content.extend(base64.b64decode(result['data_base64']))
                    offset = result['next_offset']
                assert bytes(content) == body
        service.close()
    asyncio.run(scenario())


def test_insecure_or_retargeted_destination_rejected():
    credentials = SourceCredentials('token-A', 'private-host-secret')
    with pytest.raises(JournalError, match='invalid_destination'):
        MirrorClient(Store(), dict(destination(), authority='http://public.example'), credentials)
    with pytest.raises(JournalError, match='invalid_destination'):
        MirrorClient(Store(), dict(destination(), service_path='/other/ws'), credentials)
    assert 'token-A' not in repr(credentials)
    assert 'private-host-secret' not in repr(credentials)


def test_real_tls_requires_trust_and_preserves_hostname_validation(tmp_path):
    async def scenario():
        fixtures = Path(__file__).parent / 'fixtures'
        certificate = fixtures / 'tls-loopback-cert.pem'
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, fixtures / 'tls-loopback-key.pem')
        trusted = ssl.create_default_context(cafile=str(certificate))
        assert trusted.verify_mode == ssl.CERT_REQUIRED and trusted.check_hostname
        config = tmp_path / 'auth.json'
        auth_config(config)
        service = JournalService(tmp_path / 'central', config)
        store = Store()
        async with serve(service.handle_socket, '127.0.0.1', 0, ssl=context) as server:
            target = dict(destination(), authority=f'https://127.0.0.1:{server.sockets[0].getsockname()[1]}')
            client = MirrorClient(store, target, SourceCredentials('token-A', 'private-host-secret', 'ownerA' * 8))
            with pytest.raises(JournalError, match='mirror_unavailable'):
                await client.sync_once('journal1')
            assert not store.accepted
            # Trust only this synthetic local CA, preserving certificate and hostname checks.
            client._connect = lambda url, **kw: connect(url, ssl=trusted, **kw)
            assert await client.sync_once('journal1')
            assert len(store.accepted) == 1
        service.close()
    asyncio.run(scenario())


def test_central_projection_paging_and_byte_envelope_match_source(tmp_path):
    async def scenario():
        config = tmp_path / 'auth.json'
        auth_config(config)
        service = JournalService(tmp_path / 'central', config)
        local = JournalStore(tmp_path / 'source', host_installation_id='host1')
        journal = local.create('ownerA', room='room', window_id='window1', session_id='session1',
            source_epoch='epoch1', process_generation='generation1')['journal_id']
        for i in range(110):
            local.append_raw(journal, f'raw {i}\x00'.encode())
        for i, (kind, actor, state, proof) in enumerate([
            ('user_input', 'human', 'accepted', 'provider_human_input'),
            ('assistant_final', 'assistant', 'confirmed', 'provider_final_phase'),
            ('assistant_final', 'assistant', 'unknown', 'provider_final_phase'),
            ('user_input', 'human', 'accepted', {'untrusted': True}),
            ('assistant_final', 'assistant', 'completed', {'kind': 'provider_terminal_result'}),
        ]):
            local.append_event(journal, dict(kind=kind, actor=actor, state=state, proof=proof,
                source_record_id=f'large{i}', metadata={'private_fixture': 'x' * 44000}), f'message {i}')
        expected = [e['event_id'] for e in local.read_page('ownerA', journal, projection='conversation')['events']]
        async with serve(service.handle_socket, '127.0.0.1', 0, max_size=196608) as server:
            target = dict(destination(), authority=f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}')
            client = MirrorClient(local, target, SourceCredentials('token-A', 'private-host-secret', 'ownerA' * 8))
            while await client.sync_once(journal):
                pass
            local.close()
            async with connect(target['authority'].replace('http:', 'ws:') + '/journals/ws') as viewer:
                await viewer.send(json.dumps(hello()))
                await viewer.recv()
                cursor, found, pages = None, [], 0
                while True:
                    result = await request(viewer, 'read_page', dict(journal_id=journal, cursor=cursor, limit=100))
                    assert result['ok']
                    assert len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()) <= 196608
                    assert 0 < len(result['result']['events']) <= 100
                    found.extend(result['result']['events'])
                    pages += 1
                    cursor = result['result']['next_cursor']
                    if cursor is None:
                        break
                    wrong = await request(viewer, 'read_page', dict(journal_id=journal, cursor=cursor, projection='conversation'))
                    assert wrong['error'] == 'invalid_cursor'
                assert pages >= 3 and [e['seq'] for e in found] == list(range(1, 117))
                assert all(e['preview'] == '' for e in found if e['kind'] == 'raw_output')
                result = await request(viewer, 'read_page', dict(journal_id=journal, projection='conversation'))
                assert [e['event_id'] for e in result['result']['events']] == expected
                assert (await request(viewer, 'read_page', dict(journal_id=journal, limit=101)))['error'] == 'invalid_request'
        service.close()
    asyncio.run(scenario())


def test_explicit_local_to_external_owner_mapping_keeps_destination_acl(tmp_path):
    async def scenario():
        config = tmp_path / 'auth.json'
        auth_config(config, {'ownerA': ('token-A', ['room']), 'ownerB': ('token-B', ['room']),
                             'local-owner': ('local-token', ['room'])})
        service = JournalService(tmp_path / 'central', config)
        local = JournalStore(tmp_path / 'source', host_installation_id='host1')
        journal = local.create('local-owner', room='local-room', window_id='window1', session_id='session1',
            source_epoch='epoch1', process_generation='generation1')['journal_id']
        event = local.append_event(journal, dict(kind='assistant_final', actor='assistant', state='confirmed',
            proof='provider_final_phase'), 'explicit mapping preserves private body')
        async with serve(service.handle_socket, '127.0.0.1', 0, max_size=196608) as server:
            target = dict(destination(), authority=f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}')
            # The selected route maps a local owner to the relay's independently
            # authenticated external owner, rather than requiring equal labels.
            client = MirrorClient(local, target, SourceCredentials('token-A', 'private-host-secret', 'ownerA' * 8),
                local_owner_scope='local-owner')
            assert await client.sync_once(journal)
            source = local.status('local-owner', journal)
            assert source['owner_scope'] == 'local-owner' and source['room'] == 'local-room'
            assert source['mirror']['destination']['owner_scope'] == 'ownerA'
            with pytest.raises(JournalError, match='not_found'):
                local.status('ownerA', journal)
            # A second owner at the identical service/destination ID is still a
            # different immutable mapping, and cannot retarget the source outbox.
            with pytest.raises(JournalError, match='destination_conflict'):
                local.pin_mirror(journal, dict(target, owner_scope='ownerB'))
            local.close()
            url = target['authority'].replace('http:', 'ws:') + '/journals/ws'
            for token, allowed in [('token-A', True), ('token-B', False), ('local-token', False)]:
                async with connect(url) as viewer:
                    await viewer.send(json.dumps(hello(token=token)))
                    assert json.loads(await viewer.recv())['ok']
                    result = await request(viewer, 'status', dict(journal_id=journal))
                    assert result['ok'] is allowed
                    if allowed:
                        assert result['result']['owner_scope'] == 'ownerA'
                        assert result['result']['room'] == 'room'
                        content = (await request(viewer, 'read_content', dict(journal_id=journal,
                            content_id=event['content_id'])))['result']
                        assert base64.b64decode(content['data_base64']) == b'explicit mapping preserves private body'
                    else:
                        assert result['error'] == 'not_found'
                        assert (await request(viewer, 'list'))['result']['journals'] == []
                        assert (await request(viewer, 'read_content', dict(journal_id=journal,
                            content_id=event['content_id'])))['error'] == 'not_found'
        service.close()
    asyncio.run(scenario())


def test_external_owner_is_derived_from_token_even_with_explicit_mapping(tmp_path):
    async def scenario():
        config = tmp_path / 'auth.json'
        auth_config(config)
        service = JournalService(tmp_path / 'central', config)
        local = JournalStore(tmp_path / 'source', host_installation_id='host1')
        journal = local.create('local-owner', room='local-room', window_id='window1', session_id='session1',
            source_epoch='epoch1', process_generation='generation1')['journal_id']
        local.append_raw(journal, b'local capture continues without a valid external ACK')
        async with serve(service.handle_socket, '127.0.0.1', 0, max_size=196608) as server:
            target = dict(destination(), authority=f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}')
            wrong = MirrorClient(local, target, SourceCredentials('token-B', 'private-host-secret', 'ownerB' * 8),
                local_owner_scope='local-owner')
            with pytest.raises(JournalError, match='mirror_rejected'):
                await wrong.sync_once(journal)
            pending = local.mirror_batch(journal)
            assert local.status('local-owner', journal)['mirror']['through_seq'] == 0
            local.append_raw(journal, b'new local data while the mapping credentials are wrong')
            assert local.mirror_batch(journal) == pending
            correct = MirrorClient(local, target, SourceCredentials('token-A', 'private-host-secret', 'ownerA' * 8),
                local_owner_scope='local-owner')
            assert await correct.sync_once(journal)
            assert local.status('local-owner', journal)['mirror']['through_seq'] == pending['last_seq']
            assert await correct.sync_once(journal)
            local.close()
            url = target['authority'].replace('http:', 'ws:') + '/journals/ws'
            async with connect(url) as denied:
                await denied.send(json.dumps(hello(token='token-B')))
                await denied.recv()
                assert (await request(denied, 'list'))['result']['journals'] == []
        service.close()
    asyncio.run(scenario())


def test_replication_ack_preserves_committed_capture_gap_truth(tmp_path):
    async def scenario():
        config = tmp_path / 'auth.json'
        auth_config(config)
        service = JournalService(tmp_path / 'central', config)
        local = JournalStore(tmp_path / 'source', host_installation_id='host1')
        journal = local.create('ownerA', room='room', window_id='window1', session_id='session1',
            source_epoch='epoch1', process_generation='generation1')['journal_id']
        async with serve(service.handle_socket, '127.0.0.1', 0, max_size=196608) as server:
            target = dict(destination(), authority=f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}')
            local.pin_mirror(journal, target)
            earlier_batch = local.mirror_batch(journal)
            local.append_event(journal, dict(kind='capture_gap', actor='system', origin='recorder',
                source_record_id='known-gap', state='unknown'), 'synthetic known recording gap')
            local.terminal_end(journal)
            local.seal(journal)
            assert local.status('ownerA', journal)['local_error'] == 'capture_gap'
            client = MirrorClient(local, target, SourceCredentials('token-A', 'private-host-secret', 'ownerA' * 8))
            assert await client.sync_once(journal)
            url = target['authority'].replace('http:', 'ws:') + '/journals/ws'
            async with connect(url) as viewer:
                await viewer.send(json.dumps(hello()))
                await viewer.recv()
                before_gap = (await request(viewer, 'status', dict(journal_id=journal)))['result']
                # A future mutable wrapper cannot change an earlier committed batch.
                assert before_gap['last_seq'] == earlier_batch['last_seq']
                assert before_gap['local_error'] is None
                assert before_gap['capture_state'] == 'capturing'
                assert await client.sync_once(journal)
                source_status = local.status('ownerA', journal)
                assert source_status['mirror']['through_seq'] == source_status['last_seq']
                local.close()
                archived = (await request(viewer, 'status', dict(journal_id=journal)))['result']
                assert archived['local_error'] == 'capture_gap'
                assert archived['capture_state'] == 'incomplete'
                assert archived['last_seq'] == source_status['last_seq']
        service.close()
    asyncio.run(scenario())
