"""Host console. One ConPTY per session. No reader runs until a session exists."""

from __future__ import annotations

import base64
import os
import shlex
import shutil
import subprocess
import threading


class PtyClosed(Exception):
    pass


class WinPty:
    def __init__(self, command: list[str], cwd: str, cols: int = 100, rows: int = 28) -> None:
        from winpty import PtyProcess

        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("COLORTERM", "truecolor")
        self._proc = PtyProcess.spawn(command, cwd=cwd, env=env, dimensions=(rows, cols))
        self._lock = threading.Lock()

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
            self._proc.write(data.decode("utf-8", errors="replace"))

    def resize(self, cols: int, rows: int) -> None:
        self._proc.setwinsize(rows, cols)

    def alive(self) -> bool:
        return bool(self._proc.isalive())

    def close(self) -> None:
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
