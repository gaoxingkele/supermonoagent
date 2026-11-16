"""Canonical managed ledger: synthetic provider IDs, no processes or networks."""
import hashlib
import json
import threading

import pytest

from supermonoagent.conversation_contract import JournalError
from supermonoagent.conversation_store import JournalStore, managed_creation_ids


def managed(store, nonce='create-1', provider='codex', confirmed=True):
    ids = managed_creation_ids(store.host_installation_id, 'owner', nonce)
    request = dict(anchor_window_id='native-window', anchor_session_id='native-session',
                   anchor_process_generation='native-generation', provider=provider,
                   cwd='D:/synthetic-project', room='room')
    manifest = store.create('owner', room='room', window_id=ids['window_id'], session_id=ids['session_id'],
        process_generation=ids['process_generation'], source_epoch='source-epoch', journal_id=ids['journal_id'],
        managed_creation=dict(nonce=nonce, request=request))
    target = {key: manifest[key] for key in ['window_id', 'session_id', 'process_generation', 'journal_id']}
    store.confirm_managed_provider('owner', manifest['journal_id'], target=target, provider=provider,
        provider_version='0.167.0' if provider == 'codex' else '2.1.310', provider_root_id='provider-root',
        policy={'mode': 'default'}, confirmed=confirmed)
    return manifest['journal_id'], target, request


def accept(store, journal, target, nonce='submit-1', text='你好🙂\n```py\nx=1\n```'):
    return store.accept_managed_submission('owner', journal, target=target, nonce=nonce, text=text)


def associate(store, journal, target, accepted, turn='turn-1'):
    active = control(store, journal, target)['active_submission']
    if active['dispatch_state'] == 'intent':
        store.mark_managed_dispatching('owner', journal, target=target,
            submission_id=accepted['submission_id'], cancel_version=active['cancel_version'])
    return store.associate_managed_turn('owner', journal, target=target,
        submission_id=accepted['submission_id'], user_message_uuid=accepted['user_message_uuid'],
        provider_root_id='provider-root', provider_turn_id=turn)


def settle(store, journal, target, accepted, outcome='completed', finals=None, turn='turn-1'):
    return store.settle_managed_submission('owner', journal, target=target,
        submission_id=accepted['submission_id'], user_message_uuid=accepted['user_message_uuid'],
        provider_root_id='provider-root', provider_turn_id=turn, outcome=outcome,
        finals=finals if finals is not None else [dict(provider_record_id='final-1', text='完成🙂\n```py\nx=1\n```',
            proof={'kind': 'provider_final_phase', 'phase': 'final_answer', 'terminal_status': 'completed'})])


def control(store, journal, target, **kw):
    return store.read_managed_control('owner', journal, target=target, **kw)


def test_creation_key_excludes_request_and_conflicts_survive_restart(tmp_path):
    store = JournalStore(tmp_path)
    journal, target, request = managed(store)
    creation = store.get_managed_creation('owner', journal)
    assert creation['request'] == request
    ids = managed_creation_ids(store.host_installation_id, 'owner', 'create-1')
    assert ids['journal_id'] == journal
    assert creation['binding_id'] != 'provider-root'
    store.close()
    with JournalStore(tmp_path) as store:
        assert store.get_managed_creation('owner', journal)['state'] == 'ambiguous'
        for changed in [dict(request, provider='claude'), dict(request, anchor_process_generation='new-generation'),
                        dict(request, cwd='D:/different-project')]:
            with pytest.raises(JournalError, match='creation_conflict'):
                store.create('owner', room='room', window_id=target['window_id'], session_id=target['session_id'],
                    process_generation=target['process_generation'], source_epoch='source-epoch', journal_id=journal,
                    managed_creation=dict(nonce='create-1', request=changed))
        prior = store.create('owner', room='room', window_id=target['window_id'], session_id=target['session_id'],
            process_generation=target['process_generation'], source_epoch='source-epoch', journal_id=journal,
            managed_creation=dict(nonce='create-1', request=request))
        assert prior['terminal_state'] == 'unknown'
        assert len(list(tmp_path.glob('*.history.sqlite'))) == 1


