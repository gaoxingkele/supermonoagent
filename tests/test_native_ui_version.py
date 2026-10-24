import asyncio
import base64
import hashlib
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from supermonoagent.frontend import Frontend
from supermonoagent import frontend as frontend_module
from test_ui_updates import OPENSSL, release


@pytest.fixture(scope='module')
def signer(tmp_path_factory):
    if not OPENSSL:
        pytest.skip('OpenSSL signing fixture required')
    folder = tmp_path_factory.mktemp('native-ui-version')
    private = folder / 'key.pem'
    subprocess.run([OPENSSL, 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072',
        '-out', str(private)], capture_output=True, check=True)
    public = subprocess.run([OPENSSL, 'pkey', '-in', str(private), '-pubout', '-outform', 'DER'],
        capture_output=True, check=True).stdout
    return private, {'schema': 1, 'keys': {hashlib.sha256(public).hexdigest()[:16]: base64.b64encode(public).decode()}}


@pytest.mark.parametrize('candidate,expected', [('0.2.34', 'current'), ('0.2.37', 'current'), ('0.2.38', 'available')])
def test_fresh_native_install_does_not_offer_older_ui(tmp_path, signer, candidate, expected):
    private, trust = signer
    raw, _, _, _ = release(private, trust, revision=candidate)
    frontend = Frontend('http://127.0.0.1:6769', tmp_path / 'themes.json',
        update_root=tmp_path / 'ui', update_trust=trust)
    frontend.bundled_version = '0.2.37'
    with patch('supermonoagent.ui_updates.fetch_manifest', return_value=raw):
        result = asyncio.run(frontend._check_update())
    assert result['phase'] == expected
    assert bool(frontend.update_candidate) == (expected == 'available')


def test_packaged_version_reader_uses_bounded_adjacent_version_file(tmp_path):
    folder = tmp_path / 'SuperMonoAgent'
    folder.mkdir()
    version = tmp_path / 'version.txt'
    with patch.object(frontend_module.sys, 'frozen', True, create=True), patch.object(frontend_module.sys, 'executable', str(folder / 'SuperMonoAgent.exe')):
        version.write_text('0.2.37\n', encoding='utf-8')
        assert frontend_module.native_bundle_version() == '0.2.37'
        version.write_text('9' * 80 + '.1.2', encoding='utf-8')
        assert frontend_module.native_bundle_version() is None
        version.write_text('not-a-version', encoding='utf-8')
        assert frontend_module.native_bundle_version() is None


def test_newer_ui_is_still_available_at_an_inherited_sequence_floor(tmp_path, signer):
    private, trust = signer
    raw, _, _, _ = release(private, trust, revision='0.2.38')
    frontend = Frontend('http://127.0.0.1:6769', tmp_path / 'themes.json',
        update_root=tmp_path / 'ui', update_trust=trust)
    frontend.bundled_version = '0.2.37'
    frontend.update_store.observe(raw)
    frontend.update_store.inherit_floor([], bundle_fingerprint=frontend.bundle_fingerprint)
    assert frontend.update_store.status()['native_bundle_floor'] == 1
    with patch('supermonoagent.ui_updates.fetch_manifest', return_value=raw):
        result = asyncio.run(frontend._check_update())
    assert result['phase'] == 'available'
