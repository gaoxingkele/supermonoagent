import json

import pytest

from supermonoagent.conversation_contract import JournalError
from supermonoagent.conversation_hooks import (write_hook_config, run_hook,
    claude_settings, hook_argv, main)


def test_codex_fanout_unchanged_even_when_ingestion_fails(tmp_path):
    config = write_hook_config(tmp_path / 'private', binding_id='b', capability='secret',
        terminal_generation='pid', endpoint='http://127.0.0.1:9876/journal-hook',
        original_notifier=['old.exe','fixed arg'])
    raw = '{"type":"agent-turn-complete","thread-id":"root","input-messages":["old","new"]}'
    calls = []
    def fail(*args): raise OSError('secret should never appear')
    assert run_hook(config, 'codex', raw, post=fail, spawn=lambda argv: calls.append(argv)) is False
    assert calls == [['old.exe','fixed arg', raw]]
    assert 'secret' not in str(config)


def test_claude_settings_preserves_existing_hooks_and_payload(tmp_path):
    original = dict(hooks=dict(Stop=[dict(hooks=[dict(type='command',command='existing')])]), other=True)
    settings = claude_settings('python helper --config-file private.json', existing=original)
    assert len(settings['hooks']['Stop']) == 2
    assert len(original['hooks']['Stop']) == 1
    assert settings['other'] is True
    path = write_hook_config(tmp_path / 'private', binding_id='b', capability='secret',
        terminal_generation='gen',endpoint='http://localhost:9876/journal-hook')
    captured = []
    raw = json.dumps(dict(hook_event_name='Stop', session_id='root',prompt_id='p',last_assistant_message='结束🙂'))
    assert run_hook(path,'claude',raw,post=lambda endpoint,body:captured.append((endpoint,body)))
    assert captured[0][1]['payload']['last_assistant_message'] == '结束🙂'
    assert captured[0][1]['payload']['_terminal_generation'] == 'gen'


def test_hook_rejects_remote_and_malformed_without_logging(tmp_path,capsys):
    with pytest.raises(JournalError): write_hook_config(tmp_path, binding_id='b',capability='s',
        terminal_generation='g',endpoint='https://remote.example/hook')
    path = write_hook_config(tmp_path/'private',binding_id='b',capability='s',
        terminal_generation='g',endpoint='http://127.0.0.1:9876/hook')
    assert run_hook(path,'codex','{ secret') is False
    assert main(['--config-file',str(path),'codex','malformed']) == 0
    assert capsys.readouterr() == ('','')


def test_hook_capability_written_only_after_file_acl_is_private(tmp_path,monkeypatch):
    from supermonoagent import conversation_sources as module
    original=module._secure_storage
    def refuse_file(path,directory=False):
        if directory: return original(path,directory)
        assert path.read_bytes()==b''
        raise ValueError('private synthetic path')
    monkeypatch.setattr(module,'_secure_storage',refuse_file)
    with pytest.raises(JournalError,match='private_storage_unavailable'):
        write_hook_config(tmp_path/'private',binding_id='b',capability='must-not-be-written',
            terminal_generation='g',endpoint='http://127.0.0.1:9876/hook')
    assert all(path.read_bytes()==b'' for path in (tmp_path/'private').iterdir())


@pytest.mark.parametrize('frozen',[False,True])
@pytest.mark.parametrize('provider',['codex','claude'])
def test_hook_recipe_routes_frozen_and_python_argv_without_launching(tmp_path,monkeypatch,frozen,provider):
    from supermonoagent import conversation_hooks as module
    monkeypatch.setattr(module.sys,'frozen',frozen,raising=False)
    monkeypatch.setattr(module.sys,'executable','C:/Program Files/Supermono/launcher.exe')
    def forbidden(*args,**kwargs): raise AssertionError('recipe must not start a CLI')
    monkeypatch.setattr(module.subprocess,'Popen',forbidden)
    config=tmp_path/'private config.json'
    prefix=[module.sys.executable,'journal-hook'] if frozen else [
        module.sys.executable,'-m','supermonoagent.conversation_hooks']
    assert hook_argv(config,provider)==prefix+['--config-file',str(config),provider]
    assert not config.exists()