def test_initial_creation_control_and_status_roll_back_together(tmp_path, monkeypatch):
    with JournalStore(tmp_path) as store:
        original = store._append_status
        def fail(db, manifest):
            assert store._control(db, 'managed_creation') is not None
            raise JournalError('storage_error')
        monkeypatch.setattr(store, '_append_status', fail)
        with pytest.raises(JournalError, match='storage_error'):
            managed(store)
        db = next(iter(store._dbs.values()))
        assert db.execute('SELECT COUNT(*) FROM manifest').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM control').fetchone()[0] == 0
        monkeypatch.setattr(store, '_append_status', original)
        assert managed(store)[0]


def test_input_receipt_atomic_dedup_busy_and_original_unicode(tmp_path, monkeypatch):
    with JournalStore(tmp_path) as store:
        journal, target, _ = managed(store)
        first = accept(store, journal, target)
        assert first['replayed'] is False
        assert accept(store, journal, target)['replayed'] is True
        with pytest.raises(JournalError, match='nonce_conflict'):
            accept(store, journal, target, text='changed')
        with pytest.raises(JournalError, match='managed_busy'):
            accept(store, journal, target, nonce='second')
        events = store.read_page('owner', journal, projection='conversation')['events']
        assert len(events) == 1
        assert store.content_bytes(journal, events[0]['content_id']) == '你好🙂\n```py\nx=1\n```'.encode()
        assert control(store, journal, target)['active_submission']['submission_id'] == first['submission_id']


def test_accept_failure_rolls_back_input_nonce_and_active(tmp_path, monkeypatch):
    with JournalStore(tmp_path) as store:
        journal, target, _ = managed(store)
        original = store._set_control
        def fail(db, key, value):
            if key == 'managed_active' and value is not None:
                raise JournalError('storage_error')
            return original(db, key, value)
        monkeypatch.setattr(store, '_set_control', fail)
        before = store.status('owner', journal)['last_seq']
        with pytest.raises(JournalError, match='storage_error'):
            accept(store, journal, target)
        assert store.status('owner', journal)['last_seq'] == before
        assert store.read_page('owner', journal, projection='conversation')['events'] == []
        assert control(store, journal, target, nonce='submit-1')['receipt'] is None
        monkeypatch.setattr(store, '_set_control', original)
        assert accept(store, journal, target)['replayed'] is False


def test_pre_dispatch_cancel_atomic_and_old_nonce_cannot_cancel_next_input(tmp_path):
    with JournalStore(tmp_path) as store:
        journal, target, _ = managed(store)
        first = accept(store, journal, target)
        cancelled = store.cancel_managed_submission('owner', journal, target=target,
            submission_id=first['submission_id'], nonce='cancel-1')
        assert cancelled['state'] == 'not_dispatched'
        assert cancelled['cancel_version'] == 1
        assert control(store, journal, target)['active_submission'] is None
        assert len(store.read_page('owner', journal, projection='conversation')['events']) == 1
        second = accept(store, journal, target, nonce='submit-2')
        assert second['submission_id'] != first['submission_id']
        assert store.cancel_managed_submission('owner', journal, target=target,
            submission_id=first['submission_id'], nonce='cancel-1')['replayed'] is True
        assert control(store, journal, target)['active_submission']['submission_id'] == second['submission_id']
        with pytest.raises(JournalError):
            store.mark_managed_dispatching('owner', journal, target=target,
                submission_id=first['submission_id'], cancel_version=0)


