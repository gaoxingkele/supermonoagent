"""Exercise the backport against the exact pre-enrollment relay source fixture."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import unittest

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from test_security_directory import running, announce, watch
from test_relay_token_discovery import directory


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def relay(*, proxy=False):
    legacy = load('supermonoagent.legacy_relay_fixture', ROOT / 'tests/fixtures/legacy-relay.py')
    adapter = load('token_discovery_compat', ROOT / 'deploy/relay/token_discovery_compat.py')
    return adapter.install(legacy, reverse_proxy_tls=proxy)()


class LegacyDiscoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_same_token_new_host_metadata_rotation_and_disconnect(self):
        service = relay()
        async with running(service) as url, connect(url) as watcher:
            await watcher.send(json.dumps(dict(op='watch', tokens=['wrong', 'shared'])))
            first = await directory(watcher)
            self.assertEqual(first['hosts'], [])
            self.assertFalse(first['capabilities']['host_enrollment'])
            async with connect(url) as host, connect(url) as hidden, connect(url) as events:
                await announce(host, code='pc', token='shared', machine='One')
                visible = await directory(watcher, bool)
                self.assertEqual(visible['hosts'], [dict(code='pc', online=True, machine='One', project='Project', token_index=1)])
                await announce(hidden, code='hidden', token='private', machine='Secret')
                await events.send(json.dumps(dict(op='host', code='pc-events', tokens=['shared'], service='notifications', machine='Events')))
                await events.recv()
                await host.send(json.dumps(dict(op='identity', machine='Two', project='Updated')))
                renamed = await directory(watcher, lambda hosts: any(item.get('machine') == 'Two' for item in hosts))
                self.assertEqual(len(renamed['hosts']), 1)
                self.assertNotIn('shared', json.dumps(renamed))
                self.assertNotIn('Secret', json.dumps(renamed))
                await host.send(json.dumps(dict(op='tokens', tokens=['replacement'])))
                self.assertEqual((await directory(watcher, lambda hosts: not hosts))['hosts'], [])
                await host.send(json.dumps(dict(op='tokens', tokens=['shared'])))
                await directory(watcher, bool)
                await host.close()
                self.assertEqual((await directory(watcher, lambda hosts: not hosts))['hosts'], [])

    async def test_legacy_phone_bidirectional_bytes_duplicate_host_and_revocation(self):
        service = relay()
        async with running(service) as url, connect(url) as host:
            await announce(host, code='pc', token='shared')
            async with connect(url) as duplicate:
                with self.assertRaises(ConnectionClosed):
                    await announce(duplicate, code='pc', token='attacker')
            self.assertEqual(service.rooms['pc'].tokens, {'shared'})
            async with connect(url) as phone:
                await phone.send(json.dumps(dict(op='phone', code='pc', token='shared')))
                self.assertEqual(json.loads(await phone.recv())['op'], 'ready')
                joined = json.loads(await host.recv())
                phone_id = joined['id']
                await phone.send(b'\x01owner-hello')
                forwarded = await host.recv()
                self.assertEqual(forwarded[:4], phone_id.to_bytes(4, 'little'))
                self.assertEqual(forwarded[4:], b'\x01owner-hello')
                await host.send(phone_id.to_bytes(4, 'little') + b'\x03terminal-output')
                self.assertEqual(await phone.recv(), b'\x03terminal-output')
                await host.send(json.dumps(dict(op='tokens', tokens=['replacement'])))
                with self.assertRaises(ConnectionClosed):
                    await phone.recv()
                self.assertEqual(service.rooms['pc'].queues, {})

    async def test_known_watch_validation_unknown_room_and_proxy_tls(self):
        service = relay(proxy=True)
        async with running(service) as url, connect(url) as host:
            await announce(host, code='pc', token='shared')
            async with connect(url) as plaintext:
                await plaintext.send(json.dumps(dict(op='watch', tokens=['shared'])))
                with self.assertRaises(ConnectionClosed):
                    await plaintext.recv()
            async with connect(url, additional_headers={'X-Forwarded-Proto': 'https'}) as watcher:
                known = await watch(watcher, [('pc', 'shared'), ('missing', 'shared')])
                self.assertEqual(known['hosts'], [dict(code='pc', online=True, machine='Computer', project='Project'), dict(code='missing', online=False)])
            for hello in [dict(op='watch', tokens=['shared', 'shared']), dict(op='watch', tokens=['']),
                          dict(op='watch', tokens=[42]), dict(op='watch', tokens=['x'] * 33),
                          dict(op='watch', session='session', tokens=['shared']), []]:
                async with connect(url, additional_headers={'X-Forwarded-Proto': 'https'}) as ws:
                    await ws.send(json.dumps(hello))
                    with self.assertRaises(ConnectionClosed):
                        await ws.recv()
            async with connect(url) as phone:
                await phone.send(json.dumps(dict(op='phone', code='missing', token='shared')))
                with self.assertRaises(ConnectionClosed):
                    await phone.recv()
            self.assertNotIn('missing', service.rooms)

    async def test_bounded_pending_directory_drops_revoked_identity(self):
        service = relay()
        room = service.room('pc')
        room.host, room.machine, room.tokens = object(), 'Private', {'shared'}
        queue = asyncio.Queue(1)
        service.watchers[object()] = ([], ['shared'], queue)
        service._publish()
        room.tokens.clear()
        for _ in range(100):
            service._publish()
        self.assertEqual(queue.qsize(), 1)
        self.assertNotIn('Private', queue.get_nowait())
