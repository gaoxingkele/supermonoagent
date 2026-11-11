import base64
import hashlib
import json
import os
import stat
import subprocess

import pytest

from supermonoagent.conversation_contract import JournalError, batch_digest
from supermonoagent.conversation_store import JournalStore


def create(store, **kw):
    return store.create('owner', room='room', window_id='window', session_id='session',
                        source_epoch='epoch', process_generation='generation', **kw)['journal_id']


def destination(store):
    return dict(destination_id='dest', authority='https://relay.example', service_path='/journals/ws',
                owner_scope='owner', external_room='external', host_installation_id=store.host_installation_id,
                source_epoch='epoch')


def body(store, journal, descriptor):
    return store.content_bytes(journal, descriptor['content_id'])


def test_binary_fidelity_and_reopen(tmp_path):
    data = (bytes(range(256)) + '中文🙂\x1b[31m\x00'.encode()) * 40000
    assert len(data) >= 10 * 1024 * 1024
    store = JournalStore(tmp_path)
    host = store.host_installation_id
    journal = create(store)
    for offset in range(0, len(data), 65537):
        store.append_raw(journal, data[offset:offset + 65537])
    events = []
    cursor = None
    while True:
        page = store.read_page('owner', journal, cursor=cursor)
        events.extend(event for event in page['events'] if event['kind'] == 'raw_output')
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert b''.join(body(store, journal, event) for event in events) == data
    store.close()
    store = JournalStore(tmp_path)
    assert store.host_installation_id == host
    assert store.status('owner', journal)['terminal_state'] == 'unknown'
    assert b''.join(body(store, journal, event) for event in events) == data
    store.close()


def test_source_ids_do_not_deduplicate_deliberate_equal_prompts(tmp_path):
    with JournalStore(tmp_path) as store:
        journal = create(store)
        event = dict(kind='user_input', actor='user', source_record_id='one', state='accepted', proof='app_submission')
        first = store.append_event(journal, event, 'same')
        assert store.append_event(journal, event, 'same') == first
        with pytest.raises(JournalError, match='source_conflict'):
            store.append_event(journal, event, 'different')
        store.append_event(journal, {**event, 'source_record_id': 'two'}, 'same')
        store.append_event(journal, dict(kind='assistant_candidate', state='unknown'), 'candidate')
        store.append_event(journal, dict(kind='assistant_final', state='confirmed', proof='provider_terminal_result'), 'done\n```py\nx=1\n```')
        assert len(store.read_page('owner', journal, projection='conversation')['events']) == 3


def test_lifecycle_exports_and_authorization(tmp_path):
    with JournalStore(tmp_path) as store:
        journal = create(store)
        store.append_event(journal, dict(kind='user_input', actor='user', state='accepted', proof='app_submission'), '你好🙂')
        store.suspend(journal)
        assert store.status('owner', journal)['terminal_state'] == 'running'
        with pytest.raises(JournalError, match='terminal_running'):
            store.seal(journal)
        store.terminal_end(journal, pending=True)
        with pytest.raises(JournalError, match='capture_pending'):
            store.seal(journal)
        store.terminal_end(journal, pending=False)
        manifest = store.seal(journal)
        assert manifest['capture_state'] == 'sealed'
        exports = list(tmp_path.glob('*.conversation.jsonl'))
        assert len(exports) == 1
        rows = [json.loads(line) for line in exports[0].read_text(encoding='utf-8').splitlines()]
        assert rows[0]['through_seq'] == manifest['last_seq']
        assert rows[1]['text'] == '你好🙂'
        for owner, rooms in [('other', None), ('owner', ['forbidden'])]:
            with pytest.raises(JournalError, match='not_found'):
                store.status(owner, journal, rooms=rooms)
            assert store.list_journals(owner, rooms=rooms)['journals'] == []


