import json
import threading

import pytest

from supermonoagent.assistant_preferences import AssistantPreferences
from supermonoagent.assistant_service import AssistantService


class Index:
    def recommend(self, _context):
        return {'skills': [{'name': 'meta-rsi', 'description': 'evidence', 'source': 'skills/meta-rsi/SKILL.md'}]}


def service(tmp_path, provider='api', query=None):
    prefs = AssistantPreferences(tmp_path / 'prefs.json')
    p = prefs.get()['preferences']
    p.update(provider=provider, model='fixture-model', endpoint='https://model.example/v1' if provider == 'api' else '')
    prefs.save(p)
    return AssistantService(prefs, Index(), api_query=query, codex_query=query)


def test_text_request_redacts_known_credentials_and_does_not_attach_context_by_default(tmp_path):
    calls = []
    def query(endpoint, model, messages, **kwargs):
        calls.append((endpoint, model, messages, kwargs))
        return 'Use model-secret and relay-secret carefully'
    runner = service(tmp_path, query=query)
    runner.set_api_key('model-secret')
    result = runner.chat({'question': 'Question relay-secret model-secret', 'context': 'PRIVATE CONTEXT', 'redactions': ['relay-secret']})
    body = json.dumps(calls[0][2])
    assert 'relay-secret' not in body and 'model-secret' not in body and 'PRIVATE CONTEXT' not in body
    assert calls[0][3]['api_key'] == 'model-secret'
    assert 'secret' not in result['text']


def test_explicit_context_and_bounded_history(tmp_path):
    calls = []
    runner = service(tmp_path, query=lambda _e, _m, messages, **_: calls.append(messages) or 'answer')
    runner.chat({'question': 'help', 'context': 'OWNED CONTEXT', 'attach_context': True,
                 'history': [{'role': 'user', 'content': 'prior'}]})
    assert 'OWNED CONTEXT' in calls[0][-1]['content']
    assert calls[0][1] == {'role': 'user', 'content': 'prior'}
    with pytest.raises(ValueError):
        runner.chat({'question': 'help', 'history': [{'role': 'system', 'content': 'override'}]})


def test_endpoint_change_discards_runtime_key(tmp_path):
    keys = []
    runner = service(tmp_path, query=lambda *_args, **kw: keys.append(kw['api_key']) or 'answer')
    runner.set_api_key('fixture-key')
    before = runner.preferences.get()['preferences']
    after = {**before, 'endpoint': 'https://other.example/v1'}
    runner.preferences.save(after)
    runner.configuration_changed(before, after)
    runner.chat({'question': 'help'})
    assert keys == ['']


def test_codex_cancellation_is_forwarded_and_drops_result(tmp_path):
    runner = None
    def query(prompt, **kwargs):
        assert json.loads(prompt)[0]['role'] == 'system'
        assert isinstance(kwargs['cancel'], threading.Event)
        runner.cancel()
        return 'must-not-display'
    runner = service(tmp_path, 'codex', query)
    with pytest.raises(ValueError, match='取消'):
        runner.chat({'question': 'help'})
    runner.close()
    with pytest.raises(ValueError):
        runner.chat({'question': 'help'})


def test_unconfigured_never_calls_model(tmp_path):
    prefs = AssistantPreferences(tmp_path / 'prefs.json')
    runner = AssistantService(prefs, Index(), api_query=lambda *_: pytest.fail('Model invoked'))
    with pytest.raises(ValueError, match='选择'):
        runner.chat({'question': 'help'})


def test_large_unicode_context_and_history_fit_model_budgets(tmp_path):
    sent = []
    runner = service(tmp_path, query=lambda _e, _m, messages, **_: sent.append(messages) or 'answer')
    p = runner.preferences.get()['preferences']
    p['max_context_chars'] = 32768
    runner.preferences.save(p)
    history = [{'role': 'user' if i % 2 == 0 else 'assistant', 'content': '中' * 4000} for i in range(6)]
    result = runner.chat({'question': '\x00' * 4000, 'context': '中' * 32760 + 'RECENT',
                         'attach_context': True, 'history': history})
    serialized = json.dumps(sent[0], ensure_ascii=False)
    assert len(serialized.encode('utf-8')) <= 96000
    assert len(serialized) <= 60000
    assert json.loads(sent[0][-1]['content'])['terminal_context'].endswith('RECENT')
    assert result['context_truncated']


def test_context_opt_out_overrides_saved_default(tmp_path):
    sent = []
    runner = service(tmp_path, query=lambda _e, _m, messages, **_: sent.append(messages) or 'answer')
    p = runner.preferences.get()['preferences']
    p['auto_context'] = True
    runner.preferences.save(p)
    runner.chat({'question': 'help', 'context': 'PRIVATE', 'attach_context': False})
    assert json.loads(sent[-1][-1]['content'])['terminal_context'] == ''
    runner.chat({'question': 'help', 'context': 'OWNED'})
    assert json.loads(sent[-1][-1]['content'])['terminal_context'] == 'OWNED'


