"""Bounded JSONL transport for a new, privately owned coding CLI process.

The wire is deliberately independent of the assistant gateway and its policy.
Frames and stderr never enter logs. Only fixed failure codes leave this module.
"""
from __future__ import annotations

import asyncio
import codecs
from collections import deque
import inspect
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import threading


FRAME_BYTES = 4 * 1024 * 1024
PENDING_BYTES = 8 * 1024 * 1024
PENDING_FRAMES = 32


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError("duplicate protocol key")
        result[key] = value
    return result


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number): raise ValueError("nonfinite protocol number")
    return number


def _reject_constant(_):
    raise ValueError("nonfinite protocol number")


class ManagedProcessError(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


ManagedError = ManagedProcessError


def resolve_native_binary(name, binary=None):
    candidate = str(binary) if binary is not None else shutil.which(name)
    # Prefer the native installer over a PATH wrapper, without executing a shim.
    if binary is None and (not candidate or Path(candidate).suffix.lower() in {".cmd", ".bat", ".ps1"}):
        native = Path.home() / ".local" / "bin" / (name + ".exe")
        candidate = str(native) if native.is_file() else candidate
    if not candidate or Path(candidate).suffix.lower() in {".cmd", ".bat", ".ps1", ".js", ".mjs"}:
        raise ManagedProcessError("managed_native_executable_required")
    path = Path(candidate).resolve()
    if not path.is_file() or (os.name == "nt" and path.suffix.lower() != ".exe"):
        raise ManagedProcessError("managed_native_executable_required")
    return str(path)


def inherited_environment():
    env = os.environ.copy()
    for marker in ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "SUPERMONO_CAPTURE_PARENT", "SUPERMONO_MANAGED_PARENT"):
        env.pop(marker, None)
    return env


