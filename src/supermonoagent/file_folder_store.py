"""Host-owned, bounded folder receiving roots, independent of transport/jobs.

The caller authenticates and pins the project/session before creating a batch.
It supplies an existing trusted exchange directory and an allocated root name;
neither comes from a client-supplied destination path. Directory identity checks
share TransferStore's trusted-local-directory assumption. They are not a defense
against an administrator racing individual filesystem calls.

Only the caller's private journal may supply snapshots to reopen(). Snapshot data
does not grant authority to a project, session or absolute destination. Recheck
those bindings in the finish callback. No method recursively deletes a root or
rolls back files already published.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import threading

from .file_folders import MAX_FILE_BYTES, manifest_digest, validate_manifest
from .file_transfer import TransferStore, _plain_directory, _validate_name
from .file_workspaces import _path


_MAX_SNAPSHOT_BYTES = 256 * 1024
_IDENTITY_MAX = (1 << 128) - 1
_MAX_ACTIVE = 4


def _name(value):
    if not isinstance(value, str) or len(value) > 200:
        raise ValueError('Invalid folder root name')
    _validate_name(value)
    if value.casefold() == '.supermono-partials':
        raise ValueError('Invalid folder root name')
    return value


def _identity(path):
    try:
        info = _plain_directory(path)
    except (OSError, ValueError):
        raise ValueError('Folder directory changed or unavailable') from None
    if (type(info.st_dev) is not int or type(info.st_ino) is not int
            or not 0 <= info.st_dev <= _IDENTITY_MAX or not 0 < info.st_ino <= _IDENTITY_MAX):
        raise ValueError('Folder directory identity is unavailable')
    return info.st_dev, info.st_ino


def _paths(exchange):
    value = _path(exchange)
    if not isinstance(value, Path):
        raise ValueError('Exchange directory is not on this host')
    return value, (*reversed(value.parents), value)


def _encoded_identity(identity):
    return {'dev': str(identity[0]), 'ino': str(identity[1])}


def _decoded_identity(value):
    if not isinstance(value, dict) or set(value) != {'dev', 'ino'}:
        raise ValueError('Invalid folder identity snapshot')
    result = []
    for field in ('dev', 'ino'):
        text = value[field]
        if (not isinstance(text, str) or len(text) > 39 or not re.fullmatch(r'0|[1-9][0-9]{0,38}', text)
                or int(text) > _IDENTITY_MAX or field == 'ino' and int(text) == 0):
            raise ValueError('Invalid folder identity snapshot')
        result.append(int(text))
    return tuple(result)


class FolderReceiveStore:
    """Folder hierarchy plus at most four active files across all leaf stores.

    A failed append or finish aborts that entry's owned partial and releases its
    quota. Cancellation/close keep all confirmed files and the incomplete root.
    Job states and receipt persistence belong to the receiving helper.
    """

    def __init__(self, exchange, name, manifest, ancestors, directories, partials=None):
        self.exchange = exchange
        self.root = exchange / name
        self._manifest = manifest
        self._digest = manifest_digest(manifest)
        self._ancestors = ancestors
        self._directories = directories
        self._partials = partials or {}
        self._stores = {}
        self._active = {}
        self._lock = threading.RLock()
        self._closed = False

    @classmethod
    def create(cls, exchange_dir, name, manifest):
        value = validate_manifest(manifest)
        name = _name(name)
        exchange, paths = _paths(exchange_dir)
        ancestors = {path: _identity(path) for path in paths}
        root = exchange / name
        # Exact exclusive create: the host allocates/retries timestamped names.
        root.mkdir()
        directories = {(): _identity(root)}
        store = cls(exchange, name, value, ancestors, directories)
        store.check()
        for parts in sorted(map(tuple, value['directories']), key=lambda path: (len(path), path)):
            store.check()
            path = root.joinpath(*parts)
            path.mkdir()
            directories[parts] = _identity(path)
            store.check()
        return store

    @classmethod
    def reopen(cls, exchange_dir, snapshot, manifest):
        value = validate_manifest(manifest)
        try:
            # Check exact structure/counts first, without serializing a possibly
            # corrupt, oversized journal object. All accepted values below are
            # bounded strings/lists; no caller-owned containers are retained.
            saved = snapshot
            if (not isinstance(saved, dict)
                    or set(saved) != {'version', 'name', 'manifest_digest', 'ancestors', 'directories', 'partials'}
                    or type(saved['version']) is not int or saved['version'] != 1
                    or saved['manifest_digest'] != manifest_digest(value)):
                raise ValueError()
            name = _name(saved['name'])
            exchange, paths = _paths(exchange_dir)
            identities = saved['ancestors']
            if not isinstance(identities, list) or len(identities) != len(paths):
                raise ValueError()
            ancestors = {path: _decoded_identity(item) for path, item in zip(paths, identities)}
            expected = sorted([(), *map(tuple, value['directories'])])
            entries = saved['directories']
            if not isinstance(entries, list) or len(entries) != len(expected):
                raise ValueError()
            directories = {}
            for entry, parts in zip(entries, expected):
                if (not isinstance(entry, dict) or set(entry) != {'path', 'dev', 'ino'}
                        or entry['path'] != list(parts)):
                    raise ValueError()
                directories[parts] = _decoded_identity({'dev': entry['dev'], 'ino': entry['ino']})
            partials = {}
            entries = saved['partials']
            parents = {tuple(entry['path'][:-1]) for entry in value['files']}
            if not isinstance(entries, list) or len(entries) > len(parents):
                raise ValueError()
            for entry in entries:
                if (not isinstance(entry, dict) or set(entry) != {'path', 'dev', 'ino'}
                        or not isinstance(entry['path'], list) or len(entry['path']) > 16
                        or any(not isinstance(part, str) or len(part) > 200 for part in entry['path'])):
                    raise ValueError()
                parts = tuple(entry['path'])
                if parts not in parents or parts in partials:
                    raise ValueError()
                partials[parts] = _decoded_identity({'dev': entry['dev'], 'ino': entry['ino']})
            store = cls(exchange, name, value, ancestors, directories, partials)
            store.check()
            store.snapshot()  # Also enforce the bounded journal representation.
            return store
        except (ValueError, TypeError, KeyError, OverflowError, UnicodeError, RecursionError):
            raise ValueError('Invalid or changed folder snapshot') from None

    def check(self):
        """Revalidate every ancestor/manifest directory, including empty siblings."""
        with self._lock:
            if self._closed:
                raise ValueError('Folder receiving store is closed')
            for path, expected in self._ancestors.items():
                if _identity(path) != expected:
                    raise ValueError('Folder directory changed')
            for parts, expected in self._directories.items():
                if _identity(self.root.joinpath(*parts)) != expected:
                    raise ValueError('Folder directory changed')
            for parts, expected in self._partials.items():
                if _identity(self.root.joinpath(*parts) / '.supermono-partials') != expected:
                    raise ValueError('Folder partial directory changed')
            for store in self._stores.values():
                store._check_directories()
            return True

    def snapshot(self):
        with self._lock:
            self.check()
            value = {'version': 1, 'name': self.root.name, 'manifest_digest': self._digest,
                     'ancestors': [_encoded_identity(item) for item in self._ancestors.values()],
                     'directories': [{'path': list(parts), **_encoded_identity(identity)}
                                     for parts, identity in sorted(self._directories.items())],
                     'partials': [{'path': list(parts), **_encoded_identity(identity)}
                                  for parts, identity in sorted(self._partials.items())]}
            if len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > _MAX_SNAPSHOT_BYTES:
                raise ValueError('Folder snapshot is too large')
            return value

    def _entry(self, index):
        if type(index) is not int or not 0 <= index < len(self._manifest['files']):
            raise ValueError('Invalid folder entry index')
        return self._manifest['files'][index]

    def entry_path(self, index):
        with self._lock:
            entry = self._entry(index)
            self.check()
            return self.root.joinpath(*entry['path'])

    def begin(self, owner, session, index):
        with self._lock:
            entry = self._entry(index)
            self.check()
            if any(not isinstance(item, str) or not item or len(item) > 256 for item in (owner, session)):
                raise ValueError('Invalid owner or session')
            if len(self._active) >= _MAX_ACTIVE:
                raise ValueError('Maximum active folder transfers reached')
            if any(value[1] == index for value in self._active.values()):
                raise ValueError('Folder entry transfer is already active')
            parent = tuple(entry['path'][:-1])
            store = self._stores.get(parent)
            if store is None:
                if parent not in self._partials:
                    partial = self.root.joinpath(*parent) / '.supermono-partials'
                    # A new batch must not adopt unknown pre-existing partials.
                    partial.mkdir()
                    self._partials[parent] = _identity(partial)
                    self.check()
                store = TransferStore(self.root.joinpath(*parent), max_file_size=MAX_FILE_BYTES, max_active=_MAX_ACTIVE)
                self.check()
                self._stores[parent] = store
            transfer = store.begin(owner, session, entry['path'][-1], entry['size'], entry['sha256'])
            self._active[transfer] = owner, index, store
            return transfer

    def _lookup(self, owner, transfer):
        result = self._active.get(transfer) if isinstance(transfer, str) else None
        if result is None or result[0] != owner:
            raise ValueError('Unknown folder transfer or wrong owner')
        return result

    def _abort(self, owner, transfer, store):
        try:
            store.cancel(owner, transfer)
        except (OSError, ValueError):
            # TransferStore can already have retired its partial on a failure.
            pass
        self._active.pop(transfer, None)

    def append(self, owner, transfer, offset, data):
        with self._lock:
            _, _, store = self._lookup(owner, transfer)
            try:
                self.check()
                return store.append(owner, transfer, offset, data)
            except BaseException:
                self._abort(owner, transfer, store)
                raise

    def finish(self, owner, transfer, can_publish=None):
        with self._lock:
            _, _, store = self._lookup(owner, transfer)
            def allowed():
                self.check()
                if can_publish is not None and not can_publish():
                    return False
                self.check()
                return True
            try:
                self.check()
                result = store.finish(owner, transfer, can_publish=allowed, strict_name=True)
            except BaseException:
                self._abort(owner, transfer, store)
                raise
            self._active.pop(transfer)
            return result

    def cancel(self, owner, transfer):
        with self._lock:
            _, _, store = self._lookup(owner, transfer)
            self._abort(owner, transfer, store)

    def close(self):
        with self._lock:
            for transfer, (owner, _, store) in list(self._active.items()):
                self._abort(owner, transfer, store)
            self._closed = True
