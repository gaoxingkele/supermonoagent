"""History opt-in uses the proven lazy replay queue and footer reader budget."""
import asyncio
import threading

import pytest

from supermonoagent.history_context import unpack_history_context
from supermonoagent.hub import Hub, Session
from supermonoagent.protocol import (CAPABILITIES, MAX_FRAME, OP_HISTORY_CONTEXT,
                                    OP_OUTPUT, OP_SESSIONS, OP_SNAPSHOT, pack_json, unpack)
from test_client_integration import isolated_host
from test_history_context import Loop, Pty


@pytest.mark.parametrize('capacity', [4, 32])
@pytest.mark.parametrize('resume', [False, True])
def test_atomic_history_replays_stay_lazy_and_ordered_with_legacy_and_live(capacity, resume):
    hub = Hub()
    sessions = []
    for index in range(20):
        window = hub.open_window(owner='owner')
        session = Session(f'{index:08x}', window.id, 'fixture', '.', Pty())
        session.pump.mark_grid(100, 28)
        session.pump.remember(b'initial-')
        window.sessions[session.id] = session
        window.focused = session.id
        hub.sessions[session.id] = session
        sessions.append(session)
    # These sessions already exist when a client connects. Creating them after
    # attach would exercise catalog broadcasts rather than replay admission.
    modern = hub.attach(asyncio.Queue(capacity), token='owner')
    legacy = hub.attach(asyncio.Queue(capacity), token='owner')
    for client in (modern, legacy):
        client.multiplex = client.reliable = True
        client.queue.put_nowait(pack_json(OP_SESSIONS, {'marker': 'first'}))
        client.queue.put_nowait(pack_json(OP_SESSIONS, {'marker': 'second'}))
    modern.history_context = True
    modern.history_resume = resume
    ids = [session.id for session in sessions]
    cursors = {sid: {'position': 3, 'epoch': 0, 'stream_id': hub.sessions[sid].pump.stream_id} for sid in ids}
    hub.set_subscriptions(modern, ids, history_after=cursors)
    hub.set_subscriptions(legacy, ids)
    for client in (modern, legacy):
        assert client.queue.qsize() == 2
        assert set(client.pending_replays) == set(ids)
        assert not client.feed_offsets

    # These output bytes are retained, not sent ahead of their first snapshot.
    for session in sessions:
        session.pump.remember(b'late')
        session.pump.flushed_position = session.pump.position()
        hub._flushed(session.id, b'late', False)
    for client in (modern, legacy):
        assert client.queue.qsize() == 2 and not client.failure
        frames = []
        while not client.queue.empty():
            frame = client.queue.get_nowait()
            frames.append(frame)
            hub.pump_replays(client, draining=True)
            assert client.queue.qsize() < capacity
            assert len(frame) <= MAX_FRAME + 4
        assert [unpack(frame)[0] for frame in frames[:2]] == [OP_SESSIONS, OP_SESSIONS]
        assert len(frames) == 22
        seen = set()
        for frame in frames[2:]:
            opcode, payload = unpack(frame)
            if client is modern:
                assert opcode == OP_HISTORY_CONTEXT
                sid, context, data = unpack_history_context(payload)
                assert (context['cols'], context['rows'], context['epoch']) == (100, 28, 0)
                assert context['kind'] == ('resume' if resume else 'snapshot')
                assert data == (b'tial-late' if resume else b'initial-late')
                if resume:
                    assert context['end_position'] == len(b'initial-late')
            else:
                assert opcode == OP_SNAPSHOT
                raw_sid, separator, data = payload.partition(b'\0')
                assert separator and data == b'initial-late'
                sid = raw_sid.decode()
            assert sid not in seen
            seen.add(sid)
        assert seen == set(ids)
        assert not client.pending_replays and not client.history_after and not client.failure

    # A pending pump batch already covered by the replay cannot duplicate bytes.
    for session in sessions:
        hub._flushed(session.id, b'late', False)
    assert modern.queue.empty() and legacy.queue.empty()
    session = sessions[0]
    loop = Loop()
    session.pump.add(b'fresh', loop, lambda data, overflow: hub._flushed(session.id, data, overflow))
    loop.fire()
    opcode, payload = unpack(modern.queue.get_nowait())
    assert opcode == OP_HISTORY_CONTEXT
    sid, context, data = unpack_history_context(payload)
    assert (sid, context['kind'], data) == (session.id, 'output', b'fresh')
    assert unpack(legacy.queue.get_nowait()) == (OP_OUTPUT, session.id.encode() + b'\0fresh')
    assert CAPABILITIES['replay_backpressure'] is True
    assert all(not session.pty.written and not session.pty.resize_calls for session in sessions)


