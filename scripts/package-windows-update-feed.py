"""Sign a public full-program update manifest after validating its installer ZIP."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
from supermonoagent.ui_updates import verify_signature
from ui_update_release import openssl_tool

TRUST=ROOT/'src/supermonoagent/static/update-trust.json'


def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')


def validate_archive(path):
    trust=json.loads(TRUST.read_text());total=0
    with zipfile.ZipFile(path) as archive:
        names=archive.namelist()
        if len(names)!=len(set(name.casefold() for name in names)) or len(names)>2056:raise ValueError('Duplicate or excessive installer files')
        for entry in archive.infolist():
            name=entry.filename
            if (name.startswith('/') or '\\' in name or ':' in name or any(part in {'','..','.'} for part in name.split('/'))
                    or entry.external_attr>>16&0o170000==0o120000 or entry.file_size>33554432):raise ValueError('Unsafe installer archive')
            total+=entry.file_size
        if total>268959744:raise ValueError('Installer expands beyond limits')
        if 'defaults.private.json' in names:raise ValueError('Private defaults cannot enter a public update')
        envelope=json.loads(archive.read('install-manifest.json'));raw=base64.b64decode(envelope['payload'],validate=True)
        verify_signature(base64.b64decode(trust['keys'][envelope['key_id']]),raw,base64.b64decode(envelope['signature'],validate=True))
        catalog=json.loads(raw)
        if catalog.get('schema')!=1 or catalog.get('format')!='supermono-windows-install-v1' or catalog.get('defaults_private_sha256') is not None:raise ValueError('Not a public installer catalog')
        if not re.fullmatch(r'[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}',catalog.get('version','')) or catalog.get('arch')!='x64':raise ValueError('Invalid installer version or architecture')
        expected={'install-manifest.json'}
        for section,prefix in [('files','payload/'),('bootstrap_files','')]:
            for name,item in catalog[section].items():
                name=prefix+name;data=archive.read(name);expected.add(name)
                if item!={'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}:raise ValueError('Installer file differs from signed catalog')
        if set(names)!=expected:raise ValueError('Installer ZIP contains unsigned extras')
        if set(catalog['bootstrap_files'])!={'Install.cmd','Install.ps1','Launch.ps1','Uninstall.ps1','Update.ps1'}:raise ValueError('Full updater requires a current installer')
        return catalog['version']


def package(archive,output,*,url=None,private_key=None,lifetime_days=30):
    archive=Path(archive).resolve();output=Path(output).resolve()
    if not 1<=lifetime_days<=90:raise ValueError('Invalid update manifest lifetime')
    if not 0<archive.stat().st_size<=134217728:raise ValueError('Invalid installer size')
    version=validate_archive(archive)
    url=url or '/updates/installers/'+version+'/SuperMonoAgent-'+version+'-Setup.zip'
    parsed=urlsplit(url)
    expected_path='/updates/installers/'+version+'/SuperMonoAgent-'+version+'-Setup.zip'
    if (parsed.query or parsed.fragment or parsed.username or parsed.password or not parsed.scheme and parsed.netloc
            or parsed.scheme and (parsed.scheme!='https' or parsed.hostname!='117.50.48.22' or parsed.port not in (None,443))
            or parsed.path!=expected_path):raise ValueError('Full updates must use the relay HTTPS download origin')
    now=int(time.time());sequence=time.time_ns()//1000
    if output.exists():
        old=json.loads(output.read_bytes());previous=json.loads(base64.b64decode(old['payload']));sequence=max(sequence,int(previous['sequence'])+1)
    if sequence>9007199254740991:raise ValueError('Unsafe update sequence')
    payload={'schema':1,'format':'supermono-windows-full-update-v1','channel':'stable','arch':'x64','version':version,'sequence':sequence,'created':now,'expires':now+lifetime_days*86400,'url':url,'size':archive.stat().st_size,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}
    raw=canonical(payload);key=Path(private_key or ROOT/'.omx/update-signing/development-private.pem');trust=json.loads(TRUST.read_text());key_id=next(iter(trust['keys']))
    result=subprocess.run([openssl_tool(),'dgst','-sha256','-sign',str(key)],input=raw,capture_output=True,timeout=30)
    if result.returncode:raise ValueError('Cannot sign full update feed')
    verify_signature(base64.b64decode(trust['keys'][key_id]),raw,result.stdout)
    encoded=canonical({'schema':1,'key_id':key_id,'payload':base64.b64encode(raw).decode(),'signature':base64.b64encode(result.stdout).decode()})
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent,prefix='.full-update-',delete=False) as target:
        temporary=Path(target.name);target.write(encoded)
    try:os.replace(temporary,output)
    finally:temporary.unlink(missing_ok=True)
    return payload


def main():
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--archive',type=Path,required=True);cli.add_argument('--output',type=Path,required=True)
    cli.add_argument('--url');cli.add_argument('--private-key',type=Path)
    args=cli.parse_args();result=package(args.archive,args.output,url=args.url,private_key=args.private_key)
    print(json.dumps({'version':result['version'],'sequence':result['sequence'],'output':str(args.output)}))


if __name__=='__main__':main()
