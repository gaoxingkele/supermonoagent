"""Private enrollment is installed only in owned temporary application state."""
import base64
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

from test_windows_installer import (KEY, PS, OPENSSL, TOKEN, RELAY, package as signed_package,
                                    install, sign, digest, powershell, ps_literal)
from test_windows_installer_package import payload

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('file_exchange_installer_builder', ROOT/'scripts/package-windows-installer.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
ENROLLMENT = 'owned-file-enrollment-fixture-' + 'a'*40
BROKER_TOKEN = 'owned-file-broker-fixture-token'
PROFILE = {'schema':1, 'broker_url':'wss://example.test/file/ws', 'enrollment_key':ENROLLMENT,
           'broker_token':BROKER_TOKEN}
CATALOG_KEYS = {'schema','format','version','arch','entry','relay','files','bootstrap_files','defaults_private_sha256'}
SIGNING = pytest.mark.skipif(not KEY.is_file() or not OPENSSL, reason='Owned signing fixture required')
WINDOWS = pytest.mark.skipif(sys.platform != 'win32' or not PS or not KEY.is_file() or not OPENSSL,
                            reason='Windows PowerShell and owned signing fixture required')


def add_exchange(folder, catalog, profile=PROFILE, *, raw=None):
    value = {'relay':RELAY, 'token':TOKEN, 'room_mode':'computer-name', 'file_exchange':profile}
    body = raw if raw is not None else json.dumps(value).encode()
    (folder/'defaults.private.json').write_bytes(body)
    catalog['defaults_private_sha256'] = digest(body)
    sign(folder, catalog)


def state_bytes(root):
    return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}


def assert_no_secrets(result):
    assert TOKEN not in result.stdout+result.stderr
    assert ENROLLMENT not in result.stdout+result.stderr
    assert BROKER_TOKEN not in result.stdout+result.stderr


@SIGNING
def test_public_format_stays_exact_and_private_profile_is_signed(tmp_path, payload, monkeypatch):
    runtime, templates = payload
    public = tmp_path/'public.zip'
    builder.build(runtime, public, templates=templates, private_key=KEY)
    with zipfile.ZipFile(public) as archive:
        assert 'defaults.private.json' not in archive.namelist()
        catalog = json.loads(base64.b64decode(json.loads(archive.read('install-manifest.json'))['payload']))
        assert set(catalog) == CATALOG_KEYS
        assert catalog['defaults_private_sha256'] is None
        assert ENROLLMENT.encode() not in b''.join(archive.read(name) for name in archive.namelist())
    token = tmp_path/'token.txt'; token.write_text(TOKEN)
    profile = tmp_path/'exchange.json'; profile.write_text(json.dumps(PROFILE))
    monkeypatch.setattr(builder, 'ROOT', tmp_path)
    private = tmp_path/'.omx/private/owned-setup.zip'
    result = builder.build(runtime, private, templates=templates, token_file=token,
                           file_exchange_profile=profile, private_key=KEY)
    assert ENROLLMENT not in json.dumps(result) and BROKER_TOKEN not in json.dumps(result)
    with zipfile.ZipFile(private) as archive:
        raw = archive.read('defaults.private.json')
        catalog = json.loads(base64.b64decode(json.loads(archive.read('install-manifest.json'))['payload']))
        assert set(catalog) == CATALOG_KEYS
        assert catalog['defaults_private_sha256'] == digest(raw)
        assert json.loads(raw)['file_exchange'] == PROFILE
        assert set(json.loads(raw)) == {'relay','token','room_mode','file_exchange'}


@SIGNING
def test_file_exchange_cannot_be_built_as_public_or_outside_private_root(tmp_path, payload, monkeypatch):
    runtime, templates = payload
    profile = tmp_path/'exchange.json'; profile.write_text(json.dumps(PROFILE))
    token = tmp_path/'token.txt'; token.write_text(TOKEN)
    monkeypatch.setattr(builder, 'ROOT', tmp_path)
    with pytest.raises(ValueError, match='private|Personal'):
        builder.build(runtime, tmp_path/'.omx/public.zip', templates=templates,
                      file_exchange_profile=profile, private_key=KEY)
    with pytest.raises(ValueError, match='private'):
        builder.build(runtime, tmp_path/'outside.zip', templates=templates, token_file=token,
                      file_exchange_profile=profile, private_key=KEY)
    assert not list(tmp_path.glob('*.zip'))


