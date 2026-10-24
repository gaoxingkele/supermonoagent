import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from supermonoagent.assistant_preferences import AssistantPreferences, DEFAULTS, MAX_BYTES


def settings(**changes):
    return {**DEFAULTS, **changes}


def test_missing_settings_return_default_without_creating_parent(tmp_path):
    path = tmp_path / "not-created" / "preferences.json"
    preferences = AssistantPreferences(path)
    assert preferences.get() == {"preferences": DEFAULTS, "status": "default"}
    assert not path.parent.exists()
    returned = preferences.get()
    returned["preferences"]["provider"] = "changed"
    assert preferences.get()["preferences"]["provider"] == "unconfigured"


def test_roundtrip_and_provider_change(tmp_path):
    path = tmp_path / "settings" / "preferences.json"
    store = AssistantPreferences(path)
    api = settings(provider="api", model="fixture-model", endpoint="https://api.example.test/v1",
                   auto_context=True, max_context_chars=32768)
    assert store.save(api) == {"preferences": api, "status": "saved"}
    assert AssistantPreferences(path).get() == {"preferences": api, "status": "saved"}
    assert json.loads(path.read_text(encoding="utf-8")) == {"schema": 1, "preferences": api}
    codex = settings(provider="codex", model="fixture-codex")
    assert store.save(codex)["preferences"] == codex
    assert store.get()["preferences"]["endpoint"] == ""
    assert store.save(settings())["preferences"] == DEFAULTS
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize("endpoint", [
    "http://localhost:11434/v1", "http://127.0.0.1:8000/v1", "http://[::1]:8000/v1",
    "https://remote.example.test/v1", "https://127.0.0.1/v1",
])
def test_allowed_endpoints(tmp_path, endpoint):
    store = AssistantPreferences(tmp_path / "preferences.json")
    value = settings(provider="api", model="fixture", endpoint=endpoint)
    assert store.save(value)["preferences"] == value


@pytest.mark.parametrize("endpoint", [
    "http://remote.example.test/v1", "http://localhost.evil.test/v1", "http://127.1/v1",
    "http://2130706433/v1", "http://0x7f000001/v1", "http://[::1%25zone]/v1",
    "https://user:password@api.example.test/v1", "https://@api.example.test/v1",
    "https://api.example.test/v1?api_key=private", "https://api.example.test/v1?",
    "https://api.example.test/v1#private", "https://api.example.test/v1#",
    "https://api.example.test/\nprivate", "https://api.example.test/\tprivate",
    "https://api.example.test/a b", "https://api.example.test/\x7f", "https://api.example.test\\path",
    "file:///private", "javascript:private", "https:///missing", "http://[broken",
    "https://api.example.test:0/v1", "https://api.example.test:65536/v1",
])
def test_bad_endpoints_do_not_change_saved_preferences(tmp_path, endpoint):
    path = tmp_path / "preferences.json"
    store = AssistantPreferences(path)
    store.save(settings())
    before = path.read_bytes()
    with pytest.raises(ValueError, match="preferences"):
        store.save(settings(provider="api", model="fixture", endpoint=endpoint))
    assert path.read_bytes() == before


@pytest.mark.parametrize("changes", [
    {"provider": "other"}, {"provider": []}, {"model": "x" * 129}, {"model": "bad\nmodel"},
    {"model": 1}, {"endpoint": 1}, {"endpoint": "https://not-for-codex.test", "provider": "codex"},
    {"provider": "api", "model": "", "endpoint": "https://api.example.test"},
    {"provider": "api", "model": "   ", "endpoint": "https://api.example.test"},
    {"auto_context": 1}, {"auto_context": "true"},
    {"max_context_chars": True}, {"max_context_chars": 511}, {"max_context_chars": 32769},
    {"max_context_chars": "8192"}, {"api_key": "private"}, {"token": "private"},
])
def test_invalid_fields_are_rejected_before_any_write(tmp_path, changes):
    path = tmp_path / "not-created" / "preferences.json"
    with pytest.raises(ValueError):
        AssistantPreferences(path).save(settings(**changes))
    assert not path.parent.exists()


@pytest.mark.parametrize("raw", [
    b"not-json", b"\xff", b"[]", b"{}", b"a" * (MAX_BYTES + 1),
    b'{"schema":true,"preferences":{}}',
    b'{"schema":1,"schema":1,"preferences":{}}',
    json.dumps({"schema": 2, "preferences": DEFAULTS}).encode(),
    json.dumps({"schema": 1, "preferences": DEFAULTS, "api_key": "private"}).encode(),
    json.dumps({"schema": 1, "preferences": {**DEFAULTS, "api_key": "private"}}).encode(),
])
def test_invalid_disk_settings_are_preserved_and_return_defaults(tmp_path, raw):
    path = tmp_path / "preferences.json"
    path.write_bytes(raw)
    assert AssistantPreferences(path).get() == {"preferences": DEFAULTS, "status": "invalid"}
    assert path.read_bytes() == raw


def test_read_permission_error_is_sanitized_and_does_not_write(tmp_path, monkeypatch):
    path = tmp_path / "preferences.json"
    path.write_bytes(b"preserved")
    original = Path.open

    def denied(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("secret-location-and-credential")
        return original(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    result = AssistantPreferences(path).get()
    assert result == {"preferences": DEFAULTS, "status": "invalid"}
    assert "secret" not in str(result)
    monkeypatch.setattr(Path, "open", original)
    assert path.read_bytes() == b"preserved"


def test_replace_failure_cleans_only_its_own_unique_temp(tmp_path, monkeypatch):
    from supermonoagent import assistant_preferences
    path = tmp_path / "preferences.json"
    store = AssistantPreferences(path)
    store.save(settings())
    before = path.read_bytes()
    unrelated = tmp_path / ".preferences.json.someone-else.tmp"
    unrelated.write_bytes(b"other work")

    def denied(*_args):
        raise PermissionError("private-absolute-path")

    monkeypatch.setattr(assistant_preferences.os, "replace", denied)
    with pytest.raises(ValueError) as error:
        store.save(settings(provider="codex"))
    assert "private-absolute-path" not in str(error.value)
    assert path.read_bytes() == before
    assert unrelated.read_bytes() == b"other work"
    assert sorted(tmp_path.glob("*.tmp")) == [unrelated]


def test_full_field_object_required_and_context_boundary_accepted(tmp_path):
    store = AssistantPreferences(tmp_path / "preferences.json")
    with pytest.raises(ValueError):
        store.save({"provider": "codex"})
    value = settings(provider="codex", model="x" * 128, max_context_chars=512)
    assert store.save(value)["preferences"] == value


def test_threaded_saves_are_complete_valid_envelopes(tmp_path):
    store = AssistantPreferences(tmp_path / "preferences.json")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: store.save(settings(provider="codex", model="fixture-" + str(i))), range(16)))
    assert all(result["status"] == "saved" for result in results)
    saved = store.get()
    assert saved["status"] == "saved" and saved["preferences"]["model"].startswith("fixture-")
    assert not list(tmp_path.glob("*.tmp"))
