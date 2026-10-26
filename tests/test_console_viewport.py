"""Visible-grid recovery prototype: owned fixtures only, no existing console reads."""
import copy
import json
import subprocess
from unittest.mock import Mock
import pytest
from supermonoagent import console_snapshot as C


def frame():
    return {'kind':'console_viewport','version':1,'pid':101,'parent':11,'created':12345,
      'buffer_size':[80,1000],'viewport':[4,50,7,51],'width':4,'height':2,
      'cursor_buffer':[6,51],'cursor':[2,1],'cursor_visible':False,'cursor_in_view':True,'cursor_size':25,
      'default_attributes':0x807,'palette':[[i,i,i] for i in range(16)],
      'code_units':[[0x754c,0,0xD83D,0xDE00],[65,0xD800,32,66]],
      'attributes':[[0x107,0x207,7,7],[0x8007,0x4007,7,7]],'stable_samples':2}


def test_raw_utf16_and_lvb_flags_are_valid_without_unicode_normalization():
    value=frame();before=copy.deepcopy(value)
    assert C.validate_console_viewport(value)
    assert value==before


@pytest.mark.parametrize('field,value',[
 ('version',True),('width',True),('width',501),('height',201),('buffer_size',[80,32768]),
 ('viewport',[4,50,8,51]),('viewport',[-1,50,2,51]),('cursor',[1,1]),
 ('cursor_buffer',[80,0]),('cursor_visible',1),('cursor_size',0),('stable_samples',1),
 ('default_attributes',65536),('code_units',[[1]*4]),('attributes',[[7]*4]),
 ('palette',[[256,0,0]]*16)])
def test_malformed_or_out_of_bounds_grid_is_rejected(field,value):
    data=frame();data[field]=value
    assert not C.validate_console_viewport(data)


def test_two_matching_whole_grids_and_metadata_are_required():
    sample=Mock(side_effect=[frame(),frame()]);identity=Mock(return_value=(11,12345))
    assert C._consistent_viewport(sample,identity,(11,12345))==frame()
    assert sample.call_count==2
    for field,value in [('cursor_visible',True),('default_attributes',6),('code_units',[[1]*4]*2),('palette',[[0,1,2]]*16)]:
        changed=frame();changed[field]=value
        assert C._consistent_viewport(Mock(side_effect=[frame(),changed]),identity,(11,12345)) is None
    assert C._consistent_viewport(Mock(return_value=frame()),Mock(side_effect=[(11,12345),(11,54321)]),(11,12345)) is None


def test_parent_reader_uses_separate_child_action_and_no_cache(monkeypatch):
    monkeypatch.setattr(C.sys,'platform','win32')
    monkeypatch.setattr(C,'_process_identity',lambda pid:(11,12345))
    run=Mock(return_value=subprocess.CompletedProcess([],0,stdout=json.dumps(frame()).encode()))
    monkeypatch.setattr(C.subprocess,'run',run)
    assert C.read_console_viewport(101,12345,11)==frame()
    assert C.read_console_viewport(101,12345,11)==frame()
    assert run.call_count==2
    args,kwargs=run.call_args
    assert '--viewport' in args[0] and kwargs['stdin']==subprocess.DEVNULL
    assert kwargs['stderr']==subprocess.DEVNULL and kwargs['timeout']<=2


@pytest.mark.parametrize('change',[None,'clipped','metadata','failure'])
def test_native_sample_reads_one_whole_offset_viewport_and_fails_on_change(change):
    from types import SimpleNamespace
    types=C._viewport_types();COORD,RECT,INFO,CELL,CURSOR=types
    calls=[];infos=[]
    def info(handle,pointer):
        value=pointer._obj;value.size=COORD(80,1000);value.cursor=COORD(6,51)
        value.window=RECT(4,50,7,51);value.attributes=0x807
        for i in range(16):value.colors[i]=i|(i<<8)|(i<<16)
        infos.append(True)
        if change=='metadata' and len(infos)>1:value.attributes=7
        return True
    def cursor(handle,pointer):pointer._obj.size=25;pointer._obj.visible=False;return True
    def read(handle,cells,size,origin,region):
        calls.append((size.X,size.Y,origin.X,origin.Y,[region._obj.left,region._obj.top,region._obj.right,region._obj.bottom]))
        for i,unit in enumerate(sum(frame()['code_units'],[])):
            cells[i].unit=unit;cells[i].attributes=sum(frame()['attributes'],[])[i]
        if change=='clipped':region._obj.right-=1
        return change!='failure'
    kernel=SimpleNamespace(GetConsoleScreenBufferInfoEx=info,GetConsoleCursorInfo=cursor,ReadConsoleOutputW=read)
    value=C._read_viewport_sample(kernel,123,types,101,(11,12345))
    assert calls==[(4,2,0,0,[4,50,7,51])]
    assert value==frame() if change is None else value is None