@SIGNING
@pytest.mark.parametrize('change', [
    {'schema':True}, {'schema':2}, {'schema':'1'}, {'unexpected':'not-allowed'},
    {'broker_url':'ws://127.0.0.1/file/ws'}, {'broker_url':'wss://user:password@example.test/file/ws'},
    {'broker_url':'wss://example.test/ws'}, {'broker_url':'wss://example.test/file/ws?token=secret'},
    {'broker_url':'wss://example.test/file/ws#secret'}, {'broker_url':'wss://example.test/file/%77s'},
    {'enrollment_key':'short'}, {'enrollment_key':'a'*31+' '}, {'enrollment_key':'a'*31+'\n'},
    {'enrollment_key':'a'*8193}, {'enrollment_key':'a'*31+'\u4e2d'},
    {'broker_token':None}, {'broker_token':''}, {'broker_token':'a'*4097}, {'broker_token':'a b'},
])
def test_builder_rejects_invalid_private_profile_before_archive_creation(tmp_path, payload, monkeypatch, change):
    runtime, templates = payload
    token = tmp_path/'token.txt'; token.write_text(TOKEN)
    value = {**PROFILE, **change}
    profile = tmp_path/'exchange.json'; profile.write_text(json.dumps(value))
    monkeypatch.setattr(builder, 'ROOT', tmp_path)
    output = tmp_path/'.omx/private/bad.zip'
    with pytest.raises(ValueError):
        builder.build(runtime, output, templates=templates, token_file=token,
                      file_exchange_profile=profile, private_key=KEY)
    assert not output.exists()


@SIGNING
def test_builder_rejects_duplicate_profile_keys_and_runtime_helper_state(tmp_path, payload, monkeypatch):
    runtime, templates = payload
    token = tmp_path/'token.txt'; token.write_text(TOKEN)
    profile = tmp_path/'exchange.json'
    profile.write_text('{"schema":1,"schema":1,"broker_url":"wss://example.test/file/ws","enrollment_key":"'+ENROLLMENT+'"}')
    monkeypatch.setattr(builder, 'ROOT', tmp_path)
    with pytest.raises(ValueError):
        builder.build(runtime, tmp_path/'.omx/private/bad.zip', templates=templates, token_file=token,
                      file_exchange_profile=profile, private_key=KEY)
    helper = runtime/'file-exchange/profile.json'; helper.parent.mkdir(); helper.write_text(json.dumps(PROFILE))
    with pytest.raises(ValueError, match='Private'):
        builder.build(runtime, tmp_path/'generic.zip', templates=templates, private_key=KEY)


@SIGNING
def test_private_profile_source_cannot_also_enter_runtime_payload(tmp_path, payload, monkeypatch):
    runtime, templates = payload
    profile = runtime/'exchange.json'; profile.write_text(json.dumps(PROFILE))
    token = tmp_path/'token.txt'; token.write_text(TOKEN)
    monkeypatch.setattr(builder, 'ROOT', tmp_path)
    with pytest.raises(ValueError, match='Private'):
        builder.build(runtime, tmp_path/'.omx/private/bad.zip', templates=templates, token_file=token,
                      file_exchange_profile=profile, private_key=KEY)


