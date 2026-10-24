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
        assistant.set_assistant_api_key, assistant.assistant_chat, assistant.cancel_assistant_chat]
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
