"""Same-token discovery stays private, bounded, live, and transport authenticated."""
import asyncio
import json
from types import SimpleNamespace
import unittest

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from supermonoagent.relay import Relay
from test_security_directory import running, announce, watch


async def directory(ws, predicate=lambda value: True):
    async with asyncio.timeout(2):
        while True:
            value = json.loads(await ws.recv())
            if predicate(value['hosts']):
                return value


class TokenDiscoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_new_rooms_identity_rotation_leave_and_notification_filter(self):
        relay = Relay(development=True)
        async with running(relay) as url, connect(url) as watcher:
            await watcher.send(json.dumps(dict(op='watch', tokens=['unrelated', 'shared-owner'])))
            initial = await directory(watcher)
            self.assertEqual(initial['hosts'], [])
            self.assertTrue(initial['capabilities']['token_discovery'])
            async with connect(url) as first, connect(url) as hidden, connect(url) as events:
                await announce(first, code='first', token='shared-owner', machine='First')
                added = await directory(watcher, lambda hosts: bool(hosts))
                self.assertEqual(added['hosts'], [dict(code='first', online=True, machine='First', project='Project', token_index=1)])
                await announce(hidden, code='other', token='other-owner', machine='Private')
                await events.send(json.dumps(dict(op='host', code='first-events', tokens=['shared-owner'], service='notifications', machine='Events')))
                self.assertEqual(json.loads(await events.recv())['op'], 'ready')
                await first.send(json.dumps(dict(op='identity', machine='Renamed', project='Updated')))
                changed = await directory(watcher, lambda hosts: any(item.get('machine') == 'Renamed' for item in hosts))
                self.assertEqual([item['code'] for item in changed['hosts']], ['first'])
                self.assertNotIn('shared-owner', json.dumps(changed))
                self.assertNotIn('other-owner', json.dumps(changed))
                self.assertNotIn('Private', json.dumps(changed))
                self.assertEqual(changed['hosts'][0]['project'], 'Updated')
                self.assertGreater(changed['revision'], added['revision'])
                await first.send(json.dumps(dict(op='tokens', tokens=['rotated'])))
                self.assertEqual((await directory(watcher, lambda hosts: not hosts))['hosts'], [])
                await first.send(json.dumps(dict(op='tokens', tokens=['shared-owner'])))
                await directory(watcher, bool)
                await first.close()
                self.assertEqual((await directory(watcher, lambda hosts: not hosts))['hosts'], [])

    async def test_known_room_semantics_and_duplicate_room_enrichment(self):
        relay = Relay(development=True)
        async with running(relay) as url, connect(url) as host, connect(url) as watcher:
            await announce(host, token='shared')
            legacy = await watch(watcher, [('room', 'shared'), ('unknown', 'shared')])
            self.assertEqual(legacy['hosts'], [dict(code='room', online=True, machine='Computer', project='Project'), dict(code='unknown', online=False)])
            await watcher.send(json.dumps(dict(op='watch', rooms=[dict(code='room', token='shared')], tokens=['shared'])))
            combined = await directory(watcher)
            self.assertEqual(len(combined['hosts']), 1)
            self.assertEqual(combined['hosts'][0]['token_index'], 0)
            await watcher.send(json.dumps(dict(op='watch', tokens=['wrong'])))
            self.assertEqual((await directory(watcher))['hosts'], [])
            self.assertNotIn('unknown', relay.rooms)

    async def test_invalid_requests_empty_credentials_and_session_mixing(self):
        relay = Relay(development=True)
        async with running(relay) as url:
            invalid = [None, '', {}, [''], ['same', 'same'], [42], [[]], ['x' * 4097], [str(i) for i in range(33)]]
            for tokens in invalid:
                async with connect(url) as ws:
                    await ws.send(json.dumps(dict(op='watch', tokens=tokens)))
                    with self.assertRaises(ConnectionClosed):
                        await ws.recv()
            async with connect(url) as ws:
                await ws.send(json.dumps(dict(op='watch', session='invalid', tokens=['shared'])))
                with self.assertRaises(ConnectionClosed):
                    await ws.recv()
            async with connect(url) as ws:
                await ws.send(json.dumps(dict(op='watch', tokens=[])))
                self.assertEqual((await directory(ws))['hosts'], [])
            async with connect(url + '?token=not-allowed') as ws:
                with self.assertRaises(ConnectionClosed):
                    await ws.recv()
            self.assertEqual(relay.rooms, {})

    async def test_snapshot_replacement_prunes_revoked_identity_and_bounds_results(self):
        from supermonoagent.relay import Room
        relay = Relay()
        ws = object()
        queue = asyncio.Queue(32)
        relay.watchers[ws] = ([], queue)
        relay.token_watchers[ws] = ['owner']
        relay.rooms = {str(i): Room(code=str(i), host=object(), tokens={'owner'}, machine='Private') for i in range(70)}
        relay._publish()
        self.assertEqual(len(json.loads(queue.get_nowait())['hosts']), 64)
        relay._publish()
        for room in relay.rooms.values():
            room.tokens.clear()
        for _ in range(100):
            relay._publish()
        self.assertEqual(queue.qsize(), 1)
        latest = queue.get_nowait()
        self.assertNotIn('Private', latest)
        self.assertEqual(json.loads(latest)['hosts'], [])

    def test_secure_transport_guard_never_trusts_forwarded_headers_from_public_peer(self):
        def ws(peer='203.0.113.1', forwarded='https', ssl=False):
            return SimpleNamespace(remote_address=(peer, 1000), request=SimpleNamespace(headers={'X-Forwarded-Proto': forwarded}),
                                   transport=SimpleNamespace(get_extra_info=lambda name: object() if ssl else None))
        self.assertFalse(Relay()._secure_token_discovery(ws()))
        self.assertTrue(Relay()._secure_token_discovery(ws(ssl=True)))
        self.assertTrue(Relay()._secure_token_discovery(ws(peer='127.0.0.1')))
        proxy = Relay(reverse_proxy_tls=True)
        self.assertFalse(proxy._secure_token_discovery(ws()))
        self.assertFalse(proxy._secure_token_discovery(ws(peer='127.0.0.1', forwarded='http')))
        self.assertTrue(proxy._secure_token_discovery(ws(peer='127.0.0.1')))
