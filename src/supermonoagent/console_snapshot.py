"""Opt-in, read-only recovery of an existing Windows Codex status footer.

Only a disposable hidden child attaches to the console. This is deliberately
not a screen/history replacement: authenticated local proxies may append the
validated footer to an existing replay without changing the PTY or its lease.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes as W
import json
import math
from pathlib import Path, PureWindowsPath
import re
import subprocess
import sys
import time
import unicodedata
from urllib.parse import urlsplit

MAX_BYTES = 1024 * 1024
MAX_COLS, MAX_ROWS = 500, 200
MAX_VIEWPORT_CELLS = 40000


def _integer(value, minimum=1):
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _process_identity(pid):
    """Return (parent pid, creation FILETIME), or fail closed."""
    if sys.platform != 'win32' or not _integer(pid):
        return None
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    class ENTRY(ctypes.Structure):
        _fields_ = [('size', W.DWORD), ('usage', W.DWORD), ('pid', W.DWORD),
                    ('heap', ctypes.c_size_t), ('module', W.DWORD), ('threads', W.DWORD),
                    ('parent', W.DWORD), ('priority', W.LONG), ('flags', W.DWORD),
                    ('name', W.WCHAR * 260)]
    k.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
    k.CreateToolhelp32Snapshot.restype = W.HANDLE
    k.Process32FirstW.argtypes = [W.HANDLE, ctypes.POINTER(ENTRY)]
    k.Process32FirstW.restype = W.BOOL
    k.Process32NextW.argtypes = [W.HANDLE, ctypes.POINTER(ENTRY)]
    k.Process32NextW.restype = W.BOOL
    k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    k.OpenProcess.restype = W.HANDLE
    k.GetProcessTimes.argtypes = [W.HANDLE] + [ctypes.POINTER(W.FILETIME)] * 4
    k.GetProcessTimes.restype = W.BOOL
    k.CloseHandle.argtypes = [W.HANDLE]
    k.CloseHandle.restype = W.BOOL
    snapshot = k.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return None
    parent = None
    try:
        item = ENTRY()
        item.size = ctypes.sizeof(item)
        present = k.Process32FirstW(snapshot, ctypes.byref(item))
        while present:
            if item.pid == pid:
                parent = item.parent
                break
            present = k.Process32NextW(snapshot, ctypes.byref(item))
    finally:
        k.CloseHandle(snapshot)
    if parent is None:
        return None
    process = k.OpenProcess(0x1000, False, pid)  # limited query only
    if not process:
        return None
    try:
        stamps = [W.FILETIME() for _ in range(4)]
        if not k.GetProcessTimes(process, *[ctypes.byref(x) for x in stamps]):
            return None
        return parent, (stamps[0].dwHighDateTime << 32) | stamps[0].dwLowDateTime
    finally:
        k.CloseHandle(process)


def _matches(identity, created, parent):
    return identity is not None and (created is None or identity[1] == created) and (parent is None or identity[0] == parent)


def _console_agent(kernel):
    """Identify only an executable actually attached to this helper's console.

    No command lines, tokens, titles, path strings or guessed child processes
    leave this function. Unsupported/inaccessible processes remain unknown.
    """
    kernel.GetConsoleProcessList.argtypes = [ctypes.POINTER(W.DWORD), W.DWORD]
    kernel.GetConsoleProcessList.restype = W.DWORD
    kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    kernel.OpenProcess.restype = W.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = W.BOOL
    processes = (W.DWORD * 64)()
    count = kernel.GetConsoleProcessList(processes, len(processes))
    if not count or count > 512:
        return ''
    if count > len(processes):
        processes = (W.DWORD * count)()
        count = kernel.GetConsoleProcessList(processes, len(processes))
        if not count or count > len(processes):
            return ''
    for pid in processes[:count]:
        process = kernel.OpenProcess(0x1000, False, pid)
        if not process:
            continue
        try:
            path = ctypes.create_unicode_buffer(32768)
            size = W.DWORD(len(path))
            if kernel.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
                if PureWindowsPath(path.value).name.lower() == 'codex.exe':
                    return 'codex'
        finally:
            kernel.CloseHandle(process)
    return ''


def _capture(pid, created=None, parent=None):
    """Child-only console APIs. Never call this from a running application."""
    identity = _process_identity(pid)
    if not _matches(identity, created, parent):
        return None
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    class COORD(ctypes.Structure):
        _fields_ = [('X', W.SHORT), ('Y', W.SHORT)]
    class RECT(ctypes.Structure):
        _fields_ = [('left', W.SHORT), ('top', W.SHORT), ('right', W.SHORT), ('bottom', W.SHORT)]
    class INFO(ctypes.Structure):
        _fields_ = [('length', W.ULONG), ('size', COORD), ('cursor', COORD),
                    ('attributes', W.WORD), ('window', RECT), ('maximum', COORD),
                    ('popup', W.WORD), ('fullscreen', W.BOOL), ('colors', W.DWORD * 16)]
    class CELL(ctypes.Structure):
        _fields_ = [('character', W.WCHAR), ('attributes', W.WORD)]
    k.FreeConsole.argtypes = []; k.FreeConsole.restype = W.BOOL
    k.AttachConsole.argtypes = [W.DWORD]; k.AttachConsole.restype = W.BOOL
    k.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, ctypes.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
    k.CreateFileW.restype = W.HANDLE
    k.GetConsoleScreenBufferInfoEx.argtypes = [W.HANDLE, ctypes.POINTER(INFO)]
    k.GetConsoleScreenBufferInfoEx.restype = W.BOOL
    k.ReadConsoleOutputW.argtypes = [W.HANDLE, ctypes.POINTER(CELL), COORD, COORD, ctypes.POINTER(RECT)]
    k.ReadConsoleOutputW.restype = W.BOOL
    k.CloseHandle.argtypes = [W.HANDLE]; k.CloseHandle.restype = W.BOOL
    handle = None
    attached = False
    try:
        k.FreeConsole()  # this disposable helper's console only
        if not k.AttachConsole(pid):
            return None
        attached = True
        if _process_identity(pid) != identity:
            return None
        handle = k.CreateFileW('CONOUT$', 0x80000000, 3, None, 3, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            return None
        info = INFO()
        info.length = ctypes.sizeof(info)
        if not k.GetConsoleScreenBufferInfoEx(handle, ctypes.byref(info)):
            return None
        width, height = info.size.X, info.size.Y
        if not (1 <= width <= MAX_COLS and 4 <= height <= MAX_ROWS):
            return None
        rows, attributes = [], []
        for y in range(height):
            cells = (CELL * width)()
            rect = RECT(0, y, width - 1, y)
            if not k.ReadConsoleOutputW(handle, cells, COORD(width, 1), COORD(0, 0), ctypes.byref(rect)):
                return None
            if (rect.left, rect.top, rect.right, rect.bottom) != (0, y, width - 1, y):
                return None
            rows.append(''.join(cell.character for cell in cells))
            attributes.append([cell.attributes for cell in cells])
        after = INFO()
        after.length = ctypes.sizeof(after)
        if not k.GetConsoleScreenBufferInfoEx(handle, ctypes.byref(after)):
            return None
        if (after.size.X, after.size.Y) != (width, height) or _process_identity(pid) != identity:
            return None
        agent = _console_agent(k)
        return {'pid': pid, 'parent': identity[0], 'created': identity[1], 'agent': agent,
                'width': width, 'height': height, 'cursor': [after.cursor.X, after.cursor.Y],
                'rows': rows, 'attributes': attributes,
                'palette': [[v & 255, (v >> 8) & 255, (v >> 16) & 255] for v in after.colors]}
    finally:
        if handle is not None and handle != ctypes.c_void_p(-1).value:
            k.CloseHandle(handle)
        if attached:
            k.FreeConsole()


def read_console(pid, expected_created=None, expected_parent=None, timeout=2):
    """Read a console in an isolated child; failures never disclose cell data."""
    if sys.platform != 'win32' or not _integer(pid):
        return None
    if expected_created is not None and not _integer(expected_created):
        return None
    if expected_parent is not None and not _integer(expected_parent):
        return None
    identity = _process_identity(pid)
    if not _matches(identity, expected_created, expected_parent):
        return None
    command = ([sys.executable, 'console-snapshot'] if getattr(sys, 'frozen', False)
               else [sys.executable, '-m', 'supermonoagent.console_snapshot'])
    command += ['--pid', str(pid), '--created', str(identity[1]), '--parent', str(identity[0])]
    try:
        # This trusted helper bounds JSON before writing. Output stays in a
        # private pipe and memory; no console dump or diagnostic log is created.
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
                                timeout=max(.1, min(float(timeout), 2.0)), check=True)
        if len(result.stdout) > MAX_BYTES:
            return None
        data = json.loads(result.stdout)
        if not isinstance(data, dict) or (data.get('parent'), data.get('created')) != identity or data.get('pid') != pid:
            return None
        if _process_identity(pid) != identity:
            return None
        return data
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def validate_console_viewport(data):
    """Validate bounded raw Win32 cells; this is not a lossless VT snapshot."""
    keys = {'kind', 'version', 'pid', 'parent', 'created', 'buffer_size', 'viewport',
            'width', 'height', 'cursor_buffer', 'cursor', 'cursor_visible', 'cursor_in_view',
            'cursor_size', 'default_attributes', 'palette', 'code_units', 'attributes', 'stable_samples'}
    try:
        if (not isinstance(data, dict) or set(data) != keys or data['kind'] != 'console_viewport'
                or type(data['version']) is not int or data['version'] != 1
                or type(data['stable_samples']) is not int or data['stable_samples'] != 2):
            return False
        if any(not _integer(data[name]) for name in ('pid', 'parent', 'created', 'width', 'height', 'cursor_size')):
            return False
        if data['pid'] > 0xFFFFFFFF or data['parent'] > 0xFFFFFFFF or data['created'] > 0xFFFFFFFFFFFFFFFF:
            return False
        width, height = data['width'], data['height']
        if width > MAX_COLS or height > MAX_ROWS or width * height > MAX_VIEWPORT_CELLS or data['cursor_size'] > 100:
            return False
        def vector(value, length, minimum=0, maximum=32767):
            return (isinstance(value, list) and len(value) == length
                    and all(type(item) is int and minimum <= item <= maximum for item in value))
        if not vector(data['buffer_size'], 2, 1) or not vector(data['viewport'], 4):
            return False
        bw, bh = data['buffer_size']
        left, top, right, bottom = data['viewport']
        if not (left <= right < bw and top <= bottom < bh and right-left+1 == width and bottom-top+1 == height):
            return False
        if not vector(data['cursor_buffer'], 2) or not vector(data['cursor'], 2, -32767):
            return False
        x, y = data['cursor_buffer']
        if x >= bw or y >= bh or data['cursor'] != [x-left, y-top]:
            return False
        if (type(data['cursor_visible']) is not bool or type(data['cursor_in_view']) is not bool
                or data['cursor_in_view'] != (left <= x <= right and top <= y <= bottom)
                or not _integer(data['default_attributes'], 0) or data['default_attributes'] > 65535):
            return False
        if (not isinstance(data['palette'], list) or len(data['palette']) != 16
                or not all(vector(rgb, 3, 0, 255) for rgb in data['palette'])):
            return False
        for key in ('code_units', 'attributes'):
            if (not isinstance(data[key], list) or len(data[key]) != height
                    or not all(vector(row, width, 0, 65535) for row in data[key])):
                return False
        return len(json.dumps(data, ensure_ascii=True, separators=(',', ':')).encode('ascii')) <= MAX_BYTES
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        return False


def _consistent_viewport(sample, identity_reader, identity):
    """Two equal complete grids bracketed by the exact process identity."""
    if identity_reader() != identity:
        return None
    first = sample()
    if not validate_console_viewport(first) or identity_reader() != identity:
        return None
    second = sample()
    if not validate_console_viewport(second) or first != second or identity_reader() != identity:
        return None
    return second


def _viewport_types():
    class COORD(ctypes.Structure):
        _fields_ = [('X', W.SHORT), ('Y', W.SHORT)]
    class RECT(ctypes.Structure):
        _fields_ = [('left', W.SHORT), ('top', W.SHORT), ('right', W.SHORT), ('bottom', W.SHORT)]
    class INFO(ctypes.Structure):
        _fields_ = [('length', W.ULONG), ('size', COORD), ('cursor', COORD),
                    ('attributes', W.WORD), ('window', RECT), ('maximum', COORD),
                    ('popup', W.WORD), ('fullscreen', W.BOOL), ('colors', W.DWORD * 16)]
    class CELL(ctypes.Structure):
        # CHAR_INFO's two-byte union as a WORD, avoiding surrogate decoding.
        _fields_ = [('unit', W.WORD), ('attributes', W.WORD)]
    class CURSOR(ctypes.Structure):
        _fields_ = [('size', W.DWORD), ('visible', W.BOOL)]
    return COORD, RECT, INFO, CELL, CURSOR


def _read_viewport_sample(kernel, handle, types, pid, identity):
    COORD, RECT, INFO, CELL, CURSOR = types
    def metadata():
        info, cursor = INFO(), CURSOR()
        info.length = ctypes.sizeof(info)
        if not kernel.GetConsoleScreenBufferInfoEx(handle, ctypes.byref(info)):
            return None
        if not kernel.GetConsoleCursorInfo(handle, ctypes.byref(cursor)):
            # Owned Win11 evidence: this query returns ERROR_ACCESS_DENIED on
            # a READ handle despite the documentation. Keep grid reads on the
            # READ handle; a temporary RW handle is used only for this query.
            if ctypes.get_last_error() != 5:
                return None
            query = kernel.CreateFileW('CONOUT$', 0xC0000000, 3, None, 3, 0, None)
            if not query or query == ctypes.c_void_p(-1).value:
                return None
            try:
                if not kernel.GetConsoleCursorInfo(query, ctypes.byref(cursor)):
                    return None
            finally:
                kernel.CloseHandle(query)
        rect = [info.window.left, info.window.top, info.window.right, info.window.bottom]
        left, top, right, bottom = rect
        width, height = right-left+1, bottom-top+1
        if (not 1 <= width <= MAX_COLS or not 1 <= height <= MAX_ROWS
                or width*height > MAX_VIEWPORT_CELLS
                or not 0 <= left <= right < info.size.X <= 32767
                or not 0 <= top <= bottom < info.size.Y <= 32767
                or not 0 <= info.cursor.X < info.size.X or not 0 <= info.cursor.Y < info.size.Y
                or not 1 <= cursor.size <= 100):
            return None
        return {'kind': 'console_viewport', 'version': 1, 'pid': pid,
                'parent': identity[0], 'created': identity[1], 'buffer_size': [info.size.X, info.size.Y],
                'viewport': rect, 'width': width, 'height': height,
                'cursor_buffer': [info.cursor.X, info.cursor.Y],
                'cursor': [info.cursor.X-left, info.cursor.Y-top], 'cursor_visible': bool(cursor.visible),
                'cursor_in_view': left <= info.cursor.X <= right and top <= info.cursor.Y <= bottom,
                'cursor_size': cursor.size, 'default_attributes': info.attributes,
                'palette': [[v & 255, (v >> 8) & 255, (v >> 16) & 255] for v in info.colors],
                'stable_samples': 2}
    before = metadata()
    if before is None:
        return None
    width, height = before['width'], before['height']
    cells = (CELL * (width * height))()
    region = RECT(*before['viewport'])
    if not kernel.ReadConsoleOutputW(handle, cells, COORD(width, height), COORD(0, 0), ctypes.byref(region)):
        return None
    if [region.left, region.top, region.right, region.bottom] != before['viewport'] or metadata() != before:
        return None
    return {**before, 'code_units': [[cells[y*width+x].unit for x in range(width)] for y in range(height)],
            'attributes': [[cells[y*width+x].attributes for x in range(width)] for y in range(height)]}


def _capture_viewport(pid, created=None, parent=None):
    """Disposable-child-only visible rectangle read; no input/resize APIs."""
    identity = _process_identity(pid)
    if not _matches(identity, created, parent):
        return None
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    types = _viewport_types()
    COORD, RECT, INFO, CELL, CURSOR = types
    declarations = [
        (kernel.FreeConsole, [], W.BOOL), (kernel.AttachConsole, [W.DWORD], W.BOOL),
        (kernel.CreateFileW, [W.LPCWSTR, W.DWORD, W.DWORD, ctypes.c_void_p, W.DWORD, W.DWORD, W.HANDLE], W.HANDLE),
        (kernel.GetConsoleScreenBufferInfoEx, [W.HANDLE, ctypes.POINTER(INFO)], W.BOOL),
        (kernel.GetConsoleCursorInfo, [W.HANDLE, ctypes.POINTER(CURSOR)], W.BOOL),
        (kernel.ReadConsoleOutputW, [W.HANDLE, ctypes.POINTER(CELL), COORD, COORD, ctypes.POINTER(RECT)], W.BOOL),
        (kernel.CloseHandle, [W.HANDLE], W.BOOL)]
    for fn, arguments, result in declarations:
        fn.argtypes, fn.restype = arguments, result
    attached = False
    try:
        kernel.FreeConsole()
        if not kernel.AttachConsole(pid):
            return None
        attached = True
        if _process_identity(pid) != identity:
            return None
        def sample():
            # Reopen CONOUT$ for each whole-grid sample so a permanently
            # switched active screen buffer isn't sampled via an old handle.
            grid = kernel.CreateFileW('CONOUT$', 0x80000000, 3, None, 3, 0, None)
            if not grid or grid == ctypes.c_void_p(-1).value:
                return None
            try:
                return _read_viewport_sample(kernel, grid, types, pid, identity)
            finally:
                kernel.CloseHandle(grid)
        return _consistent_viewport(sample, lambda: _process_identity(pid), identity)
    finally:
        if attached:
            kernel.FreeConsole()


def read_console_viewport(pid, expected_created=None, expected_parent=None, timeout=2):
    """Uncached bounded visible Win32 cells, not scrollback or full VT state.

    The caller must authorize and bind this PID to its own session. UTF-16/LVB
    cells and 16 console colors cannot recover truecolor, dim, combining history,
    scroll regions or an application-level atomic render transaction.
    """
    if (sys.platform != 'win32' or not _integer(pid) or pid > 0xFFFFFFFF
            or expected_created is not None and not _integer(expected_created)
            or expected_parent is not None and not _integer(expected_parent)
            or type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
        return None
    identity = _process_identity(pid)
    if not _matches(identity, expected_created, expected_parent):
        return None
    command = ([sys.executable, 'console-snapshot'] if getattr(sys, 'frozen', False)
               else [sys.executable, '-m', 'supermonoagent.console_snapshot'])
    command += ['--viewport', '--pid', str(pid), '--created', str(identity[1]), '--parent', str(identity[0])]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
                                timeout=max(.1, min(timeout, 2.0)), check=True)
        if len(result.stdout) > MAX_BYTES:
            return None
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError()
                value[key] = item
            return value
        data = json.loads(result.stdout, object_pairs_hook=unique)
        if (not validate_console_viewport(data) or data['pid'] != pid
                or (data['parent'], data['created']) != identity or _process_identity(pid) != identity):
            return None
        return data
    except (OSError, ValueError, TypeError, RecursionError, subprocess.SubprocessError):
        return None


def footer_patch(snapshot, cols, rows):
    """Return only verified Codex footer rows, never a clear or history reset."""
    if not isinstance(snapshot, dict) or not _integer(cols) or not _integer(rows):
        return b''
    if not (cols <= MAX_COLS and 4 <= rows <= MAX_ROWS):
        return b''
    if (snapshot.get('width'), snapshot.get('height')) != (cols, rows):
        return b''
    text, attrs, palette = snapshot.get('rows'), snapshot.get('attributes'), snapshot.get('palette')
    if not isinstance(text, list) or not isinstance(attrs, list) or len(text) != rows or len(attrs) != rows:
        return b''
    if not isinstance(palette, list) or len(palette) != 16:
        return b''
    if any(not isinstance(rgb, list) or len(rgb) != 3 or any(not _integer(v, 0) or v > 255 for v in rgb) for rgb in palette):
        return b''
    output = []
    for y in range(max(0, rows - 4), rows - 1):
        line, colors = text[y], attrs[y]
        if not isinstance(line, str) or not isinstance(colors, list) or len(line) != cols or len(colors) != cols:
            continue
        if not re.search(r'\bcontext\b', line, re.I) or not re.search(r'\bweekly\b', line, re.I):
            continue
        if len(re.findall(r'\b\d{1,3}(?:[.]\d+)?\s*%', line)) < 2:
            continue
        # No executable control characters, combining marks, surrogate halves,
        # wide glyphs, or console DBCS attributes may enter the VT stream.
        if any(unicodedata.category(c).startswith('C') or unicodedata.category(c).startswith('M') or
               unicodedata.east_asian_width(c) in {'W', 'F'} for c in line):
            continue
        if any(not _integer(a, 0) or a > 255 for a in colors):
            continue
        run = [f'\x1b[{y + 1};1H']
        previous = None
        for char, attribute in zip(line, colors):
            if attribute != previous:
                fg, bg = palette[attribute & 15], palette[(attribute >> 4) & 15]
                run.append('\x1b[0;38;2;%d;%d;%d;48;2;%d;%d;%dm' % (*fg, *bg))
                previous = attribute
            run.append(char)
        output.append(''.join(run))
    # The final screen row is excluded, so even an autowrap transition cannot
    # scroll the terminal. DECRC restores the caller's position and rendition.
    # Temporarily use absolute screen coordinates even if the CLI left DECOM
    # enabled. DECSC/DECRC also save and restore that origin mode.
    if not output:
        return b''
    # Passive display metadata only: no state= means the task detector cannot
    # infer activity or create notifications from this recovery marker.
    marker = '\x1b]7501;app=codex\x07' if snapshot.get('agent') == 'codex' else ''
    return (marker + '\x1b7\x1b[?6l' + ''.join(output) + '\x1b[0m\x1b8').encode('utf-8')


def _loopback_backend(value):
    try:
        uri = urlsplit(value)
        return (uri.scheme in {'http', 'ws'} and uri.hostname in {'127.0.0.1', 'localhost', '::1'}
                and uri.port is not None and uri.username is None and uri.password is None
                and not uri.query and not uri.fragment and uri.path in {'', '/', '/ws'})
    except (TypeError, ValueError):
        return False


class FooterRecovery:
    """Explicit machine-local bindings only; caller must already authorize owner."""
    def __init__(self, state_dir, backend):
        self.bindings = {}
        self.parent = None
        self.cache = {}
        if not _loopback_backend(backend):
            return
        try:
            path = Path(state_dir) / 'console-recovery.json'
            if path.stat().st_size > 65536:
                return
            data = json.loads(path.read_text(encoding='utf-8'))
            if data.get('backend') != backend or not _integer(data.get('daemon_pid')):
                return
            bindings = data.get('bindings')
            if not isinstance(bindings, dict) or len(bindings) > 32:
                return
            if any(not isinstance(sid, str) or not sid or len(sid) > 256 or not isinstance(binding, dict)
                   or not _integer(binding.get('pid')) or not _integer(binding.get('created'))
                   for sid, binding in bindings.items()):
                return
            self.parent, self.bindings = data['daemon_pid'], bindings
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def patch(self, sid, cols, rows):
        binding = self.bindings.get(sid) if isinstance(sid, str) else None
        if not binding:
            return b''
        pid, created = binding['pid'], binding['created']
        if _process_identity(pid) != (self.parent, created):
            self.cache.pop(pid, None)
            return b''
        now = time.monotonic()
        cached = self.cache.get(pid)
        if cached is None or now - cached[0] >= 1:
            data = read_console(pid, expected_created=created, expected_parent=self.parent)
            self.cache[pid] = (now, data)
        return footer_patch(self.cache[pid][1], cols, rows)

    def _frame_patch(self, frame, state):
        from .protocol import OP_SNAPSHOT, unpack, ProtocolError
        try:
            opcode, payload = unpack(frame)
            if opcode != OP_SNAPSHOT or not isinstance(state, dict):
                return None
            if isinstance(state.get('capabilities'), dict) and state['capabilities'].get('snapshot_footer') is True:
                return None
            prefix, separator, _ = payload.partition(b'\0')
            if not separator:
                return None
            sid = prefix.decode('utf-8')
            windows, controls = state.get('windows'), state.get('controls')
            if not isinstance(windows, list) or not isinstance(controls, dict):
                return None
            allowed = any(isinstance(window, dict) and isinstance(window.get('sessions'), list)
                          and any(isinstance(session, dict) and session.get('id') == sid
                                  for session in window['sessions']) for window in windows)
            control = controls.get(sid)
            if not allowed or not isinstance(control, dict):
                return None
            patch = self.patch(sid, control.get('cols'), control.get('rows'))
            return (prefix, payload, patch) if patch else None
        except (ProtocolError, UnicodeError, TypeError, ValueError):
            return None

    def augment(self, frame, state):
        """Append a footer only for a session in the caller's authorized catalog."""
        from .protocol import OP_SNAPSHOT, pack, ProtocolError
        data = self._frame_patch(frame, state)
        if data is None:
            return frame
        _, payload, patch = data
        try:
            # pack fails closed at the protocol limit. Never trim existing raw
            # replay merely to make room for an optional recovery footer.
            return pack(OP_SNAPSHOT, payload + patch)
        except ProtocolError:
            return frame

    def supplement(self, frame, state):
        """Separate output after replay, including an already full-size snapshot.

        The proxy must send the original snapshot unchanged, then this optional
        frame before forwarding later backend frames. That preserves every byte
        of history and prevents a transient newer output from being overwritten.
        """
        from .protocol import OP_OUTPUT, pack, ProtocolError
        data = self._frame_patch(frame, state)
        if data is None:
            return None
        prefix, _, patch = data
        try:
            return pack(OP_OUTPUT, prefix + b'\0' + patch)
        except ProtocolError:
            return None


def main():
    parser = argparse.ArgumentParser(description='Private read-only console snapshot helper')
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--created', type=int, required=True)
    parser.add_argument('--parent', type=int, required=True)
    parser.add_argument('--viewport', action='store_true', help='Separate stable visible-grid capture action')
    args = parser.parse_args()
    data = None
    try:
        if sys.platform == 'win32' and all(_integer(v) for v in (args.pid, args.created, args.parent)):
            capture = _capture_viewport if args.viewport else _capture
            data = capture(args.pid, args.created, args.parent)
        encoded = json.dumps(data or {}, ensure_ascii=True, separators=(',', ':')).encode('ascii')
        if len(encoded) > MAX_BYTES:
            encoded = b'{}'
    except Exception:
        encoded = b'{}'
    sys.stdout.buffer.write(encoded)


if __name__ == '__main__':
    main()
