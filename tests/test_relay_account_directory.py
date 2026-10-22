"""Account-only directory and delegated routing over isolated loopback relays."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import struct
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from supermonoagent.relay import Relay
from supermonoagent.relay_identity import RelayIdentity
from test_security_directory import running, announce


@asynccontextmanager
async def fixture():
    with tempfile.TemporaryDirectory() as directory:
        relay=Relay(identity_file=Path(directory)/'identity.sqlite3',development=True)
        a=relay.identity.create_account('a@example.test','password-a')
        b=relay.identity.create_account('b@example.test','password-b')
        relay.identity.grant(a,'room-a')
        relay.identity.grant(b,'room-b')
        async with running(relay) as url:
            relay.public_url=url
            try:
                yield relay,url,a,b
            finally:
                if relay._identity_monitor:
                    relay._identity_monitor.cancel()
                    await asyncio.gather(relay._identity_monitor,return_exceptions=True)


async def login(url,email='a@example.test',password='password-a'):
    async with connect(url) as ws:
        await ws.send(json.dumps({'op':'relay_login','email':email,'password':password}))
        return json.loads(await asyncio.wait_for(ws.recv(),3))


async def directory(ws,session):
    await ws.send(json.dumps({'op':'watch','session':session}))
    return json.loads(await asyncio.wait_for(ws.recv(),2))


async def join(phone,host,code,session,account):
    await phone.send(json.dumps({'op':'phone','code':code,'session':session,'reliable':True}))
    request=json.loads(await asyncio.wait_for(host.recv(),2))
    assert request['op']=='join' and request['account']==account
    assert set(request)=={'op','id','account','expires','reliable'}
    # The phone must not be ready until its host acknowledges a local account binding.
    try:
        await asyncio.wait_for(phone.recv(),.03)
        raise AssertionError('Ready before local binding acknowledgment')
    except asyncio.TimeoutError:
        pass
    await host.send(json.dumps({'op':'joined','id':request['id'],'ok':True}))
    assert json.loads(await asyncio.wait_for(phone.recv(),2))=={'op':'ready'}
    return request['id']


async def receive_op(ws,op):
    while True:
        message=await asyncio.wait_for(ws.recv(),2)
        if isinstance(message,str) and json.loads(message).get('op')==op:
            return json.loads(message)


class RelayAccountDirectoryTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_granted_rooms_trusted_endpoint_names_and_no_secret_propagation(self):
        async with fixture() as (relay,url,a,b),connect(url) as host_a,connect(url) as host_b:
            await announce(host_a,code='room-a',token='daemon-a',machine='A')
            await announce(host_b,code='room-b',token='daemon-b',machine='B')
            first=await login(url)
            second=await login(url,'b@example.test','password-b')
            self.assertEqual(first['op'],'relay_auth')
            self.assertEqual(first['account']['id'],a)
            async with connect(url) as watcher_a,connect(url) as watcher_b:
                got_a=await directory(watcher_a,first['session'])
                got_b=await directory(watcher_b,second['session'])
                self.assertEqual(got_a['hosts'],[{'endpoint':url,'code':'room-a','online':True,'machine':'A','project':'Project'}])
                self.assertEqual([room['code'] for room in got_b['hosts']],['room-b'])
                self.assertTrue(got_a['capabilities']['relay_accounts'])
                for response in [got_a,got_b]:
                    text=json.dumps(response)
                    for secret in ['daemon-a','daemon-b',first['session'],second['session'],'owner_secret','password-a']:
                        self.assertNotIn(secret,text)
                async with connect(url) as unauthorized:
                    await unauthorized.send(json.dumps({'op':'phone','code':'room-b','session':first['session']}))
                    with self.assertRaises(ConnectionClosed):
                        await unauthorized.recv()

    async def test_delegated_join_ack_frame_direction_and_legacy_rotation_independence(self):
        async with fixture() as (relay,url,a,_b),connect(url) as host:
            await announce(host,code='room-a',token='daemon-a')
            auth=await login(url)
            async with connect(url) as phone:
                phone_id=await join(phone,host,'room-a',auth['session'],a)
                await phone.send(b'input')
                self.assertEqual(await host.recv(),struct.pack('<I',phone_id)+b'input')
                await host.send(struct.pack('<I',phone_id)+b'output')
                self.assertEqual(await phone.recv(),b'output')
                await host.send(json.dumps({'op':'tokens','tokens':['rotated-daemon-token']}))
                await host.send(struct.pack('<I',phone_id)+b'after-token-rotation')
                self.assertEqual(await phone.recv(),b'after-token-rotation')
                await host.send(json.dumps({'op':'disconnect','id':phone_id,'code':4001,'reason':'Local binding removed'}))
                with self.assertRaises(ConnectionClosed) as closed:
                    await phone.recv()
                self.assertEqual(closed.exception.rcvd.code,4001)
                self.assertNotIn(phone_id,relay.rooms['room-a'].queues)

    async def test_missing_binding_or_no_ack_never_sends_ready(self):
        async with fixture() as (relay,url,a,_b),connect(url) as host:
            await announce(host,code='room-a')
            auth=await login(url)
            for reply in [False,None]:
                async with connect(url) as phone:
                    with patch('supermonoagent.relay.FIRST_FRAME_S',.08):
                        await phone.send(json.dumps({'op':'phone','code':'room-a','session':auth['session']}))
                        request=await receive_op(host,'join')
                        if reply is not None:
                            await host.send(json.dumps({'op':'joined','id':request['id'],'ok':reply}))
                        with self.assertRaises(ConnectionClosed) as closed:
                            await phone.recv()
                        self.assertEqual(closed.exception.rcvd.code,4001)
                    await receive_op(host,'leave')

    async def test_external_grant_revoke_prunes_routes_and_directory_but_other_account_works(self):
        async with fixture() as (relay,url,a,b),connect(url) as host_a,connect(url) as host_b:
            await announce(host_a,code='room-a')
            await announce(host_b,code='room-b')
            auth_a=await login(url)
            auth_b=await login(url,'b@example.test','password-b')
            async with connect(url) as phone_a,connect(url) as phone_b,connect(url) as watcher:
                aid=await join(phone_a,host_a,'room-a',auth_a['session'],a)
                bid=await join(phone_b,host_b,'room-b',auth_b['session'],b)
                await directory(watcher,auth_a['session'])
                external=RelayIdentity(relay.identity.path)
                started=time.monotonic()
                external.revoke(a,'room-a')
                while True:
                    snapshot=json.loads(await asyncio.wait_for(watcher.recv(),1))
                    if snapshot['hosts']==[]:
                        break
                self.assertLess(time.monotonic()-started,1)
                with self.assertRaises(ConnectionClosed):
                    await phone_a.recv()
                self.assertNotIn(aid,relay.rooms['room-a'].phones)
                self.assertNotIn(aid,relay.rooms['room-a'].queues)
                await host_b.send(struct.pack('<I',bid)+b'other-account-still-live')
                self.assertEqual(await phone_b.recv(),b'other-account-still-live')

    async def test_logout_reset_expiry_and_host_disconnect_revoke(self):
        for action in ['logout','reset','expiry','host_disconnect']:
            with self.subTest(action=action):
                async with fixture() as (relay,url,a,_b),connect(url) as host:
                    await announce(host,code='room-a')
                    auth=await login(url)
                    async with connect(url) as phone,connect(url) as watcher:
                        phone_id=await join(phone,host,'room-a',auth['session'],a)
                        await directory(watcher,auth['session'])
                        # Drain directory frames emitted before revocation (already on the wire).
                        await asyncio.sleep(.3)
                        try:
                            while True:
                                await asyncio.wait_for(watcher.recv(),.01)
                        except asyncio.TimeoutError:
                            pass
                        if action=='logout':
                            async with connect(url) as logout:
                                await logout.send(json.dumps({'op':'relay_logout','session':auth['session']}))
                                self.assertTrue(json.loads(await logout.recv())['logged_out'])
                        elif action=='reset':
                            await asyncio.to_thread(RelayIdentity(relay.identity.path).reset_password,a,'new-password')
                        elif action=='expiry':
                            with relay.identity.connection() as db:
                                db.execute('UPDATE sessions SET expires=?',(time.time()-1,))
                        else:
                            await host.close()
                        with self.assertRaises(ConnectionClosed):
                            await asyncio.wait_for(phone.recv(),1)
                        if action!='host_disconnect':
                            with self.assertRaises(ConnectionClosed):
                                while True:
                                    message=await asyncio.wait_for(watcher.recv(),1)
                                    self.assertNotIn('Computer',message)

    async def test_account_transport_requires_tls_or_explicit_trusted_proxy(self):
        relay=Relay(public_url='https://relay.example')
        ws=SimpleNamespace(transport=None,remote_address=('203.0.113.9',123),request=SimpleNamespace(headers={'X-Forwarded-Proto':'https'}))
        self.assertFalse(relay._secure_accounts(ws))
        relay.reverse_proxy_tls=True
        self.assertFalse(relay._secure_accounts(ws))
        ws.remote_address=('127.0.0.1',123)
        self.assertTrue(relay._secure_accounts(ws))
        ws.request.headers['X-Forwarded-Proto']='http'
        self.assertFalse(relay._secure_accounts(ws))
        relay.reverse_proxy_tls=False
        ws.request.headers['X-Forwarded-Proto']='https'
        self.assertFalse(relay._secure_accounts(ws))

    async def test_real_plaintext_public_mode_rejects_login_session_watch_and_phone(self):
        async with fixture() as (relay,url,a,_b):
            auth=await login(url)
            relay.development=False
            relay.public_url='wss://relay.example/ws'
            for message in [{'op':'relay_login','email':'a@example.test','password':'password-a'}, {'op':'watch','session':auth['session']},{'op':'phone','code':'room-a','session':auth['session']}]:
                async with connect(url) as ws:
                    await ws.send(json.dumps(message))
                    with self.assertRaises(ConnectionClosed):
                        await ws.recv()

class RelayAccountDaemonIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_account_discovery_binding_real_terminal_io_and_unbind(self):
        from supermonoagent.daemon import relay_uplink
        from supermonoagent.protocol import OP_HELLO,OP_HELLO_OK,OP_SUBSCRIBE,OP_INPUT_SESSION,OP_OUTPUT,pack,pack_json,unpack_json
        from test_client_integration import isolated_host,receive_op as frame_op
        async with fixture() as (relay,url,account,_b), isolated_host() as (daemon,_direct,_legacy):
            window=daemon.hub.open_window(owner=daemon.token)
            session=daemon.hub.open_cli(window.id,'cmd')
            daemon.relay_bindings.bind(url,'room-a',account,daemon.token)
            uplink=asyncio.create_task(relay_uplink(daemon,url,'room-a','account-real'))
            try:
                async with asyncio.timeout(3):
                    while url not in daemon.relay_live:
                        await asyncio.sleep(.01)
                auth=await login(url)
                async with connect(url) as watcher:
                    discovered=(await directory(watcher,auth['session']))['hosts'][0]
                    self.assertEqual((discovered['endpoint'],discovered['code']),(url,'room-a'))
                async with connect(discovered['endpoint']) as phone:
                    await phone.send(json.dumps({'op':'phone','code':discovered['code'],'session':auth['session']}))
                    self.assertEqual(json.loads(await asyncio.wait_for(phone.recv(),2)),{'op':'ready'})
                    # Login session and local daemon token never appear in terminal HELLO.
                    await phone.send(pack_json(OP_HELLO,{'multiplex':True,'reliable':True}))
                    state=unpack_json(await frame_op(phone,OP_HELLO_OK))
                    self.assertEqual(state['windows'][0]['id'],window.id)
                    await phone.send(pack_json(OP_SUBSCRIBE,{'sessions':[session.id]}))
                    await frame_op(phone,OP_HELLO_OK)
                    await phone.send(pack(OP_INPUT_SESSION,session.id.encode()+b'\0directed input'))
                    async with asyncio.timeout(2):
                        while not session.pty.written:
                            await asyncio.sleep(.01)
                    self.assertEqual(session.pty.written,b'directed input')
                    session.pty.inbox.put(b'directed output')
                    self.assertEqual(await frame_op(phone,OP_OUTPUT),session.id.encode()+b'\0directed output')
                    daemon.relay_bindings.bind(url,'room-a',account,None)
                    with self.assertRaises(ConnectionClosed) as closed:
                        while True:
                            await asyncio.wait_for(phone.recv(),1)
                    self.assertEqual(closed.exception.rcvd.code,4001)
            finally:
                uplink.cancel()
                await asyncio.gather(uplink,return_exceptions=True)

    async def test_account_tls_trusted_fixture_and_default_verification(self):
        import ssl
        from supermonoagent.security import tls_context
        fixture_path=Path(__file__).parent/'fixtures'
        cert=fixture_path/'tls-loopback-cert.pem'
        trust=ssl.create_default_context(cafile=str(cert))
        with tempfile.TemporaryDirectory() as tls_directory:
            relay=Relay(identity_file=Path(tls_directory)/'accounts.sqlite3')
            account=relay.identity.create_account('tls@example.test','tls-password')
            relay.identity.grant(account,'offline-granted-room')
            async with running(relay,ssl=tls_context(cert,fixture_path/'tls-loopback-key.pem')) as url:
                relay.public_url=url
                try:
                    with self.assertRaises(ssl.SSLCertVerificationError):
                        async with connect(url):
                            pass
                    async with connect(url,ssl=trust) as ws:
                        await ws.send(json.dumps({'op':'relay_login','email':'tls@example.test','password':'tls-password'}))
                        auth=json.loads(await ws.recv())
                    async with connect(url,ssl=trust) as ws:
                        snapshot=await directory(ws,auth['session'])
                        self.assertEqual(snapshot['hosts'],[{'endpoint':url,'code':'offline-granted-room','online':False}])
                finally:
                    if relay._identity_monitor:
                        relay._identity_monitor.cancel()
                        await asyncio.gather(relay._identity_monitor,return_exceptions=True)

    async def test_revocation_closes_phone_without_waiting_for_backpressured_host(self):
        from supermonoagent.relay import Room
        relay=Relay()
        blocked,closed,host_closed=asyncio.Event(),asyncio.Event(),asyncio.Event()
        class Host:
            async def send(self,message):
                await blocked.wait()
            async def close(self,**kwargs):
                host_closed.set()
        class Phone:
            async def close(self,**kwargs):
                closed.set()
        room=Room(host=Host())
        queue=asyncio.Queue(32)
        queue.put_nowait(b'pending private bytes')
        room.phones[1]=Phone()
        room.queues[1]=queue
        relay._drop_phone(room,1)
        self.assertNotIn(1,room.phones)
        self.assertTrue(queue.empty())
        await asyncio.wait_for(closed.wait(),.1)
        self.assertFalse(blocked.is_set())
        await asyncio.wait_for(host_closed.wait(),.5)

    async def test_expired_source_keys_do_not_permanently_fill_login_cap(self):
        async with fixture() as (relay,url,_a,_b):
            relay._login_attempts={f'old-peer-{index}':[time.monotonic()-61] for index in range(1024)}
            auth=await login(url)
            self.assertEqual(auth['op'],'relay_auth')
            self.assertIn('session',auth)
            self.assertEqual(set(relay._login_attempts),{'127.0.0.1'})
            self.assertEqual(len(relay._login_attempts['127.0.0.1']),1)
            self.assertEqual(relay._login_slots._value,4)

    async def test_active_source_cap_and_per_peer_rate_limit_remain_enforced(self):
        async with fixture() as (relay,url,_a,_b):
            now=time.monotonic()
            relay._login_attempts={f'active-peer-{index}':[now] for index in range(1024)}
            with self.assertRaises(ConnectionClosed) as closed:
                await login(url)
            self.assertEqual(closed.exception.rcvd.code,4001)
            self.assertEqual(len(relay._login_attempts),1024)
            relay._login_attempts={'127.0.0.1':[time.monotonic()]*10}
            with self.assertRaises(ConnectionClosed) as closed:
                await login(url)
            self.assertEqual(closed.exception.rcvd.code,4001)
            self.assertEqual(len(relay._login_attempts['127.0.0.1']),10)
            self.assertEqual(relay._login_slots._value,4)

    async def test_only_locally_revoked_expected_closures_are_routine(self):
        from websockets.exceptions import ConnectionClosedError
        from websockets.frames import Close
        from supermonoagent.relay import Room
        for code,revoked,routine in [(4001,True,True),(4001,False,False),(1011,True,False)]:
            with self.subTest(code=code,revoked=revoked):
                relay=Relay()
                class Host:
                    async def send(self,message):
                        pass
                room=Room(host=Host(),tokens={'legacy'})
                error=ConnectionClosedError(Close(code,'test'),Close(code,'test'),True)
                class Phone:
                    async def send(self,message):
                        pass
                    def __aiter__(self):
                        async def messages():
                            if revoked:
                                room.queues.pop(1,None)
                            raise error
                            yield
                        return messages()
                if routine:
                    await relay._phone(Phone(),'room',room,'legacy')
                else:
                    with self.assertRaises(ConnectionClosedError) as caught:
                        await relay._phone(Phone(),'room',room,'legacy')
                    self.assertIs(caught.exception,error)
