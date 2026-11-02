"""Desktop shell bridge only opens validated http(s) URLs."""
import pytest
from supermonoagent import desktop_shell as S


class Window:
    def __init__(self):
        self.url='http://127.0.0.1:1234/workspace#token=owned'
        self.exposed=[]
    def expose(self,*functions):self.exposed.extend(functions)
    def get_current_url(self):return self.url


@pytest.fixture
def window(monkeypatch):
    calls=[]
    monkeypatch.setattr(S.webbrowser,'open',lambda url,new=0:calls.append((url,new)) or True,raising=False)
    window=Window();window.opened=calls
    return window


def test_open_external_exposed_and_opens_http(window):
    item=S.DesktopShell(window,window.url)
    assert 'open_external' in [function.__name__ for function in window.exposed]
    assert item.open_external('http://117.50.48.22:8817/') is True
    assert window.opened==[('http://117.50.48.22:8817/',2)]


def test_open_external_rejects_non_http(window):
    item=S.DesktopShell(window,window.url)
    for url in ['file:///etc/passwd','javascript:alert(1)','ftp://example.com','notaurl','https:','//evil.com','http://']:
        with pytest.raises(ValueError):item.open_external(url)
    assert window.opened==[]


def test_open_external_checks_page_origin_and_path(window):
    item=S.DesktopShell(window,window.url)
    window.url='https://untrusted.invalid/workspace'
    with pytest.raises(ValueError):item.open_external('https://example.com/')
    window.url='http://127.0.0.1:1234/other'
    with pytest.raises(ValueError):item.open_external('https://example.com/')
    assert window.opened==[]
