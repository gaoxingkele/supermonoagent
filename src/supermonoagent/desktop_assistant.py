"""Optional desktop-only assistant, isolated from terminal transport."""
from pathlib import Path
from urllib.parse import urlsplit

from .mylib_assistant import SkillIndex
from .assistant_preferences import AssistantPreferences, DEFAULT_LIBRARY_ROOT
from .assistant_service import AssistantService
from .mylib_local import LocalLibrary, readable_library_root


class DesktopAssistant:
    def __init__(self, window, url, root, *, preferences_path=None, remote_client=None):
        self._window = window
        self._origin = urlsplit(url)
        self._remote_client = remote_client
        self._closed = False
        self._preferences = AssistantPreferences(preferences_path or Path.home() / '.supermonoagent' / 'assistant-preferences.json')
        saved = self._preferences.get()
        self._library_root = saved['preferences']['library_root'] if saved['status'] == 'saved' else root
        self._index = SkillIndex(self._library_root)
        if remote_client is not None:
            from .central_skill_index import CentralSkillIndex
            self._index = CentralSkillIndex(self._index, remote_client)
        self._models = AssistantService(self._preferences, self._index)
        self._library = LocalLibrary(self._library_root, self._preferences.path.parent / 'mylib-local')
        static = Path(__file__).parent / 'static'
        self._script = (static / 'mylib-assistant.js').read_text(encoding='utf-8')
        if (static / 'assistant-chat.js').is_file():
            self._script += '\n;\n' + (static / 'assistant-chat.js').read_text(encoding='utf-8')
        window.expose(self.recommend_skills, self.get_assistant_preferences, self.save_assistant_preferences,
            self.set_assistant_api_key, self.assistant_chat, self.cancel_assistant_chat, self.read_assistant_skill,
            self.library_status, self.pull_library, self.commit_library_preview, self.commit_library,
            self.run_library_evolution, self.apply_library_evolution)
        window.events.loaded += self._loaded
        window.events.closed += self._close

    def _allowed(self):
        current = urlsplit(self._window.get_current_url() or "")
        return not self._closed and (current.scheme, current.netloc) == (self._origin.scheme, self._origin.netloc) and current.path.rstrip("/") == "/workspace"

    def recommend_skills(self, context):
        if not self._allowed():
            raise ValueError("Assistant unavailable on this page")
        result = self._index.recommend(context)
        result['library'] = {'kind': 'local', 'root': self._library_root}
        if not self._allowed():
            raise ValueError("Assistant page changed")
        return result

    def read_assistant_skill(self, request):
        if not self._allowed():
            raise ValueError("Assistant unavailable on this page")
        reader = getattr(self._index, 'read_skill', None)
        if reader is None:
            raise ValueError("Local skill preview is unavailable")
        result = reader(request)
        if not self._allowed():
            raise ValueError("Assistant page changed")
        return result

    def get_assistant_preferences(self):
        if not self._allowed():
            raise ValueError('Assistant settings unavailable on this page')
        return {**self._preferences.get(), **self._models.key_status()}

    def save_assistant_preferences(self, preferences):
        if not self._allowed():
            raise ValueError('Assistant settings unavailable on this page')
        if isinstance(preferences, dict):
            previous = self._preferences.get()['preferences']['library_root']
            candidate = preferences.get('library_root', DEFAULT_LIBRARY_ROOT)
            if candidate != previous:
                preferences = {**preferences, 'library_root': str(readable_library_root(candidate))}
        result = self._models.save_preferences(preferences)
        root = result['preferences']['library_root']
        if root != self._library_root:
            self._library.close()
            close = getattr(self._index, 'close', None)
            if close is not None:
                close()
            self._library_root = root
            self._index = SkillIndex(root)
            if self._remote_client is not None:
                from .central_skill_index import CentralSkillIndex
                self._index = CentralSkillIndex(self._index, self._remote_client)
            self._models.index = self._index
            self._library = LocalLibrary(root, self._preferences.path.parent / 'mylib-local')
        return {**result, **self._models.key_status()}

    def library_status(self):
        if not self._allowed():
            raise ValueError('Library settings unavailable on this page')
        result = self._library.status()
        if not self._allowed():
            raise ValueError('Assistant page changed')
        return result

    def pull_library(self):
        if not self._allowed():
            raise ValueError('Library settings unavailable on this page')
        result = self._library.pull()
        self._index.reload()
        if not self._allowed():
            raise ValueError('Assistant page changed')
        return result

    def commit_library_preview(self):
        if not self._allowed():
            raise ValueError('Library settings unavailable on this page')
        result = self._library.commit_preview()
        if not self._allowed():
            raise ValueError('Assistant page changed')
        return result

    def commit_library(self, request):
        if not self._allowed():
            raise ValueError('Library settings unavailable on this page')
        if not isinstance(request, dict):
            raise ValueError('Invalid commit request')
        result = self._library.commit_and_push(request.get('fingerprint'), request.get('message', 'Update local skills'))
        self._index.reload()
        if not self._allowed():
            raise ValueError('Assistant page changed')
        return result

    def run_library_evolution(self, request):
        if not self._allowed():
            raise ValueError('Library evolution unavailable on this page')
        result = self._library.run_evolution(request)
        if not self._allowed():
            raise ValueError('Assistant page changed')
        return result

    def apply_library_evolution(self, request):
        if not self._allowed():
            raise ValueError('Library evolution unavailable on this page')
        if not isinstance(request, dict):
            raise ValueError('Invalid evolution request')
        result = self._library.apply_evolution(request.get('suggestion_id'))
        self._index.reload()
        if not self._allowed():
            raise ValueError('Assistant page changed')
        return result

    def set_assistant_api_key(self, key):
        if not self._allowed():
            raise ValueError('Assistant credentials unavailable on this page')
        return self._models.set_api_key(key)

    def assistant_chat(self, request):
        if not self._allowed():
            raise ValueError('Assistant unavailable on this page')
        result = self._models.chat(request)
        if not self._allowed():
            raise ValueError('Assistant page changed')
        return result

    def cancel_assistant_chat(self, request_id=None):
        if not self._allowed():
            raise ValueError('Assistant unavailable on this page')
        self._models.cancel(request_id)

    def _close(self, *_):
        self._closed = True
        self._library.close()
        close = getattr(self._index, 'close', None)
        if close is not None:
            close()
        self._models.close()

    def _loaded(self, *_):
        if self._allowed():
            self._window.run_js(self._script)
