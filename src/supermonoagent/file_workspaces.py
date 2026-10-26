"""Local file-workspace configuration; never changes shells or library contents.

The caller proves window/session ownership. Reads never create settings, project,
library or exchange directories. Only an explicit save writes configuration.
Exchange directories must be true project descendants, never the project itself.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import heapq
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import tempfile
import threading
import time

from .file_transfer import _validate_name


MAX_SETTINGS_BYTES = 1024 * 1024
MAX_REVISION = 9007199254740991
_PATH_LIMIT = 4096
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class InvalidSettings(ValueError):
    """Existing settings are unreadable or invalid; never replace with defaults."""


class ProjectSelectionRequired(ValueError):
    """The user profile root is not an established project directory."""
    def __init__(self):
        super().__init__('project_required')


class RevisionConflict(ValueError):
    def __init__(self, expected, actual):
        super().__init__('File workspace revision conflict')
        self.expected_revision = expected
        self.actual_revision = actual


def _window_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value):
        raise ValueError('Invalid window ID')


def _revision(value):
    if type(value) is not int or not 0 <= value <= MAX_REVISION:
        raise ValueError('Invalid expected revision')


def _utf16_prefix(value, units):
    return value.encode('utf-16-le')[:units * 2].decode('utf-16-le', 'ignore')


def _path(value):
    try:
        text = os.fspath(value)
    except TypeError:
        raise ValueError('Invalid local directory path') from None
    # Reject these before resolve/stat/drive probing: UNC can initiate network
    # authentication, and device namespaces bypass ordinary Win32 path rules.
    if (not isinstance(text, str) or not text or len(text) > _PATH_LIMIT
            or text.startswith(('\\', '//'))
            or any(ord(char) < 32 or ord(char) == 127 for char in text)):
        raise ValueError('Expected an absolute local directory path')
    windows = os.name == 'nt' or bool(re.match(r'^[A-Za-z]:', text))
    path = PureWindowsPath(text) if windows else Path(text)
    if not path.is_absolute() or windows and not re.fullmatch('[A-Za-z]:', path.drive):
        raise ValueError('Expected an absolute local directory path')
    for part in path.parts[1:]:
        if part in ('.', '..') or part.endswith(('.', ' ')) or any(char in part for char in '\\:<>"|?*'):
            raise ValueError('Invalid directory component')
        try:
            if len(part.encode('utf-16-le')) // 2 > 255:
                raise ValueError('Directory component is too long')
            _validate_name(_utf16_prefix(part, 200).rstrip(' .'))
        except UnicodeError:
            raise ValueError('Invalid directory encoding') from None
    return Path(str(path)) if os.name == 'nt' or not windows else path


def _drive_type(anchor):
    import ctypes
    from ctypes import wintypes
    function = ctypes.WinDLL('kernel32', use_last_error=True).GetDriveTypeW
    function.argtypes, function.restype = [wintypes.LPCWSTR], wintypes.UINT
    return function(anchor)


def _directory(value, *, required=False):
    path = _path(value)
    if os.name != 'nt' and isinstance(path, PureWindowsPath):
        if required:
            raise ValueError('Directory is not on this host')
        return str(path), 'missing'
    if os.name == 'nt':
        kind = _drive_type(path.anchor)
        if kind in (0, 1):
            if required:
                raise ValueError('Local drive is unavailable')
            return str(path), 'missing'
        if kind not in (2, 3, 6):  # Removable, fixed and RAM disks; never SMB mappings.
            raise ValueError('Directory must be on a local disk')
    found = False
    for candidate in (*reversed(path.parents), path):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                or not stat.S_ISDIR(info.st_mode)):
            raise ValueError('Directory links, reparse points and files are not accepted')
        if candidate == path:
            found = True
    if required and not found:
        raise ValueError('Directory does not exist')
    return str(path.resolve(strict=False)), 'ready' if found else 'missing'


def _description(value, *, reject_unsafe=False):
    path = str(_path(value))
    try:
        return _directory(path)
    except ValueError:
        if reject_unsafe:
            raise
        return path, 'unavailable'
    except (OSError, RuntimeError):
        return path, 'unavailable'


def _contained_exchange(project, exchange):
    project, exchange = _path(project), _path(exchange)
    if exchange == project or not exchange.is_relative_to(project):
        raise ValueError('Exchange directory must be a true project subdirectory')


def directory_binding(cfg):
    """Host-only immutable path/identity evidence; never creates directories."""
    project, _ = _directory(cfg['project_dir'], required=True)
    exchange, _ = _directory(cfg['exchange_dir'])
    _contained_exchange(project, exchange)
    project, exchange = Path(project), Path(exchange)
    directories = []
    current = project
    for part in (None, *exchange.relative_to(project).parts):
        if part is not None:
            current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400 or info.st_ino <= 0):
            raise ValueError('Invalid project directory identity')
        directories.append([os.path.normcase(str(current)), info.st_dev, info.st_ino])
    if not directories:
        raise ValueError('Project directory identity unavailable')
    return {'project': os.path.normcase(str(project)), 'exchange': os.path.normcase(str(exchange)),
            'directories': directories}


def _is_home(project):
    return _path(project) == Path.home().resolve(strict=False)


def _require_project(project):
    if _is_home(project):
        raise ProjectSelectionRequired()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidSettings('Duplicate settings field')
        result[key] = value
    return result


def _document(value):
    try:
        if (not isinstance(value, dict) or set(value) != {'schema', 'windows', 'mylib'}
                or type(value['schema']) is not int or value['schema'] != 1
                or not isinstance(value['windows'], dict) or len(value['windows']) > 256):
            raise ValueError()
        for window, entry in value['windows'].items():
            _window_id(window)
            if not isinstance(entry, dict) or set(entry) != {'project_dir', 'exchange_dir', 'revision'}:
                raise ValueError()
            _revision(entry['revision'])
            if entry['revision'] == 0:
                raise ValueError()
            _path(entry['project_dir'])
            _path(entry['exchange_dir'])
            _contained_exchange(entry['project_dir'], entry['exchange_dir'])
        library = value['mylib']
        if library is not None:
            if not isinstance(library, dict) or set(library) != {'root', 'revision'}:
                raise ValueError()
            _revision(library['revision'])
            if library['revision'] == 0:
                raise ValueError()
            _path(library['root'])
    except (TypeError, ValueError, KeyError, UnicodeError, RuntimeError):
        raise InvalidSettings('File workspace settings are invalid') from None
    return value


class FileWorkspaces:
    def __init__(self, settings_file, default_mylib_root='D:/aicoding/mylib'):
        self.settings_file = _path(settings_file)
        if not isinstance(self.settings_file, Path):
            raise ValueError('Settings must be local to this host')
        self.default_mylib_root = str(_path(default_mylib_root))
        key = os.path.normcase(str(self.settings_file))
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(key, threading.RLock())

    def _load(self):
        try:
            _directory(self.settings_file.parent)
            try:
                info = self.settings_file.lstat()
            except FileNotFoundError:
                return {'schema': 1, 'windows': {}, 'mylib': None}
            if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise InvalidSettings('Settings must be a regular local file')
            with self.settings_file.open('rb') as stream:
                raw = stream.read(MAX_SETTINGS_BYTES + 1)
            if len(raw) > MAX_SETTINGS_BYTES:
                raise InvalidSettings('Settings exceed size limit')
            return _document(json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique,
                parse_constant=lambda _value: (_ for _ in ()).throw(InvalidSettings('Invalid JSON number'))))
        except (OSError, ValueError, UnicodeError, TypeError, RecursionError, RuntimeError):
            raise InvalidSettings('File workspace settings are unreadable or invalid') from None

    @contextmanager
    def _writing(self):
        # Reject corruption before creating even the separate lock file.
        self._load()
        _directory(self.settings_file.parent)
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        lock = self.settings_file.with_name(self.settings_file.name + '.lock')
        try:
            info = lock.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and (not stat.S_ISREG(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise InvalidSettings('Invalid settings lock')
        with lock.open('a+b') as stream:
            deadline = time.monotonic() + 2
            while True:
                try:
                    if os.name == 'nt':
                        import msvcrt
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ValueError('File workspace settings are busy') from None
                    time.sleep(.02)
            try:
                yield self._load()
            finally:
                if os.name == 'nt':
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _persist(self, document):
        _document(document)
        raw = json.dumps(document, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(raw) > MAX_SETTINGS_BYTES:
            raise InvalidSettings('Settings exceed size limit')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.settings_file.parent, prefix='.file-workspaces-',
                    suffix='.tmp', mode='wb', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.settings_file)
        except OSError:
            raise ValueError('Unable to save file workspace settings') from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def get(self, window_id, session_cwd):
        _window_id(window_id)
        with self._lock:
            entry = self._load()['windows'].get(window_id)
            project = entry['project_dir'] if entry else session_cwd
            project, project_status = _description(project, reject_unsafe=entry is not None)
            if _is_home(project):
                project_status = 'selection_required'
            exchange = entry['exchange_dir'] if entry else str(_path(project) / 'tmp')
            exchange, exchange_status = _description(exchange, reject_unsafe=entry is not None)
            _contained_exchange(project, exchange)
            return {'window_id': window_id, 'project_dir': project, 'exchange_dir': exchange,
                    'source': 'configured' if entry else 'startup', 'revision': entry['revision'] if entry else 0,
                    'project_status': project_status, 'exchange_status': exchange_status}

    def save(self, window_id, project_dir, exchange_dir=None, *, expected_revision):
        _window_id(window_id)
        _revision(expected_revision)
        project, _ = _directory(project_dir, required=True)
        _require_project(project)
        exchange, _ = _directory(exchange_dir if exchange_dir is not None else _path(project) / 'tmp')
        _contained_exchange(project, exchange)
        if os.name != 'nt' and isinstance(_path(exchange), PureWindowsPath):
            raise ValueError('Exchange directory is not on this host')
        _directory(_path(exchange).anchor, required=True)
        with self._lock:
            self.get(window_id, project)  # Reject unsafe legacy paths before lock creation.
            with self._writing() as document:
                self.get(window_id, project)  # Recheck after taking the cross-process lock.
                current = document['windows'].get(window_id, {}).get('revision', 0)
                if current != expected_revision:
                    raise RevisionConflict(expected_revision, current)
                if current == MAX_REVISION:
                    raise ValueError('Revision limit reached')
                document['windows'][window_id] = {'project_dir': project, 'exchange_dir': exchange, 'revision': current + 1}
                self._persist(document)
                return self.get(window_id, project)

    def get_mylib(self):
        with self._lock:
            entry = self._load()['mylib']
            path, status = _description(entry['root'] if entry else self.default_mylib_root)
            return {'mylib_root': path, 'source': 'configured' if entry else 'default',
                    'revision': entry['revision'] if entry else 0, 'status': status}

    def save_mylib(self, root, *, expected_revision):
        _revision(expected_revision)
        path, _ = _directory(root, required=True)
        with self._lock, self._writing() as document:
            current = (document['mylib'] or {}).get('revision', 0)
            if current != expected_revision:
                raise RevisionConflict(expected_revision, current)
            if current == MAX_REVISION:
                raise ValueError('Revision limit reached')
            document['mylib'] = {'root': path, 'revision': current + 1}
            self._persist(document)
            return self.get_mylib()

    def validate_revision(self, window_id, expected_revision):
        _window_id(window_id)
        _revision(expected_revision)
        with self._lock:
            return self._load()['windows'].get(window_id, {}).get('revision', 0) == expected_revision

    def guard(self, window_id, session_cwd, expected_revision):
        """Check immediately before file publication; never creates exchange dirs."""
        _revision(expected_revision)
        with self._lock:
            result = self.get(window_id, session_cwd)
            if result['revision'] != expected_revision:
                raise RevisionConflict(expected_revision, result['revision'])
            _require_project(result['project_dir'])
            _directory(result['project_dir'], required=True)
            _directory(result['exchange_dir'])
            _directory(_path(result['exchange_dir']).anchor, required=True)
            return result

    @contextmanager
    def publish_guard(self, window_id, session_cwd, expected_revision):
        """Hold configuration CAS stable through publication in one worker thread.

        Unlike ordinary reads, this may create the settings parent/lock file.
        It never persists configuration or creates project/library/exchange dirs.
        Enter, publish, and exit on the same thread; never carry this over await.
        """
        _window_id(window_id)
        _revision(expected_revision)
        with self._lock:
            # A bad/missing project must fail before lock-parent creation could
            # accidentally create it when settings were placed beneath it.
            self.guard(window_id, session_cwd, expected_revision)
            with self._writing():
                yield self.guard(window_id, session_cwd, expected_revision)

    def browse(self, path=None, *, offset=0, limit=100):
        if type(offset) is not int or not 0 <= offset <= 100000 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('Invalid directory page')
        if path is None:
            if os.name == 'nt':
                roots = [chr(code) + ':\\' for code in range(ord('A'), ord('Z') + 1)
                         if _drive_type(chr(code) + ':\\') in (2, 3, 6)]
            else:
                roots = ['/']
            return {'path': None, 'parent': None, 'roots': roots, 'directories': [], 'offset': 0, 'next_offset': None}
        canonical, _ = _directory(path, required=True)
        directory = Path(canonical)

        def subdirectories():
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if (stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
                                and not getattr(info, 'st_file_attributes', 0) & 0x400):
                            _path(directory / entry.name)
                            yield entry.name
                    except (OSError, ValueError):
                        continue
        names = heapq.nsmallest(offset + limit + 1, subdirectories(), key=lambda name: (name.casefold(), name))
        selected = names[offset:offset + limit]
        return {'path': canonical, 'parent': str(directory.parent) if directory.parent != directory else None,
                'roots': [], 'directories': [{'name': name, 'path': str(directory / name)} for name in selected],
                'offset': offset, 'next_offset': offset + limit if len(names) > offset + limit else None}


def timestamped_name(original, aware_datetime):
    """Name a new transfer once. Never guess whether a user's name has a date."""
    if (not isinstance(aware_datetime, datetime) or aware_datetime.tzinfo is None
            or aware_datetime.utcoffset() is None):
        raise ValueError('An aware datetime is required')
    if (not isinstance(original, str) or not original or len(original) > 4096
            or original.endswith(('.', ' '))
            or any(ord(char) < 32 or ord(char) == 127 or char in '/\\:<>"|?*' for char in original)):
        raise ValueError('Invalid original filename')
    try:
        _validate_name(_utf16_prefix(original, 200).rstrip(' .'))
        suffix = Path(original).suffix
        stem = original[:-len(suffix)] if suffix else original
        prefix = (f'{aware_datetime.year:04d}{aware_datetime.month:02d}{aware_datetime.day:02d}-'
                  f'{aware_datetime.hour:02d}{aware_datetime.minute:02d}{aware_datetime.second:02d}-')
        # Existing TransferStore caps input names at 200 UTF-16 units. Reserve
        # eight more units for its collision suffix, preserving the extension.
        budget = 192 - len(prefix) - len(suffix.encode('utf-16-le')) // 2
        if budget < 1:
            raise ValueError('Filename extension is too long')
        result = prefix + _utf16_prefix(stem, budget).rstrip(' .') + suffix
        _validate_name(result)
        return result
    except UnicodeError:
        raise ValueError('Invalid filename encoding') from None
