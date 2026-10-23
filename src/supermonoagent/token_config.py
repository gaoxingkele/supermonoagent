"""Read private connection defaults at runtime; never package credentials."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

MAX_TOKEN_CHARS = 4096
MAX_TOKEN_BYTES = MAX_TOKEN_CHARS * 4 + 3


class TokenConfigurationError(ValueError):
    """A deliberately value-free error safe to show to the local owner."""


def _path(value: str | Path) -> Path:
    text = str(value)
    if not text or len(text) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise TokenConfigurationError("Default Token file path is invalid")
    return Path(value)


def default_token_path(path: str | Path | None = None, *, state_dir: Path | None = None) -> Path | None:
    if path is not None:
        return _path(path)
    configured = os.environ.get("SUPERMONO_TOKEN_FILE", "").strip()
    if configured:
        return _path(configured)
    state = _path(state_dir) if state_dir is not None else Path.home() / ".supermonoagent"
    pointer = state / "token-source.json"
    if pointer.exists():
        try:
            with pointer.open("rb") as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError()
            value = json.loads(raw.decode("utf-8-sig"))
            source = value.get("path") if isinstance(value, dict) else None
            if not isinstance(source, str) or not source.strip() or "\0" in source:
                raise ValueError()
            candidate = _path(source)
            return candidate if candidate.is_absolute() else state / candidate
        except (OSError, ValueError, UnicodeError) as exc:
            raise TokenConfigurationError("Default Token file configuration is invalid") from exc
    candidates = [Path.cwd() / "token.txt"]
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable)
        candidates += [executable.parent / "token.txt", executable.parent.parent / "token.txt"]
    candidates.append(state / "token.txt")
    return next((candidate for candidate in candidates if candidate.exists()), None)


def read_default_token(path: str | Path | None = None, *, state_dir: Path | None = None) -> str:
    source = default_token_path(path, state_dir=state_dir)
    if source is None:
        return ""
    try:
        with source.open("rb") as stream:
            raw = stream.read(MAX_TOKEN_BYTES + 1)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise TokenConfigurationError("Default Token file is not readable") from exc
    try:
        if len(raw) > MAX_TOKEN_BYTES:
            raise ValueError()
        token = raw.decode("utf-8-sig").strip()
        if (not token or len(token) > MAX_TOKEN_CHARS
                or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token)):
            raise ValueError()
    except (UnicodeError, ValueError) as exc:
        raise TokenConfigurationError("Default Token file must contain one token of 1 to 4096 characters") from exc
    return token
