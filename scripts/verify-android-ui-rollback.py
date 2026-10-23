"""Prepare isolated Android rollback QA assets. This tool never invokes adb.

The QA APK has its own application ID/data directory and a nonce-scoped HTTPS
feed. Only that feed can be published; the production stable channel is checked
before and after every remote operation. Device installation and interaction
are deliberately left to the coordinating operator.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.request import ProxyHandler, Request, build_opener
import uuid
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / '.omx/artifacts/android-ui-rollback-qa'
QA_PACKAGE = 'dev.supermonoagent.mobile.qa'
ORIGINAL_PACKAGE = 'dev.supermonoagent.mobile'
SERVER = 'https://117.50.48.22'
PRODUCTION_FEED = SERVER + '/updates/stable/manifest.json'
REMOTE_BASE = '/var/lib/supermono-updates/qa/'
PHASES = ('good', 'throw', 'hang')
ANDROID_NS = 'http://schemas.android.com/apk/res/android'
sys.path.insert(0, str(ROOT / 'scripts'))
import ui_update_release as release


def digest(data):
    return hashlib.sha256(data).hexdigest()


def public_download(url):
    if not url.startswith(SERVER + '/updates/') or any(c in url for c in ('?', '#')):
        raise ValueError('QA verification is confined to the existing HTTPS origin')
    request = Request(url, headers={'User-Agent': 'SuperMonoAgent-Isolated-Android-QA/1'})
    with build_opener(ProxyHandler({})).open(request, timeout=15) as response:
        return response.read(8 * 1024 * 1024 + 1)


def safe_run(value):
    if not re.fullmatch(r'qa-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}', value):
        raise ValueError('Use the generated QA run ID, never a production path')
    return value


def paths(run_id):
    run_id = safe_run(run_id)
    local = (ARTIFACTS / run_id).resolve()
    if not local.is_relative_to(ARTIFACTS.resolve()):
        raise ValueError('Local QA path escaped artifact directory')
    remote = REMOTE_BASE + run_id + '/stable'
    feed = SERVER + '/updates/qa/' + run_id + '/stable/manifest.json'
    return local, remote, feed


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding='utf8')


def snapshot_sources(clone):
    selected = []
    for directory in ('android', 'src/supermonoagent'):
        source = ROOT / directory
        destination = clone / directory
        shutil.copytree(source, destination, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        selected.extend(p for p in source.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc')
    (clone / 'scripts').mkdir(parents=True)
    for name in ('build-android.ps1', 'append-apk-dex.py'):
        shutil.copy2(ROOT / 'scripts' / name, clone / 'scripts' / name)
        selected.append(ROOT / 'scripts' / name)
    before = {p.relative_to(ROOT).as_posix(): digest(p.read_bytes()) for p in selected}
    for name, expected in before.items():
        if digest((clone / name).read_bytes()) != expected:
            raise RuntimeError('Source changed while the isolated copy was made; retry a new QA run')
    return before


def isolate_clone(clone, feed):
    manifest = clone / 'android/AndroidManifest.xml'
    ET.register_namespace('android', ANDROID_NS)
    tree = ET.parse(manifest)
    node = tree.getroot()
    if node.get('package') != ORIGINAL_PACKAGE or node.get('{'+ANDROID_NS+'}sharedUserId'):
        raise ValueError('Unexpected production package or shared UID')
    node.set('package', QA_PACKAGE)
    node.set('{'+ANDROID_NS+'}versionCode', '0')
    node.set('{'+ANDROID_NS+'}versionName', '0.0.0')
    app = node.find('application')
    app.set('{'+ANDROID_NS+'}label', 'SuperMonoAgent UI QA')
    # Only the isolated QA package is debuggable, so its state can be inspected.
    app.set('{'+ANDROID_NS+'}debuggable', 'true')
    for component in app:
        key = '{'+ANDROID_NS+'}name'
        name = component.get(key, '')
        if name.startswith('.'):
            component.set(key, ORIGINAL_PACKAGE + name)
    tree.write(manifest, encoding='utf-8', xml_declaration=True)
    remote = clone / 'android/src/dev/supermonoagent/mobile/RemoteUpdates.java'
    content = remote.read_text(encoding='utf8')
    old = 'DEFAULT_FEED="' + PRODUCTION_FEED + '"'
    if content.count(old) != 1:
        raise ValueError('Production feed constant was not an exact single match')
    remote.write_text(content.replace(old, 'DEFAULT_FEED="'+feed+'"'), encoding='utf8')
    mobile = clone / 'android/assets/mobile/app.js'
    mobile.write_bytes(mobile.read_bytes() + b'\nwindow.SMA_QA_BASELINE=true;\n')
    return mobile.read_bytes()


def build_apk(clone, local):
    output = local / 'apk'
    command = ['powershell', '-NoProfile', '-File', str(clone/'scripts/build-android.ps1'),
               '-ToolsRoot', str(ROOT/'.omx/tools'), '-OutputDir', str(output)]
    result = subprocess.run(command, capture_output=True, timeout=180)
    (local/'build-qa-apk.stdout.txt').write_bytes(result.stdout)
    (local/'build-qa-apk.stderr.txt').write_bytes(result.stderr)
    if result.returncode:
        raise RuntimeError('QA APK build failed; see the isolated build logs')
    apk = output/'SuperMonoAgent.apk'
    java = next((ROOT/'.omx/tools/jdk').glob('*/bin/java.exe'))
    tool = ROOT/'.omx/tools/android-sdk/build-tools/35.0.0'
    environment = {**os.environ, 'JAVA_HOME': str(java.parent.parent),
                   'PATH': str(java.parent)+os.pathsep+os.environ.get('PATH','')}
    verify = subprocess.run([str(tool/'apksigner.bat'),'verify','--verbose','--print-certs',str(apk)],
                            env=environment,capture_output=True,timeout=30)
    if verify.returncode:
        raise RuntimeError('QA APK signature verification failed')
    (local/'qa-apksigner-verification.txt').write_bytes(verify.stdout)
    badging = subprocess.run([str(tool/'aapt2.exe'),'dump','badging',str(apk)],capture_output=True,timeout=30)
    if badging.returncode or ("package: name='"+QA_PACKAGE+"'").encode() not in badging.stdout:
        raise RuntimeError('APK application ID is not the isolated QA ID')
    (local/'qa-apk-badging.txt').write_bytes(badging.stdout)
    with zipfile.ZipFile(apk) as archive:
        assert json.loads(archive.read('assets/connection-defaults.json')) == {}
        assert json.loads(archive.read('assets/update-api.json'))['native_api'] == 2
        assert feed_bytes(local) in archive.read('classes.dex'), 'QA feed missing from DEX'
        assert not any(name.lower().endswith(('.pem','.key','.keystore','.jks')) for name in archive.namelist())
        for name in archive.namelist():
            if name.startswith('assets/'):
                data = archive.read(name)
                assert b'BEGIN PRIVATE KEY' not in data and b'BEGIN RSA PRIVATE KEY' not in data
    return {'path':str(apk.relative_to(ROOT)), 'sha256':digest(apk.read_bytes()),
            'package':QA_PACKAGE, 'activity':QA_PACKAGE+'/'+ORIGINAL_PACKAGE+'.MainActivity',
            'signature_verified':True, 'generic_defaults':True, 'debuggable_qa_only':True}


def feed_bytes(local):
    return (SERVER+'/updates/qa/'+local.name+'/stable/manifest.json').encode()


def publish_phase(local, phase, *, stage_only=False):
    report = json.loads((local/'preparation.json').read_text(encoding='utf8'))
    remote = paths(local.name)[1]
    if not remote.startswith(REMOTE_BASE) or '/updates/stable' in remote:
        raise ValueError('Refusing any non-QA remote publication root')
    before = public_download(PRODUCTION_FEED)
    sys.path.insert(0,str(ROOT/'.omx'))
    import relay_admin
    client = relay_admin.connect()
    def runner(arguments, **options):
        if arguments[0] == 'ssh':
            # Publisher creates only fixed public files below the supplied QA root.
            script = options['input']
            if remote.encode() not in script:
                raise ValueError('Unexpected publication script')
            stdin,stdout,stderr=client.exec_command('sh -s',timeout=60)
            stdin.write(script);stdin.channel.shutdown_write()
            out,err=stdout.read(),stderr.read()
            if stdout.channel.recv_exit_status():raise RuntimeError('QA remote transaction failed')
            return SimpleNamespace(returncode=0,stdout=out,stderr=err)
        if arguments[0] == 'scp':
            destination=arguments[-1].split(':',1)[1]
            if not destination.startswith(remote+'/.incoming-') or not destination.endswith('/'):
                raise ValueError('QA upload escaped its nonce-scoped directory')
            sources=arguments[-4:-1]
            if {Path(name).name for name in sources}!={'desktop.zip','android.zip','manifest.json'}:
                raise ValueError('Only public UI artifacts may be uploaded')
            with client.open_sftp() as sftp:
                for name in sources:sftp.put(name,destination+Path(name).name)
            return SimpleNamespace(returncode=0)
        raise ValueError('Unexpected QA publication transport')
    try:
        published=release.publish(local/report['phases'][phase]['directory'],target='root@117.50.48.22',
                                  remote=remote,runner=runner,stage_only=stage_only)
    finally:client.close()
    after=public_download(PRODUCTION_FEED)
    evidence={'phase':phase, 'stage_only':stage_only, 'production_stable_before_sha256':digest(before),
              'production_stable_after_sha256':digest(after), 'production_stable_unchanged':before==after,
              'publication':published, 'time':int(time.time())}
    write_json(local/('publish-'+phase+('-staged' if stage_only else '-active')+'.json'),evidence)
    if before!=after:raise RuntimeError('Production stable changed concurrently; inspect evidence before continuing')
    return evidence


def cleanup(local):
    """Retire only this run's verified public files; never touch phone data."""
    report=json.loads((local/'preparation.json').read_text(encoding='utf8'))
    remote=paths(local.name)[1]
    expected={}
    reports=[report]+[json.loads(path.read_text(encoding='utf8')) for path in sorted(local.glob('preparation-round[0-9]*.json'))]
    for previous in reports:
        if previous.get('run_id')!=local.name:raise ValueError('Historical QA run ID mismatch')
        for phase in PHASES:
            item=previous['phases'][phase]
            if not item['revision'].startswith(local.name+'-'):raise ValueError('Unknown historical QA revision')
            directory=(local/item['directory']).resolve()
            if not directory.is_relative_to((local/'channel/releases').resolve()):
                raise ValueError('Local release escaped QA artifacts')
            expected[item['revision']]={name:digest((directory/name).read_bytes())
                                       for name in ('desktop.zip','android.zip','manifest.json')}
    # Hashes and fixed paths are public data. A full preflight happens before deletion.
    script="""python3 - <<'PY'
import fcntl,hashlib,json
from pathlib import Path
root=Path(%r)
expected=json.loads(%r)
if root.is_symlink() or root.resolve()!=root or not str(root).startswith('/var/lib/supermono-updates/qa/'):
 raise SystemExit('Unsafe QA cleanup root')
with (root/'.publish-lock').open('a+b') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX)
 for revision,files in expected.items():
  folder=root/'releases'/revision
  if folder.is_symlink() or (folder.exists() and set(p.name for p in folder.iterdir())!=set(files)):
   raise SystemExit('Unexpected QA release files')
  for name,sha in files.items():
   p=folder/name
   if p.exists() and (p.is_symlink() or hashlib.sha256(p.read_bytes()).hexdigest()!=sha):
    raise SystemExit('QA cleanup hash mismatch')
 manifest=root/'manifest.json'
 if manifest.exists() and (manifest.is_symlink() or hashlib.sha256(manifest.read_bytes()).hexdigest() not in {d['manifest.json'] for d in expected.values()}):
  raise SystemExit('Unknown QA channel manifest')
 manifest.unlink(missing_ok=True)
 for revision,files in expected.items():
  folder=root/'releases'/revision
  for name in files:(folder/name).unlink(missing_ok=True)
  if folder.exists():folder.rmdir()
print(json.dumps({'qa_public_files_retired':True,'qa_root':str(root)}))
PY"""%(remote,json.dumps(expected,separators=(',',':')))
    before=public_download(PRODUCTION_FEED)
    sys.path.insert(0,str(ROOT/'.omx'))
    import relay_admin
    client=relay_admin.connect()
    try:remote_report=json.loads(relay_admin.run(client,script))
    finally:client.close()
    after=public_download(PRODUCTION_FEED)
    evidence={**remote_report,'production_stable_before_sha256':digest(before),
              'production_stable_after_sha256':digest(after),'production_stable_unchanged':before==after,
              'device_actions_performed':False}
    write_json(local/'cleanup.json',evidence)
    if before!=after:raise RuntimeError('Production stable changed concurrently during QA cleanup')
    print(json.dumps(evidence,indent=2))


