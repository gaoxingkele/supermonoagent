import pytest
import asyncio
import copy
import json
import shutil
import subprocess
import threading
from unittest.mock import Mock
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed
from supermonoagent import console_viewport_rpc as R
from supermonoagent.console_viewport_rpc import encode_viewport, decode_viewport, ViewportRPCError
from supermonoagent.protocol import (CAPABILITIES, MAX_FRAME, OP_ASSISTANT_CONTEXT, OP_HELLO, OP_HELLO_OK,
    OP_PING, OP_PONG, pack, pack_json, unpack, unpack_json)
from test_console_viewport import frame
from test_client_integration import isolated_host, receive_op


@pytest.fixture(autouse=True)
def fake_supported_platform(monkeypatch):
    # Native reads in this module are always test doubles; support emulation
    # must not alter global sys.platform or call any OS console API.
    monkeypatch.setattr(R,'_windows_supported',lambda:True)


def expected_preview(raw,wire):
    return {**raw,'created':str(raw['created']),'kind':'console_viewport_preview','stream_checkpoint':False,'captured_at':wire['captured_at']}


@pytest.mark.parametrize('created',[1,133999999999999999,0xFFFFFFFFFFFFFFFF])
def test_filetime_survives_real_javascript_json_roundtrip(created):
    node=shutil.which('node')
    if node is None:pytest.skip('Node is required for the browser JSON precision regression')
    raw=frame();raw['created']=created
    before=copy.deepcopy(raw)
    wire=encode_viewport(raw)
    result=subprocess.run([node,'-e',
        "let s='';process.stdin.setEncoding('utf8');process.stdin.on('data',c=>s+=c);"
        "process.stdin.on('end',()=>process.stdout.write(JSON.stringify(JSON.parse(s))));"],
        input=json.dumps(wire),text=True,capture_output=True,check=True,timeout=5)
    browser=json.loads(result.stdout)
    assert browser['created']==str(created)
    assert decode_viewport(browser)==expected_preview(raw,browser)
    assert raw==before


@pytest.mark.parametrize('created',[0,1,True,None,1.0,'','0','00','01','-1','+1',' 1','1 ',
    '1.0','1e3','\u0661','18446744073709551616','9'*1000])
def test_wire_filetime_requires_canonical_positive_uint64_decimal_string(created):
    wire=encode_viewport(frame());wire['created']=created
    with pytest.raises(ViewportRPCError,match='invalid_payload'):decode_viewport(wire)


def test_codec_preserves_raw_uint16_and_lvb_without_changing_input():
    data=frame()
    before=copy.deepcopy(data)
    encoded=encode_viewport(data)
    assert encoded['encoding']=='base64-u16le'
    assert encoded['kind']=='console_viewport_preview' and encoded['stream_checkpoint'] is False
    assert type(encoded['captured_at']) is int and encoded['captured_at']>0
    assert 'code_units' not in encoded and 'attributes' not in encoded
    decoded=decode_viewport(encoded)
    assert decoded==expected_preview(data,encoded)
    assert not R.console_snapshot.validate_console_viewport(decoded)
    assert data==before


def payload(sid,request='owned-request',**extra):
    return json.dumps({'request':request,'action':R.ACTION,'session':sid,**extra}).encode()


def attach(daemon, *, native=True):
    window=daemon.hub.open_window(owner=daemon.token)
    session=daemon.hub.open_cli(window.id,'cmd')
    session.pty.write=Mock(wraps=session.pty.write)
    session.pty.resize=Mock(wraps=session.pty.resize)
    if native:
        session.pty._footer_pid=101;session.pty._footer_identity=(11,12345)
    client=daemon.hub.attach(asyncio.Queue(),token=daemon.token)
    client.multiplex=True;client.subscriptions={session.id}
    return session,client


def reply(frames):
    assert len(frames)==1 and unpack(frames[0])[0]==OP_ASSISTANT_CONTEXT
    return unpack_json(unpack(frames[0])[1])


