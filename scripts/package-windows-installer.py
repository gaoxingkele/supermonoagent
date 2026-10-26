"""Build a signed-catalog Windows setup ZIP; personal defaults stay private."""
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
from urllib.parse import urlsplit
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
from supermonoagent.token_config import read_default_token
from supermonoagent.ui_updates import verify_signature
from ui_update_release import openssl_tool

BOOTSTRAP=('Install.cmd','Install.ps1','Launch.ps1','Uninstall.ps1','Update.ps1')
TRUST_PATH=ROOT/'src/supermonoagent/static/update-trust.json'
PRIVATE_NAMES={'token','token.txt','token-source.json','relay-code','relay.txt','relays.json','accounts.json','desktop.json','machine.json','secure-relay.json','relay-bindings.json','.env','id_rsa','id_ed25519'}


def digest(data):return hashlib.sha256(data).hexdigest()


def _json(data):return json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')


def _signed(payload,private_key):
    trust=json.loads(TRUST_PATH.read_text(encoding='utf-8'));key_id=next(iter(trust['keys']))
    raw=_json(payload)
    result=subprocess.run([openssl_tool(), 'dgst','-sha256','-sign',str(private_key)],input=raw,capture_output=True,timeout=30)
    if result.returncode:raise ValueError('Cannot sign installer catalog')
    verify_signature(base64.b64decode(trust['keys'][key_id]),raw,result.stdout)
    return _json({'schema':1,'key_id':key_id,'payload':base64.b64encode(raw).decode(),'signature':base64.b64encode(result.stdout).decode()})


def _unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('Invalid file exchange profile')
        result[key]=value
    return result


def _file_exchange_profile(value):
    try:
        if isinstance(value,dict):profile=dict(value)
        else:
            with Path(value).open('rb') as source:raw=source.read(32769)
            if len(raw)>32768:raise ValueError()
            profile=json.loads(raw.decode('utf-8-sig'),object_pairs_hook=_unique)
        required={'schema','broker_url','enrollment_key'}
        if (not isinstance(profile,dict) or not required<=set(profile) or set(profile)-required-{'broker_token'}
                or type(profile['schema']) is not int or profile['schema']!=1):raise ValueError()
        url=profile['broker_url']
        if (not isinstance(url,str) or not 1<=len(url)<=2048 or not url.isascii()
                or any(ord(c)<33 or ord(c)>126 for c in url) or '%' in url or '\\' in url):raise ValueError()
        parsed=urlsplit(url)
        if (parsed.scheme!='wss' or not parsed.hostname or parsed.username or parsed.password
                or parsed.path!='/file/ws' or parsed.query or parsed.fragment
                or parsed.port is not None and not 1<=parsed.port<=65535):raise ValueError()
        key=profile['enrollment_key']
        if not isinstance(key,str) or not 32<=len(key)<=8192 or any(not 33<=ord(c)<=126 for c in key):raise ValueError()
        if 'broker_token' in profile:
            token=profile['broker_token']
            if not isinstance(token,str) or not 1<=len(token)<=4096 or any(not 33<=ord(c)<=126 for c in token):raise ValueError()
        return profile
    except (OSError,TypeError,ValueError,UnicodeError):
        raise ValueError('Invalid file exchange profile') from None


