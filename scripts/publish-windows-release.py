"""Build and publish a complete public Windows installer and signed update feed."""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from ui_update_release import ssh_target,remote_root,run


def load(name,filename):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/filename)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


installer=load('sma_windows_installer','package-windows-installer.py')
feed=load('sma_windows_feed','package-windows-update-feed.py')

FINALIZE=r'''
import base64,hashlib,json,os,shutil,sys
from pathlib import Path
root,stage=map(Path,sys.argv[1:3]);version=sys.argv[3];expected=json.loads(sys.argv[4])
if root.is_symlink() or stage.parent!=root or stage.is_symlink() or not stage.name.startswith('.incoming-windows-'):raise SystemExit('Invalid publish paths')
if set(p.name for p in stage.iterdir())!={'setup.zip','manifest.json'}:raise SystemExit('Unexpected staging files')
for name,sha in expected.items():
 p=stage/name
 if p.is_symlink() or hashlib.sha256(p.read_bytes()).hexdigest()!=sha:raise SystemExit('Upload hash differs')
lock=root/'.windows-publish-lock'
if lock.is_symlink():raise SystemExit('Invalid publication lock')
with lock.open('a+b') as handle:
 if os.name=='nt':
  import msvcrt
  handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_LOCK,1)
 else:
  import fcntl
  fcntl.flock(handle,fcntl.LOCK_EX)
 channel=root/'windows/stable';destination=root/'installers'/version
 channel.mkdir(parents=True,exist_ok=True);destination.mkdir(parents=True,exist_ok=True)
 if channel.is_symlink() or destination.is_symlink():raise SystemExit('Publication directory is a link')
 manifest=channel/'manifest.json';candidate=(stage/'manifest.json').read_bytes()
 metadata=json.loads(base64.b64decode(json.loads(candidate)['payload']))
 if metadata['version']!=version or metadata['sha256']!=expected['setup.zip']:raise SystemExit('Metadata does not identify archive')
 if manifest.exists():
  if manifest.is_symlink():raise SystemExit('Channel is a link')
  previous=json.loads(base64.b64decode(json.loads(manifest.read_bytes())['payload']))
  if previous['sequence']>=metadata['sequence'] and manifest.read_bytes()!=candidate:raise SystemExit('A newer update is already published')
 archive=destination/('SuperMonoAgent-'+version+'-Setup.zip')
 if archive.exists():
  if archive.is_symlink() or hashlib.sha256(archive.read_bytes()).hexdigest()!=expected['setup.zip']:raise SystemExit('Immutable installer differs')
 else:
  temporary=destination/('.setup-'+stage.name+'.tmp');shutil.copyfile(stage/'setup.zip',temporary);os.chmod(temporary,0o644);os.replace(temporary,archive)
 sidecar=destination/(archive.name+'.sha256');sidecar.write_text(expected['setup.zip']+'  '+archive.name+chr(10));os.chmod(sidecar,0o644)
 temporary=channel/('.manifest-'+stage.name+'.tmp');temporary.write_bytes(candidate);os.chmod(temporary,0o644);os.replace(temporary,manifest)
for name in ('setup.zip','manifest.json'):(stage/name).unlink()
stage.rmdir()
'''


def publish(archive,manifest,*,target,port=22,key=None,remote='/var/lib/supermono-updates',runner=run):
    target=ssh_target(target);remote=remote_root(remote)
    if type(port) is not int or not 1<=port<=65535:raise ValueError('Invalid SSH port')
    version=feed.validate_archive(archive)
    with tempfile.TemporaryDirectory(prefix='sma-full-publish-') as directory:
        directory=Path(directory);archive_bytes=Path(archive).read_bytes();manifest_bytes=Path(manifest).read_bytes()
        (directory/'setup.zip').write_bytes(archive_bytes);(directory/'manifest.json').write_bytes(manifest_bytes)
        envelope=json.loads(manifest_bytes);payload=json.loads(base64.b64decode(envelope['payload']))
        if payload['version']!=version or payload['sha256']!=hashlib.sha256(archive_bytes).hexdigest():raise ValueError('Update manifest differs from installer')
        from supermonoagent.ui_updates import verify_signature
        trust=json.loads(feed.TRUST.read_text());verify_signature(base64.b64decode(trust['keys'][envelope['key_id']]),base64.b64decode(envelope['payload']),base64.b64decode(envelope['signature']))
        options=['-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=15']
        if key:options+=['-i',str(Path(key).resolve())]
        ssh=['ssh','-p',str(port),*options,target];scp=['scp','-P',str(port),*options]
        stage=remote+'/.incoming-windows-'+uuid.uuid4().hex
        runner([*ssh,'sh -s'],input=('set -eu\nmkdir -p '+shlex.quote(remote)+'\ntest ! -L '+shlex.quote(remote)+'\nmkdir '+shlex.quote(stage)+'\n').encode())
        try:
            runner([*scp,str(directory/'setup.zip'),str(directory/'manifest.json'),target+':'+stage+'/'])
            hashes={'setup.zip':hashlib.sha256(archive_bytes).hexdigest(),'manifest.json':hashlib.sha256(manifest_bytes).hexdigest()}
            command='/usr/bin/python3 - '+' '.join(shlex.quote(x) for x in [remote,stage,version,json.dumps(hashes,separators=(',',':'))])+" <<'PY'\n"+FINALIZE+'\nPY\n'
            runner([*ssh,'sh -s'],input=command.encode())
        except (OSError,RuntimeError,subprocess.SubprocessError):
            # Only remove this unique stage's fixed public inputs, never versions.
            cleanup="/usr/bin/python3 - <<'PY'\nfrom pathlib import Path\np=Path("+repr(stage)+")\nif p.parent==Path("+repr(remote)+") and p.name.startswith('.incoming-windows-') and not p.is_symlink():\n for name in ('setup.zip','manifest.json'):(p/name).unlink(missing_ok=True)\n p.rmdir()\nPY\n"
            try:runner([*ssh,'sh -s'],input=cleanup.encode())
            except (OSError,RuntimeError,subprocess.SubprocessError):pass
            raise
    return {'version':version,'sequence':payload['sequence'],'manifest_url':'https://117.50.48.22/updates/windows/stable/manifest.json','published':True}


def main():
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--runtime',type=Path,required=True);cli.add_argument('--publish',action='store_true')
    cli.add_argument('--ssh-target',type=ssh_target);cli.add_argument('--ssh-port',type=int,default=22);cli.add_argument('--ssh-key',type=Path)
    args=cli.parse_args()
    if args.publish and not args.ssh_target:cli.error('--publish needs --ssh-target')
    version=(args.runtime/'version.txt').read_text().strip();archive=ROOT/'dist/windows'/('SuperMonoAgent-'+version+'-Setup.zip');manifest=ROOT/'.omx/windows-releases/stable/manifest.json'
    installer.build(args.runtime,archive);feed.package(archive,manifest)
    result={'version':version,'archive':str(archive),'manifest':str(manifest),'published':False}
    if args.publish:result.update(publish(archive,manifest,target=args.ssh_target,port=args.ssh_port,key=args.ssh_key))
    print(json.dumps(result))


if __name__=='__main__':main()
