import asyncio
import json
import unittest
from unittest.mock import patch

from supermonoagent.relay import FLOW_LOW_WATER, QUEUE_FRAMES, Relay, Room
from supermonoagent.protocol import OP_SNAPSHOT, unpack
from test_client_integration import barrier, isolated_host


class Socket:
    def __init__(self):
        self.sent = []
        self.closed = []

    async def send(self, value):
        self.sent.append(value)

    async def close(self, **kwargs):
        self.closed.append(kwargs)


class RelayFlowControlTest(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_relay_queue_pauses_and_requests_latest_snapshot(self):
        relay = Relay()
        host, phone = Socket(), Socket()
        room = Room(code='room', host=host, flow_control=True)
        room.phones[1] = phone
        room.queues[1] = asyncio.Queue(QUEUE_FRAMES)
        room.reliable.add(1)
        for index in range(QUEUE_FRAMES * 3):
            relay._enqueue(room, 1, bytes([index % 256]))
        await asyncio.sleep(0)
        self.assertEqual(room.queues[1].qsize(), QUEUE_FRAMES)
        self.assertEqual(room.paused, {1})
        self.assertEqual(room.resync, {1})
        self.assertEqual(phone.closed, [])
        self.assertEqual(json.loads(host.sent[-1]), {'op': 'pace', 'id': 1, 'paused': True, 'resync': False})
        while room.queues[1].qsize() > FLOW_LOW_WATER:
            room.queues[1].get_nowait()
            relay._phone_sent(room, 1)
        await asyncio.sleep(0)
        self.assertEqual(json.loads(host.sent[-1]), {'op': 'pace', 'id': 1, 'paused': False, 'resync': True})
        self.assertFalse(room.paused)
        self.assertFalse(room.resync)

    async def test_legacy_reliable_socket_keeps_explicit_overflow_disconnect(self):
        relay = Relay()
        room = Room(code='room')
        phone = Socket()
        room.phones[1] = phone
        room.queues[1] = asyncio.Queue(1)
        room.reliable.add(1)
        relay._enqueue(room, 1, b'first')
        relay._enqueue(room, 1, b'second')
        await asyncio.sleep(0)
        self.assertEqual(phone.closed[0]['code'], 1013)
        self.assertEqual(room.queues[1].get_nowait(), b'first')

    async def test_one_slow_phone_does_not_pause_another_phone(self):
        relay = Relay()
        host = Socket()
        room = Room(code='room', host=host, flow_control=True)
        for phone_id in (1, 2):
            room.phones[phone_id] = Socket()
            room.queues[phone_id] = asyncio.Queue(QUEUE_FRAMES)
            room.reliable.add(phone_id)
        for index in range(QUEUE_FRAMES * 2):
            relay._enqueue(room, 1, b'slow')
        relay._enqueue(room, 2, b'current')
        await asyncio.sleep(0)
        self.assertEqual(room.paused, {1})
        self.assertEqual(room.queues[2].get_nowait(), b'current')
        self.assertEqual(room.phones[2].closed, [])
        self.assertFalse(room.phones[1].closed)

    async def test_new_daemon_keeps_legacy_snapshot_path_on_old_relay(self):
        from supermonoagent.daemon import relay_uplink
        from supermonoagent.protocol import OP_OUTPUT
        from test_client_integration import receive_op
        from test_relay_token_discovery_compat import ROOT, load
        from test_security_directory import running

        legacy = load('supermonoagent.legacy_without_flow', ROOT / 'tests/fixtures/legacy-relay.py')
        service = legacy.Relay()
        async with isolated_host() as (daemon, _direct, _relay_url), running(service) as legacy_url:
            job = asyncio.create_task(relay_uplink(daemon, legacy_url, 'local-test', 'legacy'))
            try:
                async with asyncio.timeout(5):
                    while legacy_url not in daemon.relay_live:
                        await asyncio.sleep(.01)
                window = daemon.hub.open_window(owner=daemon.token)
                session = daemon.hub.open_cli(window.id, 'cmd')
                session.pump.remember(b'legacy-snapshot')
                from websockets.asyncio.client import connect
                from supermonoagent.protocol import OP_HELLO, OP_HELLO_OK, OP_SUBSCRIBE, pack_json
                async with connect(legacy_url) as phone:
                    await phone.send(json.dumps({'op': 'phone', 'code': 'local-test', 'token': daemon.token, 'reliable': True}))
                    await phone.recv()
                    await phone.send(pack_json(OP_HELLO, {'token': daemon.token, 'multiplex': True, 'reliable': True}))
                    await receive_op(phone, OP_HELLO_OK)
                    await phone.send(pack_json(OP_SUBSCRIBE, {'sessions': [session.id]}))
                    snapshot = await receive_op(phone, OP_SNAPSHOT)
                    self.assertEqual(snapshot, session.id.encode() + b'\0legacy-snapshot')
                    session.pty.inbox.put(b'legacy-live')
                    self.assertEqual(await receive_op(phone, OP_OUTPUT), session.id.encode() + b'\0legacy-live')
                self.assertFalse(getattr(service.rooms['local-test'], 'flow_control', False))
            finally:
                socket = daemon._relay_sockets.get(legacy_url)
                if socket is not None:
                    await socket.close(code=1000)
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)

    async def test_removed_phone_cannot_receive_stale_flow_control(self):
        relay = Relay()
        host = Socket()
        room = Room(code='room', host=host, flow_control=True)
        room.phones[1] = Socket()
        room.queues[1] = asyncio.Queue(QUEUE_FRAMES)
        relay._schedule_flow(room, 1, True)
        room.phones.pop(1)
        await asyncio.sleep(0)
        self.assertEqual(host.sent, [])

    async def test_seven_full_histories_recover_after_slow_phone_without_reconnect(self):
        from websockets.asyncio.client import connect
        from supermonoagent.protocol import OP_HELLO, OP_HELLO_OK, OP_SUBSCRIBE, pack_json
        from test_client_integration import receive_op

        from websockets.exceptions import ConnectionClosed

        release = asyncio.Event()
        release.set()
        original_enqueue = Relay._enqueue
        maximum = [0]
        opcodes = {}

        async def gated_drain(relay, ws, queue, on_sent=None):
            try:
                while True:
                    message = await queue.get()
                    await release.wait()
                    await ws.send(message)
                    if on_sent is not None:
                        on_sent()
            except (asyncio.CancelledError, ConnectionClosed):
                return

        def counted_enqueue(relay, room, phone_id, message):
            op = unpack(message)[0]
            opcodes[op] = opcodes.get(op, 0) + 1
            original_enqueue(relay, room, phone_id, message)
            queue = room.queues.get(phone_id)
            if queue is not None:
                maximum[0] = max(maximum[0], queue.qsize())

        with patch.object(Relay, '_drain', gated_drain), patch.object(Relay, '_enqueue', counted_enqueue):
            async with isolated_host() as (daemon, _direct, relay_url):
                sessions = []
                for index in range(7):
                    window = daemon.hub.open_window(f'full-{index}', owner=daemon.token)
                    session = daemon.hub.open_cli(window.id, 'cmd')
                    session.pump.remember(f'history-{index}\r\n'.encode() + b'x' * (256 * 1024))
                    sessions.append(session)
                ids = [session.id for session in sessions]
                async with connect(relay_url) as phone:
                    await phone.send(json.dumps({'op': 'phone', 'code': 'local-test', 'token': daemon.token, 'reliable': True}))
                    self.assertEqual(json.loads(await phone.recv())['op'], 'ready')
                    await phone.send(pack_json(OP_HELLO, {'token': daemon.token, 'multiplex': True, 'reliable': True}))
                    await receive_op(phone, OP_HELLO_OK)
                    release.clear()
                    await phone.send(pack_json(OP_SUBSCRIBE, {'sessions': ids}))
                    async with asyncio.timeout(2):
                        while any(client.pending_replays for client in daemon.hub.clients.values()):
                            await asyncio.sleep(.01)
                    for round_id in range(8):
                        for index, session in enumerate(sessions):
                            data = (f'round-{round_id}-session-{index}\r\n' * 512).encode()
                            if round_id == 7:
                                data += f'final-{index}'.encode()
                            session.pump.remember(data)
                            session.pump.flushed_position = session.pump.snapshot_position()[1]
                            daemon.hub._flushed(session.id, data, False)
                        await asyncio.sleep(.05)
                    await asyncio.sleep(1)
                    states = [(client.key, client.remote_paused, client.failure, client.queue.qsize(),
                               len(client.pending_replays), len(client.resync_pending), client.feed_offsets) for client in daemon.hub.clients.values()]
                    self.assertTrue(any(client.remote_paused for client in daemon.hub.clients.values()),
                                    (states, maximum[0], opcodes, {sid: session.pump.position() for sid, session in zip(ids, sessions)}))
                    release.set()
                    recovered = set()
                    async with asyncio.timeout(20):
                        while len(recovered) < len(ids):
                            op, payload = unpack(await phone.recv())
                            if op != OP_SNAPSHOT:
                                continue
                            sid, separator, data = payload.partition(b'\0')
                            for index, session in enumerate(sessions):
                                if sid == session.id.encode() and f'final-{index}'.encode() in data:
                                    recovered.add(sid)
                    await barrier(phone)
                    self.assertLessEqual(maximum[0], QUEUE_FRAMES)
                    self.assertFalse(any(client.failure for client in daemon.hub.clients.values()))
