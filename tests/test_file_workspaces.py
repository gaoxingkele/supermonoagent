from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from supermonoagent.file_workspaces import FileWorkspaces, InvalidSettings, RevisionConflict, timestamped_name


def folders(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    library = tmp_path / 'library'
    library.mkdir()
    settings = tmp_path / 'state' / 'file-workspaces.json'
    return FileWorkspaces(settings, default_mylib_root=library), project, library, settings


def test_home_startup_needs_project_selection_without_creating_directories(tmp_path, monkeypatch):
    home = tmp_path / 'synthetic-home'; home.mkdir()
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    manager = FileWorkspaces(tmp_path / 'state' / 'settings.json', default_mylib_root=home)
    cfg = manager.get('window-1', home)
    assert cfg['project_status'] == 'selection_required'
    assert cfg['project_dir'] == str(home) and cfg['exchange_dir'] == str(home / 'tmp')
    assert cfg['source'] == 'startup' and cfg['revision'] == 0
    for operation in [lambda: manager.guard('window-1', home, 0),
                      lambda: manager.save('window-1', home, expected_revision=0)]:
        with pytest.raises(ValueError, match='project_required'):
            operation()
    with pytest.raises(ValueError, match='project_required'):
        with manager.publish_guard('window-1', home, 0):
            pytest.fail('Home must never be a publication target')
    assert not (tmp_path / 'state').exists() and list(home.iterdir()) == []
    project = home / 'real-project'; project.mkdir()
    selected = manager.save('window-1', project, expected_revision=0)
    assert selected['project_status'] == 'ready' and selected['exchange_dir'] == str(project / 'tmp')
    assert manager.guard('window-1', home, 1) == selected
    assert not (project / 'tmp').exists()


def test_legacy_configured_home_requires_reselection_but_can_be_repaired(tmp_path, monkeypatch):
    home = tmp_path / 'synthetic-home'; home.mkdir()
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    settings = tmp_path / 'settings.json'
    settings.write_text(json.dumps({'schema': 1, 'windows': {'window-1': {
        'project_dir': str(home), 'exchange_dir': str(home / 'tmp'), 'revision': 3}}, 'mylib': None}))
    before = settings.read_bytes()
    manager = FileWorkspaces(settings, default_mylib_root=home)
    assert manager.get('window-1', home)['project_status'] == 'selection_required'
    with pytest.raises(ValueError, match='project_required'):
        manager.guard('window-1', home, 3)
    assert settings.read_bytes() == before and list(home.iterdir()) == []
    project = tmp_path / 'project'; project.mkdir()
    assert manager.save('window-1', project, expected_revision=3)['revision'] == 4


def test_defaults_are_readonly_and_do_not_create_configuration_or_exchange(tmp_path):
    manager, project, library, settings = folders(tmp_path)
    before = (project.stat().st_mtime_ns, library.stat().st_mtime_ns)
    result = manager.get('window-1', project)
    assert result['project_dir'] == str(project.resolve())
    assert result['exchange_dir'] == str(project / 'tmp')
    assert result['source'] == 'startup' and result['revision'] == 0
    assert result['project_status'] == 'ready' and result['exchange_status'] == 'missing'
    assert manager.get_mylib() == {'mylib_root': str(library.resolve()), 'source': 'default', 'revision': 0, 'status': 'ready'}
    assert manager.validate_revision('window-1', 0)
    assert manager.guard('window-1', project, 0) == result
    manager.browse(project)
    assert not settings.parent.exists()
    assert list(project.iterdir()) == [] and list(library.iterdir()) == []
    assert (project.stat().st_mtime_ns, library.stat().st_mtime_ns) == before


def test_per_window_configuration_cas_restart_and_independent_host_library(tmp_path):
    manager, first, library, settings = folders(tmp_path)
    second = tmp_path / 'second-project'
    second.mkdir()
    exchange = second / 'not-created' / 'exchange'
    saved = manager.save('window-1', second, exchange, expected_revision=0)
    assert saved['source'] == 'configured' and saved['revision'] == 1
    assert manager.get('window-1', first)['project_dir'] == str(second.resolve())
    assert manager.get('window-2', first)['source'] == 'startup'
    assert manager.save('window-2', first, expected_revision=0)['exchange_dir'] == str(first / 'tmp')
    other = FileWorkspaces(settings, default_mylib_root=library)
    assert other.get('window-1', first) == saved
    with pytest.raises(RevisionConflict):
        other.save('window-1', first, expected_revision=0)
    assert manager.get_mylib()['revision'] == 0
    lib_saved = other.save_mylib(second, expected_revision=0)
    assert lib_saved == {'mylib_root': str(second.resolve()), 'source': 'configured', 'revision': 1, 'status': 'ready'}
    assert manager.get_mylib() == lib_saved
    assert manager.get('window-1', first)['revision'] == 1
    with pytest.raises(RevisionConflict):
        manager.save_mylib(library, expected_revision=0)
    assert not exchange.exists() and not (first / 'tmp').exists()


@pytest.mark.parametrize('location', ['project-itself', 'outside', 'sibling-prefix', 'parent-traversal'])
def test_exchange_must_be_a_true_project_subdirectory(tmp_path, location):
    manager, project, _library, settings = folders(tmp_path)
    target = {'project-itself': project, 'outside': tmp_path / 'outside',
              'sibling-prefix': tmp_path / 'project-other' / 'tmp',
              'parent-traversal': project / '..' / 'outside'}[location]
    with pytest.raises(ValueError):
        manager.save('window-1', project, target, expected_revision=0)
    assert not settings.parent.exists()
    assert list(project.iterdir()) == []


def test_custom_nested_exchange_and_external_host_library_are_allowed(tmp_path):
    manager, project, library, _settings = folders(tmp_path)
    nested = project / 'attachments' / 'downloads'
    saved = manager.save('window-1', project, nested, expected_revision=0)
    assert saved['exchange_dir'] == str(nested.resolve())
    assert manager.guard('window-1', project, 1)['exchange_dir'] == saved['exchange_dir']
    assert manager.save_mylib(library, expected_revision=0)['mylib_root'] == str(library.resolve())
    assert not nested.exists()


def test_old_external_exchange_is_rejected_without_migration_or_overwrite(tmp_path):
    manager, project, _library, settings = folders(tmp_path)
    settings.parent.mkdir()
    raw = json.dumps({'schema': 1, 'windows': {'window-1': {
        'project_dir': str(project), 'exchange_dir': str(tmp_path / 'legacy-outside'),
        'revision': 1}}, 'mylib': None}).encode()
    settings.write_bytes(raw)
    before = settings.stat().st_mtime_ns
    def publish():
        with manager.publish_guard('window-1', project, 1):
            pytest.fail('Unsafe legacy configuration must not publish')
    for operation in [lambda: manager.get('window-1', project),
                      lambda: manager.guard('window-1', project, 1), publish,
                      lambda: manager.save('window-1', project, project / 'tmp', expected_revision=1)]:
        with pytest.raises(InvalidSettings):
            operation()
        assert settings.read_bytes() == raw and settings.stat().st_mtime_ns == before
    assert list(settings.parent.iterdir()) == [settings]
    assert list(project.iterdir()) == []


def test_guard_rejects_changed_revision_and_missing_project(tmp_path):
    manager, project, _library, _settings = folders(tmp_path)
    manager.save('window-1', project, expected_revision=0)
    assert not manager.validate_revision('window-1', 0)
    with pytest.raises(RevisionConflict):
        manager.guard('window-1', project, 0)
    project.rmdir()
    result = manager.get('window-1', tmp_path)
    assert result['source'] == 'configured' and result['project_status'] == 'missing'
    with pytest.raises(ValueError):
        manager.guard('window-1', tmp_path, 1)


@pytest.mark.parametrize('bad', [b'{broken', b'{"schema":1,"schema":1,"windows":{},"mylib":null}',
    b'{"schema":true,"windows":{},"mylib":null}', b'{"schema":1,"windows":[],"mylib":null}',
    b'{"schema":1,"windows":{},"mylib":null,"extra":1}'])
def test_corrupt_settings_are_not_overwritten_or_replaced_by_defaults(tmp_path, bad):
    manager, project, _library, settings = folders(tmp_path)
    settings.parent.mkdir()
    settings.write_bytes(bad)
    before = (settings.read_bytes(), settings.stat().st_mtime_ns)
    with pytest.raises(InvalidSettings):
        manager.get('window-1', project)
    with pytest.raises(InvalidSettings):
        manager.save('window-1', project, expected_revision=0)
    assert (settings.read_bytes(), settings.stat().st_mtime_ns) == before
    assert list(settings.parent.iterdir()) == [settings]


def test_invalid_persisted_path_revision_or_field_is_rejected(tmp_path):
    manager, project, _library, settings = folders(tmp_path)
    settings.parent.mkdir()
    for entry in [{'project_dir': 'relative', 'exchange_dir': str(project), 'revision': 1},
                  {'project_dir': str(project), 'exchange_dir': str(project), 'revision': True},
                  {'project_dir': str(project), 'exchange_dir': str(project), 'revision': 1, 'unknown': 1}]:
        raw = json.dumps({'schema': 1, 'windows': {'window-1': entry}, 'mylib': None}).encode()
        settings.write_bytes(raw)
        with pytest.raises(InvalidSettings):
            manager.get_mylib()
        assert settings.read_bytes() == raw


def test_missing_default_library_is_explicit_and_not_created(tmp_path):
    root = tmp_path / 'missing-library'
    manager = FileWorkspaces(tmp_path / 'not-created' / 'settings.json', default_mylib_root=root)
    assert manager.get_mylib()['status'] == 'missing'
    with pytest.raises(ValueError):
        manager.save_mylib(root, expected_revision=0)
    assert not root.exists() and not manager.settings_file.parent.exists()


def test_failed_atomic_save_leaves_previous_settings_and_revision_intact(tmp_path, monkeypatch):
    manager, project, _library, settings = folders(tmp_path)
    manager.save('window-1', project, expected_revision=0)
    before = settings.read_bytes()
    def fail_replace(*_args):
        raise PermissionError('fixture replacement denied')
    with monkeypatch.context() as temporary:
        temporary.setattr(os, 'replace', fail_replace)
        with pytest.raises(ValueError, match='Unable to save'):
            manager.save('window-1', project, expected_revision=1)
    assert settings.read_bytes() == before
    assert manager.get('window-1', project)['revision'] == 1
    assert not list(settings.parent.glob('.file-workspaces-*.tmp'))


def test_two_processes_cannot_both_save_the_same_expected_revision(tmp_path):
    manager, project, _library, settings = folders(tmp_path)
    code = '''
import json,sys
from supermonoagent.file_workspaces import FileWorkspaces,RevisionConflict
manager=FileWorkspaces(sys.argv[1])
print('ready',flush=True)
sys.stdin.readline()
try:
    manager.save('shared-window',sys.argv[2],expected_revision=0)
    print(json.dumps({'saved':True}))
except RevisionConflict:
    print(json.dumps({'conflict':True}))
'''
    workers = []
    environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    try:
        for _ in range(2):
            workers.append(subprocess.Popen([sys.executable, '-c', code, str(settings), str(project)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=environment, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)))
        for worker in workers:
            assert worker.stdout.readline().strip() == 'ready'
        for worker in workers:
            worker.stdin.write('go\n')
            worker.stdin.flush()
        outcomes = []
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=10)
            assert worker.returncode == 0, stderr
            outcomes.append(json.loads(stdout))
        assert sum(row.get('saved', False) for row in outcomes) == 1
        assert sum(row.get('conflict', False) for row in outcomes) == 1
        assert manager.get('shared-window', project)['revision'] == 1
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=5)


