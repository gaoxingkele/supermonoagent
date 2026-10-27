"""Replay bursts yield to bounded transport queues without losing live bytes."""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from supermonoagent.hub import Hub
from supermonoagent.protocol import OP_OUTPUT, OP_SESSIONS, OP_SNAPSHOT, pack, unpack
from test_windows import FakePty


class ReplayBackpressureTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"SUPERMONO_STATE_DIR": self.directory.name})
        self.environment.start()
        self.hub = Hub(pty_factory=lambda *_: FakePty())
        self.hub.attach_loop(asyncio.get_running_loop())

    async def asyncTearDown(self):
        for client in list(self.hub.clients):
            self.hub.detach(client)
        for window in list(self.hub.windows):
            self.hub.close_window(window)
        self.environment.stop()
        self.directory.cleanup()

    def prepare(self, count, capacity):
        sessions = []
        for index in range(count):
            window = self.hub.open_window(f"session-{index}", owner="owner")
            session = self.hub.open_cli(window.id, "cmd")
            session.pump.remember(f"history-{index}".encode())
            sessions.append(session)
        client = self.hub.attach(asyncio.Queue(capacity), token="owner")
        client.multiplex = client.reliable = True
        return sessions, client

    async def drain_replay(self, client):
        frames = []
        for _ in range(300):
            while not client.queue.empty():
                frame = client.queue.get_nowait()
                self.assertIsNotNone(frame, client.failure)
                frames.append(unpack(frame))
            if not client.pending_replays:
                return frames
            await asyncio.sleep(.006)
        self.fail("Replay did not drain")

    async def test_six_tab_replay_does_not_fail_a_small_transport_queue(self):
        sessions, client = self.prepare(6, 4)
        self.hub.set_subscriptions(client, [session.id for session in sessions])
        self.assertFalse(client.failure)
        self.assertLessEqual(client.queue.qsize(), 2)
        frames = await self.drain_replay(client)
        self.assertCountEqual(frames, [(OP_SNAPSHOT, session.id.encode() + b"\0" + session.pump.snapshot())
                                       for session in sessions])

    async def test_more_than_default_queue_frames_are_lazy_and_deduplicated(self):
        sessions, client = self.prepare(40, 32)
        ids = [session.id for session in sessions]
        self.hub.set_subscriptions(client, ids)
        for _ in range(100):
            self.hub.refresh_subscriptions(client, ids)
        self.assertFalse(client.failure)
        self.assertLessEqual(client.queue.qsize(), 16)
        self.assertLessEqual(len(client.pending_replays), len(sessions))
        frames = await self.drain_replay(client)
        self.assertEqual({payload.partition(b"\0")[0] for op, payload in frames if op == OP_SNAPSHOT},
                         {session.id.encode() for session in sessions})

    async def test_six_tabs_repeated_refresh_leaves_capacity_for_subscription_replies(self):
        sessions, client = self.prepare(6, 32)
        ids = [session.id for session in sessions]
        self.hub.set_subscriptions(client, ids)
        for _ in sessions:
            self.hub.refresh_subscriptions(client, ids)
            self.hub._send(client, pack(OP_SESSIONS, b"{}"))
        self.assertFalse(client.failure)
        self.assertLessEqual(client.queue.qsize(), 22)
        frames = await self.drain_replay(client)
        self.assertEqual(sum(op == OP_SESSIONS for op, _payload in frames), 6)

    async def test_deferred_snapshot_captures_new_bytes_then_live_bytes_are_exact(self):
        sessions, client = self.prepare(6, 4)
        self.hub.set_subscriptions(client, [session.id for session in sessions])
        deferred = self.hub.sessions[next(iter(client.pending_replays))]
        pending = b"-during-replay\xff\x1b[31m"
        deferred.pump.remember(pending)
        deferred.pump.flushed_position = deferred.pump.snapshot_position()[1]
        self.hub._flushed(deferred.id, pending, False)
        self.assertFalse(client.failure)
        frames = await self.drain_replay(client)
        expected = deferred.id.encode() + b"\0" + deferred.pump.snapshot()
        self.assertIn((OP_SNAPSHOT, expected), frames)
        self.assertNotIn((OP_OUTPUT, deferred.id.encode() + b"\0" + pending), frames)
        live = b"\x1b[0mraw\xff\r\n"
        deferred.pump.remember(live)
        deferred.pump.flushed_position += len(live)
        self.hub._flushed(deferred.id, live, False)
        self.assertEqual(unpack(client.queue.get_nowait()), (OP_OUTPUT, deferred.id.encode() + b"\0" + live))

    async def test_unsubscribe_and_detach_cancel_pending_replay(self):
        sessions, client = self.prepare(6, 4)
        self.hub.set_subscriptions(client, [session.id for session in sessions])
        self.hub.set_subscriptions(client, [])
        self.assertFalse(client.pending_replays)
        self.hub.detach(client.key)
        self.assertIsNone(client.replay_handle)

    async def test_four_busy_sessions_with_live_traffic_survive_slow_replay_drain(self):
        from supermonoagent.daemon import QUEUE_FRAMES

        sessions, client = self.prepare(4, QUEUE_FRAMES)
        ids = [session.id for session in sessions]
        # Simulate the production storm: every frontend resubscribe replays all
        # four ~256KB snapshots while the busy terminals keep streaming output.
        for _ in range(3):
            client.subscriptions.clear()
            client.pending_replays.clear()
            self.hub.set_subscriptions(client, ids)
            for index, session in enumerate(sessions):
                session.pump.remember(f"live-{index}".encode() * 64)
                session.pump.flushed_position = session.pump.snapshot_position()[1]
                self.hub._flushed(session.id, f"live-{index}".encode() * 64, False)
            self.assertFalse(client.failure)
            while client.pending_replays:
                self.assertLess(client.queue.qsize(), client.queue.maxsize)
                await asyncio.sleep(.006)
            # A consumer draining slower than the live flush rate must still
            # not trip the reliable-client fail path.
            for _ in range(client.queue.maxsize // 2):
                self.hub._send(client, pack(OP_OUTPUT, b"x" * 64))
                self.assertFalse(client.failure)
                client.queue.get_nowait()

    async def test_paced_client_gets_snapshot_resync_without_failure(self):
        sessions, client = self.prepare(1, 8)
        sid = sessions[0].id
        self.hub.set_subscriptions(client, [sid])
        # Fill the queue to the low-water mark without draining (1 replay
        # snapshot + 4 filler frames).
        for _ in range(4):
            self.hub._send(client, pack(OP_OUTPUT, b"filler"))
        self.assertEqual(client.queue.qsize(), 5)
        # A live flush at the low-water mark is skipped and marked for resync.
        sessions[0].pump.remember(b"paced-bytes\r\n")
        sessions[0].pump.flushed_position = sessions[0].pump.snapshot_position()[1]
        self.hub._flushed(sid, b"paced-bytes\r\n", False)
        self.assertFalse(client.failure)
        self.assertIn(sid, client.resync_pending)
        self.assertEqual(client.queue.qsize(), 5)  # nothing new queued yet
        # Drain below the low-water mark; a snapshot realigns the skipped bytes.
        for _ in range(3):
            client.queue.get_nowait()
        for _ in range(300):
            if not client.resync_pending:
                break
            await asyncio.sleep(.006)
        self.assertFalse(client.resync_pending)
        self.assertFalse(client.failure)
        frames = []
        while not client.queue.empty():
            frames.append(unpack(client.queue.get_nowait()))
        snapshots = [payload.partition(b"\0")[0] for op, payload in frames if op == OP_SNAPSHOT]
        self.assertIn(sid.encode(), snapshots)

    async def test_paced_resync_keeps_four_busy_sessions_alive(self):
        from supermonoagent.daemon import QUEUE_FRAMES

        sessions, client = self.prepare(4, QUEUE_FRAMES)
        ids = [session.id for session in sessions]
        self.hub.set_subscriptions(client, ids)
        for _ in range(300):
            if not client.pending_replays:
                break
            await asyncio.sleep(.006)
        self.assertFalse(client.failure)
        # Sustained live traffic with a consumer draining slower than the flush
        # rate: pacing converts streams to snapshots instead of 1013 fails.
        burst = b"burst-output\r\n" * 8
        for _ in range(40):
            for session in sessions:
                session.pump.remember(burst)
                session.pump.flushed_position = session.pump.snapshot_position()[1]
                self.hub._flushed(session.id, burst, False)
            self.assertFalse(client.failure)
            while client.queue.qsize() > 4:
                client.queue.get_nowait()
        for _ in range(300):
            if not client.resync_pending and not client.pending_replays:
                break
            await asyncio.sleep(.006)
        while not client.queue.empty():
            frame = client.queue.get_nowait()
            self.assertIsNotNone(frame, client.failure)
        self.assertFalse(client.failure)

    async def test_remote_pause_defers_live_output_and_resume_refreshes_once(self):
        sessions, client = self.prepare(2, 8)
        ids = [session.id for session in sessions]
        self.hub.set_subscriptions(client, ids)
        await self.drain_replay(client)
        self.hub.set_remote_paused(client, True)
        before = client.queue.qsize()
        for session in sessions:
            session.pump.remember(b'while-paused')
            session.pump.flushed_position = session.pump.snapshot_position()[1]
            self.hub._flushed(session.id, b'while-paused', False)
        self.assertEqual(client.queue.qsize(), before)
        self.assertEqual(client.resync_pending, set(ids))
        self.hub.set_remote_paused(client, False, True)
        frames = await self.drain_replay(client)
        for _ in range(100):
            while not client.queue.empty():
                frames.append(unpack(client.queue.get_nowait()))
            if not client.resync_pending:
                break
            await asyncio.sleep(.006)
        self.assertFalse(client.failure)
        self.assertFalse(client.remote_paused)
        self.assertFalse(client.resync_pending)
        self.assertEqual({payload.partition(b'\0')[0] for op, payload in frames if op == OP_SNAPSHOT},
                         {sid.encode() for sid in ids})

    async def test_genuinely_slow_live_client_still_fails_with_bounded_queue(self):
        _sessions, client = self.prepare(1, 4)
        for _ in range(5):
            self.hub._send(client, pack(OP_OUTPUT, b"live"))
        self.assertIn("Slow client", client.failure)
        self.assertNotIn(client.key, self.hub.clients)
        self.assertIsNone(client.queue.get_nowait())