def test_chunk_bounds_projection_and_cursor_scope(tmp_path):
    with JournalStore(tmp_path) as store:
        journal = create(store)
        text = '中文🙂\n' * 10000
        event = store.append_event(journal, dict(kind='assistant_final', state='confirmed', proof='provider_final_phase'), text)
        rebuilt = b''
        offset = 0
        while True:
            chunk = store.read_content('owner', journal, event['content_id'], offset=offset)
            decoded = base64.b64decode(chunk['data_base64'])
            assert len(decoded) <= 49152
            rebuilt += decoded
            offset = chunk['next_offset']
            if offset is None:
                break
        assert rebuilt == text.encode()
        assert event['content_sha256'] == hashlib.sha256(rebuilt).hexdigest()
        for index in range(105):
            store.append_raw(journal, str(index).encode())
        page = store.read_page('owner', journal, limit=100)
        assert len(page['events']) == 100
        assert len(json.dumps(page, ensure_ascii=False).encode()) < 192 * 1024
        with pytest.raises(JournalError):
            store.read_page('other', journal, cursor=page['next_cursor'])
        with pytest.raises(JournalError):
            store.read_page('owner', journal, cursor=page['next_cursor'], projection='conversation')


def test_pinned_mirror_exact_receipt_and_restart(tmp_path):
    store = JournalStore(tmp_path)
    journal = create(store)
    store.append_raw(journal, b'output')
    store.pin_mirror(journal, destination(store))
    with pytest.raises(JournalError, match='destination_conflict'):
        store.pin_mirror(journal, {**destination(store), 'external_room': 'other'})
    batch = store.mirror_batch(journal)
    assert batch['batch_sha256'] == batch_digest(batch)
    assert batch['contents'][1:] == [dict(content_id=batch['events'][1]['content_id'], bytes=6,
                                      sha256=hashlib.sha256(b'output').hexdigest())]
    receipt = {key: batch[key] for key in ['destination_id', 'owner_scope', 'external_room', 'host_installation_id',
               'journal_id', 'source_epoch', 'batch_id', 'first_seq', 'last_seq', 'batch_sha256']}
    receipt.update(through_seq=batch['last_seq'], last_event_id=batch['events'][-1]['event_id'], commit_id='commit')
    for field in receipt:
        altered = {**receipt, field: 999 if isinstance(receipt[field], int) else 'wrong'}
        if field == 'commit_id':
            continue
        with pytest.raises(JournalError, match='invalid_receipt'):
            store.accept_receipt(journal, altered)
        assert store.status('owner', journal)['mirror']['through_seq'] == 0
    store.close()
    store = JournalStore(tmp_path)
    assert store.mirror_batch(journal) == batch
    assert store.accept_receipt(journal, receipt)['mirror']['through_seq'] == 2
    assert store.accept_receipt(journal, receipt)['mirror']['through_seq'] == 2
    assert store.mirror_batch(journal)['first_seq'] == 3  # recorder restart status
    store.close()


def test_frozen_pending_final_repairs_after_eof_and_restart_once(tmp_path):
    now = [1000.0]
    store = JournalStore(tmp_path, clock=lambda: now[0])
    journal = create(store)
    store.authorize_pending_repair(journal, binding_id='binding', provider_thread_id='thread',
        turn_ids=['pending-turn'], source_record_ids=['pending-final'], deadline=1100.0)
    store.terminal_end(journal, pending=True)
    store.seal(journal, incomplete=True)
    store.close()
    store = JournalStore(tmp_path, clock=lambda: now[0])
    event = dict(kind='assistant_final', actor='assistant', state='confirmed', proof='provider_terminal_result',
        binding_id='binding', provider_thread_id='thread', provider_turn_id='pending-turn', source_record_id='pending-final')
    with pytest.raises(JournalError, match='journal_sealed'):
        store.append_event(journal, event, 'late exact final')
    first = store.reconcile_event(journal, event, 'late exact final')
    assert store.reconcile_event(journal, event, 'late exact final') == first
    for field in ['binding_id', 'provider_thread_id', 'provider_turn_id', 'source_record_id', 'kind']:
        with pytest.raises(JournalError, match='invalid_repair'):
            store.reconcile_event(journal, {**event, field: 'wrong'}, 'forged')
    assert len(store.read_page('owner', journal, projection='conversation')['events']) == 1
    rows = [json.loads(line) for line in next(tmp_path.glob('*.conversation.jsonl')).read_text(encoding='utf-8').splitlines()]
    assert rows[0]['through_seq'] == first['seq']
    assert rows[1]['text'] == 'late exact final'
    now[0] = 1101.0
    with pytest.raises(JournalError, match='invalid_repair'):
        store.reconcile_event(journal, event, 'late exact final')
    store.close()


