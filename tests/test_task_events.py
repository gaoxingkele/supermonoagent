import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from supermonoagent.hub import Hub, Session
from supermonoagent.protocol import OP_TASK_EVENT, OP_TASK_SUBSCRIBE, unpack, unpack_json
from supermonoagent.task_events import TaskEventStore, TaskStateDetector


def marker(state, ending=b"\x1b\\"):
    return b"\x1b]7501;state=" + state.encode() + b":app=codex" + ending


class DetectorTest(unittest.TestCase):
    def test_idle_startup_clear_and_output_are_not_completion(self):
        detector = TaskStateDetector()
        self.assertEqual(detector.feed(marker("idle") + b"Task completed text"), 0)
        self.assertEqual(detector.feed(marker("working") + marker("clear") + marker("idle")), 0)
        self.assertEqual(detector.feed(marker("idle")), 0)

    def test_fragmentation_and_duplicate_idle(self):
        detector = TaskStateDetector()
        raw = marker("working") + b"output" + marker("idle", b"\x07")
        self.assertEqual(sum(detector.feed(bytes((char,))) for char in raw), 1)
        self.assertEqual(detector.app, "codex")
        self.assertEqual(detector.feed(marker("idle")), 0)

    def test_multiple_turns_and_unsupported_sequences(self):
        detector = TaskStateDetector()
        self.assertEqual(detector.feed(marker("running") + marker("idle") + marker("busy") + marker("idle")), 2)
        self.assertEqual(detector.feed(b"\x1b]999;state=working\x07" + marker("idle")), 0)

    def test_unterminated_osc_is_bounded(self):
        detector = TaskStateDetector()
        detector.feed(b"\x1b]7501;" + b"a" * 100000)
        self.assertLessEqual(len(detector.pending), 512)
        self.assertEqual(detector.feed(marker("working") + marker("idle")), 1)


class StoreTest(unittest.TestCase):
    def test_durable_replay_cursor_owner_isolation_and_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.json"
            store = TaskEventStore(path, limit=4)
            first = store.append("secret-owner-a", {"title": "a", "token": "never-persist"})
            store.append("owner-b", {"title": "b"})
            third = store.append("secret-owner-a", {"title": "c"})
            loaded = TaskEventStore(path)
            self.assertEqual(loaded.replay("secret-owner-a", first["event_id"]), [third])
            self.assertEqual(loaded.replay("owner-b")[0]["title"], "b")
            self.assertEqual(loaded.replay("secret-owner-a", third["event_id"]), [])
            self.assertNotIn("secret-owner-a", path.read_text(encoding="utf8"))
            self.assertNotIn("never-persist", path.read_text(encoding="utf8"))
            for _ in range(5):
                store.append("secret-owner-a", {})
            self.assertEqual(len(store.records), 4)

    def test_channel_isolation_and_bounded_unknown_cursor_replay(self):
        store = TaskEventStore()
        for index in range(40):
            store.append("a", {"title": str(index)}, "one")
        store.append("a", {"title": "other room"}, "two")
        self.assertEqual(len(store.replay("a", "expired", "one")), 24)
        self.assertEqual(len(store.replay("a", channel="two")), 1)

    def test_storage_failure_preserves_live_memory_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            blocked = Path(directory) / "not-a-directory"
            blocked.write_text("file", encoding="utf8")
            store = TaskEventStore(blocked / "events.json")
            with self.assertLogs("supermonoagent.task_events", level="WARNING"):
                event = store.append("owner", {"title": "still delivered"})
            self.assertEqual(store.replay("owner"), [event])


