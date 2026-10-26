import copy
import hashlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

from supermonoagent.file_folder_store import FolderReceiveStore


def manifest():
    return {'version': 1, 'root': 'source',
            'directories': [['a'], ['b'], ['empty'], ['a', '\u8d44\u6599\U0001f600']],
            'files': [
                {'path': ['a', '\u8d44\u6599\U0001f600', 'same.txt'], 'size': 3, 'sha256': hashlib.sha256(b'one').hexdigest()},
                {'path': ['b', 'same.txt'], 'size': 3, 'sha256': hashlib.sha256(b'two').hexdigest()},
                {'path': ['zero.bin'], 'size': 0, 'sha256': hashlib.sha256(b'').hexdigest()}]}


def allocate(tmp_path, value=None):
    exchange = tmp_path / 'project' / 'tmp'
    exchange.mkdir(parents=True)
    return FolderReceiveStore.create(exchange, '2026-10-27-123456-source', value or manifest())


def put(store, index, data, owner='owner'):
    transfer = store.begin(owner, 'session', index)
    if data:
        assert store.append(owner, transfer, 0, data) == len(data)
    return Path(store.finish(owner, transfer))


def test_create_preserves_nested_names_empty_directories_and_file_bytes(tmp_path):
    store = allocate(tmp_path)
    assert (store.root / 'empty').is_dir()
    assert put(store, 0, b'one') == store.root / 'a' / '\u8d44\u6599\U0001f600' / 'same.txt'
    assert put(store, 1, b'two').read_bytes() == b'two'
    assert put(store, 2, b'').read_bytes() == b''
    assert (store.root / 'a' / '\u8d44\u6599\U0001f600' / 'same.txt').read_bytes() == b'one'
    store.close()


def test_create_is_exclusive_and_never_renames_or_replaces_root(tmp_path):
    store = allocate(tmp_path)
    (store.root / 'existing.txt').write_text('keep')
    with pytest.raises(FileExistsError):
        FolderReceiveStore.create(store.root.parent, store.root.name, manifest())
    assert (store.root / 'existing.txt').read_text() == 'keep'
    assert len(list(store.root.parent.iterdir())) == 1


@pytest.mark.parametrize('name', ['..', '../outside', 'C:root', '\\server', '.supermono-partials', 'CON'])
def test_create_rejects_unsafe_root_name_before_writing(tmp_path, name):
    exchange = tmp_path / 'exchange'
    exchange.mkdir()
    with pytest.raises(ValueError):
        FolderReceiveStore.create(exchange, name, manifest())
    assert list(exchange.iterdir()) == []


def test_exchange_must_already_exist_and_stay_in_caller_location(tmp_path):
    with pytest.raises(ValueError):
        FolderReceiveStore.create(tmp_path / 'absent', 'root', manifest())
    assert list(tmp_path.iterdir()) == []


def test_exact_original_file_name_is_required_and_existing_file_is_preserved(tmp_path):
    store = allocate(tmp_path)
    original = store.entry_path(1)
    original.write_bytes(b'keep')
    transfer = store.begin('owner', 'session', 1)
    store.append('owner', transfer, 0, b'two')
    with pytest.raises(ValueError, match='already exists'):
        store.finish('owner', transfer)
    assert original.read_bytes() == b'keep'
    assert not (original.parent / 'same (1).txt').exists()


def test_snapshot_is_detached_and_reopen_checks_manifest_and_current_identities(tmp_path):
    store = allocate(tmp_path)
    saved = store.snapshot()
    reopened = FolderReceiveStore.reopen(store.root.parent, saved, manifest())
    assert reopened.root == store.root
    assert put(reopened, 1, b'two').read_bytes() == b'two'
    saved['name'] = '../outside'
    assert store.snapshot()['name'] == store.root.name
    with pytest.raises(ValueError):
        FolderReceiveStore.reopen(store.root.parent, saved, manifest())


