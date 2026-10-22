"""Explicit agent activity transitions and bounded, owner-isolated event replay.

Only feed fresh PTY bytes to the detector, never history/snapshot frames. An idle
marker means the agent returned to input, not that its work passed validation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_SUMMARY = "任务已结束，点击查看终端结果。"
REPLAY_LIMIT = 24
_STATE = re.compile(rb"(?:^|[;:])state=([a-z]+)(?:[;:]|$)")
_APP = re.compile(rb"(?:^|[;:])app=([a-zA-Z0-9_-]{1,40})(?:[;:]|$)")


class TaskStateDetector:
    """Read fragmented OSC 7501; ordinary output and first idle are silent."""

    def __init__(self) -> None:
        self.pending = b""
        self.running = False
        self.app = ""

    def feed(self, data: bytes) -> int:
        data = self.pending + data
        self.pending = b""
        cursor = 0
        completed = 0
        while True:
            start = data.find(b"\x1b]", cursor)
            if start < 0:
                self.pending = b"\x1b" if data.endswith(b"\x1b") else b""
                return completed
            endings = [at for at in (data.find(b"\x07", start + 2), data.find(b"\x1b\\", start + 2)) if at >= 0]
            if not endings:
                # Malformed/unbounded OSC must not retain arbitrary terminal text.
                self.pending = data[start:] if len(data) - start <= 512 else (b"\x1b" if data.endswith(b"\x1b") else b"")
                return completed
            end = min(endings)
            payload = data[start + 2:end]
            cursor = end + (1 if data[end] == 7 else 2)
            if len(payload) > 512 or not payload.startswith(b"7501;"):
                continue
            state = _STATE.search(payload[5:])
            if not state:
                continue
            app = _APP.search(payload[5:])
            if app:
                self.app = app[1].decode("ascii")
            value = state[1]
            if value in {b"working", b"running", b"busy"}:
                self.running = True
            elif value == b"idle":
                completed += int(self.running)
                self.running = False
            elif value == b"clear":
                self.running = False
                self.app = ""


class TaskEventStore:
    """Atomic local records contain hashed owners, never authentication tokens."""

    def __init__(self, path: Path | None = None, limit: int = 256) -> None:
        self.path = path
        self.limit = max(1, min(limit, 1024))
        self.records: list[dict] = []
        if path is not None:
            try:
                if path.stat().st_size > 2 * 1024 * 1024:
                    return
                data = json.loads(path.read_text(encoding="utf8"))
                self.records = [item for item in data.get("events", []) if isinstance(item, dict)
                                and isinstance(item.get("event"), dict)
                                and isinstance(item.get("owner"), str)
                                and isinstance(item.get("channel"), str)][-self.limit:]
            except (OSError, ValueError, AttributeError, TypeError):
                pass

    @staticmethod
    def _owner(owner: str) -> str:
        return hashlib.sha256(owner.encode("utf8")).hexdigest()

    def append(self, owner: str, event: dict, channel: str = "") -> dict:
        # Whitelist public fields so callers cannot accidentally leak credentials.
        record = {key: str(event.get(key) or "")[:400] for key in (
            "session", "window", "title", "machine", "project", "status", "summary", "agent")}
        record["event_id"] = secrets.token_hex(16)
        record["created_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        self.records.append({"owner": self._owner(owner), "channel": channel, "event": record})
        self.records = self.records[-self.limit:]
        if self.path is not None:
            temporary = self.path.with_name(self.path.name + "." + secrets.token_hex(4) + ".tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary.write_text(json.dumps({"events": self.records}, ensure_ascii=False), encoding="utf8")
                temporary.replace(self.path)
            except OSError:
                # Notification delivery still works for this daemon lifetime;
                # storage failure must never interrupt terminal output handling.
                logging.getLogger(__name__).warning("Could not persist task notification history", exc_info=True)
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return dict(record)

    def replay(self, owner: str, after: str = "", channel: str = "") -> list[dict]:
        found = [item["event"] for item in self.records if item["owner"] == self._owner(owner)
                 and (not channel or item["channel"] == channel)]
        if after:
            for index, event in enumerate(found):
                if event.get("event_id") == after:
                    found = found[index + 1:]
                    break
        return [dict(event) for event in found[-REPLAY_LIMIT:]]
