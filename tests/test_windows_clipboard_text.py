"""Fake Win32 only: never access the real OS clipboard."""
from types import SimpleNamespace
import pytest
from supermonoagent import windows_clipboard as C


@pytest.fixture
def native(monkeypatch):
    state={'data': 'owned text'.encode('utf-16-le')+b'\0\0','calls':[], 'fail':None,'written':None}
    def call(name, result=1):
        def operation(*args):
            state['calls'].append((name,args))
            if state['fail']==name:
                return 0
            return result
        return operation
    def memmove(pointer, data, size):
        state['calls'].append(('memmove',(pointer,size)))
        if state['fail']=='memmove':
            raise OSError('DO_NOT_LEAK_TEXT')
        state['written']=bytes(data[:size])
    fake=SimpleNamespace(string_at=lambda pointer,size:state['data'][:size],memmove=memmove,
                         set_last_error=lambda value:None,get_last_error=lambda:0)
    user=SimpleNamespace(OpenClipboard=call('open'),CloseClipboard=call('close'),
      IsClipboardFormatAvailable=call('available'),GetClipboardData=call('get',100),
      EmptyClipboard=call('empty'),SetClipboardData=call('set',101),
      CreateWindowExW=call('create',77),DestroyWindow=call('destroy'))
    kernel=SimpleNamespace(GlobalSize=lambda h:len(state['data']),GlobalLock=call('lock',200),
      GlobalUnlock=call('unlock',0),GlobalAlloc=call('alloc',101),GlobalFree=call('free',0))
    monkeypatch.setattr(C,'_native_apis',lambda:(fake,user,kernel,None))
    return state


def names(state):return [name for name,_ in state['calls']]


def test_read_unicode_and_unlock_close(native):
    native['data']='hello \U0001f600\n\u4e2d\u6587'.encode('utf-16-le')+b'\0\0'
    assert C.read_clipboard_text()=='hello \U0001f600\n\u4e2d\u6587'
    assert names(native)==['open','available','get','lock','unlock','close']


def test_write_owns_hidden_hwnd_and_transfers_memory(native):
    assert C.write_clipboard_text('owned \U0001f600') is True
    assert native['written']=='owned \U0001f600'.encode('utf-16-le')+b'\0\0'
    events=names(native)
    assert events.index('alloc')<events.index('empty')
    assert events.index('memmove')<events.index('empty')
    assert events.index('unlock')<events.index('empty')
    assert events.index('set')<events.index('close')<events.index('destroy')
    assert 'free' not in events
    assert next(args for name,args in native['calls'] if name=='open')==(77,)
    assert next(args for name,args in native['calls'] if name=='set')[0]==13


@pytest.mark.parametrize('text',[None,True,'a\0b','\ud800','x'*(1024*1024//2+1)],ids=['none','bool','nul','surrogate','oversize'])
def test_bad_write_does_not_touch_clipboard(native,text):
    with pytest.raises(ValueError,match='Clipboard text unavailable'):
        C.write_clipboard_text(text)
    assert not native['calls']


@pytest.mark.parametrize('raw',[b'a',b'abc',b'\x00\xd8\0\0',b'a\0',b'a\0\0\0b\0\0\0',b'x'*(1024*1024+18)],
                         ids=['one-byte','odd','bad-surrogate','unterminated','embedded-nul','oversize'])
def test_bad_read_fixed_error_unlocks_and_closes(native,raw):
    native['data']=raw
    with pytest.raises(ValueError) as error:C.read_clipboard_text()
    assert str(error.value)=='Clipboard text unavailable'
    assert names(native)[-1]=='close'
    if 'lock' in names(native):assert 'unlock' in names(native)


@pytest.mark.parametrize('stage',['alloc','lock','memmove','create','open','empty','set','close','destroy'])
def test_write_failure_cleanup_and_does_not_clear_until_prepared(native,stage):
    native['fail']=stage
    with pytest.raises(ValueError) as error:C.write_clipboard_text('DO_NOT_LEAK_TEXT')
    assert str(error.value)=='Clipboard text unavailable'
    events=names(native)
    if stage not in ['alloc','close','destroy']:assert 'free' in events
    if stage in ['close','destroy']:assert 'free' not in events
    if stage in ['alloc','lock','memmove','create','open']:assert 'empty' not in events
    if 'open' in events and stage!='open':assert 'close' in events
    if 'create' in events and stage!='create':assert 'destroy' in events


@pytest.mark.parametrize('stage',['open','get','lock','close'])
def test_read_failure_is_safe_and_always_releases_acquired_resources(native,stage):
    native['fail']=stage
    with pytest.raises(ValueError) as error:C.read_clipboard_text()
    assert str(error.value)=='Clipboard text unavailable'
    events=names(native)
    if stage!='open':assert events[-1]=='close'
    if stage=='close':assert 'unlock' in events


def test_no_text_returns_empty_and_size_boundary(native):
    native['fail']='available'
    assert C.read_clipboard_text()==''
    assert 'get' not in names(native)
    native['fail']=None
    native['calls'].clear()
    text='x'*(1024*1024//2)
    assert C.write_clipboard_text(text)
    native['data']=native['written']+b'\0'*6
    assert C.read_clipboard_text()==text