@WINDOWS
@pytest.mark.parametrize('include_broker_token', [False, True])
def test_private_install_creates_atomic_owner_and_system_profile_only(tmp_path, include_broker_token):
    folder, catalog = signed_package(tmp_path)
    profile = dict(PROFILE)
    if not include_broker_token:profile.pop('broker_token')
    add_exchange(folder, catalog, profile)
    root, state = tmp_path/'install', tmp_path/'state'
    result = install(folder, root, state)
    assert_no_secrets(result)
    assert result.returncode == 0, result.stderr
    target = state/'file-exchange/profile.json'
    assert json.loads(target.read_text()) == profile
    assert (state/'token.txt').read_text() == TOKEN
    assert set(json.loads((root/'config.json').read_text())) == {'schema','relay','state_dir','token_file'}
    assert not (root/'defaults.private.json').exists()
    assert list((state/'file-exchange').iterdir()) == [target]
    query = ('@('+ps_literal(target.parent)+','+ps_literal(target)+')|ForEach-Object{'
             '$a=Get-Acl -LiteralPath $_;[pscustomobject]@{protected=$a.AreAccessRulesProtected;'
             'owner=$a.GetOwner([Security.Principal.SecurityIdentifier]).Value;'
             'sids=@($a.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])|ForEach-Object{$_.IdentityReference.Value})}}|ConvertTo-Json -Compress')
    acl = powershell(['-Command', query])
    assert acl.returncode == 0, acl.stderr
    sid = powershell(['-Command','[Security.Principal.WindowsIdentity]::GetCurrent().User.Value']).stdout.strip()
    assert all(row['protected'] and row['owner'] == sid and set(row['sids']) == {sid,'S-1-5-18'}
               for row in json.loads(acl.stdout))


@WINDOWS
def test_maximum_escaped_secrets_and_unicode_owner_fit_private_defaults(tmp_path, payload, monkeypatch):
    runtime, _ = payload
    token_value = '\u4ee4'*4096
    token = tmp_path/'token.txt'; token.write_text(token_value, encoding='utf-8')
    profile = {**PROFILE, 'enrollment_key':'\\"'*4096, 'broker_token':'\\"'*2048}
    monkeypatch.setattr(builder, 'ROOT', tmp_path)
    archive = tmp_path/'.omx/private/large-profile.zip'
    builder.build(runtime, archive, templates=ROOT/'scripts/windows-installer', token_file=token,
                  file_exchange_profile=profile, private_key=KEY)
    folder = tmp_path/'expanded'; folder.mkdir()
    with zipfile.ZipFile(archive) as source:
        assert 32768 < len(source.read('defaults.private.json')) <= 65536
        source.extractall(folder)
    root, state = tmp_path/'install', tmp_path/'state'
    result = install(folder, root, state)
    assert result.returncode == 0, result.stderr
    assert token_value not in result.stdout+result.stderr
    assert profile['enrollment_key'] not in result.stdout+result.stderr
    assert (state/'token.txt').read_text('utf-8') == token_value
    target = state/'file-exchange/profile.json'
    assert target.stat().st_size <= 32768
    assert json.loads(target.read_text('utf-8')) == profile


@WINDOWS
def test_upgrade_keeps_existing_profile_host_secret_token_and_configuration(tmp_path):
    first, first_catalog = signed_package(tmp_path, '0.2.34')
    add_exchange(first, first_catalog)
    root, state = tmp_path/'install', tmp_path/'state'
    assert install(first, root, state).returncode == 0
    helper = state/'file-exchange'
    (helper/'identity.json').write_text(json.dumps({'host_id':'owned-host', 'host_secret':'retained-owned-secret'}))
    (helper/'journal.sqlite').write_bytes(b'owned durable helper state')
    (state/'relay-code').write_text('retained-owned-room')
    before, config = state_bytes(state), (root/'config.json').read_bytes()
    second, second_catalog = signed_package(tmp_path, '0.2.35')
    add_exchange(second, second_catalog, {**PROFILE, 'enrollment_key':'different-key-'+'b'*40})
    result = install(second, root, state)
    assert_no_secrets(result)
    assert result.returncode == 0, result.stderr
    assert state_bytes(state) == before and (root/'config.json').read_bytes() == config
    public, _ = signed_package(tmp_path, '0.2.36', private=False)
    result = install(public, root, state)
    assert result.returncode == 0, result.stderr
    assert state_bytes(state) == before and (root/'config.json').read_bytes() == config
    assert json.loads((root/'current.json').read_text())['version'] == '0.2.36'


@WINDOWS
def test_no_profile_does_not_create_or_touch_file_exchange_state(tmp_path):
    folder, _ = signed_package(tmp_path)
    root, state = tmp_path/'install', tmp_path/'state'
    result = install(folder, root, state)
    assert result.returncode == 0, result.stderr
    assert not (state/'file-exchange').exists()