def test_dispatch_wins_cancel_waits_for_exact_ack_then_settle_atomic(tmp_path, monkeypatch):
    with JournalStore(tmp_path) as store:
        journal, target, _ = managed(store)
        accepted = accept(store, journal, target)
        store.mark_managed_dispatching('owner', journal, target=target,
            submission_id=accepted['submission_id'], cancel_version=0)
        result = store.cancel_managed_submission('owner', journal, target=target,
            submission_id=accepted['submission_id'], nonce='cancel-1')
        assert result['state'] == 'interrupt_requested'
        assert control(store, journal, target)['active_submission']['cancel_requested']
        with pytest.raises(JournalError, match='managed_busy'):
            accept(store, journal, target, nonce='second')
        active = associate(store, journal, target, accepted)
        assert active['cancel_requested'] and active['cancel_version'] == 1
        result = settle(store, journal, target, accepted, outcome='interrupted', finals=[])
        assert result['outcome'] == 'interrupted'
        assert control(store, journal, target)['active_submission'] is None
        assert len(store.read_page('owner', journal, projection='conversation')['events']) == 1


def test_final_outcome_and_active_clear_are_one_transaction_and_replay_retained(tmp_path, monkeypatch):
    with JournalStore(tmp_path) as store:
        journal, target, _ = managed(store)
        accepted = accept(store, journal, target)
        associate(store, journal, target, accepted)
        before = store.status('owner', journal)['last_seq']
        original = store._set_control
        def fail(db, key, value):
            if key == 'managed_active' and value is None:
                raise JournalError('storage_error')
            return original(db, key, value)
        monkeypatch.setattr(store, '_set_control', fail)
        with pytest.raises(JournalError, match='storage_error'):
            settle(store, journal, target, accepted)
        assert store.status('owner', journal)['last_seq'] == before
        assert control(store, journal, target)['active_submission'] is not None
        monkeypatch.setattr(store, '_set_control', original)
        result = settle(store, journal, target, accepted)
        assert result['replayed'] is False
        assert settle(store, journal, target, accepted)['replayed'] is True
        assert control(store, journal, target)['active_submission'] is None
        assert len(store.read_page('owner', journal, projection='conversation')['events']) == 2
        accept(store, journal, target, nonce='next')
        assert settle(store, journal, target, accepted)['replayed'] is True


def test_restart_keeps_intent_ambiguous_never_replays_and_keeps_receipt(tmp_path):
    store = JournalStore(tmp_path)
    journal, target, _ = managed(store)
    accepted = accept(store, journal, target)
    store.close()
    with JournalStore(tmp_path) as store:
        snapshot = control(store, journal, target, nonce='submit-1')
        assert snapshot['active_submission']['dispatch_state'] == 'ambiguous'
        assert snapshot['receipt']['event_id'] == accepted['event_id']
        assert accept(store, journal, target)['replayed'] is True
        with pytest.raises(JournalError):
            store.mark_managed_dispatching('owner', journal, target=target,
                submission_id=accepted['submission_id'], cancel_version=0)


def test_permission_cancel_withdraw_expiry_and_receipt_tombstones(tmp_path):
    now = [1000.0]
    with JournalStore(tmp_path, clock=lambda: now[0]) as store:
        journal, target, _ = managed(store)
        accepted = accept(store, journal, target)
        associate(store, journal, target, accepted)
        permission = dict(permission_request_id='permission-1', submission_id=accepted['submission_id'],
                          provider_turn_id='turn-1', tool_name='safe-name', choices=['allow_once', 'deny'])
        store.register_managed_permission('owner', journal, target=target, **permission)
        decision = dict(permission_request_id='permission-1', submission_id=accepted['submission_id'],
                        provider_turn_id='turn-1', decision='allow_once', nonce='decision-1', cancel_version=0)
        assert not store.decide_managed_permission('owner', journal, target=target, **decision)['replayed']
        assert store.decide_managed_permission('owner', journal, target=target, **decision)['replayed']
        with pytest.raises(JournalError, match='nonce_conflict'):
            store.decide_managed_permission('owner', journal, target=target, **dict(decision, decision='deny'))
        store.withdraw_managed_permission('owner', journal, target=target, permission_request_id='permission-1')
        with pytest.raises(JournalError):
            store.decide_managed_permission('owner', journal, target=target, **dict(decision, nonce='fresh'))
        store.register_managed_permission('owner', journal, target=target, **dict(permission, permission_request_id='permission-2'))
        store.cancel_managed_submission('owner', journal, target=target, submission_id=accepted['submission_id'], nonce='cancel')
        with pytest.raises(JournalError):
            store.decide_managed_permission('owner', journal, target=target,
                **dict(decision, permission_request_id='permission-2', nonce='decision-2'))
        assert store.decide_managed_permission('owner', journal, target=target, **decision)['replayed']


