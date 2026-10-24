"""Provider-neutral assistant preferences, with no model or credential access."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
from urllib.parse import urlsplit


MAX_BYTES = 16 * 1024
DEFAULTS = {"provider": "unconfigured", "model": "", "endpoint": "",
            "auto_context": False, "max_context_chars": 8192}
_PROVIDERS = frozenset({"unconfigured", "codex", "api"})


def _invalid():
    return ValueError("Assistant preferences are invalid")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


def _validate(payload):
    if not isinstance(payload, dict) or set(payload) != set(DEFAULTS):
        raise _invalid()
    provider, model, endpoint = (payload[key] for key in ("provider", "model", "endpoint"))
    if (not isinstance(provider, str) or provider not in _PROVIDERS
            or not isinstance(model, str) or len(model) > 128
            or model and not model.isprintable()
            or not isinstance(endpoint, str)
            or type(payload["auto_context"]) is not bool
            or type(payload["max_context_chars"]) is not int
            or not 512 <= payload["max_context_chars"] <= 32768):
        raise _invalid()
    if provider != "api":
        if endpoint:
            raise _invalid()
    else:
        if (not model.strip() or not endpoint or len(endpoint) > MAX_BYTES
                or not endpoint.isprintable() or any(char.isspace() for char in endpoint)
                or any(char in endpoint for char in "\\?#")):
            raise _invalid()
        try:
            parts = urlsplit(endpoint)
            port = parts.port
            if (not parts.hostname or parts.username is not None or parts.password is not None
                    or parts.query or parts.fragment or port is not None and not 1 <= port <= 65535
                    or parts.scheme not in {"https", "http"}
                    or parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}):
                raise _invalid()
        except ValueError:
            raise _invalid() from None
    return dict(payload)


def _result(preferences, status):
    return {"preferences": dict(preferences), "status": status}


class AssistantPreferences:
    """Atomic settings storage. ``save`` accepts a full five-field object."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def get(self):
        with self._lock:
            try:
                with self.path.open("rb") as stream:
                    raw = stream.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise _invalid()
                envelope = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object)
                if (not isinstance(envelope, dict) or set(envelope) != {"schema", "preferences"}
                        or type(envelope["schema"]) is not int or envelope["schema"] != 1):
                    raise _invalid()
                return _result(_validate(envelope["preferences"]), "saved")
            except FileNotFoundError:
                return _result(DEFAULTS, "default")
            except (OSError, ValueError, TypeError, RecursionError):
                return _result(DEFAULTS, "invalid")

    def save(self, payload):
        preferences = _validate(payload)
        raw = json.dumps({"schema": 1, "preferences": preferences}, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_BYTES:
            raise _invalid()
        temporary = None
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="wb", dir=self.path.parent,
                        prefix="." + self.path.name + ".", suffix=".tmp", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                return _result(preferences, "saved")
            except OSError:
                raise ValueError("Assistant preferences could not be saved") from None
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
