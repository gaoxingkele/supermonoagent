"""A compatibility bridge must not compete with its daemon's native uplink."""
import asyncio
from contextlib import asynccontextmanager
import json
import unittest

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from supermonoagent.relay_bridge import RelayBridge
from supermonoagent.protocol import OP_SESSIONS,pack_json
from test_client_integration import isolated_host


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.01)


@asynccontextmanager
async def surface(configured=False, online=False):
    async with isolated_host() as (daemon, _direct, _other_relay):
        window=daemon.hub.open_window('Bridge native fixture',owner=daemon.token)
        session=daemon.hub.open_cli(window.id,'cmd')
        registrations=[]
        async def relay(ws):
            try:
                registrations.append(json.loads(await ws.recv()))
                await ws.send(json.dumps({'op':'ready'}))
                await ws.wait_closed()
            except ConnectionClosed:
                pass
        async with serve(relay,'127.0.0.1',0) as remote, serve(daemon.handle_socket,'127.0.0.1',0) as local:
            target=f'ws://127.0.0.1:{remote.sockets[0].getsockname()[1]}/ws'
            backend=f'ws://127.0.0.1:{local.sockets[0].getsockname()[1]}/ws'
            if configured:
                # A different room is still the same daemon-managed relay.
                daemon.relay_targets=[{'url':target,'code':'native-room','name':'native'}]
                if online:daemon.relay_live.add(target)
            bridge=RelayBridge(backend,daemon.token,retry_s=.02)
            observer=asyncio.create_task(bridge.observe_backend())
            uplink=asyncio.create_task(bridge.relay(target,'compat-room'))
            try:
                await until(lambda:bridge.backend_connected)
                yield daemon,session,bridge,registrations,target,local,observer
            finally:
                observer.cancel();uplink.cancel()
                await asyncio.gather(observer,uplink,return_exceptions=True)
                daemon.hub.close_window(window.id)


