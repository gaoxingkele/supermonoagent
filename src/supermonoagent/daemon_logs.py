"""Operational daemon logs and an independent, rotation-safe console viewer."""
from __future__ import annotations

import argparse
import codecs
from contextlib import contextmanager
import ctypes
from functools import lru_cache
import hashlib
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from urllib.parse import quote, quote_plus

LOGGER_NAME = "supermonoagent.daemon"
MAX_LOG_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3
INITIAL_BYTES = 256 * 1024
READ_BYTES = 64 * 1024
SNAPSHOT_BYTES = 32 * 1024
_configuration_lock = threading.RLock()
_launch_lock = threading.Lock()
_launched = {}


class _Redactor:
    def __init__(self, values=()):
        self.values = {str(value) for value in values if value}
        variants = set()
        for value in self.values:
            variants.update((value, quote(value, safe=""), quote_plus(value, safe=""),
                             json.dumps(value, ensure_ascii=True)[1:-1]))
        self.pattern = re.compile("|".join(re.escape(value) for value in sorted(variants, key=len, reverse=True))) if variants else None

    def redact(self, text):
        return self.pattern.sub("[redacted]", text) if self.pattern is not None else text


class _SecretFilter(logging.Filter):
    def __init__(self, redactor):
        super().__init__()
        self.redactor = redactor

    def filter(self, record):
        try:
            record.msg = self.redactor.redact(record.getMessage())
        except Exception:
            record.msg = "Unformattable daemon log entry"
        record.args = ()
        if record.exc_info:
            record.exc_text = self.redactor.redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        if record.exc_text:
            record.exc_text = self.redactor.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self.redactor.redact(record.stack_info)
        return True


