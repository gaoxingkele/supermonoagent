"""Attach a real Windows console to a daemon-owned shared shell.

This is a terminal *client*. It never creates a local shell or ends a remote
session on disconnect. Ctrl+] disconnects; Ctrl+C reaches the shared shell.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import re
from pathlib import Path
import sys
import threading
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .protocol import OP_INPUT_SESSION, OP_SUBSCRIBE, pack, pack_json, unpack, unpack_json


def connection_url(value: str) -> str:
    parsed = urlsplit(value)
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
    if scheme not in {"ws", "wss"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Use a ws(s) URL without credentials, query, or fragment")
    # urlsplit delays validation of malformed/out-of-range ports until access.
    parsed.port
    path = "/ws" if parsed.path in {"", "/"} else parsed.path
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def load_access_token(path: Path | None = None, environ=None) -> str:
    environ = os.environ if environ is None else environ
    token = environ.get("SUPERMONO_TOKEN", "").strip()
    if not token:
        state = Path(environ.get("SUPERMONO_STATE_DIR") or Path.home() / ".supermonoagent")
        path = path or state / "token"
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    if not token:
        raise ValueError("No access token: set SUPERMONO_TOKEN or supply --token-file (daemon token is used locally)")
    return token


def session_rows(state: dict) -> list[dict]:
    return [{**session, "window": window["id"], "title": window.get("title", "")}
            for window in state.get("windows", []) for session in window.get("sessions", [])]


def input_frame(session: str, data: bytes) -> bytes:
    return pack(OP_INPUT_SESSION, session.encode("utf-8") + b"\0" + data)


def split_feed(payload: bytes) -> tuple[str, bytes]:
    session, sep, data = payload.partition(b"\0")
    if not sep:
        raise ValueError("Expected a session-prefixed terminal frame")
    return session.decode("utf-8"), data


def disconnect_input(data: bytes) -> tuple[bytes, bool]:
    """Recognize Ctrl+] in ordinary VT or ConPTY's Win32 input mode."""
    matches = [offset for offset in (data.find(b"\x1d"),) if offset >= 0]
    win32 = re.search(rb"\x1b\[\d*;\d*;29;1;\d*;\d*_", data)
    if win32:
        matches.append(win32.start())
    return (data[:min(matches)], True) if matches else (data, False)


