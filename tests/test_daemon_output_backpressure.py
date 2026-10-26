import asyncio
import threading
import json

import pytest
from websockets.asyncio.client import connect

from supermonoagent.protocol import OP_OUTPUT, OP_SNAPSHOT, OP_PING, OP_PONG, OP_HELLO, OP_HELLO_OK, OP_SUBSCRIBE, pack, pack_feed, pack_json, unpack
from test_client_integration import isolated_host, subscribed, receive_op


def test_gated_footer_does_not_block_more_than_queue_capacity_live_frames():
    async def check():
        entered, release = threading.Event(), threading.Event()
        async with isolated_host() as (daemon, _, _):
            session = daemon.hub.open_cli(daemon.hub.open_window(owner=daemon.token).id, 'cmd')
            def footer(*_):
                entered.set(); release.wait(5); return b'OLD-FOOTER'
            session.pty.footer_patch = footer
            client = daemon.hub.attach(asyncio.Queue(32), token=daemon.token)
            client.multiplex = client.reliable = True
            sent = []
            class Socket:
                async def send(self, frame): sent.append(frame); await asyncio.sleep(0)
                async def close(self, **_): pass
            daemon.hub.set_subscriptions(client, [session.id])
            sender = asyncio.create_task(daemon._send(Socket(), client.queue, client))
            try:
                async with asyncio.timeout(2):
                    while not entered.is_set(): await asyncio.sleep(.005)
                for i in range(40):
                    daemon.hub._flushed(session.id, str(i).encode()+b'\n', False)
                    await asyncio.sleep(.003)
                assert not client.failure
                async with asyncio.timeout(1):
                    while sum(unpack(f)[0] == OP_OUTPUT for f in sent) != 40: await asyncio.sleep(.005)
                release.set()
                await asyncio.sleep(.05)
                assert not any(b'OLD-FOOTER' in f for f in sent)
                assert unpack(sent[0])[0] == OP_SNAPSHOT
            finally:
                release.set(); sender.cancel(); await asyncio.gather(sender, return_exceptions=True)
                daemon.hub.detach(client.key)
    asyncio.run(check())


def test_cancelled_awaiters_do_not_release_real_footer_thread_slots():
    async def check():
        release = threading.Event()
        count, maximum = 0, 0
        lock = threading.Lock()
        async with isolated_host() as (daemon, _, _):
            session = daemon.hub.open_cli(daemon.hub.open_window(owner=daemon.token).id, 'cmd')
            def reader(*_):
                nonlocal count, maximum
                with lock: count += 1; maximum = max(maximum, count)
                try: release.wait(5); return b'footer'
                finally:
                    with lock: count -= 1
            session.pty.footer_patch = reader
            client = daemon.hub.attach(asyncio.Queue(32), token=daemon.token); client.multiplex = True
            daemon.hub.set_subscriptions(client, [session.id])
            frame = pack_feed(OP_SNAPSHOT, session.id, b'raw')
            jobs = [asyncio.create_task(daemon.snapshot_supplement(client, frame)) for _ in range(4)]
            try:
                async with asyncio.timeout(2):
                    while count != 4: await asyncio.sleep(.005)
                for job in jobs: job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
                assert len(daemon._footer_jobs) == 4
                for _ in range(12): assert await daemon.snapshot_supplement(client, frame) is None
                assert maximum == 4
                release.set()
                async with asyncio.timeout(2):
                    while daemon._footer_jobs: await asyncio.sleep(.005)
                assert await daemon.snapshot_supplement(client, frame) is not None
            finally:
                release.set(); daemon.hub.detach(client.key)
    asyncio.run(check())


