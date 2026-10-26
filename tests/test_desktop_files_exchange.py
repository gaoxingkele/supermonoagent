"""Configuration bridge tests never read the clipboard."""
import pytest
from supermonoagent import desktop_files as D


class Event:
    def __init__(self):self.listeners=[]
    def __iadd__(self,value):self.listeners.append(value);return self
class Window:
    def __init__(self):
        self.url='http://127.0.0.1:1234/workspace#token=owned'
        self.events=type('Events',(),{'loaded':Event(),'closed':Event()})();self.exposed=[]
    def expose(self,*functions):self.exposed.extend(functions)
    def get_current_url(self):return self.url
class Clipboard:
    def snapshot(self,*a,**k):pytest.fail('Configuration accessed clipboard')
    def close(self):pass


@pytest.fixture
def window(monkeypatch):
    monkeypatch.setattr(D,'ClipboardFiles',Clipboard);return Window()


def test_optional_bridge_disabled_and_exposed(window):
    item=D.DesktopFiles(window,window.url)
    assert 'file_exchange_config' in [function.__name__ for function in window.exposed]
    value=item.file_exchange_config();assert not value['enabled'] and not value['ready'] and value['reason']=='not_configured'


def test_projection_origin_path_and_closed_guard(window):
    calls=[]
    def provider():calls.append(True);return {'enabled':True,'ready':False,'broker_url':'wss://example.invalid/file/ws','local_room':'saved-room','reason':'connecting'}
    item=D.DesktopFiles(window,window.url,file_config_provider=provider)
    assert item.file_exchange_config()['local_room']=='saved-room'
    window.url='https://untrusted.invalid/workspace'
    with pytest.raises(ValueError):item.file_exchange_config()
    assert len(calls)==1
    window.url='http://127.0.0.1:1234/other'
    with pytest.raises(ValueError):item.file_exchange_config()
    window.url='http://127.0.0.1:1234/workspace';item._closed.set()
    with pytest.raises(ValueError):item.file_exchange_config()


@pytest.mark.parametrize('bad',[
    {'enabled':True,'ready':True,'broker_url':'wss://example.invalid/file/ws','local_room':'r','token':'DO_NOT_RETURN'},
    {'enabled':True,'broker_url':'wss://user:secret@example.invalid/file/ws','local_room':'r'},
    {'enabled':False,'ready':True},{'enabled':'true'},
])
def test_private_or_malformed_provider_rejected(window,bad):
    item=D.DesktopFiles(window,window.url,file_config_provider=lambda:bad)
    with pytest.raises(ValueError) as error:item.file_exchange_config()
    assert 'DO_NOT_RETURN' not in str(error.value) and 'secret@' not in str(error.value)


def test_generation_change_during_provider_rejected(window):
    holder={}
    def provider():holder['item']._page_generation+=1;return {'enabled':False}
    holder['item']=D.DesktopFiles(window,window.url,file_config_provider=provider)
    with pytest.raises(ValueError):holder['item'].file_exchange_config()


def test_text_clipboard_methods_are_explicit_and_exposed(window, monkeypatch):
    calls=[]
    monkeypatch.setattr(D,'read_clipboard_text',lambda:calls.append(('read',)) or 'owned text',raising=False)
    monkeypatch.setattr(D,'write_clipboard_text',lambda text:calls.append(('write',text)) or True,raising=False)
    item=D.DesktopFiles(window,window.url)
    assert not calls
    exposed={fn.__name__ for fn in window.exposed}
    assert {'clipboard_read_text','clipboard_write_text'}<=exposed
    assert item.clipboard_read_text()=='owned text'
    assert item.clipboard_write_text('selected text') is True
    assert calls==[('read',),('write','selected text')]


@pytest.mark.parametrize('operation',['read','write'])
@pytest.mark.parametrize('boundary',['origin','path','generation','closed','native-error'])
def test_text_clipboard_checks_before_and_after_and_sanitizes(window, monkeypatch, operation, boundary):
    calls=[]
    item=D.DesktopFiles(window,window.url)
    def native(*args):
        calls.append(True)
        if boundary=='origin':window.url='https://untrusted.invalid/workspace'
        elif boundary=='path':window.url='http://127.0.0.1:1234/other'
        elif boundary=='generation':item._page_generation+=1
        elif boundary=='closed':item._closed.set()
        elif boundary=='native-error':raise OSError('DO_NOT_LEAK_TEXT')
        return 'DO_NOT_LEAK_TEXT' if operation=='read' else True
    monkeypatch.setattr(D,'read_clipboard_text',native,raising=False)
    monkeypatch.setattr(D,'write_clipboard_text',native,raising=False)
    method=getattr(item,'clipboard_'+operation+'_text')
    with pytest.raises(ValueError) as error:method(*(['selected text'] if operation=='write' else []))
    assert str(error.value)=='Clipboard text unavailable' and calls==[True]
    if boundary in ['origin','path','closed']:
        with pytest.raises(ValueError):method(*(['selected text'] if operation=='write' else []))
        assert calls==[True]