def _spawn_options(cwd, env):
    options = {"cwd": str(cwd), "env": env, "stdin": subprocess.PIPE,
               "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "bufsize": 0}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    return options


async def verify_version(binary, expected, *, process_factory=None):
    """Bounded local --version probe of the same executable subsequently used."""
    values = []
    # Version output is text rather than JSON; use a separate bounded reader.
    def probe():
        process = (process_factory or subprocess.Popen)([str(binary), "--version"],
            **_spawn_options(Path.cwd(), inherited_environment()))
        close_job = None
        try:
            if os.name == "nt" and getattr(process, "_handle", None) is not None:
                from .assistant_codex import _windows_job
                close_job = _windows_job(process)
            process.stdin.close()
            def read_version():
                data = bytearray()
                while True:
                    chunk = process.stdout.read(min(4097 - len(data), 1024))
                    if not chunk: break
                    data.extend(chunk)
                    if len(data) > 4096:
                        process.kill()
                        break
                values.append(bytes(data))
            def drain_error():
                while process.stderr.read(65536):
                    pass
            readers = [threading.Thread(target=read_version, daemon=True), threading.Thread(target=drain_error, daemon=True)]
            for thread in readers: thread.start()
            try:
                code = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
                raise ManagedProcessError("managed_version_unavailable") from None
            finally:
                if close_job is not None:
                    close_job()
                    close_job = None
                for thread in readers: thread.join(timeout=3)
            if any(thread.is_alive() for thread in readers) or code != 0 or not values or len(values[0]) > 4096:
                raise ManagedProcessError("managed_version_unavailable")
            version_text = values[0].decode("utf-8", errors="strict").strip()
            match = re.fullmatch(r"(?:codex-cli\s+)?(\d+\.\d+\.\d+)(?:\s+\(Claude Code\))?", version_text)
            if not match or match.group(1) != expected:
                raise ManagedProcessError("managed_version_unsupported")
            return match.group(1)
        finally:
            if close_job is not None: close_job()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
    task = asyncio.create_task(asyncio.to_thread(probe))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # The probe owns a real child too; cancellation never abandons it.
        await asyncio.shield(task)
        raise
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        raise ManagedProcessError("managed_version_unavailable") from None


class JsonLineProcess:
    """One reader/dispatcher and a physically serialized, fenced stdin writer."""
    def __init__(self, argv, cwd, *, on_frame, on_exit, can_send, process_factory=None, env=None):
        self.argv, self.cwd = list(argv), str(cwd)
        self.on_frame, self.on_exit, self.can_send = on_frame, on_exit, can_send
        self.process_factory, self.env = process_factory, inherited_environment() if env is None else env
        self.exit_code = None
        self.failure_reason = None
        self.process = None
        self._queue = queue.Queue(maxsize=PENDING_FRAMES)
        self._pending_bytes = 0
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._threads = []
        self._writers = set()
        self._close_job = None
        self._physical_done = threading.Event()
        self._closing = False
        self._closed = asyncio.Event()
        self._wake = asyncio.Event()
        self._close_task = None

    @property
    def alive(self):
        return self.process is not None and self.process.poll() is None

    async def start(self):
        if self.process is not None:
            raise ManagedProcessError("managed_process_already_started")
        self._loop = asyncio.get_running_loop()
        try:
            self.process = (self.process_factory or subprocess.Popen)(self.argv, **_spawn_options(self.cwd, self.env))
            if os.name == "nt" and getattr(self.process, "_handle", None) is not None:
                from .assistant_codex import _windows_job
                self._close_job = _windows_job(self.process)
        except (OSError, subprocess.SubprocessError):
            if self.process is not None:
                self.process.kill()
                self.process.wait(timeout=3)
                for stream in (self.process.stdin, self.process.stdout, self.process.stderr): stream.close()
            raise ManagedProcessError("managed_spawn_failed") from None
        self._threads = [threading.Thread(target=self._read_stdout, daemon=True),
                         threading.Thread(target=self._read_stderr, daemon=True)]
        for thread in self._threads: thread.start()
        self._waiter = threading.Thread(target=self._wait_process, daemon=True)
        self._waiter.start()
        self._dispatcher = asyncio.create_task(self._dispatch())

    def _notify(self):
        self._loop.call_soon_threadsafe(self._wake.set)

    def _fail(self, reason):
        with self._pending_lock:
            if self.failure_reason is None: self.failure_reason = reason
        self._kill_owned()
        self._notify()

    def _kill_owned(self):
        if self.process is None: return
        if self._close_job is not None:
            close, self._close_job = self._close_job, None
            close()
        try:
            if os.name != "nt" and self.process_factory is None:
                # Even an exited group leader can leave pipe-owning children.
                os.killpg(self.process.pid, signal.SIGKILL)
            elif self.process.poll() is None: self.process.kill()
        except (OSError, ProcessLookupError): pass

    def _read_stdout(self):
        buffer = bytearray()
        try:
            while True:
                data = self.process.stdout.read(65536)
                if not data:
                    if buffer: self._fail("managed_truncated_frame")
                    break
                buffer.extend(data)
                while True:
                    end = buffer.find(b"\n")
                    if end < 0: break
                    if end + 1 > FRAME_BYTES:
                        self._fail("managed_frame_limit")
                        return
                    raw = bytes(buffer[:end])
                    del buffer[:end + 1]
                    if not raw.strip(): continue
                    with self._pending_lock:
                        if self._pending_bytes + len(raw) + 1 > PENDING_BYTES:
                            overflow = True
                        else:
                            try:
                                self._queue.put_nowait(raw)
                                self._pending_bytes += len(raw) + 1
                                overflow = False
                            except queue.Full: overflow = True
                    if overflow:
                        self._fail("managed_pending_limit")
                        return
                    self._notify()
                if len(buffer) > FRAME_BYTES:
                    self._fail("managed_frame_limit")
                    return
        except (OSError, ValueError):
            if not self._closing: self._fail("managed_stdout_failed")

    def _read_stderr(self):
        try:
            while self.process.stderr.read(65536): pass
        except (OSError, ValueError):
            if not self._closing: self._fail("managed_stderr_failed")

    def _wait_process(self):
        self.exit_code = self.process.wait()
        # A process that exits may leave its own children holding pipe handles.
        self._kill_owned()
        for thread in self._threads: thread.join()
        self._physical_done.set()
        self._notify()

    async def _dispatch(self):
        try:
            while True:
                self._wake.clear()
                while not self._queue.empty():
                    raw = self._queue.get_nowait()
                    with self._pending_lock: self._pending_bytes -= len(raw) + 1
                    if self.failure_reason: continue
                    try:
                        frame = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                                           parse_float=_finite_float, parse_constant=_reject_constant)
                        if not isinstance(frame, dict): raise ValueError()
                    except (ValueError, UnicodeError, RecursionError):
                        self._fail("managed_invalid_frame")
                        continue
                    try:
                        await self.on_frame(frame)
                    except Exception:
                        self._fail("managed_event_persistence_failed")
                if self._physical_done.is_set() and self._queue.empty(): break
                await self._wake.wait()
            # Writer threads cannot remain blocked once the owned tree exits.
            if self._writers: await asyncio.gather(*list(self._writers), return_exceptions=True)
            await asyncio.to_thread(self._waiter.join)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                try: stream.close()
                except OSError: pass
            try: await self.on_exit(self.exit_code, self.failure_reason)
            except Exception:
                self.failure_reason = "managed_event_persistence_failed"
        finally:
            self._closed.set()

    async def send(self, frame, context):
        try: data = (json.dumps(frame, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        except (ValueError, UnicodeError, TypeError): raise ManagedProcessError("managed_invalid_send") from None
        if len(data) > FRAME_BYTES: raise ManagedProcessError("managed_frame_limit")
        context = dict(context)
        def physical_write():
            with self._write_lock:
                if self._closing or self.failure_reason or not self.alive: return False
                try: permitted = self.can_send(context) is True
                except Exception:
                    self._fail("managed_send_fence_failed")
                    raise ManagedProcessError("managed_send_fence_failed") from None
                if not permitted: return False
                try:
                    view = memoryview(data)
                    while view:
                        written = self.process.stdin.write(view)
                        if not written: raise OSError()
                        view = view[written:]
                    self.process.stdin.flush()
                    return True
                except (OSError, ValueError):
                    self._fail("managed_stdin_failed")
                    raise ManagedProcessError("managed_stdin_failed") from None
        task = asyncio.create_task(asyncio.to_thread(physical_write))
        self._writers.add(task)
        def completed(writer):
            self._writers.discard(writer)
            if not writer.cancelled(): writer.exception()
        task.add_done_callback(completed)
        return await asyncio.shield(task)

    async def wait_closed(self):
        await self._closed.wait()
        return self.exit_code

    async def close(self):
        if self.process is None:
            self._closed.set()
            return self.exit_code
        if self._close_task is None: self._close_task = asyncio.create_task(self._close())
        try: return await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            await asyncio.shield(self._close_task)
            raise

    async def _close(self):
        self._closing = True
        # Do not acquire the writer lock: a full pipe must not deadlock close.
        def stop():
            try: self.process.stdin.close()
            except (OSError, ValueError): pass
            try: self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._kill_owned()
                self.process.wait(timeout=3)
        await asyncio.to_thread(stop)
        # Calling close from a frame callback would self-join the dispatcher.
        if asyncio.current_task() is not self._dispatcher: await self.wait_closed()
        return self.exit_code


def sanitize_render_text(text):
    """Escape terminal controls without changing canonical message bodies."""
    return "".join(character if character in "\n\t" or (ord(character) >= 32 and not 127 <= ord(character) <= 159)
                   else f"\\x{ord(character):02x}" for character in str(text))


class ManagedPty:
    """Thread-safe, bounded, read-only observational terminal adapter."""
    def __init__(self, on_close):
        self.on_close = on_close
        self._condition = threading.Condition()
        self._chunks = deque()
        self._size = 0
        self._eof = False
        self._close_requested = False
        self._limit = 1024 * 1024
        self.overflowed = False
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try: self._loop = asyncio.get_running_loop()
        except RuntimeError: self._loop = None

    def feed_output(self, data):
        """Accept the whole chunk, or report a permanent observational gap.

        Existing queued bytes are never discarded. Producers should use small
        chunks and yield while available_capacity is low; False requires the
        coordinator to mark capture loss and stop claiming complete coverage.
        """
        if not isinstance(data, bytes): raise TypeError("observational output must be bytes")
        with self._condition:
            if self._eof or self.overflowed: return False
            if len(data) > self._limit: return self._overflow()
            # Incremental decoding escapes Unicode C1 too, without mistaking
            # UTF-8 continuation bytes in ordinary text for terminal controls.
            decoder_state = self._decoder.getstate()
            data = sanitize_render_text(self._decoder.decode(data)).encode("utf-8")
            if self._size + len(data) > self._limit:
                self._decoder.setstate(decoder_state)
                return self._overflow()
            if not data: return True
            self._chunks.append(data)
            self._size += len(data)
            self._condition.notify_all()
            return True

    @property
    def available_capacity(self):
        with self._condition:
            return max(0, self._limit - self._size)

    def _overflow(self):
        # Called under the condition; loss is explicit even when no marker fits.
        self.overflowed = True
        marker = b"[Managed output buffer truncated]\n"
        if self._size + len(marker) <= self._limit:
            self._chunks.append(marker)
            self._size += len(marker)
        self._condition.notify_all()
        return False

    def feed_eof(self):
        with self._condition:
            if self._eof: return not self.overflowed
            tail = self._decoder.decode(b"", final=True).encode("utf-8")
            if tail:
                if self._size + len(tail) > self._limit: self._overflow()
                else:
                    self._chunks.append(tail)
                    self._size += len(tail)
            self._eof = True
            self._condition.notify_all()
            return not self.overflowed

    def read(self, n=65536):
        if n <= 0: return b""
        with self._condition:
            while not self._chunks and not self._eof: self._condition.wait()
            if not self._chunks: return b""
            data = self._chunks.popleft()
            chunk, remaining = data[:n], data[n:]
            if remaining: self._chunks.appendleft(remaining)
            self._size -= len(chunk)
            return chunk

    def write(self, data):
        raise PermissionError("managed terminal is read-only")

    def resize(self, cols, rows):
        pass

    def close(self):
        with self._condition:
            if self._close_requested: return
            self._close_requested = True
        def notify():
            result = self.on_close()
            if inspect.isawaitable(result): asyncio.ensure_future(result)
        if self._loop is not None: self._loop.call_soon_threadsafe(notify)
        else: notify()