def test_history_and_legacy_cancelled_footer_requests_share_four_actual_readers():
    async def scenario():
        release = threading.Event()
        count = maximum = 0
        lock = threading.Lock()
        async with isolated_host() as (daemon, _, _):
            session = daemon.hub.open_cli(daemon.hub.open_window(owner=daemon.token).id, 'cmd')

            def reader(*_):
                nonlocal count, maximum
                with lock:
                    count += 1
                    maximum = max(maximum, count)
                try:
                    assert release.wait(4)
                    return b'owned-footer'
                finally:
                    with lock:
                        count -= 1

            session.pty.footer_patch = reader
            clients, frames = [], []
            for kind in ['legacy', 'snapshot', 'resume', 'snapshot']:
                client = daemon.hub.attach(asyncio.Queue(4), token=daemon.token)
                client.multiplex = True
                client.subscriptions = {session.id}
                client.history_context = kind != 'legacy'
                client.history_resume = kind == 'resume'
                if client.history_resume:
                    client.history_after[session.id] = {'position': 0, 'epoch': 0, 'stream_id': session.pump.stream_id}
                clients.append(client)
                frames.append(daemon.hub._session_snapshot(client, session))
            tasks = [asyncio.create_task(daemon.snapshot_supplement(client, frame))
                     for client, frame in zip(clients, frames)]
            try:
                async with asyncio.timeout(2):
                    while count != 4:
                        await asyncio.sleep(.005)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                assert len(daemon._footer_jobs) == 4
                for client, frame in zip(clients, frames):
                    assert await daemon.snapshot_supplement(client, frame) is None
                assert maximum == 4
                release.set()
                async with asyncio.timeout(2):
                    while daemon._footer_jobs:
                        await asyncio.sleep(.005)
                assert await daemon.snapshot_supplement(clients[1], frames[1]) is not None
                assert not session.pty.written
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
                for client in clients:
                    daemon.hub.detach(client.key)
    asyncio.run(scenario())


@pytest.mark.parametrize('resume', [False, True])
def test_history_snapshot_footer_never_blocks_or_overtakes_live_context_frames(resume):
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        async with isolated_host() as (daemon, _, _):
            session = daemon.hub.open_cli(daemon.hub.open_window(owner=daemon.token).id, 'cmd')
            session.pump.remember(b'initial')

            def footer(*_):
                entered.set()
                assert release.wait(4)
                return b'OLD-FOOTER-MUST-NOT-OVERTAKE'

            session.pty.footer_patch = footer
            client = daemon.hub.attach(asyncio.Queue(32), token=daemon.token)
            client.multiplex = client.reliable = client.history_context = True
            client.history_resume = resume
            frames = []

            class Socket:
                async def send(self, frame):
                    frames.append(frame)
                    await asyncio.sleep(0)

                async def close(self, **_):
                    pass

            daemon.hub.set_subscriptions(client, [session.id])
            sender = asyncio.create_task(daemon._send(Socket(), client.queue, client))
            expected = b''.join(f'live-{index}\n'.encode() for index in range(40))
            try:
                async with asyncio.timeout(2):
                    while not entered.is_set():
                        await asyncio.sleep(.005)
                for index in range(40):
                    session.pump.add(f'live-{index}\n'.encode(), asyncio.get_running_loop(),
                        lambda data, overflow: daemon.hub._flushed(session.id, data, overflow))
                    await asyncio.sleep(.008)

                def live_bytes():
                    return b''.join(unpack_history_context(unpack(frame)[1])[2]
                        for frame in frames if unpack(frame)[0] == OP_HISTORY_CONTEXT
                        and unpack_history_context(unpack(frame)[1])[1]['kind'] == 'output')

                async with asyncio.timeout(2):
                    while live_bytes() != expected:
                        await asyncio.sleep(.005)
                assert not client.failure and len(expected) > client.queue.maxsize
                assert unpack(frames[0])[0] == OP_HISTORY_CONTEXT
                _, context, initial = unpack_history_context(unpack(frames[0])[1])
                assert context['kind'] == 'snapshot' and initial == b'initial'
                release.set()
                async with asyncio.timeout(2):
                    while daemon._footer_jobs or daemon._footer_tasks:
                        await asyncio.sleep(.005)
                assert not any(b'OLD-FOOTER-MUST-NOT-OVERTAKE' in frame for frame in frames)
                assert not session.pty.written
            finally:
                release.set()
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
                daemon.hub.detach(client.key)
    asyncio.run(scenario())