@pytest.mark.parametrize('outcome',['success','read-other-error','query-failure','open-failure'])
def test_access_denied_cursor_fallback_queries_only_and_closes_rw_handle(monkeypatch,outcome):
    from types import SimpleNamespace
    types=C._viewport_types();COORD,RECT,INFO,CELL,CURSOR=types
    last=[0];reads=[]
    monkeypatch.setattr(C.ctypes,'get_last_error',lambda:last[0],raising=False)
    def info(handle,pointer):
        value=pointer._obj;value.size=COORD(80,1000);value.cursor=COORD(6,51)
        value.window=RECT(4,50,7,51);value.attributes=0x807
        for i in range(16):value.colors[i]=i|(i<<8)|(i<<16)
        return True
    def cursor(handle,pointer):
        if handle==123 or outcome=='query-failure':
            last[0]=6 if outcome=='read-other-error' else 5
            return False
        pointer._obj.size=25;pointer._obj.visible=False;return True
    def read(handle,cells,size,origin,region):
        reads.append(handle)
        for i,unit in enumerate(sum(frame()['code_units'],[])):
            cells[i].unit=unit;cells[i].attributes=sum(frame()['attributes'],[])[i]
        return True
    create=Mock(return_value=0 if outcome=='open-failure' else 456);close=Mock()
    kernel=SimpleNamespace(GetConsoleScreenBufferInfoEx=info,GetConsoleCursorInfo=cursor,
        ReadConsoleOutputW=read,CreateFileW=create,CloseHandle=close)
    result=C._read_viewport_sample(kernel,123,types,101,(11,12345))
    if outcome=='success':
        assert result==frame() and reads==[123]
        assert close.call_count==2 and all(call.args==(456,) for call in close.call_args_list)
    else:
        assert result is None and not reads
        if outcome in ('read-other-error','open-failure'):close.assert_not_called()
        else:close.assert_called_once_with(456)
    for call in create.call_args_list:
        assert call.args[:2]==('CONOUT$',0xC0000000)
    if outcome=='read-other-error':create.assert_not_called()


def test_child_capture_rejects_identity_change_and_detaches_owned_helper(monkeypatch):
    from types import SimpleNamespace
    kernel=SimpleNamespace(**{name:Mock(return_value=True) for name in ('FreeConsole','AttachConsole',
      'CreateFileW','GetConsoleScreenBufferInfoEx','GetConsoleCursorInfo','ReadConsoleOutputW','CloseHandle')})
    monkeypatch.setattr(C.ctypes,'WinDLL',lambda *args,**kwargs:kernel,raising=False)
    monkeypatch.setattr(C,'_process_identity',Mock(side_effect=[(11,12345),(11,54321)]))
    assert C._capture_viewport(101,12345,11) is None
    kernel.CreateFileW.assert_not_called();kernel.ReadConsoleOutputW.assert_not_called()
    assert kernel.FreeConsole.call_count==2


@pytest.mark.parametrize('failure',['oversize','duplicate','shape','identity','timeout'])
def test_parent_bounds_and_identity_failures_return_no_cells(monkeypatch,failure):
    monkeypatch.setattr(C.sys,'platform','win32')
    identity=Mock(side_effect=[(11,12345),(11,54321)] if failure=='identity' else lambda pid:(11,12345))
    monkeypatch.setattr(C,'_process_identity',identity)
    raw=json.dumps(frame()).encode()
    if failure=='oversize':raw=b' '* (C.MAX_BYTES+1)
    elif failure=='duplicate':raw=b'{"kind":"console_viewport","kind":"console_viewport"}'
    elif failure=='shape':raw=b'{}'
    run=Mock(side_effect=subprocess.TimeoutExpired(['owned helper'],.1)) if failure=='timeout' else Mock(return_value=subprocess.CompletedProcess([],0,stdout=raw))
    monkeypatch.setattr(C.subprocess,'run',run)
    assert C.read_console_viewport(101,12345,11) is None


@pytest.mark.parametrize('pid,created,parent,timeout',[(True,None,None,1),(0,None,None,1),(1,True,None,1),
 (1,None,False,1),(1,None,None,float('nan')),(1,None,None,0)])
def test_invalid_request_never_starts_helper(monkeypatch,pid,created,parent,timeout):
    run=Mock();monkeypatch.setattr(C.subprocess,'run',run)
    assert C.read_console_viewport(pid,created,parent,timeout) is None
    run.assert_not_called()


