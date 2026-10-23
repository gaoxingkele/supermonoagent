"""Explicit host credentials remain consistent across the CLI and saved clients."""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest
from websockets.asyncio.client import connect

from supermonoagent import daemon
from supermonoagent.protocol import pack_json, unpack


def test_explicit_token_replaces_saved_value_and_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, 'STATE', tmp_path)
    (tmp_path / 'token').write_text('previous-fixture-token')
    assert daemon.load_token('shared-fixture-token') == 'shared-fixture-token'
    assert daemon.load_token() == 'shared-fixture-token'
    assert list(tmp_path.iterdir()) == [tmp_path / 'token']


@pytest.mark.parametrize('token', ['', 'two words', 'line\nbreak', 'tab\tvalue', 'control\x1bvalue', 'delete\x7fvalue', 'x' * 4097])
def test_invalid_explicit_token_does_not_change_saved_credentials(token, tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, 'STATE', tmp_path)
    (tmp_path / 'token').write_text('existing-fixture-token')
    with pytest.raises(ValueError, match='without whitespace or control'):
        daemon.load_token(token)
    assert (tmp_path / 'token').read_text() == 'existing-fixture-token'


def test_cli_token_authenticates_the_actual_host(tmp_path):
    asyncio.run(check_cli_token(tmp_path))


async def check_cli_token(tmp_path, launcher=None):
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    root = Path(__file__).resolve().parents[1]
    environment = {**os.environ, 'PYTHONPATH': str(root / 'src'), 'SUPERMONO_STATE_DIR': str(tmp_path)}
    token = 'shared-cli-fixture-token'
    launcher = launcher or [sys.executable, '-m', 'supermonoagent.launcher']
    process = subprocess.Popen(
        [*launcher, 'host', '--host', '127.0.0.1',
         '--port', str(port), '--state-dir', str(tmp_path), '--no-relay', '--token', token],
        cwd=root, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
    )
    try:
        async with asyncio.timeout(10):
            while True:
                if process.poll() is not None:
                    raise AssertionError('Test host failed to start')
                try:
                    reader, writer = await asyncio.open_connection('127.0.0.1', port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(.05)
        async with connect(f'ws://127.0.0.1:{port}/ws', additional_headers={'Authorization': 'Bearer ' + token}) as ws:
            await ws.send(pack_json(1, {'token': token, 'multiplex': True}))
            frame = await asyncio.wait_for(ws.recv(), 3)
            opcode, payload = unpack(frame)
            assert opcode == 2
            assert json.loads(payload)['capabilities']['multiplex']
        assert (tmp_path / 'token').read_text() == token
        duplicate = subprocess.run(
            [*launcher, 'host', '--host', '127.0.0.1',
             '--port', str(port), '--state-dir', str(tmp_path), '--no-relay', '--token', 'replacement-fixture-token'],
            cwd=root, env=environment, capture_output=True, timeout=5,
        )
        assert duplicate.returncode == 2
        assert b'saved token was not changed' in duplicate.stderr
        assert (tmp_path / 'token').read_text() == token
    finally:
        process.terminate()
        process.communicate(timeout=5)
