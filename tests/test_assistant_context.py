import json
from pathlib import Path
import subprocess

import pytest

from supermonoagent.assistant_context import PAGE_LIMIT, read_project_context


def project(tmp_path):
    root = tmp_path / "project"
    wiki = root / ".metarsi" / "wiki"
    wiki.mkdir(parents=True)
    (root / ".metarsi" / "constitution.json").write_text("{}", encoding="utf-8")
    return root, wiki


def library(tmp_path):
    root = tmp_path / "library"
    (root / "skill-runtime").mkdir(parents=True)
    source = root / "skills" / "review"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: fixture-review\ndescription: review\n---\n", encoding="utf-8")
    (root / "skill-runtime" / "manifest.json").write_text(json.dumps({"skills": [
        {"source": "../skills/review"}, {"source": "../skills/review"}]}), encoding="utf-8")
    return root


def test_reads_fixed_evidence_pages_and_inventory_without_writing(tmp_path):
    root, wiki = project(tmp_path)
    for name in ("README.md", "negatives.md", "trials.md"):
        (wiki / name).write_bytes(("# " + name + "\n证据\n").encode("utf-8"))
    (wiki / "private.txt").write_text("never returned", encoding="utf-8")
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    result = read_project_context(root, library(tmp_path))
    assert result["project"] == str(root.resolve())
    assert result["project_truncated"] is False
    assert result["project_source"] == "session-startup-directory"
    assert "cd" in result["note"]
    assert result["ledger"]["enabled"] and result["ledger"]["status"] == "ready"
    assert set(result["ledger"]["pages"]) == {"README.md", "negatives.md", "trials.md"}
    assert result["ledger"]["pages"]["README.md"]["text"] == "# README.md\n证据\n"
    assert not result["ledger"]["pages"]["README.md"]["truncated"]
    assert result["inventory"]["skill_names"] == ["fixture-review"]
    assert result["inventory"]["indexed"] == 1
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


def test_missing_directories_and_uninitialized_ledger_are_explicit(tmp_path):
    missing = read_project_context(tmp_path / "missing", tmp_path / "no-library")
    assert missing["project_status"] == "missing"
    assert missing["inventory"]["status"] == "missing"
    assert missing["ledger"]["enabled"] is False
    root = tmp_path / "existing"
    root.mkdir()
    result = read_project_context(root, root)
    assert result["ledger"] == {"enabled": False, "status": "not_enabled", "pages": {}}
    assert result["inventory"]["status"] == "empty"
    assert not (root / ".metarsi").exists()


@pytest.mark.parametrize("value", [None, "", "\0"])
def test_invalid_project_path_is_unavailable(tmp_path, value):
    result = read_project_context(value, tmp_path / "missing")
    assert result["project_status"] == "unavailable"
    assert not result["ledger"]["enabled"]


def test_bounded_bytes_and_invalid_utf8(tmp_path):
    root, wiki = project(tmp_path)
    (wiki / "README.md").write_bytes(b"a" * (PAGE_LIMIT + 100))
    (wiki / "negatives.md").write_bytes(b"\xff evidence")
    result = read_project_context(root, tmp_path / "missing")
    pages = result["ledger"]["pages"]
    assert pages["README.md"]["text"] == "a" * PAGE_LIMIT
    assert pages["README.md"]["truncated"]
    assert pages["negatives.md"]["text"] == "\ufffd evidence"
    assert pages["trials.md"]["status"] == "missing"


def test_permission_error_is_sanitized(tmp_path, monkeypatch):
    root, wiki = project(tmp_path)
    (wiki / "README.md").write_text("evidence", encoding="utf-8")
    original = Path.open

    def denied(path, *args, **kwargs):
        if path.name == "README.md":
            raise PermissionError("secret-path-and-token")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    result = read_project_context(root, tmp_path / "missing")
    assert result["ledger"]["status"] == "partial"
    assert result["ledger"]["pages"]["README.md"]["status"] == "unavailable"
    assert "secret-path-and-token" not in str(result)


def test_inventory_is_bounded(tmp_path, monkeypatch):
    from supermonoagent import assistant_context
    root = tmp_path / "library"
    root.mkdir()

    class Index:
        def __init__(self, _root):
            self.cards = [{"name": "skill-" + str(i)} for i in range(300)]

    monkeypatch.setattr(assistant_context, "SkillIndex", Index)
    result = read_project_context(tmp_path / "missing", root)["inventory"]
    assert result["indexed"] == 300
    assert len(result["skill_names"]) == 256
    assert result["truncated"]


def test_project_display_is_bounded_without_changing_read_root(monkeypatch):
    from supermonoagent import assistant_context
    long_root = Path("C:/") / ("x" * 5000)
    monkeypatch.setattr(assistant_context, "_canonical", lambda _value: long_root)
    monkeypatch.setattr(assistant_context, "_inventory", lambda _value: {})
    checked = []

    def missing(path, *args, **kwargs):
        checked.append(path)
        raise FileNotFoundError

    monkeypatch.setattr(Path, "stat", missing)
    result = read_project_context("project", "library")
    assert len(result["project"]) == 4096
    assert result["project_truncated"]
    assert checked == [long_root]


def test_root_permission_errors_are_unavailable_not_missing(tmp_path, monkeypatch):
    root, _wiki = project(tmp_path)
    original = Path.stat

    def denied(path, *args, **kwargs):
        if path == root:
            raise PermissionError("do not return this")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)
    result = read_project_context(root, root)
    assert result["project_status"] == "unavailable"
    assert result["inventory"]["status"] == "unavailable"
    assert "do not return this" not in str(result)


def test_page_symlink_cannot_escape_project(tmp_path):
    root, wiki = project(tmp_path)
    outside = tmp_path / "private.md"
    outside.write_text("outside private data", encoding="utf-8")
    try:
        (wiki / "README.md").symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    result = read_project_context(root, tmp_path / "missing")
    assert result["ledger"]["pages"]["README.md"]["status"] == "unavailable"
    assert "outside private data" not in str(result)


@pytest.mark.skipif(__import__('os').name != 'nt', reason="Windows junction semantics")
def test_wiki_junction_cannot_escape_project(tmp_path):
    root = tmp_path / "project"
    meta = root / ".metarsi"
    meta.mkdir(parents=True)
    (meta / "constitution.json").write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "README.md").write_text("private outside", encoding="utf-8")
    link = meta / "wiki"
    completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
    if completed.returncode:
        pytest.skip("Junction creation unavailable")
    try:
        result = read_project_context(root, tmp_path / "missing")
        assert result["ledger"]["pages"]["README.md"]["status"] == "unavailable"
        assert "private outside" not in str(result)
    finally:
        link.rmdir()


@pytest.mark.skipif(__import__('os').name != 'nt', reason="Windows junction semantics")
def test_external_constitution_is_not_followed_through_meta_junction(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "constitution.json").write_text("{}", encoding="utf-8")
    link = root / ".metarsi"
    completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
    if completed.returncode:
        pytest.skip("Junction creation unavailable")
    try:
        result = read_project_context(root, tmp_path / "missing")
        assert result["ledger"] == {"enabled": False, "status": "unavailable", "pages": {}}
    finally:
        link.rmdir()
