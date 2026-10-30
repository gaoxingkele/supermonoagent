"""Build signed public UI snapshots and publish them without shipping signing keys."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
AGENT_MODES = ('codex','claude-code','grok','kimi','pi-dev','cursor-cli')
NATIVE_API = 2
DESKTOP = ('workspace.html','workspace.js','workspace.css','terminal-client.js','tab-themes.js','tab-themes.css','xterm.js','xterm.css','xterm-fit.js','xterm-unicode11.js','xterm-webgl.js') + tuple('agent-modes/'+name+'.json' for name in AGENT_MODES)
VENDOR = ('xterm.js','xterm.css','xterm-fit.js','xterm-unicode11.js','xterm-webgl.js','terminal-client.js','file-client.js','file-upload.js')
MOBILE = ('index.html','app.js','app.css','mobile-core.js')
ANDROID = tuple('mobile/'+name for name in MOBILE) + tuple('mobile/vendor/'+name for name in VENDOR) + tuple('mobile/agent-modes/'+name+'.json' for name in AGENT_MODES)
SAFE_INTEGER = 9007199254740991
MAX_SNAPSHOT, MAX_ARCHIVE, MAX_FILE = 5*1024**2, 8*1024**2, 4*1024**2

# Runs in one SSH session. Linux needs only its existing Python3 standard library
# (fcntl); the Windows branch permits the identical transaction in local QA.
REMOTE_FINALIZER = r'''
import base64,hashlib,json,os,sys,uuid
from pathlib import Path
root,stage=map(Path,sys.argv[1:3])
revision=sys.argv[3]
stage_only=sys.argv[4]=='1'
hashes=json.loads(sys.argv[5])
preserve_android=len(sys.argv)>6 and sys.argv[6]=='1'
names={'desktop.zip','android.zip','manifest.json'}
lock_path=root/'.publish-lock'
if lock_path.is_symlink():raise SystemExit('Refusing publication lock symlink')
with lock_path.open('a+b') as lock:
    if os.name=='nt':
        import msvcrt
        # Byte-range locks beyond EOF need no initialization write.
        lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_LOCK,1)
    else:
        import fcntl
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
    # Publication lock held: OS releases it when this process exits or is killed.
    temporary=root/('.manifest.'+revision+'.'+uuid.uuid4().hex+'.tmp')
    try:
        if stage.is_symlink() or stage.parent!=root or not stage.name.startswith('.incoming-'):
            raise SystemExit('Invalid publication stage')
        if {item.name for item in stage.iterdir()}!=names:
            raise SystemExit('Unexpected staging files')
        for name in names:
            source=stage/name
            if source.is_symlink() or not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest()!=hashes[name]:
                raise SystemExit('Transfer SHA256 mismatch')
        channel=root/'manifest.json'
        candidate=(stage/'manifest.json').read_bytes()
        if channel.is_symlink():raise SystemExit('Refusing channel manifest symlink')
        previous=channel.read_bytes() if channel.exists() else None
        if preserve_android:
            if previous is None:raise SystemExit('Cannot preserve Android without current channel manifest')
            def android_descriptor(raw):
                try:
                    item=json.loads(base64.b64decode(json.loads(raw)['payload'],validate=True))['artifacts']['android']
                    if not isinstance(item,dict) or not {'sha256','size','native_api','files'}<=set(item):raise ValueError()
                    return json.dumps({key:value for key,value in item.items() if key!='url'},sort_keys=True,separators=(',',':'))
                except (ValueError,KeyError,TypeError):
                    raise SystemExit('Invalid Android channel descriptor') from None
            if android_descriptor(candidate)!=android_descriptor(previous):
                raise SystemExit('Android changed since baseline; refusing desktop-only publication')
        if not stage_only and previous is not None:
            def sequence(raw):return json.loads(base64.b64decode(json.loads(raw)['payload'],validate=True))['sequence']
            if sequence(candidate)<=sequence(previous) and candidate!=previous:
                raise SystemExit('Refusing channel sequence downgrade')
        destination=root/'releases'/revision
        if destination.is_symlink():raise SystemExit('Refusing release symlink')
        if destination.exists():
            if not destination.is_dir() or {item.name for item in destination.iterdir()}!=names:
                raise SystemExit('Incomplete immutable release')
            for name in names:
                existing=destination/name
                if existing.is_symlink() or existing.read_bytes()!=(stage/name).read_bytes():
                    raise SystemExit('Conflicting immutable release')
        else:
            os.replace(stage,destination)
        if not stage_only:
            with temporary.open('xb') as output:
                output.write(candidate);output.flush();os.fsync(output.fileno())
            os.replace(temporary,channel)
    finally:
        temporary.unlink(missing_ok=True)
        if stage.exists() and not stage.is_symlink():
            for name in names:(stage/name).unlink(missing_ok=True)
            stage.rmdir()
'''

def json_bytes(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':'),sort_keys=True).encode('utf-8')

def digest(data):
    return hashlib.sha256(data).hexdigest()

def revision_id(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}',value) or '..' in value or value == 'bundled':
        raise ValueError('Revision must be a safe ASCII identifier; bundled and .. are reserved')
    return value

def feed_url(value):
    parsed=urlsplit(value)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Update feed requires HTTPS without credentials, query or fragment')
    parsed.port
    return value

def openssl_tool(value=None):
    candidates=[value,shutil.which('openssl'),r'D:/Program Files/Git/usr/bin/openssl.exe',r'C:/Program Files/Git/usr/bin/openssl.exe']
    for candidate in candidates:
        if candidate and Path(candidate).is_file():return str(Path(candidate).resolve())
    raise ValueError('OpenSSL executable not found; use --openssl')

def run(arguments,**kwargs):
    kwargs.setdefault('timeout',180)
    result=subprocess.run(arguments,capture_output=True,**kwargs)
    if result.returncode:
        raise RuntimeError('Command failed: '+Path(str(arguments[0])).name+' ('+str(result.returncode)+')')
    return result.stdout

def files_for_platform(root,platform,feed):
    static=root/'src/supermonoagent/static'
    if platform=='desktop':
        origin=urlsplit(feed)
        environment={**os.environ,'PYTHONPATH':str(root/'src'),'SUPERMONO_DEFAULT_RELAY':origin.scheme+'://'+origin.netloc}
        html=run([sys.executable,'-c','from supermonoagent.workspace import PAGE; import sys; sys.stdout.buffer.write(PAGE.encode("utf-8"))'],env=environment)
        files={'workspace.html':html}
        files.update({name:(static/name).read_bytes() for name in DESKTOP if name!='workspace.html'})
        files['workspace.js']=run([sys.executable,'-c','from supermonoagent.workspace import javascript; import sys; sys.stdout.buffer.write(javascript().encode("utf-8"))'],env=environment)
    else:
        files={'mobile/'+name:(root/'android/assets/mobile'/name).read_bytes() for name in MOBILE}
        files.update({'mobile/vendor/'+name:(static/name).read_bytes() for name in VENDOR})
        files.update({'mobile/agent-modes/'+name+'.json':(static/'agent-modes'/(name+'.json')).read_bytes() for name in AGENT_MODES})
    if sum(map(len,files.values()))>MAX_SNAPSHOT or any(len(data)>MAX_FILE for data in files.values()):
        raise ValueError('UI snapshot exceeds size limit')
    return files

def source_fingerprint(root,*,include_android=True):
    paths=[root/'src/supermonoagent/workspace.py',root/'src/supermonoagent/defaults.py']
    paths += [root/'src/supermonoagent/static'/name for name in DESKTOP if name!='workspace.html']
    paths += [root/'src/supermonoagent/static'/name for name in ('file-client.js','file-workspace-ui.js','clipboard-files.js','terminal-composer.js','terminal-questions.js','clipboard-context-menu.js','mylib-assistant.js','assistant-chat.js')]
    if include_android:
        paths += [root/'android/assets/mobile'/name for name in MOBILE]
    return {str(path.relative_to(root)):digest(path.read_bytes()) for path in paths}

def make_archive(path,files):
    with zipfile.ZipFile(path,'x',compression=zipfile.ZIP_DEFLATED) as archive:
        for name,data in sorted(files.items()):
            info=zipfile.ZipInfo(name,date_time=(2026,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
            info.external_attr=0o100644<<16;archive.writestr(info,data)
    if path.stat().st_size>MAX_ARCHIVE:raise ValueError('UI archive exceeds download limit')

def verify_local(directory,trust,openssl=None,now=None):
    """Independently verify exact envelope signature, archive/catalog and allowlists."""
    tool=openssl_tool(openssl);directory=Path(directory)
    raw=(directory/'manifest.json').read_bytes()
    if len(raw)>128*1024:raise ValueError('Manifest exceeds limit')
    envelope=json.loads(raw)
    if set(envelope)!={'schema','key_id','payload','signature'} or type(envelope['schema']) is not int or envelope['schema']!=1:raise ValueError('Invalid envelope')
    payload=base64.b64decode(envelope['payload'],validate=True)
    signature=base64.b64decode(envelope['signature'],validate=True)
    der=base64.b64decode(trust['keys'][envelope['key_id']],validate=True)
    with tempfile.TemporaryDirectory(prefix='sma-ui-signature-') as tmp:
        tmp=Path(tmp);(tmp/'public.der').write_bytes(der);(tmp/'payload').write_bytes(payload);(tmp/'signature').write_bytes(signature)
        run([tool,'pkey','-pubin','-inform','DER','-in',str(tmp/'public.der'),'-out',str(tmp/'public.pem')])
        run([tool,'dgst','-sha256','-verify',str(tmp/'public.pem'),'-signature',str(tmp/'signature'),str(tmp/'payload')])
    value=json.loads(payload);rev=revision_id(value['revision']);clock=int(time.time() if now is None else now)
    if type(value['schema']) is not int or value['schema']!=1 or value['channel']!='stable' or type(value['sequence']) is not int or not 0<value['sequence']<=SAFE_INTEGER:raise ValueError('Invalid release metadata')
    if type(value['created']) is not int or type(value['expires']) is not int or value['created']>clock+300 or value['expires']<=max(clock,value['created']):raise ValueError('Invalid release freshness')
    if set(value['artifacts'])!={'desktop','android'}:raise ValueError('Both platform archives are required')
    for platform,allowed in [('desktop',DESKTOP),('android',ANDROID)]:
        item=value['artifacts'][platform]
        expected='releases/'+rev+'/'+platform+'.zip'
        if item['url']!=expected or type(item['native_api']) is not int or item['native_api']!=NATIVE_API or set(item['files'])!=set(allowed):raise ValueError('Invalid artifact catalog')
        data=(directory/(platform+'.zip')).read_bytes()
        if type(item['size']) is not int or not 0<item['size']<=MAX_ARCHIVE or len(data)!=item['size'] or digest(data)!=item['sha256']:raise ValueError('Archive hash/size mismatch')
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries=archive.infolist()
            if len(entries)!=len(allowed) or {entry.filename for entry in entries}!=set(allowed):raise ValueError('Archive allowlist mismatch')
            if len({entry.filename.casefold() for entry in entries})!=len(entries):raise ValueError('Case-colliding archive entries')
            if sum(entry.file_size for entry in entries)>MAX_SNAPSHOT:raise ValueError('Snapshot too large')
            for entry in entries:
                if entry.file_size>MAX_FILE or entry.external_attr>>16&0o170000 not in {0,0o100000} or entry.flag_bits&1:raise ValueError('Invalid archive entry')
                content=archive.read(entry);metadata=item['files'][entry.filename]
                if type(metadata.get('size')) is not int or metadata!={'size':len(content),'sha256':digest(content)}:raise ValueError('File catalog mismatch')
    return value


def frozen_android_release(directory,trust,openssl):
    """Verify the complete signed baseline using exactly the frozen input bytes."""
    directory=Path(directory)
    frozen={}
    for name,limit in [('manifest.json',128*1024),('desktop.zip',MAX_ARCHIVE),('android.zip',MAX_ARCHIVE)]:
        with (directory/name).open('rb') as source:data=source.read(limit+1)
        if len(data)>limit:raise ValueError('Baseline release exceeds size limit')
        frozen[name]=data
    with tempfile.TemporaryDirectory(prefix='sma-ui-baseline-') as tmp:
        tmp=Path(tmp)
        for name,data in frozen.items():(tmp/name).write_bytes(data)
        metadata=verify_local(tmp,trust,openssl)
    return metadata,frozen['android.zip']

@contextmanager
def build_lock(output):
    output.mkdir(parents=True,exist_ok=True);lock=output/'.package-lock'
    # The inode persists; ownership is an OS lock released even on process death.
    # Unlinking a lock file can let contenders lock different inodes concurrently.
    with lock.open('a+b') as stream:
        if os.name=='nt':
            import msvcrt
            # A contender may already own this range on an empty file.
            stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_LOCK,1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX)
        try:yield
        finally:
            if os.name=='nt':stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)

def package(output,*,root=ROOT,revision=None,private_key=None,key_id=None,openssl=None,feed='https://117.50.48.22/updates/stable/manifest.json',lifetime_days=30,reuse_android_from=None):
    output=Path(output).resolve();root=Path(root).resolve();feed_url(feed)
    if reuse_android_from is not None and (not isinstance(revision,str) or not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)',revision)):
        raise ValueError('Android reuse requires an explicit numeric version, such as 0.2.39')
    if not 1<=lifetime_days<=90:raise ValueError('Manifest lifetime must be 1–90 days')
    tool=openssl_tool(openssl);key=Path(private_key or root/'.omx/update-signing/development-private.pem').resolve()
    if not key.is_file():raise ValueError('Signing key missing; keys are never generated implicitly')
    trust=json.loads((root/'src/supermonoagent/static/update-trust.json').read_text(encoding='utf-8'))
    if key_id is None:
        if len(trust['keys'])!=1:raise ValueError('Select --key-id from bundled trust anchors')
        key_id=next(iter(trust['keys']))
    if key_id not in trust['keys']:raise ValueError('Signing key ID is not bundled')
    baseline=None;android_bytes=None
    if reuse_android_from is not None:
        baseline,android_bytes=frozen_android_release(reuse_android_from,trust,tool)
        if revision==baseline['revision']:raise ValueError('New revision must differ from immutable baseline revision')
    with build_lock(output):
        created=int(time.time());revision=revision_id(revision or time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+uuid.uuid4().hex[:8])
        releases=output/'releases';releases.mkdir(exist_ok=True);destination=releases/revision
        if destination.exists():raise ValueError('Revision already exists; immutable releases cannot be overwritten')
        sequence_file=output/'.highest-sequence'
        previous=int(sequence_file.read_text()) if sequence_file.exists() else 0
        if baseline is not None:previous=max(previous,baseline['sequence'])
        if (output/'manifest.json').exists():
            current=json.loads(base64.b64decode(json.loads((output/'manifest.json').read_bytes())['payload'],validate=True))
            previous=max(previous,current['sequence'])
        sequence=max(time.time_ns()//1000,previous+1)
        if sequence>SAFE_INTEGER:raise ValueError('Sequence exceeds JavaScript safe integer')
        with tempfile.TemporaryDirectory(prefix='.build-',dir=output) as tmp:
            tmp=Path(tmp);artifacts={};before=source_fingerprint(root,include_android=baseline is None)
            for platform in ['desktop','android']:
                if platform=='android' and baseline is not None:
                    (tmp/'android.zip').write_bytes(android_bytes)
                    artifacts['android']={**baseline['artifacts']['android'],'url':'releases/'+revision+'/android.zip'}
                    continue
                files=files_for_platform(root,platform,feed);archive=tmp/(platform+'.zip');make_archive(archive,files);data=archive.read_bytes()
                artifacts[platform]={'url':'releases/'+revision+'/'+platform+'.zip','sha256':digest(data),'size':len(data),'native_api':NATIVE_API,'files':{name:{'sha256':digest(content),'size':len(content)} for name,content in files.items()}}
            if before!=source_fingerprint(root,include_android=baseline is None):raise ValueError('UI source changed during packaging; retry after edits finish')
            payload=json_bytes({'schema':1,'channel':'stable','revision':revision,'sequence':sequence,'created':created,'expires':created+lifetime_days*86400,'artifacts':artifacts})
            (tmp/'payload').write_bytes(payload)
            run([tool,'dgst','-sha256','-sign',str(key),'-out',str(tmp/'signature'),str(tmp/'payload')])
            envelope=json_bytes({'schema':1,'key_id':key_id,'payload':base64.b64encode(payload).decode('ascii'),'signature':base64.b64encode((tmp/'signature').read_bytes()).decode('ascii')})
            (tmp/'manifest.json').write_bytes(envelope);verify_local(tmp,trust,tool)
            destination.mkdir()  # exclusive publication of this revision, never a replacement
            for name in ['desktop.zip','android.zip','manifest.json']:os.replace(tmp/name,destination/name)
            atomic=output/('.manifest-'+uuid.uuid4().hex);atomic.write_bytes(envelope);os.replace(atomic,output/'manifest.json')
            sequence_temp=output/('.sequence-'+uuid.uuid4().hex)
            sequence_temp.write_text(str(sequence),encoding='ascii');os.replace(sequence_temp,sequence_file)
        return destination

def ssh_target(value):
    if not re.fullmatch(r'(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9_][A-Za-z0-9_.-]*',value):raise ValueError('Use a safe SSH host alias or user@hostname')
    return value

def remote_root(value):
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+',value) or any(part in {'.','..',''} for part in value[1:].split('/')):raise ValueError('Remote root must be a normalized absolute Linux path without shell metacharacters')
    return value

def android_promotion_guard(remote,stage_only,preserve_android):
    if type(preserve_android) is not bool:raise ValueError('preserve_android must be boolean')
    if type(stage_only) is not bool:raise ValueError('stage_only must be boolean')
    if preserve_android and not stage_only and remote=='/var/lib/supermono-updates/stable':
        raise ValueError('The shared channel revision can cause Android UI rollback even when its archive is unchanged. '
                         'Reuse/preserve promotion is blocked: publish the current Desktop and Android sources together, '
                         'or use a future independent platform channel. Local builds and stage-only remain available.')


def _publish_public_snapshot(directory,*,target,port=22,key=None,remote='/var/lib/supermono-updates/stable',trust=None,openssl=None,runner=run,stage_only=False,preserve_android=False):
    target=ssh_target(target);remote=remote_root(remote)
    if type(port) is not int or not 1<=port<=65535:raise ValueError('Invalid SSH port')
    android_promotion_guard(remote,stage_only,preserve_android)
    directory=Path(directory).resolve();trust=trust or json.loads((ROOT/'src/supermonoagent/static/update-trust.json').read_text())
    metadata=verify_local(directory,trust,openssl);revision=metadata['revision']
    options=['-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=15','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=2']
    if key:options+=['-i',str(Path(key).resolve())]
    ssh=['ssh','-p',str(port),*options,target]
    scp=['scp','-P',str(port),*options]
    stage=remote+'/.incoming-'+revision+'-'+uuid.uuid4().hex
    # All remote strings are validated and shell-quoted; uploaded names are fixed.
    prepare='set -eu\numask 022\nmkdir -p '+shlex.quote(remote+'/releases')+'\ntest ! -L '+shlex.quote(remote)+'\ntest ! -L '+shlex.quote(remote+'/releases')+'\nmkdir '+shlex.quote(stage)+'\n'
    runner([*ssh,'sh -s'],input=prepare.encode())
    try:
        runner([*scp,*[str(directory/name) for name in ['desktop.zip','android.zip','manifest.json']],target+':'+stage+'/'])
    except (RuntimeError,subprocess.SubprocessError,OSError):
        # Remove only the three known public files from this unique staging directory.
        cleanup='set -eu\nrm -f '+ ' '.join(shlex.quote(stage+'/'+name) for name in ['desktop.zip','android.zip','manifest.json'])+'\nrmdir '+shlex.quote(stage)+'\n'
        try:runner([*ssh,'sh -s'],input=cleanup.encode())
        except (RuntimeError,subprocess.SubprocessError,OSError):pass
        raise
    hashes={name:digest((directory/name).read_bytes()) for name in ['desktop.zip','android.zip','manifest.json']}
    # Uploads do not hold a global lock. One short OS-locked transaction verifies
    # hashes, installs/reuses the complete version, then switches manifest last.
    arguments=[remote,stage,revision,'1' if stage_only else '0',json.dumps(hashes,separators=(',',':')),'1' if preserve_android else '0']
    finalize='set -eu\npython3 - '+' '.join(shlex.quote(value) for value in arguments)+" <<'PY'\n"+REMOTE_FINALIZER+'\nPY\n'
    runner([*ssh,'sh -s'],input=finalize.encode())
    return {'revision':revision,'sequence':metadata['sequence'],'remote_manifest':remote+'/manifest.json','staged_only':stage_only}

def publish(directory,*,target,port=22,key=None,remote='/var/lib/supermono-updates/stable',trust=None,openssl=None,runner=run,stage_only=False,preserve_android=False):
    # Freeze all public inputs before verification/upload so another build cannot
    # replace an archive between the signature check and transfer-hash calculation.
    ssh_target(target);remote_root(remote)
    if type(port) is not int or not 1<=port<=65535:raise ValueError('Invalid SSH port')
    android_promotion_guard(remote,stage_only,preserve_android)
    with tempfile.TemporaryDirectory(prefix='sma-ui-public-release-') as tmp:
        snapshot=Path(tmp)
        for name in ['desktop.zip','android.zip','manifest.json']:shutil.copyfile(Path(directory)/name,snapshot/name)
        return _publish_public_snapshot(snapshot,target=target,port=port,key=key,remote=remote,trust=trust,openssl=openssl,runner=runner,stage_only=stage_only,preserve_android=preserve_android)

def parser(publishing=False):
    cli=argparse.ArgumentParser(description='Build both signed UI bundles'+(' and optionally publish public files over SSH' if publishing else ''))
    cli.add_argument('--output',type=Path,default=ROOT/'.omx/ui-releases/stable');cli.add_argument('--revision')
    cli.add_argument('--private-key',type=Path);cli.add_argument('--key-id');cli.add_argument('--openssl')
    cli.add_argument('--feed',type=feed_url,default='https://117.50.48.22/updates/stable/manifest.json')
    cli.add_argument('--lifetime-days',type=int,default=30)
    cli.add_argument('--reuse-android-from',type=Path,help='Reuse Android bytes from a complete trusted signed local release; requires numeric --revision')
    if publishing:
        mode=cli.add_mutually_exclusive_group();mode.add_argument('--build-only',action='store_true');mode.add_argument('--stage-only',action='store_true');mode.add_argument('--publish',action='store_true')
        cli.add_argument('--release',type=Path,help='Verify/publish an existing immutable local release without signing again')
        cli.add_argument('--preserve-android',action='store_true',help='Under the publication lock, refuse if the current Android descriptor differs; automatic with --reuse-android-from')
        cli.add_argument('--ssh-target',type=ssh_target);cli.add_argument('--ssh-port',type=int,default=22)
        cli.add_argument('--ssh-key',type=Path);cli.add_argument('--remote-root',type=remote_root,default='/var/lib/supermono-updates/stable')
    return cli

def main(publishing=False):
    args=parser(publishing).parse_args()
    if publishing and (args.stage_only or args.publish) and not args.ssh_target:raise ValueError('Remote mode needs --ssh-target')
    if publishing and args.ssh_target and not(args.build_only or args.stage_only or args.publish):raise ValueError('Select --build-only, --stage-only or --publish explicitly')
    if publishing and args.ssh_target and not args.build_only:
        android_promotion_guard(args.remote_root,args.stage_only,args.preserve_android or args.reuse_android_from is not None)
    if publishing and args.release:
        if args.revision:raise ValueError('--release and --revision cannot be combined')
        if args.reuse_android_from:raise ValueError('--release and --reuse-android-from cannot be combined')
        directory=args.release.resolve();verify_local(directory,json.loads((ROOT/'src/supermonoagent/static/update-trust.json').read_text()),args.openssl)
    else:directory=package(args.output,revision=args.revision,private_key=args.private_key,key_id=args.key_id,openssl=args.openssl,feed=args.feed,lifetime_days=args.lifetime_days,reuse_android_from=args.reuse_android_from)
    report={'local_release':str(directory),'published':False}
    if publishing and args.ssh_target and not args.build_only:
        report.update(publish(directory,target=args.ssh_target,port=args.ssh_port,key=args.ssh_key,remote=args.remote_root,openssl=args.openssl,stage_only=args.stage_only,preserve_android=args.preserve_android or args.reuse_android_from is not None));report['published']=not args.stage_only
    print(json.dumps(report,ensure_ascii=False))