def test_parallel_window_updates_keep_both_windows_and_host_library(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    manager, project, library, settings = folders(tmp_path)
    other = FileWorkspaces(settings, default_mylib_root=library)
    def save_window(index):
        return (manager if index == 1 else other).save('window-' + str(index), project, expected_revision=0)
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(save_window, [1, 2]))
    assert [row['revision'] for row in outcomes] == [1, 1]
    assert manager.get('window-1', project)['source'] == 'configured'
    assert other.get('window-2', project)['source'] == 'configured'
    assert manager.get_mylib()['revision'] == 0


def test_publish_guard_can_lock_defaults_without_creating_settings_or_exchange(tmp_path):
    manager, project, library, settings = folders(tmp_path)
    before = (project.stat().st_mtime_ns, library.stat().st_mtime_ns)
    assert not settings.parent.exists()
    with manager.publish_guard('window-1', project, 0) as config:
        assert config == manager.get('window-1', project)
        assert config['revision'] == 0 and config['source'] == 'startup'
        assert not settings.exists() and not (project / 'tmp').exists()
    assert not settings.exists()
    assert settings.with_name(settings.name + '.lock').is_file()
    assert (project.stat().st_mtime_ns, library.stat().st_mtime_ns) == before


def test_publish_guard_missing_project_does_not_initialize_it_for_a_lock_file(tmp_path):
    missing = tmp_path / 'missing-project'
    manager = FileWorkspaces(missing / 'settings.json', default_mylib_root=tmp_path)
    with pytest.raises(ValueError):
        with manager.publish_guard('window-1', missing, 0):
            pytest.fail('Missing project must never enter publication')
    assert not missing.exists()