def test_reopen_does_not_create_missing_leaf_directory(tmp_path):
    store = allocate(tmp_path)
    saved = store.snapshot()
    (store.root / 'empty').rmdir()
    with pytest.raises(ValueError):
        FolderReceiveStore.reopen(store.root.parent, saved, manifest())
    assert not (store.root / 'empty').exists()


def test_reopen_preserves_owned_partial_directory_identity(tmp_path):
    store = allocate(tmp_path)
    transfer = store.begin('owner', 'session', 1)
    store.cancel('owner', transfer)
    saved = store.snapshot()
    restored = FolderReceiveStore.reopen(store.root.parent, saved, manifest())
    assert put(restored, 1, b'two').read_bytes() == b'two'
    partial = store.root / 'b' / '.supermono-partials'
    partial.rename(store.root / 'b' / 'partials-original')
    partial.mkdir()
    with pytest.raises(ValueError):
        FolderReceiveStore.reopen(store.root.parent, saved, manifest())


def test_first_leaf_use_refuses_unrecorded_existing_partial_directory(tmp_path):
    store = allocate(tmp_path)
    partial = store.root / 'b' / '.supermono-partials'
    partial.mkdir()
    sentinel = partial / 'foreign.part'
    sentinel.write_bytes(b'keep')
    with pytest.raises(FileExistsError):
        store.begin('owner', 'session', 1)
    assert sentinel.read_bytes() == b'keep'


@pytest.mark.parametrize('change', ['duplicate', 'nonparent', 'absolute', 'identity', 'numeric'])
def test_partial_snapshot_extensions_are_strict(tmp_path, change):
    store = allocate(tmp_path)
    transfer = store.begin('owner', 'session', 1)
    store.cancel('owner', transfer)
    value = store.snapshot()
    if change == 'duplicate': value['partials'].append(copy.deepcopy(value['partials'][0]))
    elif change == 'nonparent': value['partials'][0]['path'] = ['empty']
    elif change == 'absolute': value['partials'][0]['path'] = [str(tmp_path)]
    elif change == 'identity': value['partials'][0]['ino'] = '999999999'
    elif change == 'numeric': value['partials'][0]['dev'] = 1
    with pytest.raises(ValueError):
        FolderReceiveStore.reopen(store.root.parent, value, manifest())


@pytest.mark.parametrize('change', ['extra', 'digest', 'root', 'identity', 'absolute', 'duplicate', 'missing', 'bool', 'leading_zero'])
def test_forged_snapshot_fails_closed_without_creating_paths(tmp_path, change):
    store = allocate(tmp_path)
    value = store.snapshot()
    if change == 'extra': value['absolute_root'] = str(tmp_path / 'outside')
    elif change == 'digest': value['manifest_digest'] = '0' * 64
    elif change == 'root': value['name'] = '../outside'
    elif change == 'identity': value['directories'][0]['ino'] = '999999999'
    elif change == 'absolute': value['directories'][0]['path'] = [str(tmp_path)]
    elif change == 'duplicate': value['directories'].append(copy.deepcopy(value['directories'][0]))
    elif change == 'missing': value['directories'].pop()
    elif change == 'bool': value['ancestors'][0]['dev'] = True
    elif change == 'leading_zero': value['ancestors'][0]['dev'] = '00'
    with pytest.raises(ValueError):
        FolderReceiveStore.reopen(store.root.parent, value, manifest())
    assert not (tmp_path / 'outside').exists()


@pytest.mark.parametrize('target', ['project', 'exchange', 'root', 'leaf', 'empty'])
def test_any_directory_replacement_rejects_new_writes(tmp_path, target):
    store = allocate(tmp_path)
    path = {'project': store.root.parent.parent, 'exchange': store.root.parent, 'root': store.root,
            'leaf': store.root / 'b', 'empty': store.root / 'empty'}[target]
    moved = path.with_name(path.name + '-original')
    path.rename(moved)
    path.mkdir()
    with pytest.raises(ValueError, match='changed'):
        store.begin('owner', 'session', 1)
    assert list(path.iterdir()) == []


