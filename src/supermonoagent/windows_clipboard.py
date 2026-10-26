"""Explicit clipboard actions and immutable source snapshots for desktop paste.

Native API contract references:
https://learn.microsoft.com/en-us/windows/win32/api/shellapi/nf-shellapi-dragqueryfilew
https://learn.microsoft.com/en-us/windows/win32/dataxchg/standard-clipboard-formats
https://learn.microsoft.com/en-us/windows/win32/api/wingdi/ns-wingdi-bitmapinfoheader
https://learn.microsoft.com/en-us/windows/win32/api/wingdi/ns-wingdi-bitmapv4header
https://learn.microsoft.com/en-us/windows/win32/api/wingdi/ns-wingdi-bitmapv5header
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import io
import os
from pathlib import Path
import secrets
import stat
import struct
import threading
from typing import BinaryIO
import zlib


CHUNK_SIZE = 96 * 1024
MAX_IMAGE_BYTES = 128 * 1024 * 1024
MAX_IMAGE_PIXELS = 32 * 1024 * 1024
MAX_TEXT_BYTES = 1024 * 1024  # UTF-16LE payload, excluding its NUL terminator.
_PNG = b'\x89PNG\r\n\x1a\n'


def _png_chunk(kind, payload):
    return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload))


def dib_to_png(data: bytes) -> bytes:
    """Convert common 24-bit RGB and standard-mask 32-bit RGB/RGBA DIBs.

    A 40-byte BI_RGB header has a reserved high byte, so stays opaque. An
    extended header can explicitly describe a standard alpha channel; preserve
    it instead of guessing from pixel values. Unknown masks/profiles fail closed.
    """
    if not isinstance(data, bytes) or not 40 <= len(data) <= MAX_IMAGE_BYTES:
        raise ValueError('Unsupported clipboard bitmap')
    header, width, height, planes, bits, compression, _size, _x, _y, colors, _important = struct.unpack_from('<IiiHHIIiiII', data)
    if (header not in (40, 108, 124) or len(data) < header or width <= 0 or height == 0
            or planes != 1 or bits not in (24, 32) or compression not in (0, 3)
            or compression == 3 and bits != 32
            or colors != 0 or width * abs(height) > MAX_IMAGE_PIXELS):
        raise ValueError('Unsupported clipboard bitmap encoding')
    standard = (0x00FF0000, 0x0000FF00, 0x000000FF)
    offset, alpha = header, False
    if header == 40 and compression == 3:
        if len(data) < 52:
            raise ValueError('Truncated clipboard bitmap masks')
        if struct.unpack_from('<III', data, 40) != standard:
            raise ValueError('Unsupported clipboard bitmap masks')
        offset = 52
    elif header in (108, 124):
        red, green, blue, alpha_mask = struct.unpack_from('<IIII', data, 40)
        masks = red, green, blue
        implicit_rgb = compression == 0 and masks == (0, 0, 0) and alpha_mask == 0
        if (not implicit_rgb and masks != standard or alpha_mask not in (0, 0xFF000000)
                or bits != 32 and alpha_mask != 0):
            raise ValueError('Unsupported clipboard bitmap masks')
        alpha = alpha_mask == 0xFF000000
        color_space = struct.unpack_from('<I', data, 56)[0]
        if (color_space not in (0, 0x73524742, 0x57696E20)
                or color_space == 0 and any(data[60:108])
                or header == 124 and any(struct.unpack_from('<II', data, 112))):
            raise ValueError('Unsupported clipboard bitmap color profile')
    stride = ((width * bits + 31) // 32) * 4
    rows = abs(height)
    if len(data) < offset + stride * rows:
        raise ValueError('Truncated clipboard bitmap')
    compressor = zlib.compressobj()
    encoded = bytearray()
    step = bits // 8
    for y in range(rows):
        source_y = rows - y - 1 if height > 0 else y
        start = offset + source_y * stride
        row = data[start:start + width * step]
        channels = 4 if alpha else 3
        rgb = bytearray(width * channels)
        rgb[0::channels], rgb[1::channels], rgb[2::channels] = row[2::step], row[1::step], row[0::step]
        if alpha:
            rgb[3::channels] = row[3::step]
        encoded.extend(compressor.compress(b'\0' + rgb))
    encoded.extend(compressor.flush())
    ihdr = struct.pack('>IIBBBBB', width, rows, 8, 6 if alpha else 2, 0, 0, 0)
    return _PNG + _png_chunk(b'IHDR', ihdr) + _png_chunk(b'IDAT', bytes(encoded)) + _png_chunk(b'IEND', b'')


def _native_apis():
    if os.name != 'nt':
        raise OSError('Native clipboard is only available on Windows')
    import ctypes
    from ctypes import wintypes
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    declarations = [
        (user.OpenClipboard, [wintypes.HWND], wintypes.BOOL),
        (user.CloseClipboard, [], wintypes.BOOL),
        (user.IsClipboardFormatAvailable, [wintypes.UINT], wintypes.BOOL),
        (user.GetClipboardData, [wintypes.UINT], wintypes.HANDLE),
        (user.EmptyClipboard, [], wintypes.BOOL),
        (user.SetClipboardData, [wintypes.UINT, wintypes.HANDLE], wintypes.HANDLE),
        (user.CreateWindowExW, [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
            wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p], wintypes.HWND),
        (user.DestroyWindow, [wintypes.HWND], wintypes.BOOL),
        (user.RegisterClipboardFormatW, [wintypes.LPCWSTR], wintypes.UINT),
        (kernel.GlobalSize, [wintypes.HANDLE], ctypes.c_size_t),
        (kernel.GlobalLock, [wintypes.HANDLE], ctypes.c_void_p),
        (kernel.GlobalUnlock, [wintypes.HANDLE], wintypes.BOOL),
        (kernel.GlobalAlloc, [wintypes.UINT, ctypes.c_size_t], wintypes.HANDLE),
        (kernel.GlobalFree, [wintypes.HANDLE], wintypes.HANDLE),
        (shell.DragQueryFileW, [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT], wintypes.UINT),
    ]
    for function, arguments, result in declarations:
        function.argtypes, function.restype = arguments, result
    return ctypes, user, kernel, shell


def read_clipboard_text() -> str:
    """Explicit CF_UNICODETEXT read; never used by the format beacon.

    Permit zero allocation padding, but reject embedded NUL, missing terminator,
    malformed UTF-16 and allocations beyond the payload budget plus alignment.
    """
    try:
        ctypes, user, kernel, _shell = _native_apis()
        if not user.OpenClipboard(None):
            raise ValueError()
        try:
            if not user.IsClipboardFormatAvailable(13):
                return ''
            handle = user.GetClipboardData(13)
            if not handle:
                raise ValueError()
            size = kernel.GlobalSize(handle)
            if not 2 <= size <= MAX_TEXT_BYTES + 16 or size % 2:
                raise ValueError()
            pointer = kernel.GlobalLock(handle)
            if not pointer:
                raise ValueError()
            try:
                raw = ctypes.string_at(pointer, size)
            finally:
                kernel.GlobalUnlock(handle)
        finally:
            if not user.CloseClipboard():
                raise ValueError()
        decoded = raw.decode('utf-16-le', errors='strict')
        if not decoded.endswith('\0'):
            raise ValueError()
        text = decoded.rstrip('\0')
        if '\0' in text or len(text.encode('utf-16-le')) > MAX_TEXT_BYTES:
            raise ValueError()
        return text
    except Exception:
        raise ValueError('Clipboard text unavailable') from None


def write_clipboard_text(text: str) -> bool:
    """Copy bounded user text with explicit Win32 memory ownership.

    Validate, allocate and fill before EmptyClipboard. A private message-only
    HWND is needed: OpenClipboard(NULL) followed by EmptyClipboard leaves no
    owner and makes SetClipboardData fail. This window is never displayed.
    https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setclipboarddata
    """
    try:
        if not isinstance(text, str) or '\0' in text or len(text) > MAX_TEXT_BYTES // 2:
            raise ValueError()
        raw = text.encode('utf-16-le', errors='strict')
        if len(raw) > MAX_TEXT_BYTES:
            raise ValueError()
        raw += b'\0\0'
        ctypes, user, kernel, _shell = _native_apis()
        handle, owner, opened, transferred = None, None, False, False
        try:
            handle = kernel.GlobalAlloc(0x42, len(raw))  # MOVEABLE | ZEROINIT.
            if not handle:
                raise ValueError()
            pointer = kernel.GlobalLock(handle)
            if not pointer:
                raise ValueError()
            try:
                ctypes.memmove(pointer, raw, len(raw))
            finally:
                # False normally means the last lock was released, not failure.
                ctypes.set_last_error(0)
                unlocked = kernel.GlobalUnlock(handle)
                if not unlocked and ctypes.get_last_error():
                    raise ValueError()
            owner = user.CreateWindowExW(0, 'STATIC', '', 0, 0, 0, 0, 0, -3, None, None, None)
            if not owner or not user.OpenClipboard(owner):
                raise ValueError()
            opened = True
            if not user.EmptyClipboard() or not user.SetClipboardData(13, handle):
                raise ValueError()
            transferred = True  # The system now owns handle; never GlobalFree it.
            return True
        finally:
            try:
                if opened:
                    if not user.CloseClipboard():
                        raise ValueError()
            finally:
                try:
                    if owner:
                        if not user.DestroyWindow(owner):
                            raise ValueError()
                finally:
                    if handle and not transferred:
                        kernel.GlobalFree(handle)
    except Exception:
        raise ValueError('Clipboard text unavailable') from None


def has_clipboard_sources() -> bool:
    """Format beacon only; never opens clipboard data or reads file paths."""
    _ctypes, user, _kernel, _shell = _native_apis()
    png = user.RegisterClipboardFormatW('PNG')
    return any(user.IsClipboardFormatAvailable(kind) for kind in (15, 17, 8, png) if kind)


def clipboard_sources():
    """Return clipboard-derived Paths or (image filename, bytes), never text."""
    ctypes, user, kernel, shell = _native_apis()
    png_format = user.RegisterClipboardFormatW('PNG')
    if not user.OpenClipboard(None):
        raise OSError('Clipboard is busy; try paste again')
    try:
        if user.IsClipboardFormatAvailable(15):
            handle = user.GetClipboardData(15)
            if not handle:
                raise OSError('Clipboard file list is unavailable')
            count = shell.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
            if count > 32:
                raise ValueError('Clipboard contains too many files')
            paths = []
            for index in range(count):
                length = shell.DragQueryFileW(handle, index, None, 0)
                if not 0 < length <= 32767:
                    raise ValueError('Invalid clipboard file path')
                buffer = ctypes.create_unicode_buffer(length + 1)
                if shell.DragQueryFileW(handle, index, buffer, length + 1) != length:
                    raise OSError('Clipboard file list changed')
                paths.append(Path(buffer.value))
            return paths
        kind = next((kind for kind in (png_format, 17, 8)
                     if kind and user.IsClipboardFormatAvailable(kind)), None)
        if kind is None:
            return []
        handle = user.GetClipboardData(kind)
        if not handle:
            raise OSError('Clipboard image is unavailable')
        size = kernel.GlobalSize(handle)
        if not 0 < size <= MAX_IMAGE_BYTES:
            raise ValueError('Clipboard image is too large or invalid')
        pointer = kernel.GlobalLock(handle)
        if not pointer:
            raise OSError('Cannot read clipboard image')
        try:
            data = ctypes.string_at(pointer, size)
        finally:
            kernel.GlobalUnlock(handle)
    finally:
        user.CloseClipboard()
    # Release the system clipboard before potentially expensive PNG encoding.
    if kind == png_format:
        if len(data) < 33 or data[:8] != _PNG or data[12:16] != b'IHDR':
            raise ValueError('Invalid clipboard PNG')
        width, height = struct.unpack_from('>II', data, 16)
        if not width or not height or width * height > MAX_IMAGE_PIXELS:
            raise ValueError('Clipboard image dimensions exceed limits')
        return 'screenshot.png', data
    return 'screenshot.png', dib_to_png(data)


@dataclass
class _Source:
    stream: BinaryIO
    size: int
    identity: tuple | None


def _identity(stream):
    info = os.fstat(stream.fileno())
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class ClipboardFiles:
    def __init__(self, getter=None, max_file_size=512 * 1024 * 1024, *,
                 max_sources=128, max_total_size=1024 * 1024 * 1024):
        if type(max_file_size) is not int or max_file_size < 0:
            raise ValueError('Invalid clipboard file size limit')
        self._getter = getter or clipboard_sources
        self.max_file_size = max_file_size
        if type(max_sources) is not int or not 1 <= max_sources <= 128:
            raise ValueError('Invalid clipboard source count limit')
        if type(max_total_size) is not int or not 1 <= max_total_size <= 1024 * 1024 * 1024:
            raise ValueError('Invalid clipboard total size limit')
        self.max_sources, self.max_total_size = max_sources, max_total_size
        self._items: dict[str, _Source] = {}
        self._lock = threading.RLock()

    def snapshot(self, *, cancelled=None):
        """Retain published handles until release/close, within global budgets.

        No idle eviction: an idle handle may belong to a queued upload. Failed
        snapshots roll back only their own handles, preserving ongoing jobs.
        """
        if cancelled is not None and not callable(cancelled):
            raise ValueError('Invalid clipboard cancellation predicate')

        def check_cancelled():
            if cancelled is not None and cancelled():
                raise ValueError('Clipboard snapshot cancelled')

        # Capture the current OS clipboard before waiting for a previous batch's
        # long hash operation. Paths/bytes become this invocation's immutable
        # source selection; accounting, registration and hashing remain locked.
        check_cancelled()
        sources = self._getter()
        check_cancelled()
        image = isinstance(sources, tuple) and len(sources) == 2 and isinstance(sources[1], bytes)
        if not image and (not isinstance(sources, list) or len(sources) > 32):
            raise ValueError('Unsupported clipboard sources or too many files')
        if not image:
            sources = list(sources)
        with self._lock:
            check_cancelled()
            cards = []
            created = []
            declared = sum(item.size for item in self._items.values())
            unregistered_stream = None
            try:
                for source in ([sources] if image else sources):
                    check_cancelled()
                    if image:
                        name, content = source
                        if not isinstance(name, str) or not name or len(content) > self.max_file_size:
                            raise ValueError('Clipboard image exceeds limits')
                        stream, identity, size = io.BytesIO(content), None, len(content)
                        unregistered_stream = stream
                    else:
                        if not isinstance(source, Path):
                            raise ValueError('Clipboard source must be a native file path')
                        info = source.stat()
                        if not stat.S_ISREG(info.st_mode) or info.st_size > self.max_file_size:
                            raise ValueError('Clipboard source is not a file or exceeds limits')
                        stream = source.open('rb')
                        unregistered_stream = stream
                        identity = _identity(stream)
                        name, size = source.name, identity[2]
                    if len(self._items) >= self.max_sources or declared + size > self.max_total_size:
                        stream.close()
                        unregistered_stream = None
                        raise ValueError('Clipboard snapshot budget exceeded; release queued sources first')
                    source_id = secrets.token_hex(24)
                    self._items[source_id] = _Source(stream, size, identity)
                    unregistered_stream = None
                    created.append(source_id)
                    declared += size
                    if size > self.max_file_size:
                        raise ValueError('Clipboard source exceeds limits')
                    digest = hashlib.sha256()
                    total = 0
                    while True:
                        check_cancelled()
                        block = stream.read(CHUNK_SIZE)
                        check_cancelled()
                        if not block:
                            break
                        total += len(block)
                        if total > self.max_file_size:
                            raise ValueError('Clipboard source changed or exceeds limits')
                        digest.update(block)
                        check_cancelled()
                    if total != size or (identity is not None and _identity(stream) != identity):
                        raise ValueError('Clipboard source changed during snapshot')
                    stream.seek(0)
                    cards.append({'id': source_id, 'name': name, 'size': size, 'sha256': digest.hexdigest()})
                check_cancelled()
            except BaseException:
                for source_id in created:
                    self.release(source_id)
                if unregistered_stream is not None:
                    unregistered_stream.close()
                raise
            return {'kind': 'image' if image else 'files' if cards else 'empty', 'items': cards}

    def read(self, source_id, offset):
        with self._lock:
            item = self._items.get(source_id) if isinstance(source_id, str) else None
            if item is None:
                raise ValueError('Clipboard file ID expired or unavailable')
            if type(offset) is not int or not 0 <= offset <= item.size:
                raise ValueError('Invalid clipboard read offset')
            if item.identity is not None and _identity(item.stream) != item.identity:
                self.release(source_id)
                raise ValueError('Clipboard source changed after snapshot')
            item.stream.seek(offset)
            data = item.stream.read(min(CHUNK_SIZE, item.size - offset))
            if (len(data) != min(CHUNK_SIZE, item.size - offset)
                    or item.identity is not None and _identity(item.stream) != item.identity):
                self.release(source_id)
                raise ValueError('Clipboard source changed during read')
            end = offset + len(data)
            return {'data': base64.b64encode(data).decode('ascii'), 'offset': offset,
                    'next': end, 'eof': end == item.size}

    def release(self, source_id):
        with self._lock:
            item = self._items.pop(source_id, None) if isinstance(source_id, str) else None
            if item is not None:
                item.stream.close()

    def close(self):
        with self._lock:
            for source_id in list(self._items):
                self.release(source_id)
