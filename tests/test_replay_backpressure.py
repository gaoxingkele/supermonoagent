import asyncio
import threading
import time

import pytest

from supermonoagent.hub import Hub
from supermonoagent.protocol import OP_OUTPUT, OP_SNAPSHOT, unpack
from test_windows import FakePty


def owned_sessions(hub, count):
    before = set(threading.enumerate())
    sessions = [hub.open_cli(hub.open_window().id, 'cmd') for _ in range(count)]
    # Bind handles while readers are alive: names alone must never select a
    # real daemon's reader or another concurrently running test's thread.
    readers = [thread for thread in set(threading.enumerate()) - before
               if thread.name in {f'pty-{s.id}' for s in sessions}
               and getattr(thread, '_target', None) == hub._read
               and any(getattr(thread, '_args', ()) and thread._args[0] is session for session in sessions)]
    assert len(readers) == count
    return sessions, readers


async def close_owned_sessions(hub, sessions, readers):
    for session in sessions:
        hub.close_window(session.window_id)

    def join_readers():
        deadline = time.monotonic() + 3
        for reader in readers:
            reader.join(max(0, deadline - time.monotonic()))
        assert not any(reader.is_alive() for reader in readers)

    await asyncio.to_thread(join_readers)
    # _read schedules close_session immediately before exiting. Keep this loop
    # alive until those already-enqueued callbacks and their follow-ups drain.
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.parametrize('capacity', [4, 32])
def test_forty_replays_are_lazy_and_late_bytes_are_in_snapshot(capacity):
    async def check():
        hub = Hub(pty_factory=lambda *_: FakePty())
        hub.attach_loop(asyncio.get_running_loop())
        sessions, readers = owned_sessions(hub, 40)
        client = hub.attach(asyncio.Queue(capacity)); client.multiplex = client.reliable = True
        try:
            for session in sessions:
                session.pump.remember(b'initial')
            hub.set_subscriptions(client, [s.id for s in sessions])
            assert not client.failure
            assert client.queue.qsize() < capacity
            pending = next(s for s in sessions if s.id in client.pending_replays)
            pending.pump.remember(b'-late')
            pending.pump.flushed_position = len(b'initial-late')
            hub._flushed(pending.id, b'-late', False)
            assert not client.failure and pending.id not in client.feed_offsets
            frames = []
            while not client.queue.empty():
                frames.append(client.queue.get_nowait()); hub.pump_replays(client)
                assert client.queue.qsize() < capacity
            decoded = [unpack(f) for f in frames]
            assert len(decoded) == 40 and all(op == OP_SNAPSHOT for op, _ in decoded)
            assert len({body.partition(b'\0')[0] for _, body in decoded}) == 40
            assert next(body for _, body in decoded if body.startswith(pending.id.encode()+b'\0')).endswith(b'initial-late')
            assert not client.pending_replays and not client.failure
        finally:
            hub.detach(client.key)
            await close_owned_sessions(hub, sessions, readers)
    asyncio.run(check())


def test_refresh_is_bounded_preserves_lease_and_cancels_removed_pending_views():
    async def check():
        hub = Hub(pty_factory=lambda *_: FakePty()); hub.attach_loop(asyncio.get_running_loop())
        sessions, readers = owned_sessions(hub, 40)
        client = hub.attach(asyncio.Queue(4)); client.multiplex = client.reliable = True
        try:
            ids = [s.id for s in sessions]
            hub.set_subscriptions(client, ids)
            sessions[0].resize_owner = client.key
            for _ in range(100): hub.refresh_subscriptions(client, ids)
            assert len(client.pending_replays) <= 40 and client.queue.qsize() == 1 and not client.failure
            assert client.replay_drain_credit == 0
            assert sessions[0].resize_owner == client.key
            removed = next(iter(client.pending_replays))
            hub.set_subscriptions(client, [sid for sid in ids if sid != removed])
            assert removed not in client.pending_replays
            # Detach clears pending identifiers without needing a reader/timer.
            hub.detach(client.key)
            assert not client.pending_replays
            assert client.replay_drain_credit == 0
            assert sessions[0].resize_owner is None
        finally:
            hub.detach(client.key)
            await close_owned_sessions(hub, sessions, readers)
    asyncio.run(check())


