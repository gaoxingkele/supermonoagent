import json

import pytest

from supermonoagent.mylib_assistant import SkillIndex


def library(tmp_path):
    (tmp_path / "skill-runtime").mkdir()
    folder = tmp_path / "skills" / "review"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: review\ndescription: 检查专利说明书 patent review\n---\n", encoding="utf-8")
    (tmp_path / "skill-runtime" / "manifest.json").write_text(json.dumps({"skills": [
        {"source": "../skills/review"}, {"source": "../skills/review"},
        {"source": "../../outside"}]}), encoding="utf-8")
    return SkillIndex(tmp_path)


def test_curated_matching_and_no_unrelated_recommendations(tmp_path):
    index = library(tmp_path)
    assert index.recommend("专利说明书")['skills'][0]['name'] == "review"
    assert index.recommend("PATENT")['indexed'] == 1
    assert index.recommend("network disconnect")['skills'] == []


def test_missing_library_is_available_as_empty(tmp_path):
    result = SkillIndex(tmp_path / "missing").recommend("test")
    assert result == {"mode": "local-keywords", "skills": [], "indexed": 0, "available": False, "evolution": []}


def test_external_source_rejected(tmp_path):
    index = library(tmp_path)
    with pytest.raises(ValueError):
        index._read("../outside/SKILL.md")


def test_evolution_suggestion_requires_observed_failures_not_prose(tmp_path):
    index = library(tmp_path)
    assert not index.recommend('There are no errors.\n0 failed\n0 failed')['evolution']
    assert not index.recommend('ValueError: one observation')['evolution']
    result = index.recommend('ValueError: failed\nError running remote compact task: Connection failed')['evolution']
    assert len(result) == 1 and result[0]['observed_lines'] == 2
    assert result[0]['status'] == 'unverified'
    assert len(result[0]['evidence']) == 2


@pytest.mark.parametrize("context,limit", [(None, 5), ("x" * 32769, 5), ("x", 0), ("x", True)],
                         ids=["not-text", "oversize", "zero-limit", "bool-limit"])
def test_request_bounds(tmp_path, context, limit):
    with pytest.raises(ValueError):
        library(tmp_path).recommend(context, limit)


def test_native_bridge_rejects_navigation_before_library_access(tmp_path):
    from types import SimpleNamespace
    from supermonoagent.desktop_assistant import DesktopAssistant

    class Event:
        def __iadd__(self, callback):
            self.callback = callback
            return self

    exposed, scripts = [], []
    current = ["http://127.0.0.1:6771/workspace"]
    window = SimpleNamespace(expose=lambda *methods: exposed.extend(methods), run_js=scripts.append,
        get_current_url=lambda: current[0], events=SimpleNamespace(loaded=Event(), closed=Event()))
    library(tmp_path)
    preferences_path = tmp_path / 'assistant-preferences.json'
    assistant = DesktopAssistant(window, current[0], tmp_path, preferences_path=preferences_path)
    assert exposed == [assistant.recommend_skills, assistant.get_assistant_preferences, assistant.save_assistant_preferences,
        assistant.set_assistant_api_key, assistant.assistant_chat, assistant.cancel_assistant_chat, assistant.read_assistant_skill]
    assert assistant.get_assistant_preferences()['status'] == 'default'
    assert not preferences_path.exists()
    assistant._loaded()
    assert len(scripts) == 1

    assert assistant.recommend_skills("patent")["indexed"] == 1
    for unsafe in ["https://example.com/workspace", "http://127.0.0.1:6772/workspace", "http://127.0.0.1:6771/"]:
        current[0] = unsafe
        with pytest.raises(ValueError):
            assistant.recommend_skills("patent")
        with pytest.raises(ValueError):
            assistant.get_assistant_preferences()
        with pytest.raises(ValueError):
            assistant.save_assistant_preferences({})
        for action in (lambda: assistant.set_assistant_api_key('fixture'), lambda: assistant.assistant_chat({'question': 'no'}), assistant.cancel_assistant_chat):
            with pytest.raises(ValueError):
                action()
        assistant._loaded()
    assert len(scripts) == 1


def test_native_skill_preview_rechecks_navigation_and_close(tmp_path):
    from types import SimpleNamespace
    from supermonoagent.desktop_assistant import DesktopAssistant

    class Event:
        def __iadd__(self, callback):
            return self

    current = ['http://127.0.0.1:6771/workspace']
    window = SimpleNamespace(expose=lambda *_: None, run_js=lambda _: None,
        get_current_url=lambda: current[0], events=SimpleNamespace(loaded=Event(), closed=Event()))
    assistant = DesktopAssistant(window, current[0], tmp_path, preferences_path=tmp_path / 'prefs.json')
    calls = []
    def read(request):
        calls.append(request)
        current[0] = 'https://other.invalid/workspace'
        return {'text': 'PRIVATE_LATE_RESPONSE'}
    assistant._index = SimpleNamespace(read_skill=read, close=lambda: calls.append('closed'))
    request = {'skill_id': 'a' * 64, 'revision': 'b' * 40}
    with pytest.raises(ValueError, match='page changed'):
        assistant.read_assistant_skill(request)
    with pytest.raises(ValueError, match='unavailable'):
        assistant.read_assistant_skill(request)
    assert calls == [request]
    current[0] = 'http://127.0.0.1:6771/workspace'
    assistant._close()
    assert calls == [request, 'closed']
    with pytest.raises(ValueError, match='unavailable'):
        assistant.read_assistant_skill(request)


def test_window_close_cancels_chat_before_index_close_wakes_catalog_waiter(tmp_path):
    import threading
    from types import SimpleNamespace
    from supermonoagent.desktop_assistant import DesktopAssistant

    class Event:
        def __iadd__(self, _callback):
            return self

    url = 'http://127.0.0.1:6771/workspace'
    window = SimpleNamespace(expose=lambda *_: None, run_js=lambda _: None,
        get_current_url=lambda: url, events=SimpleNamespace(loaded=Event(), closed=Event()))
    assistant = DesktopAssistant(window, url, tmp_path, preferences_path=tmp_path/'prefs.json')
    profile = assistant._preferences.get()['preferences']
    assistant.save_assistant_preferences({**profile, 'provider': 'api',
        'endpoint': 'https://owned-model.invalid/v1', 'model': 'owned-model'})
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    model_calls, errors = [], []

    class WaitingIndex:
        def recommend(self, _context):
            entered.set()
            assert release.wait(3)
            return {'skills': []}

        def close(self):
            # CentralSkillIndex.close wakes recommendation waiters before its
            # remote close finishes. Force that interleaving without a network.
            release.set()
            assert finished.wait(3)

    assistant._index = assistant._models.index = WaitingIndex()
    assistant._models._api_query = lambda *_args, **_kwargs: model_calls.append('started') or 'answer'

    def chat():
        try:
            assistant.assistant_chat({'question': 'owned close race'})
        except ValueError as error:
            errors.append(error)
        finally:
            finished.set()

    thread = threading.Thread(target=chat)
    thread.start()
    try:
        assert entered.wait(3)
        assistant._close()
    finally:
        release.set()
        thread.join(3)
        assistant._models.close()
    assert not thread.is_alive()
    assert errors
    assert model_calls == [], 'Closing the window must cancel before a catalog waiter can start a model'
