"""Host console. One ConPTY per session. No reader runs until a session exists."""

from __future__ import annotations

import base64
import codecs
import os
import shlex
import shutil
import subprocess
import threading
import time


class PtyClosed(Exception):
    pass


class ConptyInputFilter:
    """ConPTY drops paste markers; remove them before their partial forms reach it."""
    START = b"\x1b[200~"
    END = b"\x1b[201~"

    def __init__(self) -> None:
        self._pending = b""
        self._paste: bytearray | None = None

    def feed(self, data: bytes) -> bytes:
        data = self._pending + bytes(data)
        self._pending = b""
        output = bytearray()
        position = 0
        while position < len(data):
            starts = [index for marker in (self.START, self.END) if (index := data.find(marker, position)) >= 0]
            if not starts:
                hold = self._partial_suffix(data[position:])
                end = len(data) - hold
                (output if self._paste is None else self._paste).extend(data[position:end])
                self._pending = data[end:]
                break
            index = min(starts)
            (output if self._paste is None else self._paste).extend(data[position:index])
            marker = self.START if data.startswith(self.START, index) else self.END
            if marker == self.START and self._paste is None:
                self._paste = bytearray()
            elif marker == self.END and self._paste is not None:
                # xterm normalizes clipboard line breaks to CR. Without the
                # envelope (discarded by ConPTY), Kimi interprets short CR
                # pastes as submit keys. LF retains line breaks as editor text.
                output.extend(self._paste.replace(b"\r\n", b"\n").replace(b"\r", b"\n"))
                self._paste = None
            position = index + len(marker)
        return bytes(output)

    def flush_escape(self) -> bytes:
        """Flush an incomplete escape when it is a key, rather than a paste."""
        if self._paste is not None:
            return b""
        data, self._pending = self._pending, b""
        return data

    @classmethod
    def _partial_suffix(cls, data: bytes) -> int:
        for size in range(min(len(data), len(cls.START) - 1), 0, -1):
            suffix = data[-size:]
            if cls.START.startswith(suffix) or cls.END.startswith(suffix):
                return size
        return 0


class WinPty:
    def __init__(self, command: list[str], cwd: str, cols: int = 100, rows: int = 28) -> None:
        from winpty import PtyProcess

        env = os.environ.copy()
        # This new PTY is an interactive xterm/ConPTY surface, even when the
        # daemon inherited headless capture flags. Never change the parent env
        # or force ANSI in captured tool subprocesses. Preserve is an explicit
        # opt-out; unknown nonempty policies also preserve rather than failing.
        if env.get("SUPERMONO_TERMINAL_COLOR", "").strip().lower() in {"", "interactive"}:
            if env.get("TERM", "").strip().lower() in {"", "dumb"}:
                env["TERM"] = "xterm-256color"
            if not env.get("COLORTERM", "").strip():
                env["COLORTERM"] = "truecolor"
            env.pop("NO_COLOR", None)
        self._proc = PtyProcess.spawn(command, cwd=cwd, env=env, dimensions=(rows, cols))
        self._lock = threading.Lock()
        self._input = ConptyInputFilter()
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._escape_timer: threading.Timer | None = None
        # Bind once to the actual spawned console generation. Each session owns
        # its own bounded cache; no saved PID map survives a daemon restart.
        from .console_snapshot import _process_identity
        self._footer_pid = getattr(self._proc, 'pid', None)
        self._footer_identity = _process_identity(self._footer_pid)
        self._footer_lock = threading.Lock()
        self._footer_cache = None

    def footer_patch(self, cols: int, rows: int) -> bytes:
        """Read current status cells in a hidden helper; never write to the PTY."""
        from .console_snapshot import _process_identity, read_console, footer_patch
        identity = self._footer_identity
        if identity is None:
            return b''
        with self._footer_lock:
            if _process_identity(self._footer_pid) != identity:
                self._footer_cache = None
                return b''
            now = time.monotonic()
            if self._footer_cache is None or now - self._footer_cache[0] >= 1:
                data = read_console(self._footer_pid, expected_created=identity[1], expected_parent=identity[0])
                self._footer_cache = (now, data)
            return footer_patch(self._footer_cache[1], cols, rows)

    def read(self, size: int = 4096) -> bytes:
        try:
            text = self._proc.read(size)
        except EOFError as exc:
            raise PtyClosed() from exc
        if text is None:
            raise PtyClosed()
        return text.encode("utf-8", errors="replace")

    def write(self, data: bytes) -> None:
        with self._lock:
            if self._escape_timer is not None:
                self._escape_timer.cancel()
                self._escape_timer = None
            self._write_text(self._input.feed(data))
            # Let split envelopes complete without making an ordinary Escape
            # key wait indefinitely. Active paste content is emitted atomically
            # so ConPTY's removal of envelopes does not fragment paste bursts.
            if self._input._pending and self._input._paste is None:
                timer = threading.Timer(0.05, self._flush_escape)
                timer.daemon = True
                self._escape_timer = timer
                timer.start()

    def _write_text(self, data: bytes) -> None:
        text = self._decoder.decode(data)
        if text:
            self._proc.write(text)

    def _flush_escape(self) -> None:
        with self._lock:
            if self._escape_timer is not threading.current_thread():
                return
            self._escape_timer = None
            self._write_text(self._input.flush_escape())

    def resize(self, cols: int, rows: int) -> None:
        with self._lock:
            self._proc.setwinsize(rows, cols)

    def alive(self) -> bool:
        return bool(self._proc.isalive())

    def close(self) -> None:
        with self._lock:
            if self._escape_timer is not None:
                self._escape_timer.cancel()
                self._escape_timer = None
        try:
            self._proc.terminate(force=True)
        except Exception:
            pass