def test_late_footer_does_not_overtake_live_waiting_for_network_send():
    async def check():
        entered, release = threading.Event(), threading.Event()
        blocked, network = asyncio.Event(), asyncio.Event()
        async with isolated_host() as (daemon, _, _):
            session = daemon.hub.open_cli(daemon.hub.open_window(owner=daemon.token).id, 'cmd')
            def footer(*_): entered.set(); release.wait(5); return b'OLD-FOOTER'
            session.pty.footer_patch = footer
            client = daemon.hub.attach(asyncio.Queue(4), token=daemon.token); client.multiplex = client.reliable = True
            sent = []
            class Socket:
                async def send(self, frame):
                    if unpack(frame)[0] == OP_OUTPUT:
                        blocked.set(); await network.wait()
                    sent.append(frame)
                async def close(self, **_): pass
            daemon.hub.set_subscriptions(client, [session.id])
            sender = asyncio.create_task(daemon._send(Socket(), client.queue, client))
            try:
                async with asyncio.timeout(2):
                    while not entered.is_set(): await asyncio.sleep(.005)
                daemon.hub._flushed(session.id, b'QUEUED-LIVE', False)
                await asyncio.wait_for(blocked.wait(), 2)
                assert not any(b'QUEUED-LIVE' in f for f in sent)
                release.set()
                async with asyncio.timeout(2):
                    while daemon._footer_tasks: await asyncio.sleep(.005)
                assert not any(b'OLD-FOOTER' in f for f in sent)
                network.set()
                async with asyncio.timeout(2):
                    while not any(b'QUEUED-LIVE' in f for f in sent): await asyncio.sleep(.005)
                assert not client.failure
            finally:
                release.set(); network.set(); sender.cancel(); await asyncio.gather(sender, return_exceptions=True)
                daemon.hub.detach(client.key)
    asyncio.run(check())


def test_relay_checks_late_footer_again_at_actual_outbound(monkeypatch):
    async def check():
        entered, release = threading.Event(), threading.Event()
        blocked, network = asyncio.Event(), asyncio.Event()
        async with isolated_host() as (daemon, _, relay):
            a = daemon.hub.open_cli(daemon.hub.open_window(owner=daemon.token).id, 'cmd')
            b = daemon.hub.open_cli(daemon.hub.open_window(owner=daemon.token).id, 'cmd')
            def footer(*_): entered.set(); release.wait(5); return b'OLD-FOOTER'
            a.pty.footer_patch = footer
            async with subscribed(relay, [a.id, b.id], relay=True) as socket:
                async with asyncio.timeout(2):
                    while not entered.is_set(): await asyncio.sleep(.005)
                uplink = next(iter(daemon._relay_sockets.values()))
                original = uplink.send
                async def gated(data, *args, **kwargs):
                    if isinstance(data, bytes) and b'BLOCK-OTHER' in data:
                        blocked.set(); await network.wait()
                    return await original(data, *args, **kwargs)
                monkeypatch.setattr(uplink, 'send', gated)
                try:
                    daemon.hub._flushed(b.id, b'BLOCK-OTHER', False)
                    await asyncio.wait_for(blocked.wait(), 2)
                    release.set()
                    async with asyncio.timeout(2):
                        while daemon._footer_tasks: await asyncio.sleep(.005)
                    # Footer is now queued behind B; A live has not hit the network.
                    daemon.hub._flushed(a.id, b'A-QUEUED-LIVE', False)
                    network.set()
                    await socket.send(pack(OP_PING, b'done'))
                    frames = []
                    async with asyncio.timeout(3):
                        while True:
                            frame = await socket.recv(); frames.append(frame)
                            if isinstance(frame, bytes) and unpack(frame) == (OP_PONG, b'done'): break
                    assert any(b'A-QUEUED-LIVE' in f for f in frames if isinstance(f, bytes))
                    assert not any(b'OLD-FOOTER' in f for f in frames if isinstance(f, bytes))
                finally:
                    release.set(); network.set()
    asyncio.run(check())


@pytest.mark.parametrize('relay', [False, True])
@pytest.mark.parametrize('capacity', [4, 32])
def test_forty_owned_windows_replay_over_real_socket_without_1013(monkeypatch, relay, capacity):
    monkeypatch.setattr('supermonoagent.daemon.QUEUE_FRAMES', capacity)
    async def check():
        async with isolated_host() as (daemon, direct, uplink):
            sessions = [daemon.hub.open_cli(daemon.hub.open_window(owner=daemon.token).id, 'cmd') for _ in range(40)]
            for session in sessions: session.pump.remember(b'owned-history')
            headers = {} if relay else {'Authorization': 'Bearer '+daemon.token}
            async with connect(uplink if relay else direct, additional_headers=headers, max_size=1024*1024) as ws:
                if relay:
                    await ws.send(json.dumps({'op': 'phone', 'code': 'local-test', 'token': daemon.token, 'reliable': True}))
                    assert json.loads(await ws.recv())['op'] == 'ready'
                await ws.send(pack_json(OP_HELLO, {'token': daemon.token, 'multiplex': True, 'reliable': True}))
                await receive_op(ws, OP_HELLO_OK)
                await ws.send(pack_json(OP_SUBSCRIBE, {'sessions': [s.id for s in sessions]}))
                seen = set()
                async with asyncio.timeout(4):
                    while len(seen) < 40:
                        frame = await ws.recv()
                        if isinstance(frame, bytes) and unpack(frame)[0] == OP_SNAPSHOT:
                            sid, _, body = unpack(frame)[1].partition(b'\0')
                            assert body == b'owned-history'; seen.add(sid)
                assert seen == {s.id.encode() for s in sessions}
                assert all(not client.failure for client in daemon.hub.clients.values())
                assert all(not s.pty.written for s in sessions)
    asyncio.run(check())


