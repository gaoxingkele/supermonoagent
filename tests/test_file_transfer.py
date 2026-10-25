import hashlib
from pathlib import Path

import pytest

from supermonoagent.file_transfer import TransferStore


def digest(data):
    return hashlib.sha256(data).hexdigest()


def start(store, data=b'hello', name='document.pdf', owner='owner'):
    return store.begin(owner, 'session-1', name, len(data), digest(data))


def test_chunked_transfer_only_publishes_verified_complete_file(tmp_path):
    store = TransferStore(tmp_path)
    data = b'a' * 100000 + b'end'
    transfer = start(store, data)
    assert not (tmp_path / 'document.pdf').exists()
    assert store.append('owner', transfer, 0, data[:90000]) == 90000
    with pytest.raises(ValueError, match='incomplete'):
        store.finish('owner', transfer)
    assert store.append('owner', transfer, 90000, data[90000:]) == len(data)
    result = Path(store.finish('owner', transfer))
    assert result == tmp_path / 'document.pdf'
    assert result.read_bytes() == data
    assert not list((tmp_path / '.supermono-partials').iterdir())


def test_empty_file_and_duplicate_names_do_not_overwrite(tmp_path):
    store = TransferStore(tmp_path)
    first = Path(store.finish('owner', start(store, b'')))
    second_id = start(store, b'different')
    store.append('owner', second_id, 0, b'different')
    second = Path(store.finish('owner', second_id))
    assert first.name == 'document.pdf'
    assert second.name == 'document (1).pdf'
    assert first.read_bytes() == b''
    assert second.read_bytes() == b'different'


@pytest.mark.parametrize('racing', [False, True])
def test_strict_name_never_writes_alias_or_removes_existing_target(tmp_path, monkeypatch, racing):
    import os
    target = tmp_path / 'document.pdf'
    store = TransferStore(tmp_path)
    data = b'new staged bytes'
    transfer = start(store, data)
    store.append('owner', transfer, 0, data)
    if racing:
        original = os.link
        def create_competing_file(source, destination):
            Path(destination).write_bytes(b'existing user data')
            return original(source, destination)
        monkeypatch.setattr(os, 'link', create_competing_file)
    else:
        target.write_bytes(b'existing user data')
    with pytest.raises(ValueError, match='already exists'):
        store.finish('owner', transfer, strict_name=True)
    assert target.read_bytes() == b'existing user data'
    assert sorted(path.name for path in tmp_path.iterdir()) == ['.supermono-partials', 'document.pdf']
    assert not list((tmp_path / '.supermono-partials').iterdir())


@pytest.mark.parametrize('name', ['../a', 'a/b', 'a\\b', 'C:a', 'a:stream', 'CON',
    'con.txt', 'COM1.pdf', 'LPT9', 'NUL', 'a.', 'a ', '..', '', 'a\x00b', 'a?b',
    'a*b', 'a<b', 'a>b', 'a|b', 'a"b', 'COM¹.txt', 'CONIN$', 'CONOUT$', 'CLOCK$'])
def test_windows_unsafe_names_rejected(tmp_path, name):
    with pytest.raises(ValueError):
        start(TransferStore(tmp_path), name=name)


def test_offsets_size_chunk_and_metadata_limits(tmp_path):
    store = TransferStore(tmp_path, max_file_size=10, max_active=1)
    with pytest.raises(ValueError):
        start(store, b'x' * 11)
    with pytest.raises(ValueError):
        store.begin('owner', 's', 'a', True, digest(b'x'))
    with pytest.raises(ValueError):
        store.begin('owner', 's', 'a', 1, 'not-a-digest')
    transfer = start(store)
    with pytest.raises(ValueError, match='active'):
        start(store)
    for offset, chunk in [(1, b'x'), (False, b'x'), (0, b'x' * 6), (0, b'x' * (96 * 1024 + 1))]:
        with pytest.raises(ValueError):
            store.append('owner', transfer, offset, chunk)
    assert store.append('owner', transfer, 0, b'hello') == 5
    assert Path(store.finish('owner', transfer)).read_bytes() == b'hello'