def test_unconfirmed_claude_root_accepts_real_input_but_not_fake_final(tmp_path):
    with JournalStore(tmp_path) as store:
        journal, target, _ = managed(store, provider='claude', confirmed=False)
        accepted = accept(store, journal, target)
        assert control(store, journal, target)['provider']['provider_root_id'] is None
        with pytest.raises(JournalError):
            associate(store, journal, target, accepted)
        with pytest.raises(JournalError):
            settle(store, journal, target, accepted)
        store.confirm_managed_provider('owner', journal, target=target, provider='claude', provider_version='2.1.310',
            provider_root_id='provider-root', policy={'mode': 'default'}, confirmed=True)
        associate(store, journal, target, accepted)
        assert settle(store, journal, target, accepted, finals=[dict(provider_record_id='result-1', text='done',
            proof={'kind': 'provider_terminal_result', 'num_turns': 1})])['outcome'] == 'completed'


def test_prompt_bytes_limit_owner_room_and_exact_target(tmp_path):
    with JournalStore(tmp_path) as store:
        journal, target, _ = managed(store)
        for owner, rooms, wrong in [('other', None, target), ('owner', ['revoked'], target),
                                    ('owner', None, dict(target, process_generation='other'))]:
            with pytest.raises(JournalError):
                store.accept_managed_submission(owner, journal, target=wrong, rooms=rooms, nonce='x', text='private')
        with pytest.raises(JournalError, match='prompt_too_large'):
            accept(store, journal, target, text='🙂' * 16385)
        receipt = accept(store, journal, target, text='🙂' * 16384)
        event = store.read_page('owner', journal, projection='conversation')['events'][0]
        assert store.content_bytes(journal, event['content_id']) == ('🙂' * 16384).encode()
        assert event['content_sha256'] == hashlib.sha256(('🙂' * 16384).encode()).hexdigest()
        assert receipt['event_id'] == event['event_id']


def test_concurrent_distinct_nonces_have_one_atomic_winner(tmp_path):
    with JournalStore(tmp_path) as store:
        journal, target, _ = managed(store)
        barrier, results = threading.Barrier(3), []
        def submit(nonce):
            barrier.wait()
            try:
                results.append(accept(store, journal, target, nonce=nonce))
            except JournalError as error:
                results.append(error.code)
        threads = [threading.Thread(target=submit, args=(nonce,)) for nonce in ['first', 'second']]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
        assert sum(isinstance(item, dict) for item in results) == 1
        assert 'managed_busy' in results
        assert len(store.read_page('owner', journal, projection='conversation')['events']) == 1


@pytest.mark.parametrize('dispatch', [False, True])
def test_reopen_ambiguous_never_makes_original_acceptance_replayable(tmp_path, dispatch):
    store = JournalStore(tmp_path)
    journal, target, _ = managed(store)
    accepted = accept(store, journal, target)
    if dispatch:
        store.mark_managed_dispatching('owner', journal, target=target,
            submission_id=accepted['submission_id'], cancel_version=0)
    store.close()
    with JournalStore(tmp_path) as store:
        state = control(store, journal, target)
        assert state['state'] == 'ambiguous'
        assert state['active_submission']['dispatch_state'] == 'ambiguous'
        assert accept(store, journal, target)['replayed']
        with pytest.raises(JournalError):
            accept(store, journal, target, nonce='new')
        assert len(store.read_page('owner', journal, projection='conversation')['events']) == 1


