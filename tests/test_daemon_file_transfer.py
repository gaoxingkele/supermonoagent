"""File RPC is isolated from terminal input and validates session ownership."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import pytest

from supermonoagent.protocol import OP_FILE_TRANSFER, pack_json, unpack, unpack_json
from test_client_integration import isolated_host, subscribed, receive_op


def test_owner_file_upload_is_bound_to_target_and_never_enters_terminal():
    asyncio.run(check_upload())


async def check_upload():
    async with isolated_host() as (host, *_):
        win = host.hub.open_window('Receiver fixture', owner=host.token)
        session = host.hub.open_cli(win.id, 'cmd')
        owner = host.hub.attach(asyncio.Queue(), token=host.token)

        async def request(client, body):
            frames, close = await host._op(client, OP_FILE_TRANSFER, json.dumps({'request': 'file-fixture', **body}).encode())
            assert close is None
            op, payload = unpack(frames[0])
            assert op == OP_FILE_TRANSFER
            return unpack_json(payload)

        content = b'fixture PDF bytes\x00\xff'
        begin = await request(owner, {'action': 'begin', 'session': session.id,
            'name': 'sample.pdf', 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
        assert begin['ok']
        upload = {'id': begin['id'], 'session': session.id}
        assert (await request(owner, {**upload, 'action': 'chunk', 'offset': 0,
            'data': base64.b64encode(content).decode()}))['received'] == len(content)
        finish = await request(owner, {**upload, 'action': 'finish'})
        assert finish['ok'] and Path(finish['path']).read_bytes() == content
        assert Path(finish['path']).parent == host.receive_dir
        assert not session.pty.written
        # Different local account cannot create files or reuse an owner's upload.
        token = host.accounts.register('file-viewer@example.test', 'fixture-password')
        outsider = host.hub.attach(asyncio.Queue(), token=token)
        assert (await request(outsider, {**upload, 'action': 'finish'}))['error_code'] == 'owner_required'
        # A live connection cannot finish an upload whose target has closed.
        another = await request(owner, {'action': 'begin', 'session': session.id,
            'name': 'cancel.txt', 'size': 0, 'sha256': hashlib.sha256(b'').hexdigest()})
        host.hub.close_session(session.id)
        reply = await request(owner, {'id': another['id'], 'session': session.id, 'action': 'finish'})
        assert reply['error_code'] == 'session_unavailable'
        assert not (host.receive_dir / 'cancel.txt').exists()
        host.hub.detach(owner.key)
        host.hub.detach(outsider.key)


@pytest.mark.parametrize('relay', [False, True], ids=['direct', 'relay'])
def test_file_frames_travel_real_websockets_and_cancel_on_disconnect(relay):
    asyncio.run(check_wire(relay))


async def check_wire(relay):
    async with isolated_host() as (host, direct_url, relay_url):
        win = host.hub.open_window('File wire fixture', owner=host.token)
        session = host.hub.open_cli(win.id, 'cmd')
        content = bytes(range(256)) * 600
        async with subscribed(relay_url if relay else direct_url, [session.id], relay=relay) as ws:
            async def request(body):
                await ws.send(pack_json(OP_FILE_TRANSFER, {'request': 'wire-upload', 'session': session.id, **body}))
                result = unpack_json(await receive_op(ws, OP_FILE_TRANSFER))
                assert result['ok'], result
                return result
            begin = await request({'action': 'begin', 'name': 'wire.docx', 'size': len(content),
                                   'sha256': hashlib.sha256(content).hexdigest()})
            for offset in range(0, len(content), 96 * 1024):
                await request({'action': 'chunk', 'id': begin['id'], 'offset': offset,
                               'data': base64.b64encode(content[offset:offset + 96 * 1024]).decode()})
            result = await request({'action': 'finish', 'id': begin['id']})
            assert Path(result['path']).read_bytes() == content
            await request({'action': 'begin', 'name': 'incomplete.pdf', 'size': len(content),
                           'sha256': hashlib.sha256(content).hexdigest()})
            assert list((host.receive_dir / '.supermono-partials').glob('*.part'))
        async with asyncio.timeout(3):
            while list((host.receive_dir / '.supermono-partials').glob('*.part')):
                await asyncio.sleep(.01)
        assert not (host.receive_dir / 'incomplete.pdf').exists()
        assert not session.pty.written