def test_reopened_store_keeps_no_alias_to_mutable_snapshot(tmp_path):
    store = allocate(tmp_path)
    snapshot = store.snapshot()
    restored = FolderReceiveStore.reopen(store.root.parent, snapshot, manifest())
    snapshot['directories'][0]['ino'] = '999999999'
    snapshot['ancestors'].clear()
    snapshot['partials'].append({'path': ['empty'], 'dev': '0', 'ino': '1'})
    assert restored.check()
    assert put(restored, 1, b'two').read_bytes() == b'two'


def test_sibling_empty_directory_change_blocks_append_and_publication(tmp_path):
    store = allocate(tmp_path)
    transfer = store.begin('owner', 'session', 1)
    empty = store.root / 'empty'
    empty.rename(store.root / 'empty-original')
    empty.mkdir()
    with pytest.raises(ValueError, match='changed'):
        store.append('owner', transfer, 0, b'two')
    assert not (store.root / 'b' / 'same.txt').exists()


def test_authority_callback_is_rechecked_after_fsync_and_cannot_publish_on_change(tmp_path):
    store = allocate(tmp_path)
    transfer = store.begin('owner', 'session', 1)
    store.append('owner', transfer, 0, b'two')
    calls = []
    def revoked():
        calls.append(True)
        return False
    with pytest.raises(ValueError, match='authorized'):
        store.finish('owner', transfer, can_publish=revoked)
    assert calls == [True]
    assert not store.entry_path(1).exists()


def test_callback_directory_replacement_is_rechecked_before_publication(tmp_path):
    store = allocate(tmp_path)
    transfer = store.begin('owner', 'session', 1)
    store.append('owner', transfer, 0, b'two')
    def changed():
        empty = store.root / 'empty'
        empty.rename(store.root / 'empty-original')
        empty.mkdir()
        return True
    with pytest.raises(ValueError, match='changed'):
        store.finish('owner', transfer, can_publish=changed)
    assert not (store.root / 'b' / 'same.txt').exists()


def test_global_active_limit_is_shared_between_leaves(tmp_path):
    value = {'version': 1, 'root': 'source', 'directories': [[str(i)] for i in range(5)],
             'files': [{'path': [str(i), 'f'], 'size': 0, 'sha256': hashlib.sha256(b'').hexdigest()} for i in range(5)]}
    store = allocate(tmp_path, value)
    transfers = [store.begin('owner', 'session', i) for i in range(4)]
    with pytest.raises(ValueError, match='active'):
        store.begin('owner', 'session', 4)
    store.cancel('owner', transfers.pop())
    transfers.append(store.begin('owner', 'session', 4))
    store.close()
    assert not any(p.name.endswith('.part') for p in store.root.rglob('*'))


def test_owner_wrong_index_and_cancel_preserve_saved_files(tmp_path):
    store = allocate(tmp_path)
    saved = put(store, 0, b'one')
    transfer = store.begin('owner', 'session', 1)
    with pytest.raises(ValueError): store.append('other', transfer, 0, b'two')
    with pytest.raises(ValueError): store.finish('other', transfer)
    with pytest.raises(ValueError): store.begin('owner', 'session', True)
    with pytest.raises(ValueError): store.begin('owner', 'session', 3)
    with pytest.raises(ValueError): store.begin('owner', 'session', 1)
    store.cancel('owner', transfer)
    store.close()
    assert saved.read_bytes() == b'one'


def test_link_exchange_rejected_without_writing_target(tmp_path):
    target = tmp_path / 'target'
    target.mkdir()
    link = tmp_path / 'link'
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip('Directory symlink privileges unavailable')
    with pytest.raises(ValueError):
        FolderReceiveStore.create(link, 'root', manifest())
    assert list(target.iterdir()) == []


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows junction proof')
def test_junction_exchange_rejected_without_writing_target(tmp_path):
    target = tmp_path / 'target'
    target.mkdir()
    link = tmp_path / 'junction'
    result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], capture_output=True)
    assert result.returncode == 0
    try:
        with pytest.raises(ValueError):
            FolderReceiveStore.create(link, 'root', manifest())
        assert list(target.iterdir()) == []
    finally:
        link.rmdir()
