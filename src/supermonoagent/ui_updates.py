"""Signed UI-only updates. Windows verification uses CNG; native trust stays bundled."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import ssl
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener
import zipfile

TRUST_PATH = Path(__file__).with_name('static') / 'update-trust.json'
DEFAULT_FEED = 'https://117.50.48.22/updates/stable/manifest.json'
NATIVE_API = 2
MAX_MANIFEST = 128 * 1024
MAX_ARCHIVE = 8 * 1024 * 1024
MAX_SNAPSHOT = 5 * 1024 * 1024
MAX_FILE = 4 * 1024 * 1024
FILES = {
    'desktop': frozenset(('workspace.html','workspace.js','workspace.css','terminal-client.js','tab-themes.js','tab-themes.css','xterm.js','xterm.css','xterm-fit.js','xterm-unicode11.js','xterm-webgl.js','agent-modes/codex.json','agent-modes/claude-code.json','agent-modes/grok.json','agent-modes/kimi.json','agent-modes/pi-dev.json','agent-modes/cursor-cli.json')),
    'android': frozenset(('mobile/index.html','mobile/app.js','mobile/app.css','mobile/mobile-core.js','mobile/vendor/xterm.js','mobile/vendor/xterm.css','mobile/vendor/xterm-fit.js','mobile/vendor/xterm-unicode11.js','mobile/vendor/xterm-webgl.js','mobile/vendor/terminal-client.js','mobile/vendor/file-client.js','mobile/vendor/file-upload.js','mobile/agent-modes/codex.json','mobile/agent-modes/claude-code.json','mobile/agent-modes/grok.json','mobile/agent-modes/kimi.json','mobile/agent-modes/pi-dev.json','mobile/agent-modes/cursor-cli.json')),
}
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


class UpdateError(ValueError):
    pass


class UpdateIncompatible(UpdateError):
    pass


def _json(raw):
    def unique(pairs):
        result = {}
        for key,value in pairs:
            if key in result:
                raise UpdateError('Duplicate JSON key')
            result[key] = value
        return result
    try:
        return json.loads(raw.decode('utf-8') if isinstance(raw,bytes) else raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(UpdateError('Nonfinite JSON number')))
    except (ValueError, UnicodeError, TypeError, RecursionError) as exc:
        raise UpdateError('Invalid JSON') from exc


def _b64(value, limit):
    if not isinstance(value,str) or len(value) > limit * 2:
        raise UpdateError('Invalid base64 field')
    try:
        result = base64.b64decode(value,validate=True)
    except (ValueError,TypeError) as exc:
        raise UpdateError('Invalid base64 field') from exc
    if len(result)>limit:
        raise UpdateError('Oversized base64 field')
    return result


def _der_item(data, offset, tag):
    if offset+2>len(data) or data[offset]!=tag:
        raise UpdateError('Invalid RSA public key')
    length=data[offset+1]; start=offset+2
    if length & 128:
        count=length & 127
        if not 1<=count<=4 or start+count>len(data):
            raise UpdateError('Invalid DER length')
        length=int.from_bytes(data[start:start+count],'big'); start+=count
    end=start+length
    if end>len(data):
        raise UpdateError('Truncated DER')
    return data[start:end],end


def _rsa_blob(spki):
    outer,end=_der_item(spki,0,0x30)
    if end!=len(spki):
        raise UpdateError('Trailing RSA key bytes')
    algorithm,pos=_der_item(outer,0,0x30)
    if algorithm!=bytes.fromhex('06092a864886f70d0101010500'):
        raise UpdateError('RSA encryption SPKI required')
    bits,end=_der_item(outer,pos,3)
    if end!=len(outer) or not bits or bits[0]!=0:
        raise UpdateError('Invalid RSA bit string')
    public,end=_der_item(bits,1,0x30)
    if end!=len(bits):
        raise UpdateError('Invalid RSA sequence')
    modulus,pos=_der_item(public,0,2)
    exponent,end=_der_item(public,pos,2)
    if end!=len(public) or not modulus or not exponent or modulus[0]&128 or exponent[0]&128:
        raise UpdateError('Invalid RSA integer')
    modulus=modulus.lstrip(b'\0'); exponent=exponent.lstrip(b'\0')
    if not 384<=len(modulus)<=1024 or int.from_bytes(modulus,'big').bit_length()<3072 or not 1<=len(exponent)<=8:
        raise UpdateError('RSA key must contain at least 3072 bits')
    return struct.pack('<6I',0x31415352,len(modulus)*8,len(exponent),len(modulus),0,0)+exponent+modulus


def verify_signature(public_der, payload, signature):
    blob=_rsa_blob(public_der)
    if sys.platform != 'win32':
        openssl=shutil.which('openssl')
        if not openssl:
            raise UpdateError('OpenSSL required for non-Windows verification')
        with tempfile.TemporaryDirectory(prefix='sma-public-verify-') as directory:
            directory=Path(directory)
            (directory/'public.der').write_bytes(public_der)
            (directory/'signature').write_bytes(signature)
            result=subprocess.run([openssl,'dgst','-sha256','-verify',str(directory/'public.der'),'-keyform','DER','-signature',str(directory/'signature')],input=payload,capture_output=True,timeout=10)
            if result.returncode:
                raise UpdateError('Invalid update signature')
        return
    # API contracts: https://learn.microsoft.com/windows/win32/api/bcrypt/nf-bcrypt-bcryptimportkeypair
    # https://learn.microsoft.com/windows/win32/api/bcrypt/nf-bcrypt-bcryptverifysignature
    dll=ctypes.WinDLL('bcrypt.dll')
    pointer=ctypes.c_void_p; uint=ctypes.c_ulong; status=ctypes.c_long
    dll.BCryptOpenAlgorithmProvider.argtypes=[ctypes.POINTER(pointer),ctypes.c_wchar_p,ctypes.c_wchar_p,uint]
    dll.BCryptOpenAlgorithmProvider.restype=status
    dll.BCryptImportKeyPair.argtypes=[pointer,pointer,ctypes.c_wchar_p,ctypes.POINTER(pointer),pointer,uint,uint]
    dll.BCryptImportKeyPair.restype=status
    dll.BCryptVerifySignature.argtypes=[pointer,pointer,pointer,uint,pointer,uint,uint]
    dll.BCryptVerifySignature.restype=status
    dll.BCryptDestroyKey.argtypes=[pointer]; dll.BCryptDestroyKey.restype=status
    dll.BCryptCloseAlgorithmProvider.argtypes=[pointer,uint]; dll.BCryptCloseAlgorithmProvider.restype=status
    class Padding(ctypes.Structure):
        _fields_=[('pszAlgId',ctypes.c_wchar_p)]
    algorithm=pointer(); key=pointer()
    try:
        if dll.BCryptOpenAlgorithmProvider(ctypes.byref(algorithm),'RSA',None,0):
            raise UpdateError('Windows RSA provider unavailable')
        key_bytes=ctypes.create_string_buffer(blob)
        if dll.BCryptImportKeyPair(algorithm,None,'RSAPUBLICBLOB',ctypes.byref(key),key_bytes,len(blob),0):
            raise UpdateError('Windows RSA key import failed')
        digest=hashlib.sha256(payload).digest()
        hash_bytes=ctypes.create_string_buffer(digest); sig_bytes=ctypes.create_string_buffer(signature)
        padding=Padding('SHA256')
        if dll.BCryptVerifySignature(key,ctypes.byref(padding),hash_bytes,len(digest),sig_bytes,len(signature),2):
            raise UpdateError('Invalid update signature')
    finally:
        if key.value:
            dll.BCryptDestroyKey(key)
        if algorithm.value:
            dll.BCryptCloseAlgorithmProvider(algorithm,0)


def _integer(value,minimum=0):
    return type(value) is int and minimum<=value<=2**53-1


def _revision(value):
    return isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}',value) and '..' not in value and value!='bundled'


def _catalog(files,platform):
    if platform not in FILES or not isinstance(files,dict) or set(files)!=FILES[platform]:
        raise UpdateError('Snapshot must contain exactly the platform UI files')
    total=0
    for value in files.values():
        if not isinstance(value,dict) or not _integer(value.get('size')) or value['size']>MAX_FILE or not isinstance(value.get('sha256'),str) or not re.fullmatch(r'[0-9a-f]{64}',value['sha256']):
            raise UpdateError('Invalid file hash or size')
        total+=value['size']
    if total>MAX_SNAPSHOT:
        raise UpdateError('Oversized UI snapshot')


def _verify_envelope(raw,trust,platform,native_api,now,check_time=True):
    if not isinstance(raw,bytes) or len(raw)>MAX_MANIFEST:
        raise UpdateError('Oversized or invalid update envelope')
    envelope=_json(raw)
    if not isinstance(envelope,dict) or envelope.get('schema')!=1 or type(envelope.get('schema')) is not int:
        raise UpdateError('Unsupported update envelope')
    if not isinstance(trust,dict) or trust.get('schema')!=1 or type(trust.get('schema')) is not int or not isinstance(trust.get('keys'),dict):
        raise UpdateError('Invalid bundled trust anchor')
    key_id=envelope.get('key_id')
    if not isinstance(key_id,str) or key_id not in trust['keys']:
        raise UpdateError('Untrusted update signing key')
    key=_b64(trust['keys'][key_id],2048)
    if hashlib.sha256(key).hexdigest()[:16]!=key_id:
        raise UpdateError('Signing key identifier mismatch')
    payload_bytes=_b64(envelope.get('payload'),MAX_MANIFEST)
    verify_signature(key,payload_bytes,_b64(envelope.get('signature'),1024))
    payload=_json(payload_bytes)
    if not isinstance(payload,dict) or type(payload.get('schema')) is not int or payload['schema']!=1 or payload.get('channel')!='stable' or not _revision(payload.get('revision')) or not _integer(payload.get('sequence'),1):
        raise UpdateError('Invalid update identity')
    created,expires=payload.get('created'),payload.get('expires')
    if not _integer(created) or not _integer(expires) or expires<=created:
        raise UpdateError('Invalid update validity window')
    if check_time and (created>now+300 or expires<=now):
        raise UpdateError('Update is expired or issued in the future')
    artifacts=payload.get('artifacts')
    if not isinstance(artifacts,dict) or platform not in artifacts:
        raise UpdateError('Missing platform artifact')
    artifact=artifacts[platform]
    if not isinstance(artifact,dict) or not _integer(artifact.get('native_api'),1):
        raise UpdateError('Invalid native update API')
    if artifact['native_api']!=native_api:
        raise UpdateIncompatible('This update requires a different native shell')
    if not _integer(artifact.get('size'),1) or artifact['size']>MAX_ARCHIVE or not isinstance(artifact.get('sha256'),str) or not re.fullmatch(r'[0-9a-f]{64}',artifact['sha256']):
        raise UpdateError('Invalid artifact hash or size')
    if not isinstance(artifact.get('url'),str) or not artifact['url'] or len(artifact['url'])>2048:
        raise UpdateError('Invalid artifact URL')
    _catalog(artifact.get('files'),platform)
    return payload


def verify_envelope(raw,trust,platform,native_api=NATIVE_API,now=None):
    return _verify_envelope(raw,trust,platform,native_api,time.time() if now is None else now)


def _origin(url):
    parsed=urlsplit(url)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise UpdateError('Update downloads require credential-free HTTPS')
    return parsed.scheme,parsed.hostname.lower(),parsed.port or 443


def _download(url,limit,origin):
    if _origin(url)!=origin:
        raise UpdateError('Cross-origin update download refused')
    class Redirect(HTTPRedirectHandler):
        def redirect_request(self,request,fp,code,msg,headers,newurl):
            if _origin(newurl)!=origin:
                raise UpdateError('Cross-origin update redirect refused')
            return super().redirect_request(request,fp,code,msg,headers,newurl)
    opener=build_opener(ProxyHandler({}),HTTPSHandler(context=ssl.create_default_context()),Redirect())
    request=Request(url,headers={'User-Agent':'SuperMonoAgent-UI-Updater/1','Cache-Control':'no-cache'})
    deadline=time.monotonic()+20
    with opener.open(request,timeout=10) as response:
        if _origin(response.geturl())!=origin:
            raise UpdateError('Cross-origin update response refused')
        chunks=[]; total=0
        while True:
            chunk=response.read1(min(65536,limit+1-total))
            if time.monotonic()>deadline:
                raise UpdateError('Update download deadline expired')
            total+=len(chunk)
            if total>limit:
                raise UpdateError('Oversized update download')
            if not chunk:
                return b''.join(chunks)
            chunks.append(chunk)


def fetch_manifest(url=DEFAULT_FEED):
    return _download(url,MAX_MANIFEST,_origin(url))


def fetch_archive(payload,manifest_url,platform):
    artifact=payload['artifacts'][platform]
    url=urljoin(manifest_url,artifact['url'])
    data=_download(url,min(MAX_ARCHIVE,artifact['size']),_origin(manifest_url))
    if len(data)!=artifact['size'] or hashlib.sha256(data).hexdigest()!=artifact['sha256']:
        raise UpdateError('Artifact download hash mismatch')
    return data


def verify_archive(payload,archive_bytes,platform):
    return _archive(archive_bytes,payload["artifacts"][platform],platform)


def _archive(data,artifact,platform):
    if not isinstance(data,bytes) or len(data)!=artifact['size'] or hashlib.sha256(data).hexdigest()!=artifact['sha256']:
        raise UpdateError('Artifact archive hash mismatch')
    result={}; seen=set(); total=0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries=archive.infolist()
            if len(entries)!=len(FILES[platform]):
                raise UpdateError('Unexpected archive entry count')
            for entry in entries:
                name=entry.filename
                if name not in FILES[platform] or entry.orig_filename!=name or name.casefold() in seen or '\\' in name or stat.S_ISLNK(entry.external_attr>>16) or entry.flag_bits & 1 or entry.file_size>MAX_FILE:
                    raise UpdateError('Unsafe or duplicate archive entry')
                seen.add(name.casefold()); total+=entry.file_size
                if total>MAX_SNAPSHOT:
                    raise UpdateError('Oversized expanded archive')
                expected=artifact['files'][name]
                if entry.file_size!=expected['size']:
                    raise UpdateError('Archive file size mismatch')
                with archive.open(entry) as source:
                    content=source.read(MAX_FILE+1)
                if len(content)!=expected['size'] or hashlib.sha256(content).hexdigest()!=expected['sha256']:
                    raise UpdateError('Archive file hash mismatch')
                result[name]=content
    except (zipfile.BadZipFile,RuntimeError,OSError,NotImplementedError) as exc:
        raise UpdateError('Invalid UI archive') from exc
    _catalog({name:{'sha256':hashlib.sha256(value).hexdigest(),'size':len(value)} for name,value in result.items()},platform)
    return result


def _process_identity(pid):
    if sys.platform=='win32':
        kernel=ctypes.WinDLL('kernel32.dll',use_last_error=True)
        kernel.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong]; kernel.OpenProcess.restype=ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_ulong]; kernel.WaitForSingleObject.restype=ctypes.c_ulong
        kernel.GetProcessTimes.argtypes=[ctypes.c_void_p]+[ctypes.c_void_p]*4; kernel.GetProcessTimes.restype=ctypes.c_int
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]; kernel.CloseHandle.restype=ctypes.c_int
        handle=kernel.OpenProcess(0x101000,False,pid)  # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            if kernel.WaitForSingleObject(handle,0)!=0x102:
                return None
            values=[ctypes.c_ulonglong() for _ in range(4)]
            if not kernel.GetProcessTimes(handle,*[ctypes.byref(value) for value in values]):
                return None
            return {'pid':pid,'created':values[0].value}
        finally:
            kernel.CloseHandle(handle)
    try:
        fields=Path('/proc')/str(pid)/'stat'
        parts=fields.read_text().rsplit(')',1)[1].split()
        if parts[0]=='Z':
            return None
        value=parts[19]
        return {'pid':pid,'created':value}
    except (OSError,IndexError):
        return None


class UpdateStore:
    def __init__(self,root,platform,bundled_files,trust=None,native_api=NATIVE_API):
        self.root=Path(root).resolve(); self.root.mkdir(parents=True,exist_ok=True)
        self.platform=platform; self.native_api=native_api
        self.bundled_files=dict(bundled_files)
        _catalog({name:{'sha256':hashlib.sha256(value).hexdigest(),'size':len(value)} for name,value in self.bundled_files.items()},platform)
        self.trust=_json(TRUST_PATH.read_bytes()) if trust is None else trust
        with _LOCKS_GUARD:
            self._thread_lock=_LOCKS.setdefault(str(self.root).casefold(),threading.RLock())
        (self.root/'snapshots').mkdir(exist_ok=True)
        (self.root/'envelopes').mkdir(exist_ok=True)
        with self._locked():
            if not (self.root/'state.json').exists():
                self._save({'active_revision':'bundled','active_sequence':0,'previous_revision':None,'previous_sequence':0,'highest_sequence':0,'failed_sequences':[],'pending':None,'bundled_revision':'bundled','observed_hash':None,'generation':0})

    @contextmanager
    def _locked(self):
        with self._thread_lock,open(self.root/'store.lock','a+b') as handle:
            # Windows can lock a byte beyond EOF. Never initialize it before
            # locking: another process may already hold that empty-file range.
            handle.seek(0)
            if os.name=='nt':
                import msvcrt
                deadline=time.monotonic()+10
                while True:
                    try:
                        msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1); break
                    except OSError:
                        if time.monotonic()>=deadline:
                            raise UpdateError('Update store is busy')
                        time.sleep(.01)
            else:
                import fcntl
                fcntl.flock(handle,fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name=='nt':
                    msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    fcntl.flock(handle,fcntl.LOCK_UN)

    def _state(self):
        state=_json((self.root/'state.json').read_bytes())
        if not isinstance(state,dict) or not _integer(state.get('highest_sequence')):
            raise UpdateError('Invalid update state')
        return state

    def _atomic(self,path,data):
        temporary=path.with_name(path.name+'.'+secrets.token_hex(8)+'.tmp')
        try:
            with open(temporary,'xb') as output:
                output.write(data); output.flush(); os.fsync(output.fileno())
            os.replace(temporary,path)
        finally:
            temporary.unlink(missing_ok=True)

    def _save(self,state):
        self._atomic(self.root/'state.json',json.dumps(state,sort_keys=True).encode())

    def status(self):
        with self._locked():
            return self._state()

    def inherit_floor(self,sources,*,bundle_fingerprint=None):
        """Merge trusted local observation floors without changing UI pointers or boot state."""
        with self._locked():
            state=self._state()
            floors=[state,*sources]
            hashes={}; failed=set(); highest=0
            for floor in floors:
                if not isinstance(floor,dict) or not _integer(floor.get('highest_sequence')):
                    raise UpdateError('Invalid inherited update sequence')
                sequence=floor['highest_sequence']; digest=floor.get('observed_hash')
                if (sequence and (not isinstance(digest,str) or not re.fullmatch(r'[0-9a-f]{64}',digest))) or (not sequence and digest is not None):
                    raise UpdateError('Invalid inherited update hash')
                quarantined=floor.get('failed_sequences')
                if not isinstance(quarantined,list) or any(not _integer(value,1) or value>sequence for value in quarantined):
                    raise UpdateError('Invalid inherited update quarantine')
                if sequence in hashes and hashes[sequence]!=digest:
                    raise UpdateError('Conflicting inherited update sequence')
                hashes[sequence]=digest; highest=max(highest,sequence); failed.update(quarantined)
            updated=dict(state)
            updated.update(highest_sequence=highest,observed_hash=hashes[highest],failed_sequences=sorted(failed))
            if bundle_fingerprint is not None:
                if not isinstance(bundle_fingerprint,str) or not re.fullmatch(r'[0-9a-f]{64}',bundle_fingerprint):
                    raise UpdateError('Invalid native bundle fingerprint')
                if state.get('bundle_fingerprint',bundle_fingerprint)!=bundle_fingerprint:
                    raise UpdateError('Native bundle namespace mismatch')
                updated.setdefault('bundle_fingerprint',bundle_fingerprint)
                updated.setdefault('native_bundle_floor',highest)
                updated.setdefault('native_bundle_migrated_at',int(time.time()))
            if updated!=state:
                self._save(updated)
            return updated

    def _observe(self,state,raw,payload):
        sequence=payload['sequence']; digest=hashlib.sha256(_b64(_json(raw)['payload'],MAX_MANIFEST)).hexdigest()
        if sequence<state['highest_sequence'] or sequence in state['failed_sequences']:
            raise UpdateError('Update is old or quarantined')
        if sequence==state['highest_sequence'] and state['observed_hash'] not in {None,digest}:
            raise UpdateError('Conflicting update sequence')
        state['highest_sequence']=sequence; state['observed_hash']=digest
        self._save(state)

    def observe(self,envelope_bytes):
        payload=verify_envelope(envelope_bytes,self.trust,self.platform,self.native_api)
        with self._locked():
            self._observe(self._state(),envelope_bytes,payload)
        return payload

    def _write_snapshot(self,revision,files):
        destination=self.root/'snapshots'/revision
        if destination.exists():
            if {path.relative_to(destination).as_posix():path.read_bytes() for path in destination.rglob('*') if path.is_file()}!=files:
                raise UpdateError('Immutable snapshot revision collision')
            return
        temporary=self.root/'snapshots'/('stage-'+secrets.token_hex(12))
        temporary.mkdir()
        try:
            for name,content in files.items():
                path=temporary/name; path.parent.mkdir(parents=True,exist_ok=True)
                with open(path,'xb') as output:
                    output.write(content); output.flush(); os.fsync(output.fileno())
            os.replace(temporary,destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def install(self,envelope_bytes,archive_bytes):
        payload=verify_envelope(envelope_bytes,self.trust,self.platform,self.native_api)
        with self._locked():
            state=self._state()
            self._observe(state,envelope_bytes,payload)
            files=_archive(archive_bytes,payload['artifacts'][self.platform],self.platform)
            if payload['sequence']==state['active_sequence'] and payload['revision']==state['active_revision']:
                return state
            if state['pending']:
                raise UpdateError('An update is awaiting startup confirmation')
            if not (self.root/'snapshots'/'bundled').exists():
                self._write_snapshot('bundled',self.bundled_files)
            envelope_path=self.root/'envelopes'/(payload['revision']+'.json')
            if envelope_path.exists() and envelope_path.read_bytes()!=envelope_bytes:
                raise UpdateError('Immutable revision envelope collision')
            self._write_snapshot(payload['revision'],files)
            if not envelope_path.exists():
                self._atomic(envelope_path,envelope_bytes)
            state['generation']=state.get('generation',0)+1
            state.update(previous_revision=state['active_revision'],previous_sequence=state['active_sequence'],active_revision=payload['revision'],active_sequence=payload['sequence'],pending={'ticket':None,'revision':payload['revision'],'sequence':payload['sequence'],'deadline':time.time()+20,'generation':state['generation'],'owner':_process_identity(os.getpid())})
            self._save(state)
            return state

    def _load_snapshot(self,revision):
        if revision!='bundled' and not _revision(revision):
            raise UpdateError('Invalid snapshot revision')
        directory=self.root/'snapshots'/revision
        if revision=='bundled':
            if not directory.exists():
                return dict(self.bundled_files)
            try:
                if any((directory/name).stat().st_size!=len(self.bundled_files[name]) for name in FILES[self.platform]):
                    return dict(self.bundled_files)
                saved={name:(directory/name).read_bytes() for name in FILES[self.platform]}
                # The bootstrap's immutable assets remain the fallback trust boundary.
                return saved if saved==self.bundled_files else dict(self.bundled_files)
            except OSError:
                return dict(self.bundled_files)
        payload=_verify_envelope((self.root/'envelopes'/(revision+'.json')).read_bytes(),self.trust,self.platform,self.native_api,0,False)
        if payload['revision']!=revision:
            raise UpdateError('Snapshot envelope revision mismatch')
        catalog=payload['artifacts'][self.platform]['files']
        result={}
        for name in FILES[self.platform]:
            path=directory/name
            if path.stat().st_size!=catalog[name]['size']:
                raise UpdateError('Stored snapshot size failure')
            value=path.read_bytes()
            if len(value)!=catalog[name]['size'] or hashlib.sha256(value).hexdigest()!=catalog[name]['sha256']:
                raise UpdateError('Stored snapshot integrity failure')
            result[name]=value
        return result

    def _recover_integrity(self,state):
        try:
            self._load_snapshot(state['active_revision'])
        except (OSError,UpdateError):
            self._rollback(state,'snapshot_integrity_failure')
            try:
                self._load_snapshot(state['active_revision'])
            except (OSError,UpdateError):
                self._rollback(state,'snapshot_integrity_failure')
        previous=state['previous_revision']
        if previous:
            try:
                self._load_snapshot(previous)
            except (OSError,UpdateError):
                state.update(previous_revision='bundled',previous_sequence=0)
                self._save(state)
        return state

    def snapshot(self,revision=None):
        with self._locked():
            if revision is None:
                state=self._recover_integrity(self._state())
                revision=state['active_revision']
            return self._load_snapshot(revision)

    def _rollback(self,state,reason):
        if state['active_sequence']:
            state['failed_sequences']=sorted(set(state['failed_sequences']+[state['active_sequence']]))
        state.update(active_revision=state['previous_revision'] or 'bundled',active_sequence=state['previous_sequence'],previous_revision=None,previous_sequence=0,pending=None,failure_reason=reason)
        self._save(state)
        return state

    def begin_boot(self):
        with self._locked():
            state=self._state(); pending=state['pending']
            if not pending:
                return None
            if pending['deadline'] is not None and pending['deadline']<=time.time():
                self._rollback(state,'startup_timeout'); self._recover_integrity(state); return None
            if pending['ticket'] is None:
                pending.update(ticket=secrets.token_urlsafe(32),deadline=pending['deadline'] or time.time()+20,owner=_process_identity(os.getpid()))
                self._save(state)
            return dict(pending)

    def acknowledge(self,ticket):
        with self._locked():
            state=self._state(); pending=state['pending']
            if not pending or pending['revision']!=state['active_revision'] or pending['sequence']!=state['active_sequence'] or pending['generation']!=state['generation'] or not secrets.compare_digest(str(pending['ticket']),str(ticket)) or pending['ticket'] is None:
                raise UpdateError('Invalid startup ticket')
            if pending['deadline']<=time.time():
                self._rollback(state,'startup_timeout'); self._recover_integrity(state)
                raise UpdateError('Startup deadline expired')
            try:
                self._load_snapshot(state['active_revision'])
            except (OSError,UpdateError) as exc:
                self._recover_integrity(state)
                raise UpdateError('Cannot commit damaged UI snapshot') from exc
            state['pending']=None; self._save(state); return state

    def fail(self,ticket,reason='startup_failed'):
        with self._locked():
            state=self._state(); pending=state['pending']
            if not pending or pending['ticket'] is None or not secrets.compare_digest(pending['ticket'],str(ticket)):
                raise UpdateError('Invalid startup ticket')
            return self._recover_integrity(self._rollback(state,'startup_failed'))

    def rollback(self):
        with self._locked():
            return self._recover_integrity(self._rollback(self._state(),'manual_rollback'))

    def recover_pending(self):
        with self._locked():
            state=self._state()
            pending=state['pending']
            if pending and (pending['deadline'] is None or pending['deadline']<=time.time() or pending.get('owner') is None or _process_identity(pending['owner']['pid'])!=pending['owner']):
                self._rollback(state,'restart_before_ready')
            return self._recover_integrity(state)
