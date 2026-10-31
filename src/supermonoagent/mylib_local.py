"""Local-only skill worktree, Git synchronization and reviewed evolution."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import threading
import time

from .mylib_assistant import SkillIndex, evolution_suggestions
from .mylib_catalog import _private_path
from .mylib_evolution import EvolutionService, EvolutionError
from .mylib_sync import _environment, _run, _url
from .file_workspaces import _directory


class LocalLibraryError(ValueError):
    """Fixed public error codes without Git output or credential material."""


def _fail(code):
    raise LocalLibraryError(code)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _root(value):
    try:
        path = Path(value)
        if not path.is_absolute():
            _fail('invalid_config')
        path = path.absolute()
        if path == Path(path.anchor) or '\0' in str(path):
            _fail('invalid_config')
        return path
    except (OSError, ValueError, TypeError):
        _fail('invalid_config')


def readable_library_root(value):
    try:
        path, _ = _directory(value, required=True)
        with os.scandir(path) as entries:
            next(entries, None)
    except (OSError, ValueError, RuntimeError):
        _fail('directory_unavailable')
    return Path(path)


def library_root_accessible(value):
    try:
        readable_library_root(value)
        return True
    except LocalLibraryError:
        return False


def _relative(value):
    if not isinstance(value, str) or not value or len(value) > 1024 or '\\' in value or ':' in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        _fail('invalid_path')
    candidate = value[:-1] if value.endswith('/') and not value.endswith('//') else value
    path = PurePosixPath(candidate)
    if path.is_absolute() or str(path) != candidate or any(part in {'.', '..', '.git'} for part in path.parts):
        _fail('invalid_path')
    return value


def _plain(path, *, directory=False):
    try:
        info = path.lstat()
    except OSError:
        _fail('source_unavailable')
    wanted = stat.S_ISDIR if directory else stat.S_ISREG
    if not wanted(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        _fail('source_unavailable')
    return info


def _read(root, relative, limit=65536):
    relative = _relative(relative)
    path = root
    _plain(root, directory=True)
    for part in PurePosixPath(relative).parts[:-1]:
        path /= part
        _plain(path, directory=True)
    path /= PurePosixPath(relative).name
    info = _plain(path)
    if info.st_size > limit:
        _fail('source_limit')
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        _fail('source_limit')
    return data


def _atomic_write(root, relative, data):
    relative = _relative(relative)
    path = root / PurePosixPath(relative)
    _plain(path.parent, directory=True)
    _plain(path)
    descriptor, name = tempfile.mkstemp(prefix='.local-skill-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


class LocalCatalog:
    def __init__(self, root):
        self.root = _root(root)
        self._cards = None

    @property
    def revision(self):
        self._load()
        return self._revision

    def _load(self):
        cards = []
        for card in SkillIndex(self.root).cards:
            source = card['source']
            data = _read(self.root, source)
            cards.append({'id': _sha(source.encode()), 'name': card['name'], 'description': card['description'],
                          'path': source, 'available': True, 'sha256': _sha(data), 'text': data})
        self._revision = _sha(_canonical([[card['path'], card['sha256']] for card in cards]))
        self._cards = cards

    def list_skills(self, cursor=None, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            _fail('invalid_limit')
        self._load()
        offset = 0 if cursor is None else cursor
        if type(offset) is not int or not 0 <= offset <= len(self._cards):
            _fail('invalid_cursor')
        items = [{key: card[key] for key in ('id', 'name', 'description', 'path', 'available')} for card in self._cards[offset:offset + limit]]
        end = offset + len(items)
        return {'revision': self._revision, 'items': items, 'next_cursor': end if end < len(self._cards) else None, 'total': len(self._cards)}

    def read_skill(self, skill_id, revision=None):
        self._load()
        if revision != self._revision:
            _fail('revision_mismatch')
        card = next((item for item in self._cards if item['id'] == skill_id), None)
        if card is None:
            _fail('unknown_skill')
        return {'revision': revision, 'skill_id': skill_id, 'path': card['path'], 'name': card['name'],
                'description': card['description'], 'available': True, 'sha256': card['sha256'],
                'size': len(card['text']), 'text': card['text'].decode('utf-8'), 'truncated': False}


class LocalLibrary:
    def __init__(self, root, state_root, *, runner=None):
        self.root = _root(root)
        self.state_root = _root(state_root)
        self.runner = runner or _run
        self._lock = threading.Lock()
        self._closed = False
        self._deadline = time.monotonic() + 900
        self.env = _environment(self.state_root)
        self.options = ['-c', 'credential.interactive=false', '-c', 'core.askPass=',
                        '-c', 'core.fsmonitor=false', '-c', 'core.longpaths=true',
                        '-c', 'core.hooksPath=' + str(self.state_root / 'no-hooks'),
                        '-c', 'protocol.allow=never', '-c', 'protocol.https.allow=always',
                        '-c', 'protocol.file.allow=never', '-c', 'protocol.ext.allow=never',
                        '-c', 'submodule.recurse=false', '-c', 'fetch.fsckObjects=true',
                        '-c', 'http.sslVerify=true', '-c', 'http.followRedirects=false',
                        '-c', 'http.lowSpeedLimit=1024', '-c', 'http.lowSpeedTime=30',
                        '-c', 'maintenance.auto=false', '-c', 'gc.auto=0']

    def close(self):
        self._closed = True

    @contextmanager
    def _action(self):
        if self._closed:
            _fail('cancelled')
        if not self._lock.acquire(blocking=False):
            _fail('sync_busy')
        self._deadline = time.monotonic() + 900
        try:
            yield self
        finally:
            self._lock.release()

    def _check(self):
        if self._closed or time.monotonic() >= self._deadline:
            _fail('cancelled' if self._closed else 'git_timeout')

    def _call(self, repo, *arguments, allowed=(0,)):
        self._check()
        git = __import__('shutil').which('git')
        if not git:
            _fail('git_unavailable')
        result = self.runner([git, *self.options, *arguments], cwd=repo, env=self.env,
                             timeout=min(120, max(1, int(self._deadline - time.monotonic()))),
                             max_output=8 * 1024 * 1024, check=self._check)
        if result.returncode not in allowed:
            _fail('git_failed')
        return result

    def _repository(self):
        _plain(self.root, directory=True)
        top = self._call(self.root, 'rev-parse', '--show-toplevel').stdout.decode().strip()
        if Path(top).absolute() != self.root:
            _fail('source_unavailable')
        origin = self._call(self.root, 'config', '--local', '--get', 'remote.origin.url').stdout.decode().strip()
        return _url(origin)

    def _identity(self):
        options = []
        for key in ('user.name', 'user.email'):
            value = self._call(self.root, 'config', '--get', key, allowed=(0, 1)).stdout.decode('utf-8', 'replace').strip()
            if not value:
                git = __import__('shutil').which('git')
                if not git:
                    _fail('git_unavailable')
                result = self.runner([git, 'config', '--global', '--get', key], cwd=self.root,
                                     env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}, timeout=10,
                                     max_output=4096, check=self._check)
                if result.returncode not in (0, 1):
                    _fail('git_failed')
                value = result.stdout.decode('utf-8', 'replace').strip()
            if value:
                options.extend(['-c', key + '=' + value])
        return options

    def _changes(self):
        raw = self._call(self.root, 'status', '--porcelain=v1', '-z', '--untracked-files=all').stdout
        items = []
        for record in raw.split(b'\0'):
            if not record:
                continue
            try:
                code = record[:2].decode('ascii')
                path = record[3:].decode('utf-8')
                _relative(path)
            except (UnicodeError, ValueError):
                _fail('invalid_path')
            items.append({'path': path, 'code': code, 'excluded': bool(_private_path(path))})
        return items

    def _status(self):
        result = {'root': str(self.root), 'available': self.root.is_dir(),
                  'accessible': library_root_accessible(self.root), 'git': True,
                  'origin': '', 'revision': None, 'changes': [], 'excluded_count': 0}
        if not result['available']:
            result['git'] = False
            return result
        try:
            result['origin'] = self._repository()
            result['revision'] = self._call(self.root, 'rev-parse', 'HEAD').stdout.decode().strip()
            changes = self._changes()
            result['changes'] = changes
            result['excluded_count'] = sum(item['excluded'] for item in changes)
        except LocalLibraryError:
            result['git'] = False
        return result

    def status(self):
        with self._action():
            return self._status()

    @staticmethod
    def _fingerprint(changes):
        return _sha(_canonical(changes))

    def pull(self):
        with self._action():
            self._repository()
            if self._changes():
                _fail('dirty_worktree')
            branch = self._call(self.root, 'symbolic-ref', '--quiet', '--short', 'HEAD').stdout.decode().strip()
            if not branch or branch.startswith('-'):
                _fail('invalid_revision')
            self._call(self.root, 'fetch', '--quiet', '--no-tags', '--no-recurse-submodules', 'origin',
                       '+refs/heads/' + branch + ':refs/remotes/origin/mylib-local')
            remote = self._call(self.root, 'rev-parse', '--verify', 'refs/remotes/origin/mylib-local^{commit}').stdout.decode().strip()
            local = self._call(self.root, 'rev-parse', 'HEAD').stdout.decode().strip()
            if self._call(self.root, 'merge-base', '--is-ancestor', local, remote, allowed=(0, 1)).returncode:
                _fail('non_fast_forward')
            if local != remote:
                self._call(self.root, 'merge', '--ff-only', '--no-edit', remote)
            return {**self._status(), 'changed': local != remote, 'previous_revision': local}

    def commit_preview(self):
        with self._action():
            self._repository()
            changes = self._changes()
            allowed = [item['path'] for item in changes if not item['excluded']]
            stat = self._call(self.root, 'diff', '--stat', '--', *allowed).stdout.decode('utf-8', 'replace') if allowed else ''
            return {'items': changes, 'stat': stat[-8192:], 'fingerprint': self._fingerprint(changes),
                    'can_commit': bool(allowed), 'excluded_count': sum(item['excluded'] for item in changes)}

    def commit_and_push(self, fingerprint, message='Update local skills'):
        if not isinstance(fingerprint, str) or len(fingerprint) != 64 or not isinstance(message, str) or not message.strip() or len(message) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in message):
            _fail('invalid_config')
        with self._action():
            self._repository()
            changes = self._changes()
            if self._fingerprint(changes) != fingerprint:
                _fail('changes_changed')
            allowed = [item['path'] for item in changes if not item['excluded']]
            if not allowed:
                _fail('no_changes')
            self._call(self.root, 'add', '--', *allowed)
            if self._call(self.root, 'diff', '--cached', '--quiet', allowed=(0, 1)).returncode == 0:
                _fail('no_changes')
            self._call(self.root, *self._identity(), 'commit', '-m', message)
            branch = self._call(self.root, 'symbolic-ref', '--quiet', '--short', 'HEAD').stdout.decode().strip()
            if not branch or branch.startswith('-'):
                _fail('invalid_revision')
            self._call(self.root, 'push', 'origin', branch)
            return self._status()

    def apply_candidate(self, source, candidate):
        if not isinstance(source, dict) or set(source) != {'revision', 'skill_id', 'path', 'sha256'} or not isinstance(candidate, bytes):
            _fail('invalid_signal')
        _relative(source['path'])
        current = _read(self.root, source['path'])
        if _sha(current) != source['sha256']:
            _fail('source_drift')
        _atomic_write(self.root, source['path'], candidate)
        return {'path': source['path'], 'sha256': _sha(candidate), 'requires_commit': True}

    def run_evolution(self, request):
        if not isinstance(request, dict) or not isinstance(request.get('text'), str) or len(request['text']) > 32768:
            _fail('invalid_signal')
        target = request.get('target', '当前窗口')
        if not isinstance(target, str) or len(target) > 160 or any(ord(c) < 32 or ord(c) == 127 for c in target):
            _fail('invalid_signal')
        with self._action():
            catalog = LocalCatalog(self.root)
            review_root = self.state_root / hashlib.sha256(str(self.root).encode()).hexdigest()[:16]
            service = EvolutionService(review_root, self.root / 'skills' / 'meta-rsi', catalog)
            try:
                cards = catalog.list_skills(limit=100)['items']
                recommended = SkillIndex(self.root).recommend(request['text'])['skills']
                source = None
                for card in recommended:
                    match = next((item for item in cards if item['path'] == card['source']), None)
                    if match:
                        detail = catalog.read_skill(match['id'], revision=catalog.revision)
                        source = {'revision': detail['revision'], 'skill_id': detail['skill_id'],
                                  'path': detail['path'], 'sha256': detail['sha256']}
                        break
                failures = evolution_suggestions(request['text'])
                evidence = failures[0]['evidence'] if failures else []
                note = json.dumps({'target': target, 'observed': evidence[:3], 'source': 'current-window'},
                                  ensure_ascii=False)[:2000]
                service.record_signal('failure' if failures else 'request', target, note, source)
                summary = service.run_once()
                suggestions = service.list_suggestions(20)
                for item in suggestions['items']:
                    item['candidate_text'] = _read(service.project, item['proposal']['artifact'], 70000).decode('utf-8')
                return {'summary': summary, **suggestions, 'status': service.status()}
            except EvolutionError as error:
                _fail(str(error))
            finally:
                service.close()

    def apply_evolution(self, suggestion_id):
        if not isinstance(suggestion_id, str) or len(suggestion_id) != 64 or any(c not in '0123456789abcdef' for c in suggestion_id):
            _fail('invalid_signal')
        with self._action():
            review_root = self.state_root / hashlib.sha256(str(self.root).encode()).hexdigest()[:16]
            catalog = LocalCatalog(self.root)
            service = EvolutionService(review_root, self.root / 'skills' / 'meta-rsi', catalog)
            try:
                item = next((item for item in service.list_suggestions(100)['items'] if item['suggestion_id'] == suggestion_id), None)
                if item is None:
                    _fail('unknown_suggestion')
                proposal = item['proposal']
                candidate = _read(service.project, proposal['artifact'], 70000)
                if _sha(candidate) != proposal['sha256']:
                    _fail('state_invalid')
                return self.apply_candidate(item['source'], candidate)
            except EvolutionError as error:
                _fail(str(error))
            finally:
                service.close()
