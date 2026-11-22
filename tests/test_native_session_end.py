"""OP9 on test-owned PowerShell processes: observe exact process objects and both catalogs."""
import asyncio
import base64
import json
import os
import shutil
import threading
from contextlib import contextmanager

import pytest
from websockets.asyncio.client import connect

from supermonoagent.protocol import OP_CLOSE, OP_HELLO, OP_HELLO_OK, OP_SESSIONS, pack_json, unpack
from supermonoagent.console_snapshot import _process_identity
from supermonoagent.pty import PtyCloseError, WinPty
from test_client_integration import ControlledPty, isolated_host


@contextmanager
def owned_process(pty):
    """Yield a pinned kernel handle for a new PTY created by this test."""
    with pinned_test_pid(pty._proc.pid) as handle:
        yield handle


@contextmanager
def pinned_test_pid(pid):
    import _winapi
    handle = _winapi.OpenProcess(_winapi.SYNCHRONIZE | 0x0001, False, pid)
    try:
        yield handle
    finally:
        if _winapi.WaitForSingleObject(handle, 0) == _winapi.WAIT_TIMEOUT:
            _winapi.TerminateProcess(handle, 1)
            assert _winapi.WaitForSingleObject(handle, 3000) == _winapi.WAIT_OBJECT_0
        _winapi.CloseHandle(handle)


async def next_catalog(ws, op, predicate):
    async with asyncio.timeout(10):
        async for frame in ws:
            if isinstance(frame, bytes):
                opcode, data = unpack(frame)
                if opcode == op:
                    state = json.loads(data)
                    if predicate(state):
                        return state
    raise AssertionError('catalog not received')


@pytest.mark.skipif(os.name!='nt', reason='Windows kernel process handles')
def test_explicit_end_confirms_owned_powershell_and_refreshes_other_viewer(tmp_path,record_testsuite_property):
    import _winapi
    async def run():
        async with isolated_host(real_shell=True) as (daemon, endpoint, _relay):
            window=daemon.hub.open_window('target', owner=daemon.token)
            neighbour=daemon.hub.open_window('neighbour', owner=daemon.token)
            marker=tmp_path/'foreground.pid'
            detached_marker=tmp_path/'separate-console.pid'
            ps=shutil.which('powershell')
            def encode(script):
                return base64.b64encode(script.encode('utf-16le')).decode('ascii')
            child_script="[IO.File]::WriteAllText('"+str(marker).replace("'","''")+"',[string]$PID); Start-Sleep -Seconds 45"
            detached_script=child_script.replace(str(marker).replace("'","''"),str(detached_marker).replace("'","''"))
            parent_script=("Start-Process -WindowStyle Hidden -FilePath '"+ps.replace("'","''")+
                           "' -ArgumentList '-NoLogo','-NoProfile','-EncodedCommand','"+encode(detached_script)+
                           "'; & '"+ps.replace("'","''")+"' -NoProfile -EncodedCommand "+encode(child_script))
            daemon.hub.pty_factory=lambda shell,cwd:WinPty([ps,'-NoProfile','-NoLogo','-NoExit','-EncodedCommand',encode(parent_script)],cwd)
            target=daemon.hub.open_cli(window.id,'powershell',str(tmp_path))
            daemon.hub.pty_factory=lambda shell,cwd:WinPty([ps,'-NoProfile','-NoLogo'],cwd)
            other=daemon.hub.open_cli(neighbour.id,'powershell',str(tmp_path))
            expected=dict(session=target.id,window_id=window.id,process_generation=target.process_generation)
            async with asyncio.timeout(8):
                while not all(p.exists() and p.read_text().strip() for p in (marker,detached_marker)):
                    await asyncio.sleep(.025)
            child_pid=int(marker.read_text().strip())
            assert _process_identity(child_pid)[0]==target.pty._proc.pid
            detached_pid=int(detached_marker.read_text().strip())
            assert _process_identity(detached_pid)[0]==target.pty._proc.pid
            with (owned_process(target.pty) as process, owned_process(other.pty) as untouched,
                  pinned_test_pid(child_pid) as child, pinned_test_pid(detached_pid) as detached):
                assert _winapi.WaitForSingleObject(child,0)==_winapi.WAIT_TIMEOUT
                async with connect(endpoint) as first,connect(endpoint) as second:
                    for ws in (first,second):
                        await ws.send(pack_json(OP_HELLO,{'token':daemon.token,'multiplex':True}))
                        initial=await next_catalog(ws,OP_HELLO_OK,lambda s:s['capabilities'].get('close_ack')==1)
                        assert any(x['process_generation']==target.process_generation for w in initial['windows'] for x in w['sessions'])
                    await first.send(pack_json(OP_CLOSE,dict(expected,request='stale',process_generation='old')))
                    stale=await next_catalog(first,OP_HELLO_OK,lambda s:s.get('close_ack',{}).get('request')=='stale')
                    assert stale['close_ack']['closed'] is False
                    assert _winapi.WaitForSingleObject(process,0)==_winapi.WAIT_TIMEOUT
                    await first.send(pack_json(OP_CLOSE,dict(expected,request='chosen')))
                    acknowledgment=await next_catalog(first,OP_HELLO_OK,lambda s:s.get('close_ack',{}).get('request')=='chosen')
                    assert acknowledgment['close_ack']==dict(expected,request='chosen',closed=True)
                    assert _winapi.WaitForSingleObject(process,0)==_winapi.WAIT_OBJECT_0
                    assert await asyncio.to_thread(_winapi.WaitForSingleObject,child,2000)==_winapi.WAIT_OBJECT_0
                    assert _winapi.WaitForSingleObject(untouched,0)==_winapi.WAIT_TIMEOUT
                    assert _winapi.WaitForSingleObject(detached,0)==_winapi.WAIT_TIMEOUT
                    record_testsuite_property("exited_root_pid",target.pty._proc.pid)
                    record_testsuite_property("exited_attached_child_pid",child_pid)
                    record_testsuite_property("preserved_separate_console_pid",detached_pid)
                    record_testsuite_property("preserved_neighbour_session_pid",other.pty._proc.pid)
                    directory=await next_catalog(second,OP_SESSIONS,lambda s:all(x['id']!=target.id for w in s['windows'] for x in w['sessions']))
                    assert next(w for w in directory['windows'] if w['id']==window.id)['sessions']==[]
                    assert next(w for w in directory['windows'] if w['id']==neighbour.id)['sessions'][0]['id']==other.id
    asyncio.run(run())