def prepare(run_id):
    local,remote,feed=paths(run_id)
    if (local/'preparation.json').exists():
        raise ValueError('QA run already prepared; use activate/status rather than rebuilding it')
    local.mkdir(parents=True,exist_ok=False)
    stable_before=public_download(PRODUCTION_FEED)
    clone=local/'workspace'
    source_snapshot=snapshot_sources(clone)
    baseline=isolate_clone(clone,feed)
    write_json(local/'source-snapshot.json',source_snapshot)
    apk=build_apk(clone,local)
    output=local/'channel'
    trust=json.loads((ROOT/'src/supermonoagent/static/update-trust.json').read_text())
    variants={'good':baseline+b"\nwindow.SMA_QA_PHASE='good';\n",
              'throw':b"throw new Error('SMA_QA_BOOT_THROW');\n",
              'hang':b"console.warn('SMA_QA_HANG_STARTED');window.SMA_QA_HANG_STARTED=true;while(true){}\n"}
    phases={}
    for phase in PHASES:
        (clone/'android/assets/mobile/app.js').write_bytes(variants[phase])
        directory=release.package(output,root=clone,revision=run_id+'-'+phase,
                                  private_key=ROOT/'.omx/update-signing/development-private.pem',feed=feed)
        metadata=release.verify_local(directory,trust)
        assert len(metadata['artifacts']['desktop']['files'])==17
        assert len(metadata['artifacts']['android']['files'])==16
        phases[phase]={'revision':metadata['revision'],'sequence':metadata['sequence'],
                       'directory':directory.relative_to(local).as_posix(),
                       'app_js_sha256':digest(variants[phase])}
    assert phases['good']['sequence']<phases['throw']['sequence']<phases['hang']['sequence']
    report={'run_id':run_id,'qa_package':QA_PACKAGE,'qa_feed':feed,'qa_remote_root':remote,'apk':apk,
            'phases':phases,'native_api':2,'device_actions_performed':False,'production_source_mutations':False,
            'production_stable_before_sha256':digest(stable_before)}
    write_json(local/'preparation.json',report)
    publish_phase(local,'good')
    publish_phase(local,'throw',stage_only=True)
    publish_phase(local,'hang',stage_only=True)
    downloaded=public_download(feed)
    assert downloaded==(local/phases['good']['directory']/'manifest.json').read_bytes()
    for phase in PHASES:
        for platform in ('desktop','android'):
            url=feed.rsplit('/',1)[0]+'/releases/'+phases[phase]['revision']+'/'+platform+'.zip'
            assert public_download(url)==(local/phases[phase]['directory']/(platform+'.zip')).read_bytes()
    stable_after=public_download(PRODUCTION_FEED)
    report.update(public_qa_artifacts_verified=True,production_stable_after_sha256=digest(stable_after),
                  production_stable_unchanged=stable_before==stable_after)
    write_json(local/'preparation.json',report)
    if stable_before!=stable_after:raise RuntimeError('Production stable changed during preparation; inspect metadata')
    print(json.dumps(report,indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','activate','status','cleanup'))
    parser.add_argument('--run-id')
    parser.add_argument('--phase',choices=PHASES)
    args=parser.parse_args()
    run_id=args.run_id or ('qa-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+uuid.uuid4().hex[:12])
    if args.action=='prepare':prepare(run_id);return
    if not args.run_id:parser.error('--run-id is required for an existing QA run')
    local,_,feed=paths(run_id)
    if args.action=='activate':
        if not args.phase:parser.error('--phase is required')
        print(json.dumps(publish_phase(local,args.phase),indent=2));return
    if args.action=='cleanup':cleanup(local);return
    report=json.loads((local/'preparation.json').read_text(encoding='utf8'))
    current=json.loads(public_download(feed));payload=json.loads(__import__('base64').b64decode(current['payload']))
    print(json.dumps({'run_id':run_id,'qa_feed':feed,'active_revision':payload['revision'],
                      'active_sequence':payload['sequence'],'apk':report['apk'],'device_actions_performed':False},indent=2))


if __name__=='__main__':main()