def test_maximum_grid_binary_pair_fits_single_existing_protocol_frame():
    data=frame();data.update(created=0xFFFFFFFFFFFFFFFF,buffer_size=[500,80],viewport=[0,0,499,79],width=500,height=80,
        cursor_buffer=[0,0],cursor=[0,0],code_units=[[65535]*500 for _ in range(80)],
        attributes=[[65535]*500 for _ in range(80)])
    wire=encode_viewport(data)
    message=pack_json(OP_ASSISTANT_CONTEXT,{'request':'r'*64,'session':'s'*256,'action':R.ACTION,'ok':True,'viewport':wire})
    assert len(message)<215000<MAX_FRAME
    assert decode_viewport(wire)==expected_preview(data,wire)
    data['code_units'][0].append(1)
    with pytest.raises(ViewportRPCError):encode_viewport(data)


@pytest.mark.parametrize('change',['extra','encoding','truncated','whitespace','word_count','bad_metadata','kind','checkpoint','timestamp'])
def test_strict_binary_codec_rejects_injection_and_malformed_payload(change):
    data=encode_viewport(frame())
    if change=='extra':data['pid_from_browser']=1
    elif change=='encoding':data['encoding']='native-endian'
    elif change=='truncated':data['code_units_le']=data['code_units_le'][:-1]
    elif change=='whitespace':data['attributes_le']=' '+data['attributes_le'][1:]
    elif change=='word_count':data['width']=5
    elif change=='bad_metadata':data['cursor_buffer']=[10000,0]
    elif change=='kind':data['kind']='console_viewport'
    elif change=='checkpoint':data['stream_checkpoint']=True
    else:data['captured_at']=True
    with pytest.raises(ViewportRPCError,match='invalid_payload'):decode_viewport(data)


@pytest.mark.parametrize('case',['foreign','unknown','pid','extra','duplicate','delegated','unsupported','channel'])
def test_rejections_never_reach_os_reader(monkeypatch,case):
    read=Mock(return_value=frame());monkeypatch.setattr(R.console_snapshot,'read_console_viewport',read)
    async def scenario():
        async with isolated_host() as (daemon,_,_):
            session,client=attach(daemon,native=case!='unsupported')
            request=payload(session.id)
            if case=='foreign':
                group=daemon.hub.groups[daemon.hub.windows[session.window_id].group_id];group.owner='different-owner'
            elif case=='unknown':request=payload('unknown-session')
            elif case=='pid':request=payload(session.id,pid=999999)
            elif case=='extra':request=payload(session.id,path='private-path')
            elif case=='duplicate':request=request[:-1]+b',"request":"duplicate"}'
            elif case=='delegated':client.delegated=True
            elif case=='channel':
                daemon.hub.clients.pop(client.key);client.key='relay-owned-fixture';daemon.hub.clients[client.key]=client
                client.channel='wrong';daemon.hub.groups[daemon.hub.windows[session.window_id].group_id].channel='allowed'
            frames,code=await daemon._op(client,OP_ASSISTANT_CONTEXT,request)
            if case=='delegated':assert code==4001 and not frames
            else:
                value=reply(frames);assert not value['ok'] and 'viewport' not in value
                assert value['error_code'] in ('unauthorized','invalid_request','unsupported')
            read.assert_not_called();session.pty.write.assert_not_called();session.pty.resize.assert_not_called()
    asyncio.run(scenario())


@pytest.mark.parametrize('race',['token','peer','removed','pty','pid','identity','dimensions','unsubscribed','disconnected'])
def test_late_scope_change_discards_native_cells(monkeypatch,race):
    started,release=threading.Event(),threading.Event()
    def read(*args,**kwargs):started.set();assert release.wait(3);return frame()
    monkeypatch.setattr(R.console_snapshot,'read_console_viewport',read)
    async def scenario():
        async with isolated_host() as (daemon,_,_):
            session,client=attach(daemon);original=session.pty;peer=[True];client.authorize=lambda:peer[0]
            job=asyncio.create_task(daemon._op(client,OP_ASSISTANT_CONTEXT,payload(session.id)))
            try:
                assert await asyncio.to_thread(started.wait,1)
                if race=='token':client.token='revoked'
                elif race=='peer':peer[0]=False
                elif race=='removed':daemon.hub.sessions.pop(session.id)
                elif race=='pty':session.pty=object()
                elif race=='pid':session.pty._footer_pid=202
                elif race=='identity':session.pty._footer_identity=(11,54321)
                elif race=='dimensions':session.cols+=1
                elif race=='unsubscribed':client.subscriptions.clear()
                else:daemon.hub.detach(client.key)
                release.set();frames,code=await job
                if code:assert code==4001 and frames==[]
                else:assert reply(frames)['error_code']=='target_changed' and 'viewport' not in reply(frames)
                original.write.assert_not_called();original.resize.assert_not_called()
            finally:
                release.set();session.pty=original;daemon.hub.sessions[session.id]=session
    asyncio.run(scenario())


