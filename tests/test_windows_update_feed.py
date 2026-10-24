import base64
import importlib.util
import json
from pathlib import Path

import pytest

from supermonoagent.ui_updates import verify_signature
from test_windows_installer_package import package as installers,KEY,payload

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('windows_update_feed',ROOT/'scripts/package-windows-update-feed.py')
updates=importlib.util.module_from_spec(spec);spec.loader.exec_module(updates)
pytestmark=pytest.mark.skipif(not KEY.exists(),reason='Local release signing key required')


def test_full_update_feed_is_signed_and_strictly_increases_sequence(tmp_path,payload):
    runtime,templates=payload;archive=tmp_path/'setup.zip'
    installers.build(runtime,archive,templates=templates,private_key=KEY)
    target=tmp_path/'manifest.json';first=updates.package(archive,target,private_key=KEY)
    second=updates.package(archive,target,private_key=KEY)
    assert second['sequence']>first['sequence'] and second['version']=='0.2.34'
    assert second['url']=='/updates/installers/0.2.34/SuperMonoAgent-0.2.34-Setup.zip'
    envelope=json.loads(target.read_bytes());raw=base64.b64decode(envelope['payload']);trust=json.loads(updates.TRUST.read_text())
    verify_signature(base64.b64decode(trust['keys'][envelope['key_id']]),raw,base64.b64decode(envelope['signature']))
    assert json.loads(raw)==second


@pytest.mark.parametrize('url',[
    'http://117.50.48.22/updates/installers/0.2.34/SuperMonoAgent-0.2.34-Setup.zip',
    'https://other.example/updates/installers/0.2.34/SuperMonoAgent-0.2.34-Setup.zip',
    '//other.example/updates/installers/0.2.34/SuperMonoAgent-0.2.34-Setup.zip',
    '/install/archive','/updates/installers/%2e%2e/private.zip',
])
def test_full_update_feed_refuses_other_origins_or_nonrelease_paths(tmp_path,payload,url):
    runtime,templates=payload;archive=tmp_path/'setup.zip'
    installers.build(runtime,archive,templates=templates,private_key=KEY)
    with pytest.raises(ValueError,match='relay HTTPS'):
        updates.package(archive,tmp_path/'manifest.json',url=url,private_key=KEY)


def test_full_update_feed_rejects_private_default_archives(tmp_path,payload,monkeypatch):
    runtime,templates=payload;token=tmp_path/'token.txt';token.write_text('private-test-fixture')
    monkeypatch.setattr(installers,'ROOT',tmp_path)
    archive=tmp_path/'.omx/personal.zip'
    installers.build(runtime,archive,templates=templates,private_key=KEY,token_file=token)
    with pytest.raises(ValueError,match='Private defaults'):
        updates.package(archive,tmp_path/'manifest.json',private_key=KEY)