def test_publish_guard_blocks_another_instance_save_until_exit(tmp_path):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    import threading
    manager, project, library, settings = folders(tmp_path)
    manager.save('window-1', project, expected_revision=0)
    other = FileWorkspaces(settings, default_mylib_root=library)
    entered = threading.Event()
    before = settings.read_bytes()
    def save():
        entered.set()
        return other.save('window-1', project, expected_revision=1)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with manager.publish_guard('window-1', project, 1) as config:
            future = executor.submit(save)
            assert entered.wait(2)
            with pytest.raises(TimeoutError):
                future.result(timeout=.05)
            assert settings.read_bytes() == before and config['revision'] == 1
        assert future.result(timeout=5)['revision'] == 2


def test_publish_guard_blocks_another_process_save_until_exit(tmp_path):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    manager, project, _library, settings = folders(tmp_path)
    manager.save('window-1', project, expected_revision=0)
    code = '''
import json,sys
from supermonoagent.file_workspaces import FileWorkspaces
manager=FileWorkspaces(sys.argv[1])
print('ready',flush=True)
sys.stdin.readline()
print('attempting',flush=True)
saved=manager.save('window-1',sys.argv[2],expected_revision=1)
print(json.dumps({'revision':saved['revision']}),flush=True)
'''
    environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    worker = subprocess.Popen([sys.executable, '-c', code, str(settings), str(project)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=environment, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        assert worker.stdout.readline().strip() == 'ready'
        before = settings.read_bytes()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with manager.publish_guard('window-1', project, 1):
                worker.stdin.write('go\n')
                worker.stdin.flush()
                assert worker.stdout.readline().strip() == 'attempting'
                result = executor.submit(worker.communicate, timeout=5)
                with pytest.raises(TimeoutError):
                    result.result(timeout=.1)
                assert settings.read_bytes() == before
            stdout, stderr = result.result(timeout=6)
        assert worker.returncode == 0, stderr
        assert json.loads(stdout) == {'revision': 2}
        assert manager.get('window-1', project)['revision'] == 2
    finally:
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=5)


def test_publish_guard_wrong_revision_never_enters_body_or_changes_settings(tmp_path):
    manager, project, _library, settings = folders(tmp_path)
    manager.save('window-1', project, expected_revision=0)
    before = (settings.read_bytes(), settings.stat().st_mtime_ns)
    entered = False
    with pytest.raises(RevisionConflict):
        with manager.publish_guard('window-1', project, 0):
            entered = True
    assert not entered
    assert (settings.read_bytes(), settings.stat().st_mtime_ns) == before
    assert not (project / 'tmp').exists()


def test_publish_guard_exception_releases_lock_and_preserves_settings(tmp_path):
    manager, project, library, settings = folders(tmp_path)
    manager.save('window-1', project, expected_revision=0)
    before = settings.read_bytes()
    with pytest.raises(RuntimeError, match='fixture failed'):
        with manager.publish_guard('window-1', project, 1):
            raise RuntimeError('fixture failed')
    assert settings.read_bytes() == before
    other = FileWorkspaces(settings, default_mylib_root=library)
    assert other.save('window-1', project, expected_revision=1)['revision'] == 2


def test_directory_browse_paginates_only_local_subdirectories(tmp_path):
    manager, project, _library, settings = folders(tmp_path)
    for number in range(207):
        (project / f'dir-{number:03d}').mkdir()
    (project / 'private-file.txt').write_text('never returned')
    first = manager.browse(project, limit=200)
    second = manager.browse(project, offset=first['next_offset'], limit=200)
    names = [item['name'] for item in first['directories'] + second['directories']]
    assert names == [f'dir-{number:03d}' for number in range(207)]
    assert len(first['directories']) == 200 and len(second['directories']) == 7
    assert first['path'] == str(project.resolve()) and first['parent'] == str(tmp_path.resolve())
    assert second['next_offset'] is None and 'private-file' not in str(first)
    assert all(Path(item['path']).parent == project for item in first['directories'])
    assert not settings.parent.exists()


@pytest.mark.parametrize('path', ['relative', 'C:relative', '\\root-relative', '\\\\server\\share',
    '//server/share', '\\\\?\\C:\\project', '\\\\.\\C:\\project', 'C:\\bad\x00name'])
def test_unsafe_directory_input_rejected_before_resolving_network_or_device_paths(tmp_path, monkeypatch, path):
    manager, _project, _library, _settings = folders(tmp_path)
    def forbidden(*_args, **_kwargs):
        pytest.fail('Unsafe path must be rejected before filesystem resolution')
    monkeypatch.setattr(Path, 'resolve', forbidden)
    with pytest.raises(ValueError):
        manager.browse(path)


@pytest.mark.skipif(os.name != 'nt', reason='Windows mapped drive semantics')
def test_network_mapped_drive_is_rejected_before_filesystem_resolution(tmp_path, monkeypatch):
    import supermonoagent.file_workspaces as module
    manager, project, _library, _settings = folders(tmp_path)
    monkeypatch.setattr(module, '_drive_type', lambda _anchor: 4)
    def forbidden(*_args, **_kwargs):
        pytest.fail('Network drive must not be resolved')
    monkeypatch.setattr(Path, 'resolve', forbidden)
    with pytest.raises(ValueError, match='local disk'):
        manager.browse(project)


@pytest.mark.parametrize('window', ['', '../escape', 'bad/id', 'room:window', 'x' * 65, None])
def test_window_ids_are_bounded_and_not_paths(tmp_path, window):
    manager, project, _library, _settings = folders(tmp_path)
    with pytest.raises(ValueError):
        manager.get(window, project)


@pytest.mark.skipif(os.name != 'nt', reason='Windows local drive/junction semantics')
def test_local_drive_roots_and_explicit_cross_drive_selection(tmp_path):
    manager, project, _library, _settings = folders(tmp_path)
    roots = manager.browse()['roots']
    assert project.anchor in roots
    other = next((path for path in roots if path.casefold() != project.anchor.casefold()), None)
    if other is None:
        pytest.skip('Second local drive unavailable')
    with pytest.raises(ValueError):
        manager.save('window-1', other, project / 'not-created', expected_revision=0)
    default_exchange = Path(other) / 'tmp'
    existed = default_exchange.exists()
    result = manager.save('window-1', other, expected_revision=0)
    assert Path(result['project_dir']).anchor.casefold() == other.casefold()
    assert Path(result['exchange_dir']).is_relative_to(Path(other))
    assert default_exchange.exists() == existed
    assert not (project / 'not-created').exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction semantics')
def test_junction_is_not_browsed_or_saved_and_target_is_untouched(tmp_path):
    manager, project, _library, _settings = folders(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = project / 'link'
    completed = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
        capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if completed.returncode:
        pytest.skip('Junction creation unavailable')
    try:
        assert manager.browse(project)['directories'] == []
        for operation in [lambda: manager.browse(link),
                          lambda: manager.save('window-1', link, expected_revision=0),
                          lambda: manager.save('window-1', project, link / 'tmp', expected_revision=0),
                          lambda: manager.save_mylib(link, expected_revision=0)]:
            with pytest.raises(ValueError):
                operation()
        assert list(outside.iterdir()) == []
    finally:
        link.rmdir()


def test_directory_symlink_is_rejected_without_following_target(tmp_path):
    manager, project, _library, _settings = folders(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = project / 'link'
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('Directory symlink privileges unavailable')
    assert manager.browse(project)['directories'] == []
    with pytest.raises(ValueError):
        manager.save('window-1', link, expected_revision=0)
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction semantics')
def test_legacy_exchange_junction_cannot_hide_an_external_canonical_directory(tmp_path):
    manager, project, _library, settings = folders(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = project / 'legacy-link'
    result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
        capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        pytest.skip('Junction creation unavailable')
    try:
        settings.parent.mkdir()
        raw = json.dumps({'schema': 1, 'windows': {'window-1': {
            'project_dir': str(project), 'exchange_dir': str(link / 'tmp'), 'revision': 1}}, 'mylib': None}).encode()
        settings.write_bytes(raw)
        with pytest.raises(ValueError):
            manager.get('window-1', project)
        with pytest.raises(ValueError):
            manager.save('window-1', project, project / 'tmp', expected_revision=1)
        assert settings.read_bytes() == raw
        assert list(settings.parent.iterdir()) == [settings]
        assert list(outside.iterdir()) == []
    finally:
        link.rmdir()


@pytest.mark.parametrize('offset,limit', [(0, 201), (False, 100), (0, True), (-1, 100), (100001, 100)])
def test_browse_page_parameters_are_bounded(tmp_path, offset, limit):
    manager, project, _library, _settings = folders(tmp_path)
    with pytest.raises(ValueError):
        manager.browse(project, offset=offset, limit=limit)


def test_timestamp_name_uses_given_aware_seconds_and_retains_unicode_extension():
    now = datetime(2026, 10, 25, 9, 8, 7, 999999, tzinfo=timezone(timedelta(hours=8)))
    assert timestamped_name('研究报告.pdf', now) == '20261025-090807-研究报告.pdf'
    assert timestamped_name('20200101-000000-original.txt', now) == '20261025-090807-20200101-000000-original.txt'
    result = timestamped_name('研究' * 30 + '😀' * 90 + '.docx', now)
    assert result.startswith('20261025-090807-') and result.endswith('.docx')
    assert len(result.encode('utf-16-le')) // 2 <= 192
    assert '\ufffd' not in result
    with pytest.raises(ValueError):
        timestamped_name('report.pdf', datetime(2026, 10, 25))


@pytest.mark.parametrize('name', ['../file.pdf', 'C:file.pdf', 'bad\\file.pdf', 'bad\0.pdf', 'NUL.txt', 'file. ', 'file.' + 'x' * 190])
def test_timestamp_name_never_turns_unsafe_basename_into_an_allowed_upload(name):
    with pytest.raises(ValueError):
        timestamped_name(name, datetime.now(timezone.utc))
