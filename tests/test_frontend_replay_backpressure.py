"""Frozen legacy semantics fixture: no imports of the changing Hub or Daemon."""
import asyncio
from contextlib import asynccontextmanager
import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed
from supermonoagent import frontend as F
from supermonoagent.protocol import (pack, pack_json, unpack, unpack_json, OP_HELLO, OP_HELLO_OK,
    OP_SUBSCRIBE, OP_SNAPSHOT, OP_OUTPUT, OP_PING, OP_PONG, OP_SELECT, OP_FOCUS, OP_RESIZE)

TOKEN='owned-legacy-replay-token'


class Legacy:
    def __init__(self, count=16, *, pong=True):
        self.sessions=[f'sid-{i:02}' for i in range(count)]
        self.present=set(self.sessions);self.subscriptions=set();self.leases={}
        self.focused={f'window-{i:02}':sid for i,sid in enumerate(self.sessions)}
        self.mapping={sid:window for window,sid in self.focused.items()}
        self.window='window-00';self.session='sid-00';self.frames=[];self.pong=pong
        self.closed=asyncio.Event();self.overflow=False
        self.replay_live=True
        self.cols=80
    def state(self):
        return {'windows':[{'id':win,'focused':sid,'sessions':[{'id':item} for item in self.sessions if item in self.present and self.mapping.get(item)==win]} for win,sid in self.focused.items() if sid in self.present],
            'controls':{sid:{'cols':self.cols,'rows':24} for sid in self.subscriptions if sid in self.present},
            'selected':self.window,'session':self.session,
            'capabilities':{'conpty_paste':True,'window_themes':True},'service':{'owner_authenticated':True}}
    async def handler(self, ws):
        queue=asyncio.Queue(maxsize=8)
        def emit(frame):
            try:queue.put_nowait(frame)
            except asyncio.QueueFull:
                self.overflow=True
                asyncio.create_task(ws.close(code=1013,reason='Legacy queue full'))
        def replay(sid):
            emit(pack(OP_SNAPSHOT,sid.encode()+b'\0replay:'+sid.encode()+b'\x1b[31m'))
            if self.replay_live:emit(pack(OP_OUTPUT,sid.encode()+b'\0footer:'+sid.encode()))
        self.emit=emit
        async def pump():
            while True:
                frame=await queue.get()
                await asyncio.sleep(.001)
                await ws.send(frame)
        task=asyncio.create_task(pump())
        try:
            first=await ws.recv();op,payload=unpack(first);body=unpack_json(payload)
            if op!=OP_HELLO or body.get('token')!=TOKEN or ws.request.headers.get('Authorization')!='Bearer '+TOKEN:
                await ws.close(code=4001,reason='Rejected');return
            emit(pack_json(OP_HELLO_OK,self.state()))
            async for frame in ws:
                op,payload=unpack(frame);self.frames.append((op,payload))
                if op==OP_PING:
                    if self.pong:emit(pack(OP_PONG,payload))
                elif op==OP_SUBSCRIBE:
                    body=unpack_json(payload);selected=set(body.get('sessions',[]))&self.present
                    removed=self.subscriptions-selected
                    for sid in removed:self.leases.pop(sid,None)
                    added=selected-self.subscriptions;self.subscriptions=selected
                    for sid in sorted(selected if body.get('refresh') else added):replay(sid)
                    emit(pack_json(OP_HELLO_OK,self.state()))
                elif op==OP_SELECT:
                    window=unpack_json(payload).get('window');sid=self.focused.get(window)
                    if sid in self.present:self.window,self.session=window,sid;replay(sid)
                    emit(pack_json(OP_HELLO_OK,self.state()))
                elif op==OP_FOCUS:
                    sid=unpack_json(payload).get('session')
                    if sid in self.present and self.mapping.get(sid)==self.window:self.session=sid;replay(sid)
                    emit(pack_json(OP_HELLO_OK,self.state()))
                elif op==OP_RESIZE:
                    sid=unpack_json(payload).get('session')
                    if sid in self.subscriptions:self.leases[sid]='viewer'
        except ConnectionClosed:pass
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True);self.closed.set()


