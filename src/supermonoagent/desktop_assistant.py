"""Optional desktop-only assistant, isolated from terminal transport."""
from pathlib import Path
from urllib.parse import urlsplit

from .mylib_assistant import SkillIndex
from .assistant_preferences import AssistantPreferences


class DesktopAssistant:
    def __init__(self, window, url, root, *, preferences_path=None):
        self._window = window
        self._origin = urlsplit(url)
        self._index = SkillIndex(root)
        self._preferences = AssistantPreferences(preferences_path or Path.home() / '.supermonoagent' / 'assistant-preferences.json')
        self._script = (Path(__file__).parent / "static" / "mylib-assistant.js").read_text(encoding="utf-8")
        window.expose(self.recommend_skills, self.get_assistant_preferences, self.save_assistant_preferences)
        window.events.loaded += self._loaded

    def _allowed(self):
        current = urlsplit(self._window.get_current_url() or "")
        return (current.scheme, current.netloc) == (self._origin.scheme, self._origin.netloc) and current.path.rstrip("/") == "/workspace"

    def recommend_skills(self, context):
        if not self._allowed():
            raise ValueError("Assistant unavailable on this page")
        return self._index.recommend(context)

    def get_assistant_preferences(self):
        if not self._allowed():
            raise ValueError('Assistant settings unavailable on this page')
        return self._preferences.get()

    def save_assistant_preferences(self, preferences):
        if not self._allowed():
            raise ValueError('Assistant settings unavailable on this page')
        return self._preferences.save(preferences)

    def _loaded(self, *_):
        if self._allowed():
            self._window.run_js(self._script)