def test_pending_replays_finish_while_live_backlog_never_becomes_empty():
    async def check():
        async with isolated_host() as (daemon, _, _):
            sessions = [daemon.hub.open_cli(daemon.hub.open_window(owner=daemon.token).id, 'cmd') for _ in range(17)]
            active = sessions[0]
            client = daemon.hub.attach(asyncio.Queue(8), token=daemon.token); client.multiplex = client.reliable = True
            daemon.hub.set_subscriptions(client, [active.id])
            first, done = asyncio.Event(), asyncio.Event()
            seen, depths, sends = set(), [], 0
            class Socket:
                async def send(self, frame):
                    nonlocal sends
                    sends += 1
                    if unpack(frame)[0] == OP_SNAPSHOT: seen.add(unpack(frame)[1].partition(b'\0')[0])
                    if len(seen) == len(sessions) or sends >= 300: done.set()
                    while client.queue.qsize() < 2:
                        active.pump.remember(b'x')
                        active.pump.flushed_position += 1
                        daemon.hub._flushed(active.id, b'x', False)
                    depths.append(client.queue.qsize())
                    first.set()
                    await asyncio.sleep(0)
                async def close(self, **_): pass
            sender = asyncio.create_task(daemon._send(Socket(), client.queue, client))
            try:
                await asyncio.wait_for(first.wait(), 2)
                daemon.hub.set_subscriptions(client, [s.id for s in sessions])
                await asyncio.wait_for(done.wait(), 2)
                assert seen == {s.id.encode() for s in sessions}
                assert sends <= 140 and not client.failure and not client.pending_replays
                assert all(2 <= depth <= 3 for depth in depths)
            finally:
                sender.cancel(); await asyncio.gather(sender, return_exceptions=True)
                daemon.hub.detach(client.key)
    asyncio.run(check())


@pytest.mark.parametrize('count', [16, 40])
def test_all_quiet_windows_eventually_get_footer_after_first_four_reads_are_gated(count):
    async def check():
        release = threading.Event()
        calls, active, maximum = 0, 0, 0
        lock = threading.Lock()
        async with isolated_host() as (daemon, _, _):
            sessions = [daemon.hub.open_cli(daemon.hub.open_window(owner=daemon.token).id, 'cmd') for _ in range(count)]
            def footer(*_):
                nonlocal calls, active, maximum
                with lock: calls += 1; active += 1; maximum = max(maximum, active)
                try: release.wait(5); return b'QUIET-FOOTER'
                finally:
                    with lock: active -= 1
            for session in sessions:
                session.pty.footer_patch = footer
                session.pump.remember(b'h' * 100000)
            client = daemon.hub.attach(asyncio.Queue(32), token=daemon.token); client.multiplex = client.reliable = True
            daemon.hub.set_subscriptions(client, [s.id for s in sessions])
            sent = []
            class Socket:
                async def send(self, frame): sent.append(frame); await asyncio.sleep(0)
                async def close(self, **_): pass
            sender = asyncio.create_task(daemon._send(Socket(), client.queue, client))
            extra_clients = []
            try:
                async with asyncio.timeout(2):
                    while calls < 4 or sum(unpack(f)[0] == OP_SNAPSHOT for f in sent) < count or len(daemon._footer_pending) + len(daemon._footer_tasks) < count:
                        await asyncio.sleep(.005)
                assert calls == maximum == 4
                assert len(daemon._footer_pending) == count - 4
                assert all(len(stamp) <= 32 for _, stamp, _ in daemon._footer_pending.values())
                pending_client, stamp, emit = next(iter(daemon._footer_pending.values()))
                for _ in range(100): daemon.schedule_snapshot_supplement(pending_client, stamp, emit)
                assert len(daemon._footer_pending) == count - 4
                if count == 40:
                    for _ in range(6):
                        extra = daemon.hub.attach(asyncio.Queue(32), token=daemon.token); extra.multiplex = True
                        extra_clients.append(extra)
                        daemon.hub.set_subscriptions(extra, [s.id for s in sessions])
                        while not extra.queue.empty(): extra.queue.get_nowait(); daemon.hub.pump_replays(extra)
                        for session in sessions:
                            daemon.schedule_snapshot_supplement(extra, pack_feed(OP_SNAPSHOT, session.id, b'raw'), emit)
                    assert len(daemon._footer_pending) == 256
                    for extra in extra_clients: daemon.forget_relay_client(extra)
                    assert len(daemon._footer_pending) == count - 4 and calls == 4
                release.set()
                async with asyncio.timeout(2):
                    while daemon._footer_tasks or daemon._footer_jobs: await asyncio.sleep(.005)
                recovered = {unpack(f)[1].partition(b'\0')[0] for f in sent if b'QUIET-FOOTER' in f}
                assert recovered == {s.id.encode() for s in sessions}
                assert calls == count and maximum <= 4 and not client.failure
            finally:
                release.set(); sender.cancel(); await asyncio.gather(sender, return_exceptions=True)
                for extra in extra_clients: daemon.forget_relay_client(extra)
                daemon.hub.detach(client.key)
    asyncio.run(check())


