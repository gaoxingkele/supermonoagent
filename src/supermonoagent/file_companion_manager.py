"""Private, opt-in bootstrap for the independent host file companion.

No daemon operation or clipboard access. Public configuration never contains
credentials or private file paths; a live helper is left running after GUI exit.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time

from .file_companion import _endpoint, _process_birth, CompanionError
from .file_broker import PROTOCOL
from .file_broker import FILE_PROTOCOL
from .file_workspaces import _document, _unique, _directory, MAX_SETTINGS_BYTES

_LOCKS, _ATTEMPTS = {}, {}
_GUARD = threading.Lock()


class ManagerError(ValueError):
    pass


def _present(path):
    try: return path.lstat()
    except FileNotFoundError: return None


def _no_links(path):
    for parent in (*reversed(path.parents), path):
        info = _present(parent)
        if info is not None and (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise ManagerError('unsafe_state')


def _win_functions():
    from ctypes import wintypes as W
    security = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = W.HANDLE
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [W.HANDLE]
    security.OpenProcessToken.argtypes = [W.HANDLE, W.DWORD, ctypes.POINTER(W.HANDLE)]
    security.GetTokenInformation.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD)]
    security.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(W.LPWSTR)]
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [W.LPCWSTR, W.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(W.DWORD)]
    security.SetFileSecurityW.argtypes = [W.LPCWSTR, W.DWORD, ctypes.c_void_p]
    security.GetNamedSecurityInfoW.argtypes = [W.LPWSTR, W.DWORD, W.DWORD, *([ctypes.POINTER(ctypes.c_void_p)]*5)]
    security.GetNamedSecurityInfoW.restype = W.DWORD
    security.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, W.DWORD, ctypes.c_int]
    security.GetAce.argtypes = [ctypes.c_void_p, W.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    return security, kernel, W


def _sid_text(pointer, security, kernel, W):
    output = W.LPWSTR()
    if not security.ConvertSidToStringSidW(pointer, ctypes.byref(output)):
        raise ManagerError('unsafe_state')
    try: return output.value
    finally: kernel.LocalFree(ctypes.cast(output, ctypes.c_void_p))


@lru_cache(maxsize=1)
def _user_sid():
    security, kernel, W = _win_functions()
    handle, length = W.HANDLE(), W.DWORD()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(handle)):
        raise ManagerError('unsafe_state')
    try:
        security.GetTokenInformation(handle, 1, None, 0, ctypes.byref(length))
        data = ctypes.create_string_buffer(length.value)
        if not security.GetTokenInformation(handle, 1, data, length, ctypes.byref(length)):
            raise ManagerError('unsafe_state')
        return _sid_text(ctypes.c_void_p.from_buffer(data).value, security, kernel, W)
    finally: kernel.CloseHandle(handle)


def _secure(path, directory=False):
    if os.name != 'nt':
        os.chmod(path, 0o700 if directory else 0o600)
        return
    security, kernel, W = _win_functions()
    sid = _user_sid()
    inheritance = 'OICI' if directory else ''
    descriptor = ctypes.c_void_p()
    sddl = 'D:P(A;'+inheritance+';FA;;;'+sid+')(A;'+inheritance+';FA;;;SY)'
    if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ManagerError('unsafe_state')
    try:
        if not security.SetFileSecurityW(str(path), 4 | 0x80000000, descriptor):
            raise ManagerError('unsafe_state')
    finally: kernel.LocalFree(descriptor)


def _private(path, directory=False):
    _no_links(path)
    info = path.lstat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ManagerError('unsafe_state')
    if os.name != 'nt':
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ManagerError('unsafe_state')
        return
    security, kernel, W = _win_functions()
    owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    if security.GetNamedSecurityInfoW(str(path), 1, 1 | 4, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor)):
        raise ManagerError('unsafe_state')
    try:
        current = _user_sid()
        if not dacl.value or _sid_text(owner, security, kernel, W) != current:
            raise ManagerError('unsafe_state')
        class AclInfo(ctypes.Structure):
            _fields_ = [('count', W.DWORD), ('used', W.DWORD), ('free', W.DWORD)]
        details = AclInfo()
        if not security.GetAclInformation(dacl, ctypes.byref(details), ctypes.sizeof(details), 2) or details.count > 64:
            raise ManagerError('unsafe_state')
        own = False
        for index in range(details.count):
            ace = ctypes.c_void_p()
            if not security.GetAce(dacl, index, ctypes.byref(ace)):
                raise ManagerError('unsafe_state')
            kind = ctypes.c_ubyte.from_address(ace.value).value
            if kind == 1: continue  # A denial cannot expose private content.
            if kind != 0: raise ManagerError('unsafe_state')
            sid = _sid_text(ace.value+8, security, kernel, W)
            if sid not in {current, 'S-1-5-18'}: raise ManagerError('unsafe_state')
            own |= sid == current
        if not own: raise ManagerError('unsafe_state')
    finally: kernel.LocalFree(descriptor)


def _private_directory(path):
    _no_links(path)
    if _present(path) is None:
        path.mkdir(parents=True, exist_ok=True)
        _secure(path, True)
    _private(path, True)


def _write_private(path, value):
    _private_directory(path.parent)
    if _present(path) is not None: _private(path)
    fd, temporary = tempfile.mkstemp(prefix='.bootstrap-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            _secure(Path(temporary))
            stream.write(value);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass


def _read_private(path, maximum=32768):
    _private(path)
    with path.open('rb') as stream: raw = stream.read(maximum+1)
    if len(raw) > maximum: raise ManagerError('invalid_profile')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ManagerError('invalid_profile')
            result[key] = value
        return result
    return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ManagerError('invalid_profile')))


@contextmanager
def _lease(path):
    if _present(path) is not None: _private(path)
    with path.open('a+b') as stream:
        _secure(path)
        try:
            if os.name == 'nt':
                import msvcrt
                stream.seek(0);msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError: raise ManagerError('starting') from None
        yield


def _token(value):
    return isinstance(value, str) and 0 < len(value) <= 4096 and not any(c.isspace() or ord(c)<33 or ord(c)==127 for c in value)


def _parent_pid(pid):
    if os.name != 'nt':
        return int(Path('/proc',str(pid),'stat').read_text().rsplit(')',1)[1].split()[1])
    from ctypes import wintypes as W
    class Basic(ctypes.Structure):
        _fields_=[('reserved',ctypes.c_void_p),('peb',ctypes.c_void_p),
                  ('reserved2',ctypes.c_void_p*2),('pid',ctypes.c_size_t),('parent',ctypes.c_size_t)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD];kernel.OpenProcess.restype=W.HANDLE
    kernel.CloseHandle.argtypes=[W.HANDLE]
    native=ctypes.WinDLL('ntdll')
    native.NtQueryInformationProcess.argtypes=[W.HANDLE,W.ULONG,ctypes.c_void_p,W.ULONG,ctypes.POINTER(W.ULONG)]
    native.NtQueryInformationProcess.restype=W.LONG
    handle=kernel.OpenProcess(0x1000,False,pid)
    if not handle:raise ManagerError('helper_identity_unavailable')
    try:
        value,length=Basic(),W.ULONG()
        if native.NtQueryInformationProcess(handle,0,ctypes.byref(value),ctypes.sizeof(value),ctypes.byref(length))<0:
            raise ManagerError('helper_identity_unavailable')
        return value.parent
    finally:kernel.CloseHandle(handle)


def _descendant(pid,ancestor,birth):
    try:
        if _process_birth(ancestor)!=birth:return False
        seen=set()
        for _ in range(8):
            if pid in seen or pid<=0:return False
            seen.add(pid);pid=_parent_pid(pid)
            if pid==ancestor:return _process_birth(ancestor)==birth
    except (OSError,ValueError,IndexError):pass
    return False


def public_config(value):
    """Strict projection for the native bridge; unknown/private fields rejected."""
    if not isinstance(value, dict) or set(value)-{'enabled','ready','broker_url','local_room','protocol','reason'}:
        raise ValueError('File exchange configuration unavailable')
    if type(value.get('enabled')) is not bool or type(value.get('ready',False)) is not bool:
        raise ValueError('File exchange configuration unavailable')
    result = {'enabled': value['enabled'], 'ready': value.get('ready',False), 'protocol': PROTOCOL}
    if result['ready'] and not result['enabled']: raise ValueError('File exchange configuration unavailable')
    if value.get('protocol', PROTOCOL) != PROTOCOL: raise ValueError('File exchange configuration unavailable')
    for key in ('broker_url','local_room','reason'):
        item = value.get(key, '')
        if not isinstance(item, str) or len(item)>2048 or any(ord(c)<32 or ord(c)==127 for c in item):
            raise ValueError('File exchange configuration unavailable')
        result[key] = item
    if result['broker_url']:
        result['broker_url'] = _endpoint(result['broker_url'])
    if len(result['local_room'])>128 or result['local_room']!=result['local_room'].strip():
        raise ValueError('File exchange configuration unavailable')
    if result['enabled'] and (not result['broker_url'] or not result['local_room']):
        raise ValueError('File exchange configuration unavailable')
    if result['reason'] and not re.fullmatch('[a-z_]{1,64}',result['reason']):
        raise ValueError('File exchange configuration unavailable')
    return result


class FileCompanionManager:
    def __init__(self, state_root, backend, owner_token, room, profile_path=None):
        self.base = Path(state_root).absolute()/'file-exchange'
        self.root = self.base/'v2'
        self.profile = Path(profile_path).absolute() if profile_path is not None else self.base/'profile.json'
        self.owner_token, self.room = owner_token, room
        self._invalid = None
        try: self.backend = _endpoint(backend, backend=True)
        except ValueError: self.backend='';self._invalid='invalid_backend'
        if not _token(owner_token): self._invalid='missing_owner_token'
        if not isinstance(room,str) or not room or len(room)>128 or not room.isprintable() or room!=room.strip():
            self._invalid='missing_room'
        with _GUARD:
            self._lock = _LOCKS.setdefault(os.path.normcase(str(self.root)), threading.Lock())

    def _config(self, enabled=False, ready=False, reason='not_configured', broker=''):
        return public_config({'enabled':enabled,'ready':ready,'reason':reason,'broker_url':broker,
            'local_room':self.room if isinstance(self.room,str) and not self._invalid else '', 'protocol':PROTOCOL})

    def _command(self, broker, helper, status_id):
        executable = Path(sys.executable)
        if getattr(sys,'frozen',False): command=[str(executable),'file-companion']
        else:
            candidate=executable.with_name('pythonw.exe')
            if sys.platform=='win32' and candidate.is_file(): executable=candidate
            command=[str(executable),'-m','supermonoagent.file_companion']
        return command+['--backend',self.backend,'--broker',broker,'--room',self.room,
            '--state-dir',str(helper),'--owner-token-file',str(helper/'owner.token'),
            '--broker-token-file',str(helper/'broker.token'),'--enrollment-key-file',str(helper/'enrollment.key'),
            '--status-id',status_id]

    def _status(self, record, broker):
        try:
            status = _read_private(self.root/'helper/status.json', 8192)
            fields={'role':'file-companion','protocol':PROTOCOL,'file_protocol':FILE_PROTOCOL,'backend':self.backend,'broker_url':broker,
                    'room':self.room,'status_id':record['status_id']}
            if not isinstance(status,dict) or type(status.get('schema')) is not int or status['schema']!=1 or any(status.get(key)!=item for key,item in fields.items()): return 'connecting'
            updated=status.get('updated_at')
            if type(updated) not in (int,float) or not math.isfinite(updated) or not 0<=time.time()-updated<=15: return 'connecting'
            pid,birth=status.get('pid'),status.get('process_start')
            if type(pid) is not int or type(birth) is not int or _process_birth(pid)!=birth:return 'connecting'
            if (pid,birth)!=(record['pid'],record['process_start']):
                # Windows venv redirectors launch a second Python process.
                # Adopt only its verified descendant with the private launch nonce.
                if not _descendant(pid,record['pid'],record['process_start']):return 'connecting'
                record.update(pid=pid,process_start=birth)
                _write_private(self.root/'manager.json',json.dumps(record,separators=(',',':')).encode())
            state=status.get('state')
            return state if state in {'online','connecting','parent_unavailable','broker_unavailable','authentication_failed','stopped'} else 'connecting'
        except (OSError,ValueError,TypeError,KeyError): return 'connecting'

    def _migrate_workspaces(self, helper):
        """Copy only a validated V1 settings snapshot, once, without overwrite."""
        source, target = self.base/'helper/workspaces.json', helper/'workspaces.json'
        if _present(target) is not None or _present(source) is None:
            return
        _private(source)
        with source.open('rb') as stream:raw=stream.read(MAX_SETTINGS_BYTES+1)
        if len(raw)>MAX_SETTINGS_BYTES:raise ManagerError('workspace_settings_unavailable')
        try:
            document = _document(json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError())))
            for entry in document['windows'].values():
                _directory(entry['project_dir']);_directory(entry['exchange_dir'])
            if document['mylib'] is not None:_directory(document['mylib']['root'])
        except (ValueError,UnicodeError,TypeError,KeyError,RuntimeError):
            raise ManagerError('workspace_settings_unavailable') from None
        descriptor, name = tempfile.mkstemp(prefix='.workspace-migration-', dir=str(helper))
        try:
            with os.fdopen(descriptor,'wb') as stream:
                _secure(Path(name));stream.write(raw);stream.flush();os.fsync(stream.fileno())
            try:os.link(name,target)
            except FileExistsError:pass
        finally:
            Path(name).unlink(missing_ok=True)

    def ensure_running(self):
        if self._invalid: return self._config(reason=self._invalid)
        if _present(self.profile) is None: return self._config()
        broker = ''
        try:
            profile=_read_private(self.profile)
            if not isinstance(profile,dict) or not {'schema','broker_url','enrollment_key'}<=set(profile) or set(profile)-{'schema','broker_url','enrollment_key','broker_token'} or type(profile['schema']) is not int or profile['schema']!=1:
                raise ManagerError('invalid_profile')
            broker=_endpoint(profile['broker_url'])
            key=profile['enrollment_key'];token=profile.get('broker_token',self.owner_token)
            if not isinstance(key,str) or not 32<=len(key)<=8192 or any(not 33<=ord(c)<=126 for c in key) or not _token(token):
                raise ManagerError('invalid_profile')
            fingerprint=hashlib.sha256(json.dumps([self.backend,broker,self.room,self.owner_token,token,key],separators=(',',':')).encode()).hexdigest()
            if not self._lock.acquire(False): return self._config(True,reason='starting',broker=broker)
            try:
                _private_directory(self.root)
                with _lease(self.root/'manager.lock'):
                    path=self.root/'manager.json';record=None
                    if _present(path) is not None:
                        record=_read_private(path,8192)
                        if not isinstance(record,dict) or set(record)!={'schema','config_sha256','status_id','pid','process_start'} or record['schema']!=1 or type(record['pid']) is not int or type(record['process_start']) is not int or not re.fullmatch('[a-f0-9]{32}',record['status_id']):
                            raise ManagerError('unsafe_state')
                        try: alive=_process_birth(record['pid'])==record['process_start']
                        except CompanionError: alive=False
                        if alive:
                            if record['config_sha256']!=fingerprint: return self._config(reason='configuration_changed')
                            state=self._status(record,broker)
                            return self._config(True,state=='online',state,broker)
                    attempt=_ATTEMPTS.get(str(self.root))
                    if attempt and attempt[0]==fingerprint and time.monotonic()-attempt[1]<5:
                        return self._config(True,reason='connecting',broker=broker)
                    helper=self.root/'helper';_private_directory(helper)
                    try:
                        with _lease(helper/'companion.lock'): pass
                    except ManagerError: return self._config(reason='helper_already_running')
                    self._migrate_workspaces(helper)
                    _write_private(helper/'owner.token',self.owner_token.encode())
                    _write_private(helper/'broker.token',token.encode())
                    _write_private(helper/'enrollment.key',key.encode())
                    status_id=secrets.token_hex(16)
                    command=self._command(broker,helper,status_id)
                    flags=(subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP) if sys.platform=='win32' else 0
                    process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                        cwd=str(Path(sys.executable).parent),creationflags=flags,close_fds=True)
                    _ATTEMPTS[str(self.root)]=(fingerprint,time.monotonic())
                    try: birth=_process_birth(process.pid)
                    except CompanionError: return self._config(reason='start_failed')
                    _write_private(path,json.dumps({'schema':1,'config_sha256':fingerprint,'status_id':status_id,
                        'pid':process.pid,'process_start':birth},separators=(',',':')).encode())
                    return self._config(True,reason='connecting',broker=broker)
            finally:self._lock.release()
        except ManagerError as error:
            reason=str(error) if str(error) in {'unsafe_state','invalid_profile','starting','workspace_settings_unavailable'} else 'invalid_profile'
            return self._config(reason=reason)
        except (OSError,ValueError,TypeError,KeyError):return self._config(reason='invalid_profile')