def test_archive_isolation_all_reader_methods_and_opaque_ids(tmp_path):
    with JournalStore(tmp_path) as store:
        journal = create(store)
        event = store.append_raw(journal, b'private')
        store.terminal_end(journal)
        store.seal(journal)
        for owner, rooms in [('other', None), ('owner', ['revoked'])]:
            for read in [lambda: store.status(owner, journal, rooms=rooms),
                         lambda: store.read_page(owner, journal, rooms=rooms),
                         lambda: store.read_content(owner, journal, event['content_id'], rooms=rooms)]:
                with pytest.raises(JournalError, match='not_found'):
                    read()
        for value in ['../outside', 'a:b', 'CON', 'NUL', 'journal/other']:
            with pytest.raises(JournalError):
                store.status('owner', value)


def test_only_proven_inputs_and_finals_enter_conversation(tmp_path):
    with JournalStore(tmp_path) as store:
        journal = create(store)
        for kind, actor, state, proof in [
            ('user_input', 'user', 'accepted', 'unknown'),
            ('user_input', 'tool', 'accepted', 'provider_user_item'),
            ('user_input', 'user', 'submitted', 'app_submission'),
            ('assistant_candidate', 'assistant', 'confirmed', 'provider_terminal_result'),
            ('assistant_final', 'assistant', 'unknown', 'provider_final_phase'),
            ('assistant_final', 'assistant', 'confirmed', {'some_boolean': True})]:
            store.append_event(journal, dict(kind=kind, actor=actor, state=state, proof=proof), 'candidate')
        assert store.read_page('owner', journal, projection='conversation')['events'] == []
        accepted = store.append_event(journal, dict(kind='user_input', actor='human', state='accepted',
            proof={'kind': 'provider_human_input', 'version': 'fake-1'}), 'same literal synthetic-token')
        final = store.append_event(journal, dict(kind='assistant_final', actor='assistant', state='completed',
            proof='provider_terminal_result', metadata={'task_outcome': 'failed'}), 'Task failed, original final retained.')
        assert [event['event_id'] for event in store.read_page('owner', journal, projection='conversation')['events']] == [accepted['event_id'], final['event_id']]
        assert store.content_bytes(journal, accepted['content_id']) == b'same literal synthetic-token'
        with pytest.raises(JournalError, match='invalid_metadata'):
            store.append_event(journal, dict(kind='status', metadata={'nested': {'token': 'synthetic-token'}}))


def test_lifecycle_events_are_mirrored_and_seal_rebuild_is_idempotent(tmp_path):
    with JournalStore(tmp_path) as store:
        journal = create(store)
        store.suspend(journal)
        store.terminal_end(journal)
        ended = store.seal(journal)
        before = ended['last_seq']
        next(tmp_path.glob('*.conversation.jsonl')).unlink()
        assert store.seal(journal)['last_seq'] == before
        assert next(tmp_path.glob('*.conversation.jsonl')).exists()
        events = store.read_page('owner', journal)['events']
        assert [event['metadata']['journal_state']['capture_state'] for event in events] == ['capturing', 'suspended', 'draining', 'sealed']


def test_failed_sqlite_commit_is_atomic_and_errors_are_fixed(tmp_path):
    with JournalStore(tmp_path) as store:
        journal = create(store)
        db = store._dbs[journal]
        db.execute("CREATE TRIGGER synthetic_disk_full BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'secret body/path'); END")
        before = store.status('owner', journal)['last_seq']
        with pytest.raises(JournalError, match='^storage_error$'):
            store.append_raw(journal, b'payload')
        assert store.status('owner', journal)['last_seq'] == before
        assert db.execute('SELECT COUNT(*) FROM content').fetchone()[0] == 1


def test_directory_links_and_second_writer_are_refused(tmp_path):
    store = JournalStore(tmp_path)
    with pytest.raises(JournalError, match='storage_error'):
        JournalStore(tmp_path)
    store.close()
    target, link = tmp_path / 'target', tmp_path / 'link'
    target.mkdir()
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip('symlink creation unavailable')
    with pytest.raises(JournalError):
        JournalStore(link)