def test_direct_read_slots_wake_pending_recovery_without_losing_requests():
    async def check():
        release = threading.Event()
        calls, active, maximum = 0, 0, 0
        lock = threading.Lock()
        async with isolated_host() as (daemon, _, _):
            sessions = [daemon.hub.open_cli(daemon.hub.open_window(owner=daemon.token).id, 'cmd') for _ in range(9)]
            def footer(*_):
                nonlocal calls, active, maximum
                with lock: calls += 1; active += 1; maximum = max(maximum, active)
                try: release.wait(5); return b'DIRECT-SLOT-FOOTER'
                finally:
                    with lock: active -= 1
            for session in sessions: session.pty.footer_patch = footer
            client = daemon.hub.attach(asyncio.Queue(32), token=daemon.token); client.multiplex = True
            daemon.hub.set_subscriptions(client, [s.id for s in sessions])
            while not client.queue.empty(): client.queue.get_nowait(); daemon.hub.pump_replays(client)
            jobs = [asyncio.create_task(daemon.snapshot_supplement(client, pack_feed(OP_SNAPSHOT, s.id, b'raw'))) for s in sessions[:3]]
            recovered = []
            async def emit(frame, current):
                if current(): recovered.append(frame)
            try:
                async with asyncio.timeout(2):
                    while calls != 3: await asyncio.sleep(.005)
                for session in sessions[3:]:
                    daemon.schedule_snapshot_supplement(client, pack_feed(OP_SNAPSHOT, session.id, b'h'*100000), emit)
                async with asyncio.timeout(2):
                    while calls != 4: await asyncio.sleep(.005)
                assert len(daemon._footer_pending) == 5
                release.set()
                await asyncio.gather(*jobs)
                async with asyncio.timeout(2):
                    while daemon._footer_tasks or daemon._footer_jobs or daemon._footer_pending: await asyncio.sleep(.005)
                assert len(recovered) == 6 and calls == 9 and maximum == 4
            finally:
                release.set(); await asyncio.gather(*jobs, return_exceptions=True)
                daemon.hub.detach(client.key)
    asyncio.run(check())


def test_sidecar_close_prevents_late_footer_even_with_live_client_and_session():
    async def check():
        entered, release = threading.Event(), threading.Event()
        async with isolated_host() as (daemon, _, _):
            session = daemon.hub.open_cli(daemon.hub.open_window(owner=daemon.token).id, 'cmd')
            def footer(*_): entered.set(); release.wait(5); return b'LATE-AFTER-CLOSE'
            session.pty.footer_patch = footer
            client = daemon.hub.attach(asyncio.Queue(32), token=daemon.token); client.multiplex = True
            daemon.hub.set_subscriptions(client, [session.id])
            emitted = []
            async def emit(frame, current):
                if current(): emitted.append(frame)
            daemon.schedule_snapshot_supplement(client, pack_feed(OP_SNAPSHOT, session.id, b'raw'), emit)
            try:
                async with asyncio.timeout(2):
                    while not entered.is_set(): await asyncio.sleep(.005)
                await daemon.close_sidecars()
                assert daemon.client_authorized(client) and daemon.hub.sessions.get(session.id) is session
                release.set()
                async with asyncio.timeout(2):
                    while daemon._footer_tasks or daemon._footer_jobs: await asyncio.sleep(.005)
                assert not emitted and not daemon._footer_pending
            finally:
                release.set(); daemon.hub.detach(client.key)
    asyncio.run(check())