def build(runtime,output,*,templates=None,token_file=None,private_key=None,file_exchange_profile=None):
    runtime=Path(runtime).resolve();output=Path(output).resolve()
    templates=Path(templates or ROOT/'scripts/windows-installer').resolve()
    private_key=Path(private_key or ROOT/'.omx/update-signing/development-private.pem').resolve()
    if file_exchange_profile is not None and token_file is None:
        raise ValueError('File exchange configuration requires a private personalized installer')
    if token_file is not None and not output.is_relative_to((ROOT/'.omx').resolve()):
        raise ValueError('Personal installer archives must remain under the private .omx directory')
    if (file_exchange_profile is not None and not isinstance(file_exchange_profile,dict)
            and Path(file_exchange_profile).resolve().is_relative_to(runtime)):
        raise ValueError('Private configuration cannot enter an installer runtime')
    exchange=_file_exchange_profile(file_exchange_profile) if file_exchange_profile is not None else None
    version=(runtime/'version.txt').read_text(encoding='utf-8-sig').strip()
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',version):raise ValueError('Invalid numeric installer version')
    manifest=json.loads((runtime/'build-manifest.json').read_text(encoding='utf-8-sig'))
    if manifest.get('version')!=version:raise ValueError('Runtime version differs from its build manifest')
    entry='SuperMonoAgent/SuperMonoAgent.exe'
    if digest((runtime/entry).read_bytes())!=manifest.get('exe_sha256'):raise ValueError('Runtime executable checksum differs')
    files={};catalog={};seen=set()
    for path in sorted(runtime.rglob('*')):
        if path.is_symlink() or getattr(path,'is_junction',lambda:False)():raise ValueError('Installer runtime cannot contain links')
        if not path.is_file():continue
        name=path.relative_to(runtime).as_posix()
        if (path.name.lower() in PRIVATE_NAMES or any(part.lower() in {'.omx','trial-state','logs','__pycache__','file-exchange'} for part in path.relative_to(runtime).parts)
                or path.suffix.lower() in {'.key','.pfx','.keystore','.jks','.log','.lock'} or 'private' in path.name.lower()):
            raise ValueError('Private configuration or runtime state cannot enter an installer')
        if name.casefold() in seen:raise ValueError('Installer contains case-colliding paths')
        seen.add(name.casefold());data=path.read_bytes()
        files['payload/'+name]=data;catalog[name]={'size':len(data),'sha256':digest(data)}
    if len(catalog)>2048 or sum(value['size'] for value in catalog.values())>268435456:raise ValueError('Installer payload exceeds supported bounds')
    bootstrap={}
    for name in BOOTSTRAP:
        data=(templates/name).read_bytes();files[name]=data;bootstrap[name]={'size':len(data),'sha256':digest(data)}
    defaults_hash=None
    if token_file is not None:
        token=read_default_token(token_file)
        if not token:raise ValueError('Private default Token is unavailable')
        profile={'relay':'wss://117.50.48.22/ws','token':token,'room_mode':'computer-name'}
        if exchange is not None:profile['file_exchange']=exchange
        defaults=_json(profile)
        if len(defaults)>65536:raise ValueError('Private defaults exceed supported bounds')
        files['defaults.private.json']=defaults;defaults_hash=digest(defaults)
    payload={'schema':1,'format':'supermono-windows-install-v1','version':version,'arch':'x64','entry':entry,
             'relay':'wss://117.50.48.22/ws','files':catalog,'bootstrap_files':bootstrap,'defaults_private_sha256':defaults_hash}
    files['install-manifest.json']=_signed(payload,private_key)
    output.parent.mkdir(parents=True,exist_ok=True)
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent,prefix='.installer-',suffix='.tmp',delete=False) as stream:
            temporary=Path(stream.name)
        with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
            for name,data in sorted(files.items()):
                info=zipfile.ZipInfo(name,date_time=(2026,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
                info.external_attr=0o100644<<16;archive.writestr(info,data)
        raw=temporary.read_bytes()
        if len(raw)>134217728:raise ValueError('Installer ZIP exceeds download bounds')
        if output.exists():
            if output.read_bytes()!=raw:raise ValueError('An immutable installer archive already exists with different content')
        else:os.replace(temporary,output);temporary=None
        result={'version':version,'archive':str(output),'size':len(raw),'sha256':digest(raw),'files':len(files),'personalized':token_file is not None}
        if token_file is None:output.with_suffix(output.suffix+'.sha256').write_text(result['sha256']+'  '+output.name+'\n',encoding='ascii')
        return result
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def main():
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--runtime',type=Path,required=True)
    cli.add_argument('--output',type=Path,required=True)
    cli.add_argument('--token-file',type=Path)
    cli.add_argument('--file-exchange-profile',type=Path,help='Private enrollment profile; requires --token-file and private .omx output')
    cli.add_argument('--private-key',type=Path)
    args=cli.parse_args()
    print(json.dumps(build(args.runtime,args.output,token_file=args.token_file,private_key=args.private_key,
                           file_exchange_profile=args.file_exchange_profile)))


if __name__=='__main__':main()
