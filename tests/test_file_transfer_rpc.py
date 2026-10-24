import base64
import hashlib
from pathlib import Path

import pytest

from supermonoagent.file_transfer_rpc import FileTransferRPC


def begin(rpc, owner='connection-1', session='session-1', data=b'hello'):
    return rpc.handle(owner, {'request': 'begin-1', 'action': 'begin', 'session': session,
        'name': 'report.pdf', 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})


def request(action, transfer_id, session='session-1', **extra):
    return {'request': action + '-1', 'action': action, 'session': session, 'id': transfer_id, **extra}


def test_complete_two_chunks_and_empty_file(tmp_path):
    calls = []
    rpc = FileTransferRPC(tmp_path, lambda owner, session: not calls.append((owner, session)))
    started = begin(rpc)
    assert started['ok'] and started['received'] == 0
    transfer = started['id']
    for offset, chunk in [(0, b'he'), (2, b'llo')]:
        result = rpc.handle('connection-1', request('chunk', transfer, offset=offset,
            data=base64.b64encode(chunk).decode('ascii')))
        assert result['ok'] and result['received'] == offset + len(chunk)
    result = rpc.handle('connection-1', request('finish', transfer))
    assert result['request'] == 'finish-1' and Path(result['path']).read_bytes() == b'hello'
    assert len(calls) == 5  # Each action, plus permission recheck before atomic publication.
    empty = begin(rpc, data=b'')
    saved = rpc.handle('connection-1', request('finish', empty['id']))
    assert saved['ok'] and Path(saved['path']).read_bytes() == b''


def test_other_owner_cannot_use_or_cancel_transfer(tmp_path):
    rpc = FileTransferRPC(tmp_path, lambda *_: True)
    transfer = begin(rpc)['id']
    wrong = rpc.handle('connection-2', request('cancel', transfer))
    assert wrong['error_code'] == 'unknown_transfer'
    assert rpc.handle('connection-1', request('cancel', transfer))['ok']


def test_exact_session_binding_rejects_and_cancels_owned_transfer(tmp_path):
    rpc = FileTransferRPC(tmp_path, lambda *_: True)
    transfer = begin(rpc)['id']
    wrong = rpc.handle('connection-1', request('finish', transfer, session='session-2'))
    assert wrong['error_code'] == 'unknown_transfer'
    assert not list((tmp_path / '.supermono-partials').iterdir())
    assert rpc.handle('connection-1', request('cancel', transfer))['ok'] is False


def test_revoked_session_cancels_and_errors_without_private_paths(tmp_path):
    allowed = [True]
    rpc = FileTransferRPC(tmp_path, lambda *_: allowed[0])
    transfer = begin(rpc)['id']
    allowed[0] = False
    result = rpc.handle('connection-1', request('finish', transfer))
    assert result['error_code'] == 'session_unavailable'
    assert str(tmp_path) not in str(result)
    assert not list((tmp_path / '.supermono-partials').iterdir())


@pytest.mark.parametrize('data', ['not base64!', 'abc', 'a\nb=', '\u4e2d\u6587', 'x' * 131073],
                         ids=['invalid-alphabet', 'bad-padding', 'newline', 'non-ascii', 'oversize'])
def test_invalid_base64_is_rejected(tmp_path, data):
    rpc = FileTransferRPC(tmp_path, lambda *_: True)
    transfer = begin(rpc)['id']
    result = rpc.handle('connection-1', request('chunk', transfer, offset=0, data=data))
    assert not result['ok']
    assert 'path' not in result
    rpc.cancel_owner('connection-1')


@pytest.mark.parametrize('body', [None, [], {}, {'request': 4}, {'request': 'x' * 65},
    {'request': 'r', 'action': 'execute', 'session': 's'},
    {'request': 'r', 'action': 'begin', 'session': []},
    {'request': 'r', 'action': 'chunk', 'session': 's', 'id': []}],
    ids=['null', 'array', 'empty', 'bad-request', 'long-request', 'action', 'session', 'id'])