def _powershell_color_command() -> str:
    # Profile has already loaded when -EncodedCommand runs. Colors are VT
    # sequences so ConPTY does not remap "Red" into a dim console attribute.
    return r"""
$e = [char]27
try {
  if (-not (Get-Module PSReadLine)) { Import-Module PSReadLine -ErrorAction SilentlyContinue }
  $wanted = @{
    Command = "$e[93m"
    Parameter = "$e[96m"
    String = "$e[92m"
    Operator = "$e[37m"
    Variable = "$e[96m"
    Number = "$e[95m"
    Member = "$e[97m"
    Type = "$e[37m"
    Comment = "$e[32m"
    Keyword = "$e[95m"
    ContinuationPrompt = "$e[90m"
    Default = "$e[37m"
    Error = "$e[91m"
    Emphasis = "$e[91;1m"
  }
  foreach ($name in @($wanted.Keys)) {
    try { Set-PSReadLineOption -Colors @{ $name = $wanted[$name] } } catch {}
  }
} catch {}
try { $Host.PrivateData.ErrorForegroundColor = 'Red' } catch {}
try { $Host.PrivateData.WarningForegroundColor = 'Yellow' } catch {}
try {
  if (Test-Path variable:PSStyle) {
    $PSStyle.Formatting.Error = "$e[91m"
    $PSStyle.Formatting.ErrorAccent = "$e[91;1m"
    $PSStyle.Formatting.Warning = "$e[93m"
  }
} catch {}
"""


def _powershell_args(exe: str) -> list[str]:
    encoded = base64.b64encode(_powershell_color_command().encode("utf-16le")).decode("ascii")
    return [exe, "-NoLogo", "-NoExit", "-EncodedCommand", encoded]


def _command_line(line: str) -> list[str]:
    parts = shlex.split(line, posix=False)
    if not parts:
        raise FileNotFoundError(line)
    exe = parts[0].strip('"')
    if not os.path.isabs(exe):
        found = shutil.which(exe)
        if not found:
            raise FileNotFoundError(exe)
        exe = found
    parts[0] = exe
    return parts


def shell_command(shell: str) -> list[str]:
    name = (shell or "powershell").strip()
    key = name.lower()
    if key in {"powershell", "ps"}:
        exe = shutil.which("powershell") or r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        return _powershell_args(exe)
    if key == "pwsh":
        exe = shutil.which("pwsh")
        if not exe:
            raise FileNotFoundError("pwsh")
        return _powershell_args(exe)
    if key == "cmd":
        return [os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe"]
    if key in {"claude", "codex"}:
        exe = shutil.which(key)
        if not exe:
            raise FileNotFoundError(key)
        return [exe]
    return _command_line(name)


def default_cwd() -> str:
    home = os.path.expanduser("~")
    return home if os.path.isdir(home) else os.getcwd()


def windows_pid_count(image: str) -> int | None:
    """Best-effort count used by the manual CPU note, not the hot path."""
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
            text=True,
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return sum(1 for line in out.splitlines() if image.lower() in line.lower())
