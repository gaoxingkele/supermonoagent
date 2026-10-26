"""Owned loopback protocol isolation; no V1 production state or real PTYs."""
import asyncio
import json
from pathlib import Path

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed
from websockets.asyncio.server import serve
from websockets.protocol import State

from test_file_broker import running, auth_file, hello, login, stage, call, http_fetch
from supermonoagent.file_companion_manager import FileCompanionManager
from supermonoagent.file_broker import Broker, BrokerError
from supermonoagent.file_companion import FileCompanion, _endpoint
from test_file_broker import TOKEN, KEY, enrollment_proof
from test_file_companion import OldParent, eventually


def test_v2_selector_and_positive_ready_marker(tmp_path):
    async def check():
        async with running(tmp_path/'broker', auth_file(tmp_path)) as (_, url):
            async with connect(url+'?file_protocol=2', proxy=None) as ws:
                await ws.send(json.dumps({**hello(), 'file_protocol': 2}))
                ready = json.loads(await ws.recv())
                assert ready['type'] == 'ready' and ready['file_protocol'] == 2
    asyncio.run(check())


def test_v2_http_cannot_use_ticket_without_explicit_selector(tmp_path):
    async def check():
        async with running(tmp_path/'broker', auth_file(tmp_path)) as (broker, url):
            _, address = broker.start_http()
            host, viewer = await login(url, hello('host')), await login(url, hello())
            try:
                _, job = await stage(viewer, host)
                ticket = (await call(host, 'download.ticket', {'file_id': job['file_id']}))['result']
                import urllib.request, urllib.error
                def missing():
                    request = urllib.request.Request(f'http://127.0.0.1:{address[1]}'+ticket['download_path'],
                        headers={'Authorization': 'Bearer '+ticket['ticket']})
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    try:
                        with opener.open(request, timeout=2) as response:return response.status
                    except urllib.error.HTTPError as error:return error.code
                assert await asyncio.to_thread(missing) == 401
            finally:await viewer.close();await host.close()
    asyncio.run(check())


def test_manager_uses_separate_v2_state_but_original_profile(tmp_path):
    manager = FileCompanionManager(tmp_path, 'http://127.0.0.1:1234', 'owned-owner', 'room')
    assert manager.root == tmp_path/'file-exchange/v2'
    assert manager.profile == tmp_path/'file-exchange/profile.json'


@pytest.mark.parametrize('suffix', ['?file_protocol=1','?file_protocol=02','?file_protocol=%32',
    '?FILE_PROTOCOL=2','?file_protocol=2&file_protocol=2','?file_protocol=2&token=owned-secret','?token=owned-secret'])
def test_other_query_variants_are_rejected_before_auth(tmp_path, suffix):
    async def check():
        async with running(tmp_path/'broker', auth_file(tmp_path)) as (broker, url):
            async with connect(url+suffix, proxy=None) as ws:
                with pytest.raises(ConnectionClosed):
                    await ws.send(json.dumps(hello('host')));await ws.recv()
            assert broker._db.execute('SELECT COUNT(*) FROM hosts').fetchone()[0] == 0
    asyncio.run(check())


@pytest.mark.parametrize('role', ['host','client'])
@pytest.mark.parametrize('marker', [None,1,'2',True,2.0])
def test_v2_rejects_old_or_malformed_hello_marker_before_host_pin(tmp_path, role, marker):
    async def check():
        async with running(tmp_path/'broker', auth_file(tmp_path)) as (broker, url):
            value=hello(role)
            if marker is None:value.pop('file_protocol')
            else:value['file_protocol']=marker
            async with connect(url, proxy=None) as ws:
                with pytest.raises(ConnectionClosed):await ws.send(json.dumps(value));await ws.recv()
            assert broker._db.execute('SELECT COUNT(*) FROM hosts').fetchone()[0] == 0
    asyncio.run(check())


