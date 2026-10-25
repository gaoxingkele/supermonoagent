"""Owned Git repositories and immutable catalog fixtures; never run skill code."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from supermonoagent import mylib_catalog as C


def git(repo, *args, data=None):
    result = subprocess.run(["git", "-C", str(repo), *args], input=data,
                            capture_output=True, check=True,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0",
                                 "GIT_AUTHOR_NAME": "Catalog fixture", "GIT_AUTHOR_EMAIL": "fixture@example.test",
                                 "GIT_COMMITTER_NAME": "Catalog fixture", "GIT_COMMITTER_EMAIL": "fixture@example.test",
                                 "GIT_AUTHOR_DATE": "2026-10-25T00:00:00+00:00",
                                 "GIT_COMMITTER_DATE": "2026-10-25T00:00:00+00:00"})
    return result.stdout.decode().strip()


def repository(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    return path


def write(repo, name, body):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body.encode() if isinstance(body, str) else body)


def commit(repo, parent=None):
    git(repo, "add", "-A")
    tree = git(repo, "write-tree")
    result = git(repo, "commit-tree", tree, *(["-p", parent] if parent else []), data=b"Owned fixture revision\n")
    git(repo, "update-ref", "HEAD", result)
    return result


def skill(repo, folder="skills/alpha", name="same-name", body="Review source safely.", description="科研论文 evidence review"):
    write(repo, folder + "/SKILL.md", "---\nname: " + name + "\ndescription: " + description + "\n---\n# Review\n" + body)


def snapshot(tmp_path, repo, revision=None, name="candidate", mapping=None):
    path = tmp_path / name
    manifest = C.build_snapshot(repo, path, revision, submodule_repos=mapping)
    return path, manifest


def active_fixture(tmp_path):
    repo = repository(tmp_path / "repo")
    skill(repo)
    revision = commit(repo)
    candidate, manifest = snapshot(tmp_path, repo, revision)
    store = C.CatalogStore(tmp_path / "store")
    published = C.publish_snapshot(store, candidate, expected_revision=None)
    assert published["revision"] == revision
    return repo, revision, candidate, manifest, store


def test_empty_store_is_readonly_and_explicit(tmp_path):
    root = tmp_path / "not-created"
    store = C.CatalogStore(root)
    result = store.status()
    assert result["available"] is False and result["revision"] is result["source_revision"] is None
    with pytest.raises(C.CatalogError, match="No library"):
        store.list_skills()
    assert not root.exists()


def test_fixed_git_revision_ignores_dirty_and_untracked_data(tmp_path):
    repo = repository(tmp_path / "repo")
    skill(repo, body="Original body")
    revision = commit(repo)
    write(repo, "skills/alpha/SKILL.md", "dirty contents must not export")
    write(repo, "Graphic/untracked.txt", "untracked contents must not export")
    candidate, manifest = snapshot(tmp_path, repo, revision)
    assert manifest["revision"] == revision
    assert C.verify_snapshot(candidate) == manifest
    assert b"Original body" in (candidate / "files/skills/alpha/SKILL.md").read_bytes()
    assert "Graphic/untracked.txt" not in manifest["files"]
    assert (repo / "Graphic/untracked.txt").read_text() == "untracked contents must not export"


def test_full_catalog_pagination_stays_pinned_across_publish_and_duplicate_names(tmp_path):
    repo = repository(tmp_path / "repo")
    for folder in ["skills/alpha", "skills/beta", "unregistered/gamma"]:
        skill(repo, folder)
    write(repo, "skill-runtime/manifest.json", '{"skills":[{"name":"same-name","source":"../skills/alpha"}]}')
    first = commit(repo)
    candidate, _manifest = snapshot(tmp_path, repo, first)
    store = C.CatalogStore(tmp_path / "store")
    C.publish_snapshot(store, candidate)
    page = store.list_skills(limit=2)
    assert page["total"] == 3 and len({entry["id"] for entry in page["items"]}) == 2
    skill(repo, "new/delta", name="new-skill")
    second = commit(repo, first)
    next_candidate, _ = snapshot(tmp_path, repo, second, "candidate-two")
    C.publish_snapshot(store, next_candidate, expected_revision=first)
    old_page = store.list_skills(cursor=page["next_cursor"], limit=2)
    assert old_page["revision"] == first and len(old_page["items"]) == 1 and old_page["next_cursor"] is None
    assert store.list_skills()["total"] == 4
    found = store.search("科研论文")
    assert found["revision"] == second and found["total"] == 4
    assert store.read_skill(page["items"][0]["id"], first)["revision"] == first


def test_resources_are_scoped_and_cross_references_are_explicit_aliases(tmp_path):
    repo = repository(tmp_path / "repo")
    body = "[local](references/local.md)\n[shared](../shared/guide.md)\n[missing](missing.md)\n[excluded](token.txt)\n[web](https://user:password@example.test/doc?q=credential#part)\n"
    skill(repo, body=body)
    write(repo, "skills/alpha/references/local.md", "local")
    write(repo, "skills/shared/guide.md", "shared")
    write(repo, "skills/unrelated/private.md", "not in this skill scope")
    write(repo, "skills/alpha/token.txt", "private runtime token")
    write(repo, "skills/alpha/scripts/never.py", "raise RuntimeError('must never execute')")
    revision = commit(repo)
    candidate, manifest = snapshot(tmp_path, repo, revision)
    store = C.CatalogStore(tmp_path / "store")
    C.publish_snapshot(store, candidate)
    entry = store.list_skills()["items"][0]
    content = store.read_skill(entry["id"])
    assert store.read_resource(entry["id"], "references/local.md")["text"] == "local"
    shared = next(reference for reference in content["references"] if reference["target"] == "skills/shared/guide.md")
    assert shared["resource_path"] == "@/skills/shared/guide.md"
    assert store.read_resource(entry["id"], shared["resource_path"])["text"] == "shared"
    assert store.read_resource(entry["id"], "scripts/never.py")["text"].startswith("raise RuntimeError")
    statuses = {reference["status"] for reference in content["references"]}
    assert {"available", "missing", "excluded", "external"} <= statuses
    assert "user:password" not in json.dumps(content["references"])
    assert "skills/alpha/token.txt" not in manifest["files"]
    assert not (candidate / "files/skills/alpha/token.txt").exists()
    for bad in ["../shared/guide.md", "@/skills/unrelated/private.md", "token.txt", "/etc/passwd",
                "C:/Windows/win.ini", r"..\secret", "%2e%2e/secret", "@/../outside"]:
        with pytest.raises(C.CatalogError):
            store.read_resource(entry["id"], bad)


def test_credentials_and_tracked_links_are_explicitly_excluded(tmp_path):
    repo = repository(tmp_path / "repo")
    skill(repo)
    for name in [".env", "auth.json", "token.txt", "config/credentials.live.json"]:
        write(repo, name, "private material")
    write(repo, "skills/alpha/.env.example", "API_KEY=your-example-key")
    write(repo, "skills/alpha/private.pem", "-----BEGIN PRIVATE KEY-----\nnot real but must not deploy\n")
    revision = commit(repo)
    target_blob = git(repo, "hash-object", "-w", "--stdin", data=b"../../outside")
    git(repo, "update-index", "--add", "--cacheinfo", "120000," + target_blob + ",skills/alpha/link")
    tree = git(repo, "write-tree")
    revision = git(repo, "commit-tree", tree, "-p", revision, data=b"Owned link fixture\n")
    git(repo, "update-ref", "HEAD", revision)
    candidate, manifest = snapshot(tmp_path, repo, revision)
    excluded = {entry["path"]: entry["reason"] for entry in manifest["excluded"]}
    assert excluded["skills/alpha/link"] == "unsupported_link"
    assert excluded["skills/alpha/private.pem"] == "possible_credential_content"
    assert ".env" in excluded and "auth.json" in excluded and "token.txt" in excluded
    assert "skills/alpha/.env.example" in manifest["files"]
    assert not any(name.startswith(".git/") for name in manifest["files"])
    assert C.verify_snapshot(candidate)["revision"] == revision


def test_standard_opaque_credential_paths_and_private_containers_are_excluded(tmp_path):
    repo = repository(tmp_path / "repo")
    skill(repo)
    skill(repo, ".claude/skills/normal", name="normal-claude-skill")
    write(repo, ".claude/settings.json", '{"permissions":{"allow":[]}}')
    opaque = b"eyJhbGciOiJub25lIn0.eyJzdWIiOiJmaXh0dXJlIn0.fixture-signature"
    for name in [".claude/.credentials.json", ".supermonoagent/token", ".supermonoagent/config.json",
                 "skills/alpha/assets/private.pfx", "skills/alpha/assets/private.p12"]:
        write(repo, name, opaque if not name.endswith((".pfx", ".p12")) else b"\x30\x82\x01\x00owned-private-container")
    for name in ["skills/alpha/assets/public.cer", "skills/alpha/assets/public.crt"]:
        write(repo, name, b"-----BEGIN CERTIFICATE-----\nOwned public certificate fixture\n")
    revision = commit(repo)
    candidate, manifest = snapshot(tmp_path, repo, revision)
    excluded = {entry["path"]: entry["reason"] for entry in manifest["excluded"]}
    for name in [".claude/.credentials.json", ".supermonoagent/token", ".supermonoagent/config.json",
                 "skills/alpha/assets/private.pfx", "skills/alpha/assets/private.p12"]:
        assert name in excluded and name not in manifest["files"]
        assert not (candidate / "files" / name).exists()
    for name in [".claude/settings.json", ".claude/skills/normal/SKILL.md",
                 "skills/alpha/assets/public.cer", "skills/alpha/assets/public.crt"]:
        assert name in manifest["files"]


@pytest.mark.parametrize("marker", ["test-", "test_", "example", "placeholder", "dummy", "replace"], ids=[
    "test-hyphen", "test-underscore", "example-infix", "placeholder-infix", "dummy-infix", "replace-infix"])
def test_high_entropy_key_shape_is_excluded_even_with_placeholder_substring(tmp_path, marker):
    repo = repository(tmp_path / "repo")
    skill(repo)
    material = b"sk-proj-" + b"A" * 36 + marker.encode() + b"B" * 36
    write(repo, "skills/alpha/config.py", b"API_KEY = '" + material + b"'\n")
    revision = commit(repo)
    candidate, manifest = snapshot(tmp_path, repo, revision)
    assert "skills/alpha/config.py" not in manifest["files"]
    excluded = {entry["path"]: entry["reason"] for entry in manifest["excluded"]}
    assert excluded["skills/alpha/config.py"] == "possible_credential_content"
    assert not (candidate / "files/skills/alpha/config.py").exists()


def test_missing_gitlink_fails_and_nested_sources_use_pinned_commits(tmp_path):
    grand = repository(tmp_path / "grand")
    skill(grand, "leaf", name="grandchild", body="Pinned grandchild")
    grand_revision = commit(grand)
    child = repository(tmp_path / "child")
    skill(child, name="child", body="Pinned child")
    child_first = commit(child)
    git(child, "update-index", "--add", "--cacheinfo", "160000," + grand_revision + ",nested")
    child_tree = git(child, "write-tree")
    child_pinned = git(child, "commit-tree", child_tree, "-p", child_first, data=b"Owned nested Gitlink\n")
    git(child, "update-ref", "HEAD", child_pinned)
    write(child, "skills/alpha/SKILL.md", "Working tree differs from pinned content")
    child_later = commit(child, child_pinned)
    assert child_later != child_pinned
    parent = repository(tmp_path / "parent")
    skill(parent, name="parent")
    parent_base = commit(parent)
    git(parent, "update-index", "--add", "--cacheinfo", "160000," + child_pinned + ",sdk")
    parent_tree = git(parent, "write-tree")
    root_revision = git(parent, "commit-tree", parent_tree, "-p", parent_base, data=b"Owned root Gitlink\n")
    git(parent, "update-ref", "HEAD", root_revision)
    (parent / "sdk").mkdir()
    with pytest.raises(C.CatalogError) as error:
        snapshot(tmp_path, parent, root_revision, "missing")
    assert error.value.code == "gitlink_unavailable"
    assert error.value.details == {"path": "sdk", "revision": child_pinned}
    assert not (tmp_path / "missing").exists()
    candidate, manifest = snapshot(tmp_path, parent, root_revision, mapping={"sdk": child, "sdk/nested": grand})
    assert {entry["path"]: entry["revision"] for entry in manifest["sources"]} == {
        "": root_revision, "sdk": child_pinned, "sdk/nested": grand_revision}
    assert "Pinned child" in (candidate / "files/sdk/skills/alpha/SKILL.md").read_text()
    assert len(manifest["skills"]) == 3
    assert manifest["files"]["sdk/nested/leaf/SKILL.md"]["source_revision"] == grand_revision


def test_content_and_manifest_tampering_never_replace_active_pointer(tmp_path):
    repo, first, candidate, _manifest, store = active_fixture(tmp_path)
    before = (store.root / "current.json").read_bytes()
    write(repo, "skills/alpha/SKILL.md", "---\nname: changed\ndescription: changed\n---\nNew body")
    second = commit(repo, first)
    bad, _ = snapshot(tmp_path, repo, second, "bad")
    (bad / "files/skills/alpha/SKILL.md").write_text("corrupt")
    with pytest.raises(C.CatalogError):
        C.publish_snapshot(store, bad)
    assert (store.root / "current.json").read_bytes() == before and store.status()["revision"] == first
    manifest = json.loads((candidate / "manifest.json").read_text())
    manifest["skills"][0]["id"] = "0" * 64
    (candidate / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(C.CatalogError):
        C.verify_snapshot(candidate)


def test_publish_idempotence_cas_and_explicit_rollback(tmp_path):
    repo, first, candidate, manifest, store = active_fixture(tmp_path)
    again, _ = snapshot(tmp_path, repo, first, "same-export")
    assert (candidate / "manifest.json").read_bytes() == (again / "manifest.json").read_bytes()
    result = C.publish_snapshot(store, again, expected_revision=first)
    assert result["changed"] is result["created"] is False
    write(repo, "skills/alpha/extra.md", "second revision")
    second = commit(repo, first)
    next_candidate, _ = snapshot(tmp_path, repo, second, "second")
    with pytest.raises(C.CatalogConflictError):
        C.publish_snapshot(store, next_candidate, expected_revision=None)
    assert store.status()["revision"] == first
    C.publish_snapshot(store, next_candidate, expected_revision=first)
    assert store.status()["previous_revision"] == first
    store.activate(first, expected_revision=second)
    assert store.status()["revision"] == first and store.status()["previous_revision"] == second


def test_failed_pointer_replace_preserves_old_active_and_other_temp_files(tmp_path, monkeypatch):
    repo, first, _candidate, _manifest, store = active_fixture(tmp_path)
    write(repo, "skills/alpha/next.txt", "next")
    second = commit(repo, first)
    candidate, _ = snapshot(tmp_path, repo, second, "candidate-two")
    before = (store.root / "current.json").read_bytes()
    unrelated = store.root / ".someone-else.tmp"
    unrelated.write_text("preserve")
    original = C.os.replace
    def fail(source, target):
        if Path(target).name == "current.json":
            raise OSError("private-path-do-not-log")
        return original(source, target)
    monkeypatch.setattr(C.os, "replace", fail)
    with pytest.raises(OSError):
        C.publish_snapshot(store, candidate)
    assert (store.root / "current.json").read_bytes() == before
    assert store.status()["revision"] == first and unrelated.read_text() == "preserve"
    assert not list(store.root.glob(".current.json-*.tmp"))


def test_binary_and_utf8_text_resources_are_bounded_with_full_source_hash(tmp_path):
    repo = repository(tmp_path / "repo")
    skill(repo)
    text = ("独立证据" * 25000).encode()
    binary = b"%PDF-" + b"\0" + bytes(range(256)) * 800
    write(repo, "skills/alpha/references/large.md", text)
    write(repo, "skills/alpha/assets/paper.pdf", binary)
    revision = commit(repo)
    candidate, _ = snapshot(tmp_path, repo, revision)
    store = C.CatalogStore(tmp_path / "store")
    C.publish_snapshot(store, candidate)
    sid = store.list_skills()["items"][0]["id"]
    result = store.read_resource(sid, "references/large.md")
    assert result["encoding"] == "utf-8" and result["truncated"] and "\ufffd" not in result["text"]
    assert len(result["text"].encode()) <= C.MAX_TEXT_BYTES
    assert result["sha256"] == hashlib.sha256(text).hexdigest() and result["size"] == len(text)
    pdf = store.read_resource(sid, "assets/paper.pdf")
    assert pdf["encoding"] == "base64" and pdf["truncated"]
    assert base64.b64decode(pdf["data"]) == binary[:C.MAX_BINARY_BYTES]
    assert len(json.dumps(pdf).encode()) < 256 * 1024


def test_release_links_and_unexpected_files_are_rejected(tmp_path):
    _repo, _revision, candidate, _manifest, _store = active_fixture(tmp_path)
    write(candidate, "files/unexpected.txt", "not versioned")
    with pytest.raises(C.CatalogError):
        C.verify_snapshot(candidate)
    (candidate / "files/unexpected.txt").unlink()
    original = candidate / "files/skills/alpha/SKILL.md"
    outside = tmp_path / "outside.md"
    outside.write_bytes(original.read_bytes())
    original.unlink()
    os.link(outside, original)
    with pytest.raises(C.CatalogError):
        C.verify_snapshot(candidate)
    assert outside.read_bytes()


def test_manifest_cache_rehashes_actual_bytes_and_is_deeply_immutable(tmp_path, monkeypatch):
    _repo, revision, _candidate, _manifest, store = active_fixture(tmp_path)
    store = C.CatalogStore(store.root)
    original = C._json
    decodes = []
    def observe(raw):
        if b'"kind":"supermono-mylib-snapshot-v1"' in raw:
            decodes.append(len(raw))
        return original(raw)
    monkeypatch.setattr(C, "_json", observe)
    first = store.list_skills()
    sid = first["items"][0]["id"]
    store.read_skill(sid)
    store.status()
    assert len(decodes) == 1
    _rev, frozen, _path, _digest = store._pin()
    with pytest.raises(TypeError):
        frozen["skills"][0]["name"] = "changed"
    manifest_path = store.root / "releases" / revision / "manifest.json"
    before = manifest_path.stat()
    data = bytearray(manifest_path.read_bytes())
    offset = data.index(b"same-name")
    data[offset:offset+9] = b"evil-name"
    manifest_path.write_bytes(data)
    os.utime(manifest_path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(C.CatalogError):
        store.read_skill(sid)
    assert len(decodes) == 2  # Unchanged mtime did not authorize the old cache.


def test_manifest_cache_has_at_most_two_revision_entries(tmp_path):
    repo, revision, _candidate, _manifest, store = active_fixture(tmp_path)
    for index in range(5):
        write(repo, "skills/alpha/extra.txt", str(index))
        previous = revision
        revision = commit(repo, revision)
        candidate, _ = snapshot(tmp_path, repo, revision, "candidate-" + str(index))
        C.publish_snapshot(store, candidate, expected_revision=previous)
        store.list_skills()
    assert len(store._manifest_cache) <= 2
    assert sum(item[1] for item in store._manifest_cache.values()) <= 16 * 1024 * 1024


def test_concurrent_cold_reads_serialize_manifest_buffers_and_parse_once(tmp_path, monkeypatch):
    import time
    _repo, _revision, _candidate, manifest, existing = active_fixture(tmp_path)
    store = C.CatalogStore(existing.root)
    sid = manifest["skills"][0]["id"]
    original_read, original_json = C._read_bounded, C._json
    guard, barrier = threading.Lock(), threading.Barrier(8)
    counts = {"read_active": 0, "read_peak": 0, "reads": 0, "decodes": 0}
    def observed_read(path, maximum):
        if path.name != "manifest.json":
            return original_read(path, maximum)
        with guard:
            counts["read_active"] += 1
            counts["read_peak"] = max(counts["read_peak"], counts["read_active"])
            counts["reads"] += 1
        try:
            time.sleep(.01)
            return original_read(path, maximum)
        finally:
            with guard:
                counts["read_active"] -= 1
    def observed_json(raw):
        if b'"kind":"supermono-mylib-snapshot-v1"' in raw:
            with guard:
                counts["decodes"] += 1
            time.sleep(.01)
        return original_json(raw)
    monkeypatch.setattr(C, "_read_bounded", observed_read)
    monkeypatch.setattr(C, "_json", observed_json)
    def read(_index):
        barrier.wait(timeout=5)
        return store.read_skill(sid)["sha256"]
    with ThreadPoolExecutor(max_workers=8) as executor:
        values = list(executor.map(read, range(8)))
    assert len(set(values)) == 1
    assert counts["reads"] == 8  # The actual bytes were rehashed for every call.
    assert counts["read_peak"] == 1 and counts["decodes"] == 1


def test_operator_batch_pins_one_manifest_but_checks_each_leaf_and_exit(tmp_path, monkeypatch):
    _repo, revision, _candidate, _manifest, store = active_fixture(tmp_path)
    original_read, original_body = C._read_bounded, C._checked_body
    manifest_reads, body_reads = [], []
    def observed_read(path, maximum):
        if path.name == "manifest.json":
            manifest_reads.append(path)
        return original_read(path, maximum)
    def observed_body(path, item, maximum=0):
        body_reads.append(path)
        return original_body(path, item, maximum)
    monkeypatch.setattr(C, "_read_bounded", observed_read)
    monkeypatch.setattr(C, "_checked_body", observed_body)
    with store.pinned() as batch:
        assert batch.status()["revision"] == revision and batch.status()["pinned"]
        sid = batch.list_skills()["items"][0]["id"]
        for _ in range(40):
            assert batch.read_skill(sid, revision)["sha256"]
        assert len(manifest_reads) == 1
        assert len(body_reads) == 40
        with pytest.raises(C.CatalogError):
            batch.read_skill(sid, "b" * 40)
        with pytest.raises(C.CatalogError):
            batch.activate(revision)
        with pytest.raises(C.CatalogError):
            C.publish_snapshot(batch, tmp_path / "candidate")
    assert len(manifest_reads) == 2
    with pytest.raises(C.CatalogError, match="closed"):
        batch.list_skills()


def test_pinned_old_snapshot_stays_consistent_when_active_changes(tmp_path):
    repo, first, _candidate, _manifest, store = active_fixture(tmp_path)
    skill(repo, body="Second body")
    second = commit(repo, first)
    candidate, _ = snapshot(tmp_path, repo, second, "candidate-two")
    with store.pinned() as batch:
        sid = batch.list_skills()["items"][0]["id"]
        C.publish_snapshot(store, candidate, expected_revision=first)
        assert batch.status()["revision"] == first
        assert "Review source safely." in batch.read_skill(sid, first)["text"]
        assert batch.search("evidence")["revision"] == first
    assert store.status()["revision"] == second


def test_pinned_manifest_change_is_rejected_at_exit_even_with_preserved_mtime(tmp_path):
    _repo, revision, _candidate, _manifest, store = active_fixture(tmp_path)
    path = store.root / "releases" / revision / "manifest.json"
    with pytest.raises(C.CatalogError):
        with store.pinned() as batch:
            sid = batch.list_skills()["items"][0]["id"]
            before = path.stat()
            data = path.read_bytes().replace(b"same-name", b"evil-name")
            path.write_bytes(data)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            # The safe frozen index remains unchanged during the batch.
            assert batch.read_skill(sid)["name"] == "same-name"


def test_pinned_leaf_change_is_rejected_immediately(tmp_path):
    _repo, revision, _candidate, _manifest, store = active_fixture(tmp_path)
    with store.pinned() as batch:
        sid = batch.list_skills()["items"][0]["id"]
        (store.root / "releases" / revision / "files/skills/alpha/SKILL.md").write_text("corrupt")
        with pytest.raises(C.CatalogError):
            batch.read_skill(sid)


def test_operator_budget_reserves_blobs_and_manifest_once_before_writes(tmp_path):
    repo = repository(tmp_path / "repo")
    skill(repo)
    payload = b"owned binary data" * 20000
    write(repo, "skills/alpha/payload.bin", payload)
    revision = commit(repo)
    calls = []
    def check(pending_bytes=0):
        assert type(pending_bytes) is int and pending_bytes >= 0
        calls.append(pending_bytes)
    destination = tmp_path / "budgeted"
    manifest = C.build_snapshot(repo, destination, revision, budget_check=check)
    reserved = [value for value in calls if value]
    assert sorted(reserved) == sorted([entry["size"] for entry in manifest["files"].values()] + [len(C._encode(manifest))])
    assert len(calls) > len(reserved) + 10  # Metadata/chunk/index/verify checks.
    assert C.verify_snapshot(destination)["revision"] == revision


@pytest.mark.parametrize("exception_type", [ValueError, OSError], ids=["value-budget", "io-budget"])
def test_operator_budget_abort_during_chunk_preserves_exception_and_cleans_only_owned_stage(tmp_path, exception_type):
    repo = repository(tmp_path / "repo")
    skill(repo)
    write(repo, "skills/alpha/payload.bin", b"owned body" * 70000)
    revision = commit(repo)
    destination = tmp_path / "aborted"
    unrelated = tmp_path / ".aborted-unrelated"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("preserve")
    sentinel = exception_type("owned resource budget reached")
    checks = []
    def check(pending_bytes=0):
        checks.append(pending_bytes)
        for path in tmp_path.glob(".aborted-*/files/skills/alpha/payload.bin"):
            if path.stat().st_size >= 128 * 1024:
                raise sentinel
    with pytest.raises(exception_type) as caught:
        C.build_snapshot(repo, destination, revision, budget_check=check)
    assert caught.value is sentinel
    assert not destination.exists()
    assert (unrelated / "keep.txt").read_text() == "preserve"
    assert list(tmp_path.glob(".aborted-*")) == [unrelated]
    assert len(checks) > 10


def test_operator_budget_abort_before_git_is_not_misreported_as_source_failure(tmp_path):
    repo = repository(tmp_path / "repo")
    skill(repo)
    revision = commit(repo)
    sentinel = ValueError("owned deadline reached")
    calls = []
    def check(pending_bytes=0):
        calls.append(pending_bytes)
        if len(calls) == 3:
            raise sentinel
    with pytest.raises(ValueError) as caught:
        C.build_snapshot(repo, tmp_path / "never-created", revision, budget_check=check)
    assert caught.value is sentinel
    assert not (tmp_path / "never-created").exists()
    assert not list(tmp_path.glob(".never-created-*"))


def test_readonly_verification_budget_abort_leaves_candidate_unchanged(tmp_path):
    repo = repository(tmp_path / "repo")
    skill(repo)
    revision = commit(repo)
    candidate, _manifest = snapshot(tmp_path, repo, revision)
    before = (candidate / "manifest.json").read_bytes()
    sentinel, calls = OSError("owned verification budget"), []
    def check(pending_bytes=0):
        calls.append(pending_bytes)
        if len(calls) == 5:
            raise sentinel
    with pytest.raises(OSError) as caught:
        C.verify_snapshot(candidate, budget_check=check)
    assert caught.value is sentinel
    assert (candidate / "manifest.json").read_bytes() == before
    assert C.verify_snapshot(candidate)["revision"] == revision


@pytest.mark.parametrize("value", ["../outside", "/absolute", r"C:\secret", "a/../b", "a//b", "AUX.txt", "a:b", "%2e%2e/x", "a\u0000b"])
def test_resource_path_rejections(value):
    with pytest.raises(C.CatalogError):
        C._relative(value)


@pytest.mark.parametrize("method,args", [("list_skills", {"limit": True}), ("list_skills", {"limit": 101}),
                                      ("list_skills", {"cursor": "!"}), ("search", {"query": ""}),
                                      ("search", {"query": "x", "limit": 51})])
def test_public_limits_fail_closed(tmp_path, method, args):
    with pytest.raises(C.CatalogError):
        getattr(C.CatalogStore(tmp_path), method)(**args)