class WindowsConsole:
    """Use byte-oriented Win32 console I/O with UTF-8 code pages and VT modes."""
    def __enter__(self):
        if sys.platform != "win32":
            raise ValueError("Interactive attach requires a Windows console; --list works on other systems")
        from ctypes import wintypes as w
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k.GetStdHandle.argtypes = [w.DWORD]
        self.k.GetStdHandle.restype = w.HANDLE
        self.k.GetConsoleMode.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        self.k.SetConsoleMode.argtypes = [w.HANDLE, w.DWORD]
        self.k.ReadFile.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p]
        self.k.WriteFile.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p]
        self.k.GetConsoleScreenBufferInfo.argtypes = [w.HANDLE, ctypes.c_void_p]
        self.input = self.k.GetStdHandle(-10 & 0xFFFFFFFF)
        self.output = self.k.GetStdHandle(-11 & 0xFFFFFFFF)
        self.saved = []
        self.pages = (self.k.GetConsoleCP(), self.k.GetConsoleOutputCP())
        for handle in (self.input, self.output):
            mode = w.DWORD()
            if not self.k.GetConsoleMode(handle, ctypes.byref(mode)):
                raise ValueError("Attach needs interactive console stdin and stdout")
            self.saved.append((handle, mode.value))
        try:
            # Disable processed input (Ctrl+C), echo, line mode, quick edit;
            # VT input emits terminal sequences for cursor/function keys.
            if not self.k.SetConsoleMode(self.input, (self.saved[0][1] & ~0x47) | 0x280):
                raise ctypes.WinError(ctypes.get_last_error())
            if not self.k.SetConsoleMode(self.output, self.saved[1][1] | 0x5):
                raise ctypes.WinError(ctypes.get_last_error())
            if not self.k.SetConsoleCP(65001) or not self.k.SetConsoleOutputCP(65001):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        for handle, mode in self.saved:
            self.k.SetConsoleMode(handle, mode)
        self.k.SetConsoleCP(self.pages[0])
        self.k.SetConsoleOutputCP(self.pages[1])

    def read(self) -> bytes:
        from ctypes import wintypes as w
        buf = ctypes.create_string_buffer(4096)
        count = w.DWORD()
        if not self.k.ReadFile(self.input, buf, len(buf), ctypes.byref(count), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return buf.raw[:count.value]

    def write(self, data: bytes) -> None:
        from ctypes import wintypes as w
        while data:
            count = w.DWORD()
            if not self.k.WriteFile(self.output, data, len(data), ctypes.byref(count), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if not count.value:
                raise OSError("Console output made no progress")
            data = data[count.value:]

    def cancel_read(self, thread_id: int) -> None:
        """Release synchronous ReadFile before restoring console input mode."""
        from ctypes import wintypes as w
        self.k.OpenThread.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.k.OpenThread.restype = w.HANDLE
        self.k.CancelSynchronousIo.argtypes = [w.HANDLE]
        self.k.CloseHandle.argtypes = [w.HANDLE]
        handle = self.k.OpenThread(0x0001, False, thread_id)  # THREAD_TERMINATE
        if handle:
            try:
                self.k.CancelSynchronousIo(handle)
            finally:
                self.k.CloseHandle(handle)

    def size(self) -> tuple[int, int]:
        # CONSOLE_SCREEN_BUFFER_INFO: COORD, COORD, WORD, SMALL_RECT, COORD.
        import struct
        buf = ctypes.create_string_buffer(22)
        if not self.k.GetConsoleScreenBufferInfo(self.output, buf):
            raise ctypes.WinError(ctypes.get_last_error())
        left, top, right, bottom = struct.unpack_from("hhhh", buf.raw, 10)
        return right - left + 1, bottom - top + 1


async def receive_state(ws) -> dict:
    async with asyncio.timeout(15):
        while True:
            message = await ws.recv()
            if isinstance(message, str):
                ctrl = json.loads(message)
                if ctrl.get("error"):
                    raise ValueError(str(ctrl["error"]))
                continue
            op, payload = unpack(message)
            if op in (2, 7):
                state = unpack_json(payload)
                if state.get("error"):
                    raise ValueError(str(state["error"]))
                return state
            if op == 10:
                await ws.send(pack(11, payload))


async def attach(args) -> None:
    token = load_access_token(args.token_file)
    headers = None if args.relay_code else {"Authorization": f"Bearer {token}"}
    async with connect(connection_url(args.url), additional_headers=headers, max_size=512 * 1024, compression=None) as ws:
        if args.relay_code:
            await ws.send(json.dumps({"op": "phone", "code": args.relay_code, "token": token, "reliable": True}))
        await ws.send(pack_json(1, {"token": token, "multiplex": True, "reliable": True}))
        state = await receive_state(ws)
        if not state.get("capabilities", {}).get("multiplex"):
            raise ValueError("Host does not support negotiated multiplexing; upgrade the daemon")
        if args.list:
            print(json.dumps(session_rows(state), ensure_ascii=False, indent=2))
            return
        if args.shell:
            existing = {row["id"] for row in session_rows(state)}
            await ws.send(pack_json(6, {"shell": args.shell, "cwd": args.cwd}))
            async with asyncio.timeout(15):
                while True:
                    state = await receive_state(ws)
                    new = [row["id"] for row in session_rows(state) if row["id"] not in existing]
                    if new:
                        args.session = state.get("session") or new[-1]
                        break
        if not args.session or args.session not in {row["id"] for row in session_rows(state)}:
            raise ValueError("Choose an existing --session from --list, or create one with --shell powershell/cmd")
        with WindowsConsole() as console:
            await ws.send(pack_json(OP_SUBSCRIBE, {"sessions": [args.session]}))
            await run_console(ws, console, args.session, args.take_control)


async def run_console(ws, console, session: str, take_control: bool = False) -> None:
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue(maxsize=64)
    stopped = threading.Event()

    def reader():
        while not stopped.is_set():
            try:
                data = console.read()
                if not data:
                    return
                # Backpressure instead of dropping user keystrokes.
                future = asyncio.run_coroutine_threadsafe(queue.put(data), loop)
                while not stopped.is_set():
                    try:
                        future.result(timeout=0.2)
                        break
                    except TimeoutError:
                        continue
                if stopped.is_set():
                    future.cancel()
            except (OSError, RuntimeError):
                return

    async def input_loop():
        pending = b""
        while True:
            data = pending + await queue.get()
            pending = b""
            # A ReadFile buffer may end midway through a Win32 key record.
            partial = re.search(rb"\x1b\[[0-9;]+$", data)
            if partial and len(data) - partial.start() < 128:
                pending, data = data[partial.start():], data[:partial.start()]
            before, escape = disconnect_input(data)
            if before:
                await ws.send(input_frame(session, before))
            if escape:
                return

    async def resize_loop():
        previous = None
        while True:
            size = console.size()
            if size != previous:
                await ws.send(pack_json(5, {"session": session, "cols": size[0], "rows": size[1], "take_control": take_control}))
                previous = size
            await asyncio.sleep(0.1)

    async def output_loop():
        async for message in ws:
            if isinstance(message, str):
                continue
            op, payload = unpack(message)
            if op in (3, 8):
                sid, data = split_feed(payload)
                if sid == session:
                    console.write(data)
            elif op == 10:
                await ws.send(pack(11, payload))
            elif op in (2, 7):
                state = unpack_json(payload)
                if state.get("error"):
                    raise ValueError(str(state["error"]))
                if session not in {row["id"] for row in session_rows(state)}:
                    return

    input_thread = threading.Thread(target=reader, daemon=True, name="native-console-input")
    input_thread.start()
    tasks = [asyncio.create_task(coro()) for coro in (input_loop, resize_loop, output_loop)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        stopped.set()
        cancel_read = getattr(console, "cancel_read", None)
        if cancel_read and input_thread.native_id is not None:
            cancel_read(input_thread.native_id)
        input_thread.join(timeout=0.25)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Deliberately no OP_CLOSE: the daemon and other clients keep the shell.


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--url", default=os.environ.get("SUPERMONO_URL", "ws://127.0.0.1:6769/ws"))
    result.add_argument("--token-file", type=Path)
    result.add_argument("--relay-code", default="")
    choice = result.add_mutually_exclusive_group()
    choice.add_argument("--list", action="store_true")
    choice.add_argument("--session")
    choice.add_argument("--shell")
    result.add_argument("--cwd")
    result.add_argument("--take-control", action="store_true")
    return result


def main() -> None:
    try:
        asyncio.run(attach(parser().parse_args()))
    except (ValueError, OSError, TimeoutError, ConnectionClosed) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