def test_invalid_request_shape(tmp_path, body):
    rpc = FileTransferRPC(tmp_path, lambda *_: True)
    result = rpc.handle('connection-1', body)
    assert result['ok'] is False and result['error_code'] == 'invalid_request'


def test_disconnect_releases_only_owned_transfers(tmp_path):
    rpc = FileTransferRPC(tmp_path, lambda *_: True)
    first = begin(rpc)['id']
    second = begin(rpc, owner='connection-2', data=b'')['id']
    assert rpc.cancel_owner('connection-1') == 1
    assert not rpc.handle('connection-1', request('cancel', first))['ok']
    assert rpc.handle('connection-2', request('finish', second))['ok']


def test_changed_directory_disconnect_closes_handles_without_deleting_new_files(tmp_path):
    root = tmp_path / 'receive'
    rpc = FileTransferRPC(root, lambda *_: True)
    transfer = begin(rpc)['id']
    stream = rpc._store._transfers[transfer].stream
    rpc._store._identities[root] = (-1, -1)
    partial = root / '.supermono-partials' / (transfer + '.part')
    assert rpc.cancel_owner('connection-1') == 1
    assert stream.closed
    assert partial.exists()  # Unsafe path cleanup is deliberately skipped.
    assert rpc._store._transfers == {} and rpc._bindings == {}


def test_quota_invalid_size_and_bad_hash_leave_no_registered_transfer(tmp_path):
    rpc = FileTransferRPC(tmp_path, lambda *_: True)
    metadata = {'request': 'r', 'action': 'begin', 'session': 's', 'name': 'file',
                'size': 512 * 1024 * 1024 + 1, 'sha256': hashlib.sha256(b'').hexdigest()}
    assert rpc.handle('connection-1', metadata)['error_code'] == 'invalid_transfer'
    metadata['size'] = 0
    metadata['sha256'] = 'x' * 65
    assert rpc.handle('connection-1', metadata)['error_code'] == 'invalid_request'
    transfers = [begin(rpc, data=b'')['id'] for _ in range(4)]
    assert begin(rpc, data=b'')['error_code'] == 'invalid_transfer'
    for transfer in transfers:
        assert rpc.handle('connection-1', request('cancel', transfer))['ok']
    assert rpc._bindings == {}


def test_io_error_reply_is_generic_and_does_not_parse_untrusted_id(tmp_path, monkeypatch):
    rpc = FileTransferRPC(tmp_path, lambda *_: True)
    def fail(*_args):
        raise OSError('Sensitive local path: ' + str(tmp_path))
    monkeypatch.setattr(rpc._store, 'begin', fail)
    body = {'request': 'r', 'action': 'begin', 'session': 's', 'name': 'file',
            'size': 0, 'sha256': hashlib.sha256(b'').hexdigest(), 'id': []}
    result = rpc.handle('connection-1', body)
    assert result['error_code'] == 'unavailable'
    assert str(tmp_path) not in str(result)
def test_idle_upload_expires_and_releases_partial_and_quota(tmp_path):
    import hashlib
    from supermonoagent.file_transfer_rpc import FileTransferRPC
    clock = [100.0]
    service = FileTransferRPC(tmp_path, lambda *_: True, clock=lambda: clock[0], max_idle=10)
    start = service.handle('owner', {'request': 'lost-ack', 'action': 'begin', 'session': 's',
        'name': 'abandoned.pdf', 'size': 1, 'sha256': hashlib.sha256(b'x').hexdigest()})
    assert start['ok']
    clock[0] += 11
    assert service.expire() == 1
    assert not list((tmp_path / '.supermono-partials').glob('*.part'))
    assert service.handle('owner', {'request': 'late', 'action': 'finish', 'session': 's', 'id': start['id']})['error_code'] == 'unknown_transfer'