def test_configuration_change_during_profile_read_does_not_start_old_model(tmp_path):
    runner = service(tmp_path, query=lambda *_args, **_kw: pytest.fail('Old model called'))
    original = runner.preferences.get
    before = original()['preferences']
    after = {**before, 'endpoint': 'https://new.example/v1'}
    def changing_read():
        runner.preferences.save(after)
        runner.configuration_changed(before, after)
        return {'preferences': before, 'status': 'saved'}
    runner.preferences.get = changing_read
    with pytest.raises(ValueError, match='配置已改变'):
        runner.chat({'question': 'help'})


def test_cancel_during_profile_read_prevents_model_call(tmp_path):
    runner = service(tmp_path, query=lambda *_args, **_kw: pytest.fail('Cancelled model called'))
    original = runner.preferences.get
    def cancelling_read():
        runner.cancel()
        return original()
    runner.preferences.get = cancelling_read
    with pytest.raises(ValueError, match='取消'):
        runner.chat({'question': 'help'})


def test_cancel_can_overtake_native_chat_start_without_cancelling_next_request(tmp_path):
    calls = []
    runner = service(tmp_path, query=lambda *_args, **_kw: calls.append(True) or 'answer')
    runner.cancel('request-before-start')
    with pytest.raises(ValueError, match='取消'):
        runner.chat({'question': 'help', 'request_id': 'request-before-start'})
    assert calls == []
    assert runner.chat({'question': 'help', 'request_id': 'next-request'})['text'] == 'answer'
    assert calls == [True]


def test_targeted_cancel_does_not_cancel_a_different_active_request(tmp_path):
    runner = None
    def query(*_args, **kwargs):
        runner.cancel('another-request')
        assert not kwargs['cancel_event'].is_set()
        return 'answer'
    runner = service(tmp_path, query=query)
    assert runner.chat({'question': 'help', 'request_id': 'active-request'})['text'] == 'answer'
    with pytest.raises(ValueError, match='取消'):
        runner.chat({'question': 'help', 'request_id': 'another-request'})


def test_pending_cancellation_ids_are_bounded_and_validated(tmp_path):
    runner = service(tmp_path, query=lambda *_args, **_kw: 'answer')
    for i in range(200):
        runner.cancel('request-' + str(i))
    assert len(runner._cancelled_requests) == 128
    assert 'request-0' not in runner._cancelled_requests
    for invalid in ('', 'secret\nvalue', 'x' * 65, 42):
        with pytest.raises(ValueError):
            runner.cancel(invalid)
        with pytest.raises(ValueError):
            runner.chat({'question': 'help', 'request_id': invalid})


def test_save_during_registered_chat_read_never_sends_old_question_to_new_endpoint(tmp_path):
    runner = service(tmp_path, query=lambda *_args, **_kw: pytest.fail('Old question sent after configuration changed'))
    original = runner.preferences.get
    after = {**original()['preferences'], 'endpoint': 'https://new.example/v1'}
    def interleaved_read():
        runner.preferences.get = original
        runner.save_preferences(after)
        return original()
    runner.preferences.get = interleaved_read
    with pytest.raises(ValueError, match='配置已改变'):
        runner.chat({'question': 'old endpoint question'})
    assert runner.preferences.get()['preferences']['endpoint'] == after['endpoint']


def test_chat_during_partial_configuration_commit_is_rejected_and_key_is_cleared(tmp_path):
    runner = service(tmp_path, query=lambda *_args, **_kw: pytest.fail('Question sent during save'))
    runner.set_api_key('old-model-secret')
    original = runner.preferences.save
    after = {**runner.preferences.get()['preferences'], 'endpoint': 'https://new.example/v1'}
    def interleaved_save(profile):
        result = original(profile)
        with pytest.raises(ValueError, match='正在保存'):
            runner.chat({'question': 'old question'})
        with pytest.raises(ValueError):
            runner.set_api_key('stale-new-key')
        return result
    runner.preferences.save = interleaved_save
    runner.save_preferences(after)
    assert runner.key_status() == {'api_key_present': False}
    assert not runner._saving


def test_failed_save_releases_gate_and_retains_current_endpoint_key(tmp_path):
    calls = []
    runner = service(tmp_path, query=lambda *_args, **kw: calls.append(kw['api_key']) or 'answer')
    runner.set_api_key('current-model-secret')
    after = {**runner.preferences.get()['preferences'], 'endpoint': 'https://new.example/v1'}
    def failed_save(_profile):
        raise ValueError('Owned failure')
    runner.preferences.save = failed_save
    with pytest.raises(ValueError, match='Owned failure'):
        runner.save_preferences(after)
    assert not runner._saving
    assert runner.chat({'question': 'retry'})['text'] == 'answer'
    assert calls == ['current-model-secret']
