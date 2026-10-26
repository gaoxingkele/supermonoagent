"""Owned profiles/processes only; no production daemon or credentials."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import pytest
from supermonoagent import file_companion_manager as M


def profile(tmp_path,**changes):
    root=tmp_path/'state';path=root/'file-exchange/profile.json'
    value={'schema':1,'broker_url':'ws://127.0.0.1:12345/file/ws','enrollment_key':'OWNED_ENROLLMENT_'+('x'*40)}
    value.update(changes);M._write_private(path,json.dumps(value).encode())
    return root,path,value


def test_missing_profile_is_noop_and_room_is_not_guessed(tmp_path,monkeypatch):
    monkeypatch.setattr(subprocess,'Popen',lambda *a,**k:pytest.fail('Unexpected process'))
    manager=M.FileCompanionManager(tmp_path/'absent','http://127.0.0.1:1234','OWNED_OWNER','saved-room')
    assert manager.ensure_running()['reason']=='not_configured' and not (tmp_path/'absent').exists()
    assert M.FileCompanionManager(tmp_path/'absent','http://127.0.0.1:1234','OWNED_OWNER','').ensure_running()['reason']=='missing_room'


@pytest.mark.parametrize('change',[{'schema':True},{'enrollment_key':'short'},{'broker_url':'ws://remote.invalid:123/file/ws'},
    {'broker_url':'wss://user:password@example.invalid/file/ws'},{'unknown_secret':'owned'},{'broker_token':'bad token'}])
def test_invalid_profile_never_starts(tmp_path,monkeypatch,change):
    root,_,_=profile(tmp_path,**change)
    monkeypatch.setattr(subprocess,'Popen',lambda *a,**k:pytest.fail('Unexpected process'))
    assert not M.FileCompanionManager(root,'http://127.0.0.1:1234','OWNED_OWNER','r').ensure_running()['enabled']
    assert not (root/'file-exchange/helper').exists()


@pytest.fixture
def owned_helper(tmp_path,monkeypatch):
    processes=[];jobs=[];script=tmp_path/'owned_helper.py'
    source_root=str(Path(__file__).resolve().parents[1]/'src')
    script.write_text('''import argparse,json,os,sys,time
from pathlib import Path
sys.path.insert(0,SOURCE)
from supermonoagent.file_companion import _process_birth
from supermonoagent.file_companion_manager import _write_private
p=argparse.ArgumentParser()
for n in ['backend','broker','room','state-dir','owner-token-file','broker-token-file','enrollment-key-file','status-id']:p.add_argument('--'+n)
a=p.parse_args()
body={'schema':1,'role':'file-companion','protocol':'sma-files-1','pid':os.getpid(),'process_start':_process_birth(os.getpid()),'backend':a.backend,'broker_url':a.broker,'room':a.room,'status_id':a.status_id,'state':'online'}
while True:
 body['updated_at']=time.time();_write_private(Path(a.state_dir)/'status.json',json.dumps(body).encode());time.sleep(.1)
'''.replace('SOURCE',repr(source_root)),encoding='utf8')
    original_command=M.FileCompanionManager._command
    def command(self,broker,helper,status_id):
        actual=original_command(self,broker,helper,status_id)
        return [sys.executable,str(script),*actual[actual.index('--backend'):]]
    monkeypatch.setattr(M.FileCompanionManager,'_command',command)
    original_popen=subprocess.Popen
    def popen(command,**options):
        assert not any('OWNED_' in str(part) for part in command)
        assert options['stdin']==options['stdout']==options['stderr']==subprocess.DEVNULL
        if sys.platform=='win32':assert options['creationflags']&subprocess.CREATE_NO_WINDOW
        process=original_popen(command,**options);processes.append(process)
        if os.name=='nt':
            from supermonoagent.assistant_codex import _windows_job
            jobs.append(_windows_job(process))
        return process
    monkeypatch.setattr(subprocess,'Popen',popen)
    yield processes
    for close in jobs:close()
    for process in processes:
        if process.poll() is None:process.terminate()
        process.wait(timeout=5)


def online(manager):
    end=time.monotonic()+8
    while time.monotonic()<end:
        value=manager.ensure_running()
        if value.get('ready'):return value
        time.sleep(.05)
    pytest.fail('Owned helper did not become online: '+json.dumps(value))


def test_hidden_private_launch_singleton_and_new_gui_adopts(tmp_path,owned_helper):
    root,_,raw=profile(tmp_path,broker_token='OWNED_BROKER')
    manager=M.FileCompanionManager(root,'http://127.0.0.1:1234','OWNED_OWNER','saved-code')
    first=manager.ensure_running();assert first['enabled'] and not first['ready'] and first['reason']=='connecting'
    value=online(manager)
    assert value['reason']=='online' and value['local_room']=='saved-code'
    assert M.FileCompanionManager(root,'http://127.0.0.1:1234','OWNED_OWNER','saved-code').ensure_running()['ready']
    assert len(owned_helper)==1
    assert all(secret not in json.dumps(value) for secret in ['OWNED_OWNER','OWNED_BROKER',raw['enrollment_key'],str(root)])
    for name in ['owner.token','broker.token','enrollment.key']:M._private(root/'file-exchange/helper'/name)


def test_live_config_change_preserves_files_and_process(tmp_path,owned_helper):
    root,_,_=profile(tmp_path);manager=M.FileCompanionManager(root,'http://127.0.0.1:1234','OWNED_OWNER','r')
    online(manager);path=root/'file-exchange/helper/owner.token';original=path.read_bytes()
    value=M.FileCompanionManager(root,'http://127.0.0.1:1234','OTHER_OWNER','r').ensure_running()
    assert not value['enabled'] and value['reason']=='configuration_changed'
    assert path.read_bytes()==original and len(owned_helper)==1 and owned_helper[0].poll() is None


def test_stale_or_wrong_status_cannot_claim_ready(tmp_path,owned_helper):
    root,_,_=profile(tmp_path);manager=M.FileCompanionManager(root,'http://127.0.0.1:1234','OWNED_OWNER','r')
    online(manager);record=M._read_private(root/'file-exchange/manager.json');path=root/'file-exchange/helper/status.json'
    value=M._read_private(path);owned_helper[0].terminate();owned_helper[0].wait(timeout=5)
    value['status_id']='0'*32;M._write_private(path,json.dumps(value).encode())
    assert manager._status(record,'ws://127.0.0.1:12345/file/ws')=='connecting'
    value['status_id']=record['status_id'];value['updated_at']=time.time()-60;M._write_private(path,json.dumps(value).encode())
    assert manager._status(record,'ws://127.0.0.1:12345/file/ws')=='connecting'


def test_real_companion_safe_status(tmp_path):
    from test_file_companion import environment,eventually
    async def exercise():
        async with environment(tmp_path) as (_,_,_,helpers):
            helper=helpers[0];helper.status_id='a'*32
            path=helper.state_dir/'status.json'
            await eventually(path.exists)
            end=time.monotonic()+5
            while time.monotonic()<end:
                value=json.loads(path.read_text())
                if value.get('state')=='online' and value.get('status_id')=='a'*32:break
                await asyncio.sleep(.05)
            else:pytest.fail('Owned safe status unavailable')
            assert value['pid']==os.getpid() and value['process_start']==M._process_birth(os.getpid())
            assert not set(value)&{'token','owner_token','host_secret','enrollment_key'}
            assert 'owned-owner-' not in json.dumps(value)
    asyncio.run(exercise())


def test_profile_with_broad_acl_is_rejected_before_body_read(tmp_path,monkeypatch):
    root,path,_=profile(tmp_path)
    if os.name=='nt':
        import ctypes
        security,kernel,_=M._win_functions();descriptor=ctypes.c_void_p()
        assert security.ConvertStringSecurityDescriptorToSecurityDescriptorW('D:P(A;;FA;;;WD)',1,ctypes.byref(descriptor),None)
        try:assert security.SetFileSecurityW(str(path),4|0x80000000,descriptor)
        finally:kernel.LocalFree(descriptor)
    else:os.chmod(path,0o644)
    original=Path.open
    def observed(self,*args,**kwargs):
        if self==path:pytest.fail('Unsafe profile body was read')
        return original(self,*args,**kwargs)
    monkeypatch.setattr(Path,'open',observed)
    value=M.FileCompanionManager(root,'http://127.0.0.1:1234','OWNED_OWNER','r').ensure_running()
    assert not value['enabled'] and value['reason']=='unsafe_state'


def test_matching_nonce_cannot_adopt_an_unrelated_pid(tmp_path,owned_helper):
    root,_,_=profile(tmp_path);manager=M.FileCompanionManager(root,'http://127.0.0.1:1234','OWNED_OWNER','r')
    online(manager);record=M._read_private(root/'file-exchange/manager.json')
    value=M._read_private(root/'file-exchange/helper/status.json')
    value.update(pid=os.getpid(),process_start=M._process_birth(os.getpid()))
    owned_helper[0].terminate();owned_helper[0].wait(timeout=5)
    M._write_private(root/'file-exchange/helper/status.json',json.dumps(value).encode())
    assert manager._status(record,'ws://127.0.0.1:12345/file/ws')=='connecting'


def test_source_and_frozen_dispatch_include_only_paths_and_public_status_id(tmp_path,monkeypatch):
    manager=M.FileCompanionManager(tmp_path,'http://127.0.0.1:1234','OWNED_OWNER','existing-code')
    executable=tmp_path/'SuperMonoAgent.exe'
    monkeypatch.setattr(M,'sys',type('Runtime',(),{'executable':str(executable),'platform':'win32','frozen':True})())
    command=manager._command('wss://example.invalid/file/ws',tmp_path/'helper','a'*32)
    assert command[:2]==[str(executable),'file-companion']
    assert '--status-id' in command and 'OWNED_OWNER' not in command
    monkeypatch.setattr(M,'sys',sys)
    command=manager._command('wss://example.invalid/file/ws',tmp_path/'helper','b'*32)
    assert '-m' in command and 'supermonoagent.file_companion' in command
