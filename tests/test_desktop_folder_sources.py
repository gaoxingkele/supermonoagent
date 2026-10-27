from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from supermonoagent import desktop_files as D


class Event:
    def __init__(self):
        self.listeners = []
    def __iadd__(self, function):
        self.listeners.append(function)
        return self


class Window:
    def __init__(self):
        self.url = 'http://127.0.0.1:1234/workspace'
        self.events = SimpleNamespace(loaded=Event(), closed=Event())
        self.exposed = []
        self.dialog_calls = []
        self.selection = None
        self.on_dialog = lambda: None
    def expose(self, *functions):
        self.exposed.extend(functions)
    def get_current_url(self):
        return self.url
    def create_file_dialog(self, *args, **kwargs):
        self.dialog_calls.append((args, kwargs))
        self.on_dialog()
        return self.selection


class Files:
    def __init__(self):
        self.calls = []
        self.released = []
        self.closed = False
        self.on_snapshot = lambda: None
    def snapshot_folder(self, root, *, cancelled):
        self.calls.append(('folder', root))
        assert not cancelled()
        self.on_snapshot()
        return {'kind': 'folder', 'manifest': {}, 'items': [{'id': 'own-new'}]}
    def snapshot_clipboard_folders(self, *, cancelled):
        self.calls.append(('clipboard',))
        assert not cancelled()
        self.on_snapshot()
        return {'kind': 'folders', 'folders': [
            {'kind': 'folder', 'manifest': {}, 'items': [{'id': 'own-new'}]}]}
    def release(self, source_id):
        self.released.append(source_id)
    def close(self):
        self.closed = True


@pytest.fixture
def bridge(monkeypatch):
    files = Files()
    window = Window()
    monkeypatch.setattr(D, 'ClipboardFiles', lambda: files)
    monkeypatch.setitem(sys.modules, 'webview', SimpleNamespace(FileDialog=SimpleNamespace(FOLDER=20)))
    return D.DesktopFiles(window, window.url), window, files


def test_folder_apis_are_explicit_and_do_not_capture_during_setup(bridge):
    item, window, files = bridge
    assert {'choose_folder', 'clipboard_folders', 'clipboard_has_folders'} <= {
        function.__name__ for function in window.exposed}
    assert not files.calls and not window.dialog_calls
    with pytest.raises(TypeError):
        item.choose_folder('untrusted-absolute-path')
    with pytest.raises(TypeError):
        item.clipboard_folders('untrusted-absolute-path')


def test_folder_picker_only_uses_native_result(bridge, tmp_path):
    item, window, files = bridge
    window.selection = (str(tmp_path),)
    result = item.choose_folder()
    assert result['kind'] == 'folder'
    assert files.calls == [('folder', tmp_path)]
    assert window.dialog_calls == [((20,), {'allow_multiple': False})]


def test_cancelled_native_picker_does_not_read_files(bridge):
    item, window, files = bridge
    assert item.choose_folder() == {'kind': 'empty', 'items': []}
    assert not files.calls


@pytest.mark.parametrize('bad', [['relative'], [1], ['a', 'b'], 'not-a-selection'])
def test_malformed_native_selection_is_rejected(bridge, bad):
    item, window, files = bridge
    window.selection = bad
    # Relative strings are rejected by ClipboardFiles; mock that boundary here.
    if bad == ['relative']:
        def reject(root, **kwargs):
            assert root == Path('relative')
            raise ValueError('absolute path required')
        files.snapshot_folder = reject
    with pytest.raises(ValueError):
        item.choose_folder()
    assert not files.calls


@pytest.mark.parametrize('boundary', ['origin', 'path', 'closed', 'generation'])
def test_page_change_during_dialog_prevents_source_capture(bridge, tmp_path, boundary):
    item, window, files = bridge
    window.selection = (str(tmp_path),)
    def change():
        if boundary == 'origin':
            window.url = 'https://untrusted.invalid/workspace'
        elif boundary == 'path':
            window.url = 'http://127.0.0.1:1234/other'
        elif boundary == 'closed':
            item._closed.set()
        else:
            item._page_generation += 1
    window.on_dialog = change
    with pytest.raises(ValueError):
        item.choose_folder()
    assert not files.calls and not files.closed


@pytest.mark.parametrize('operation', ['choose_folder', 'clipboard_folders'])
@pytest.mark.parametrize('boundary', ['origin', 'path', 'closed', 'generation'])
def test_late_folder_result_releases_only_own_handles(bridge, tmp_path, operation, boundary):
    item, window, files = bridge
    window.selection = (str(tmp_path),)
    def change():
        if boundary == 'origin':
            window.url = 'https://untrusted.invalid/workspace'
        elif boundary == 'path':
            window.url = 'http://127.0.0.1:1234/other'
        elif boundary == 'closed':
            item._closed.set()
        else:
            item._page_generation += 1
    files.on_snapshot = change
    with pytest.raises(ValueError):
        getattr(item, operation)()
    assert files.released == ['own-new'] and not files.closed


def test_folder_clipboard_capture_uses_existing_manager(bridge):
    item, window, files = bridge
    assert item.clipboard_folders()['folders'][0]['items'] == [{'id': 'own-new'}]
    assert files.calls == [('clipboard',)] and not window.dialog_calls


def test_folder_metadata_probe_is_guarded_and_does_not_start_capture(bridge, monkeypatch):
    item, window, files = bridge
    calls = []
    monkeypatch.setattr(D, 'has_clipboard_folders', lambda: calls.append(True) or True)
    assert item.clipboard_has_folders() is True
    assert calls == [True] and not files.calls
    window.url = 'https://untrusted.invalid/workspace'
    with pytest.raises(ValueError):
        item.clipboard_has_folders()
    assert calls == [True]


def test_folder_metadata_probe_rejects_late_page_change(bridge, monkeypatch):
    item, window, files = bridge
    def probe():
        item._page_generation += 1
        return True
    monkeypatch.setattr(D, 'has_clipboard_folders', probe)
    with pytest.raises(ValueError):
        item.clipboard_has_folders()


def test_bad_origin_prevents_native_dialog_or_snapshot(bridge):
    item, window, files = bridge
    window.url = 'https://untrusted.invalid/workspace'
    with pytest.raises(ValueError):
        item.choose_folder()
    with pytest.raises(ValueError):
        item.clipboard_folders()
    assert not window.dialog_calls and not files.calls


def test_only_one_native_folder_picker_can_be_open(bridge):
    item, window, files = bridge
    item._folder_picker_lock.acquire()
    try:
        with pytest.raises(ValueError, match='already in progress'):
            item.choose_folder()
    finally:
        item._folder_picker_lock.release()
    assert not window.dialog_calls
    assert item.choose_folder()['kind'] == 'empty'


def test_legacy_supported_pywebview_folder_constant(bridge, monkeypatch):
    item, window, files = bridge
    monkeypatch.setitem(sys.modules, 'webview', SimpleNamespace(FOLDER_DIALOG=20))
    assert item.choose_folder()['kind'] == 'empty'
    assert window.dialog_calls == [((20,), {'allow_multiple': False})]
