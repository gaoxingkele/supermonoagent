import threading
import time

import pytest

from supermonoagent.conversation_capture import JournalCapture
from supermonoagent.conversation_store import JournalStore
from test_conversation_store import create


def test_no_viewers_capture_flush_suspend_and_end(tmp_path):
    store = JournalStore(tmp_path)
    journal = create(store)
    capture = JournalCapture(store)
    assert capture.record_output(journal, b'zero viewers\x00\xff')
    assert capture.flush()
    assert capture.status()['pending_items'] == 0
    assert capture.close()
    assert store.status('owner', journal)['terminal_state'] == 'running'
    assert store.status('owner', journal)['capture_state'] == 'suspended'
    assert any(event['kind'] == 'raw_output' for event in store.read_page('owner', journal)['events'])
    store.close()


def test_reader_does_not_wait_for_disk_and_queue_overflow_is_visible(tmp_path):
    store = JournalStore(tmp_path)
    journal = create(store)
    entered, release = threading.Event(), threading.Event()
    original = store.append_raw
    def slow(*args, **kwargs):
        entered.set()
        release.wait(5)
        return original(*args, **kwargs)
    store.append_raw = slow
    capture = JournalCapture(store, queue_bytes=4, queue_items=2)
    assert capture.record_output(journal, b'abcd')
    assert entered.wait(1)
    assert not capture.record_output(journal, b'efghi')
    assert capture.status()['dropped_bytes'] == 5
    assert not capture.flush(timeout=0.01)
    release.set()
    assert capture.flush()
    assert capture.status()['error'] == 'capture_gap'
    assert store.status('owner', journal)['local_error'] == 'capture_gap'
    assert capture.close()
    store.close()


def test_disk_failure_does_not_become_saved(tmp_path):
    store = JournalStore(tmp_path)
    journal = create(store)
    capture = JournalCapture(store)
    def fail(*args, **kwargs):
        raise OSError('synthetic secret payload must not escape')
    store.append_raw = fail
    assert capture.record_output(journal, b'bytes')
    assert capture.flush()
    status = capture.status()
    assert status['error'] == 'storage_error'
    assert status['committed_items'] == 0
    assert 'secret' not in str(status)
    capture.close()
    store.close()


def test_eof_waits_for_last_queued_output_and_explicit_seal(tmp_path):
    store = JournalStore(tmp_path)
    journal = create(store)
    capture = JournalCapture(store)
    data = [b'last\x00', b'\xf0\x9f', b'\x99\x82']
    for chunk in data:
        assert capture.record_output(journal, chunk)
    assert capture.terminal_end(journal, pending=True)
    assert capture.flush()
    assert store.status('owner', journal)['capture_state'] == 'draining'
    # A delayed trusted semantic record may still append while draining.
    assert capture.append_event(journal, dict(kind='assistant_final', state='confirmed',
        proof='provider_terminal_result'), 'late after EOF')
    assert capture.terminal_end(journal, pending=False)
    assert capture.seal(journal)
    assert capture.flush()
    assert store.status('owner', journal)['capture_state'] == 'sealed'
    events = store.read_page('owner', journal)['events']
    assert b''.join(store.content_bytes(journal, e['content_id']) for e in events if e['kind'] == 'raw_output') == b''.join(data)
    assert len(store.read_page('owner', journal, projection='conversation')['events']) == 1
    capture.close()
    store.close()


def test_gap_is_committed_before_queued_seal(tmp_path):
    store = JournalStore(tmp_path)
    journal = create(store)
    capture = JournalCapture(store, queue_bytes=4, queue_items=10)
    assert not capture.record_output(journal, b'overflow')
    assert capture.terminal_end(journal)
    assert capture.seal(journal)
    assert capture.flush()
    manifest = store.status('owner', journal)
    assert manifest['capture_state'] == 'incomplete'
    assert manifest['local_error'] == 'capture_gap'
    capture.close()
    store.close()