def test_ten_thousand_events_page_without_truncation_or_unbounded_envelopes(tmp_path):
    with JournalStore(tmp_path) as store:
        journal = create(store)
        for index in range(10000):
            store.append_event(journal, dict(kind='user_input', actor='human', state='accepted',
                proof='app_submission', source_record_id='submission-' + str(index)), '中文🙂' + str(index))
        cursor, seen = None, []
        while True:
            page = store.read_page('owner', journal, cursor=cursor, limit=100, projection='conversation')
            assert len(page['events']) <= 100
            assert len(json.dumps(page, ensure_ascii=False).encode()) < 192 * 1024
            seen.extend(event['source_record_id'] for event in page['events'])
            cursor = page['next_cursor']
            if cursor is None:
                break
        assert seen == ['submission-' + str(index) for index in range(10000)]


def test_failed_export_is_visible_and_original_event_is_rebuildable(tmp_path, monkeypatch):
    with JournalStore(tmp_path) as store:
        journal = create(store)
        event = store.append_event(journal, dict(kind='assistant_final', state='confirmed', proof='provider_final_phase'), 'original\n```py\npass\n```')
        store.terminal_end(journal)
        original = store._export
        def fail(*args):
            raise OSError('synthetic-private-path')
        monkeypatch.setattr(store, '_export', fail)
        manifest = store.seal(journal)
        assert manifest['local_error'] == 'export_error'
        assert store.content_bytes(journal, event['content_id']) == b'original\n```py\npass\n```'
        monkeypatch.setattr(store, '_export', original)
        store.seal(journal)
        rows = [json.loads(line) for line in next(tmp_path.glob('*.conversation.jsonl')).read_text(encoding='utf-8').splitlines()]
        assert rows[1]['text'] == 'original\n```py\npass\n```'


def test_resume_capture_after_recorder_restart_preserves_journal_and_gap(tmp_path):
    store = JournalStore(tmp_path)
    journal = create(store)
    original = store.append_raw(journal, b'before recorder stop')
    store.suspend(journal)
    store.close()
    with JournalStore(tmp_path) as store:
        before = store.status('owner', journal)
        assert before['terminal_state'] == 'unknown'
        assert before['capture_state'] == 'suspended'
        for generation, epoch in [('new-generation', 'epoch'), ('generation', 'new-epoch')]:
            with pytest.raises(JournalError, match='journal_conflict'):
                store.resume_capture(journal, process_generation=generation, source_epoch=epoch)
        resumed = store.resume_capture(journal, process_generation='generation', source_epoch='epoch')
        assert resumed['journal_id'] == journal
        assert resumed['terminal_state'] == 'running'
        assert resumed['capture_state'] == 'capturing'
        assert resumed['local_error'] == 'recorder_restart'
        assert resumed['last_seq'] == before['last_seq'] + 1
        assert store.resume_capture(journal, process_generation='generation', source_epoch='epoch') == resumed
        latest = store.read_page('owner', journal)['events'][-1]
        assert latest['metadata']['journal_state']['capture_state'] == 'capturing'
        assert store.content_bytes(journal, original['content_id']) == b'before recorder stop'
        store.append_raw(journal, b'after resume')
        assert len(store.list_journals('owner')['journals']) == 1


@pytest.mark.parametrize('ended,sealed', [(False, False), (True, False), (True, True)])
def test_resume_capture_checks_suspended_vs_ended_state(tmp_path, ended, sealed):
    with JournalStore(tmp_path) as store:
        journal = create(store)
        if ended:
            store.terminal_end(journal)
        else:
            store.suspend(journal)
        if sealed:
            store.seal(journal)
        if ended:
            with pytest.raises(JournalError, match='terminal_ended'):
                store.resume_capture(journal, process_generation='generation', source_epoch='epoch')
        else:
            manifest = store.resume_capture(journal, process_generation='generation', source_epoch='epoch')
            assert manifest['terminal_state'] == 'running'
            assert manifest['capture_state'] == 'capturing'
            assert manifest['local_error'] is None


def test_raw_batch_preserves_original_ids_order_and_is_atomic(tmp_path):
    with JournalStore(tmp_path) as store:
        journal = create(store)
        records = [dict(data=bytes([index]) + b'\x00\xff', source_record_id='raw-' + str(index),
                        observed_at=1000 + index) for index in range(64)]
        events = store.append_raw_many(journal, records)
        assert [event['source_record_id'] for event in events] == [item['source_record_id'] for item in records]
        assert [store.content_bytes(journal, event['content_id']) for event in events] == [item['data'] for item in records]
        assert store.append_raw_many(journal, records) == events
        before = store.status('owner', journal)['last_seq']
        with pytest.raises(JournalError, match='source_conflict'):
            store.append_raw_many(journal, [dict(data=b'new', source_record_id='new'),
                                           dict(data=b'conflicting', source_record_id='raw-1')])
        assert store.status('owner', journal)['last_seq'] == before
        assert store._dbs[journal].execute('SELECT COUNT(*) FROM content').fetchone()[0] == 65
        assert store.append_raw(journal, b'new', source_record_id='new')['seq'] == before + 1