def test_confirmed_provider_binding_is_immutable_and_ack_needs_dispatch_marker(tmp_path):
    with JournalStore(tmp_path) as store:
        journal, target, _ = managed(store)
        accepted = accept(store, journal, target)
        with pytest.raises(JournalError):
            store.associate_managed_turn('owner', journal, target=target, submission_id=accepted['submission_id'],
                user_message_uuid=accepted['user_message_uuid'], provider_root_id='provider-root', provider_turn_id='turn')
        with pytest.raises(JournalError, match='binding_conflict'):
            store.confirm_managed_provider('owner', journal, target=target, provider='codex',
                provider_version='0.167.0', provider_root_id='other-root', policy={'mode': 'default'})
        associate(store, journal, target, accepted)
        for field, value in [('provider_root_id', 'wrong'), ('provider_turn_id', 'wrong'), ('user_message_uuid', 'wrong')]:
            params = dict(submission_id=accepted['submission_id'], user_message_uuid=accepted['user_message_uuid'],
                          provider_root_id='provider-root', provider_turn_id='turn-1')
            with pytest.raises(JournalError):
                store.associate_managed_turn('owner', journal, target=target, **dict(params, **{field: value}))


@pytest.mark.parametrize('proof', [
    {'kind': 'provider_final_phase', 'terminal_status': 'failed'},
    {'kind': 'provider_final_phase', 'submission_id': 'other'},
    {'kind': 'provider_final_phase', 'client_user_message_id': 'other'},
    {'kind': 'provider_final_phase', 'num_turns': 0},
    {'kind': 'provider_final_phase', 'phase': 'commentary'},
    {'kind': 'provider_final_phase', 'control': {'arguments': 'private'}},
])
def test_conflicting_or_control_proof_never_leaves_a_final(tmp_path, proof):
    with JournalStore(tmp_path) as store:
        journal, target, _ = managed(store)
        accepted = accept(store, journal, target)
        associate(store, journal, target, accepted)
        before = store.status('owner', journal)['last_seq']
        with pytest.raises(JournalError):
            settle(store, journal, target, accepted, finals=[dict(provider_record_id='result', text='candidate', proof=proof)])
        assert store.status('owner', journal)['last_seq'] == before
        assert control(store, journal, target)['active_submission'] is not None
        assert len(store.read_page('owner', journal, projection='conversation')['events']) == 1


def test_cancel_request_does_not_override_a_proven_completed_provider_outcome(tmp_path):
    with JournalStore(tmp_path) as store:
        journal, target, _ = managed(store)
        accepted = accept(store, journal, target)
        associate(store, journal, target, accepted)
        store.cancel_managed_submission('owner', journal, target=target,
            submission_id=accepted['submission_id'], nonce='cancel')
        assert settle(store, journal, target, accepted)['outcome'] == 'completed'
        assert len(store.read_page('owner', journal, projection='conversation')['events']) == 2


def test_failed_turn_retains_input_and_original_receipt_across_restart(tmp_path):
    store = JournalStore(tmp_path)
    journal, target, _ = managed(store)
    accepted = accept(store, journal, target)
    associate(store, journal, target, accepted)
    settled = settle(store, journal, target, accepted, outcome='failed', finals=[])
    store.close()
    with JournalStore(tmp_path) as store:
        assert control(store, journal, target, nonce='submit-1')['receipt']['event_id'] == accepted['event_id']
        assert settle(store, journal, target, accepted, outcome='failed', finals=[])['replayed']
        assert store.status('owner', journal)['last_seq'] >= settled['seq']
        assert len(store.read_page('owner', journal, projection='conversation')['events']) == 1


