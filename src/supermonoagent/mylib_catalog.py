"""Immutable, tracked-Git skill snapshots and a bounded read-only catalog.

Only packaging helpers invoke Git. Catalog requests read verified snapshot data;
they never execute instructions or scripts from the library.
"""
from __future__ import annotations

import base64
from collections import OrderedDict
from contextlib import contextmanager
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import posixpath
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from types import MappingProxyType
from urllib.parse import unquote, urlsplit, urlunsplit


KIND = "supermono-mylib-snapshot-v1"
MAX_FILES = 200000
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_TEXT_BYTES = 64 * 1024
MAX_BINARY_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 240 * 1024
_UNSET = object()
_REVISION = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_RUNTIME_DIRS = {".git", ".omx", ".metarsi", ".supermonoagent", ".venv", "venv", "__pycache__"}
_PRIVATE_NAMES = {"token.txt", "token-source.json", "relay.txt", "auth.json", "credentials.json", ".credentials.json",
                  "secrets.json", "settings.local.json", ".npmrc", ".pypirc", "id_rsa",
                  "id_dsa", "id_ecdsa", "id_ed25519"}
_SECRET = re.compile(
    rb"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----"
    rb"|\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}"
    rb"|AKIA[A-Z0-9]{16}|sk-(?:proj-)?[A-Za-z0-9_-]{32,})\b")
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class CatalogError(ValueError):
    def __init__(self, message="Catalog data is unavailable", *, code="catalog_error", details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class CatalogConflictError(CatalogError):
    def __init__(self):
        super().__init__("The active library changed; retry with its current revision", code="cas_mismatch")


class _BudgetAbort(BaseException):
    def __init__(self, original):
        self.original = original


def _budget_tick(check, pending_bytes=0):
    if check is not None:
        try:
            check(pending_bytes=pending_bytes) if pending_bytes else check()
        except BaseException as error:
            # Preserve operator cancellation even if its type is ValueError or
            # OSError, both of which Git/file adapters otherwise sanitize.
            raise _BudgetAbort(error) from None


def _fail(code, message="Invalid catalog data", details=None):
    raise CatalogError(message, code=code, details=details)


def _revision(value):
    if not isinstance(value, str) or not _REVISION.fullmatch(value.lower()):
        _fail("invalid_revision", "A full immutable Git revision is required")
    return value.lower()


def _relative(value):
    try:
        encoded = value.encode("utf-8", "strict") if isinstance(value, str) else b""
    except UnicodeError:
        _fail("invalid_path", "Resource path is invalid")
    if (not isinstance(value, str) or not value or len(encoded) > 1024
            or value.startswith("/") or "\\" in value or ":" in value
            or re.search(r"%(?:2e|2f|5c)", value, re.I)
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        _fail("invalid_path", "Resource path is invalid")
    parts = value.split("/")
    for part in parts:
        if (not part or part in {".", ".."} or part[-1] in ". "
                or any(char in part for char in '<>"|?*')
                or len(part.encode("utf-16-le")) // 2 > 255
                or re.fullmatch(r"CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³]",
                                part.split(".")[0].rstrip(" "), re.I)):
            _fail("invalid_path", "Resource path is invalid")
    return value


def _link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _plain(path, directory=False):
    try:
        info = path.lstat()
    except OSError:
        _fail("unavailable", "Catalog storage is unavailable")
    if (_link(info) or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or not directory and info.st_nlink != 1):
        _fail("unsafe_link", "Catalog storage contains an unsupported link")
    return info


def _file(root, relative):
    relative = _relative(relative)
    path = root
    _plain(root, True)
    parts = relative.split("/")
    for part in parts[:-1]:
        path = path / part
        _plain(path, True)
    path = path / parts[-1]
    _plain(path)
    return path


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail("invalid_json")
            result[key] = value
        return result
    try:
        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique)
    except (UnicodeError, ValueError, TypeError, RecursionError):
        _fail("invalid_json")


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _read_bounded(path, maximum):
    try:
        with path.open("rb") as stream:
            raw = stream.read(maximum + 1)
    except OSError:
        _fail("unavailable", "Catalog data could not be read")
    if len(raw) > maximum:
        _fail("too_large", "Catalog metadata exceeds its limit")
    return raw


def _clip(value, maximum):
    return value.encode("utf-8", "replace")[:maximum].decode("utf-8", "ignore")


def _private_path(path):
    parts = [part.casefold() for part in path.split("/")]
    name = parts[-1]
    if any(part in _RUNTIME_DIRS for part in parts[:-1]):
        return "private_runtime"
    if (name in _PRIVATE_NAMES or name.endswith((".pyc", ".pyo", ".keystore", ".jks", ".pfx", ".p12"))
            or name == ".env"
            or name.startswith(".env.") and not name.endswith((".example", ".sample", ".template", ".dist"))
            or name.startswith("credentials.") and name.endswith(".json")):
        return "credential_filename"
    return None


def _secret(data):
    # A test-labelled credential can still be live. Never waive a concrete
    # credential shape merely because it contains a documentation-like word.
    return _SECRET.search(data) is not None


def _frontmatter(text, fallback):
    fields = {}
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                break
            found = re.match(r"^(name|description):\s*(.*)$", line)
            if not found:
                continue
            value = found[2].strip()
            if value in {"|", ">", "|-", ">-", "|+", ">+"}:
                following = []
                for continuation in lines[index + 1:]:
                    if continuation and not continuation[0].isspace():
                        break
                    following.append(continuation.strip())
                value = " ".join(following)
            fields[found[1]] = value.strip("\"'")
    name = " ".join(fields.get("name", fallback).split()) or fallback
    description = " ".join(fields.get("description", "").split())
    return _clip(name, 256), _clip(description, 1024)


def _references(text, path, files, excluded, directories):
    root = posixpath.dirname(path)
    links = re.findall(r"!?\[[^\]\n]*\]\(\s*<?([^\s)>]+)>?(?:\s+[^)\n]*)?\)", text)
    links += re.findall(r"^\s*\[[^\]\n]+\]:\s*<?([^\s>]+)>?", text, re.M)
    # Inline file references are indexed when they resolve to known data; shell
    # commands and speculative filenames are not treated as broken links.
    inline = re.findall(r"`([^`\n]+)`", text)
    records, seen = [], set()
    for raw, explicit in [(link, True) for link in links] + [(link, False) for link in inline]:
        if len(raw) > 4096:
            continue
        try:
            uri = urlsplit(raw)
        except ValueError:
            continue
        if uri.scheme or uri.netloc:
            if explicit:
                target = urlunsplit((uri.scheme, uri.hostname or "", uri.path, "", ""))
                key = ("external", target)
                if key not in seen:
                    seen.add(key)
                    records.append({"target": _clip(target, 1024), "status": "external"})
            continue
        if not uri.path:
            continue
        decoded = unquote(uri.path)
        target = posixpath.normpath(posixpath.join(root, decoded)) if not decoded.startswith("/") else decoded.lstrip("/")
        status = "available" if target in files else "excluded" if target in excluded else "directory" if target in directories else "missing"
        if target == ".." or target.startswith("../") or "\\" in decoded or ":" in decoded:
            status = "outside_repo"
        if not explicit and status not in {"available", "directory"}:
            continue
        key = (status, target)
        if key in seen:
            continue
        seen.add(key)
        record = {"target": _clip(target, 1024), "status": status}
        if status == "available":
            record["resource_path"] = (posixpath.relpath(target, root) if target.startswith(root + "/") or not root else "@/" + target)
        records.append(record)
    return records


def _index(files_root, files, excluded, budget_check=None):
    _budget_tick(budget_check)
    excluded_paths = {entry["path"] for entry in excluded}
    all_paths = set(files) | excluded_paths
    directories = {name[:index] for name in all_paths for index, char in enumerate(name) if char == "/"}
    # Include every tracked SKILL, even if its body was explicitly excluded.
    skills = []
    for path in sorted(name for name in all_paths if posixpath.basename(name) == "SKILL.md"):
        _budget_tick(budget_check)
        root = posixpath.dirname(path)
        available = path in files
        text = _read_bounded_prefix(_file(files_root, path), MAX_TEXT_BYTES)[0].decode("utf-8", "replace") if available else ""
        name, description = _frontmatter(text, posixpath.basename(root) or "mylib")
        if not available:
            description = "Instruction body excluded by snapshot safety policy."
        skills.append({"id": hashlib.sha256(path.encode()).hexdigest(), "name": name,
                       "description": description, "path": path, "available": available,
                       "references": _references(text, path, files, excluded_paths, directories) if available else []})
        _budget_tick(budget_check)
    return skills


def _read_bounded_prefix(path, maximum):
    try:
        with path.open("rb") as stream:
            value = stream.read(maximum + 1)
    except OSError:
        _fail("unavailable")
    return value[:maximum], len(value) > maximum


def _remote(repo, budget_check=None):
    value = ""
    try:
        value = _git(repo, "config", "--get", "remote.origin.url", allow_failure=True, budget_check=budget_check).decode("utf-8", "replace").strip()
        uri = urlsplit(value)
        if uri.scheme in {"https", "http", "ssh", "git"} and uri.hostname:
            host = uri.hostname
            if ":" in host:
                host = "[" + host + "]"
            if uri.port:
                host += ":" + str(uri.port)
            return _clip(urlunsplit((uri.scheme, host, uri.path, "", "")), 2048)
        scp = re.fullmatch(r"(?:[^@/:\s]+@)?([^/:\s]+):(.+)", value)
        if scp and not re.match(r"^[A-Za-z]:", value):
            return _clip("ssh://" + scp[1] + "/" + scp[2], 2048)
    except (ValueError, CatalogError):
        pass
    return "local" if value else ""


def _git(repo, *arguments, allow_failure=False, budget_check=None):
    _budget_tick(budget_check)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
           "GIT_NO_LAZY_FETCH": "1", "GIT_LFS_SKIP_SMUDGE": "1"}
    process, timer = None, None
    try:
        process = subprocess.Popen(["git", "-C", str(repo), *arguments], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        timer = threading.Timer(30, process.kill)
        timer.daemon = True
        timer.start()
        output = bytearray()
        while True:
            _budget_tick(budget_check)
            chunk = process.stdout.read(128 * 1024)
            _budget_tick(budget_check)
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > MAX_MANIFEST_BYTES:
                _fail("source_limit", "Git metadata exceeds its limit")
        result = process.wait(timeout=5)
        _budget_tick(budget_check)
        if result and not allow_failure:
            _fail("source_unavailable", "Git source is unavailable")
        return bytes(output) if result == 0 else b""
    except (OSError, subprocess.TimeoutExpired):
        _fail("source_unavailable", "Git source is unavailable")
    finally:
        if timer:
            timer.cancel()
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()


def _repo(path, budget_check=None):
    _budget_tick(budget_check)
    try:
        root = Path(path).resolve()
        if _git(root, "rev-parse", "--is-bare-repository", budget_check=budget_check).strip() != b"true":
            top = Path(_git(root, "rev-parse", "--show-toplevel", budget_check=budget_check).decode("utf-8", "strict").strip()).resolve()
            if top != root:
                _fail("source_unavailable", "A Git repository root is required")
        return root
    except (OSError, UnicodeError, ValueError):
        _fail("source_unavailable", "A Git repository root is required")


def _commit(repo, revision, budget_check=None):
    _budget_tick(budget_check)
    if not isinstance(revision, str) or not revision or len(revision) > 256 or revision.startswith("-") or "\0" in revision:
        _fail("invalid_revision")
    return _revision(_git(repo, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}", budget_check=budget_check).decode().strip())


class _BlobReader:
    def __init__(self, repo, budget_check=None):
        self.budget_check = budget_check
        _budget_tick(budget_check)
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
               "GIT_NO_LAZY_FETCH": "1", "GIT_LFS_SKIP_SMUDGE": "1"}
        try:
            self.process = subprocess.Popen(["git", "-C", str(repo), "cat-file", "--batch"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        except OSError:
            _fail("source_unavailable")
        self.timer = threading.Timer(300, self.process.kill)
        self.timer.daemon = True
        self.timer.start()

    def export(self, oid, size, destination):
        digest, tail, sensitive, first = hashlib.sha256(), b"", False, True
        _budget_tick(self.budget_check, pending_bytes=size)
        try:
            self.process.stdin.write((oid + "\n").encode("ascii"))
            self.process.stdin.flush()
            header = self.process.stdout.readline(256).split()
            if len(header) != 3 or header[0].decode("ascii") != oid or header[1] != b"blob" or int(header[2]) != size:
                _fail("source_unavailable", "A pinned Git object is unavailable")
            with destination.open("xb") as output:
                remaining = size
                while remaining:
                    _budget_tick(self.budget_check)
                    chunk = self.process.stdout.read(min(128 * 1024, remaining))
                    _budget_tick(self.budget_check)
                    if not chunk:
                        _fail("source_unavailable")
                    if first and chunk.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
                        _fail("lfs_unavailable", "Git LFS data requires an explicit source export")
                    first = False
                    sensitive = sensitive or _secret(tail + chunk)
                    tail = chunk[-16384:]
                    digest.update(chunk)
                    _budget_tick(self.budget_check)
                    output.write(chunk)
                    _budget_tick(self.budget_check)
                    remaining -= len(chunk)
            if self.process.stdout.read(1) != b"\n":
                _fail("source_unavailable")
            return digest.hexdigest(), sensitive
        except CatalogError:
            raise
        except (OSError, ValueError, UnicodeError):
            _fail("source_unavailable", "A pinned Git object could not be exported")

    def close(self):
        self.timer.cancel()
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout):
            stream.close()


def _remove_owned(path, parent):
    if path.parent.resolve() != parent.resolve() or not path.resolve().is_relative_to(parent.resolve()):
        return
    try:
        if _link(path.lstat()):
            path.rmdir() if path.is_dir() else path.unlink()
        else:
            shutil.rmtree(path)
    except FileNotFoundError:
        pass


def build_snapshot(repo, destination, revision=None, *, submodule_repos=None, budget_check=None):
    """Export fixed tracked data; an optional operator check guards resource use.

    budget_check(pending_bytes=0) is not an MCP input. Positive byte hints
    reserve each output blob/manifest once before creation; zero-argument calls
    check time and resources between metadata, traversal, IO and verification.
    """
    if budget_check is not None and not callable(budget_check):
        _fail("invalid_budget", "Snapshot budget checker is invalid")
    try:
        return _build_snapshot(repo, destination, revision, submodule_repos=submodule_repos,
                               budget_check=budget_check)
    except _BudgetAbort as aborted:
        raise aborted.original.with_traceback(aborted.original.__traceback__) from None


def _build_snapshot(repo, destination, revision=None, *, submodule_repos=None, budget_check=None):
    """Export a fixed tracked tree, recursively requiring pinned Gitlink data."""
    _budget_tick(budget_check)
    source = _repo(repo, budget_check)
    commit = _commit(source, revision or "HEAD", budget_check)
    destination = Path(destination).absolute()
    mappings = {_relative(str(key)): Path(value) for key, value in (submodule_repos or {}).items()}
    if destination.exists():
        _fail("destination_exists", "Snapshot destination must not already exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="." + destination.name + "-", dir=destination.parent))
    files_root = stage / "files"
    files_root.mkdir()
    files, excluded, sources, seen, active = {}, [], [], set(), set()
    total = 0

    def export(repository, pinned, prefix="", depth=0):
        nonlocal total
        _budget_tick(budget_check)
        if depth > 8 or len(sources) >= 128 or (str(repository), pinned) in active:
            _fail("source_cycle", "Recursive Gitlink sources are unsupported")
        active.add((str(repository), pinned))
        sources.append({"path": prefix, "revision": pinned, "remote": _remote(repository, budget_check)})
        tree = _git(repository, "ls-tree", "-r", "-l", "-z", pinned, budget_check=budget_check)
        entries = []
        for raw in tree.split(b"\0"):
            _budget_tick(budget_check)
            if not raw:
                continue
            try:
                header, name = raw.split(b"\t", 1)
                mode, kind, oid, length = header.split()
                path = _relative((prefix + "/" if prefix else "") + name.decode("utf-8", "strict"))
                mode, kind, oid = mode.decode(), kind.decode(), oid.decode()
                folded = path.casefold()
                if folded in seen:
                    _fail("path_collision", "Git paths collide on a supported filesystem")
                seen.add(folded)
                entries.append((path, mode, kind, oid, None if length == b"-" else int(length)))
            except (UnicodeError, ValueError):
                _fail("source_tree_invalid")
        reader = _BlobReader(repository, budget_check)
        try:
            for path, mode, kind, oid, size in entries:
                _budget_tick(budget_check)
                if mode == "160000" and kind == "commit":
                    candidate = mappings.get(path, repository / path.removeprefix(prefix + "/") if prefix else repository / path)
                    try:
                        child = _repo(candidate, budget_check)
                        if _commit(child, oid, budget_check) != oid:
                            _fail("gitlink_unavailable")
                    except CatalogError:
                        _fail("gitlink_unavailable", "A pinned Gitlink source is unavailable",
                              {"path": path, "revision": oid})
                    export(child, oid, path, depth + 1)
                    continue
                if len(files) + len(excluded) >= MAX_FILES or size is None or size < 0:
                    _fail("source_limit", "Snapshot source exceeds its limits")
                record = {"path": path, "mode": mode, "source_revision": pinned, "git_oid": oid, "size": size}
                reason = _private_path(path) or ("unsupported_link" if mode == "120000" else None)
                if reason:
                    excluded.append({**record, "reason": reason})
                    continue
                if kind != "blob" or mode not in {"100644", "100755"}:
                    _fail("source_tree_invalid")
                if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
                    _fail("source_limit", "Snapshot source exceeds its limits", {"path": path, "size": size})
                target = files_root.joinpath(*path.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                digest, sensitive = reader.export(oid, size, target)
                _budget_tick(budget_check)
                if sensitive:
                    target.unlink()
                    excluded.append({**record, "reason": "possible_credential_content"})
                    continue
                files[path] = {key: value for key, value in {**record, "sha256": digest}.items() if key != "path"}
                total += size
        finally:
            reader.close()
            active.remove((str(repository), pinned))

    try:
        export(source, commit)
        manifest = {"schema": 1, "kind": KIND, "revision": commit,
                    "source": {"revision": commit, "remote": sources[0]["remote"]},
                    "sources": sources, "files": files, "excluded": sorted(excluded, key=lambda item: item["path"]),
                    "skills": _index(files_root, files, excluded, budget_check), "total_bytes": total}
        raw = _encode(manifest)
        if len(raw) > MAX_MANIFEST_BYTES:
            _fail("source_limit", "Snapshot metadata exceeds its limit")
        _budget_tick(budget_check, pending_bytes=len(raw))
        (stage / "manifest.json").write_bytes(raw)
        _budget_tick(budget_check)
        verify_snapshot(stage, budget_check=budget_check)
        _budget_tick(budget_check)
        os.rename(stage, destination)
        return manifest
    except BaseException:
        _remove_owned(stage, destination.parent)
        raise


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _manifest(path, cache=None, cache_lock=None, budget_check=None):
    if cache_lock is not None:
        # Serialize the raw read too: bounded MCP concurrency must not multiply
        # the complete manifest buffer or cold JSON/freeze allocations.
        with cache_lock:
            return _manifest_loaded(path, cache, cache_lock, budget_check)
    return _manifest_loaded(path, budget_check=budget_check)


def _manifest_loaded(path, cache=None, cache_lock=None, budget_check=None):
    _budget_tick(budget_check)
    _plain(path, True)
    raw = _read_bounded(_file(path, "manifest.json"), MAX_MANIFEST_BYTES)
    digest = hashlib.sha256(raw).hexdigest()
    _budget_tick(budget_check)
    if cache is not None:
        with cache_lock:
            found = cache.get(digest)
            if found is not None:
                cache.move_to_end(digest)
                return found[0], digest
    value = _json(raw)
    _budget_tick(budget_check)
    if (not isinstance(value, dict) or value.get("schema") != 1 or type(value.get("schema")) is not int
            or value.get("kind") != KIND or not isinstance(value.get("files"), dict)
            or not isinstance(value.get("skills"), list) or not isinstance(value.get("excluded"), list)
            or not isinstance(value.get("sources"), list) or not 1 <= len(value["sources"]) <= 128):
        _fail("invalid_snapshot", "Snapshot metadata is invalid")
    revision = _revision(value.get("revision"))
    if not isinstance(value.get("source"), dict) or value["source"].get("revision") != revision:
        _fail("invalid_snapshot")
    if len(value["files"]) + len(value["excluded"]) > MAX_FILES:
        _fail("source_limit")
    if _secret(raw):
        _fail("credential_material", "Snapshot metadata contains possible credential material")
    source_paths = set()
    for source in value["sources"]:
        _budget_tick(budget_check)
        if not isinstance(source, dict) or not isinstance(source.get("path"), str):
            _fail("invalid_snapshot")
        prefix = source["path"]
        if prefix:
            _relative(prefix)
        _revision(source.get("revision"))
        remote = source.get("remote")
        if not isinstance(remote, str) or len(remote.encode("utf-8")) > 2048:
            _fail("invalid_snapshot")
        if remote not in {"", "local"}:
            try:
                uri = urlsplit(remote)
                if (uri.scheme not in {"https", "http", "ssh", "git"} or not uri.hostname
                        or uri.username is not None or uri.password is not None or uri.query or uri.fragment):
                    _fail("invalid_snapshot")
            except ValueError:
                _fail("invalid_snapshot")
        if prefix in source_paths:
            _fail("invalid_snapshot")
        source_paths.add(prefix)
    if value["sources"][0]["path"] != "" or value["sources"][0]["revision"] != revision:
        _fail("invalid_snapshot")
    seen, total = set(), 0
    for name, item in value["files"].items():
        _budget_tick(budget_check)
        _relative(name)
        if (name.casefold() in seen or not isinstance(item, dict)
                or item.get("mode") not in {"100644", "100755"}
                or type(item.get("size")) is not int or not 0 <= item["size"] <= MAX_FILE_BYTES
                or not isinstance(item.get("sha256"), str) or not _HASH.fullmatch(item["sha256"])
                or _private_path(name)):
            _fail("invalid_snapshot")
        seen.add(name.casefold())
        _revision(item.get("git_oid"))
        _revision(item.get("source_revision"))
        source = max((entry for entry in value["sources"] if not entry["path"] or
                      name.startswith(entry["path"] + "/")), key=lambda entry: len(entry["path"]))
        if item["source_revision"] != source["revision"]:
            _fail("invalid_snapshot")
        total += item["size"]
    for item in value["excluded"]:
        _budget_tick(budget_check)
        if not isinstance(item, dict) or not isinstance(item.get("reason"), str):
            _fail("invalid_snapshot")
        name = _relative(item.get("path"))
        if name.casefold() in seen:
            _fail("invalid_snapshot")
        seen.add(name.casefold())
    if type(value.get("total_bytes")) is not int or total != value["total_bytes"] or total > MAX_TOTAL_BYTES:
        _fail("invalid_snapshot")
    skill_ids, skill_paths = set(), set()
    for skill in value["skills"]:
        _budget_tick(budget_check)
        if not isinstance(skill, dict):
            _fail("invalid_snapshot")
        name = _relative(skill.get("path"))
        if (posixpath.basename(name) != "SKILL.md" or skill.get("id") != hashlib.sha256(name.encode()).hexdigest()
                or skill["id"] in skill_ids or name in skill_paths
                or type(skill.get("available")) is not bool or skill["available"] != (name in value["files"])
                or not isinstance(skill.get("name"), str) or len(skill["name"].encode("utf-8")) > 256
                or not isinstance(skill.get("description"), str) or len(skill["description"].encode("utf-8")) > 1024
                or not isinstance(skill.get("references"), list)):
            _fail("invalid_snapshot")
        skill_ids.add(skill["id"])
        skill_paths.add(name)
        parent = posixpath.dirname(name)
        for reference in skill["references"]:
            _budget_tick(budget_check)
            if (not isinstance(reference, dict) or not isinstance(reference.get("target"), str)
                    or len(reference["target"].encode("utf-8")) > 1024
                    or reference.get("status") not in {"available", "missing", "excluded", "external", "outside_repo", "directory"}):
                _fail("invalid_snapshot")
            if reference["status"] == "available":
                target = _relative(reference["target"])
                expected = posixpath.relpath(target, parent) if not parent or target.startswith(parent + "/") else "@/" + target
                if target not in value["files"] or reference.get("resource_path") != expected:
                    _fail("invalid_snapshot")
    expected_skills = {name for name in set(value["files"]) | {entry["path"] for entry in value["excluded"]}
                       if posixpath.basename(name) == "SKILL.md"}
    if expected_skills != skill_paths:
        _fail("invalid_snapshot")
    if cache is not None:
        value = _freeze(value)
        if len(raw) <= 16 * 1024 * 1024:
            with cache_lock:
                cache[digest] = (value, len(raw))
                cache.move_to_end(digest)
                while len(cache) > 2 or sum(item[1] for item in cache.values()) > 16 * 1024 * 1024:
                    cache.popitem(last=False)
    return value, digest


def _checked_body(path, item, maximum=0, *, budget_check=None):
    _budget_tick(budget_check)
    info = _plain(path)
    if info.st_size != item["size"]:
        _fail("hash_mismatch", "Snapshot integrity check failed")
    digest = hashlib.sha256()
    git_digest = hashlib.sha1() if len(item["git_oid"]) == 40 else hashlib.sha256()
    git_digest.update(("blob " + str(item["size"]) + "\0").encode())
    prefix, total, tail = bytearray(), 0, b""
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(128 * 1024):
                _budget_tick(budget_check)
                total += len(chunk)
                if total > item["size"] or _secret(tail + chunk):
                    _fail("hash_mismatch", "Snapshot integrity check failed")
                tail = chunk[-16384:]
                digest.update(chunk)
                git_digest.update(chunk)
                if len(prefix) < maximum:
                    prefix.extend(chunk[:maximum-len(prefix)])
                _budget_tick(budget_check)
    except OSError:
        _fail("unavailable", "Snapshot data could not be read")
    if total != item["size"] or digest.hexdigest() != item["sha256"] or git_digest.hexdigest() != item["git_oid"]:
        _fail("hash_mismatch", "Snapshot integrity check failed")
    return bytes(prefix)


def _walk_files(root, budget_check=None):
    _budget_tick(budget_check)
    _plain(root, True)
    found = set()
    pending = [(root, "")]
    while pending:
        _budget_tick(budget_check)
        parent, prefix = pending.pop()
        try:
            entries = list(parent.iterdir())
        except OSError:
            _fail("unavailable")
        for entry in entries:
            _budget_tick(budget_check)
            relative = (prefix + "/" if prefix else "") + entry.name
            _relative(relative)
            info = entry.lstat()
            if _link(info):
                _fail("unsafe_link", "Snapshot contains an unsupported link")
            if stat.S_ISDIR(info.st_mode):
                pending.append((entry, relative))
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                found.add(relative)
            else:
                _fail("unsafe_link")
            if len(found) + len(pending) > MAX_FILES * 2:
                _fail("source_limit")
    return found


def verify_snapshot(path, *, budget_check=None):
    """Verify exact file inventory, content SHA/Git blob IDs, and rebuilt index."""
    try:
        return _verify_snapshot(path, budget_check=budget_check)
    except _BudgetAbort as aborted:
        raise aborted.original.with_traceback(aborted.original.__traceback__) from None


def _verify_snapshot(path, *, budget_check=None):
    _budget_tick(budget_check)
    path = Path(path).absolute()
    value, _digest = _manifest(path, budget_check=budget_check)
    if {entry.name for entry in path.iterdir()} != {"manifest.json", "files"}:
        _fail("invalid_snapshot", "Snapshot contains unexpected entries")
    files_root = path / "files"
    if _walk_files(files_root, budget_check) != set(value["files"]):
        _fail("invalid_snapshot", "Snapshot inventory does not match its manifest")
    for name, item in value["files"].items():
        _budget_tick(budget_check)
        if budget_check is None:
            _checked_body(_file(files_root, name), item)
        else:
            _checked_body(_file(files_root, name), item, budget_check=budget_check)
    if _index(files_root, value["files"], value["excluded"], budget_check) != value["skills"]:
        _fail("invalid_snapshot", "Snapshot skill index does not match its files")
    _budget_tick(budget_check)
    return value


def _thread_lock(root):
    key = os.path.normcase(str(root))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _locked(root):
    root.mkdir(parents=True, exist_ok=True)
    _plain(root, True)
    lock = root / "catalog.lock"
    if lock.exists():
        _plain(lock)
    with _thread_lock(root), lock.open("a+b") as handle:
        _plain(lock)
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            deadline = time.monotonic() + 15
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        _fail("store_busy", "Catalog store is busy")
                    time.sleep(.02)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _atomic(path, value):
    temporary = path.with_name("." + path.name + "-" + secrets.token_hex(12) + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(_encode(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _budget(value):
    while len(_encode(value)) > MAX_RESPONSE_BYTES:
        if value.get("text"):
            value["text"] = _clip(value["text"], max(0, len(value["text"].encode("utf-8")) // 2))
            value["truncated"] = True
        elif value.get("references"):
            value["references"] = value["references"][:len(value["references"]) // 2]
            value["references_truncated"] = True
        elif value.get("resource_paths"):
            value["resource_paths"] = value["resource_paths"][:len(value["resource_paths"]) // 2]
            value["resources_truncated"] = True
        else:
            _fail("response_limit", "Catalog response exceeds its limit")
    return value


def _utf8_prefix(data, total_size):
    try:
        return data.decode("utf-8", "strict"), len(data)
    except UnicodeDecodeError as error:
        if error.end == len(data) and error.reason == "unexpected end of data" and total_size > len(data):
            complete = data[:error.start]
            return complete.decode("utf-8", "strict"), len(complete)
        raise


class CatalogStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self._manifest_cache = OrderedDict()
        self._manifest_lock = threading.RLock()

    def _pointer(self):
        try:
            path = self.root / "current.json"
            path.lstat()
        except FileNotFoundError:
            return None
        except OSError:
            _fail("unavailable", "Catalog pointer is unavailable")
        _plain(self.root, True)
        _plain(path)
        value = _json(_read_bounded(path, 4096))
        if (not isinstance(value, dict) or set(value) != {"schema", "revision", "manifest_sha256", "previous_revision"}
                or type(value.get("schema")) is not int or value["schema"] != 1
                or not isinstance(value.get("manifest_sha256"), str) or not _HASH.fullmatch(value["manifest_sha256"])):
            _fail("invalid_pointer", "Catalog pointer is invalid")
        _revision(value.get("revision"))
        if value["previous_revision"] is not None:
            _revision(value["previous_revision"])
        return value

    def _pin(self, revision=None):
        pointer = self._pointer() if revision is None else None
        if revision is None:
            if pointer is None:
                _fail("no_active_library", "No library snapshot is active")
            revision = pointer["revision"]
        revision = _revision(revision)
        releases = self.root / "releases"
        _plain(self.root, True)
        _plain(releases, True)
        path = releases / revision
        value, digest = _manifest(path, self._manifest_cache, self._manifest_lock)
        if value["revision"] != revision or pointer is not None and digest != pointer["manifest_sha256"]:
            _fail("hash_mismatch", "Catalog manifest integrity check failed")
        return revision, value, path, digest

    @staticmethod
    def _summary(pointer, manifest, digest):
        revision = manifest["revision"]
        return {"available": True, "revision": revision, "active_revision": revision,
                "source_revision": manifest["source"]["revision"],
                "previous_revision": pointer["previous_revision"], "manifest_sha256": digest,
                "skill_count": len(manifest["skills"]), "file_count": len(manifest["files"]),
                "excluded_count": len(manifest["excluded"]), "total_bytes": manifest["total_bytes"]}

    def status(self):
        pointer = self._pointer()
        if pointer is None:
            return {"available": False, "revision": None, "active_revision": None,
                    "source_revision": None, "previous_revision": None, "manifest_sha256": None,
                    "skill_count": 0, "file_count": 0, "excluded_count": 0, "total_bytes": 0}
        _revision_value, manifest, _path, digest = self._pin(pointer["revision"])
        if digest != pointer["manifest_sha256"]:
            _fail("hash_mismatch", "Catalog manifest integrity check failed")
        return self._summary(pointer, manifest, digest)

    @staticmethod
    def _item(skill):
        return {name: skill[name] for name in ("id", "name", "description", "path", "available")}

    def list_skills(self, cursor=None, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            _fail("invalid_limit", "Skill page limit must be between 1 and 100")
        revision, offset = None, 0
        if cursor is not None:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 512 or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
                _fail("invalid_cursor", "Skill cursor is invalid")
            try:
                data = _json(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                if not isinstance(data, dict) or set(data) != {"revision", "offset"}:
                    _fail("invalid_cursor")
                revision, offset = _revision(data["revision"]), data["offset"]
                if type(offset) is not int or offset < 0:
                    _fail("invalid_cursor")
            except (ValueError, TypeError):
                _fail("invalid_cursor", "Skill cursor is invalid")
        revision, manifest, _path, _digest = self._pin(revision)
        if offset > len(manifest["skills"]):
            _fail("invalid_cursor")
        items = []
        for skill in manifest["skills"][offset:offset+limit]:
            items.append(self._item(skill))
            if len(_encode({"items": items})) > MAX_RESPONSE_BYTES - 2048:
                items.pop()
                break
        end = offset + len(items)
        next_cursor = (base64.urlsafe_b64encode(_encode({"revision": revision, "offset": end})).decode().rstrip("=")
                       if end < len(manifest["skills"]) else None)
        return {"revision": revision, "items": items, "next_cursor": next_cursor, "total": len(manifest["skills"])}

    def search(self, query, limit=10):
        if not isinstance(query, str) or not query.strip() or len(query) > 512 or type(limit) is not int or not 1 <= limit <= 50:
            _fail("invalid_query", "Search query or limit is invalid")
        revision, manifest, _path, _digest = self._pin()
        words = set(re.findall(r"[\w.-]+", query.casefold()))
        chinese = re.findall(r"[\u4e00-\u9fff]+", query)
        for phrase in chinese:
            words.update(phrase[index:index+2] for index in range(len(phrase)-1))
        words = list(words)[:64]
        matches = []
        for skill in manifest["skills"]:
            text = (skill["name"] + " " + skill["description"] + " " + skill["path"]).casefold()
            score = sum(1 for word in words if word in text)
            if score:
                matches.append({**self._item(skill), "score": score})
        matches.sort(key=lambda item: (-item["score"], item["path"]))
        return {"revision": revision, "items": matches[:limit], "total": len(matches)}

    @staticmethod
    def _skill(manifest, skill_id):
        if not isinstance(skill_id, str) or not _HASH.fullmatch(skill_id):
            _fail("unknown_skill", "Skill ID is unknown")
        skill = next((item for item in manifest["skills"] if item.get("id") == skill_id), None)
        if skill is None or not skill.get("available"):
            _fail("unknown_skill", "Skill is unavailable in this snapshot")
        return skill

    def read_skill(self, skill_id, revision=None):
        revision, manifest, root, _digest = self._pin(revision)
        skill = self._skill(manifest, skill_id)
        item = manifest["files"][skill["path"]]
        data = _checked_body(_file(root / "files", skill["path"]), item, MAX_TEXT_BYTES)
        parent = posixpath.dirname(skill["path"])
        resources = [name[len(parent)+1:] if parent else name for name in manifest["files"]
                     if (not parent or name.startswith(parent + "/")) and name != skill["path"]]
        try:
            text, included = _utf8_prefix(data, item["size"])
        except UnicodeError:
            text, included = data.decode("utf-8", "replace"), len(data)
        return _budget({"revision": revision, "skill_id": skill_id, **self._item(skill),
                        "sha256": item["sha256"], "size": item["size"],
                        "text": text, "truncated": item["size"] > included,
                        "references": [dict(record) for record in skill["references"][:64]],
                        "references_truncated": len(skill["references"]) > 64,
                        "resource_paths": sorted(resources)[:64], "resources_truncated": len(resources) > 64})

    def read_resource(self, skill_id, path, revision=None):
        revision, manifest, root, _digest = self._pin(revision)
        skill = self._skill(manifest, skill_id)
        if isinstance(path, str) and path.startswith("@/"):
            name = _relative(path[2:])
            if not any(reference.get("resource_path") == path and reference["status"] == "available"
                       for reference in skill["references"]):
                _fail("unknown_resource", "Resource is not declared by this skill")
        else:
            relative = _relative(path)
            parent = posixpath.dirname(skill["path"])
            name = (parent + "/" if parent else "") + relative
        item = manifest["files"].get(name)
        if item is None:
            _fail("unknown_resource", "Resource is unavailable in this snapshot")
        data = _checked_body(_file(root / "files", name), item, MAX_BINARY_BYTES)
        result = {"revision": revision, "skill_id": skill_id, "path": name,
                  "sha256": item["sha256"], "size": item["size"],
                  "mime_type": mimetypes.guess_type(name)[0] or "application/octet-stream"}
        binary = Path(name).suffix.casefold() in {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".zip", ".docx", ".xlsx", ".pptx", ".exe", ".dll", ".ico"}
        try:
            if binary or b"\0" in data:
                raise UnicodeError()
            text, included = _utf8_prefix(data[:MAX_TEXT_BYTES], item["size"])
            result.update(encoding="utf-8", text=text, truncated=item["size"] > included)
        except UnicodeError:
            result.update(encoding="base64", data=base64.b64encode(data).decode(), truncated=item["size"] > len(data))
        return _budget(result)

    @contextmanager
    def pinned(self, revision=None):
        """Operator batch only: pin once, verify the same manifest again on exit.

        The normal MCP methods continue rehashing metadata on every request.
        Batch reads retain real per-file SHA checks and cannot activate releases.
        """
        pointer = self._pointer() if revision is None else None
        if revision is None:
            if pointer is None:
                _fail("no_active_library", "No library snapshot is active")
            revision = pointer["revision"]
        snapshot = self._pin(revision)
        if pointer is not None and snapshot[3] != pointer["manifest_sha256"]:
            _fail("hash_mismatch", "Catalog manifest integrity check failed")
        reader = _PinnedCatalog(self, snapshot, pointer)
        try:
            yield reader
        finally:
            reader._open = False
            ending = self._pin(snapshot[0])
            if ending[3] != snapshot[3]:
                _fail("hash_mismatch", "Pinned snapshot metadata changed during the batch")

    def activate(self, revision, *, expected_revision=_UNSET):
        """Operator rollback only; not exposed by the MCP reader."""
        revision = _revision(revision)
        before = self._pointer()
        expected = before["revision"] if before else None
        if expected_revision is not _UNSET:
            expected = expected_revision
        manifest = verify_snapshot(self.root / "releases" / revision)
        digest = hashlib.sha256((self.root / "releases" / revision / "manifest.json").read_bytes()).hexdigest()
        with _locked(self.root):
            current = self._pointer()
            actual = current["revision"] if current else None
            if actual != expected:
                raise CatalogConflictError()
            if actual != revision:
                _atomic(self.root / "current.json", {"schema": 1, "revision": revision,
                        "manifest_sha256": digest, "previous_revision": actual})
        return {**self.status(), "changed": actual != revision, "created": False}


class _PinnedCatalog(CatalogStore):
    def __init__(self, parent, snapshot, pointer):
        self.root = parent.root
        self._snapshot = snapshot
        self._previous = pointer["previous_revision"] if pointer else None
        self._open = True

    def _pin(self, revision=None):
        if not self._open:
            _fail("reader_closed", "The pinned catalog reader is closed")
        if revision is not None and _revision(revision) != self._snapshot[0]:
            _fail("pinned_revision_mismatch", "A batch cannot read a different library revision")
        return self._snapshot

    def status(self):
        revision, manifest, _path, digest = self._pin()
        return {**self._summary({"previous_revision": self._previous}, manifest, digest),
                "pinned": True, "revision": revision}

    def activate(self, revision, *, expected_revision=_UNSET):
        _fail("read_only", "A pinned reader cannot change the active library")

    def pinned(self, revision=None):
        _fail("read_only", "Nested catalog batch readers are unsupported")


def publish_snapshot(store, candidate, *, expected_revision=_UNSET):
    """Verify before and after private staging; atomically switch one active pointer."""
    store = store if isinstance(store, CatalogStore) else CatalogStore(store)
    if isinstance(store, _PinnedCatalog):
        _fail("read_only", "A pinned reader cannot publish snapshots")
    before = store._pointer()
    expected = before["revision"] if before else None
    if expected_revision is not _UNSET:
        expected = expected_revision
    candidate = Path(candidate).absolute()
    manifest = verify_snapshot(candidate)
    revision = manifest["revision"]
    digest = hashlib.sha256((candidate / "manifest.json").read_bytes()).hexdigest()
    created = False
    with _locked(store.root):
        current = store._pointer()
        actual = current["revision"] if current else None
        if actual != expected:
            raise CatalogConflictError()
        releases = store.root / "releases"
        releases.mkdir(exist_ok=True)
        _plain(releases, True)
        destination = releases / revision
        if destination.exists():
            verify_snapshot(destination)
            if hashlib.sha256((destination / "manifest.json").read_bytes()).hexdigest() != digest:
                _fail("immutable_conflict", "An immutable revision already has different snapshot data")
        else:
            stage = Path(tempfile.mkdtemp(prefix=".publish-", dir=releases))
            try:
                shutil.copytree(candidate, stage, dirs_exist_ok=True, symlinks=True)
                verify_snapshot(stage)
                if hashlib.sha256((stage / "manifest.json").read_bytes()).hexdigest() != digest:
                    _fail("hash_mismatch", "Snapshot changed during publication")
                os.rename(stage, destination)
                created = True
            except Exception:
                _remove_owned(stage, releases)
                raise
        if actual != revision:
            _atomic(store.root / "current.json", {"schema": 1, "revision": revision,
                    "manifest_sha256": digest, "previous_revision": actual})
    return {**store.status(), "changed": actual != revision, "created": created}