def test_success_is_explicit_data_only_and_never_resizes_or_writes(monkeypatch):
    read=Mock(return_value=frame());monkeypatch.setattr(R.console_snapshot,'read_console_viewport',read)
    async def scenario():
        async with isolated_host() as (daemon,_,_):
            session,client=attach(daemon)
            read.assert_not_called()
            frames,code=await daemon._op(client,OP_ASSISTANT_CONTEXT,payload(session.id))
            value=reply(frames);assert code is None and value['ok']
            assert decode_viewport(value['viewport'])==expected_preview(frame(),value['viewport'])
            read.assert_called_once_with(101,12345,11,timeout=2)
            session.pty.write.assert_not_called();session.pty.resize.assert_not_called()
    asyncio.run(scenario())


def test_counter_output_generation_change_can_still_return_explicit_preview(monkeypatch):
    async def scenario():
        async with isolated_host() as (daemon,_,_):
            session,client=attach(daemon)
            def read(*args,**kwargs):session.output_generation+=1;return frame()
            monkeypatch.setattr(R.console_snapshot,'read_console_viewport',read)
            result=reply((await daemon._op(client,OP_ASSISTANT_CONTEXT,payload(session.id)))[0])
            assert result['ok'] and result['viewport']['kind']=='console_viewport_preview'
            assert result['viewport']['stream_checkpoint'] is False
            assert decode_viewport(result['viewport'])==expected_preview(frame(),result['viewport'])
    asyncio.run(scenario())


@pytest.mark.parametrize('boundary',['cancel','timeout'])
def test_actual_reader_slot_survives_request_cancellation_or_timeout(monkeypatch,boundary):
    started,release=threading.Event(),threading.Event()
    def read(*args,**kwargs):started.set();assert release.wait(3);return frame()
    monkeypatch.setattr(R.console_snapshot,'read_console_viewport',read)
    monkeypatch.setattr(R,'READ_TIMEOUT',.04 if boundary=='timeout' else 2.5)
    async def scenario():
        async with isolated_host() as (daemon,_,_):
            session,client=attach(daemon)
            job=asyncio.create_task(daemon._op(client,OP_ASSISTANT_CONTEXT,payload(session.id)))
            try:
                assert await asyncio.to_thread(started.wait,1)
                if boundary=='cancel':
                    job.cancel()
                    with pytest.raises(asyncio.CancelledError):await job
                else:assert reply((await job)[0])['error_code']=='timeout'
                assert len(daemon._footer_jobs)==1
                actual=list(daemon._footer_jobs);release.set()
                await asyncio.gather(*actual);await asyncio.sleep(0)
                assert not daemon._footer_jobs
            finally:release.set()
    asyncio.run(scenario())


