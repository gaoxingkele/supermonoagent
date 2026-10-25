"""Bounded receiver for files, independent of PTYs and network transports.

The caller authenticates the owner and validates the target session before begin.
The configured receive directory is trusted and must not be writable by hostile
local users. Transfers never accept a caller-provided destination path.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat
import threading
from typing import BinaryIO


MAX_CHUNK = 96 * 1024


@dataclass
class _Transfer:
    owner: str
    session: str
    name: str
    size: int
    sha256: str
    path: Path
    stream: BinaryIO
    digest: object
    received: int = 0


def _plain_directory(path: Path) -> os.stat_result:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, 'st_file_attributes', 0) & 0x400):
        raise ValueError('Receive directory must not be a link or reparse point')
    return info


def _validate_name(name: str) -> None:
    if (not isinstance(name, str) or not name or name[-1] in '. '
            or any(ord(c) < 32 or ord(c) == 127 or c in '/\\:<>"|?*' for c in name)):
        raise ValueError('Invalid Windows filename')
    try:
        units = len(name.encode('utf-16-le')) // 2
    except UnicodeEncodeError:
        raise ValueError('Invalid Windows filename') from None
    base = name.split('.')[0].rstrip(' ').upper()
    if units > 200 or re.fullmatch(r'CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³]', base):
        raise ValueError('Invalid Windows filename')


class TransferStore:
    """Thread-safe transfer registry, with a global active-transfer quota."""

    def __init__(self, root, max_file_size=512 * 1024 * 1024, max_active=4):
        if type(max_file_size) is not int or max_file_size < 0:
            raise ValueError('Invalid maximum file size')
        if type(max_active) is not int or not 1 <= max_active <= 1024:
            raise ValueError('Invalid active transfer limit')
        path = Path(os.path.abspath(root))
        for ancestor in (*reversed(path.parents), path):
            if ancestor.exists() or ancestor.is_symlink():
                _plain_directory(ancestor)
        path.mkdir(parents=True, exist_ok=True)
        self.root = path
        self._partial = path / '.supermono-partials'
        self._partial.mkdir(exist_ok=True)
        self._identities = {
            directory: (info.st_dev, info.st_ino)
            for directory in (*reversed(self.root.parents), self.root, self._partial)
            for info in [_plain_directory(directory)]
        }
        self.max_file_size = max_file_size
        self.max_active = max_active
        self._lock = threading.RLock()
        self._transfers: dict[str, _Transfer] = {}
        self._published_partials = set()

    def _check_directories(self):
        for path, identity in self._identities.items():
            try:
                info = _plain_directory(path)
            except OSError:
                raise ValueError('Receive directory changed') from None
            if (info.st_dev, info.st_ino) != identity:
                raise ValueError('Receive directory changed')

    def _lookup(self, owner, transfer_id):
        transfer = self._transfers.get(transfer_id) if isinstance(transfer_id, str) else None
        if transfer is None or transfer.owner != owner:
            raise ValueError('Unknown transfer or wrong owner')
        return transfer

    def begin(self, owner, session, name, size, sha256):
        _validate_name(name)
        if any(not isinstance(value, str) or not value or len(value) > 256 for value in (owner, session)):
            raise ValueError('Invalid owner or session')
        if type(size) is not int or not 0 <= size <= self.max_file_size:
            raise ValueError('Invalid or excessive file size')
        if not isinstance(sha256, str) or not re.fullmatch('[a-fA-F0-9]{64}', sha256):
            raise ValueError('Invalid SHA-256')
        with self._lock:
            self._check_directories()
            self.cleanup_published()
            if len(self._published_partials) >= 256:
                raise ValueError('Published-file cleanup is pending')
            if len(self._transfers) >= self.max_active:
                raise ValueError('Maximum active transfers reached')
            transfer_id = secrets.token_hex(24)
            path = self._partial / (transfer_id + '.part')
            stream = path.open('xb')
            self._transfers[transfer_id] = _Transfer(owner, session, name, size,
                sha256.lower(), path, stream, hashlib.sha256())
            return transfer_id

    def append(self, owner, transfer_id, offset, data):
        with self._lock:
            transfer = self._lookup(owner, transfer_id)
            self._check_directories()
            if type(offset) is not int or offset != transfer.received:
                raise ValueError('Invalid chunk offset')
            if not isinstance(data, bytes) or len(data) > MAX_CHUNK:
                raise ValueError('Invalid or excessive chunk')
            if transfer.received + len(data) > transfer.size:
                raise ValueError('Chunk exceeds declared size')
            try:
                if transfer.stream.write(data) != len(data):
                    raise OSError('Incomplete file write')
            except OSError:
                self.cancel(owner, transfer_id)
                raise
            transfer.digest.update(data)
            transfer.received += len(data)
            return transfer.received

    def finish(self, owner, transfer_id, can_publish=None, *, strict_name=False):
        if type(strict_name) is not bool:
            raise ValueError('Invalid strict filename policy')
        with self._lock:
            transfer = self._lookup(owner, transfer_id)
            self._check_directories()
            if transfer.received != transfer.size:
                raise ValueError('Transfer is incomplete')
            if transfer.digest.hexdigest() != transfer.sha256:
                self.cancel(owner, transfer_id)
                raise ValueError('SHA-256 mismatch')
            try:
                transfer.stream.flush()
                os.fsync(transfer.stream.fileno())
                transfer.stream.close()
            except OSError:
                self.cancel(owner, transfer_id)
                raise
            # Hard-link publication is atomic and never overwrites a destination.
            # Both paths share a filesystem; unsupported filesystems fail safely.
            stem, suffix = Path(transfer.name).stem, Path(transfer.name).suffix
            try:
                if can_publish is not None and not can_publish():
                    raise ValueError('Target session is no longer authorized')
                for number in range(10000):
                    name = transfer.name if number == 0 else f'{stem} ({number}){suffix}'
                    target = self.root / name
                    try:
                        os.link(transfer.path, target)
                    except FileExistsError:
                        if strict_name:
                            raise ValueError('Target file already exists') from None
                        continue
                    break
                else:
                    raise ValueError('Too many filename collisions')
            except BaseException:
                self.cancel(owner, transfer_id)
                raise
            self._transfers.pop(transfer_id)
            try:
                transfer.path.unlink()
            except OSError:
                self._published_partials.add(transfer.path)
            return str(target)

    def cleanup_published(self):
        with self._lock:
            self._check_directories()
            removed = 0
            for path in list(self._published_partials):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    continue
                self._published_partials.remove(path)
                removed += 1
            return removed

    def cancel(self, owner, transfer_id):
        with self._lock:
            transfer = self._lookup(owner, transfer_id)
            # A replaced directory must never redirect cleanup to an unrelated
            # path. Still release the live file handle and registry quota.
            try:
                self._check_directories()
            except (ValueError, OSError):
                safe_to_unlink = False
            else:
                safe_to_unlink = True
            try:
                transfer.stream.close()
            finally:
                self._transfers.pop(transfer_id)
            if safe_to_unlink:
                transfer.path.unlink(missing_ok=True)

    def cancel_owner(self, owner):
        with self._lock:
            ids = [key for key, value in self._transfers.items() if value.owner == owner]
            for transfer_id in ids:
                try:
                    self.cancel(owner, transfer_id)
                except OSError:
                    # The owned handle/registry entry was already released;
                    # continue releasing the owner's other live handles.
                    pass
            return len(ids)