def test_owner_isolation_and_cancel_only_removes_owned_partial(tmp_path):
    store = TransferStore(tmp_path)
    transfer = start(store)
    unrelated = tmp_path / '.supermono-partials' / 'unrelated'
    unrelated.write_bytes(b'keep')
    for operation in [lambda: store.append('intruder', transfer, 0, b'hello'),
                      lambda: store.finish('intruder', transfer),
                      lambda: store.cancel('intruder', transfer)]:
        with pytest.raises(ValueError):
            operation()
    store.cancel('owner', transfer)
    assert unrelated.read_bytes() == b'keep'
    assert len(list(unrelated.parent.iterdir())) == 1
    with pytest.raises(ValueError):
        store.append('owner', transfer, 0, b'hello')


def test_hash_mismatch_discards_partial(tmp_path):
    store = TransferStore(tmp_path)
    transfer = start(store)
    store.append('owner', transfer, 0, b'wrong')
    with pytest.raises(ValueError, match='SHA-256'):
        store.finish('owner', transfer)
    assert not (tmp_path / 'document.pdf').exists()
    assert not list((tmp_path / '.supermono-partials').iterdir())


def test_directory_replacement_is_rejected(tmp_path):
    root = tmp_path / 'receive'
    store = TransferStore(root)
    root.rename(tmp_path / 'old')
    root.mkdir()
    with pytest.raises(ValueError, match='changed'):
        start(store)


def test_cancel_owner_preserves_other_owners(tmp_path):
    store = TransferStore(tmp_path)
    first = start(store)
    second = start(store, owner='other')
    assert store.cancel_owner('owner') == 1
    with pytest.raises(ValueError):
        store.finish('owner', first)
    store.append('other', second, 0, b'hello')
    assert Path(store.finish('other', second)).read_bytes() == b'hello'


def test_concurrent_finishes_choose_distinct_names(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store = TransferStore(tmp_path)
    ids = [start(store, b'') for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(lambda transfer: store.finish('owner', transfer), ids))
    assert len(set(paths)) == 4
    assert all(Path(path).read_bytes() == b'' for path in paths)


def test_publication_failure_cleans_partial_and_releases_quota(tmp_path, monkeypatch):
    import os
    store = TransferStore(tmp_path, max_active=1)
    transfer = start(store, b'')
    def fail(*_args):
        raise OSError('Filesystem does not support atomic hard-link publication')
    monkeypatch.setattr(os, 'link', fail)
    with pytest.raises(OSError):
        store.finish('owner', transfer)
    assert not list((tmp_path / '.supermono-partials').iterdir())
    store.cancel('owner', start(store, b''))


def test_symlink_root_rejected(tmp_path):
    target = tmp_path / 'real'
    target.mkdir()
    link = tmp_path / 'link'
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip('Directory symlink privileges unavailable')
    with pytest.raises(ValueError, match='link or reparse'):
        TransferStore(link)


def test_windows_junction_root_rejected(tmp_path):
    import os
    import subprocess
    if os.name != 'nt':
        pytest.skip('Windows junction regression')
    target = tmp_path / 'junction-target'
    target.mkdir()
    link = tmp_path / 'junction'
    environment = {**os.environ, 'SMA_LINK_TEST': str(link), 'SMA_TARGET_TEST': str(target)}
    subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command',
        'New-Item -ItemType Junction -Path $env:SMA_LINK_TEST -Target $env:SMA_TARGET_TEST | Out-Null'],
        env=environment, check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        with pytest.raises(ValueError, match='link or reparse'):
            TransferStore(link)
    finally:
        link.rmdir()  # Non-recursive removal of the test junction, not its target.
    assert target.is_dir()