def test_sixteen_requests_share_four_real_reader_slots_and_footer_budget(monkeypatch):
    release=threading.Event();started=threading.Event();lock=threading.Lock();active=[0];peak=[0]
    def read(*args,**kwargs):
        with lock:
            active[0]+=1;peak[0]=max(peak[0],active[0]);started.set()
        try:assert release.wait(3);return frame()
        finally:
            with lock:active[0]-=1
    monkeypatch.setattr(R.console_snapshot,'read_console_viewport',read)
    async def scenario():
        async with isolated_host() as (daemon,_,_):
            session,owner=attach(daemon);clients=[owner]
            for _ in range(15):
                client=daemon.hub.attach(asyncio.Queue(),token=daemon.token);client.multiplex=True;client.subscriptions={session.id};clients.append(client)
            jobs=[asyncio.create_task(daemon._op(client,OP_ASSISTANT_CONTEXT,payload(session.id,str(index)))) for index,client in enumerate(clients)]
            try:
                assert await asyncio.to_thread(started.wait,1)
                await asyncio.sleep(.03)
                assert len(daemon._footer_jobs)==4 and peak[0]<=4
                footer=Mock(return_value=b'footer');session.pty.footer_patch=footer
                from supermonoagent.protocol import pack_feed,OP_SNAPSHOT
                assert await daemon.snapshot_supplement(owner,pack_feed(OP_SNAPSHOT,session.id,b'')) is None
                footer.assert_not_called()
                release.set();results=[reply((await job)[0]) for job in jobs]
                assert sum(result['ok'] for result in results)==4
                assert sum(result.get('error_code')=='busy' for result in results)==12
                assert peak[0]==4
            finally:release.set();await asyncio.gather(*jobs,return_exceptions=True)
    asyncio.run(scenario())


def test_platform_and_reserved_footer_slots_reject_without_read(monkeypatch):
    read=Mock(return_value=frame());monkeypatch.setattr(R.console_snapshot,'read_console_viewport',read)
    async def scenario():
        async with isolated_host() as (daemon,_,_):
            session,client=attach(daemon)
            monkeypatch.setattr(R,'_windows_supported',lambda:False)
            assert reply((await daemon._op(client,OP_ASSISTANT_CONTEXT,payload(session.id)))[0])['error_code']=='unsupported'
            monkeypatch.setattr(R,'_windows_supported',lambda:True)
            daemon._footer_starting.update(('owned-reservation',i) for i in range(4))
            assert reply((await daemon._op(client,OP_ASSISTANT_CONTEXT,payload(session.id)))[0])['error_code']=='busy'
            read.assert_not_called();daemon._footer_starting.clear()
    asyncio.run(scenario())


def test_running_footer_reads_and_preview_reads_share_exactly_four_slots(monkeypatch):
    release=threading.Event();lock=threading.Lock();counts={'active':0,'footer':0,'preview':0,'peak':0}
    def enter(kind):
        with lock:
            counts[kind]+=1;counts['active']+=1;counts['peak']=max(counts['peak'],counts['active'])
        try:assert release.wait(3)
        finally:
            with lock:counts['active']-=1
    def read(*args,**kwargs):enter('preview');return frame()
    monkeypatch.setattr(R.console_snapshot,'read_console_viewport',read)
    async def scenario():
        from supermonoagent.protocol import pack_feed,OP_SNAPSHOT
        async with isolated_host() as (daemon,_,_):
            session,owner=attach(daemon)
            def footer(*args):enter('footer');return b'owned footer'
            session.pty.footer_patch=footer
            footer_jobs=[asyncio.create_task(daemon.snapshot_supplement(owner,pack_feed(OP_SNAPSHOT,session.id,b''))) for _ in range(2)]
            async with asyncio.timeout(1):
                while counts['footer']<2:await asyncio.sleep(0)
            clients=[]
            for _ in range(16):
                client=daemon.hub.attach(asyncio.Queue(),token=daemon.token);client.multiplex=True;client.subscriptions={session.id};clients.append(client)
            requests=[asyncio.create_task(daemon._op(client,OP_ASSISTANT_CONTEXT,payload(session.id,str(i)))) for i,client in enumerate(clients)]
            try:
                async with asyncio.timeout(1):
                    while counts['preview']<2:await asyncio.sleep(0)
                assert len(daemon._footer_jobs)==4
                release.set();results=[reply((await job)[0]) for job in requests]
                assert sum(result['ok'] for result in results)==2 and counts['peak']==4
                assert sum(result.get('error_code')=='busy' for result in results)==14
            finally:release.set();await asyncio.gather(*requests,*footer_jobs,return_exceptions=True)
    asyncio.run(scenario())


