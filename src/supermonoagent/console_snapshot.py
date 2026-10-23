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
from pathlib import Path, PureWindowsPath
import re
import subprocess
import sys
import time
import unicodedata
from urllib.parse import urlsplit

MAX_BYTES = 1024 * 1024
MAX_COLS, MAX_ROWS = 500, 200


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
    args = parser.parse_args()
    data = None
    try:
        if sys.platform == 'win32' and all(_integer(v) for v in (args.pid, args.created, args.parent)):
            data = _capture(args.pid, args.created, args.parent)
        encoded = json.dumps(data or {}, ensure_ascii=True, separators=(',', ':')).encode('ascii')
        if len(encoded) > MAX_BYTES:
            encoded = b'{}'
    except Exception:
        encoded = b'{}'
    sys.stdout.buffer.write(encoded)


if __name__ == '__main__':
    main()
