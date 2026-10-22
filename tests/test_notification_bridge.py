import asyncio
import json
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from supermonoagent.notification_bridge import NotificationBridge, socket_url
from supermonoagent.protocol import (OP_HELLO, OP_HELLO_OK, OP_INPUT, OP_OUTPUT,
    OP_SESSIONS, OP_SNAPSHOT, OP_SUBSCRIBE, OP_TASK_EVENT, OP_TASK_SUBSCRIBE,
    pack, pack_feed, pack_json, unpack, unpack_json)
from supermonoagent.relay import Relay
from supermonoagent.task_events import TaskEventStore


RUNNING = b"\x1b]7501;app=codex;state=working\x07"
IDLE = b"\x1b]7501;app=codex;state=idle\x07"
CATALOG = {"machine": "desktop", "project": "default", "groups": [],
           "windows": [{"id": "window01", "title": "Work", "sessions": [
               {"id": "session1", "shell": "codex"}]}]}


class BridgeUnitTest(unittest.TestCase):
    def test_authenticated_transport_probe_is_labelled_and_does_not_touch_pty(self):
        bridge = NotificationBridge('http://localhost:6769', 'owner', TaskEventStore())
        queue = asyncio.Queue(32)
        bridge.request(pack_json(OP_TASK_SUBSCRIBE, {'replay':False}), queue)
        reply = bridge.request(pack_json(20, {'probe':'abcdef123456'}), asyncio.Queue())
        event = unpack_json(unpack(reply[0])[1])
        self.assertIn('abcdef123456', event['title'])
        self.assertIn('测试', event['summary'])
        self.assertEqual(event['session'], '')
        self.assertEqual(unpack_json(unpack(queue.get_nowait())[1]), event)
        with self.assertRaises(ValueError):
            bridge.request(pack_json(20, {'probe':'abcdef123456'}), queue)

    def test_backend_must_be_loopback_without_url_credentials(self):
        self.assertEqual(socket_url("http://127.0.0.1:6769", local=True), "ws://127.0.0.1:6769/ws")
        for url in ("ws://public.example/ws", "ws://x@y/ws", "ws://localhost/ws?token=secret"):
            with self.assertRaises(ValueError):
                socket_url(url, local=True)

    def test_snapshots_gaps_closed_sessions_and_restart_do_not_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.json"
            bridge = NotificationBridge("http://localhost:6769", "owner", TaskEventStore(path))
            bridge.receive(pack_json(OP_HELLO_OK, CATALOG))
            bridge.receive(pack_feed(OP_SNAPSHOT, "session1", RUNNING + IDLE))
            bridge.receive(pack_feed(OP_OUTPUT, "session1", IDLE))
            self.assertEqual(bridge.store.replay("owner"), [])
            bridge.receive(pack_feed(OP_OUTPUT, "session1", RUNNING))
            bridge.receive(pack_feed(OP_SNAPSHOT, "session1", b"gap"))
            bridge.receive(pack_feed(OP_OUTPUT, "session1", IDLE))
            self.assertEqual(bridge.store.replay("owner"), [])
            bridge.receive(pack_feed(OP_OUTPUT, "session1", RUNNING + IDLE))
            event = bridge.store.replay("owner")[0]
            self.assertEqual((event["title"], event["agent"]), ("Work", "codex"))
            bridge.receive(pack_json(OP_SESSIONS, {"windows": []}))
            bridge.receive(pack_feed(OP_OUTPUT, "session1", RUNNING + IDLE))
            self.assertEqual(len(bridge.store.replay("owner")), 1)
            self.assertEqual(TaskEventStore(path).replay("owner"), [event])
            self.assertEqual(TaskEventStore(path).replay("other"), [])
            self.assertNotIn(': "owner"', path.read_text(encoding="utf8"))


class BridgeIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_health_reports_only_connection_status(self):
        bridge = NotificationBridge("http://localhost:6769", "private-owner", TaskEventStore())
        bridge.connected = True
        async with serve(bridge.handle_socket, "127.0.0.1", 0,
                         process_request=bridge.serve_http) as server:
            url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/health"

            def fetch():
                with urllib.request.urlopen(url, timeout=2) as response:
                    return response.read().decode("utf8")

            response = await asyncio.to_thread(fetch)
            self.assertNotIn("private-owner", response)
            self.assertEqual(json.loads(response), {"ok": True, "role": "notification-bridge",
                            "backend_connected": True, "relay_connected": False, "subscribers": 0})

    async def test_old_daemon_to_existing_relay_notifications_without_shells(self):
        output = asyncio.Queue()
        subscribed = asyncio.Event()
        observed_ops = []

        async def old_daemon(ws):
            if ws.request.headers.get("Authorization") != "Bearer owner":
                await ws.close(code=4001, reason="Header authentication required")
                return
            op, body = unpack(await ws.recv())
            observed_ops.append(op)
            self.assertEqual(unpack_json(body)["token"], "owner")
            await ws.send(pack_json(OP_HELLO_OK, CATALOG))
            op, body = unpack(await ws.recv())
            observed_ops.append(op)
            self.assertEqual(unpack_json(body), {"sessions": ["session1"]})
            await ws.send(pack_feed(OP_SNAPSHOT, "session1", RUNNING + IDLE))
            subscribed.set()
            try:
                while True:
                    await ws.send(await output.get())
            except ConnectionClosed:
                return

        relay = Relay(development=True)
        with patch("supermonoagent.pty.WinPty") as pty:
            async with serve(old_daemon, "127.0.0.1", 0) as old, serve(relay.handler, "127.0.0.1", 0) as server:
                backend = f"ws://127.0.0.1:{old.sockets[0].getsockname()[1]}"
                endpoint = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                bridge = NotificationBridge(backend, "owner", TaskEventStore())
                observer = asyncio.create_task(bridge.observe())
                uplink = asyncio.create_task(bridge.relay(endpoint, "original-events"))
                try:
                    await asyncio.wait_for(subscribed.wait(), 3)
                    async with asyncio.timeout(3):
                        while not bridge.relay_connected:
                            await asyncio.sleep(.01)
                    async with connect(endpoint) as phone:
                        await phone.send(json.dumps({"op": "phone", "code": "original-events", "token": "owner"}))
                        self.assertEqual(json.loads(await phone.recv())["op"], "ready")
                        await phone.send(pack_json(OP_HELLO, {"token": "owner"}))
                        self.assertEqual(unpack(await phone.recv())[0], OP_HELLO_OK)
                        await phone.send(pack_json(OP_TASK_SUBSCRIBE, {}))
                        await output.put(pack_feed(OP_OUTPUT, "session1", IDLE))
                        with self.assertRaises(asyncio.TimeoutError):
                            await asyncio.wait_for(phone.recv(), .1)
                        await output.put(pack_feed(OP_OUTPUT, "session1", RUNNING[:8]))
                        await output.put(pack_feed(OP_OUTPUT, "session1", RUNNING[8:] + IDLE))
                        op, body = unpack(await asyncio.wait_for(phone.recv(), 3))
                        self.assertEqual(op, OP_TASK_EVENT)
                        event = unpack_json(body)
                        self.assertEqual(event["session"], "session1")
                        # Replay uses the same durable ID; cursor suppresses it.
                        await phone.send(pack_json(OP_TASK_SUBSCRIBE, {}))
                        self.assertEqual(unpack_json(unpack(await phone.recv())[1])["event_id"], event["event_id"])
                        await phone.send(pack_json(OP_TASK_SUBSCRIBE, {"after": event["event_id"]}))
                        with self.assertRaises(asyncio.TimeoutError):
                            await asyncio.wait_for(phone.recv(), .1)
                        # Terminal mutation is rejected by the companion.
                        await phone.send(pack(OP_INPUT, b"danger\r"))
                        with self.assertRaises(ConnectionClosed):
                            await asyncio.wait_for(phone.recv(), 3)
                    async with connect(endpoint) as intruder:
                        await intruder.send(json.dumps({"op": "phone", "code": "original-events", "token": "other"}))
                        with self.assertRaises(ConnectionClosed):
                            await asyncio.wait_for(intruder.recv(), 3)
                finally:
                    observer.cancel()
                    uplink.cancel()
                    await asyncio.gather(observer, uplink, return_exceptions=True)
                    # Unblock fixture shutdown without waiting for a new output.
                    for connection in list(old.connections):
                        await connection.close()
                    await output.put(b"")
                self.assertEqual(observed_ops, [OP_HELLO, OP_SUBSCRIBE])
                pty.assert_not_called()

    async def test_direct_endpoint_requires_token_and_rejects_mismatch(self):
        bridge = NotificationBridge("http://localhost:6769", "owner", TaskEventStore())
        async with serve(bridge.handle_socket, "127.0.0.1", 0) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            async with connect(url) as client:
                await client.send(pack_json(OP_HELLO, {"token": "other"}))
                with self.assertRaises(ConnectionClosed):
                    await client.recv()

            async with connect(url) as client:
                await client.send(pack_json(OP_HELLO, {"token": "owner"}))
                self.assertEqual(unpack(await client.recv())[0], OP_HELLO_OK)
                await client.send(pack_json(OP_HELLO, {"token": "other"}))
                with self.assertRaises(ConnectionClosed):
                    await client.recv()

    async def test_reconnect_disarms_old_run_and_baseline_skips_prior_events(self):
        connections = 0
        second_ready = asyncio.Event()
        release = asyncio.Event()

        async def backend(ws):
            nonlocal connections
            connections += 1
            await ws.recv()
            await ws.send(pack_json(OP_HELLO_OK, CATALOG))
            await ws.recv()
            if connections == 1:
                await ws.send(pack_feed(OP_OUTPUT, "session1", RUNNING))
                await ws.close()
                return
            await ws.send(pack_feed(OP_SNAPSHOT, "session1", RUNNING + IDLE))
            await ws.send(pack_feed(OP_OUTPUT, "session1", IDLE))
            second_ready.set()
            await release.wait()
            await ws.wait_closed()

        async with serve(backend, "127.0.0.1", 0) as server:
            bridge = NotificationBridge(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                                        "owner", TaskEventStore())
            job = asyncio.create_task(bridge.observe())
            try:
                await asyncio.wait_for(second_ready.wait(), 4)
                await asyncio.sleep(.02)
                self.assertEqual(bridge.store.replay("owner"), [])
                bridge.receive(pack_feed(OP_OUTPUT, "session1", RUNNING + IDLE))
                prior = bridge.store.replay("owner")[0]
                queue = asyncio.Queue(32)
                replies = bridge.request(pack_json(OP_TASK_SUBSCRIBE, {"replay": False}), queue)
                self.assertEqual(unpack_json(unpack(replies[0])[1]),
                                 {"type": "cursor", "cursor": prior["event_id"]})
                self.assertTrue(queue.empty())
                bridge.receive(pack_feed(OP_OUTPUT, "session1", RUNNING + IDLE))
                received = unpack_json(unpack(queue.get_nowait())[1])
                self.assertNotEqual(received["event_id"], prior["event_id"])
            finally:
                release.set()
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