class _SecretFormatter(logging.Formatter):
    def __init__(self, redactor):
        super().__init__("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        self.redactor = redactor

    def format(self, record):
        return self.redactor.redact(super().format(record))


def configure_logging(state_dir, secrets=()):
    """Configure only this application's logger, never stdout or the root logger."""
    directory = Path(state_dir).resolve() / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    with _configuration_lock:
        old_filters = [item for item in logger.filters if isinstance(item, _SecretFilter)]
        known = set(secrets)
        for item in old_filters:
            known.update(item.redactor.values)
            logger.removeFilter(item)
        redactor = _Redactor(known)
        logger.addFilter(_SecretFilter(redactor))
        managed = [handler for handler in logger.handlers if getattr(handler, "_sma_daemon_log", False)]
        target = str(directory / "daemon.log")
        if not any(isinstance(handler, RotatingFileHandler) and handler.baseFilename == target for handler in managed):
            for handler in managed:
                logger.removeHandler(handler)
                handler.close()
            managed = [RotatingFileHandler(target, maxBytes=MAX_LOG_BYTES, backupCount=BACKUP_COUNT,
                                          encoding="utf-8", delay=True), logging.StreamHandler()]
            for handler in managed:
                handler._sma_daemon_log = True
                logger.addHandler(handler)
        for handler in managed:
            handler.setFormatter(_SecretFormatter(redactor))
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def shutdown_logging(logger=None):
    """Release only owned handlers; viewers and daemon processes are untouched."""
    logger = logger or logging.getLogger(LOGGER_NAME)
    with _configuration_lock:
        for handler in list(logger.handlers):
            if getattr(handler, "_sma_daemon_log", False):
                logger.removeHandler(handler)
                try:
                    handler.flush()
                finally:
                    handler.close()
        for item in list(logger.filters):
            if isinstance(item, _SecretFilter):
                logger.removeFilter(item)


@lru_cache(maxsize=1)
def _kernel32():
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    api.CreateMutexW.restype = wintypes.HANDLE
    api.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    api.OpenMutexW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                               wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    return api


def _mutex_name(state_dir):
    path = os.path.normcase(str(Path(state_dir).resolve()))
    return "Local\\SuperMonoAgent.Logs." + hashlib.sha256(path.encode("utf-8")).hexdigest()


def _mutex_exists(state_dir):
    api = _kernel32()
    handle = api.OpenMutexW(0x00100000, False, _mutex_name(state_dir))  # SYNCHRONIZE
    if handle:
        api.CloseHandle(handle)
        return True
    return ctypes.get_last_error() == 5  # An existing mutex with another ACL is not ours to replace.


@contextmanager
def _viewer_instance(state_dir):
    if sys.platform != "win32":
        yield True
        return
    api = _kernel32()
    ctypes.set_last_error(0)
    handle = api.CreateMutexW(None, False, _mutex_name(state_dir))
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    created = ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
    try:
        yield created
    finally:
        api.CloseHandle(handle)


def open_log_window(state_dir):
    """Open one visible console per state. No credentials or daemon data are argv."""
    if sys.platform != "win32":
        return False
    state = str(Path(state_dir).resolve())
    key = os.path.normcase(state)
    with _launch_lock:
        prior = _launched.get(key)
        if prior is not None and prior.poll() is None:
            return True
        try:
            if _mutex_exists(state):
                return True
            executable = Path(sys.executable)
            if not getattr(sys, "frozen", False) and executable.name.lower() == "pythonw.exe":
                executable = executable.with_name("python.exe")
                if not executable.is_file():
                    return False
            entry = ["logs"] if getattr(sys, "frozen", False) else ["-m", "supermonoagent.daemon_logs"]
            flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
            _launched[key] = subprocess.Popen([str(executable), *entry, "--state-dir", state, "--tail", "80"],
                                                creationflags=flags, close_fds=True)
            return True
        except OSError:
            return False


def _open_shared(path):
    if sys.platform != "win32":
        return Path(path).open("rb", buffering=0)
    import msvcrt
    api = _kernel32()
    handle = api.CreateFileW(str(path), 0x80000000, 1 | 2 | 4, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor = None
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        return os.fdopen(descriptor, "rb", buffering=0)
    except Exception:
        if descriptor is None:
            api.CloseHandle(handle)
        else:
            os.close(descriptor)
        raise


def _decoder():
    return io.IncrementalNewlineDecoder(codecs.getincrementaldecoder("utf-8")(errors="replace"), True)


class LogTail:
    """Reopen briefly each poll so rotating writers can rename/delete old logs."""
    def __init__(self, path, tail_lines=80, *, read_bytes=READ_BYTES, initial_bytes=INITIAL_BYTES):
        if not 0 <= tail_lines <= 1000 or read_bytes < 1 or initial_bytes < 1:
            raise ValueError("Invalid log tail bounds")
        self.path = Path(path)
        self.tail_lines = tail_lines
        self.read_bytes = min(read_bytes, READ_BYTES)
        self.initial_bytes = min(initial_bytes, INITIAL_BYTES)
        self.identity = None
        self.offset = 0
        self.anchor = b""
        self.decoder = _decoder()

    def poll(self):
        try:
            stream = _open_shared(self.path)
        except FileNotFoundError:
            return ""
        with stream:
            stat = os.fstat(stream.fileno())
            identity = (stat.st_dev, stat.st_ino)
            first = self.identity is None
            if not first:
                stream.seek(max(0, self.offset - len(self.anchor)))
                same_anchor = stream.read(len(self.anchor)) == self.anchor
                if identity != self.identity or stat.st_size < self.offset or not same_anchor:
                    self.offset = 0
                    self.anchor = b""
                    self.decoder = _decoder()
            start = max(0, stat.st_size - self.initial_bytes) if first else self.offset
            aligned = start == 0
            if first and start:
                stream.seek(start - 1)
                aligned = stream.read(1) == b"\n"
            stream.seek(start)
            data = stream.read(min(stat.st_size - start, self.initial_bytes) if first else self.read_bytes)
            self.offset = stream.tell()
            self.identity = identity
            stream.seek(max(0, self.offset - 64))
            self.anchor = stream.read(self.offset - stream.tell())
        text = self.decoder.decode(data, final=False)
        if first:
            if not aligned:
                text = text.partition("\n")[2]  # Don't print a cropped partial first line.
            text = "".join(text.splitlines(keepends=True)[-self.tail_lines:]) if self.tail_lines else ""
        return text

    def finish(self):
        return self.decoder.decode(b"", final=True)


def read_log_tail(state_dir, limit=80, max_bytes=SNAPSHOT_BYTES):
    """Read only the current log, with no retained handle and a 32 KiB ceiling."""
    if type(limit) is not int or type(max_bytes) is not int or limit < 0 or max_bytes < 0:
        raise ValueError("Invalid log snapshot bounds")
    if not limit or not max_bytes:
        return ""
    budget = min(max_bytes, SNAPSHOT_BYTES)
    reader = LogTail(Path(state_dir).resolve() / "logs" / "daemon.log", min(limit, 1000), initial_bytes=budget)
    text = reader.poll()  # Don't flush an unfinished UTF-8 character from a live write.
    encoded = text.encode("utf-8")
    if len(encoded) > budget:
        start = len(encoded) - budget
        tail = encoded[start:]
        if encoded[start - 1] != 10:
            tail = tail.partition(b"\n")[2]
        text = tail.decode("utf-8")
    return text


def follow_log(reader, output=None, *, once=False, stop_event=None, poll_interval=.2):
    output = output or sys.stdout
    stop_event = stop_event or threading.Event()
    last_error = None
    while True:
        try:
            text = reader.poll()
            last_error = None
        except OSError as error:
            text = ""
            if type(error).__name__ != last_error:
                output.write("Log temporarily unavailable (" + type(error).__name__ + ").\n")
                output.flush()
                last_error = type(error).__name__
            if once:
                return 1
        if once:
            text += reader.finish() if reader.tail_lines else ""
        if text:
            output.write(text)
            output.flush()
        if once or stop_event.is_set():
            return 0
        if not text:
            stop_event.wait(poll_interval)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("SUPERMONO_STATE_DIR") or Path.home() / ".supermonoagent"))
    parser.add_argument("--once", action="store_true", help="Print current tail and exit")
    parser.add_argument("--tail", type=int, default=80, help="Initial lines (0–1000, bounded to 256 KiB)")
    args = parser.parse_args(argv)
    if not 0 <= args.tail <= 1000:
        parser.error("--tail must be between 0 and 1000")
    reader = LogTail(args.state_dir.resolve() / "logs" / "daemon.log", args.tail)
    if args.once:
        return follow_log(reader, once=True)
    with _viewer_instance(args.state_dir) as acquired:
        if not acquired:
            return 0
        print("SuperMonoAgent 实时日志；关闭此窗口不会结束后台终端。", flush=True)
        print("日志路径：" + str(reader.path), flush=True)
        if not reader.path.is_file():
            print("等待守护进程日志；旧后台需在会话结束后正常重启到新版，关闭本窗口不会结束终端。", flush=True)
        try:
            return follow_log(reader)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
