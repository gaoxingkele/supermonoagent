import importlib.util
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

from test_windows_installer_package import KEY,payload,package as installers
from test_windows_update_feed import updates

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('windows_release_publisher',ROOT/'scripts/publish-windows-release.py')
publisher=importlib.util.module_from_spec(spec);spec.loader.exec_module(publisher)
pytestmark=pytest.mark.skipif(not KEY.exists(),reason='Local release signing key required')


class LocalTransport:
    def __init__(self,root):self.root=root
    def path(self,value):return Path(value.replace('/fixture-root',self.root.as_posix()))
    def __call__(self,arguments,**options):
        if arguments[0]=='scp':
            destination=self.path(arguments[-1].split(':',1)[1])
            for source in arguments[-3:-1]:shutil.copyfile(source,destination/Path(source).name)
            return
        script=options['input'].decode()
        if script.startswith('set -eu'):
            for line in script.splitlines():
                if line.startswith('mkdir '):self.path(shlex.split(line)[-1]).mkdir(parents=True,exist_ok=True)
            return
        command,body=script.split(" <<'PY'\n",1)
        code=body.rsplit('\nPY',1)[0].replace('/fixture-root',self.root.as_posix())
        args=[value.replace('/fixture-root',self.root.as_posix()) for value in shlex.split(command)[2:]]
        result=subprocess.run([sys.executable,'-c',code,*args],capture_output=True)
        if result.returncode:raise RuntimeError('Fixture publication refused: '+result.stderr.decode(errors='replace'))


def test_full_release_publisher_switches_manifest_last_and_refuses_old_sequence(tmp_path,payload):
    runtime,templates=payload;archive=tmp_path/'setup.zip'
    installers.build(runtime,archive,templates=templates,private_key=KEY)
    first=tmp_path/'first.json';updates.package(archive,first,private_key=KEY)
    remote=tmp_path/'remote';runner=LocalTransport(remote)
    report=publisher.publish(archive,first,target='qa@localhost',remote='/fixture-root',runner=runner)
    assert report['published'] and report['version']=='0.2.34'
    channel=remote/'windows/stable/manifest.json'
    assert channel.read_bytes()==first.read_bytes()
    assert (remote/'installers/0.2.34/SuperMonoAgent-0.2.34-Setup.zip').read_bytes()==archive.read_bytes()
    second=tmp_path/'second.json';updates.package(archive,second,private_key=KEY)
    publisher.publish(archive,second,target='qa@localhost',remote='/fixture-root',runner=runner)
    assert channel.read_bytes()==second.read_bytes()
    with pytest.raises(RuntimeError,match='newer update'):
        publisher.publish(archive,first,target='qa@localhost',remote='/fixture-root',runner=runner)
    assert channel.read_bytes()==second.read_bytes()
    assert not list(remote.glob('.incoming-windows-*'))
