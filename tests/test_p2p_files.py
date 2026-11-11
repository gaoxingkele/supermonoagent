import hashlib
import os
from pathlib import Path

import pytest

from supermonoagent.p2p import files as fs
from supermonoagent.p2p import secure
from supermonoagent.p2p.transfer import IncomingTransfer, decode_ranges, encode_ranges, plan, transfer_id


@pytest.mark.parametrize('rel', ['..', 'a/../b', '/abs', 'C:x', 'a\\b', 'CON', 'con.txt', 'a/', 'dot.', 'sp ',
                                 'a//b', '.sma-p2p/x', 'f.sma-part', 'x\x00y', 'a/./b'])
def test_validate_rel_rejects(rel):
    with pytest.raises(ValueError):
        fs.validate_rel(rel)


@pytest.mark.parametrize('rel', ['a', 'a/b/c.txt', '中文/文件.md', '.gitignore', 'a b/c'])
def test_validate_rel_accepts(rel):
    assert fs.validate_rel(rel) == rel


def test_roots_and_listing(tmp_path):
    root = tmp_path / 'root'
    (root / 'sub').mkdir(parents=True)
    (root / 'f.txt').write_text('x')
    (root / '.sma-p2p').mkdir()
    (root / 'g.bin.sma-part').write_text('x')
    listing = fs.list_dir(str(root), [str(root)])
    assert [e['name'] for e in listing['entries']] == ['sub', 'f.txt']
    assert listing['parent'] == ''
    with pytest.raises(PermissionError):
        fs.list_dir(str(tmp_path), [str(root)])
    with pytest.raises(PermissionError):
        fs.check_allowed(str(root) + '-evil', [str(root)])
    assert fs.list_dir('', [str(root)])['entries'][0]['path'] == str(root)


def test_scan_tree(tmp_path):
    src = tmp_path / 'proj'
    (src / 'a' / 'empty').mkdir(parents=True)
    (src / 'a' / 'x.txt').write_bytes(b'12345')
    (src / 'y.bin').write_bytes(b'\0' * 10)
    manifest = fs.scan(str(src))
    assert manifest['name'] == 'proj' and not manifest['single']
    assert sorted(f[0] for f in manifest['files']) == ['a/x.txt', 'y.bin']
    assert sorted(manifest['dirs']) == ['a', 'a/empty']
    assert manifest['bytes'] == 15
    single = fs.scan(str(src / 'y.bin'))
    assert single['single'] and single['files'][0][0] == 'y.bin'


def test_ranges_roundtrip():
    values = [0, 1, 2, 5, 7, 8]
    assert encode_ranges(values) == [[0, 3], [5, 6], [7, 9]]
    assert decode_ranges(encode_ranges(values)) == values


def test_join_code_and_signature():
    key = secure.new_key()
    assert secure.parse_join_code(secure.join_code(key)) == key
    body = {'a': 1, 'b': 'x'}
    mac = secure.sign(key, body)
    assert secure.verify(key, body, mac)
    assert not secure.verify(key, {**body, 'a': 2}, mac)
    assert not secure.verify(secure.new_key(), body, mac)
    with pytest.raises(ValueError):
        secure.parse_join_code('nope')


def _offer(src: Path, chunk=65536):
    manifest = fs.scan(str(src))
    return manifest, {'name': manifest['name'], 'single': manifest['single'], 'chunk': chunk,
                      'files': manifest['files'], 'dirs': manifest['dirs']}


def _feed(transfer, manifest, need, chunk, limit=None):
    sent = 0
    for index, offset, length in plan(manifest['files'], need, chunk):
        if limit is not None and sent >= limit:
            break
        rel = manifest['files'][index][0]
        data = (Path(manifest['root']) / rel).read_bytes()[offset:offset + length]
        assert transfer.write(index, offset, data, hashlib.sha256(data).digest()) == 0
        sent += 1
    return sent


def test_incoming_resume_and_finish(tmp_path):
    src = tmp_path / 'src'
    (src / 'd').mkdir(parents=True)
    (src / 'd' / 'big.bin').write_bytes(os.urandom(300_000))
    (src / 'small.txt').write_bytes(b'hello')
    (src / 'zero').write_bytes(b'')
    dest = tmp_path / 'dest'
    dest.mkdir()
    manifest, offer = _offer(src)
    tid = transfer_id('a', str(src), 'b', str(dest))
    first = IncomingTransfer(tid, str(dest), offer)
    need = first.prepare()['need']
    assert _feed(first, manifest, need, 65536, limit=2) == 2
    assert first.finish()['complete'] is False
    first.close()

    second = IncomingTransfer(tid, str(dest), offer)
    again = second.prepare()
    remaining = sum(len(decode_ranges(r)) for r in again['need'].values())
    total = sum(len(decode_ranges(r)) for r in need.values())
    assert remaining == total - 2 and again['bytes_done'] > 0
    _feed(second, manifest, again['need'], 65536)
    result = second.finish()
    assert result['complete']
    for rel, size, mtime in manifest['files']:
        out = dest / 'src' / rel
        assert out.read_bytes() == (src / rel).read_bytes()
        assert abs(out.stat().st_mtime_ns - mtime) < 2_000_000_000
    assert not (dest / 'src' / '.sma-p2p').exists()
    assert not list((dest / 'src').rglob('*.sma-part'))

    # Everything already there: a re-send skips every file.
    third = IncomingTransfer(tid, str(dest), offer)
    assert third.prepare()['need'] == {}
    assert third.finish()['complete']


def test_incoming_rejects_bad_chunks_and_paths(tmp_path):
    src = tmp_path / 'src'
    src.mkdir()
    (src / 'f').write_bytes(b'a' * 70000)
    dest = tmp_path / 'dest'
    dest.mkdir()
    manifest, offer = _offer(src)
    transfer = IncomingTransfer('0' * 24, str(dest), offer)
    transfer.prepare()
    data = b'a' * 65536
    assert transfer.write(0, 0, data, b'\0' * 32) == 1          # checksum
    assert transfer.write(0, 100, data, hashlib.sha256(data).digest()) == 2   # misaligned
    assert transfer.write(5, 0, data, hashlib.sha256(data).digest()) == 2     # unknown file
    transfer.close(discard=True)
    with pytest.raises(ValueError):
        IncomingTransfer('0' * 24, str(dest), {**offer, 'files': [['../evil', 1, 0]]})
    with pytest.raises(ValueError):
        IncomingTransfer('0' * 24, str(dest), {**offer, 'name': '..'})
    with pytest.raises(ValueError):
        IncomingTransfer('0' * 24, str(dest), {**offer, 'files': [['a', 1, 0], ['A', 1, 0]]})
