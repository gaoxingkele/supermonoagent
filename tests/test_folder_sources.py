import base64
import hashlib
import os
from pathlib import Path
import threading

import pytest

from supermonoagent import windows_clipboard as C


def tree(tmp_path, name='source'):
    root = tmp_path / name
    (root / 'nested' / 'empty').mkdir(parents=True)
    (root / 'nested' / 'same.txt').write_bytes(b'nested')
    (root / 'same.txt').write_bytes(b'root')
    return root


def test_folder_inventory_preserves_hierarchy_and_empty_directory(tmp_path):
    root = tree(tmp_path)
    files = C.ClipboardFiles()
    try:
        result = files.snapshot_folder(root)
        assert result['kind'] == 'folder'
        assert result['manifest']['directories'] == [['nested'], ['nested', 'empty']]
        assert [x['path'] for x in result['manifest']['files']] == [['nested', 'same.txt'], ['same.txt']]
        assert [x['index'] for x in result['items']] == [0, 1]
        for index, item in enumerate(result['items']):
            entry = result['manifest']['files'][index]
            data = root.joinpath(*entry['path']).read_bytes()
            assert base64.b64decode(files.read(item['id'], 0)['data']) == data
            assert entry['sha256'] == item['sha256'] == hashlib.sha256(data).hexdigest()
        assert not files._reservations
    finally:
        files.close()


def test_empty_folder_returns_manifest_without_retained_files(tmp_path):
    root = tmp_path / 'empty'
    root.mkdir()
    files = C.ClipboardFiles()
    result = files.snapshot_folder(root)
    assert result['manifest'] == {'version': 1, 'root': 'empty', 'directories': [], 'files': []}
    assert result['items'] == [] and not files._items and not files._reservations


def test_folder_sources_share_existing_file_budget(tmp_path):
    root = tree(tmp_path)
    regular = tmp_path / 'regular.txt'
    regular.write_bytes(b'old')
    files = C.ClipboardFiles(lambda: [regular], max_file_size=16, max_sources=2, max_total_size=32)
    old = files.snapshot()['items'][0]
    with pytest.raises(ValueError):
        files.snapshot_folder(root)
    assert list(files._items) == [old['id']] and not files._reservations
    assert base64.b64decode(files.read(old['id'], 0)['data']) == b'old'
    files.close()


def test_source_change_after_snapshot_rejects_read_and_releases_only_itself(tmp_path):
    root = tree(tmp_path)
    files = C.ClipboardFiles()
    result = files.snapshot_folder(root)
    first = result['items'][0]
    root.joinpath(*result['manifest']['files'][0]['path']).write_bytes(b'changed')
    with pytest.raises(ValueError):
        files.read(first['id'], 0)
    assert first['id'] not in files._items
    assert result['items'][1]['id'] in files._items
    files.close()


def test_folder_clipboard_selection_rejects_mixed_files_without_leaks(tmp_path):
    root = tree(tmp_path)
    regular = tmp_path / 'regular'
    regular.write_bytes(b'file')
    files = C.ClipboardFiles(lambda: [root, regular])
    with pytest.raises(ValueError):
        files.snapshot_clipboard_folders()
    assert not files._items and not files._reservations


def test_sequential_folder_copies_keep_earlier_sources(tmp_path):
    roots = [tree(tmp_path, 'first'), tree(tmp_path, 'second')]
    selected = [roots[0]]
    files = C.ClipboardFiles(lambda: list(selected))
    first = files.snapshot_clipboard_folders()['folders'][0]
    selected[:] = [roots[1]]
    second = files.snapshot_clipboard_folders()['folders'][0]
    assert first['manifest']['root'] == 'first' and second['manifest']['root'] == 'second'
    assert len(files._items) == 4
    files.close()


def test_cancelled_folder_capture_preserves_previous_batch(tmp_path):
    root = tree(tmp_path)
    files = C.ClipboardFiles()
    previous = files.snapshot_folder(root)
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls > 7
    with pytest.raises(ValueError):
        files.snapshot_folder(root, cancelled=cancelled)
    assert set(files._items) == {x['id'] for x in previous['items']}
    assert not files._reservations
    files.close()


def test_aggregate_folder_file_limit_applies_to_selection(tmp_path):
    roots = []
    for name in ['first', 'second']:
        root = tmp_path / name
        root.mkdir()
        for number in range(17):
            (root / str(number)).write_bytes(b'x')
        roots.append(root)
    files = C.ClipboardFiles(lambda: roots)
    with pytest.raises(ValueError):
        files.snapshot_clipboard_folders()
    assert not files._items and not files._reservations


def test_folder_symlink_rejected(tmp_path):
    target = tmp_path / 'target'
    target.mkdir()
    root = tmp_path / 'root'
    root.mkdir()
    try:
        (root / 'link').symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip('This account cannot create symlinks')
    files = C.ClipboardFiles()
    with pytest.raises(ValueError):
        files.snapshot_folder(root)
    assert not files._items and not files._reservations


@pytest.mark.parametrize('bad', [None, 'relative', Path('relative')])
def test_folder_root_must_be_explicit_native_absolute_path(bad):
    with pytest.raises(ValueError):
        C.ClipboardFiles().snapshot_folder(bad)


def test_unicode_zero_byte_folder_entries_and_detached_manifest(tmp_path):
    root = tmp_path / '\u6587\u4ef6\u5939'
    (root / '\U0001f4c1').mkdir(parents=True)
    (root / '\U0001f4c1' / '\u6587\u672c.txt').write_bytes(b'')
    files = C.ClipboardFiles()
    result = files.snapshot_folder(root)
    item = result['items'][0]
    result['manifest']['files'][0]['path'][0] = 'untrusted-new-value'
    assert files.read(item['id'], 0) == {'data': '', 'offset': 0, 'next': 0, 'eof': True}
    files.close()