@WINDOWS
def test_profile_directory_junction_cannot_redirect_private_install(tmp_path):
    folder, catalog = signed_package(tmp_path)
    add_exchange(folder, catalog)
    root, state, outside = tmp_path/'install', tmp_path/'state', tmp_path/'outside'
    state.mkdir(); outside.mkdir()
    retained = outside/'profile.json'; retained.write_text('owned outside data stays unchanged')
    link = state/'file-exchange'
    created = subprocess.run(['cmd.exe','/c','mklink','/J',str(link),str(outside)],
                             capture_output=True, text=True, timeout=10)
    assert created.returncode == 0, created.stderr
    result = install(folder, root, state)
    assert result.returncode != 0
    assert_no_secrets(result)
    assert not root.exists() and not (state/'token.txt').exists()
    assert retained.read_text() == 'owned outside data stays unchanged'


@WINDOWS
@pytest.mark.parametrize('fault', ['malformed','extra-key','insecure-acl'])
def test_existing_profile_is_not_blindly_replaced(tmp_path, fault):
    first, catalog = signed_package(tmp_path, '0.2.34')
    add_exchange(first, catalog)
    root, state = tmp_path/'install', tmp_path/'state'
    assert install(first, root, state).returncode == 0
    path = state/'file-exchange/profile.json'
    if fault == 'malformed':
        path.write_text('{not-json')
    elif fault == 'extra-key':
        path.write_text(json.dumps({**PROFILE, 'unknown':'retain but reject'}))
    else:
        # icacls updates only this owned DACL; Set-Acl can also request SACL
        # privileges unavailable to a normal per-user installer test process.
        changed = subprocess.run([shutil.which('icacls.exe'), str(path), '/grant', '*S-1-1-0:(R)'],
                                 capture_output=True, text=True, timeout=10)
        assert changed.returncode == 0, changed.stderr
    before, config, current = state_bytes(state), (root/'config.json').read_bytes(), (root/'current.json').read_bytes()
    second, catalog = signed_package(tmp_path, '0.2.35'); add_exchange(second, catalog)
    result = install(second, root, state)
    assert result.returncode != 0
    assert_no_secrets(result)
    assert state_bytes(state) == before and (root/'config.json').read_bytes() == config
    assert (root/'current.json').read_bytes() == current
    assert not (root/'versions/0.2.35').exists()


@WINDOWS
@pytest.mark.parametrize('change', [{'schema':True}, {'unexpected':True}, {'enrollment_key':'bad'},
                                  {'broker_url':'ws://example.test/file/ws'}])
def test_signed_bad_nested_profile_is_rejected_before_install(tmp_path, change):
    folder, catalog = signed_package(tmp_path)
    add_exchange(folder, catalog, {**PROFILE, **change})
    root, state = tmp_path/'install', tmp_path/'state'
    result = install(folder, root, state)
    assert result.returncode != 0
    assert_no_secrets(result)
    assert not root.exists() and not state.exists()


@WINDOWS
@pytest.mark.parametrize('duplicate,tamper', [(False,True),(True,False)])
def test_nested_profile_hash_and_duplicate_keys_are_enforced(tmp_path, duplicate, tamper):
    folder, catalog = signed_package(tmp_path)
    if duplicate:
        raw = ('{"relay":"'+RELAY+'","token":"'+TOKEN+'","room_mode":"computer-name",'
               '"file_exchange":{"schema":1,"schema":1,"broker_url":"wss://example.test/file/ws",'
               '"enrollment_key":"'+ENROLLMENT+'"}}').encode()
        add_exchange(folder, catalog, raw=raw)
    else:
        add_exchange(folder, catalog)
    if tamper:
        (folder/'defaults.private.json').write_text(json.dumps({'relay':RELAY,'token':TOKEN,'room_mode':'computer-name',
                                                             'file_exchange':{**PROFILE,'enrollment_key':'changed-'+'z'*40}}))
    root, state = tmp_path/'install', tmp_path/'state'
    result = install(folder, root, state)
    assert result.returncode != 0
    assert_no_secrets(result)
    assert not root.exists() and not state.exists()
