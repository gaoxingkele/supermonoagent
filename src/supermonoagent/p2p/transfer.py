"""Chunk plans, receiver disk state with a resume journal, sender chunk reads. No sockets."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import threading
from collections import OrderedDict
from pathlib import Path

from .files import PART_SUFFIX, WORK_DIR, safe_join, validate_name, validate_rel

CHUNK = 4 * 1024 * 1024
_RECORD = struct.Struct('<II')
FILE_DONE = 0xFFFFFFFF


def transfer_id(src_peer: str, src_path: str, dst_peer: str, dst_path: str) -> str:
    raw = '\0'.join((src_peer, os.path.normcase(os.path.abspath(src_path)), dst_peer, dst_path))
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def chunk_count(size: int, chunk: int) -> int:
    return (size + chunk - 1) // chunk


def encode_ranges(indices) -> list[list[int]]:
    ranges = []
    for i in sorted(indices):
        if ranges and ranges[-1][1] == i:
            ranges[-1][1] = i + 1
        else:
            ranges.append([i, i + 1])
    return ranges


def decode_ranges(ranges) -> list[int]:
    result = []
    for start, end in ranges:
        if not (isinstance(start, int) and isinstance(end, int) and 0 <= start < end):
            raise ValueError('bad range')
        result.extend(range(start, end))
    return result


def read_chunk(path: str, offset: int, length: int, size: int, mtime_ns: int) -> tuple[bytes, bytes]:
    """Runs in a worker thread; refuses a file that changed after the manifest was made."""
    st = os.stat(path)
    if st.st_size != size or st.st_mtime_ns != mtime_ns:
        raise FileChangedError(path)
    with open(path, 'rb') as handle:
        handle.seek(offset)
        data = handle.read(length)
    if len(data) != length:
        raise FileChangedError(path)
    return data, hashlib.sha256(data).digest()


class FileChangedError(OSError):
    pass


class IncomingTransfer:
    """Receiver side of one transfer. All public methods are thread-safe."""

    def __init__(self, tid: str, dest: str, offer: dict, *, skip_existing: bool = True):
        if not isinstance(tid, str) or len(tid) != 24 or not all(c in '0123456789abcdef' for c in tid):
            raise ValueError('传输号无效')
        self.tid = tid
        self.chunk = int(offer['chunk'])
        if not 64 * 1024 <= self.chunk <= 8 * 1024 * 1024:
            raise ValueError('块大小无效')
        name = validate_name(offer['name'])
        self.single = bool(offer.get('single'))
        dest_dir = Path(os.path.abspath(dest))
        if not dest_dir.is_dir():
            raise NotADirectoryError('目标文件夹不存在')
        self.base = dest_dir if self.single else dest_dir / name
        self.name = name
        files = offer['files']
        dirs = offer.get('dirs', [])
        if not isinstance(files, list) or not isinstance(dirs, list) or len(files) + len(dirs) > 500_000:
            raise ValueError('清单无效')
        self.files: list[tuple[str, int, int]] = []
        seen = set()
        for item in files:
            rel, size, mtime = item
            validate_rel(rel)
            if not (isinstance(size, int) and size >= 0 and isinstance(mtime, int)):
                raise ValueError('清单无效')
            if self.single and '/' in rel:
                raise ValueError('清单无效')
            key = rel.lower()
            if key in seen:
                raise ValueError('清单里有重复文件')
            seen.add(key)
            self.files.append((rel, size, mtime))
        self.dirs = [validate_rel(d) for d in dirs]
        self.paths = [safe_join(self.base, rel) for rel, _, _ in self.files]
        self.work = (dest_dir if self.single else self.base) / WORK_DIR / tid
        self.skip_existing = skip_existing
        self._lock = threading.RLock()
        self._file_locks = [threading.Lock() for _ in self.files]
        self._handles: OrderedDict[int, object] = OrderedDict()
        self._journal = None
        self.done_chunks: list[set[int]] = [set() for _ in self.files]
        self.complete = [False] * len(self.files)
        self.bytes_total = sum(size for _, size, _ in self.files)
        self.bytes_done = 0
        self.skipped_files = 0
        self.closed = False

    # ---------- setup / resume ----------
    def prepare(self) -> dict:
        with self._lock:
            self.base.mkdir(parents=True, exist_ok=True)
            for rel in self.dirs:
                safe_join(self.base, rel).mkdir(parents=True, exist_ok=True)
            self.work.mkdir(parents=True, exist_ok=True)
            manifest_path = self.work / 'manifest.json'
            journal_path = self.work / 'chunks.log'
            previous = None
            if manifest_path.exists():
                try:
                    previous = json.loads(manifest_path.read_text('utf-8'))
                except (OSError, ValueError):
                    previous = None
            old_index = {}
            if previous and previous.get('chunk') == self.chunk:
                for index, (rel, size, mtime) in enumerate(previous.get('files', [])):
                    old_index[index] = (rel, size, mtime)
            old_done: dict[int, set[int]] = {}
            old_complete: set[int] = set()
            if old_index and journal_path.exists():
                raw = journal_path.read_bytes()
                for pos in range(0, len(raw) - len(raw) % _RECORD.size, _RECORD.size):
                    fi, ci = _RECORD.unpack_from(raw, pos)
                    if ci == FILE_DONE:
                        old_complete.add(fi)
                    else:
                        old_done.setdefault(fi, set()).add(ci)
            # Map previous file indices to the new manifest by identity (path, size, mtime).
            by_identity = {v: k for k, v in old_index.items()}
            need = {}
            for index, (rel, size, mtime) in enumerate(self.files):
                final = self.paths[index]
                part = final.with_name(final.name + PART_SUFFIX)
                count = chunk_count(size, self.chunk)
                old = by_identity.get((rel, size, mtime))
                if self.skip_existing and self._matches(final, size, mtime):
                    self.complete[index] = True
                    self.skipped_files += 1 if old is None or old not in old_complete else 0
                    self.bytes_done += size
                    continue
                if old is not None and part.exists() and part.stat().st_size == size:
                    done = {c for c in old_done.get(old, set()) if c < count}
                else:
                    done = set()
                    if part.exists():
                        part.unlink()
                self.done_chunks[index] = done
                self.bytes_done += sum(self._chunk_len(index, c) for c in done)
                if size == 0:
                    final.parent.mkdir(parents=True, exist_ok=True)
                    final.write_bytes(b'')
                    os.utime(final, ns=(mtime, mtime))
                    self.complete[index] = True
                    continue
                missing = [c for c in range(count) if c not in done]
                if not missing:
                    self._finalize(index)
                    continue
                need[index] = encode_ranges(missing)
            manifest_path.write_text(json.dumps({'chunk': self.chunk, 'files': self.files}), 'utf-8')
            # Rewrite the journal so indices refer to the current manifest.
            with open(journal_path, 'wb') as journal:
                for index, done in enumerate(self.done_chunks):
                    if self.complete[index]:
                        journal.write(_RECORD.pack(index, FILE_DONE))
                    else:
                        for c in sorted(done):
                            journal.write(_RECORD.pack(index, c))
            self._journal = open(journal_path, 'ab')
            return {'need': {str(k): v for k, v in need.items()}, 'bytes_done': self.bytes_done,
                    'bytes_total': self.bytes_total, 'skipped_files': self.skipped_files,
                    'base': str(self.base)}

    @staticmethod
    def _matches(path: Path, size: int, mtime: int) -> bool:
        try:
            st = path.stat()
        except OSError:
            return False
        # Many filesystems keep 100ns or 1s precision; compare to the coarser one.
        return st.st_size == size and abs(st.st_mtime_ns - mtime) < 2_000_000_000

    def _chunk_len(self, index: int, c: int) -> int:
        size = self.files[index][1]
        return min(self.chunk, size - c * self.chunk)

    # ---------- data ----------
    def _handle(self, index: int):
        handle = self._handles.get(index)
        if handle is not None:
            self._handles.move_to_end(index)
            return handle
        final = self.paths[index]
        part = final.with_name(final.name + PART_SUFFIX)
        final.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(part, os.O_RDWR | os.O_CREAT | getattr(os, 'O_BINARY', 0), 0o644)
        handle = os.fdopen(fd, 'r+b')
        size = self.files[index][1]
        handle.seek(0, os.SEEK_END)
        if handle.tell() != size:
            handle.truncate(size)
        self._handles[index] = handle
        while len(self._handles) > 64:
            old_index, old = self._handles.popitem(last=False)
            old.close()
        return handle

    def write(self, index: int, offset: int, data: bytes, digest: bytes) -> int:
        """0 ok, 1 checksum, 2 rejected/disk. Runs in a worker thread."""
        if not 0 <= index < len(self.files) or offset % self.chunk:
            return 2
        c = offset // self.chunk
        if c >= chunk_count(self.files[index][1], self.chunk) or len(data) != self._chunk_len(index, c):
            return 2
        if hashlib.sha256(data).digest() != digest:
            return 1
        with self._file_locks[index]:
            with self._lock:
                if self.closed:
                    return 2
                if self.complete[index] or c in self.done_chunks[index]:
                    return 0
                handle = self._handle(index)
            try:
                handle.seek(offset)
                handle.write(data)
            except OSError:
                return 2
            with self._lock:
                self.done_chunks[index].add(c)
                self.bytes_done += len(data)
                self._journal.write(_RECORD.pack(index, c))
                self._journal.flush()
                if len(self.done_chunks[index]) == chunk_count(self.files[index][1], self.chunk):
                    self._finalize(index)
        return 0

    def _finalize(self, index: int) -> None:
        final = self.paths[index]
        part = final.with_name(final.name + PART_SUFFIX)
        handle = self._handles.pop(index, None)
        if handle is not None:
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
        os.replace(part, final)
        mtime = self.files[index][2]
        os.utime(final, ns=(mtime, mtime))
        self.complete[index] = True
        self.done_chunks[index] = set()
        if self._journal is not None:
            self._journal.write(_RECORD.pack(index, FILE_DONE))
            self._journal.flush()

    # ---------- end ----------
    def missing(self) -> int:
        with self._lock:
            return sum(1 for done in self.complete if not done)

    def finish(self) -> dict:
        with self._lock:
            left = self.missing()
            if left:
                return {'complete': False, 'missing_files': left}
            self._close_handles()
            self._remove_work()
            return {'complete': True, 'files': len(self.files), 'bytes': self.bytes_total,
                    'skipped_files': self.skipped_files, 'base': str(self.base)}

    def close(self, discard: bool = False) -> None:
        with self._lock:
            self.closed = True
            self._close_handles()
            if discard:
                for index, path in enumerate(self.paths):
                    if not self.complete[index]:
                        part = path.with_name(path.name + PART_SUFFIX)
                        try:
                            part.unlink()
                        except OSError:
                            pass
                self._remove_work()

    def _close_handles(self) -> None:
        for handle in self._handles.values():
            try:
                handle.close()
            except OSError:
                pass
        self._handles.clear()
        if self._journal is not None:
            self._journal.close()
            self._journal = None

    def _remove_work(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)
        try:
            self.work.parent.rmdir()  # .sma-p2p, only when empty
        except OSError:
            pass


def plan(files: list, need: dict, chunk: int) -> list[tuple[int, int, int]]:
    """(file index, offset, length) for every missing chunk, small files first so they finish early."""
    items = []
    for key, ranges in need.items():
        index = int(key)
        size = files[index][1]
        for c in decode_ranges(ranges):
            offset = c * chunk
            if offset >= size:
                raise ValueError('bad need')
            items.append((index, offset, min(chunk, size - offset)))
    items.sort(key=lambda item: (files[item[0]][1] > chunk, item[0], item[1]))
    return items