def test_batched_writer_does_not_cross_semantic_lifecycle_or_journal_boundaries(tmp_path):
    store = JournalStore(tmp_path)
    first_journal, second_journal = create(store), create(store)
    entered, release = threading.Event(), threading.Event()
    append_raw, append_many = store.append_raw, store.append_raw_many
    batches = []
    def slow(journal, *args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return append_raw(journal, *args, **kwargs)
    def spy(journal, records):
        batches.append((journal, [record['source_record_id'] for record in records]))
        return append_many(journal, records)
    store.append_raw, store.append_raw_many = slow, spy
    capture = JournalCapture(store)
    assert capture.record_output(first_journal, b'first', source_record_id='first')
    assert entered.wait(1)
    for index in range(5):
        assert capture.record_output(first_journal, str(index).encode(), source_record_id='before-' + str(index))
    assert capture.append_event(first_journal, dict(kind='user_input', actor='user', state='accepted', proof='app_submission'), 'semantic boundary')
    for index in range(3):
        assert capture.record_output(first_journal, b'after', source_record_id='after-' + str(index))
    for index in range(2):
        assert capture.record_output(second_journal, b'other', source_record_id='other-' + str(index))
    assert capture.terminal_end(first_journal)
    assert capture.seal(first_journal)
    release.set()
    assert capture.flush(timeout=5)
    assert batches == [(first_journal, ['before-' + str(index) for index in range(5)]),
                       (first_journal, ['after-' + str(index) for index in range(3)]),
                       (second_journal, ['other-' + str(index) for index in range(2)])]
    events = store.read_page('owner', first_journal)['events']
    kinds = [event['kind'] for event in events]
    assert kinds == ['status'] + ['raw_output'] * 6 + ['user_input'] + ['raw_output'] * 3 + ['status'] * 2
    status = capture.status()
    assert status['raw_batch_commits'] == 3
    assert status['max_raw_batch_items'] == 5
    assert status['peak_pending_items'] <= 1024
    assert status['peak_pending_bytes'] <= 16 * 1024 * 1024
    capture.close()
    store.close()


def test_failed_raw_batch_is_all_gap_and_never_partly_saved(tmp_path):
    store = JournalStore(tmp_path)
    journal = create(store)
    entered, release = threading.Event(), threading.Event()
    original = store.append_raw
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    store.append_raw = slow
    store._dbs[journal].execute("CREATE TRIGGER fail_second_batch_row BEFORE INSERT ON events WHEN NEW.seq=4 AND instr(CAST(NEW.descriptor AS TEXT),'\"kind\":\"raw_output\"')>0 BEGIN SELECT RAISE(ABORT,'synthetic path'); END")
    capture = JournalCapture(store)
    assert capture.record_output(journal, b'first')
    assert entered.wait(1)
    assert capture.record_output(journal, b'one')
    assert capture.record_output(journal, b'two')
    release.set()
    assert capture.flush(timeout=5)
    status = capture.status()
    assert status['error'] == 'storage_error'
    assert status['failed_items'] == 2
    assert status['committed_items'] == 1
    assert status['dropped_bytes'] == 6
    events = store.read_page('owner', journal)['events']
    assert [store.content_bytes(journal, e['content_id']) for e in events if e['kind'] == 'raw_output'] == [b'first']
    assert store.status('owner', journal)['local_error'] == 'capture_gap'
    capture.close()
    store.close()


def _capture_waiting_to_coalesce(store, monkeypatch):
    from supermonoagent import conversation_capture as module
    # A long test-only window makes accidental waiting observable without
    # fragile sub-millisecond scheduler assertions. Production remains 3 ms.
    monkeypatch.setattr(module, '_RAW_COALESCE_SECONDS', 10.0)
    capture = JournalCapture(store)
    waiting = threading.Event()
    wait = capture._condition.wait
    def observe(timeout=None):
        if timeout is not None and timeout > 1:
            waiting.set()
        return wait(timeout)
    monkeypatch.setattr(capture._condition, 'wait', observe)
    return capture, waiting


@pytest.mark.parametrize('barrier', ['semantic', 'terminal_end', 'other_journal'])
def test_raw_coalescer_releases_enqueue_lock_and_barriers_cut_deadline(tmp_path, monkeypatch, barrier):
    store = JournalStore(tmp_path)
    journal, other = create(store), create(store)
    captured = threading.Event()
    append_raw, append_many = store.append_raw, store.append_raw_many
    def single(*args, **kwargs):
        captured.set()
        return append_raw(*args, **kwargs)
    def batch(*args, **kwargs):
        captured.set()
        return append_many(*args, **kwargs)
    monkeypatch.setattr(store, 'append_raw', single)
    monkeypatch.setattr(store, 'append_raw_many', batch)
    capture, waiting = _capture_waiting_to_coalesce(store, monkeypatch)
    assert capture.record_output(journal, b'first', source_record_id='first')
    assert waiting.wait(1)
    started = time.monotonic()
    assert capture.record_output(journal, b'second', source_record_id='second')
    assert time.monotonic() - started < 1
    if barrier == 'semantic':
        assert capture.append_event(journal, dict(kind='user_input', actor='human',
            state='accepted', proof='app_submission'), 'boundary')
    elif barrier == 'terminal_end':
        assert capture.terminal_end(journal)
    else:
        assert capture.record_output(other, b'other', source_record_id='other')
    assert captured.wait(1), 'barrier incorrectly waited for the 10-second test window'
    assert capture.flush(timeout=2)
    events = store.read_page('owner', journal)['events']
    raw = [event for event in events if event['kind'] == 'raw_output']
    assert [event['source_record_id'] for event in raw] == ['first', 'second']
    assert [store.content_bytes(journal, event['content_id']) for event in raw] == [b'first', b'second']
    assert capture.close()
    store.close()


@pytest.mark.parametrize('finish', ['flush', 'close'])
def test_flush_and_close_cut_collection_deadline_and_preserve_accepted_bytes(tmp_path, monkeypatch, finish):
    store = JournalStore(tmp_path)
    journal = create(store)
    capture, waiting = _capture_waiting_to_coalesce(store, monkeypatch)
    assert capture.record_output(journal, b'accepted\x00\xff', source_record_id='accepted')
    assert waiting.wait(1)
    assert getattr(capture, finish)(timeout=1), 'drain incorrectly waited for the 10-second test window'
    event = next(e for e in store.read_page('owner', journal)['events'] if e['kind'] == 'raw_output')
    assert event['source_record_id'] == 'accepted'
    assert store.content_bytes(journal, event['content_id']) == b'accepted\x00\xff'
    assert capture.status()['pending_items'] == 0
    assert capture.close()
    store.close()


def test_raw_coalescing_monotonic_deadline_is_not_extended_by_new_chunks(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from supermonoagent import conversation_capture as module
    store = JournalStore(tmp_path)
    journal = create(store)
    clock = [0.0]
    capture = JournalCapture(store)
    original_wait = capture._condition.wait
    timeouts, committed = [], threading.Event()
    append_many = store.append_raw_many
    def batch(*args, **kwargs):
        result = append_many(*args, **kwargs)
        committed.set()
        return result
    def deterministic_wait(timeout=None):
        if timeout is None:
            return original_wait(timeout)
        timeouts.append(timeout)
        clock[0] += 0.001
        if len(timeouts) <= 3:
            assert capture.record_output(journal, b'chunk', source_record_id='raw-' + str(len(timeouts)))
        return True
    with capture._condition:
        with monkeypatch.context() as patch:
            patch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
            patch.setattr(store, 'append_raw_many', batch)
            patch.setattr(capture._condition, 'wait', deterministic_wait)
            assert capture.record_output(journal, b'first', source_record_id='raw-0')
            deadline = time.monotonic() + 5
            # Use the real wait/clock for test synchronization. Do not restore
            # the worker's clock while the physical transaction is still busy.
            while capture._queue or capture._inflight or capture._gaps:
                remaining = deadline - time.monotonic()
                assert remaining > 0, 'capture writer did not become quiescent'
                original_wait(remaining)
            assert committed.is_set()
            assert capture.status()['error'] is None
            # New chunks consume the original window; none starts a new one.
            assert timeouts == pytest.approx([0.003, 0.002, 0.001])
        # Restore all patches while holding the condition, after the worker is
        # quiescent; it cannot observe a mixed fake/real clock and wait pair.
    assert capture.flush()
    events = store.read_page('owner', journal)['events']
    assert [e['source_record_id'] for e in events if e['kind'] == 'raw_output'] == ['raw-' + str(index) for index in range(4)]
    assert capture.close()
    store.close()
