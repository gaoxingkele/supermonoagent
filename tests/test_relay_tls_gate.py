"""The deployment gate requires trusted TLS and rejects legacy public directory leaks."""
import asyncio
import importlib.util
from pathlib import Path
import ssl

import pytest
from websockets.asyncio.server import serve

from supermonoagent.relay import Relay


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('relay_tls_gate', ROOT / 'scripts/check-relay-tls.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def test_deployment_gate_trusted_tls_origin_and_auth(tmp_path):
    asyncio.run(_check_gate(tmp_path))


async def _check_gate(tmp_path):
    certificate = ROOT / 'tests/fixtures/tls-loopback-cert.pem'
    key = ROOT / 'tests/fixtures/tls-loopback-key.pem'
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    relay = Relay(identity_file=tmp_path / 'accounts.sqlite3', public_url='wss://127.0.0.1/ws')
    async with serve(relay.handler, '127.0.0.1', 0, process_request=relay.serve_page, ssl=context) as server:
        endpoint = f'https://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        with pytest.raises(Exception, match='CERTIFICATE_VERIFY_FAILED|certificate verify failed'):
            await gate.check(endpoint)
        result = await gate.check(endpoint, str(certificate))
        assert result['trusted_tls'] is True
        assert result['account_session_required'] is True
        assert result['foreign_origin_rejected'] is True
        assert result['health_no_roster_or_credentials'] is True


@pytest.mark.parametrize('url', ['http://127.0.0.1', 'https://user:secret@example.com', 'https://example.com/path', 'https://example.com?token=secret'])
def test_deployment_gate_rejects_unsafe_target(url):
    with pytest.raises(ValueError):
        asyncio.run(gate.check(url))