def test_permission_expiry_turn_change_and_nonce_replay_after_settlement(tmp_path):
    now = [1000.0]
    with JournalStore(tmp_path, clock=lambda: now[0]) as store:
        journal, target, _ = managed(store)
        accepted = accept(store, journal, target)
        associate(store, journal, target, accepted)
        register = dict(permission_request_id='request', submission_id=accepted['submission_id'],
            provider_turn_id='turn-1', choices=['allow_once', 'deny'], expires_at=1001.0)
        store.register_managed_permission('owner', journal, target=target, **register)
        decision = dict(permission_request_id='request', submission_id=accepted['submission_id'],
            provider_turn_id='turn-1', decision='deny', nonce='decision', cancel_version=0)
        now[0] = 1002.0
        with pytest.raises(JournalError, match='stale_permission'):
            store.decide_managed_permission('owner', journal, target=target, **decision)
        store.register_managed_permission('owner', journal, target=target,
            **dict(register, permission_request_id='fresh', expires_at=1003.0))
        receipt = store.decide_managed_permission('owner', journal, target=target,
            **dict(decision, permission_request_id='fresh'))
        assert receipt['decision'] == 'deny'
        settle(store, journal, target, accepted)
        next_accepted = accept(store, journal, target, nonce='next')
        assert store.decide_managed_permission('owner', journal, target=target,
            **dict(decision, permission_request_id='fresh'))['replayed']
        with pytest.raises(JournalError):
            store.decide_managed_permission('owner', journal, target=target,
                **dict(decision, permission_request_id='fresh', nonce='new-decision'))
        assert control(store, journal, target)['active_submission']['submission_id'] == next_accepted['submission_id']


def test_permissions_persist_only_registered_safe_metadata_and_bounded_choices(tmp_path):
    with JournalStore(tmp_path) as store:
        journal, target, _ = managed(store)
        accepted = accept(store, journal, target)
        associate(store, journal, target, accepted)
        base = dict(permission_request_id='request', submission_id=accepted['submission_id'], provider_turn_id='turn-1')
        for changes in [dict(choices=['allow_once']), dict(choices=['allow_always', 'deny']),
                        dict(choices=['allow_once', 'deny'], requires_user_interaction=True),
                        dict(expires_at=store._clock() + 1000)]:
            with pytest.raises(JournalError):
                store.register_managed_permission('owner', journal, target=target, **dict(base, **changes))
        descriptor = store.register_managed_permission('owner', journal, target=target,
            **dict(base, requires_user_interaction=True, choices=['deny']))
        assert set(descriptor) == {'permission_request_id', 'submission_id', 'provider_turn_id',
            'tool_name', 'choices', 'requires_user_interaction', 'expires_at', 'cancel_version', 'state'}
        assert 'details' not in json.dumps(control(store, journal, target)['permissions'])
        with pytest.raises(JournalError, match='invalid_policy'):
            store.confirm_managed_provider('owner', journal, target=target, provider='codex',
                provider_version='0.167.0', provider_root_id='provider-root', policy={'nested': {'arguments': 'private'}})


def test_cancel_settlement_failure_rolls_back_latch_outcome_and_active_clear(tmp_path, monkeypatch):
    with JournalStore(tmp_path) as store:
        journal, target, _ = managed(store)
        accepted = accept(store, journal, target)
        original = store._set_control
        def fail(db, key, value):
            if key == 'managed_active' and value is None:
                raise JournalError('storage_error')
            return original(db, key, value)
        before = store.status('owner', journal)['last_seq']
        monkeypatch.setattr(store, '_set_control', fail)
        with pytest.raises(JournalError, match='storage_error'):
            store.cancel_managed_submission('owner', journal, target=target,
                submission_id=accepted['submission_id'], nonce='cancel')
        active = control(store, journal, target)['active_submission']
        assert not active['cancel_requested'] and active['cancel_version'] == 0
        assert store.status('owner', journal)['last_seq'] == before
        monkeypatch.setattr(store, '_set_control', original)
        assert store.cancel_managed_submission('owner', journal, target=target,
            submission_id=accepted['submission_id'], nonce='cancel')['state'] == 'not_dispatched'