@pytest.mark.skipif(C.sys.platform!='win32',reason='Owned hidden Windows console fixture')
def test_owned_hidden_console_viewport_cjk_emoji_bottomrow_and_hidden_cursor(tmp_path,monkeypatch):
    import os
    import queue
    import sys
    import threading
    from pathlib import Path
    # Only this fixture process owns/writes its newly allocated hidden console.
    # The capture helper itself remains read-only, and no existing PID is used.
    script=r'''
import ctypes,json,time,os
from ctypes import wintypes as W
from supermonoagent.console_snapshot import _viewport_types
COORD,RECT,INFO,CELL,CURSOR=_viewport_types()
k=ctypes.WinDLL('kernel32',use_last_error=True)
decl=[(k.CreateFileW,[W.LPCWSTR,W.DWORD,W.DWORD,ctypes.c_void_p,W.DWORD,W.DWORD,W.HANDLE],W.HANDLE),
 (k.SetConsoleWindowInfo,[W.HANDLE,W.BOOL,ctypes.POINTER(RECT)],W.BOOL),
 (k.SetConsoleScreenBufferSize,[W.HANDLE,COORD],W.BOOL),
 (k.SetConsoleCursorPosition,[W.HANDLE,COORD],W.BOOL),
 (k.SetConsoleCursorInfo,[W.HANDLE,ctypes.POINTER(CURSOR)],W.BOOL),
 (k.SetConsoleTextAttribute,[W.HANDLE,W.WORD],W.BOOL),
 (k.WriteConsoleOutputW,[W.HANDLE,ctypes.POINTER(CELL),COORD,COORD,ctypes.POINTER(RECT)],W.BOOL),
 (k.ReadConsoleOutputW,[W.HANDLE,ctypes.POINTER(CELL),COORD,COORD,ctypes.POINTER(RECT)],W.BOOL)]
for f,args,result in decl:f.argtypes,f.restype=args,result
try:
 h=k.CreateFileW('CONOUT$',0xC0000000,3,None,3,0,None)
 assert h not in (None,ctypes.c_void_p(-1).value)
 assert k.SetConsoleWindowInfo(h,True,ctypes.byref(RECT(0,0,59,9)))
 assert k.SetConsoleScreenBufferSize(h,COORD(80,40))
 assert k.SetConsoleCursorPosition(h,COORD(9,12))
 assert k.SetConsoleWindowInfo(h,True,ctypes.byref(RECT(4,6,63,15)))
 assert k.SetConsoleCursorInfo(h,ctypes.byref(CURSOR(25,False)))
 assert k.SetConsoleTextAttribute(h,0x1E)
 cells=(CELL*600)()
 for cell in cells:cell.unit=32;cell.attributes=7
 for i,unit in enumerate([0x754C,0,0xD83D,0xDE00,65,66]):cells[i].unit=unit
 cells[0].attributes=0x107;cells[1].attributes=0x207
 for i,unit in enumerate(map(ord,'BOTTOM')):cells[540+i].unit=unit
 assert k.WriteConsoleOutputW(h,cells,COORD(60,10),COORD(0,0),ctypes.byref(RECT(4,6,63,15)))
 actual=(CELL*600)()
 assert k.ReadConsoleOutputW(h,actual,COORD(60,10),COORD(0,0),ctypes.byref(RECT(4,6,63,15)))
 print(json.dumps({'ok':True,'pid':os.getpid(),'units':[c.unit for c in actual[:8]],'attrs':[c.attributes for c in actual[:8]]}),flush=True)
except Exception:
 print(json.dumps({'ok':False}),flush=True)
 raise SystemExit(1)
time.sleep(30)
'''
    root=str(Path(__file__).resolve().parents[1]/'src')
    monkeypatch.setenv('PYTHONPATH',root+os.pathsep+os.environ.get('PYTHONPATH',''))
    startup=subprocess.STARTUPINFO();startup.dwFlags|=subprocess.STARTF_USESHOWWINDOW;startup.wShowWindow=0
    # Use the actual interpreter, avoiding a Windows venv launcher child that
    # could outlive termination of its launcher during fixture cleanup.
    executable=getattr(sys,'_base_executable',None) or sys.executable
    process=subprocess.Popen([executable,'-u','-c',script],stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,startupinfo=startup,
        creationflags=subprocess.CREATE_NEW_CONSOLE)
    ready=queue.Queue(maxsize=1)
    reader=threading.Thread(target=lambda:ready.put(process.stdout.readline()),daemon=True);reader.start()
    try:
        status=json.loads(ready.get(timeout=5))
        if not status.get('ok'):pytest.skip('Owned hidden console APIs unavailable on this host')
        assert status['pid']==process.pid
        identity=C._process_identity(process.pid)
        assert identity is not None and identity[0]==os.getpid()
        value=C.read_console_viewport(process.pid,identity[1],os.getpid())
        assert value is not None
        assert value['buffer_size']==[80,40] and value['viewport']==[4,6,63,15]
        assert value['width']==60 and value['height']==10 and value['cursor']==[5,6]
        assert value['cursor_visible'] is False and value['default_attributes']==0x1E
        assert value['code_units'][0][:8]==status['units'] and value['attributes'][0][:8]==status['attrs']
        assert 0x754C in status['units'] and 0xD83D in status['units'] and 0xDE00 in status['units']
        assert value['code_units'][-1][:6]==list(map(ord,'BOTTOM'))
        assert len(value['palette'])==16
        assert C.read_console_viewport(process.pid,identity[1]+1,os.getpid()) is None
    finally:
        process.terminate();process.wait(timeout=5);reader.join(timeout=1)
        process.stdout.close()
