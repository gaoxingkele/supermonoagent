"""Owned local worktrees only; never contacts GitHub or the real mylib."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from supermonoagent import mylib_sync as S
from supermonoagent.mylib_local import LocalLibrary, LocalLibraryError
from supermonoagent.mylib_evolution import _FILES as ENGINE_FILES


SOURCE = 'https://example.invalid/mylib.git'


def git(path, *args, check=True):
    env = S._environment(path)
    result = subprocess_run(['git', '-c', 'user.name=Owned test', '-c', 'user.email=owned@example.invalid',
                             '-c', 'protocol.file.allow=always', '-C', str(path), *args], env=env)
    if check and result.returncode:
        raise AssertionError(result.stderr.decode('utf-8', 'replace'))
    return result.stdout.decode().strip()


def subprocess_run(arguments, *, env):
    return S._run(arguments, cwd=Path.cwd(), env=env, timeout=30, max_output=1024 * 1024)


def repository(path, text='---\nname: demo\ndescription: owned fixture\n---\n# Demo\n'):
    path.mkdir(parents=True)
    git(path, 'init', '-b', 'main')
    (path / 'SKILL.md').write_text(text, encoding='utf-8')
    git(path, 'add', 'SKILL.md')
    git(path, 'commit', '-m', 'Owned initial')
    return path


def clone(source, destination):
    git(destination.parent, 'clone', str(source), str(destination))
    git(destination, 'remote', 'set-url', 'origin', SOURCE)
    return destination


def bare_remote(source, destination):
    git(source.parent, 'clone', '--bare', str(source), str(destination))
    return destination


def change(repo, text, path='SKILL.md', message='Owned change'):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding='utf-8')
    git(repo, 'add', path)
    git(repo, 'commit', '-m', message)
    return git(repo, 'rev-parse', 'HEAD')


class Runner:
    def __init__(self, sources):
        self.sources = sources
        self.calls = []

    def __call__(self, args, *, cwd, env, timeout, max_output, check=None):
        self.calls.append(list(args))
        assert env['GIT_TERMINAL_PROMPT'] == '0'
        actual = [value.replace('protocol.file.allow=never', 'protocol.file.allow=always') for value in args]
        if 'fetch' in actual or 'push' in actual:
            assert 'protocol.file.allow=never' in args
            assert 'core.hooksPath=' in ' '.join(args)
            index = actual.index('origin')
            remote = git(Path(cwd), 'config', '--get', 'remote.origin.url')
            actual[index] = str(self.sources[remote])
        return S._run(actual, cwd=cwd, env=env, timeout=timeout, max_output=max_output, check=check)


def service(work, remote, state):
    return LocalLibrary(work, state, runner=Runner({SOURCE: remote}))


def test_relative_library_root_is_rejected(tmp_path):
    with pytest.raises(LocalLibraryError, match='invalid_config'):
        LocalLibrary('relative/mylib', tmp_path / 'state')


def test_pull_fast_forward_and_dirty_worktree_guard(tmp_path):
    remote = repository(tmp_path / 'remote')
    work = clone(remote, tmp_path / 'work')
    library = service(work, remote, tmp_path / 'state')
    revision = change(remote, '---\nname: demo\ndescription: updated upstream\n---\n# Updated\n')
    result = library.pull()
    assert result['changed'] and result['revision'] == revision
    assert (work / 'SKILL.md').read_text().startswith('---\nname: demo\ndescription: updated upstream')
    assert not library.pull()['changed']
    (work / 'dirty.txt').write_text('do not overwrite', encoding='utf-8')
    with pytest.raises(LocalLibraryError, match='dirty_worktree'):
        library.pull()
    assert (work / 'dirty.txt').read_text() == 'do not overwrite'


def test_commit_preview_scopes_staging_excludes_secrets_and_hooks(tmp_path):
    remote = bare_remote(repository(tmp_path / 'source'), tmp_path / 'remote.git')
    work = clone(remote, tmp_path / 'work')
    library = service(work, remote, tmp_path / 'state')
    (work / 'SKILL.md').write_text('---\nname: demo\ndescription: local evolution\n---\n# Local\n', encoding='utf-8')
    (work / 'notes.txt').write_text('owned note', encoding='utf-8')
    (work / '.metarsi').mkdir()
    (work / '.metarsi/secret.txt').write_text('must-not-stage', encoding='utf-8')
    (work / '.git/hooks/pre-commit').write_text('#!/bin/sh\ntouch hook-ran\n', encoding='utf-8')
    (work / '.git/hooks/pre-commit').chmod(0o755)
    preview = library.commit_preview()
    assert preview['can_commit'] and preview['excluded_count'] == 1
    assert {item['path'] for item in preview['items'] if not item['excluded']} == {'SKILL.md', 'notes.txt'}
    result = library.commit_and_push(preview['fingerprint'])
    assert all(item['excluded'] for item in result['changes']) and result['excluded_count'] == 1
    assert 'local evolution' in git(remote, 'show', 'HEAD:SKILL.md')
    assert git(remote, 'show', 'HEAD:notes.txt') == 'owned note'
    with pytest.raises(AssertionError):
        git(remote, 'cat-file', '-e', 'HEAD:.metarsi/secret.txt')
    assert not (work / 'hook-ran').exists()


def test_preview_fingerprint_blocks_new_changes(tmp_path):
    remote = bare_remote(repository(tmp_path / 'source'), tmp_path / 'remote.git')
    work = clone(remote, tmp_path / 'work')
    library = service(work, remote, tmp_path / 'state')
    (work / 'one.txt').write_text('one', encoding='utf-8')
    preview = library.commit_preview()
    (work / 'two.txt').write_text('two', encoding='utf-8')
    with pytest.raises(LocalLibraryError, match='changes_changed'):
        library.commit_and_push(preview['fingerprint'])
    with pytest.raises(AssertionError):
        git(remote, 'cat-file', '-e', 'HEAD:one.txt')
    with pytest.raises(AssertionError):
        git(remote, 'cat-file', '-e', 'HEAD:two.txt')


def test_untracked_directory_is_valid_change_but_duplicate_separator_is_rejected(tmp_path):
    remote = bare_remote(repository(tmp_path / 'source'), tmp_path / 'remote.git')
    work = clone(remote, tmp_path / 'work')
    directory = work / 'Graphic/handraw-style'
    repository(directory)
    preview = service(work, remote, tmp_path / 'state').commit_preview()
    assert preview['can_commit']
    assert any(item['path'] == 'Graphic/handraw-style/' for item in preview['items'])
    from supermonoagent.mylib_local import _relative
    with pytest.raises(LocalLibraryError, match='invalid_path'):
        _relative('Graphic/handraw-style//')


def test_apply_candidate_requires_exact_source(tmp_path):
    remote = repository(tmp_path / 'remote')
    work = clone(remote, tmp_path / 'work')
    library = service(work, remote, tmp_path / 'state')
    source = {'revision': 'a' * 64, 'skill_id': 'b' * 64, 'path': 'SKILL.md',
              'sha256': hashlib.sha256((work / 'SKILL.md').read_bytes()).hexdigest()}
    result = library.apply_candidate(source, b'candidate\n')
    assert result['requires_commit'] and (work / 'SKILL.md').read_text() == 'candidate\n'
    with pytest.raises(LocalLibraryError, match='source_drift'):
        library.apply_candidate(source, b'changed again\n')


def copy_engine(destination):
    source = Path('D:/aicoding/mylib/skills/meta-rsi')
    if not source.exists():
        pytest.skip('Read-only metaRSI engine fixture unavailable')
    for name in ENGINE_FILES:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((source / name).read_bytes())


def make_library(root):
    (root / 'skill-runtime').mkdir(parents=True)
    (root / 'skill-runtime/manifest.json').write_text(json.dumps({'skills': [{'source': '../skills/demo'}]}), encoding='utf-8')
    (root / 'skills/demo').mkdir(parents=True)
    (root / 'skills/demo/SKILL.md').write_text('---\nname: demo\ndescription: current window fixture\n---\n# Demo\n', encoding='utf-8')
    copy_engine(root / 'skills/meta-rsi')


def test_current_window_evolution_and_explicit_apply(tmp_path):
    root = tmp_path / 'library'
    make_library(root)
    original = (root / 'skills/demo/SKILL.md').read_bytes()
    library = LocalLibrary(root, tmp_path / 'state')
    result = library.run_evolution({'text': 'ValueError: first failure\nError: second failure', 'target': 'demo window'})
    assert result['count'] == 1
    item = result['items'][0]
    assert item['source']['path'] == 'skills/demo/SKILL.md'
    assert '## Verification' in item['candidate_text']
    assert (root / 'skills/demo/SKILL.md').read_bytes() == original
    applied = library.apply_evolution(item['suggestion_id'])
    assert applied['requires_commit']
    assert '## Verification' in (root / 'skills/demo/SKILL.md').read_text()
    with pytest.raises(LocalLibraryError, match='source_drift'):
        library.apply_evolution(item['suggestion_id'])


def test_desktop_directory_switch_reloads_index(tmp_path):
    from supermonoagent.desktop_assistant import DesktopAssistant

    first, second = tmp_path / 'first', tmp_path / 'second'
    for root in (first, second):
        (root / 'skill-runtime').mkdir(parents=True)
        (root / 'skill-runtime/manifest.json').write_text(json.dumps({'skills': []}), encoding='utf-8')
    class Event:
        def __iadd__(self, callback):
            return self

    current = ['http://127.0.0.1:6771/workspace']
    window = SimpleNamespace(expose=lambda *_: None, run_js=lambda _: None,
        get_current_url=lambda: current[0], events=SimpleNamespace(loaded=Event(), closed=Event()))
    assistant = DesktopAssistant(window, current[0], first, preferences_path=tmp_path / 'prefs.json')
    preferences = assistant.get_assistant_preferences()['preferences']
    preferences['library_root'] = str(second)
    assistant.save_assistant_preferences(preferences)
    assert assistant._library_root == str(second)
    assert assistant._index.root == second
    assert assistant._library.root == second
    assistant._close()


def test_directory_access_probe_checks_existence_type_and_read_permission(tmp_path, monkeypatch):
    from supermonoagent.mylib_local import readable_library_root

    root = tmp_path / 'library'
    root.mkdir()
    assert readable_library_root(root) == root
    assert list(root.iterdir()) == []
    missing = tmp_path / 'missing'
    file = tmp_path / 'not-a-directory'
    file.write_text('not a directory', encoding='utf-8')
    for candidate in (missing, file):
        with pytest.raises(LocalLibraryError, match='directory_unavailable'):
            readable_library_root(candidate)
    def denied(_path):
        raise PermissionError('fixture denied')
    monkeypatch.setattr('supermonoagent.mylib_local.os.scandir', denied)
    with pytest.raises(LocalLibraryError, match='directory_unavailable'):
        readable_library_root(root)
    runner = lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=b'')
    assert LocalLibrary(root, tmp_path / 'state', runner=runner).status()['accessible'] is False


def test_invalid_directory_change_preserves_preferences_and_live_index(tmp_path, monkeypatch):
    from supermonoagent.desktop_assistant import DesktopAssistant

    class Event:
        def __iadd__(self, callback):
            return self

    current = ['http://127.0.0.1:6771/workspace']
    window = SimpleNamespace(expose=lambda *_: None, run_js=lambda _: None,
        get_current_url=lambda: current[0], events=SimpleNamespace(loaded=Event(), closed=Event()))
    root = tmp_path / 'library'
    root.mkdir()
    preferences_path = tmp_path / 'prefs.json'
    assistant = DesktopAssistant(window, current[0], root, preferences_path=preferences_path)
    preferences = assistant.get_assistant_preferences()['preferences']
    preferences['library_root'] = str(root)
    assistant.save_assistant_preferences(preferences)
    original_bytes = preferences_path.read_bytes()
    original_index, original_library = assistant._index, assistant._library
    missing = tmp_path / 'missing'
    file = tmp_path / 'not-a-directory'
    file.write_text('not a directory', encoding='utf-8')
    for candidate in (missing, file):
        with pytest.raises(LocalLibraryError, match='directory_unavailable'):
            assistant.save_assistant_preferences({**preferences, 'library_root': str(candidate)})
        assert assistant._library_root == str(root)
        assert assistant._index is original_index and assistant._library is original_library
        assert preferences_path.read_bytes() == original_bytes
    monkeypatch.setattr('supermonoagent.desktop_assistant.readable_library_root',
                        lambda _value: pytest.fail('Unchanged model settings must not rescan the library'))
    assistant.save_assistant_preferences(preferences)
    assert assistant._library_root == str(root)
    assistant._close()