def test_permission_decision_failure_rolls_back_once_only_consumption(tmp_path, monkeypatch):
    with JournalStore(tmp_path) as store:
        journal, target, _ = managed(store)
        accepted = accept(store, journal, target)
        associate(store, journal, target, accepted)
        store.register_managed_permission('owner', journal, target=target, permission_request_id='request',
            submission_id=accepted['submission_id'], provider_turn_id='turn-1', choices=['allow_once', 'deny'])
        original = store._set_control
        def fail(db, key, value):
            if key.startswith('managed_permission_nonce:'):
                raise JournalError('storage_error')
            return original(db, key, value)
        params = dict(permission_request_id='request', submission_id=accepted['submission_id'],
            provider_turn_id='turn-1', decision='allow_once', nonce='choice', cancel_version=0)
        monkeypatch.setattr(store, '_set_control', fail)
        with pytest.raises(JournalError, match='storage_error'):
            store.decide_managed_permission('owner', journal, target=target, **params)
        assert control(store, journal, target)['permissions'][0]['state'] == 'pending'
        monkeypatch.setattr(store, '_set_control', original)
        assert not store.decide_managed_permission('owner', journal, target=target, **params)['replayed']


def test_crashed_accepted_control_is_wal_durable_without_close(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    source = Path(__file__).resolve().parents[1] / 'src'
    script = '''import os,sys
from pathlib import Path
from supermonoagent.conversation_store import JournalStore,managed_creation_ids
s=JournalStore(Path(sys.argv[1]),host_installation_id='synthetic-host')
i=managed_creation_ids(s.host_installation_id,'owner','create-1')
r=dict(anchor_window_id='native-window',anchor_session_id='native-session',anchor_process_generation='native-generation',provider='codex',cwd='D:/synthetic',room='room')
s.create('owner',room='room',window_id=i['window_id'],session_id=i['session_id'],process_generation=i['process_generation'],source_epoch='epoch',journal_id=i['journal_id'],managed_creation=dict(nonce='create-1',request=r))
t={k:i[k] for k in ['window_id','session_id','process_generation','journal_id']}
s.confirm_managed_provider('owner',i['journal_id'],target=t,provider='codex',provider_version='0.167.0',provider_root_id='root')
s.accept_managed_submission('owner',i['journal_id'],target=t,nonce='submission',text='synthetic exact input')
os._exit(0)
'''
    env = dict(os.environ, PYTHONPATH=str(source))
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)], env=env,
                            capture_output=True, timeout=15)
    assert result.returncode == 0
    with JournalStore(tmp_path) as store:
        ids = managed_creation_ids(store.host_installation_id, 'owner', 'create-1')
        target = {key: ids[key] for key in ['window_id', 'session_id', 'process_generation', 'journal_id']}
        state = control(store, ids['journal_id'], target, nonce='submission')
        assert state['state'] == 'ambiguous'
        assert state['active_submission']['dispatch_state'] == 'ambiguous'
        event = store.read_page('owner', ids['journal_id'], projection='conversation')['events'][0]
        assert event['event_id'] == state['receipt']['event_id']
        assert store.content_bytes(ids['journal_id'], event['content_id']) == b'synthetic exact input'


def test_many_sequential_permissions_keep_only_live_index_and_all_nonce_tombstones(tmp_path):
    with JournalStore(tmp_path) as store:
        journal, target, _ = managed(store)
        accepted = accept(store, journal, target)
        associate(store, journal, target, accepted)
        for index in range(40):
            permission_id, nonce = 'permission-' + str(index), 'decision-' + str(index)
            store.register_managed_permission('owner', journal, target=target, permission_request_id=permission_id,
                submission_id=accepted['submission_id'], provider_turn_id='turn-1', choices=['allow_once', 'deny'])
            store.decide_managed_permission('owner', journal, target=target, permission_request_id=permission_id,
                submission_id=accepted['submission_id'], provider_turn_id='turn-1', decision='deny', nonce=nonce, cancel_version=0)
            store.withdraw_managed_permission('owner', journal, target=target, permission_request_id=permission_id)
            assert control(store, journal, target)['permissions'] == []
        assert store.decide_managed_permission('owner', journal, target=target, permission_request_id='permission-0',
            submission_id=accepted['submission_id'], provider_turn_id='turn-1', decision='deny', nonce='decision-0',
            cancel_version=0)['replayed']