def test_raw_batch_bounds_and_disk_failure_roll_back_every_descriptor(tmp_path):
    with JournalStore(tmp_path) as store:
        journal = create(store)
        for records in [[dict(data=b'x')] * 65, [dict(data=b'x' * (256 * 1024 + 1))]]:
            with pytest.raises(JournalError, match='invalid_batch'):
                store.append_raw_many(journal, records)
        db = store._dbs[journal]
        db.execute("CREATE TRIGGER synthetic_batch_disk_full BEFORE INSERT ON events WHEN NEW.seq=3 BEGIN SELECT RAISE(ABORT,'synthetic path'); END")
        with pytest.raises(JournalError, match='storage_error'):
            store.append_raw_many(journal, [dict(data=b'one'), dict(data=b'two')])
        assert store.status('owner', journal)['last_seq'] == 1
        assert db.execute('SELECT COUNT(*) FROM content').fetchone()[0] == 1


@pytest.mark.parametrize('outcome', ['exited', 'interrupted'])
def test_settle_pending_does_not_fake_another_terminal_end(tmp_path, outcome):
    with JournalStore(tmp_path, clock=lambda: 0.0) as store:
        journal = create(store)
        with pytest.raises(JournalError, match='invalid_lifecycle'):
            store.settle_pending(journal)
        store.terminal_end(journal, outcome=outcome, pending=True)
        before = store.status('owner', journal)
        with pytest.raises(JournalError, match='capture_pending'):
            store.seal(journal)
        settled = store.settle_pending(journal)
        assert settled['terminal_state'] == ('ended' if outcome == 'exited' else 'interrupted')
        assert settled['capture_state'] == 'draining'
        assert settled['terminal_ended_at'] == before['terminal_ended_at'] == 0.0
        assert store.settle_pending(journal) == settled
        assert store.seal(journal)['capture_state'] == 'sealed'
        with pytest.raises(JournalError, match='invalid_lifecycle'):
            store.settle_pending(journal)


def test_root_privacy_is_applied_before_canonical_file_creation(tmp_path, monkeypatch):
    from supermonoagent import conversation_store as module
    calls = []
    def refuse(path, directory=False):
        calls.append((path, directory, list(path.iterdir())))
        raise ValueError('synthetic private path must not escape')
    monkeypatch.setattr(module, '_secure_storage', refuse)
    root = tmp_path / 'new-private-root'
    with pytest.raises(JournalError, match='private_storage_unavailable'):
        JournalStore(root)
    assert calls == [(root, True, [])]
    assert list(root.iterdir()) == []


@pytest.mark.skipif(os.name != 'nt', reason='Windows ACL assertion')
def test_windows_explicit_everyone_grants_removed_from_root_and_existing_db(tmp_path):
    from supermonoagent.file_companion_manager import _private, ManagerError
    root = tmp_path / 'canonical'
    store = JournalStore(root)
    journal = create(store)
    store.terminal_end(journal)
    store.seal(journal)
    store.close()
    database = root / (journal + '.history.sqlite')
    export = root / (journal + '.conversation.jsonl')
    for path in [root, database, export]:
        subprocess.run(['icacls', str(path), '/grant', '*S-1-1-0:(RX)'], check=True,
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        with pytest.raises(ManagerError):
            _private(path, path == root)
    with JournalStore(root):
        _private(root, True)
        _private(database)
        _private(export)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX mode assertion')
def test_posix_private_root_and_existing_files_restrict_modes(tmp_path):
    root = tmp_path / 'canonical'
    root.mkdir(mode=0o777)
    root.chmod(0o777)
    store = JournalStore(root)
    journal = create(store)
    store.close()
    database = root / (journal + '.history.sqlite')
    database.chmod(0o666)
    with JournalStore(root):
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE(database.stat().st_mode) == 0o600
        for path in root.iterdir():
            assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
