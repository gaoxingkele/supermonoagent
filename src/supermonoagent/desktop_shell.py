"""Desktop shell bridge: opens user-activated links in the local browser."""
from urllib.parse import urlsplit
import webbrowser


class DesktopShell:
    def __init__(self, window, url):
        self._window, self._origin = window, urlsplit(url)
        window.expose(self.open_external)

    def _allowed(self):
        current = urlsplit(self._window.get_current_url() or '')
        return ((current.scheme, current.netloc) == (self._origin.scheme, self._origin.netloc)
                and current.path.rstrip('/') == '/workspace')

    def open_external(self, url):
        if not self._allowed():
            raise ValueError('External browser unavailable on this page')
        if not isinstance(url, str):
            raise ValueError('URL must be a string')
        parsed = urlsplit(url.strip())
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            raise ValueError('Only absolute http(s) URLs can be opened')
        return bool(webbrowser.open(parsed.geturl(), new=2))