@pytest.mark.parametrize('change', ['authorization', 'session_close', 'window_close'])
def test_pending_views_are_not_sampled_after_target_or_auth_ends(change):
    async def check():
        hub = Hub(pty_factory=lambda *_: FakePty()); hub.attach_loop(asyncio.get_running_loop())
        sessions, readers = owned_sessions(hub, 8)
        client = hub.attach(asyncio.Queue(4)); client.multiplex = client.reliable = True
        try:
            hub.set_subscriptions(client, [s.id for s in sessions])
            sid = next(iter(client.pending_replays)); session = hub.sessions[sid]
            if change == 'authorization': client.authorize = lambda: False
            elif change == 'session_close': hub.close_session(sid)
            else: hub.close_window(session.window_id)
            while not client.queue.empty(): client.queue.get_nowait()
            hub.pump_replays(client)
            assert sid not in client.pending_replays and sid not in client.feed_offsets
        finally:
            hub.detach(client.key)
            await close_owned_sessions(hub, sessions, readers)
    asyncio.run(check())


@pytest.mark.parametrize('count,capacity', [(16, 32), (40, 4), (40, 32)])
@pytest.mark.parametrize('refresh', [False, True])
def test_same_tick_many_session_live_burst_does_not_compete_with_history(count, capacity, refresh):
    async def check():
        hub = Hub(pty_factory=lambda *_: FakePty()); hub.attach_loop(asyncio.get_running_loop())
        sessions, readers = owned_sessions(hub, count)
        client = hub.attach(asyncio.Queue(capacity)); client.multiplex = client.reliable = True
        try:
            for session in sessions: session.pump.remember(b'initial')
            hub.set_subscriptions(client, [s.id for s in sessions])
            if refresh: hub.refresh_subscriptions(client, [s.id for s in sessions])
            hub._catalog()
            # One synchronous callback batch: no scheduling opportunity for the sender.
            for session in sessions:
                session.pump.remember(b'-live')
                session.pump.flushed_position = len(b'initial-live')
                hub._flushed(session.id, b'-live', False)
            assert not client.failure
            assert len(client.pending_replays) <= count and client.queue.qsize() <= capacity
            displays = {}
            while not client.queue.empty():
                frame = client.queue.get_nowait(); hub.pump_replays(client)
                op, body = unpack(frame)
                if op in (OP_SNAPSHOT, OP_OUTPUT):
                    sid, _, text = body.partition(b'\0')
                    displays[sid] = text if op == OP_SNAPSHOT else displays.get(sid, b'') + text
            assert displays == {s.id.encode(): b'initial-live' for s in sessions}
        finally:
            hub.detach(client.key)
            await close_owned_sessions(hub, sessions, readers)
    asyncio.run(check())


@pytest.mark.parametrize('count,capacity,overflow', [(16, 32, False), (40, 32, True), (40, 4, True)])
def test_fully_active_live_only_burst_keeps_explicit_hard_queue_limit(count, capacity, overflow):
    async def check():
        hub = Hub(pty_factory=lambda *_: FakePty()); hub.attach_loop(asyncio.get_running_loop())
        sessions, readers = owned_sessions(hub, count)
        client = hub.attach(asyncio.Queue(capacity)); client.multiplex = client.reliable = True
        try:
            hub.set_subscriptions(client, [s.id for s in sessions])
            while not client.queue.empty(): client.queue.get_nowait(); hub.pump_replays(client)
            assert not client.pending_replays
            for session in sessions:
                session.pump.remember(b'live'); session.pump.flushed_position = 4
                hub._flushed(session.id, b'live', False)
            assert bool(client.failure) is overflow
            if overflow:
                assert 'queue overflow' in client.failure and client.queue.get_nowait() is None
            else:
                assert client.queue.qsize() == count
            assert all(not s.stop.is_set() and not s.pty.written for s in sessions)
        finally:
            hub.detach(client.key)
            await close_owned_sessions(hub, sessions, readers)
    asyncio.run(check())