def test_failed_end_leaves_session_visible_and_can_retry():
    async def run():
        async with isolated_host() as (daemon, endpoint, _relay):
            class FailingPty(ControlledPty):
                def close(self):
                    if self.unclosable:
                        raise PtyCloseError('process_still_alive')
                    return super().close()
                unclosable=True
            daemon.hub.pty_factory=lambda *_:FailingPty()
            w=daemon.hub.open_window('target',owner=daemon.token)
            s=daemon.hub.open_cli(w.id,'powershell')
            try:
                async with connect(endpoint) as ws:
                    await ws.send(pack_json(OP_HELLO,{'token':daemon.token,'multiplex':True}))
                    await next_catalog(ws,OP_HELLO_OK,lambda _:True)
                    close=dict(session=s.id,window_id=w.id,process_generation=s.process_generation)
                    await ws.send(pack_json(OP_CLOSE,dict(close,request='denied')))
                    reply=await next_catalog(ws,OP_HELLO_OK,lambda data:data.get('close_ack',{}).get('request')=='denied')
                    assert reply['close_ack']['error']=='process_still_alive'
                    assert not reply['close_ack']['closed']
                    assert daemon.hub.sessions[s.id] is s and not s.stop.is_set()
                    s.pty.unclosable=False
                    await ws.send(pack_json(OP_CLOSE,dict(close,request='retry')))
                    reply=await next_catalog(ws,OP_HELLO_OK,lambda data:data.get('close_ack',{}).get('request')=='retry')
                    assert reply['close_ack']['closed'] and s.id not in daemon.hub.sessions
            finally:
                s.pty.unclosable=False
    asyncio.run(run())


def test_simultaneous_end_requests_share_verified_result_after_one_socket_detaches():
    async def run():
        async with isolated_host() as (daemon,_endpoint,_relay):
            started,released = threading.Event(),threading.Event()
            class SlowPty(ControlledPty):
                closes = 0
                def close(self):
                    self.closes += 1
                    started.set()
                    if not released.wait(3):
                        raise RuntimeError('fixture timeout')
                    super().close()
            daemon.hub.pty_factory=lambda *_:SlowPty()
            win=daemon.hub.open_window('target',owner=daemon.token)
            session=daemon.hub.open_cli(win.id,'powershell')
            first=asyncio.create_task(daemon.hub.end_native_session(session))
            try:
                assert await asyncio.to_thread(started.wait,2)
                second=asyncio.create_task(daemon.hub.end_native_session(session))
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
                assert daemon.hub.sessions[session.id] is session
                released.set()
                await asyncio.wait_for(second,3)
                assert session.id not in daemon.hub.sessions and session.pty.closes==1
            finally:
                released.set()
    asyncio.run(run())


def test_catalog_after_terminating_session_rechecks_source_cockpit_identity():
    async def run():
        async with isolated_host() as (daemon,endpoint,_relay):
            config=daemon.log_state_dir/'cockpit-feedback.json'
            config.write_text('{"bindings":[]}',encoding='utf-8')
            config.chmod(0o600)
            install=daemon.journals.store.host_installation_id
            win=daemon.hub.open_window('view',owner=daemon.token)
            session=daemon.hub.open_cli(win.id,'cmd')
            async with connect(endpoint) as ws:
                await ws.send(pack_json(OP_HELLO,dict(token=daemon.token,multiplex=True,
                    conversation_journal=1,managed_conversation=1)))
                hello=await next_catalog(ws,OP_HELLO_OK,lambda v:True)
                assert (hello['host_installation_id'],hello['capabilities']['cockpit_feedback'])==(install,1)
                await ws.send(pack_json(OP_CLOSE,{'session':session.id}))
                updated=await next_catalog(ws,OP_SESSIONS,
                    lambda v:all(s['id']!=session.id for w in v['windows'] for s in w['sessions']))
                assert (updated['host_installation_id'],updated['capabilities']['cockpit_feedback'])==(install,1)
                config.unlink()
                daemon.hub.open_window('refresh after revocation',owner=daemon.token)
                revoked=await next_catalog(ws,OP_SESSIONS,
                    lambda v:any(w['title']=='refresh after revocation' for w in v['windows']))
                assert 'host_installation_id' not in revoked
                assert 'cockpit_feedback' not in revoked['capabilities']
    asyncio.run(run())
