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
