"""Path safety, root limits, directory listing and source scanning. No sockets."""
from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

RESERVED = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
_BAD_CHARS = re.compile(r'[\x00-\x1f<>:"|?*\\]')
WORK_DIR = '.sma-p2p'
PART_SUFFIX = '.sma-part'


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not name or len(name) > 255 or name in ('.', '..'):
        raise ValueError('名称无效')
    if _BAD_CHARS.search(name) or name[-1] in ' .':
        raise ValueError('名称含有不允许的字符')
    if name.split('.')[0].upper() in RESERVED:
        raise ValueError('名称是 Windows 保留名')
    return name


def validate_rel(rel: str) -> str:
    """A manifest path: relative, '/' separated, nothing that can climb out."""
    if not isinstance(rel, str) or not rel or len(rel) > 4096 or rel.startswith('/'):
        raise ValueError('相对路径无效')
    parts = rel.split('/')
    for part in parts:
        validate_name(part)
    if parts[0] == WORK_DIR or parts[-1].endswith(PART_SUFFIX):
        raise ValueError('路径与传输工作文件冲突')
    return rel


def default_roots() -> list[str]:
    if sys.platform == 'win32':
        import ctypes
        roots = []
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for index in range(26):
            if mask & (1 << index):
                drive = f'{chr(65 + index)}:\\'
                if ctypes.windll.kernel32.GetDriveTypeW(drive) == 3:  # DRIVE_FIXED
                    roots.append(drive)
        return roots
    return [str(Path.home())]


def _norm(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def within(path: str | Path, root: str | Path) -> bool:
    p, r = _norm(path), _norm(root)
    if p == r:
        return True
    return p.startswith(r if r.endswith(os.sep) else r + os.sep)


def check_allowed(path: str, roots: list[str]) -> Path:
    if not isinstance(path, str) or not path or len(path) > 4096 or '\x00' in path:
        raise PermissionError('路径无效')
    resolved = Path(os.path.abspath(path))
    if not any(within(resolved, root) for root in roots):
        raise PermissionError('路径不在允许的根目录内')
    return resolved


def safe_join(base: Path, rel: str) -> Path:
    validate_rel(rel)
    target = Path(os.path.abspath(base / Path(*rel.split('/'))))
    if not within(target, base) or _norm(target) == _norm(base):
        raise ValueError('路径越界')
    return target


def _is_link(entry: os.DirEntry) -> bool:
    if entry.is_symlink():
        return True
    isjunction = getattr(os.path, 'isjunction', None)
    return bool(isjunction and isjunction(entry.path))


def list_dir(path: str, roots: list[str]) -> dict:
    """Empty path lists the allowed roots."""
    if not path:
        return {'path': '', 'parent': None, 'entries': [
            {'name': root, 'path': root, 'dir': True, 'size': 0, 'mtime': 0} for root in roots]}
    target = check_allowed(path, roots)
    if not target.is_dir():
        raise NotADirectoryError('不是文件夹')
    entries = []
    with os.scandir(target) as items:
        for entry in items:
            if len(entries) >= 5000:
                break
            try:
                if _is_link(entry) or entry.name == WORK_DIR or entry.name.endswith(PART_SUFFIX):
                    continue
                info = entry.stat(follow_symlinks=False)
                hidden = getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_SYSTEM', 0)
                if hidden:
                    continue
                directory = stat.S_ISDIR(info.st_mode)
                entries.append({'name': entry.name, 'path': entry.path, 'dir': directory,
                                'size': 0 if directory else info.st_size, 'mtime': int(info.st_mtime)})
            except OSError:
                continue
    entries.sort(key=lambda e: (not e['dir'], e['name'].lower()))
    parent = str(target.parent) if target.parent != target else ''
    if parent and not any(within(parent, root) for root in roots):
        parent = ''
    return {'path': str(target), 'parent': parent, 'entries': entries}


def make_dir(path: str, name: str, roots: list[str]) -> str:
    parent = check_allowed(path, roots)
    target = parent / validate_name(name)
    target.mkdir(exist_ok=True)
    return str(target)


def scan(source: str, roots: list[str] | None = None, limit: int = 500_000) -> dict:
    """Manifest of a file or a directory tree. Links are skipped, never followed."""
    src = Path(os.path.abspath(source))
    if roots is not None:
        check_allowed(str(src), roots)
    info = os.stat(src, follow_symlinks=False)
    if stat.S_ISREG(info.st_mode):
        return {'name': validate_name(src.name), 'single': True, 'root': str(src.parent),
                'files': [[src.name, info.st_size, info.st_mtime_ns]], 'dirs': [], 'skipped': 0,
                'bytes': info.st_size}
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError('只能发送文件或文件夹')
    files, dirs, skipped, total = [], [], 0, 0
    stack = [(src, '')]
    while stack:
        current, prefix = stack.pop()
        try:
            items = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError:
            skipped += 1
            continue
        for entry in items:
            rel = prefix + entry.name
            try:
                if _is_link(entry) or entry.name == WORK_DIR or entry.name.endswith(PART_SUFFIX):
                    skipped += 1
                    continue
                validate_rel(rel)
                st = entry.stat(follow_symlinks=False)
            except (OSError, ValueError):
                skipped += 1
                continue
            if stat.S_ISDIR(st.st_mode):
                dirs.append(rel)
                stack.append((Path(entry.path), rel + '/'))
            elif stat.S_ISREG(st.st_mode):
                files.append([rel, st.st_size, st.st_mtime_ns])
                total += st.st_size
            else:
                skipped += 1
            if len(files) + len(dirs) > limit:
                raise ValueError(f'文件数超过 {limit}')
    return {'name': validate_name(src.name), 'single': False, 'root': str(src), 'files': files,
            'dirs': dirs, 'skipped': skipped, 'bytes': total}
