import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

import pytest

from supermonoagent.ui_updates import verify_signature

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('windows_installer_package',ROOT/'scripts/package-windows-installer.py')
package=importlib.util.module_from_spec(spec);spec.loader.exec_module(package)
KEY=ROOT/'.omx/update-signing/development-private.pem'


@pytest.fixture
def payload(tmp_path):
    runtime=tmp_path/'runtime';(runtime/'SuperMonoAgent').mkdir(parents=True)
    data=b'installer-test-runtime-fixture'
    (runtime/'SuperMonoAgent/SuperMonoAgent.exe').write_bytes(data)
    (runtime/'version.txt').write_text('0.2.34\n')
    (runtime/'build-manifest.json').write_text(json.dumps({'version':'0.2.34','exe_sha256':hashlib.sha256(data).hexdigest()}))
    templates=tmp_path/'templates';templates.mkdir()
    for name in package.BOOTSTRAP:(templates/name).write_text('bootstrap-test-'+name)
    return runtime,templates


@pytest.mark.skipif(not KEY.exists(),reason='Local release signing key required')
def test_generic_catalog_covers_all_bytes_and_personal_profile_stays_private(tmp_path,payload,monkeypatch):
    runtime,templates=payload
    generic=tmp_path/'generic.zip'
    result=package.build(runtime,generic,templates=templates,private_key=KEY)
    assert not result['personalized']
    with zipfile.ZipFile(generic) as archive:
        assert 'defaults.private.json' not in archive.namelist()
        envelope=json.loads(archive.read('install-manifest.json'))
        raw=base64.b64decode(envelope['payload']);catalog=json.loads(raw)
        trust=json.loads(package.TRUST_PATH.read_text())
        verify_signature(base64.b64decode(trust['keys'][envelope['key_id']]),raw,base64.b64decode(envelope['signature']))
        assert catalog['defaults_private_sha256'] is None
        for section,prefix in [('files','payload/'),('bootstrap_files','')]:
            for name,metadata in catalog[section].items():
                content=archive.read(prefix+name)
                assert metadata=={'size':len(content),'sha256':hashlib.sha256(content).hexdigest()}
    token=tmp_path/'token.txt';token.write_text('private-default-fixture')
    with pytest.raises(ValueError,match='private .omx'):
        package.build(runtime,tmp_path/'public-personal.zip',templates=templates,token_file=token,private_key=KEY)
    monkeypatch.setattr(package,'ROOT',tmp_path)
    private=tmp_path/'.omx/private/personal.zip'
    result=package.build(runtime,private,templates=templates,token_file=token,private_key=KEY)
    assert result['personalized'] and 'private-default-fixture' not in json.dumps(result)
    with zipfile.ZipFile(private) as archive:
        catalog=json.loads(base64.b64decode(json.loads(archive.read('install-manifest.json'))['payload']))
        defaults=archive.read('defaults.private.json')
        assert catalog['defaults_private_sha256']==hashlib.sha256(defaults).hexdigest()
        assert json.loads(defaults)=={'token':'private-default-fixture','relay':'wss://117.50.48.22/ws','room_mode':'computer-name'}


@pytest.mark.parametrize('name',['token.txt','logs/daemon.log','accounts.json'])
def test_runtime_local_state_is_rejected_before_publication(tmp_path,payload,name):
    runtime,templates=payload
    target=runtime/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_text('private-fixture')
    with pytest.raises(ValueError,match='Private configuration'):
        package.build(runtime,tmp_path/'bad.zip',templates=templates)