def test_stripped_query_cannot_register_new_host_with_legacy_broker(tmp_path):
    async def check():
        # Faithful V1 HELLO allowlist. Deliberately no new-marker negotiation.
        class LegacyBroker(Broker):
            pins=0
            attempts=[]
            def _authenticate(self, value):
                self.attempts.append(value)
                if (not isinstance(value,dict) or value.get('protocol')!='sma-files-1'
                    or set(value)-{'id','protocol','role','room','token','host_id','host_secret','enrollment'}
                    or value.get('token')!=TOKEN or value.get('room')!='room'
                    or value.get('role') not in {'host','client'}):raise BrokerError('authentication_failed')
                if value['role']=='host':self._pin('owner','room',value)
                return 'owner','room',value['role']
            def _pin(self,*args):
                self.pins+=1;return super()._pin(*args)
            async def _send(self,connection,body):
                if body.get('type')=='ready':body={k:v for k,v in body.items() if k!='file_protocol'}
                return await super()._send(connection,body)
        broker=LegacyBroker(tmp_path/'legacy-broker',auth_file(tmp_path))
        seen=[]
        async def stripping_proxy(ws):
            seen.append(ws.request.path);ws.request.path='/file/ws'
            await broker.handle_socket(ws)
        project=tmp_path/'project';project.mkdir()
        owner=tmp_path/'owner';owner.write_text('owned-parent-token')
        token=tmp_path/'token';token.write_text(TOKEN)
        key=tmp_path/'key';key.write_text(KEY)
        state=tmp_path/'new-helper';state.mkdir()
        identity={'host_id':'1'*32,'host_secret':'2'*64}
        (state/'host.json').write_text(json.dumps(identity))
        parent=OldParent(project,owner.read_text())
        try:
            async with serve(stripping_proxy,'127.0.0.1',0) as server, serve(parent.handle,'127.0.0.1',0,process_request=parent.http) as backend:
                url=f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/file/ws'
                old_hello=hello('host',host_id=identity['host_id'],secret=identity['host_secret']);old_hello.pop('file_protocol',None)
                with broker._db:
                    broker._db.execute('INSERT INTO jobs(file_id,scope,room,session,window_id,daemon_epoch,revision,name,size,sha256,state,receipt,error,project_binding) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        ('f'*48,'owner','room','legacy-session','legacy-window','0'*64,0,'legacy.bin',0,'0'*64,'queued',None,None,None))
                original_job=tuple(broker._db.execute('SELECT * FROM jobs WHERE file_id=?',('f'*48,)).fetchone())
                old=await login(url,old_hello)
                viewer_hello=hello('client');viewer_hello.pop('file_protocol',None)
                old_viewer=await login(url,viewer_hello)
                helper=FileCompanion(f'http://127.0.0.1:{backend.sockets[0].getsockname()[1]}',url,'room',state,owner,key,broker_token_file=token)
                task=asyncio.create_task(helper.run())
                try:
                    await eventually(lambda:len(broker.attempts)>=3)
                    assert broker.pins==1 and old.state is State.OPEN
                    assert helper._status_state!='online'
                    assert seen[-1]=='/file/ws?file_protocol=2'
                    assert broker._db.execute('SELECT COUNT(*) FROM hosts').fetchone()[0]==1
                    assert tuple(broker._db.execute('SELECT * FROM jobs WHERE file_id=?',('f'*48,)).fetchone())==original_job
                    assert old_viewer.state is State.OPEN
                    assert set(parent.operations)<={1,10} and not parent.pty_written
                finally:
                    await helper.close();await asyncio.wait_for(task,5);await old.close();await old_viewer.close()
        finally:await broker.close()
    asyncio.run(check())


@pytest.mark.parametrize('marker', [None,1,'2',True])
def test_helper_does_not_accept_legacy_ready_even_after_socket_accepts_hello(tmp_path,marker):
    async def check():
        project=tmp_path/'project';project.mkdir()
        owner=tmp_path/'owner';owner.write_text('owned-parent-token')
        token=tmp_path/'token';token.write_text(TOKEN)
        key=tmp_path/'key';key.write_text(KEY)
        parent=OldParent(project,owner.read_text())
        closed,seen=asyncio.Event(),[]
        async def incompatible(ws):
            seen.append(ws.request.path)
            incoming=json.loads(await ws.recv())
            assert incoming['file_protocol']==2
            body={'type':'ready','role':'host','room':'room'}
            if marker is not None:body['file_protocol']=marker
            await ws.send(json.dumps(body));await ws.wait_closed();closed.set()
        async with serve(incompatible,'127.0.0.1',0) as server, serve(parent.handle,'127.0.0.1',0,process_request=parent.http) as backend:
            url=f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/file/ws'
            with pytest.raises(ValueError):_endpoint(url+'?file_protocol=2')
            helper=FileCompanion(f'http://127.0.0.1:{backend.sockets[0].getsockname()[1]}',url,'room',tmp_path/'helper',owner,key,broker_token_file=token)
            task=asyncio.create_task(helper.run())
            try:
                await eventually(lambda:closed.is_set() or helper._status_state=='online')
                assert helper._status_state!='online' and helper.broker_url==url
                assert seen and all(path=='/file/ws?file_protocol=2' for path in seen)
            finally:await helper.close();await asyncio.wait_for(task,5)
    asyncio.run(check())


@pytest.mark.parametrize('selectors', [[],['1'],['02'],['2','2'],['2, 2']])
def test_v2_http_rejects_missing_wrong_or_duplicate_selector_headers(tmp_path,selectors):
    async def check():
        async with running(tmp_path/'broker',auth_file(tmp_path)) as (broker,url):
            _,address=broker.start_http()
            host,viewer=await login(url,hello('host')),await login(url,hello())
            try:
                _,job=await stage(viewer,host)
                ticket=(await call(host,'download.ticket',{'file_id':job['file_id']}))['result']
                def fetch():
                    import http.client
                    http=http.client.HTTPConnection('127.0.0.1',address[1],timeout=2)
                    try:
                        http.putrequest('GET',ticket['download_path'])
                        http.putheader('Authorization','Bearer '+ticket['ticket'])
                        for value in selectors:http.putheader('X-SuperMono-File-Protocol',value)
                        http.endheaders();response=http.getresponse();response.read();return response.status
                    finally:http.close()
                assert await asyncio.to_thread(fetch)==401
                status,headers,payload=await asyncio.to_thread(http_fetch,f'http://127.0.0.1:{address[1]}',ticket['download_path'],ticket['ticket'])
                assert status==200 and headers['X-SuperMono-File-Protocol']=='2' and payload==b'fixture PDF bytes'
            finally:await viewer.close();await host.close()
    asyncio.run(check())
