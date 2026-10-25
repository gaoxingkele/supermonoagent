"""Synchronize an operator-selected Git library without executing its contents.

This module is a timer/CLI component, never an MCP tool. Only its private cache
and catalog store are writable. Git fetches use explicit HTTPS allowlists;
submodule URLs are checked against configuration before any fetch occurs.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import configparser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit
import uuid

from .file_workspaces import _directory

_SHA = re.compile(r'(?:[a-f0-9]{40}|[a-f0-9]{64})\Z')
_REF = re.compile(r'refs/heads/[A-Za-z0-9][A-Za-z0-9_./-]{0,180}\Z')
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
_MAX_OUTPUT = 8 * 1024 * 1024


class SyncError(ValueError):
    """A fixed public error code, never Git output or a configured path."""


def _fail(code):
    raise SyncError(code)


def _relative(value):
    if (not isinstance(value, str) or not value or len(value) > 1024
            or '\\' in value or ':' in value or any(ord(c) < 32 for c in value)):
        _fail('invalid_config')
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(p in {'.', '..', '.git'} for p in path.parts):
        _fail('invalid_config')
    return value


def _url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        _fail('invalid_config')
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.port not in {None, 443} or parsed.query or parsed.fragment
                or not re.fullmatch(r'[A-Za-z0-9.-]+', parsed.hostname)
                or '%' in parsed.path or '\\' in parsed.path or not parsed.path.endswith('.git')
                or any(p in {'.', '..', ''} for p in parsed.path.split('/')[1:])):
            _fail('invalid_config')
    except ValueError:
        _fail('invalid_config')
    return value


def _path(value):
    try:
        raw = os.fspath(value)
        path = Path(_directory(raw)[0])
    except (OSError, ValueError, TypeError, RuntimeError):
        _fail('invalid_config')
    if not path.is_absolute() or path == Path(path.anchor):
        _fail('invalid_config')
    return path


def _config(value):
    required = {'schema', 'source_url', 'ref', 'cache_root', 'store_root', 'submodules'}
    optional = {'command_timeout', 'total_timeout', 'min_free_bytes', 'max_cache_bytes', 'review'}
    if (not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional
            or type(value['schema']) is not int or value['schema'] != 1):
        _fail('invalid_config')
    result = dict(value)
    result['source_url'] = _url(value['source_url'])
    ref = value['ref']
    if (not isinstance(ref, str) or not _REF.fullmatch(ref) or '..' in ref or '@{' in ref
            or ref.endswith(('/', '.', '.lock')) or '//' in ref):
        _fail('invalid_config')
    result['cache_root'], result['store_root'] = _path(value['cache_root']), _path(value['store_root'])
    roots = [result['cache_root'], result['store_root']]
    if roots[0].is_relative_to(roots[1]) or roots[1].is_relative_to(roots[0]):
        _fail('invalid_config')
    modules = value['submodules']
    if not isinstance(modules, dict) or len(modules) > 32:
        _fail('invalid_config')
    result['submodules'] = {_relative(path): _url(url) for path, url in modules.items()}
    for key, default, low, high in [('command_timeout', 120, 1, 300), ('total_timeout', 900, 1, 1800),
                                   ('min_free_bytes', 1024**3, 0, 1024**4),
                                   ('max_cache_bytes', 4*1024**3, 1024, 32*1024**3)]:
        number = value.get(key, default)
        if type(number) is not int or not low <= number <= high:
            _fail('invalid_config')
        result[key] = number
    review = value.get('review')
    if review is not None:
        if not isinstance(review, dict) or set(review) != {'state_root', 'engine_root'}:
            _fail('invalid_config')
        result['review'] = {key: _path(path) for key, path in review.items()}
        state = result['review']['state_root']
        if any(state.is_relative_to(p) or p.is_relative_to(state) for p in roots):
            _fail('invalid_config')
        engine = result['review']['engine_root']
        if any(engine.is_relative_to(p) or p.is_relative_to(engine) for p in [*roots, state]):
            _fail('invalid_config')
    return result


def _safe_child(root, *parts):
    child = root.joinpath(*parts)
    if child == root or not child.is_relative_to(root):
        _fail('invalid_cache')
    try:
        _directory(child)
    except (OSError, ValueError, RuntimeError):
        _fail('invalid_cache')
    return child


def _regular(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        _fail('invalid_cache')
    return True


@contextmanager
def _lock(root):
    key = os.path.normcase(str(root))
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.Lock())
    if not lock.acquire(blocking=False):
        _fail('sync_busy')
    try:
        _directory(root)
        root.mkdir(parents=True, exist_ok=True)
        path = root / 'sync.lock'
        _regular(path)
        with path.open('a+b') as stream:
            try:
                if os.name == 'nt':
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                _fail('sync_busy')
            try:
                yield
            finally:
                if os.name == 'nt':
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        lock.release()


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: bytes
    stderr: bytes = b''


def _run(arguments, *, cwd, env, timeout, max_output=_MAX_OUTPUT, check=None):
    """Drain bounded pipes and kill only this invocation's private process tree."""
    options = {'cwd': str(cwd), 'env': env, 'stdin': subprocess.DEVNULL,
               'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options['start_new_session'] = True
    try:
        process = subprocess.Popen(arguments, **options)
    except OSError:
        _fail('git_unavailable')
    close_job = None
    job_lock = threading.Lock()
    overflow = threading.Event()
    chunks = [bytearray(), bytearray()]

    def close_private_job():
        nonlocal close_job
        with job_lock:
            closer, close_job = close_job, None
        if closer is not None:
            closer()
            return True
        return False

    def kill():
        if os.name == 'nt':
            if not close_private_job() and process.poll() is None:
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def drain(pipe, index):
        try:
            while data := pipe.read(65536):
                if len(chunks[index]) + len(data) > max_output:
                    overflow.set()
                    kill()
                    break
                chunks[index].extend(data)
        finally:
            pipe.close()

    try:
        if os.name == 'nt':
            # Reuse the existing private process-tree isolation utility.
            from .assistant_codex import _windows_job
            close_job = _windows_job(process)
        readers = [threading.Thread(target=drain, args=(pipe, i), daemon=True)
                   for i, pipe in enumerate((process.stdout, process.stderr))]
        for reader in readers:
            reader.start()
        deadline = time.monotonic() + timeout
        try:
            while True:
                if check is not None:
                    check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _fail('git_timeout')
                try:
                    code = process.wait(timeout=min(.2, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except SyncError:
            kill()
            process.wait(timeout=3)
            raise
        for reader in readers:
            reader.join(timeout=3)
        if any(reader.is_alive() for reader in readers):
            kill()
            _fail('git_timeout')
        if overflow.is_set():
            _fail('git_output_limit')
        return GitResult(code, bytes(chunks[0]), bytes(chunks[1]))
    except OSError:
        kill()
        _fail('git_unavailable')
    finally:
        if process.poll() is None:
            kill()
            process.wait(timeout=3)
        close_private_job()


def _environment(root):
    permitted = {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'COMSPEC', 'PATHEXT'}
    env = {key: value for key, value in os.environ.items() if key.upper() in permitted}
    env.update(HOME=str(root/'home'), GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='', GIT_LFS_SKIP_SMUDGE='1', LC_ALL='C')
    return env


def _size(root, maximum):
    count = total = 0
    pending = [root]
    while pending:
        folder = pending.pop()
        _directory(folder, required=True)
        with os.scandir(folder) as entries:
            for entry in entries:
                count += 1
                if count > 200000:
                    _fail('cache_limit')
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    # Git may atomically rename an in-progress pack while the
                    # independent quota monitor scans this private cache.
                    continue
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    _fail('invalid_cache')
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
                    if total > maximum:
                        _fail('cache_limit')
                else:
                    _fail('invalid_cache')
    return total


class _BuildBudget:
    """Reserve every blob before writing; amortize external disk/cache checks.

    Git fetches are already finished and the sync lock owns this cache. The
    builder declares bytes before a blob/manifest write, so the private cache
    limit is checked in O(1) even between occasional complete stat scans.
    Deleted excluded files deliberately do not refund reservations.
    """
    _INTERVAL = 1.0

    def __init__(self, config, deadline):
        self.config, self.deadline = config, deadline
        self.root = config['cache_root']
        self._lock = threading.RLock()
        self._reserved = self._extra = self._disk_reserved = 0
        self._deadline()
        self._base = _size(self.root, config['max_cache_bytes'])
        self._free = shutil.disk_usage(self.root).free
        self._next_scan = time.monotonic() + self._INTERVAL
        self()

    def _deadline(self):
        if time.monotonic() >= self.deadline:
            _fail('sync_timeout')

    def __call__(self, pending_bytes=0):
        with self._lock:
            self._deadline()
            if type(pending_bytes) is not int or pending_bytes < 0:
                _fail('invalid_budget')
            if time.monotonic() >= self._next_scan:
                observed = _size(self.root, self.config['max_cache_bytes'])
                growth = max(0, observed - self._base)
                self._extra = max(self._extra, growth - self._reserved)
                # A blob is reserved in full before its chunks are written.
                # Keep its still-unwritten bytes reserved after a disk refresh.
                outstanding = max(0, self._reserved - growth)
                self._free = shutil.disk_usage(self.root).free - outstanding
                self._disk_reserved = self._reserved
                self._next_scan = time.monotonic() + self._INTERVAL
            projected = self._reserved + pending_bytes
            if self._base + self._extra + projected > self.config['max_cache_bytes']:
                _fail('cache_limit')
            if self._free - (projected - self._disk_reserved) < self.config['min_free_bytes']:
                _fail('insufficient_space')
            self._reserved = projected
            self._deadline()


class _Git:
    def __init__(self, config, runner):
        self.config, self.runner = config, runner
        self.root = config['cache_root']
        self.deadline = time.monotonic() + config['total_timeout']
        self.executable = shutil.which('git')
        if not self.executable:
            _fail('git_unavailable')
        self.env = _environment(self.root)
        self.options = ['-c', 'credential.helper=', '-c', 'credential.interactive=false',
                        '-c', 'core.askPass=', '-c', 'core.fsmonitor=false', '-c', 'core.longpaths=true',
                        '-c', 'core.hooksPath='+str(self.root/'no-hooks'),
                        '-c', 'protocol.allow=never', '-c', 'protocol.https.allow=always',
                        '-c', 'protocol.file.allow=never', '-c', 'protocol.ext.allow=never',
                        '-c', 'submodule.recurse=false', '-c', 'fetch.fsckObjects=true',
                        '-c', 'http.sslVerify=true', '-c', 'http.followRedirects=false',
                        '-c', 'http.lowSpeedLimit=1024', '-c', 'http.lowSpeedTime=30',
                        '-c', 'maintenance.auto=false', '-c', 'gc.auto=0']

    def call(self, repo, *arguments, allowed=(0,)):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            _fail('git_timeout')
        result = self.runner([self.executable, *self.options, *arguments], cwd=repo, env=self.env,
                             timeout=min(self.config['command_timeout'], remaining), max_output=_MAX_OUTPUT,
                             check=self.resources)
        if result.returncode not in allowed:
            _fail('git_failed')
        return result

    def resources(self):
        if shutil.disk_usage(self.root).free < self.config['min_free_bytes']:
            _fail('insufficient_space')
        _size(self.root, self.config['max_cache_bytes'])

    def repository(self, destination, url):
        _safe_child(self.root, destination.relative_to(self.root))
        if not destination.exists():
            temporary = destination.with_name(destination.name+'.clone-'+uuid.uuid4().hex)
            _safe_child(self.root, temporary.relative_to(self.root))
            try:
                self.call(destination.parent, 'clone', '--quiet', '--no-checkout', '--no-hardlinks', '--', url, str(temporary))
                _directory(temporary, required=True)
                os.replace(temporary, destination)
            finally:
                if temporary.exists():
                    # This exact, generated child is the only recursive cleanup target.
                    _safe_child(self.root, temporary.relative_to(self.root))
                    shutil.rmtree(temporary)
        _directory(destination, required=True)
        gitdir = _safe_child(self.root, destination.relative_to(self.root), '.git')
        _directory(gitdir, required=True)
        if (gitdir/'objects/info/alternates').exists():
            _fail('invalid_cache')
        origin = self.call(destination, 'config', '--local', '--get', 'remote.origin.url').stdout.decode('utf-8').strip()
        if origin != url:
            _fail('source_changed')
        local = self.call(destination, 'config', '--local', '--name-only', '--list').stdout.decode('utf-8').splitlines()
        permitted = {'core.repositoryformatversion', 'core.filemode', 'core.bare', 'core.logallrefupdates',
                     'core.symlinks', 'core.ignorecase', 'core.precomposeunicode',
                     'remote.origin.url', 'remote.origin.fetch', 'extensions.objectformat', 'extensions.refstorage'}
        if any(key.lower() not in permitted and not re.fullmatch(r'branch\.[A-Za-z0-9_./-]+\.(?:remote|merge)', key)
               for key in local):
            _fail('invalid_cache')
        return destination

    def commit(self, repo, revision):
        if not isinstance(revision, str) or not _SHA.fullmatch(revision):
            _fail('invalid_revision')
        if self.call(repo, 'cat-file', '-e', revision+'^{commit}', allowed=(0, 1, 128)).returncode:
            self.call(repo, 'fetch', '--quiet', '--no-tags', '--no-recurse-submodules', 'origin', revision)
        self.call(repo, 'cat-file', '-e', revision+'^{commit}')

    def submodules(self, repo, revision, prefix='', found=None):
        found = {} if found is None else found
        raw = self.call(repo, 'ls-tree', '-rz', revision).stdout
        links = []
        for row in raw.split(b'\0'):
            if row.startswith(b'160000 '):
                header, raw_name = row.split(b'\t', 1)
                name = _relative(raw_name.decode('utf-8'))
                links.append((name, header.split()[2].decode('ascii')))
        if not links:
            return found
        document = self.call(repo, 'show', revision+':.gitmodules').stdout
        if len(document) > 65536:
            _fail('submodule_source_changed')
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        try:
            parser.read_string(document.decode('utf-8'))
            declarations = {}
            for section in parser.sections():
                if not section.startswith('submodule '):
                    _fail('submodule_source_changed')
                path, url = _relative(parser[section]['path']), _url(parser[section]['url'])
                if path in declarations:
                    _fail('submodule_source_changed')
                declarations[path] = url
        except (KeyError, ValueError, configparser.Error, UnicodeError):
            _fail('submodule_source_changed')
        for name, pinned in links:
            full = prefix+'/'+name if prefix else name
            if full in found or full.count('/') > 16 or len(found) >= 32:
                _fail('submodule_limit')
            url = self.config['submodules'].get(full)
            if not url or declarations.get(name) != url:
                _fail('submodule_source_changed')
            identity = hashlib.sha256((full+'\0'+url).encode()).hexdigest()
            path = self.repository(_safe_child(self.root, 'submodules', identity), url)
            self.commit(path, pinned)
            found[full] = path
            self.submodules(path, pinned, full, found)
        return found


def _report(root, report):
    _directory(root, required=True)
    target = root/'last-sync.json'
    _regular(target)
    fd, name = tempfile.mkstemp(prefix='.last-sync-', dir=root)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(report, stream, sort_keys=True, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)


def _record_report(root, report):
    try:
        _report(root, report)
        return True
    except (OSError, SyncError):
        # Audit-disk failure must not mask a pre-publication error or claim a
        # successfully switched version was rolled back.
        return False


def sync_once(config, *, runner=None, catalog_api=None, review_factory=None):
    """Sync one fixed operator configuration; never accept a client-supplied URL.

    ``runner``/``catalog_api`` are explicit test injection points. Review failures
    are reported independently after a successful, verified library promotion.
    """
    settings = _config(config)
    if catalog_api is None:
        from . import mylib_catalog as catalog_api
    cache, store = settings['cache_root'], settings['store_root']
    with _lock(cache):
        result = {'schema': 1, 'checked_at': int(time.time()), 'ok': False, 'changed': False}
        try:
            if shutil.disk_usage(cache).free < settings['min_free_bytes']:
                _fail('insufficient_space')
            _size(cache, settings['max_cache_bytes'])
            library = catalog_api.CatalogStore(store)
            before = library.status()
            previous = before.get('source_revision')
            active = before.get('revision')
            if before.get('available') and (not isinstance(previous, str) or not _SHA.fullmatch(previous)):
                _fail('invalid_active')
            for child in ('repos', 'submodules', 'candidates', 'home', 'no-hooks'):
                _safe_child(cache, child).mkdir(exist_ok=True)
            git = _Git(settings, runner or _run)
            repo = git.repository(_safe_child(cache, 'repos', 'source'), settings['source_url'])
            git.call(repo, 'fetch', '--quiet', '--no-tags', '--no-recurse-submodules', 'origin',
                     '+'+settings['ref']+':refs/remotes/origin/mylib-sync')
            revision = git.call(repo, 'rev-parse', '--verify', 'refs/remotes/origin/mylib-sync^{commit}').stdout.decode('ascii').strip()
            if not _SHA.fullmatch(revision):
                _fail('invalid_revision')
            if previous:
                git.commit(repo, previous)
                if git.call(repo, 'merge-base', '--is-ancestor', previous, revision, allowed=(0, 1)).returncode:
                    _fail('non_fast_forward')
            modules = git.submodules(repo, revision)
            _size(cache, settings['max_cache_bytes'])
            if previous == revision:
                budget = _BuildBudget(settings, git.deadline)
                catalog_api.verify_snapshot(store/'releases'/active, budget_check=budget)
                budget()
                published = {'revision': active, 'changed': False}
            else:
                parent = _safe_child(cache, 'candidates')
                with tempfile.TemporaryDirectory(prefix='snapshot-', dir=parent) as temporary:
                    candidate = _safe_child(cache, Path(temporary).relative_to(cache), 'release')
                    budget = _BuildBudget(settings, git.deadline)
                    catalog_api.build_snapshot(repo, candidate, revision=revision, submodule_repos=modules,
                                               budget_check=budget)
                    budget()
                    catalog_api.verify_snapshot(candidate, budget_check=budget)
                    _size(cache, settings['max_cache_bytes'])
                    candidate_bytes = _size(candidate, settings['max_cache_bytes'])
                    if time.monotonic() >= git.deadline:
                        _fail('sync_timeout')
                    probe = store
                    while not probe.exists():
                        probe = probe.parent
                    if shutil.disk_usage(probe).free < settings['min_free_bytes'] + candidate_bytes:
                        _fail('insufficient_space')
                    published = catalog_api.publish_snapshot(store, candidate, expected_revision=active)
            result.update(ok=True, changed=published['changed'], revision=published['revision'],
                          source_revision=revision, submodules=len(modules))
            if settings.get('review'):
                try:
                    if review_factory is None:
                        from .mylib_evolution import EvolutionService
                        review_factory = EvolutionService
                    review = review_factory(settings['review']['state_root'], settings['review']['engine_root'], library)
                    review_result = review.run_once()
                    result['review'] = {'ok': True, 'revision': review_result.get('revision'),
                                        'suggestion_count': review_result.get('suggestion_count')}
                except Exception:
                    result['review'] = {'ok': False, 'error': 'review_failed'}
            result['audit_written'] = _record_report(cache, result)
            return result
        except SyncError as exc:
            result['error'] = str(exc)
            _record_report(cache, result)
            raise
        except Exception:
            result['error'] = 'sync_failed'
            _record_report(cache, result)
            _fail('sync_failed')


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _fail('invalid_config')
        value[key] = item
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description='Sync a fixed trusted mylib Git source into immutable snapshots')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--once', action='store_true', required=True)
    args = parser.parse_args(argv)
    try:
        _directory(args.config.absolute().parent, required=True)
        if not _regular(args.config):
            _fail('invalid_config')
        with args.config.open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            _fail('invalid_config')
        config = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique)
        print(json.dumps(sync_once(config), sort_keys=True))
    except (SyncError, OSError, ValueError, UnicodeError):
        print(json.dumps({'ok': False, 'error': 'sync_failed'}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
