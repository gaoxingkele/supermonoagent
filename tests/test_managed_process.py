"""Owned transport and observational terminal tests; never launch a model CLI."""
import asyncio
import sys
import threading

import pytest

from supermonoagent.managed_process import JsonLineProcess, ManagedPty


def test_serialized_physical_send_rechecks_fence(tmp_path):
    async def run():
        frames, exits = [], []
        live = True
        async def frame(value): frames.append(value)
        async def exited(code, reason): exits.append((code, reason))
        process = JsonLineProcess([sys.executable, "-u", "-c", "import sys; [print(x.strip(),flush=True) for x in sys.stdin]"],
                                  tmp_path, on_frame=frame, on_exit=exited, can_send=lambda _: live)
        await process.start()
        process._write_lock.acquire()
        send = asyncio.create_task(process.send({"secret": "not sent"}, {"operation": "submit"}))
        await asyncio.sleep(.03)
        live = False
        process._write_lock.release()
        assert await send is False
        await process.close()
        assert frames == [] and len(exits) == 1
        assert not process.alive
        assert all(not thread.is_alive() for thread in process._threads)
    asyncio.run(run())


def test_multiturn_frames_have_no_lifetime_budget(tmp_path):
    async def run():
        count = 0
        async def frame(value):
            nonlocal count
            count += 1
        async def exited(*_): pass
        process = JsonLineProcess([sys.executable, "-u", "-c", "import sys; [print(x.strip(),flush=True) for x in sys.stdin]"],
                                  tmp_path, on_frame=frame, on_exit=exited, can_send=lambda _: True)
        await process.start()
        for index in range(12):
            await process.send({"text": "x" * 500000}, {"operation": "submit"})
            for _ in range(200):
                if count == index + 1: break
                await asyncio.sleep(.005)
            assert count == index + 1
        await process.close()
        assert process.failure_reason is None
    asyncio.run(run())


def test_oversized_frame_is_fixed_failure_and_joined(tmp_path):
    async def run():
        exits = []
        async def frame(_): pytest.fail("oversized frame was forwarded")
        async def exited(code, reason): exits.append(reason)
        process = JsonLineProcess([sys.executable, "-c", "import sys; sys.stderr.write('SECRET'); print('x'*4194305)"],
                                  tmp_path, on_frame=frame, on_exit=exited, can_send=lambda _: True)
        await process.start()
        await asyncio.wait_for(process.wait_closed(), 10)
        assert exits == ["managed_frame_limit"]
        assert all(not thread.is_alive() for thread in process._threads)
    asyncio.run(run())


def test_pty_close_is_not_eof_and_read_returns_byte_chunks():
    closed = []
    pty = ManagedPty(lambda: closed.append(True))
    pty.feed_output(b"a\x1b]52;SECRET\x07\nbc")
    pty.close()
    assert closed == [True]
    with pytest.raises(PermissionError): pty.write(b"input")
    assert pty.read(2) == b"a\\"
    assert b"\x1b" not in pty.read(1024)
    result = []
    reader = threading.Thread(target=lambda: result.append(pty.read(10)))
    reader.start()
    reader.join(.05)
    assert reader.is_alive()
    pty.feed_eof()
    reader.join(1)
    assert result == [b""]


def test_pending_overflow_fails_closed_without_late_final(tmp_path):
    async def run():
        frames, exits = [], []
        entered = asyncio.Event()
        release = asyncio.Event()
        async def frame(value):
            frames.append(value)
            entered.set()
            await release.wait()
        async def exited(code, reason): exits.append(reason)
        code = "import time; print('{}',flush=True); time.sleep(.15); print('{}\\n'*40,flush=True); time.sleep(10)"
        process = JsonLineProcess([sys.executable, "-u", "-c", code], tmp_path,
            on_frame=frame, on_exit=exited, can_send=lambda _: True)
        await process.start()
        await asyncio.wait_for(entered.wait(), 3)
        for _ in range(200):
            if process.failure_reason: break
            await asyncio.sleep(.005)
        assert process.failure_reason == "managed_pending_limit"
        release.set()
        await asyncio.wait_for(process.wait_closed(), 5)
        assert len(frames) == 1 and exits == ["managed_pending_limit"]
    asyncio.run(run())


