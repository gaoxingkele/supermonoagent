"""Bounded private discovery and transport authentication, using isolated fixtures only."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import ssl
from types import SimpleNamespace
import unittest
import tempfile
from unittest.mock import patch

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed, InvalidStatus

from supermonoagent.relay import Relay, Room
from supermonoagent.security import request_allowed, tls_context
from supermonoagent.protocol import OP_HELLO, OP_HELLO_OK, pack_json, unpack
from supermonoagent.frontend import Frontend
from test_client_integration import isolated_host, receive_op


@asynccontextmanager
async def running(relay, **options):
    async with serve(relay.handler, '127.0.0.1', 0, process_request=relay.serve_page,
                     max_size=1024 * 1024, **options) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"{'wss' if options.get('ssl') else 'ws'}://127.0.0.1:{port}/ws"


async def announce(ws, code='room', secret='owner', token='access', machine='Computer'):
    await ws.send(json.dumps(dict(op='host', code=code, owner_secret=secret,
                                 tokens=[token], machine=machine, project='Project')))
    return json.loads(await asyncio.wait_for(ws.recv(), 2))


async def watch(ws, rooms):
    await ws.send(json.dumps(dict(op='watch', rooms=[dict(code=c, token=t) for c, t in rooms])))
    return json.loads(await asyncio.wait_for(ws.recv(), 2))


async def next_hosts(ws):
    return json.loads(await asyncio.wait_for(ws.recv(), 2))['hosts']


class DirectorySecurityTest(unittest.IsolatedAsyncioTestCase):
    async def test_owner_enrollment_restart_duplicate_and_terminal_token_cannot_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'owners.json'
            relay = Relay(path)
            secret = relay.enroll('room')
            self.assertNotIn(secret, path.read_text())
            relay = Relay(path)
            async with running(relay) as url:
                for code, offered in [('missing', secret), ('room', 'access'), ('room', 'wrong')]:
                    async with connect(url) as attacker:
                        with self.assertRaises(ConnectionClosed):
                            await announce(attacker, code=code, secret=offered, machine='Attacker')
                self.assertEqual(relay.rooms, {})
                async with connect(url) as host:
                    self.assertEqual((await announce(host, secret=secret))['op'], 'ready')
                    async with connect(url) as duplicate:
                        with self.assertRaises(ConnectionClosed):
                            await announce(duplicate, secret=secret, machine='Replacement')
                    self.assertEqual(relay.rooms['room'].machine, 'Computer')
                    self.assertEqual(relay.rooms['room'].tokens, {'access'})

    async def test_known_rooms_privacy_live_identity_offline_rotation_and_cross_room_tokens(self):
        relay = Relay(development=True)
        async with running(relay) as url, connect(url) as host, connect(url) as watcher:
            await announce(host)
            got = await watch(watcher, [('room','access'),('room','wrong'),('unknown','access')])
            self.assertEqual(got['hosts'], [dict(code='room', online=True, machine='Computer', project='Project'),
                                          dict(code='room', online=False), dict(code='unknown',online=False)])
            self.assertNotIn('unknown', relay.rooms)
            self.assertTrue(got['capabilities']['host_enrollment'])
            await host.send(json.dumps(dict(op='identity', machine='N'*100, project='P'*100)))
            self.assertEqual((await next_hosts(watcher))[0]['machine'], 'N'*40)
            async with connect(url) as phone:
                await phone.send(json.dumps(dict(op='phone',code='room', token='access')))
                self.assertEqual(json.loads(await phone.recv())['op'], 'ready')
                await host.recv()  # join
                await host.send(json.dumps(dict(op='tokens', tokens=['replacement'])))
                self.assertEqual(await next_hosts(watcher), [dict(code='room', online=False),dict(code='room',online=False),dict(code='unknown',online=False)])
                with self.assertRaises(ConnectionClosed):
                    await phone.recv()
            got = await watch(watcher, [('room','replacement')])
            self.assertTrue(got['hosts'][0]['online'])
            await host.close()
            self.assertEqual(await next_hosts(watcher), [dict(code='room', online=False)])
            async with connect(url) as fresh:
                self.assertEqual((await watch(fresh,[('room','replacement')]))['hosts'], [dict(code='room',online=False)])

    async def test_rotation_prunes_pending_identity_and_terminal_frames(self):
        relay = Relay()
        room = Room(host=object(), tokens={'old'}, machine='Secret')
        relay.rooms['room'] = room
        queue = asyncio.Queue(32)
        relay.watchers[object()] = ([('room','old')], queue)
        relay._publish()
        phone_queue = asyncio.Queue(32)
        phone_queue.put_nowait(b'stale terminal bytes')
        class Phone:
            async def close(self, **kwargs):
                self.closed = kwargs
        phone = Phone()
        room.phones[1] = phone
        room.phone_tokens[1] = 'old'
        room.queues[1] = phone_queue
        await relay._replace_tokens(room, ['new'])
        self.assertEqual(queue.qsize(), 1)
        self.assertNotIn('Secret', queue.get_nowait())
        self.assertTrue(phone_queue.empty())
        self.assertNotIn(1, room.queues)
        self.assertEqual(phone.closed['code'],4001)
        for _ in range(100):
            relay._publish()
        self.assertLessEqual(queue.qsize(), 32)

    async def test_room_count_code_length_rate_and_first_frame_timeout(self):
        relay = Relay(development=True)
        async with running(relay) as url:
            for rooms in [[('r'+str(i),'t') for i in range(65)], [('x'*129,'t')]]:
                async with connect(url) as ws:
                    with self.assertRaises(ConnectionClosed):
                        await watch(ws, rooms)
            async with connect(url) as ws:
                for _ in range(11):
                    await ws.send(json.dumps(dict(op='watch',rooms=[])))
                with self.assertRaises(ConnectionClosed):
                    while True:
                        await ws.recv()
            with patch('supermonoagent.relay.FIRST_FRAME_S', 0.03):
                async with connect(url) as ws:
                    with self.assertRaises(ConnectionClosed):
                        await ws.recv()
            self.assertEqual(relay.rooms,{})

    async def test_unknown_phone_and_account_do_not_create_rooms(self):
        relay = Relay()
        async with running(relay) as url:
            for op in ['phone', 'login', 'register']:
                async with connect(url) as ws:
                    await ws.send(json.dumps(dict(op=op,code='unknown',token='wrong')))
                    try:
                        await ws.recv()
                    except ConnectionClosed:
                        pass
            self.assertEqual(relay.rooms,{})


class TransportSecurityTest(unittest.IsolatedAsyncioTestCase):
    async def test_origin_exact_scheme_port_null_and_native_auth(self):
        relay = Relay()
        async with running(relay) as url:
            origin = url.replace('ws:', 'http:').removesuffix('/ws')
            for hostile in ['null','https://evil.example',origin.replace('http:', 'https:'), origin+'1']:
                with self.assertRaises(InvalidStatus):
                    async with connect(url, origin=hostile):
                        pass
            async with connect(url,origin=origin) as browser:
                self.assertEqual((await watch(browser,[('unknown','x')]))['hosts'],[dict(code='unknown',online=False)])
            async with connect(url) as native:
                await native.send(json.dumps(dict(op='phone',code='unknown',token='')))
                with self.assertRaises(ConnectionClosed):
                    await native.recv()

    async def test_http_health_rejects_rebinding_and_never_exposes_directory(self):
        relay = Relay(development=True)
        async with running(relay) as url:
            port = int(url.split(':')[-1].split('/')[0])
            for host, status in [('evil.example', b'403'),('127.0.0.1:'+str(port),b'200')]:
                reader, writer = await asyncio.open_connection('127.0.0.1',port)
                writer.write(f'GET /health HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n'.encode())
                await writer.drain()
                result = await reader.read()
                writer.close()
                await writer.wait_closed()
                self.assertIn(status,result.split(b'\r\n')[0])
                if status == b'200':
                    self.assertEqual(json.loads(result.split(b'\r\n\r\n')[1]), {'ok':True,'role':'relay'})

    async def test_direct_first_binary_hello_native_rejection_and_no_query_tokens(self):
        async with isolated_host() as (_daemon, direct, _relay):
            async with connect(direct) as ws:
                await ws.send(pack_json(OP_HELLO, {'token':'integration-owner'}))
                await receive_op(ws, OP_HELLO_OK)
            for target, frame in [(direct,pack_json(OP_HELLO, {'token':'wrong'})),
                                  (direct+'?token=integration-owner',pack_json(OP_HELLO, {'token':'integration-owner'}))]:
                async with connect(target) as ws:
                    try:
                        await ws.send(frame)
                        await ws.recv()
                        self.fail('Unauthenticated connection accepted')
                    except ConnectionClosed:
                        pass

    async def test_old_loopback_adapter_uses_header_forwards_hello_once_and_no_query(self):
        seen=[]
        async def old(ws):
            seen.append((ws.request.path, ws.request.headers.get('Authorization')))
            first=await ws.recv()
            seen.append(first)
            await ws.send(pack_json(OP_HELLO_OK, {'windows':[], 'capabilities':{}}))
            async for frame in ws:
                seen.append(frame)
        with tempfile.TemporaryDirectory() as directory:
            async with serve(old,'127.0.0.1',0) as backend:
                frontend=Frontend(f'http://127.0.0.1:{backend.sockets[0].getsockname()[1]}', Path(directory)/'themes.json')
                async with serve(frontend.handler,'127.0.0.1',0,process_request=frontend.serve_http) as server:
                    async with connect(f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ws') as browser:
                        hello=pack_json(OP_HELLO,{'token':'private'})
                        await browser.send(hello)
                        await receive_op(browser,OP_HELLO_OK)
                        await asyncio.sleep(.02)
                        self.assertEqual(seen,[('/ws','Bearer private'),hello])

    async def test_local_trusted_tls_and_default_trust_failure(self):
        fixture=Path(__file__).parent/'fixtures'
        cert=fixture/'tls-loopback-cert.pem'
        context=tls_context(cert,fixture/'tls-loopback-key.pem')
        relay=Relay()
        async with running(relay,ssl=context) as url:
            with self.assertRaises(ssl.SSLCertVerificationError):
                async with connect(url):
                    pass
            trust=ssl.create_default_context(cafile=str(cert))
            async with connect(url, ssl=trust) as ws:
                self.assertEqual((await watch(ws,[('unknown','x')]))['hosts'],[dict(code='unknown',online=False)])
        self.assertTrue(ssl.create_default_context().check_hostname)


class HostPolicyTest(unittest.TestCase):
    def test_configured_public_origin_requires_exact_authority_and_no_null(self):
        connection=SimpleNamespace(local_address=('0.0.0.0',6770))
        def allowed(host,origin):
            return request_allowed(connection,SimpleNamespace(headers={'Host':host,'Origin':origin}),['https://terminal.example:443'])
        self.assertTrue(allowed('terminal.example:443','https://terminal.example:443'))
        for host,origin in [('evil.example','https://terminal.example:443'),('terminal.example:443','null'),('terminal.example:443','http://terminal.example:443'),('terminal.example:443','https://terminal.example')]:
            self.assertFalse(allowed(host,origin))

class DirectRevocationTest(unittest.IsolatedAsyncioTestCase):
    async def test_account_rotation_revokes_direct_and_frontend_pending_output_and_input(self):
        from supermonoagent.protocol import OP_SUBSCRIBE, OP_INPUT_SESSION, OP_OUTPUT
        async with isolated_host() as (daemon, direct, _relay):
            account=daemon.account_reply({'op':'register','email':'revoked@example.test','password':'test-password'})
            old=account['token']
            window=daemon.hub.open_window(owner=old)
            session=daemon.hub.open_cli(window.id,'cmd')
            with tempfile.TemporaryDirectory() as directory:
                frontend=Frontend(direct.replace('ws:','http:').removesuffix('/ws'),Path(directory)/'themes.json')
                async with serve(frontend.handler,'127.0.0.1',0,process_request=frontend.serve_http) as server:
                    adapter=f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ws'
                    async with connect(direct) as native, connect(adapter) as browser:
                        for ws in [native,browser]:
                            await ws.send(pack_json(OP_HELLO,{'token':old,'multiplex':True}))
                            await receive_op(ws,OP_HELLO_OK)
                            await ws.send(pack_json(OP_SUBSCRIBE,{'sessions':[session.id]}))
                            await receive_op(ws,OP_HELLO_OK)
                        stale=[client for client in daemon.hub.clients.values() if client.token==old]
                        self.assertEqual(len(stale),2)
                        senders=[daemon._direct_sockets[client.key][1] for client in stale]
                        for client in stale:
                            client.queue.put_nowait(b'queued private output')
                        # This synchronous boundary must remove every route before a close can await.
                        reply=daemon.account_reply({'op':'rotate','email':'revoked@example.test','password':'test-password'})
                        daemon.publish_tokens()
                        self.assertNotEqual(reply['token'],old)
                        for client in stale:
                            self.assertNotIn(client.key,daemon.hub.clients)
                            self.assertTrue(client.failure)
                            self.assertEqual(client.queue.qsize(),1)  # failure sentinel only
                            frames,code=await daemon._op(client,OP_INPUT_SESSION,session.id.encode()+b'\0stale input')
                            self.assertEqual((frames,code),([],4001))
                        self.assertTrue(all(sender.cancelling() for sender in senders))
                        self.assertEqual(session.pty.written,b'')
                        session.pty.inbox.put(b'after-rotation-private-marker')
                        for ws in [native,browser]:
                            with self.assertRaises(ConnectionClosed) as closed:
                                while True:
                                    frame=await asyncio.wait_for(ws.recv(),2)
                                    self.assertNotIn(b'after-rotation-private-marker', frame if isinstance(frame,bytes) else frame.encode())
                            self.assertEqual(closed.exception.rcvd.code,4001)
                        async with connect(direct) as replacement:
                            await replacement.send(pack_json(OP_HELLO,{'token':reply['token']}))
                            await receive_op(replacement,OP_HELLO_OK)

    async def test_benchmark_multi_host_shutdown_has_no_handler_errors(self):
        import sys
        script=Path(__file__).resolve().parents[1]/'scripts'/'benchmark-global-workspace.py'
        with tempfile.TemporaryDirectory() as directory:
            process=await asyncio.create_subprocess_exec(sys.executable,str(script),'--hosts','1','4','16','--output',str(Path(directory)/'report.json'),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            stdout,stderr=await asyncio.wait_for(process.communicate(),20)
            self.assertEqual(process.returncode,0,stderr.decode())
            self.assertEqual(stderr,b'')
            report=json.loads(stdout)
            self.assertEqual([item['hosts'] for item in report['samples']],[1,4,16])
            self.assertTrue(all(item['unopened_terminal_output_bytes']==0 for item in report['samples']))
