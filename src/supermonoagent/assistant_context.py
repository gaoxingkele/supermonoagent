"""Bounded, read-only project evidence for the desktop assistant.

The daemon supplies Session.cwd; terminal output never chooses a filesystem path.
This reads generated wiki pages and does not initialize or advance metaRSI.
"""
from __future__ import annotations

from pathlib import Path
import stat

from .mylib_assistant import SkillIndex


PAGE_LIMIT = 8192
_PAGES = ("README.md", "negatives.md", "trials.md", "nodes.md", "ideas.md")
_PATH_ERRORS = (OSError, ValueError, TypeError, RuntimeError)


def _canonical(value) -> Path:
    if value is None or isinstance(value, str) and not value.strip():
        raise ValueError("Missing directory")
    path = Path(value)
    if "\0" in str(path):
        raise ValueError("Invalid directory")
    return path.resolve()


def _contained(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Project evidence escaped its root")
    return path


def _inventory(library_root) -> dict:
    result = {"available": False, "status": "unavailable", "indexed": 0,
              "skill_names": [], "truncated": False}
    try:
        root = _canonical(library_root)
        if not stat.S_ISDIR(root.stat().st_mode):
            result["status"] = "missing"
            return result
        index = SkillIndex(root)
        names = list(dict.fromkeys(card["name"] for card in index.cards))
        return {"available": True, "status": "ready" if names else "empty",
                "indexed": len(index.cards), "skill_names": names[:256],
                "truncated": len(names) > 256}
    except FileNotFoundError:
        result["status"] = "missing"
        return result
    except _PATH_ERRORS:
        return result


def _page(root: Path, name: str) -> dict:
    try:
        path = _contained(root, ".metarsi/wiki/" + name)
        with path.open("rb") as stream:
            data = stream.read(PAGE_LIMIT + 1)
            modified = path.stat().st_mtime
        return {"status": "ready", "text": data[:PAGE_LIMIT].decode("utf-8", "replace"),
                "truncated": len(data) > PAGE_LIMIT, "mtime": modified}
    except FileNotFoundError:
        return {"status": "missing", "text": "", "truncated": False}
    except _PATH_ERRORS:
        return {"status": "unavailable", "text": "", "truncated": False}


def read_project_context(project_root, library_root) -> dict:
    """Return startup-directory evidence and local skill names, without mutation.

    ``ledger.pages`` contains only fixed generated Markdown pages. Its enabled
    flag means a local constitution exists; it is not a claim about admission,
    an evolution mode, or the integrity of the append-only database.
    """
    result = {"project": None, "project_truncated": False, "project_source": "session-startup-directory",
              "project_status": "unavailable",
              "note": "使用终端启动目录；不会跟踪 shell 后续的 cd，也不推断账本模式。",
              "inventory": _inventory(library_root),
              "ledger": {"enabled": False, "status": "unavailable", "pages": {}}}
    try:
        root = _canonical(project_root)
        display = str(root)
        result["project"], result["project_truncated"] = display[:4096], len(display) > 4096
        try:
            directory = stat.S_ISDIR(root.stat().st_mode)
        except FileNotFoundError:
            directory = False
        if not directory:
            result["project_status"] = "missing"
            return result
        result["project_status"] = "ready"
        constitution = _contained(root, ".metarsi/constitution.json")
        try:
            enabled = stat.S_ISREG(constitution.stat().st_mode)
        except FileNotFoundError:
            enabled = False
        if not enabled:
            result["ledger"]["status"] = "not_enabled"
            return result
        result["ledger"] = {"enabled": True, "status": "ready",
                            "pages": {name: _page(root, name) for name in _PAGES}}
        if any(page["status"] == "unavailable" for page in result["ledger"]["pages"].values()):
            result["ledger"]["status"] = "partial"
        return result
    except _PATH_ERRORS:
        return result