class HubTaskTest(unittest.TestCase):
    def setUp(self):
        self.hub = Hub()
        self.window = self.hub.open_window("Task", owner="owner")
        self.session = Session("12345678", self.window.id, "codex", ".", object())
        self.hub.sessions[self.session.id] = self.session
        self.window.sessions[self.session.id] = self.session
        self.owner = self.hub.attach(asyncio.Queue(), token="owner")
        self.other = self.hub.attach(asyncio.Queue(), token="other")

    def drain(self, client):
        result = []
        while not client.queue.empty():
            frame = client.queue.get_nowait()
            if frame and unpack(frame)[0] == OP_TASK_EVENT:
                result.append(unpack_json(unpack(frame)[1]))
        return result

    def test_no_terminal_watchers_receive_completion_and_replay_deduplicates(self):
        self.hub.subscribe_task_events(self.owner)
        self.hub.subscribe_task_events(self.other)
        self.hub._task_output(self.session.id, marker("idle"))
        self.assertEqual(self.drain(self.owner), [])
        self.hub._task_output(self.session.id, marker("working") + marker("idle"))
        events = self.drain(self.owner)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["agent"], "codex")
        self.assertEqual(events[0]["status"], "completed")
        self.assertEqual(self.drain(self.other), [])
        self.assertEqual(self.hub.subscribe_task_events(self.owner, events[0]["event_id"]), [])
        self.assertEqual(len(self.hub.subscribe_task_events(self.owner)), 1)

    def test_revoked_and_different_channel_clients_receive_no_event(self):
        self.hub.subscribe_task_events(self.owner)
        self.owner.authorize = lambda: False
        self.hub._task_output(self.session.id, marker("working") + marker("idle"))
        self.assertEqual(self.drain(self.owner), [])
        isolated = self.hub.attach(asyncio.Queue(), key="relay-isolated", token="owner")
        isolated.channel = "other-room"
        self.assertEqual(self.hub.subscribe_task_events(isolated), [])
        self.hub._task_output(self.session.id, marker("working") + marker("idle"))
        self.assertEqual(self.drain(isolated), [])

    def test_snapshot_is_never_a_completion(self):
        self.hub.subscribe_task_events(self.owner)
        self.hub._flushed(self.session.id, marker("working") + marker("idle"), True)
        self.assertEqual(self.drain(self.owner), [])

    def test_fresh_enable_establishes_cursor_without_old_notifications(self):
        self.hub._task_output(self.session.id, marker("working") + marker("idle"))
        frames = self.hub.subscribe_task_events(self.owner, replay=False)
        cursor = unpack_json(unpack(frames[0])[1])
        self.assertEqual(cursor["type"], "cursor")
        self.assertTrue(cursor["cursor"])
        self.assertEqual(self.hub.subscribe_task_events(self.owner, cursor["cursor"]), [])
        self.hub._task_output(self.session.id, marker("working") + marker("idle"))
        self.assertEqual(len(self.drain(self.owner)), 1)

    def test_raw_pty_chunks_detect_completion_without_altering_bytes(self):
        from supermonoagent.pty import PtyClosed
        chunks = [marker("working")[:5], marker("working")[5:], b"original output", marker("idle")]

        class Pty:
            def __init__(self):
                self.items = iter(chunks)

            def read(self, _):
                try:
                    return next(self.items)
                except StopIteration:
                    raise PtyClosed()

        class Loop:
            def __init__(self):
                self.callbacks = []

            def call_soon_threadsafe(self, callback, *args):
                self.callbacks.append((callback, args))

        loop = Loop()
        self.hub.loop = loop
        self.session.pty = Pty()
        self.hub.subscribe_task_events(self.owner)
        self.hub._read(self.session)
        for callback, args in loop.callbacks:
            if callback == self.hub._task_output:
                callback(*args)
        self.assertEqual(self.session.pump.snapshot(), b"".join(chunks))
        self.assertEqual(len(self.drain(self.owner)), 1)


class DaemonTaskTest(unittest.IsolatedAsyncioTestCase):
    async def test_subscribe_opcode_is_owner_scoped_and_requires_authorization(self):
        from supermonoagent import daemon
        with tempfile.TemporaryDirectory() as directory, patch.object(daemon, "STATE", Path(directory)):
            service = daemon.Daemon("owner")
            service.hub.task_events.append("owner", {"title": "mine"})
            service.hub.task_events.append("other", {"title": "private"})
            client = service.hub.attach(asyncio.Queue(), token="owner")
            frames, close = await service._op(client, OP_TASK_SUBSCRIBE, b"{}")
            self.assertIsNone(close)
            self.assertEqual([unpack_json(unpack(frame)[1])["title"] for frame in frames], ["mine"])
            baseline, _ = await service._op(client, OP_TASK_SUBSCRIBE, b'{"replay":false}')
            self.assertEqual(unpack_json(unpack(baseline[0])[1])["type"], "cursor")
            client.token = "revoked"
            self.assertEqual(await service._op(client, OP_TASK_SUBSCRIBE, b"{}"), ([], 4001))


if __name__ == "__main__":
    unittest.main()
