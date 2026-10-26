"""Optional desktop-only assistant, isolated from terminal transport."""
from pathlib import Path
from urllib.parse import urlsplit

from .mylib_assistant import SkillIndex
from .assistant_preferences import AssistantPreferences
from .assistant_service import AssistantService


class DesktopAssistant:
    def __init__(self, window, url, root, *, preferences_path=None, remote_client=None):
        self._window = window
        self._origin = urlsplit(url)
        self._index = SkillIndex(root)
        if remote_client is not None:
            from .central_skill_index import CentralSkillIndex
            self._index = CentralSkillIndex(self._index, remote_client)
        self._closed = False
        self._preferences = AssistantPreferences(preferences_path or Path.home() / '.supermonoagent' / 'assistant-preferences.json')
        self._models = AssistantService(self._preferences, self._index)
        static = Path(__file__).parent / 'static'
        self._script = (static / 'mylib-assistant.js').read_text(encoding='utf-8')
        if (static / 'assistant-chat.js').is_file():
            self._script += '\n;\n' + (static / 'assistant-chat.js').read_text(encoding='utf-8')
        window.expose(self.recommend_skills, self.get_assistant_preferences, self.save_assistant_preferences,
            self.set_assistant_api_key, self.assistant_chat, self.cancel_assistant_chat, self.read_assistant_skill)
        window.events.loaded += self._loaded
        window.events.closed += self._close

    def _allowed(self):
        current = urlsplit(self._window.get_current_url() or "")
        return not self._closed and (current.scheme, current.netloc) == (self._origin.scheme, self._origin.netloc) and current.path.rstrip("/") == "/workspace"

    def recommend_skills(self, context):
        if not self._allowed():
            raise ValueError("Assistant unavailable on this page")
        result = self._index.recommend(context)
        if not self._allowed():
            raise ValueError("Assistant page changed")
        return result

    def read_assistant_skill(self, request):
        if not self._allowed():
            raise ValueError("Assistant unavailable on this page")
        reader = getattr(self._index, 'read_skill', None)
        if reader is None:
            raise ValueError("Central library is unavailable")
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
        result = self._models.save_preferences(preferences)
        return {**result, **self._models.key_status()}

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
        # Closing the catalog wakes recommendations waiting for its refresh.
        # Cancel first so those waiters cannot proceed to a model request.
        self._models.close()
        close = getattr(self._index, 'close', None)
        if close is not None:
            close()

    def _loaded(self, *_):
        if self._allowed():
            self._window.run_js(self._script)
