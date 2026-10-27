"""Desktop clipboard bridge; reads content only after an explicit paste action."""
from pathlib import Path
import threading
from urllib.parse import urlsplit

from .windows_clipboard import (ClipboardFiles, has_clipboard_sources,
    has_clipboard_folders, read_clipboard_text, write_clipboard_text)


class DesktopFiles:
    def __init__(self, window, url, *, file_config_provider=None):
        self._window, self._origin = window, urlsplit(url)
        self._files = ClipboardFiles()
        self._file_config_provider = file_config_provider
        self._closed = threading.Event()
        self._thread = None
        self._page_generation = 0
        self._retire_thread = None
        self._folder_picker_lock = threading.Lock()
        self._script = (Path(__file__).parent / 'static' / 'clipboard-files.js').read_text(encoding='utf-8')
        window.expose(self.clipboard_files, self.clipboard_chunk, self.clipboard_release, self.clipboard_has_files,
                      self.file_exchange_config, self.clipboard_read_text, self.clipboard_write_text,
                      self.choose_folder, self.clipboard_folders, self.clipboard_has_folders)
        window.events.loaded += self._loaded
        window.events.closed += self._close

    def _allowed(self):
        current = urlsplit(self._window.get_current_url() or '')
        return ((current.scheme, current.netloc) == (self._origin.scheme, self._origin.netloc)
                and current.path.rstrip('/') == '/workspace')

    def _check(self):
        if self._closed.is_set() or not self._allowed():
            raise ValueError('Clipboard unavailable on this page')

    def clipboard_files(self):
        self._check()
        retirement = self._retire_thread
        if retirement is not None and retirement.is_alive():
            retirement.join()
        self._check()
        generation = self._page_generation
        result = self._files.snapshot(cancelled=lambda: self._closed.is_set() or generation != self._page_generation)
        if self._closed.is_set() or generation != self._page_generation:
            self._files.close()
            raise ValueError('Clipboard page changed during snapshot')
        return result

    def file_exchange_config(self):
        self._check()
        generation = self._page_generation
        from .file_companion_manager import public_config
        value = self._file_config_provider() if self._file_config_provider is not None else {
            'enabled': False, 'ready': False, 'reason': 'not_configured'}
        result = public_config(value)
        self._check()
        if generation != self._page_generation:
            raise ValueError('File exchange page changed during configuration')
        return result

    def clipboard_has_files(self):
        self._check()
        return has_clipboard_sources()

    def clipboard_has_folders(self):
        """Explicit paste-time metadata only; no directory polling/beacon."""
        self._check()
        generation = self._page_generation
        result = has_clipboard_folders()
        self._check()
        if generation != self._page_generation:
            raise ValueError('Clipboard page changed during folder check')
        return result

    def _release_folder_result(self, result):
        folders = result.get('folders', []) if isinstance(result, dict) else []
        if isinstance(result, dict) and result.get('kind') == 'folder':
            folders = [result]
        for folder in folders:
            for item in folder.get('items', []):
                try:
                    self._files.release(item['id'])
                except (OSError, ValueError):
                    pass  # Failed closes remain reachable in the shared pool.

    def _folder_capture(self, capture):
        self._check()
        retirement = self._retire_thread
        if retirement is not None and retirement.is_alive():
            retirement.join()
        self._check()
        generation = self._page_generation
        def cancelled():
            return self._closed.is_set() or generation != self._page_generation
        result = None
        try:
            result = capture(cancelled)
            self._check()
            if cancelled():
                raise ValueError('Clipboard page changed during folder snapshot')
            return result
        except BaseException:
            self._release_folder_result(result)
            raise

    def clipboard_folders(self):
        """Capture folder paths from the current OS clipboard, never JS paths."""
        return self._folder_capture(lambda cancelled:
            self._files.snapshot_clipboard_folders(cancelled=cancelled))

    def choose_folder(self):
        """Open the native picker; the webpage cannot supply a local path."""
        def choose(cancelled):
            import webview
            kind = getattr(getattr(webview, 'FileDialog', None), 'FOLDER', None)
            if kind is None:
                kind = webview.FOLDER_DIALOG  # Older supported pywebview releases.
            selected = self._window.create_file_dialog(kind,
                                                       allow_multiple=False)
            self._check()
            if cancelled():
                raise ValueError('Clipboard page changed during folder selection')
            if not selected:
                return {'kind': 'empty', 'items': []}
            if (not isinstance(selected, (tuple, list)) or len(selected) != 1
                    or not isinstance(selected[0], str) or len(selected[0]) > 32767):
                raise ValueError('Invalid native folder selection')
            return self._files.snapshot_folder(Path(selected[0]), cancelled=cancelled)
        if not self._folder_picker_lock.acquire(blocking=False):
            raise ValueError('Folder selection is already in progress')
        try:
            return self._folder_capture(choose)
        finally:
            self._folder_picker_lock.release()

    def _clipboard_text(self, operation, *args):
        try:
            self._check()
            generation = self._page_generation
            result = operation(*args)
            self._check()
            if generation != self._page_generation:
                raise ValueError()
            return result
        except Exception:
            raise ValueError('Clipboard text unavailable') from None

    def clipboard_read_text(self):
        """Read only for an explicit user paste; never poll text contents."""
        return self._clipboard_text(read_clipboard_text)

    def clipboard_write_text(self, text):
        """Copy caller-selected user text after verifying the workspace page."""
        return self._clipboard_text(write_clipboard_text, text)

    def clipboard_chunk(self, file_id, offset):
        self._check()
        generation = self._page_generation
        result = self._files.read(file_id, offset)
        self._check()
        if generation != self._page_generation:
            raise ValueError('Clipboard page changed during read')
        return result

    def clipboard_release(self, file_id):
        self._check()
        self._files.release(file_id)

    def _loaded(self, *_):
        if not self._allowed():
            return
        self._page_generation += 1
        self._retire_sources()
        self._window.run_js(self._script)
        if self._thread is None:
            self._thread = threading.Thread(target=self._beacon, name='clipboard-formats', daemon=True)
            self._thread.start()

    def _beacon(self):
        previous = None
        while not self._closed.is_set():
            try:
                if self._allowed():
                    value = 'true' if has_clipboard_sources() else 'false'
                    current = (self._page_generation, value)
                    if current != previous:
                        self._window.run_js('window.supermonoClipboardHasFiles=' + value)
                        previous = current
            except Exception:
                # Optional format awareness must not interrupt the terminal.
                pass
            self._closed.wait(.2)

    def _close(self, *_):
        self._closed.set()
        self._page_generation += 1
        self._retire_sources()

    def _retire_sources(self):
        if self._retire_thread is None or not self._retire_thread.is_alive():
            self._retire_thread = threading.Thread(target=self._files.close, name='clipboard-release', daemon=True)
            self._retire_thread.start()