@asynccontextmanager
async def setup(tmp_path, *, pong=True):
    old=Legacy(pong=pong)
    async with serve(old.handler,'127.0.0.1',0,compression=None) as backend:
        proxy=F.Frontend(f'http://127.0.0.1:{backend.sockets[0].getsockname()[1]}',tmp_path/'themes.json')
        proxy.footer_recovery=type('NoFooter',(),{'supplement':lambda *args:None})()
        async with serve(proxy.handler,'127.0.0.1',0,compression=None) as server:
            try:
                async with connect(f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ws',compression=None) as client:
                    yield old,proxy,client
            finally:await proxy.close_updates()


async def hello(client, token=TOKEN):
    await client.send(pack_json(OP_HELLO,{'token':token,'multiplex':True,'reliable':True}))
    return await asyncio.wait_for(client.recv(),2)


async def barrier(client, marker=b'owned-client-marker'):
    await client.send(pack(OP_PING,marker));frames=[]
    while True:
        frame=await asyncio.wait_for(client.recv(),3);op,payload=unpack(frame)
        if op==OP_PONG:
            assert payload==marker,'Private PONG leaked downstream'
            return frames
        frames.append(frame)


def test_updated_host_handles_its_own_replay_without_legacy_navigation(tmp_path):
    async def scenario():
        async with setup(tmp_path) as (host, _proxy, client):
            original = host.state
            host.state = lambda: {**original(), 'capabilities': {
                'conpty_paste': True, 'window_themes': True, 'replay_backpressure': True}}
            await hello(client)
            selected = [host.sessions[0]]
            await client.send(pack_json(OP_SUBSCRIBE, {'sessions': selected}))
            await barrier(client)
            await client.send(pack_json(OP_SUBSCRIBE, {'sessions': selected, 'refresh': True}))
            await barrier(client)
            assert [op for op, _ in host.frames] == [OP_SUBSCRIBE, OP_PING, OP_SUBSCRIBE, OP_PING]
            assert unpack_json(host.frames[2][1]) == {'sessions': selected, 'refresh': True}
    asyncio.run(scenario())


def test_sixteen_legacy_windows_initial_and_refresh_preserve_view_leases_and_bytes(tmp_path):
    async def scenario():
        async with setup(tmp_path) as (old,proxy,client):
            initial=unpack_json(unpack(await hello(client))[1]);view=(initial['selected'],initial['session'])
            await client.send(pack_json(OP_SUBSCRIBE,{'sessions':old.sessions}))
            frames=await barrier(client)
            assert sum(unpack(f)[0]==OP_SNAPSHOT for f in frames)==16
            assert [f for f in frames if unpack(f)[0] in (OP_SNAPSHOT,OP_OUTPUT)] == [
                frame for sid in old.sessions for frame in (
                    pack(OP_SNAPSHOT,sid.encode()+b'\0replay:'+sid.encode()+b'\x1b[31m'),
                    pack(OP_OUTPUT,sid.encode()+b'\0footer:'+sid.encode()))]
            for sid in old.sessions:await client.send(pack_json(OP_RESIZE,{'session':sid,'cols':80,'rows':24}))
            await barrier(client)
            leases=dict(old.leases);focused=dict(old.focused)
            await client.send(pack_json(OP_SUBSCRIBE,{'sessions':old.sessions,'refresh':True}))
            frames=await barrier(client)
            assert not old.overflow and old.leases==leases and old.focused==focused
            for frame in frames:
                op,payload=unpack(frame)
                if op==OP_HELLO_OK:
                    state=unpack_json(payload);assert (state['selected'],state['session'])==view
            snapshots=[unpack(f)[1] for f in frames if unpack(f)[0]==OP_SNAPSHOT]
            for sid in old.sessions:assert sid.encode()+b'\0replay:'+sid.encode()+b'\x1b[31m' in snapshots
            assert all(not unpack_json(payload).get('refresh') for op,payload in old.frames if op==OP_SUBSCRIBE)
    asyncio.run(scenario())


def test_rapid_add_remove_refresh_skips_disappeared_and_foreign_sessions(tmp_path):
    async def scenario():
        async with setup(tmp_path) as (old,proxy,client):
            await hello(client)
            for selected in (old.sessions[:8], old.sessions, old.sessions[1::2]):
                await client.send(pack_json(OP_SUBSCRIBE,{'sessions':selected,'refresh':True}))
            await barrier(client)
            assert old.subscriptions==set(old.sessions[1::2]) and not old.overflow
            for sid in old.subscriptions:await client.send(pack_json(OP_RESIZE,{'session':sid}))
            await barrier(client)
            leases=dict(old.leases)
            missing=old.sessions[-1];old.present.remove(missing)
            old.emit(pack_json(OP_HELLO_OK,old.state()))
            await barrier(client)
            await client.send(pack_json(OP_SUBSCRIBE,{'sessions':old.sessions[1::2]+['foreign-secret-sid'],'refresh':True}))
            frames=await barrier(client)
            assert old.subscriptions==set(old.sessions[1::2])-{missing}
            assert all(old.leases.get(sid)==value for sid,value in leases.items() if sid!=missing)
            assert not old.overflow
            for op,payload in old.frames:
                if op in (OP_FOCUS,OP_SUBSCRIBE):
                    assert 'foreign-secret-sid' not in str(unpack_json(payload))
            assert not any(unpack(frame)[1].startswith(missing.encode()+b'\0') for frame in frames if unpack(frame)[0] in (OP_OUTPUT,OP_SNAPSHOT))
    asyncio.run(scenario())


@pytest.mark.parametrize('boundary',['close','timeout'])
def test_missing_private_pong_has_bounded_timeout_and_client_close_cancels(tmp_path, monkeypatch, boundary):
    monkeypatch.setattr(F,'REPLAY_DRAIN_TIMEOUT',.12 if boundary=='timeout' else 10)
    async def scenario():
        async with setup(tmp_path,pong=False) as (old,proxy,client):
            await hello(client)
            await client.send(pack_json(OP_SUBSCRIBE,{'sessions':old.sessions}))
            while not any(op==OP_PING for op,_ in old.frames):await asyncio.sleep(0)
            if boundary=='close':await client.close()
            else:
                with pytest.raises(ConnectionClosed) as error:
                    while True:await asyncio.wait_for(client.recv(),1)
                assert error.value.rcvd.code==1011
            await asyncio.wait_for(old.closed.wait(),1)
            assert len([1 for op,_ in old.frames if op==OP_SUBSCRIBE])==1
    asyncio.run(scenario())


def test_wrong_token_is_still_rejected_before_replay(tmp_path):
    async def scenario():
        async with setup(tmp_path) as (old,proxy,client):
            with pytest.raises(ConnectionClosed) as error:await hello(client,'wrong-owned-token')
            assert error.value.rcvd.code==4001
            assert old.frames==[]
    asyncio.run(scenario())


def test_slow_footer_does_not_hold_private_barriers_or_live_bytes(tmp_path, monkeypatch):
    import threading
    monkeypatch.setattr(F,'FOOTER_RECOVERY_TIMEOUT',.06)
    async def scenario():
        entered,release=threading.Event(),threading.Event()
        async with setup(tmp_path) as (old,proxy,client):
            old.replay_live=False
            def supplement(frame,state):
                entered.set();assert release.wait(2)
                return pack(OP_OUTPUT,b'sid-00\0stale footer')
            proxy.footer_recovery=type('Reader',(),{'supplement':staticmethod(supplement)})()
            try:
                await hello(client)
                await client.send(pack_json(OP_SUBSCRIBE,{'sessions':old.sessions}))
                frames=await barrier(client)
                # Legacy static output follows each snapshot and is never held
                # by an optional reader, even if a read is already underway.
                assert await asyncio.to_thread(entered.wait,1)
                assert sum(unpack(f)[0]==OP_SNAPSHOT for f in frames)==16
                live=pack(OP_OUTPUT,b'sid-00\0current live bytes')
                old.emit(live)
                assert live in await barrier(client)
                assert not old.overflow
                assert len(proxy._footer_reads)<=2
            finally:release.set()
    asyncio.run(scenario())


@pytest.mark.parametrize('change',['grid','removed'])
def test_pending_footer_is_discarded_after_canonical_target_changes(tmp_path, change):
    import threading
    async def scenario():
        entered,release=threading.Event(),threading.Event()
        async with setup(tmp_path) as (old,proxy,client):
            old.replay_live=False
            def supplement(frame,state):
                entered.set();assert release.wait(2)
                return pack(OP_OUTPUT,b'sid-00\0old canonical footer')
            proxy.footer_recovery=type('Reader',(),{'supplement':staticmethod(supplement)})()
            try:
                await hello(client)
                await client.send(pack_json(OP_SUBSCRIBE,{'sessions':['sid-00']}))
                await barrier(client)
                assert await asyncio.to_thread(entered.wait,1)
                if change=='grid':old.cols=100
                else:old.present.remove('sid-00')
                old.emit(pack_json(OP_HELLO_OK,old.state()))
                await barrier(client)
                release.set()
                with pytest.raises(asyncio.TimeoutError):await asyncio.wait_for(client.recv(),.08)
            finally:release.set()
    asyncio.run(scenario())


def test_interleaved_refresh_returns_to_window_before_focusing_its_second_session(tmp_path):
    async def scenario():
        async with setup(tmp_path) as (old,proxy,client):
            old.sessions=['sid-00','sid-01','sid-A2'];old.present.add('sid-A2');old.mapping['sid-A2']='window-00'
            await hello(client)
            await client.send(pack_json(OP_SUBSCRIBE,{'sessions':old.sessions}))
            await barrier(client)
            for sid in old.sessions:await client.send(pack_json(OP_RESIZE,{'session':sid}))
            await barrier(client)
            leases,focused=dict(old.leases),dict(old.focused)
            await client.send(pack_json(OP_SUBSCRIBE,{'sessions':old.sessions,'refresh':True}))
            frames=await barrier(client)
            snapshots=[unpack(frame)[1] for frame in frames if unpack(frame)[0]==OP_SNAPSHOT]
            assert b'sid-A2\0replay:sid-A2\x1b[31m' in snapshots
            assert old.leases==leases and old.focused==focused and not old.overflow
    asyncio.run(scenario())