def test_lstat_and_fstat_ctime_can_differ_without_weakening_same_api_guards(tmp_path, monkeypatch):
    root = tree(tmp_path)
    original = C._identity
    offset = [12345]
    def different_ctime(stream):
        identity = original(stream)
        return (*identity[:4], identity[4] + offset[0])
    monkeypatch.setattr(C, '_identity', different_ctime)
    files = C.ClipboardFiles()
    result = files.snapshot_folder(root)
    item = result['items'][0]
    assert files.read(item['id'], 0)['eof']
    offset[0] += 1
    with pytest.raises(ValueError):
        files.read(item['id'], 0)
    assert item['id'] not in files._items
    files.close()


def test_limits_reject_before_opening_oversized_contents(tmp_path, monkeypatch):
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'large').write_bytes(b'oversized')
    original = Path.open
    opened = []
    def record(path, *args, **kwargs):
        opened.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', record)
    files = C.ClipboardFiles(max_file_size=3)
    with pytest.raises(ValueError):
        files.snapshot_folder(root)
    assert not opened and not files._items and not files._reservations


def test_depth_limit_rejects_before_opening_file(tmp_path):
    root = tmp_path / 'source'
    child = root
    for _ in range(17):
        child = child / 'a'
    child.mkdir(parents=True)
    files = C.ClipboardFiles()
    with pytest.raises(ValueError):
        files.snapshot_folder(root)
    assert not files._items and not files._reservations


def test_source_changed_between_inventory_and_open_is_rejected(tmp_path, monkeypatch):
    root = tree(tmp_path)
    original = Path.open
    changed = []
    def change(path, *args, **kwargs):
        if path.is_relative_to(root) and not changed:
            changed.append(path)
            info = path.stat()
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', change)
    files = C.ClipboardFiles()
    with pytest.raises(ValueError):
        files.snapshot_folder(root)
    assert changed and not files._items and not files._reservations


def test_cancel_after_register_releases_only_new_sources(tmp_path, monkeypatch):
    root = tree(tmp_path)
    files = C.ClipboardFiles()
    previous = files.snapshot_folder(root)
    original = files._register_source
    registered = []
    def register(reservation, source_id, source):
        original(reservation, source_id, source)
        registered.append(source)
    monkeypatch.setattr(files, '_register_source', register)
    with pytest.raises(ValueError):
        files.snapshot_folder(root, cancelled=lambda: bool(registered))
    assert registered and all(source.stream.closed for source in registered)
    assert set(files._items) == {item['id'] for item in previous['items']}
    assert not files._reservations
    files.close()


def test_close_during_enumeration_cancels_without_leaking_reservation(tmp_path, monkeypatch):
    root = tree(tmp_path)
    files = C.ClipboardFiles()
    entered, proceed = threading.Event(), threading.Event()
    original = C.os.scandir
    def paused(path):
        entered.set()
        assert proceed.wait(5)
        return original(path)
    monkeypatch.setattr(C.os, 'scandir', paused)
    errors = []
    def run():
        try:
            files.snapshot_folder(root)
        except ValueError as error:
            errors.append(str(error))
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(5)
        files.close()
    finally:
        proceed.set()
        worker.join(5)
    assert not worker.is_alive() and errors
    assert not files._items and not files._reservations


def test_queued_folder_capture_respects_live_shared_reservation(tmp_path, monkeypatch):
    root = tree(tmp_path)
    files = C.ClipboardFiles(max_sources=2)
    entered, proceed = threading.Event(), threading.Event()
    original = C.hashlib.sha256
    first = [True]
    def paused(*args, **kwargs):
        if first[0]:
            first[0] = False
            entered.set()
            assert proceed.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(C.hashlib, 'sha256', paused)
    errors, results = [], []
    def run():
        try:
            results.append(files.snapshot_folder(root))
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(ValueError, match='budget'):
            files.snapshot_folder(root)
    finally:
        proceed.set()
        worker.join(5)
    assert not worker.is_alive() and not errors and len(results) == 1
    assert len(files._items) == 2 and not files._reservations
    files.close()


def test_directory_identity_change_after_snapshot_stops_reads(tmp_path, monkeypatch):
    root = tree(tmp_path)
    files = C.ClipboardFiles()
    result = files.snapshot_folder(root)
    original = C._folder_info
    def replaced(path, directory=False):
        identity = original(path, directory)
        if directory and path == root.parent:
            return identity[0], identity[1] + 1
        return identity
    monkeypatch.setattr(C, '_folder_info', replaced)
    with pytest.raises(ValueError):
        files.read(result['items'][0]['id'], 0)
    files.close()


def test_explicit_folder_probe_reads_metadata_only(tmp_path, monkeypatch):
    root = tree(tmp_path)
    called = []
    monkeypatch.setattr(C, 'clipboard_sources', lambda **kwargs: called.append(kwargs) or [root])
    monkeypatch.setattr(Path, 'open', lambda *args, **kwargs: pytest.fail('Probe read file content'))
    assert C.has_clipboard_folders() is True
    assert called == [{'files_only': True}]


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction test')
def test_source_junction_is_rejected(tmp_path):
    import subprocess
    root = tmp_path / 'root'
    target = tmp_path / 'target'
    root.mkdir()
    target.mkdir()
    result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(root / 'junction'), str(target)],
                            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        pytest.skip('This account cannot create junctions')
    files = C.ClipboardFiles()
    with pytest.raises(ValueError):
        files.snapshot_folder(root)
    assert not files._items and not files._reservations