class NativeRelayBridgeTest(unittest.IsolatedAsyncioTestCase):
    async def test_native_configured_online_and_offline_do_not_register_another_host(self):
        for online in (False,True):
            with self.subTest(online=online):
                async with surface(True,online) as (daemon,session,bridge,registrations,_url,_server,_observer):
                    await asyncio.sleep(.1)
                    self.assertEqual(registrations,[], 'Compatibility bridge competed with native room')
                    self.assertTrue(bridge.health()['direct_relay_managed'])
                    self.assertFalse(bridge.relay_connected)
                    self.assertEqual(bytes(session.pty.written),b'')
                    self.assertEqual(len(daemon.hub.sessions),1)
                    self.assertIsNone(session.resize_owner)

    async def test_native_configuration_transition_releases_only_bridge_uplink_and_can_resume(self):
        async with surface() as (daemon,session,bridge,registrations,target,_server,_observer):
            await until(lambda:bridge.relay_connected)
            self.assertEqual(len(registrations),1)
            daemon.relay_targets=[{'url':target,'code':'another-native-room','name':'native'}]
            daemon.publish_status()
            await until(lambda:bridge.health()['direct_relay_managed'] and not bridge.relay_connected)
            await asyncio.sleep(.1)
            self.assertEqual(len(registrations),1)
            daemon.relay_targets=[];daemon.publish_status()
            await until(lambda:bridge.relay_connected and len(registrations)==2)
            self.assertEqual(bytes(session.pty.written),b'')
            self.assertIn(session.id,daemon.hub.sessions)
            self.assertIsNone(session.resize_owner)

    async def test_other_native_endpoint_does_not_disable_legacy_proxy(self):
        async with surface() as (daemon,session,bridge,registrations,_target,_server,_observer):
            await until(lambda:bridge.relay_connected)
            daemon.relay_targets=[{'url':'wss://other.invalid/ws','code':'other','name':'other'}]
            daemon.publish_status();await asyncio.sleep(.1)
            self.assertTrue(bridge.relay_connected)
            self.assertFalse(bridge.health()['direct_relay_managed'])
            self.assertEqual(len(registrations),1)
            self.assertEqual(bytes(session.pty.written),b'')

    async def test_backend_disconnect_stops_advertising_and_reconnect_resumes(self):
        async with surface() as (_daemon,session,bridge,registrations,_target,_server,observer):
            await until(lambda:bridge.relay_connected)
            observer.cancel();await asyncio.gather(observer,return_exceptions=True)
            await until(lambda:not bridge.backend_connected and not bridge.relay_connected)
            previous=len(registrations);await asyncio.sleep(.1)
            self.assertEqual(len(registrations),previous)
            again=asyncio.create_task(bridge.observe_backend())
            try:
                await until(lambda:bridge.backend_connected and bridge.relay_connected)
                self.assertEqual(len(registrations),previous+1)
                self.assertEqual(bytes(session.pty.written),b'')
            finally:
                again.cancel();await asyncio.gather(again,return_exceptions=True)

    async def test_endpoint_comparison_preserves_legacy_ws_and_normalizes_tls_default_port(self):
        async with isolated_host() as (daemon,backend,_relay):
            bridge=RelayBridge(backend,daemon.token,retry_s=.02)
            bridge.relay_endpoint='wss://example.invalid/ws'
            daemon.relay_targets=[{'url':'https://EXAMPLE.invalid:443','code':'native','name':'native'}]
            observer=asyncio.create_task(bridge.observe_backend())
            try:
                await until(lambda:bridge.backend_connected)
                self.assertTrue(bridge.health()['direct_relay_managed'])
                daemon.relay_targets=[{'url':'ws://example.invalid:8817/ws','code':'legacy','name':'legacy'}]
                daemon.publish_status()
                await until(lambda:not bridge.health()['direct_relay_managed'])
            finally:
                observer.cancel();await asyncio.gather(observer,return_exceptions=True)

    async def test_no_host_registration_before_backend_authentication(self):
        registrations=[]
        async def rejected(ws):
            await ws.close(code=4001)
        async def relay(ws):
            try:registrations.append(await ws.recv())
            except ConnectionClosed:pass
        async with serve(rejected,'127.0.0.1',0) as local,serve(relay,'127.0.0.1',0) as remote:
            bridge=RelayBridge(f'ws://127.0.0.1:{local.sockets[0].getsockname()[1]}/ws','fixture-owner',retry_s=.02)
            observer=asyncio.create_task(bridge.observe_backend())
            uplink=asyncio.create_task(bridge.relay(f'ws://127.0.0.1:{remote.sockets[0].getsockname()[1]}/ws','must-not-register'))
            try:
                await asyncio.sleep(.15)
                self.assertEqual(registrations,[])
                self.assertFalse(bridge.backend_connected)
            finally:
                observer.cancel();uplink.cancel();await asyncio.gather(observer,uplink,return_exceptions=True)

    async def test_unsolicited_catalog_is_not_an_authentication_acknowledgement(self):
        registrations=[]
        async def invalid(ws):
            await ws.recv()
            await ws.send(pack_json(OP_SESSIONS,{'service':{'relays':[]}}))
            await ws.wait_closed()
        async def relay(ws):
            try:registrations.append(await ws.recv())
            except ConnectionClosed:pass
        async with serve(invalid,'127.0.0.1',0) as local,serve(relay,'127.0.0.1',0) as remote:
            bridge=RelayBridge(f'ws://127.0.0.1:{local.sockets[0].getsockname()[1]}/ws','fixture-owner',retry_s=.02)
            observer=asyncio.create_task(bridge.observe_backend())
            uplink=asyncio.create_task(bridge.relay(f'ws://127.0.0.1:{remote.sockets[0].getsockname()[1]}/ws','must-not-register'))
            try:
                await asyncio.sleep(.15)
                self.assertEqual(registrations,[])
                self.assertFalse(bridge.backend_connected)
            finally:
                observer.cancel();uplink.cancel();await asyncio.gather(observer,uplink,return_exceptions=True)
