"""Bounded, portable folder inventories; no filesystem or authorization effects.

Paths are arrays of original components, never absolute destinations. Callers
must separately bind a validated inventory to an authenticated host and project.
The receiver's private staging component is unsupported, rather than omitted or
renamed silently. Canonicalization sorts entries; it never rewrites names.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata

from .file_transfer import _validate_name


MAX_MANIFEST_BYTES = 64 * 1024
MAX_FILES = 32
MAX_DIRECTORIES = 256
MAX_DEPTH = 16
MAX_PATH_UNITS = 1024
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
_KEYS = {'version', 'root', 'directories', 'files'}
_FILE_KEYS = {'path', 'size', 'sha256'}
_SHA256 = re.compile(r'[a-f0-9]{64}')


def _invalid():
    raise ValueError('Invalid folder manifest')


def _alias(component):
    return unicodedata.normalize('NFC', unicodedata.normalize('NFC', component).casefold())


def _component(value):
    # Bound strings before the shared validator encodes them. UTF-16 may use two
    # units per character; the shared filename rules enforce the 200-unit limit.
    if type(value) is not str or len(value) > 200 or value in {'.', '..'}:
        _invalid()
    _validate_name(value)
    if (any(unicodedata.category(char) == 'Cc' for char in value)
            or _alias(value) == '.supermono-partials'):
        _invalid()


def _path(value):
    if type(value) is not list or not 1 <= len(value) <= MAX_DEPTH:
        _invalid()
    units = len(value) - 1  # Separators count toward the relative path limit.
    for component in value:
        _component(component)
        units += len(component.encode('utf-16-le')) // 2
        if units > MAX_PATH_UNITS:
            _invalid()
    return tuple(value)


def _bounded_json(value):
    """Encode only a schema-checked object and stop at the wire byte budget."""
    chunks = []
    size = 0
    encoder = json.JSONEncoder(sort_keys=True, separators=(',', ':'),
                               ensure_ascii=False, allow_nan=False)
    for chunk in encoder.iterencode(value):
        encoded = chunk.encode('utf-8')
        size += len(encoded)
        if size > MAX_MANIFEST_BYTES:
            _invalid()
        chunks.append(encoded)
    return b''.join(chunks)


def validate_manifest(value):
    """Return a detached, sorted JSON manifest or raise ``ValueError``.

    All shape/count/component limits are checked before serialization. Both
    original parent spelling and NFC/casefold uniqueness are required, so the
    inventory cannot describe two objects which alias on a destination host.
    File indexes always refer to this returned canonical ordering.
    """
    if (type(value) is not dict or len(value) != len(_KEYS)
            or any(type(key) is not str for key in value) or set(value) != _KEYS):
        _invalid()
    if type(value['version']) is not int or value['version'] != 1:
        _invalid()
    _component(value['root'])
    directories, files = value['directories'], value['files']
    if (type(directories) is not list or len(directories) > MAX_DIRECTORIES
            or type(files) is not list or len(files) > MAX_FILES):
        _invalid()

    aliases = set()
    original_directories = set()
    directory_paths = []
    file_entries = []

    def add_path(raw):
        path = _path(raw)
        key = tuple(_alias(component) for component in path)
        if key in aliases:
            _invalid()
        aliases.add(key)
        return path

    for raw in directories:
        path = add_path(raw)
        original_directories.add(path)
        directory_paths.append(path)

    total = 0
    for entry in files:
        if (type(entry) is not dict or len(entry) != len(_FILE_KEYS)
                or any(type(key) is not str for key in entry) or set(entry) != _FILE_KEYS):
            _invalid()
        path = add_path(entry['path'])
        size, digest = entry['size'], entry['sha256']
        if (type(size) is not int or not 0 <= size <= MAX_FILE_BYTES
                or type(digest) is not str or not _SHA256.fullmatch(digest)):
            _invalid()
        total += size
        if total > MAX_TOTAL_BYTES:
            _invalid()
        file_entries.append((path, size, digest))

    for path in [*directory_paths, *(entry[0] for entry in file_entries)]:
        if any(path[:length] not in original_directories for length in range(1, len(path))):
            _invalid()

    # No arbitrary objects, cycles, unbounded lists or oversized components can
    # reach the encoder. Enforce the compact UTF-8 budget before copying output.
    _bounded_json(value)
    return {'version': 1, 'root': value['root'],
            'directories': [list(path) for path in sorted(directory_paths)],
            'files': [{'path': list(path), 'size': size, 'sha256': digest}
                      for path, size, digest in sorted(file_entries)]}


def manifest_digest(value):
    """SHA-256 of validated canonical UTF-8 JSON, including the root label."""
    return hashlib.sha256(_bounded_json(validate_manifest(value))).hexdigest()


def parse_manifest(raw):
    """Parse a bounded UTF-8 bytes payload, rejecting duplicate JSON keys."""
    if type(raw) is not bytes or len(raw) > MAX_MANIFEST_BYTES:
        _invalid()

    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                _invalid()
            result[key] = item
        return result

    def invalid_constant(_value):
        _invalid()

    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                           parse_constant=invalid_constant)
        return validate_manifest(value)
    except (ValueError, UnicodeError, TypeError, RecursionError, OverflowError):
        raise ValueError('Invalid folder manifest') from None