def test_close_cancellation_still_joins_owned_readers(tmp_path):
    async def run():
        async def frame(_): pass
        async def exited(*_): pass
        process = JsonLineProcess([sys.executable, "-u", "-c", "import time; time.sleep(10)"], tmp_path,
            on_frame=frame, on_exit=exited, can_send=lambda _: True)
        await process.start()
        close = asyncio.create_task(process.close())
        await asyncio.sleep(.01)
        close.cancel()
        with pytest.raises(asyncio.CancelledError): await close
        assert not process.alive and all(not thread.is_alive() for thread in process._threads)
    asyncio.run(run())


def test_pty_bounds_escapes_c1_and_keeps_split_unicode():
    pty = ManagedPty(lambda: None)
    text = "汉字".encode()
    pty.feed_output(text[:2])
    pty.feed_output(text[2:] + "\u009b31m".encode())
    assert pty.read(1024).decode() == "汉字\\x9b31m"
    pty.feed_output(b"z" * (2 * 1024 * 1024))
    assert pty._size <= pty._limit
    assert b"truncated" in pty.read(100)
    pty.feed_eof()


def test_persistence_failure_terminates_before_later_frames(tmp_path):
    async def run():
        called, exits = [], []
        async def frame(value):
            called.append(value)
            raise OSError("PRIVATE persistence diagnostic")
        async def exited(code, reason): exits.append(reason)
        process = JsonLineProcess([sys.executable, "-u", "-c", "import time; print('{}\\n{}',flush=True); time.sleep(10)"],
            tmp_path, on_frame=frame, on_exit=exited, can_send=lambda _: True)
        await process.start()
        await asyncio.wait_for(process.wait_closed(), 5)
        assert len(called) == 1 and exits == ["managed_event_persistence_failed"]
        assert not process.alive
    asyncio.run(run())


def test_pty_reports_overflow_without_discarding_queued_capture():
    pty = ManagedPty(lambda: None)
    pty._limit = 128
    assert pty.feed_output(b"a" * 100) is True
    assert pty.available_capacity == 28
    assert pty.feed_output(b"b" * 30) is False
    assert pty.overflowed is True
    assert pty.read(128) == b"a" * 100
    assert pty.feed_output(b"c") is False
    pty.feed_eof()
    assert pty.feed_output(b"after EOF") is False


@pytest.mark.parametrize("raw", [
    b'{"type":"result","type":"other"}\n',
    b'{"request":{"decision":"deny","decision":"allow"}}\n',
    b'{"value":NaN}\n', b'{"value":Infinity}\n', b'{"value":-Infinity}\n',
    b'{"value":1e999}\n',
])
def test_protocol_rejects_duplicate_keys_and_nonfinite_numbers(tmp_path, raw):
    async def run():
        frames, exits = [], []
        async def frame(value): frames.append(value)
        async def exited(code, reason): exits.append(reason)
        code = f"import sys,time; sys.stdout.buffer.write({raw!r}); sys.stdout.flush(); time.sleep(10)"
        process = JsonLineProcess([sys.executable, "-u", "-c", code], tmp_path,
            on_frame=frame, on_exit=exited, can_send=lambda _: True)
        await process.start()
        try:
            await asyncio.wait_for(process.wait_closed(), 5)
            assert not frames and exits == ["managed_invalid_frame"]
            assert not process.alive
        finally:
            await process.close()
    asyncio.run(run())


def test_pty_close_callback_may_return_existing_task_without_double_execution():
    async def run():
        loop = asyncio.get_running_loop()
        failures = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _, context: failures.append(context))
        calls = []
        executions = []
        completed = asyncio.Event()
        async def close_driver():
            executions.append(True)
            await asyncio.sleep(0)
            completed.set()
        def on_close():
            calls.append(True)
            return asyncio.create_task(close_driver())
        try:
            pty = ManagedPty(on_close)
            pty.close()
            pty.close()
            await asyncio.wait_for(completed.wait(), 1)
            await asyncio.sleep(0)
            assert failures == []
            assert calls == [True] and executions == [True]
            assert pty._eof is False
            assert pty.feed_output(b"driver still draining") is True
            assert pty.read(1024) == b"driver still draining"
            pty.feed_eof()
            assert pty.read(1024) == b""
        finally:
            loop.set_exception_handler(previous_handler)
    asyncio.run(run())
