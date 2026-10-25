"""Pinned metaRSI ledger plus deterministic, non-activating library review.

Only this service's ``state_root/mylib-review`` project is mutated. Catalog text
is data, never executable code. Formal evidence concerns structure only;
semantic effectiveness remains unverified and no admission is requested.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time


class EvolutionError(ValueError):
    """Fixed public code; never expose subprocess output or private paths."""


_FILES = ('scripts/metarsi.py', 'config/defaults.json', 'config/ideation.json')
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
_RULE = 'verification-section-v1'
_HEADING = re.compile(r'^#{1,6}\s+.*(?:verif|validat|\u9a8c\u8bc1|\u9a8c\u6536|\u68c0\u67e5)', re.I | re.M)
_APPEND = ('\n## Verification\n\n'
    '- [ ] Specify a reproducible structural check and retain its artifact.\n'
    '- [ ] Predeclare a semantic outcome and falsifier before activation.\n\n'
    'This proposed checklist has not demonstrated improved task outcomes.\n')

# Run only the pinned engine, with user metadata on stdin rather than argv.
# Discard engine prose (signal log prints all notes) without buffering it.
_BOOTSTRAP = r'''
import contextlib,json,runpy,sys
class Sink:
 def write(self,text): return len(text)
 def flush(self): pass
out=sys.stdout
code=1
try:
 raw=sys.stdin.buffer.read(32769)
 if len(raw)>32768: raise ValueError()
 value=json.loads(raw)
 if set(value)!={'args'} or not isinstance(value['args'],list): raise ValueError()
 with contextlib.redirect_stdout(Sink()),contextlib.redirect_stderr(Sink()):
  namespace=runpy.run_path(sys.argv[1],run_name='pinned_metarsi_engine')
  code=namespace['main'](['--project',sys.argv[2],'--by','mylib-review-service']+value['args'])
except BaseException:
 code=1
out.write(json.dumps({'exit_code':code})+'\n')
sys.exit(0 if code==0 else 2)
'''

# Independent process/parser, not the candidate generator. Its positive result
# proves preservation + a visible verification checklist, not semantic benefit.
_VALIDATOR = r'''
import hashlib,json,pathlib,re,sys
root=pathlib.Path(sys.argv[1])
source=(root/'source.md').read_bytes()
candidate=(root/'candidate.md').read_bytes()
metadata=json.loads((root/'source.json').read_text(encoding='utf8'))
source_text=source.decode('utf8'); text=candidate.decode('utf8')
headings=[]
for line in text.splitlines():
 if re.match(r'^#{1,6}\s+',line): headings.append(line.lstrip('#').strip().lower())
checks={
 'source_sha256':hashlib.sha256(source).hexdigest()==metadata['sha256'],
 'source_preserved':candidate.startswith(source),
 'explicit_verification_heading':any(title=='verification' for title in headings),
 'reproducible_check':bool(re.search(r'(?m)^- \[ \] .*reproducible.*artifact',text)),
 'semantic_falsifier':bool(re.search(r'(?m)^- \[ \] .*semantic.*falsifier',text)),
 'semantic_limit_explicit':'has not demonstrated improved task outcomes' in text,
 'candidate_changed':candidate!=source,
}
result={'schema':1,'kind':'formal','scope':'format_and_source_preservation_only','passed':all(checks.values()),
 'checks':checks,'semantic_efficacy':'unverified','source':metadata,
 'candidate_sha256':hashlib.sha256(candidate).hexdigest()}
print(json.dumps(result,sort_keys=True))
sys.exit(0 if result['passed'] else 2)
'''


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _plain(path, *, directory=False):
    info = path.lstat()
    wanted = stat.S_ISDIR if directory else stat.S_ISREG
    if not wanted(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise EvolutionError('state_invalid')
    return info


def _parents(path):
    for item in reversed(path.parents):
        try:
            _plain(item, directory=True)
        except FileNotFoundError:
            continue


def _read(path, limit=4 * 1024 * 1024):
    _parents(path)
    _plain(path)
    with path.open('rb') as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise EvolutionError('state_invalid')
    return value


def _atomic(path, value):
    _parents(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _plain(path.parent, directory=True)
    try:
        _plain(path)
    except FileNotFoundError:
        pass
    fd, name = tempfile.mkstemp(prefix='.review-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(value);stream.flush();os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _text(value, limit, *, empty=False):
    if not isinstance(value, str) or len(value) > limit or not empty and not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise EvolutionError('invalid_signal')
    try: value.encode('utf8')
    except UnicodeError: raise EvolutionError('invalid_signal') from None
    return value


def _source(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {'revision', 'skill_id', 'path', 'sha256'}:
        raise EvolutionError('invalid_signal')
    limits = {'revision': 128, 'skill_id': 512, 'path': 1024, 'sha256': 64}
    result = {key: _text(value[key], limits[key]) for key in value}
    if len(result['path'].encode('utf8')) > 1024:
        raise EvolutionError('invalid_signal')
    if not re.fullmatch('[a-f0-9]{64}', result['sha256']):
        raise EvolutionError('invalid_signal')
    path = PurePosixPath(result['path'])
    if path.is_absolute() or '..' in path.parts or '\\' in result['path'] or ':' in result['path']:
        raise EvolutionError('invalid_signal')
    return result


def _windows_job(process):
    """Kill only this newly created process tree when its private job closes."""
    import ctypes
    from ctypes import wintypes
    class Basic(ctypes.Structure):
        _fields_ = [("times", ctypes.c_longlong * 2), ("flags", wintypes.DWORD),
                    ("minimum", ctypes.c_size_t), ("maximum", ctypes.c_size_t),
                    ("active", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                    ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
    class Extended(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", ctypes.c_ulonglong * 6),
                    ("memory", ctypes.c_size_t * 4)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    info = Extended()
    info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not job:
        raise OSError("Cannot create assistant process job")
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)) or not kernel.AssignProcessToJobObject(job, int(process._handle)):
        kernel.CloseHandle(job)
        raise OSError("Cannot isolate assistant process tree")
    return lambda: kernel.CloseHandle(job)

def _child_environment():
    """Use interpreter-owned ELF libraries, never caller loader/Python settings.

    A relocated shared Python needs its own libpython before -I/-S can take
    effect. The running interpreter's base_prefix is the trusted runtime root;
    no inherited LD_* value contributes to the child search path.
    """
    env = {key: value for key, value in os.environ.items() if key.upper() in {'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'SYSTEMDRIVE'}}
    if sys.platform.startswith('linux'):
        try:
            configured = sys.base_prefix
            if (not isinstance(configured, str) or not configured
                    or any(ord(char) < 32 or ord(char) == 127 for char in configured)
                    or os.pathsep in configured or not Path(configured).is_absolute()):
                raise ValueError()
            prefix = Path(configured).resolve(strict=True)
            libraries = (prefix/'lib').resolve(strict=True)
            if not libraries.is_dir() or not libraries.is_relative_to(prefix):
                raise ValueError()
            env['LD_LIBRARY_PATH'] = str(libraries)
        except (OSError, ValueError, RuntimeError):
            raise EvolutionError('engine_unavailable') from None
    return env


def _run_process(command, *, cwd, payload=b'', timeout=10, limit=128 * 1024, budget_check=None):
    """Own process group, bounded stdout, sanitized failures, no shell/env hooks."""
    if not isinstance(command, (list, tuple)) or not command or command[0] != sys.executable:
        raise EvolutionError('review_failed')
    env = _child_environment()
    options = {'cwd': str(cwd), 'env': env, 'stdin': subprocess.PIPE,
               'stdout': subprocess.PIPE, 'stderr': subprocess.DEVNULL}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW
    else:
        options['start_new_session'] = True
    process = subprocess.Popen(command, **options)
    close_job = None
    if os.name == "nt":
        try: close_job = _windows_job(process)
        except OSError:
            process.kill();process.wait(timeout=3);process.stdin.close();process.stdout.close()
            raise EvolutionError("review_failed") from None
    chunks, failed = [], threading.Event()
    def reader():
        total = 0
        try:
            while True:
                data = process.stdout.read(4096)
                if not data:
                    break
                total += len(data)
                if total > limit:
                    failed.set();break
                chunks.append(data)
        except OSError:
            failed.set()
    thread = threading.Thread(target=reader, daemon=True);thread.start()
    end = time.monotonic() + timeout
    try:
        process.stdin.write(payload);process.stdin.close()
        next_check = 0
        while process.poll() is None:
            if budget_check is not None and time.monotonic() >= next_check:
                budget_check();next_check = time.monotonic() + .1
            if failed.is_set() or time.monotonic() >= end:
                raise EvolutionError('review_failed')
            time.sleep(.02)
        thread.join(timeout=1)
        if thread.is_alive() or failed.is_set():
            raise EvolutionError('review_failed')
        if budget_check is not None: budget_check()
        return process.returncode, b''.join(chunks)
    finally:
        if close_job: close_job()
        if os.name != "nt":
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
        elif process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        if not process.stdin.closed:
            process.stdin.close()
        process.stdout.close();thread.join(timeout=1)


class EvolutionService:
    """Explicit pinned engine; only server-owned review state, never activation."""
    def __init__(self, state_root, engine_root, library, *, max_state_bytes=256*1024**2, min_free_bytes=1024**3):
        if type(max_state_bytes) is not int or max_state_bytes <= 0 or type(min_free_bytes) is not int or min_free_bytes < 0:
            raise EvolutionError('state_invalid')
        self.max_state_bytes, self.min_free_bytes = max_state_bytes, min_free_bytes
        self.root, self.engine_root = Path(state_root).absolute(), Path(engine_root).absolute()
        if self.root == self.engine_root or self.root in self.engine_root.parents or self.engine_root in self.root.parents:
            raise EvolutionError('state_invalid')
        catalog_root = getattr(library, 'root', None)
        if catalog_root is not None:
            catalog_root = Path(catalog_root).absolute()
            if self.root == catalog_root or self.root in catalog_root.parents or catalog_root in self.root.parents:
                raise EvolutionError('state_invalid')
        self.library = library
        self.project = self.root / 'mylib-review'
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='mylib-review')
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(os.path.normcase(str(self.root)), threading.Lock())
        self.process_timeout = 10
        self.max_suggestions_per_run = 3

    def close(self):
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _tree_bytes(self, root):
        try: _plain(root, directory=True)
        except FileNotFoundError: return 0
        total, count, pending = 0, 0, [root]
        while pending:
            directory = pending.pop();_plain(directory, directory=True)
            total += 4096
            with os.scandir(directory) as entries:
                for entry in entries:
                    try: info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError: continue
                    count += 1
                    if count > 100000 or stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                        raise EvolutionError('state_invalid')
                    if stat.S_ISDIR(info.st_mode): pending.append(Path(entry.path))
                    elif stat.S_ISREG(info.st_mode):
                        total += max(info.st_size, getattr(info, 'st_blocks', 0)*512, ((info.st_size+4095)//4096)*4096)
                    else: raise EvolutionError('state_invalid')
        return total

    def _space(self):
        _parents(self.root)
        used = self._tree_bytes(self.root)
        existing = self.root
        while not existing.exists(): existing = existing.parent
        _plain(existing, directory=True)
        return {'used_bytes': used, 'free_bytes': shutil.disk_usage(existing).free,
                'max_state_bytes': self.max_state_bytes, 'min_free_bytes': self.min_free_bytes}

    def _storage_guard(self, reserve=0):
        value = self._space()
        if value['used_bytes'] + reserve > self.max_state_bytes:
            raise EvolutionError('quota_exceeded')
        if value['free_bytes'] < self.min_free_bytes + reserve:
            raise EvolutionError('insufficient_space')
        return value

    def _growth(self, payload=0):
        # A full SQLite rollback journal + rebuilt views/wiki need headroom.
        return 3*self._tree_bytes(self.project/'.metarsi') + 64*payload + 512*1024

    def _entry_growth(self, *, review=False, payload=0):
        pin = self.root/'engine-pin.json'
        try: _plain(pin); engine = 0
        except FileNotFoundError:
            try: _, _, files = self._engine_snapshot(self.engine_root)
            except (OSError, ValueError, TypeError): raise EvolutionError('engine_unavailable') from None
            engine = 2*sum(len(value) for value in files.values()) + 65536
        return engine + self._growth(payload) + (self.max_suggestions_per_run*384*1024 if review else 128*1024)

    def _write(self, path, value):
        self._storage_guard(2*len(value)+16384)
        _atomic(path, value)

    @contextmanager
    def _exclusive(self):
        if not self._lock.acquire(False):
            raise EvolutionError('review_busy')
        lease = None
        try:
            _parents(self.root)
            try: _plain(self.root, directory=True)
            except FileNotFoundError: self.root.mkdir(parents=True, exist_ok=True)
            _plain(self.root, directory=True)
            if os.name != 'nt': os.chmod(self.root, 0o700)
            path = self.root / 'review.lock'
            try: _plain(path)
            except FileNotFoundError: pass
            lease = path.open('a+b')
            try:
                if os.name == 'nt':
                    import msvcrt
                    lease.seek(0);msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise EvolutionError('review_busy') from None
            yield
        except EvolutionError:
            raise
        except (OSError, ValueError, TypeError):
            raise EvolutionError('state_invalid') from None
        finally:
            if lease: lease.close()
            self._lock.release()

    def _dispatch(self, function, *, review=False, payload=0):
        try: self._storage_guard(self._entry_growth(review=review, payload=payload))
        except EvolutionError: raise
        except (OSError, ValueError, TypeError): raise EvolutionError('state_invalid') from None
        with self._exclusive():
            self._storage_guard(self._entry_growth(review=review, payload=payload))
            self._deadline = time.monotonic() + 120
            try:
                return self._executor.submit(function).result()
            except EvolutionError:
                raise
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error, subprocess.SubprocessError):
                raise EvolutionError('review_failed') from None

    def _engine_snapshot(self, root):
        _parents(root);_plain(root, directory=True)
        files = {name: _read(root / name, 1024 * 1024) for name in _FILES}
        defaults = json.loads(files['config/defaults.json'])
        if defaults.get('store') != {'dir': '.metarsi', 'database': 'metarsi.db', 'wiki': 'wiki'}:
            raise EvolutionError('engine_unavailable')
        hashes = {name: _sha(value) for name, value in files.items()}
        return _sha(_canonical(hashes).encode()), hashes, files

    def _pin_engine(self):
        try:
            digest, hashes, files = self._engine_snapshot(self.engine_root)
        except (OSError, ValueError, TypeError):
            raise EvolutionError('engine_unavailable') from None
        pin_path = self.root / 'engine-pin.json'
        try:
            pin = json.loads(_read(pin_path, 16384))
            if pin != {'schema': 1, 'source': str(self.engine_root), 'sha256': digest, 'files': hashes}:
                raise EvolutionError('engine_changed')
        except FileNotFoundError:
            pin = {'schema': 1, 'source': str(self.engine_root), 'sha256': digest, 'files': hashes}
            destination = self.root / 'engine' / digest
            self._storage_guard(2*sum(len(value) for value in files.values())+65536)
            for name, value in files.items():
                self._write(destination / name, value)
            self._write(pin_path, _canonical(pin).encode())
        destination = self.root / 'engine' / digest
        try:
            actual, _, _ = self._engine_snapshot(destination)
            if actual != digest:
                raise EvolutionError('engine_changed')
        except (OSError, ValueError):
            raise EvolutionError('engine_changed') from None
        self._pin, self._engine_copy = pin, destination

    def _engine(self, args):
        allowed = {('init',), ('tick',), ('verify-ledger',), ('signal', 'log'), ('idea', 'add'), ('trial', 'log')}
        if tuple(args[:2]) not in allowed and tuple(args[:1]) not in allowed:
            raise EvolutionError('review_failed')
        self._pin_engine()
        payload = _canonical({'args': args}).encode()
        if len(payload) > 32768:
            raise EvolutionError('review_failed')
        self._storage_guard(0 if args[0] == 'verify-ledger' else self._growth(len(payload)))
        remaining = min(self.process_timeout, getattr(self, '_deadline', time.monotonic()+120) - time.monotonic())
        if remaining <= 0: raise EvolutionError('review_failed')
        code, raw = _run_process([sys.executable, '-I', '-S', '-X', 'utf8', '-c', _BOOTSTRAP,
            str(self._engine_copy / 'scripts/metarsi.py'), str(self.project)], cwd=self.project,
            payload=payload, timeout=remaining, budget_check=self._storage_guard)
        if code != 0 or json.loads(raw).get('exit_code') != 0:
            raise EvolutionError('state_invalid' if args[0] == 'verify-ledger' else 'review_failed')

    def _ensure_review(self):
        self._pin_engine()
        marker = self.project / 'service.json'
        constitution = self.project / '.metarsi/constitution.json'
        try:
            value = json.loads(_read(marker, 16384))
            if value != {'schema': 1, 'owner': 'mylib-review-service', 'constitution_sha256': _sha(_read(constitution))}:
                raise EvolutionError('state_invalid')
        except FileNotFoundError:
            if self.project.exists() and any(self.project.iterdir()):
                raise EvolutionError('state_invalid')
            self.project.mkdir(parents=True, exist_ok=True)
            self._engine(['init', '--name=mylib-review'])
            self._write(marker, _canonical({'schema': 1, 'owner': 'mylib-review-service',
                'constitution_sha256': _sha(_read(constitution))}).encode())
        try: _plain(self.project/'.metarsi/metarsi.db')
        except (OSError, ValueError): raise EvolutionError('state_invalid') from None
        self._engine(['verify-ledger'])

    def _events(self, ledger, key):
        path = self.project / '.metarsi/metarsi.db'
        _parents(path);_plain(path)
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            rows = connection.execute('SELECT data FROM events WHERE ledger=? AND data LIKE ? ORDER BY seq LIMIT 11', (ledger, '%' + key + '%')).fetchall()
        finally:
            connection.close()
        if len(rows) > 10: raise EvolutionError('state_invalid')
        for row in rows:
            item = json.loads(row[0])
            try: note = json.loads(item.get('note', ''))
            except (ValueError, TypeError): continue
            if note.get('service') == 'mylib-review' and note.get('key') == key:
                return item
        return None

    def _registry(self):
        self._storage_guard(128*1024)
        path = self.root / 'review.sqlite3'
        try: _plain(path)
        except FileNotFoundError: pass
        connection = sqlite3.connect(path, timeout=2)
        connection.execute('PRAGMA synchronous=FULL')
        connection.executescript('CREATE TABLE IF NOT EXISTS suggestions(id TEXT PRIMARY KEY,data TEXT); CREATE TABLE IF NOT EXISTS signals(id TEXT PRIMARY KEY,data TEXT); CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,data TEXT);')
        return connection

    def record_signal(self, kind, name, note, source=None):
        if not isinstance(kind, str) or kind not in {'request', 'failure', 'drift', 'opportunity'}:
            raise EvolutionError('invalid_signal')
        name, note, source = _text(name, 160), _text(note, 2000, empty=True), _source(source)
        metadata = {'kind': kind, 'name': name, 'note': note, 'source': source}
        key = _sha(_canonical(metadata).encode())
        try:
            existing = self._events('signals', key)
            if existing is not None:
                self._verify_chain_readonly()
                return {'signal_id': existing['id'], 'duplicate': True, 'recorded': True, 'scope': 'mylib-review'}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, sqlite3.Error):
            raise EvolutionError('state_invalid') from None
        def record():
            self._ensure_review()
            existing = self._events('signals', key)
            if existing is None:
                wrapper = _canonical({'service': 'mylib-review', 'key': key, **metadata})
                self._engine(['signal', 'log', '--kind=' + kind, '--name=' + name, '--note=' + wrapper])
                existing = self._events('signals', key)
                if existing is None: raise EvolutionError('state_invalid')
                duplicate = False
            else:
                duplicate = True
            connection = self._registry()
            try:
                self._storage_guard(2*len(_canonical(metadata).encode())+65536)
                with connection:
                    connection.execute('INSERT OR IGNORE INTO signals VALUES(?,?)', (key, _canonical(metadata)))
            finally: connection.close()
            return {'signal_id': existing['id'], 'duplicate': duplicate, 'recorded': True, 'scope': 'mylib-review'}
        return self._dispatch(record, payload=len(_canonical(metadata).encode()))

    def run_once(self):
        return self._dispatch(self._review, review=True)

    def _review(self):
        self._ensure_review()
        self._engine(['tick'])
        try:
            pinned = getattr(self.library, 'pinned', None)
            with pinned() if callable(pinned) else nullcontext(self.library) as catalog:
                summary = self._scan(catalog)
        except EvolutionError:
            raise
        except (ValueError, OSError, KeyError, TypeError):
            raise EvolutionError('library_unavailable') from None
        connection = self._registry()
        try:
            self._storage_guard(2*len(_canonical(summary).encode())+65536)
            with connection:
                connection.execute('INSERT OR REPLACE INTO state VALUES(?,?)', ('last_run', _canonical(summary)))
        finally: connection.close()
        return summary

    def _scan(self, catalog):
        try:
            page = catalog.list_skills(limit=50)
            revision = page['revision']
            if not isinstance(revision, str) or not re.fullmatch('[a-f0-9]{40,64}', revision):
                raise ValueError()
        except (ValueError, KeyError, TypeError, OSError):
            raise EvolutionError('library_unavailable') from None
        cards, seen = [], set()
        while True:
            if page.get('revision') != revision or not isinstance(page.get('items'), list):
                raise EvolutionError('library_unavailable')
            for card in page['items']:
                identity = card.get('id') if isinstance(card, dict) else None
                if not isinstance(identity, str) or identity in seen or len(cards) >= 5000:
                    raise EvolutionError('library_unavailable')
                seen.add(identity);cards.append(card)
            cursor = page.get('next_cursor')
            if cursor is None: break
            try: page = catalog.list_skills(cursor=cursor, limit=50)
            except (ValueError, KeyError, TypeError, OSError): raise EvolutionError('library_unavailable') from None
        scanned = created = skipped = pending = unavailable = exact = 0
        connection = self._registry()
        try:
            reported = set()
            for row in connection.execute('SELECT data FROM signals ORDER BY rowid DESC LIMIT 1000'):
                metadata = json.loads(row[0])
                if isinstance(metadata.get('source'), dict):
                    reported.add(metadata['source'].get('skill_id'))
            # Reports affect review order only; they are not confirmed failures.
            cards.sort(key=lambda card: card['id'] not in reported)
            for card in cards:
                if time.monotonic() >= self._deadline: raise EvolutionError('review_failed')
                scanned += 1
                if card.get('available') is False:
                    unavailable += 1
                    continue
                try:
                    value = catalog.read_skill(card['id'], revision=revision)
                    if value.get('revision') != revision or value.get('skill_id') != card['id']:
                        raise EvolutionError('library_unavailable') from None
                    source = _source({'revision': revision, 'skill_id': card['id'], 'path': value['path'], 'sha256': value['sha256']})
                    text = value['text']
                    if value.get('truncated') or not isinstance(text, str) or len(text.encode()) > 65536 or _sha(text.encode()) != source['sha256']:
                        skipped += 1;continue
                except (ValueError, KeyError, TypeError, OSError):
                    raise EvolutionError('library_unavailable') from None
                exact += 1
                if _HEADING.search(text): continue
                identity = _sha(_canonical([_RULE, revision, card['id'], source['sha256']]).encode())
                if connection.execute('SELECT 1 FROM suggestions WHERE id=?', (identity,)).fetchone(): continue
                if created >= self.max_suggestions_per_run:
                    pending += 1;continue
                self._suggest(connection, identity, source, text)
                created += 1
            self._engine(['verify-ledger'])
            summary = {'ok': True, 'revision': revision, 'scanned': scanned, 'suggestions': created,
                'suggestion_count': created,
                'skipped_unavailable': unavailable, 'exact_sources_checked': exact,
                'reviewed_scope': 'available_exact_skill_text',
                'skipped_nonexact_or_truncated': skipped, 'pending_candidates': pending,
                'complete': pending == 0, 'ledger_verified': True, 'completed_at': int(time.time()),
                'semantic_efficacy': 'unverified', 'activation': 'manual_review_only', 'model_configured': False, 'limitations': ['Deterministic documentation-format review only; no model-based innovation or semantic outcome validation.']}
            return summary
        finally: connection.close()

    def _suggest(self, connection, identity, source, text):
        self._storage_guard(self._growth(4096)+3*len(text.encode())+128*1024)
        folder = self.project / 'reviews' / identity
        _parents(folder)
        try: _plain(folder, directory=True)
        except FileNotFoundError: folder.mkdir(parents=True, exist_ok=True)
        _plain(folder, directory=True)
        candidate = text + ('' if text.endswith('\n') else '\n') + _APPEND
        for name, raw in [('source.md', text.encode()), ('candidate.md', candidate.encode()), ('source.json', _canonical(source).encode())]:
            self._write(folder / name, raw)
        code, output = _run_process([sys.executable, '-I', '-S', '-X', 'utf8', '-c', _VALIDATOR, str(folder)], cwd=folder, timeout=self.process_timeout, budget_check=self._storage_guard)
        try: validation = json.loads(output)
        except ValueError: raise EvolutionError('review_failed') from None
        if code not in (0, 2) or validation.get('passed') is not (code == 0):
            raise EvolutionError('review_failed')
        validation.update(validator_sha256=_sha(_VALIDATOR.encode()), engine_sha256=self._pin['sha256'],
                          python_version=sys.version.split()[0])
        artifact = folder / 'validation.json'
        self._write(artifact, _canonical(validation).encode())
        note = _canonical({'service': 'mylib-review', 'key': identity, 'source': source,
            'scope': 'format_and_source_preservation_only', 'semantic_efficacy': 'unverified'})
        idea = self._events('ideas', identity)
        if idea is None:
            self._engine(['idea', 'add', '--title=Review explicit verification checklist',
                '--operator=L3.falsify', '--premise=A visible acceptance section is structurally inspectable; usefulness is not yet measured.',
                '--claim=Consider the candidate checklist without activating it.',
                '--falsifier=Reject if the independent structural checker fails, or later task evaluation finds no useful effect.', '--note=' + note])
            idea = self._events('ideas', identity)
        trial = self._events('trials', identity)
        if trial is None:
            self._engine(['trial', 'log', '--rule-id=' + identity, '--family=skill-document-structure', '--kind=formal',
                '--verdict=' + ('pass' if validation['passed'] else 'fail'),
                '--artifact=' + artifact.relative_to(self.project).as_posix(), '--note=' + note])
            trial = self._events('trials', identity)
        if not idea or not trial: raise EvolutionError('state_invalid')
        suggestion = {'suggestion_id': identity, 'skill_id': source['skill_id'],
            'title': 'Consider an explicit verification checklist',
            'problem': 'Potential documentation gap: no verification/validation/check heading was found. This is not an observed capability failure.',
            'hypothesis': 'A visible checklist may make acceptance conditions easier to locate; semantic benefit is unverified.',
            'falsifier': 'Reject if the structural checker fails or independent task evaluation shows no benefit.',
            'source': source, 'status': 'structurally_validated' if validation['passed'] else 'validation_failed',
            'proposal': {'kind': 'documentation_template', 'text': _APPEND.strip(), 'applied': False, 'artifact': (folder/'candidate.md').relative_to(self.project).as_posix(), 'sha256': _sha(candidate.encode())},
            'validation': {'kind': 'formal', 'scope': 'format_and_source_preservation_only', 'passed': validation['passed'],
                'artifact': artifact.relative_to(self.project).as_posix(), 'artifact_sha256': _sha(_read(artifact)),
                'validator_sha256': validation['validator_sha256'], 'engine_sha256': validation['engine_sha256'],
                'trial_id': trial['id'], 'semantic_efficacy': 'unverified'},
            'idea_id': idea['id'], 'admission': 'not_requested'}
        with connection:
            self._storage_guard(2*len(_canonical(suggestion).encode())+65536)
            connection.execute('INSERT OR IGNORE INTO suggestions VALUES(?,?)', (identity, _canonical(suggestion)))

    def _verify_chain_readonly(self):
        path = self.project/'.metarsi/metarsi.db'
        _parents(path);_plain(path)
        connection = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
        try:
            last = {}
            for number, (ledger, data, previous, digest) in enumerate(connection.execute('SELECT ledger,data,prev,hash FROM events ORDER BY seq')):
                if number >= 100000 or previous != last.get(ledger, '0'*16) or _sha((previous+data).encode())[:16] != digest:
                    raise EvolutionError('state_invalid')
                last[ledger] = digest
        finally: connection.close()

    def _read_registry(self, limit=20):
        path = self.root / 'review.sqlite3'
        try: _parents(path);_plain(path)
        except FileNotFoundError: return None, [], 0, 0
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.2)
        try:
            row = connection.execute('SELECT data FROM state WHERE key=?', ('last_run',)).fetchone()
            count = connection.execute('SELECT COUNT(*) FROM suggestions').fetchone()[0]
            signals = connection.execute('SELECT COUNT(*) FROM signals').fetchone()[0]
            items = [json.loads(row[0]) for row in connection.execute('SELECT data FROM suggestions ORDER BY rowid DESC LIMIT ?', (limit,))]
            for item in items: self._verify_artifact(item)
            return json.loads(row[0]) if row else None, items, count, signals
        except (ValueError, sqlite3.Error): raise EvolutionError('state_invalid') from None
        finally: connection.close()

    def _verify_artifact(self, item):
        try:
            identity = item['suggestion_id']
            if not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{64}', identity): raise ValueError()
            validation, proposal = item['validation'], item['proposal']
            folder = self.project/'reviews'/identity
            artifact_path = 'reviews/' + identity + '/validation.json'
            candidate_path = 'reviews/' + identity + '/candidate.md'
            if validation['artifact'] != artifact_path or proposal['artifact'] != candidate_path or proposal['applied'] is not False:
                raise ValueError()
            raw = _read(folder/'validation.json', 32768)
            artifact = json.loads(raw)
            source = _source(item['source'])
            if (item['skill_id'] != source['skill_id'] or item['admission'] != 'not_requested'
                    or validation['kind'] != 'formal' or validation['scope'] != 'format_and_source_preservation_only'
                    or validation['semantic_efficacy'] != 'unverified' or type(artifact['passed']) is not bool
                    or _sha(raw) != validation['artifact_sha256'] or artifact['source'] != source
                    or json.loads(_read(folder/'source.json', 16384)) != source
                    or artifact['passed'] is not validation['passed']
                    or artifact['validator_sha256'] != validation['validator_sha256']
                    or artifact['engine_sha256'] != validation['engine_sha256']
                    or artifact['scope'] != 'format_and_source_preservation_only'
                    or artifact['semantic_efficacy'] != 'unverified'
                    or _sha(_read(folder/'source.md', 65536)) != source['sha256']
                    or _sha(_read(folder/'candidate.md', 70000)) != artifact['candidate_sha256']
                    or proposal['sha256'] != artifact['candidate_sha256']):
                raise ValueError()
            trial = self._events('trials', identity)
            if (not trial or trial['id'] != validation['trial_id'] or trial['tier'] != 'formal'
                    or trial['kind'] != 'formal' or trial['verdict'] != ('pass' if artifact['passed'] else 'fail')
                    or not any(row.get('path') == artifact_path and row.get('sha256') == _sha(raw)[:16] for row in trial.get('artifacts', []))):
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            raise EvolutionError('state_invalid') from None

    def status(self):
        try:
            last, _, count, signals = self._read_registry()
            space = self._space()
            try:
                self._storage_guard(self._entry_growth(review=True))
                space['review_writable'] = True
            except EvolutionError as error:
                space.update(review_writable=False, error=str(error))
            pin_path = self.root / 'engine-pin.json'
            try: pin = json.loads(_read(pin_path, 16384))
            except FileNotFoundError: pin = None
            if pin is not None and (not isinstance(pin, dict) or not isinstance(pin.get('sha256'), str) or not re.fullmatch('[a-f0-9]{64}', pin['sha256'])):
                raise EvolutionError('state_invalid')
            digest, _, _ = self._engine_snapshot(self.engine_root)
            state = 'not_pinned' if pin is None else 'ready' if digest == pin.get('sha256') else 'changed'
            if pin is not None:
                copy_digest, _, _ = self._engine_snapshot(self.root/'engine'/pin['sha256'])
                if copy_digest != pin['sha256']: state = 'changed'
            return {'available': state in {'ready', 'not_pinned'}, 'mode': 'structural_review_only', 'review_project': 'mylib-review',
                'engine': {'pinned': pin is not None, 'sha256': pin.get('sha256') if pin else None, 'status': state},
                'last_run': last, 'suggestion_count': count, 'pending_signal_count': signals,
                'storage': space,
                'semantic_efficacy': 'unverified', 'activation': 'manual_review_only', 'model_configured': False, 'limitations': ['Deterministic documentation-format review only; no model-based innovation or semantic outcome validation.']}
        except (ValueError, OSError, KeyError, TypeError, sqlite3.Error):
            return {'available': False, 'mode': 'structural_review_only', 'error': 'state_invalid', 'semantic_efficacy': 'unverified', 'activation': 'manual_review_only', 'model_configured': False, 'limitations': ['Deterministic documentation-format review only; no model-based innovation or semantic outcome validation.']}

    def list_suggestions(self, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise EvolutionError('invalid_limit')
        try: _, items, count, _ = self._read_registry(limit)
        except (OSError, ValueError, sqlite3.Error): raise EvolutionError('state_invalid') from None
        return {'items': items, 'count': count, 'semantic_efficacy': 'unverified'}


def main():
    parser = argparse.ArgumentParser(description='Pinned, non-activating mylib structural review')
    parser.add_argument('--state-root', type=Path, required=True)
    parser.add_argument('--engine-root', type=Path, required=True)
    parser.add_argument('--catalog-root', type=Path, required=True)
    parser.add_argument('--max-state-bytes', type=int, default=256*1024**2)
    parser.add_argument('--min-free-bytes', type=int, default=1024**3)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--once', action='store_true')
    mode.add_argument('--status', action='store_true')
    mode.add_argument('--list', action='store_true')
    args = parser.parse_args()
    from .mylib_catalog import CatalogStore
    service = EvolutionService(args.state_root, args.engine_root, CatalogStore(args.catalog_root),
        max_state_bytes=args.max_state_bytes, min_free_bytes=args.min_free_bytes)
    try:
        result = service.run_once() if args.once else service.list_suggestions() if args.list else service.status()
        print(_canonical(result))
    except EvolutionError as error:
        print(_canonical({'ok': False, 'error': str(error)}));raise SystemExit(2)
    finally: service.close()


if __name__ == '__main__':
    main()