def test_loopback_heartbeat_and_close_stay_responsive_during_native_read(monkeypatch):
    started,release=threading.Event(),threading.Event()
    def read(*args,**kwargs):started.set();assert release.wait(3);return frame()
    monkeypatch.setattr(R.console_snapshot,'read_console_viewport',read)
    async def scenario():
        from supermonoagent.protocol import OP_SUBSCRIBE
        async with isolated_host() as (daemon,direct,_):
            session,_=attach(daemon)
            async with connect(direct,additional_headers={'Authorization':'Bearer '+daemon.token},compression=None) as ws:
                await ws.send(pack_json(OP_HELLO,{'token':daemon.token,'multiplex':True,'reliable':True}))
                hello=unpack_json(await receive_op(ws,OP_HELLO_OK))
                assert hello['capabilities']['console_viewport']==CAPABILITIES['console_viewport']
                await ws.send(pack_json(OP_SUBSCRIBE,{'sessions':[session.id]}))
                await ws.send(pack(OP_PING,b'before-preview'));assert await receive_op(ws,OP_PONG)==b'before-preview'
                await ws.send(pack(OP_ASSISTANT_CONTEXT,payload(session.id)))
                try:
                    assert await asyncio.to_thread(started.wait,1)
                    await ws.send(pack(OP_PING,b'during-preview'))
                    assert await receive_op(ws,OP_PONG,timeout=1)==b'during-preview'
                    assert not release.is_set() and len(daemon._footer_jobs)==1
                    await ws.close()
                    async with asyncio.timeout(1):
                        while daemon._sidecar_workers:await asyncio.sleep(0)
                    assert len(daemon._footer_jobs)==1
                    actual=list(daemon._footer_jobs);release.set();await asyncio.gather(*actual);await asyncio.sleep(0)
                    assert not daemon._footer_jobs
                    session.pty.write.assert_not_called();session.pty.resize.assert_not_called()
                finally:release.set()
    asyncio.run(scenario())


def test_wrong_token_loopback_never_starts_a_reader(monkeypatch):
    read=Mock();monkeypatch.setattr(R.console_snapshot,'read_console_viewport',read)
    async def scenario():
        async with isolated_host() as (daemon,direct,_):
            async with connect(direct,additional_headers={'Authorization':'Bearer owned-wrong-token'}) as ws:
                with pytest.raises(ConnectionClosed) as error:
                    await ws.send(pack_json(OP_HELLO,{'token':'owned-wrong-token','multiplex':True}))
                    await ws.recv()
                assert error.value.rcvd.code==4001
            read.assert_not_called()
    asyncio.run(scenario())


def test_old_actionless_context_rpc_retains_its_existing_behavior(monkeypatch):
    import supermonoagent.assistant_context as context
    monkeypatch.setattr(context,'read_project_context',lambda *args:{'cwd':'owned-context'})
    read=Mock();monkeypatch.setattr(R.console_snapshot,'read_console_viewport',read)
    async def scenario():
        async with isolated_host() as (daemon,_,_):
            session,client=attach(daemon)
            value=reply((await daemon._op(client,OP_ASSISTANT_CONTEXT,json.dumps({'request':'old-client','session':session.id}).encode()))[0])
            assert value=={'request':'old-client','ok':True,'cwd':'owned-context'}
            read.assert_not_called()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure',['exception','unexpected_code','bad_grid','oversize','wrong_pid','request_size'])
def test_capture_errors_are_fixed_and_never_disclose_raw_cells_or_credentials(monkeypatch,failure):
    secret='DO_NOT_RETURN_NATIVE_OR_TOKEN'
    data=frame()
    if failure=='bad_grid':data['code_units']=[[secret]]
    if failure=='wrong_pid':data['pid']=202
    read=Mock(side_effect=OSError(secret)) if failure=='exception' else Mock(
        side_effect=ViewportRPCError(secret)) if failure=='unexpected_code' else Mock(return_value=data)
    monkeypatch.setattr(R.console_snapshot,'read_console_viewport',read)
    if failure=='oversize':monkeypatch.setattr(R,'MAX_FRAME',512)
    async def scenario():
        async with isolated_host() as (daemon,_,_):
            session,client=attach(daemon)
            request=payload(session.id)+(b' '*2048 if failure=='request_size' else b'')
            frames,code=await daemon._op(client,OP_ASSISTANT_CONTEXT,request)
            result=reply(frames)
            assert not result['ok'] and 'viewport' not in result and secret not in str(result)
            assert result['error_code'] in ('unavailable','invalid_payload','too_large','target_changed','invalid_request')
            if failure=='request_size':read.assert_not_called()
    asyncio.run(scenario())


def test_sixteen_loopback_requests_reach_only_four_actual_threads(monkeypatch):
    from contextlib import AsyncExitStack
    from supermonoagent.protocol import OP_SUBSCRIBE
    gate=threading.Event();lock=threading.Lock();counts={'active':0,'peak':0}
    def read(*args,**kwargs):
        with lock:counts['active']+=1;counts['peak']=max(counts['peak'],counts['active'])
        try:assert gate.wait(4);return frame()
        finally:
            with lock:counts['active']-=1
    monkeypatch.setattr(R.console_snapshot,'read_console_viewport',read)
    async def scenario():
        async with isolated_host() as (daemon,direct,_):
            session,_=attach(daemon)
            async with AsyncExitStack() as stack:
                sockets=[]
                for _ in range(16):
                    ws=await stack.enter_async_context(connect(direct,additional_headers={'Authorization':'Bearer '+daemon.token},compression=None))
                    await ws.send(pack_json(OP_HELLO,{'token':daemon.token,'multiplex':True,'reliable':True}))
                    await receive_op(ws,OP_HELLO_OK)
                    await ws.send(pack_json(OP_SUBSCRIBE,{'sessions':[session.id]}))
                    await ws.send(pack(OP_PING,b'ready'));assert await receive_op(ws,OP_PONG)==b'ready'
                    sockets.append(ws)
                async with asyncio.timeout(1):
                    while daemon._footer_starting or daemon._footer_tasks:await asyncio.sleep(0)
                pending=[]
                try:
                    for i,ws in enumerate(sockets):
                        await ws.send(pack(OP_ASSISTANT_CONTEXT,payload(session.id,str(i))))
                        pending.append(asyncio.create_task(receive_op(ws,OP_ASSISTANT_CONTEXT)))
                    async with asyncio.timeout(1):
                        while len(daemon._footer_jobs)<4:await asyncio.sleep(0)
                    done,_=await asyncio.wait(pending,timeout=.05)
                    assert len(done)==12 and counts['peak']==4
                    assert all(unpack_json(task.result())['error_code']=='busy' for task in done)
                    gate.set();results=[unpack_json(await task) for task in pending]
                    assert sum(result['ok'] for result in results)==4
                    assert counts['peak']==4
                    session.pty.write.assert_not_called();session.pty.resize.assert_not_called()
                finally:gate.set();await asyncio.gather(*pending,return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize('change',['cancel','revoke','disconnect'])
def test_executor_wait_rechecks_scope_before_any_os_read(monkeypatch,change):
    waiting,release,finished=threading.Event(),threading.Event(),threading.Event()
    original=R._capture
    def queued(*args):
        waiting.set()
        try:
            assert release.wait(3)
            return original(*args)
        finally:finished.set()
    monkeypatch.setattr(R,'_capture',queued)
    read=Mock(return_value=frame());monkeypatch.setattr(R.console_snapshot,'read_console_viewport',read)
    async def scenario():
        async with isolated_host() as (daemon,_,_):
            session,client=attach(daemon)
            job=asyncio.create_task(daemon._op(client,OP_ASSISTANT_CONTEXT,payload(session.id)))
            try:
                assert await asyncio.to_thread(waiting.wait,1)
                actual=list(daemon._footer_jobs)
                if change=='cancel':job.cancel()
                elif change=='revoke':client.token='revoked'
                else:daemon.hub.detach(client.key)
                release.set()
                if change=='cancel':
                    # Deliberately hold the event-loop turn after cancel(). A
                    # queued worker must see the cancellation request before
                    # CancelledError delivery can set the threading Event.
                    assert finished.wait(1)
                    with pytest.raises(asyncio.CancelledError):await job
                else:
                    frames,code=await job
                    assert code==4001 or reply(frames)['error_code']=='target_changed'
                await asyncio.gather(*actual,return_exceptions=True)
                read.assert_not_called()
            finally:release.set()
    asyncio.run(scenario())
