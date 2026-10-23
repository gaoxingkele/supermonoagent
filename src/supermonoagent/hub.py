"""Windows own CLI sessions. A direct client receives the CLI it is viewing. A relay client receives every open window."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .protocol import (
    CAPABILITIES,
    OP_HELLO_OK,
    OP_OUTPUT,
    OP_SESSIONS,
    OP_SNAPSHOT,
    OP_TASK_EVENT,
    fit_payload,
    pack,
    pack_feed,
    pack_json,
)
from .pty import PtyClosed, WinPty, default_cwd, shell_command
from .pump import OutputPump
from .task_events import DEFAULT_SUMMARY, TaskEventStore, TaskStateDetector


@dataclass
class Session:
    id: str
    window_id: str
    shell: str
    cwd: str
    pty: object
    pump: OutputPump = field(default_factory=OutputPump)
    stop: threading.Event = field(default_factory=threading.Event)
    resize_owner: str | None = None
    cols: int = 100
    rows: int = 28
    io_lock: threading.Lock = field(default_factory=threading.Lock)
    task_detector: TaskStateDetector = field(default_factory=TaskStateDetector)


@dataclass
class Group:
    id: str
    name: str
    channel: str
    owner: str = ""


@dataclass
class Window:
    id: str
    title: str
    group_id: str = "default"
    sessions: dict[str, Session] = field(default_factory=dict)
    focused: str | None = None
    tab_theme: str = ""


@dataclass
class Client:
    key: str
    queue: asyncio.Queue
    window_id: str | None = None
    session_id: str | None = None
    channel: str = ""
    token: str = ""
    multiplex: bool = False
    reliable: bool = False
    subscriptions: set[str] = field(default_factory=set)
    failure: str = ""
    feed_offsets: dict[str, int] = field(default_factory=dict)
    delegated: bool = False
    authorize: Callable[[], bool] | None = field(default=None, repr=False)
    task_subscribed: bool = False


DEFAULT_PRESETS = (
    {"id": "powershell", "label": "PowerShell", "command": "powershell", "shortcut": "Ctrl+Alt+1"},
    {"id": "pwsh", "label": "pwsh", "command": "pwsh", "shortcut": "Ctrl+Alt+2"},
    {"id": "cmd", "label": "cmd", "command": "cmd", "shortcut": "Ctrl+Alt+3"},
    {"id": "claude", "label": "claude", "command": "claude", "shortcut": "Ctrl+Alt+4"},
    {"id": "codex", "label": "codex", "command": "codex", "shortcut": "Ctrl+Alt+5"},
)

TAB_THEMES = frozenset({"ink", "red", "blue", "green", "purple", "amber", "cyan"})


def _desktop_path() -> Path:
    return Path(os.environ.get("SUPERMONO_STATE_DIR") or Path.home() / ".supermonoagent") / "desktop.json"


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")
_PROMPT = re.compile(r"(?:PS [^>]{0,80}>|[A-Za-z]:\\[^>]{0,80}>|[$#] )(.+)$")


def short_title(shell: str, raw: bytes) -> str:
    """Shell name plus the latest command or a short line from the screen."""
    name = (shell or "cli").strip().split()[0] or "cli"
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    if name.lower().endswith(".exe"):
        name = name[:-4]
    text = _ANSI.sub("", raw.decode("utf-8", "replace"))
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    command = ""
    for line in lines:
        match = _PROMPT.search(line)
        if match and match.group(1).strip():
            command = match.group(1).strip()
    snippet = command
    if not snippet:
        for line in reversed(lines):
            if line.endswith(">") or line.endswith("$") or line.endswith("#"):
                continue
            snippet = line
            break
    snippet = snippet[:18].strip()
    if snippet:
        return f"{name} · {snippet}"[:40]
    return name[:40]


class Hub:
    def __init__(self, pty_factory=None) -> None:
        self.pty_factory = pty_factory
        self.windows: dict[str, Window] = {}
        self.sessions: dict[str, Session] = {}
        self.clients: dict[str, Client] = {}
        self.groups: dict[str, Group] = {}
        self.presets: list[dict] = [dict(item) for item in DEFAULT_PRESETS]
        self.persist = False
        self._window_no = 0
        self.task_events = TaskEventStore()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._seq = 0
        self.default_owner = ""
        self.machine = ""
        self.project = "默认"
        self._ensure_default()

    def set_identity(self, machine: str, project: str) -> None:
        """Name this computer and the one project it belongs to. Windows stay here."""
        self.machine = str(machine or "")[:40]
        self.project = str(project or "默认")[:40] or "默认"

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def attach(self, queue: asyncio.Queue, key: str | None = None, token: str = "") -> Client:
        with self._lock:
            self._seq += 1
            client = Client(key or f"c{self._seq}", queue, token=token)
            self.clients[client.key] = client
            return client

    def detach(self, key: str) -> None:
        released = False
        with self._lock:
            self.clients.pop(key, None)
            for session in self.sessions.values():
                if session.resize_owner == key:
                    session.resize_owner = None
                    released = True
        if released:
            self._catalog()

    def summary(self, owner: str | None = None) -> dict:
        groups = list(self.groups.values())
        if owner is not None:
            groups = [group for group in groups if group.owner == owner]
        owned = {group.id for group in groups}
        windows = [window for window in self.windows.values() if owner is None or window.group_id in owned]
        return {
            "capabilities": CAPABILITIES,
            "windows": [
                {
                    "id": window.id,
                    "title": window.title,
                    "tab_theme": window.tab_theme,
                    "group": window.group_id,
                    "focused": window.focused,
                    "sessions": [
                        {"id": item.id, "shell": item.shell, "cwd": item.cwd,
                         **({"agent": item.task_detector.app} if item.task_detector.app else {})}
                        for item in window.sessions.values()
                    ],
                }
                for window in windows
            ],
            "groups": [
                {"id": group.id, "name": group.name, "channel": group.channel}
                for group in groups
            ],
            "presets": self.presets,
            "machine": self.machine,
            "project": self.project,
        }

    def load_desktop(self) -> None:
        """Read group names and command presets. Tests leave persist off."""
        self.persist = True
        path = _desktop_path()
        if not path.exists():
            self._save()
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        groups: dict[str, Group] = {}
        for item in data.get("groups") or []:
            if not isinstance(item, dict):
                continue
            gid = str(item.get("id") or "")
            if not gid:
                continue
            groups[gid] = Group(
                id=gid,
                name=str(item.get("name") or gid)[:40],
                channel=str(item.get("channel") or gid)[:40],
                owner=str(item.get("owner") or self.default_owner),
            )
        if groups:
            self.groups = groups
            self._ensure_default()
        presets = []
        for item in data.get("presets") or []:
            preset = self._clean_preset(item)
            if preset:
                presets.append(preset)
        if presets:
            self.presets = presets

    def _ensure_default(self) -> None:
        if "default" not in self.groups:
            self.groups["default"] = Group("default", "默认", "default", self.default_owner)
        elif not self.groups["default"].owner and self.default_owner:
            self.groups["default"].owner = self.default_owner

    def ensure_owner(self, owner: str) -> Group:
        owned = [group for group in self.groups.values() if group.owner == owner]
        for group in owned:
            if group.id == "default":
                return group
        if owned:
            return owned[0]
        if owner == self.default_owner and "default" not in self.groups:
            group = Group("default", "默认", "default", owner)
        else:
            group = Group(secrets.token_hex(3), "默认", self._unique_channel("默认"), owner)
        self.groups[group.id] = group
        self._save()
        return group

    def owns(self, token: str, window_id: str) -> bool:
        window = self.windows.get(window_id)
        if window is None:
            return False
        group = self.groups.get(window.group_id)
        return group is not None and group.owner == token

    def _save(self) -> None:
        if not self.persist:
            return
        path = _desktop_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "groups": [
                {"id": group.id, "name": group.name, "channel": group.channel, "owner": group.owner}
                for group in self.groups.values()
            ],
            "presets": self.presets,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _unique_channel(self, text: str, keep: str | None = None) -> str:
        raw = "".join(ch if ch not in "\0\r\n\t " else "-" for ch in (text or "").strip())
        base = raw.strip("-")[:40] or "ch"
        used = {group.channel for gid, group in self.groups.items() if gid != keep}
        if base not in used:
            return base
        number = 2
        while f"{base}-{number}" in used:
            number += 1
        return f"{base}-{number}"

    def _clean_preset(self, item) -> dict | None:
        if not isinstance(item, dict):
            return None
        command = str(item.get("command") or "").strip()
        label = str(item.get("label") or command).strip()
        if not command or not label:
            return None
        shortcut = str(item.get("shortcut") or "").strip()[:40]
        preset_id = str(item.get("id") or "").strip() or secrets.token_hex(3)
        return {"id": preset_id[:40], "label": label[:40], "command": command[:240], "shortcut": shortcut}

    def add_group(self, name: str, channel: str | None = None, owner: str = "") -> Group:
        self.ensure_owner(owner)
        label = (name or "").strip()[:40] or "分组"
        group = Group(
            id=secrets.token_hex(3),
            name=label,
            channel=self._unique_channel(channel or label),
            owner=owner,
        )
        self.groups[group.id] = group
        self._save()
        self._catalog()
        return group

    def rename_group(self, group_id: str, name: str | None, channel: str | None, owner: str | None = None) -> None:
        group = self.groups.get(group_id or "")
        if group is None or (owner is not None and group.owner != owner):
            return
        if name and name.strip():
            group.name = name.strip()[:40]
        if channel and channel.strip():
            group.channel = self._unique_channel(channel, keep=group.id)
        self._save()
        self._catalog()

    def delete_group(self, group_id: str, owner: str | None = None) -> None:
        group = self.groups.get(group_id or "")
        if group is None or group_id == "default":
            return
        if owner is not None and group.owner != owner:
            return
        token = group.owner
        self.groups.pop(group_id, None)
        home = self.ensure_owner(token).id
        for window in self.windows.values():
            if window.group_id == group_id:
                window.group_id = home
        self._save()
        self._catalog()

    def rename_window(self, window_id: str, title: str) -> None:
        window = self.windows.get(window_id or "")
        label = (title or "").strip()[:40]
        if window is None or not label:
            return
        window.title = label
        self._catalog()

    def autotitle(self, window_id: str) -> None:
        window = self.windows.get(window_id or "")
        if window is None:
            return
        session = window.sessions.get(window.focused or "")
        if session is None and window.sessions:
            session = next(iter(window.sessions.values()))
        if session is None:
            return
        window.title = short_title(session.shell, session.pump.snapshot())
        self._catalog()

    def set_tab_theme(self, window_id: str, theme: str) -> None:
        window = self.windows.get(window_id)
        if window is None or (theme and theme not in TAB_THEMES):
            return
        window.tab_theme = theme
        self._catalog()

    def move_window(self, window_id: str, group_id: str, owner: str | None = None) -> None:
        window = self.windows.get(window_id or "")
        target = self.groups.get(group_id or "")
        if window is None or target is None:
            return
        if owner is not None and (not self.owns(owner, window.id) or target.owner != owner):
            return
        window.group_id = group_id
        self._catalog()

    def save_presets(self, items) -> None:
        cleaned = []
        for item in items or []:
            preset = self._clean_preset(item)
            if preset:
                cleaned.append(preset)
        if not cleaned:
            return
        self.presets = cleaned
        self._save()
        self._catalog()

    def subscribe(self, client: Client, channel: str) -> None:
        channel = str(channel or "")
        if not any(group.channel == channel and group.owner == client.token for group in self.groups.values()):
            return
        client.channel = channel
        session = self.sessions.get(client.session_id or "")
        if session is not None and self._channel_ok(client, session):
            self._send(client, self._session_snapshot(client, session))

    def open_window(self, title: str | None = None, group_id: str | None = None, owner: str = "") -> Window:
        home = self.ensure_owner(owner)
        chosen = group_id if group_id in self.groups and self.groups[group_id].owner == owner else home.id
        self._window_no += 1
        label = (title or "").strip()[:40] or f"窗口 {self._window_no}"
        window = Window(
            id=secrets.token_hex(3),
            title=label,
            group_id=chosen,
        )
        self.windows[window.id] = window
        self._catalog()
        return window

    def open_cli(self, window_id: str, shell: str, cwd: str | None = None) -> Session:
        if self.loop is None:
            raise RuntimeError("loop is not attached")
        window = self.windows.get(window_id)
        if window is None:
            raise KeyError(window_id)
        workdir = cwd or default_cwd()
        pty = self._spawn(shell, workdir)
        session = Session(
            id=secrets.token_hex(4),
            window_id=window.id,
            shell=shell,
            cwd=workdir,
            pty=pty,
        )
        with self._lock:
            window.sessions[session.id] = session
            window.focused = session.id
        self.sessions[session.id] = session
        threading.Thread(target=self._read, args=(session,), name=f"pty-{session.id}", daemon=True).start()
        self._catalog()
        return session

    def select(self, client: Client, window_id: str) -> None:
        window = self.windows.get(window_id)
        if window is None or not self.owns(client.token, window.id):
            return
        with self._lock:
            client.window_id = window.id
            client.session_id = window.focused
        self._send(client, self._feed_snapshot(client))

    def focus(self, client: Client, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if session is None or session.window_id != client.window_id or not self.owns(client.token, session.window_id):
            return
        with self._lock:
            client.session_id = session.id
        self._send(client, self._session_snapshot(client, session))

    def follow(self, client: Client, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if session is None or not self.owns(client.token, session.window_id):
            return
        with self._lock:
            client.window_id = session.window_id
            client.session_id = session.id
        self._send(client, self._session_snapshot(client, session))

    def write(self, client: Client, data: bytes) -> None:
        session = self.view(client)
        if session is None or not data:
            return
        with session.io_lock:
            if self.view(client) is session:
                session.pty.write(data)

    def set_subscriptions(self, client: Client, session_ids: list[str]) -> None:
        if not client.multiplex or client.failure:
            return
        selected = {
            sid for sid in session_ids
            if isinstance(sid, str) and sid in self.sessions and self._channel_ok(client, self.sessions[sid])
        }
        added = selected - client.subscriptions
        removed = client.subscriptions - selected
        client.subscriptions = selected
        released = False
        for sid in removed:
            client.feed_offsets.pop(sid, None)
            session = self.sessions.get(sid)
            if session is not None and session.resize_owner == client.key:
                session.resize_owner = None
                released = True
        for sid in sorted(added):
            session = self.sessions[sid]
            # Keep interactive output batches short without one wakeup per read.
            session.pump.coalesce_s = min(session.pump.coalesce_s, 0.004)
            self._send(client, self._session_snapshot(client, session))
        if released:
            self._catalog()

    def subscribed_session(self, client: Client, session_id: str) -> Session | None:
        if (client.failure or self.clients.get(client.key) is not client
                or (client.authorize is not None and not client.authorize())):
            return None
        session = self.sessions.get(session_id)
        if not client.multiplex or session_id not in client.subscriptions or session is None:
            return None
        return session if self._channel_ok(client, session) else None

    def write_session(self, client: Client, session_id: str, data: bytes) -> bool:
        session = self.subscribed_session(client, session_id)
        if session is None or not data:
            return False
        with session.io_lock:
            if self.subscribed_session(client, session_id) is not session:
                return False
            session.pty.write(data)
        return True

    def resize_session(self, client: Client, session_id: str, cols: int, rows: int, take_control: bool = False) -> bool:
        session = self.subscribed_session(client, session_id)
        if session is None:
            return False
        with session.io_lock:
            if self.subscribed_session(client, session_id) is not session:
                return False
            if session.resize_owner not in {None, client.key} and not take_control:
                return False
            cols, rows = self._terminal_size(cols, rows)
            changed = (session.cols, session.rows, session.resize_owner) != (cols, rows, client.key)
            if (session.cols, session.rows) != (cols, rows):
                session.pty.resize(cols, rows)
            if self.clients.get(client.key) is not client:
                return False
            session.resize_owner = client.key
            session.cols, session.rows = cols, rows
        if changed:
            self._catalog()
        return True

    @staticmethod
    def _terminal_size(cols: int, rows: int) -> tuple[int, int]:
        return max(2, min(cols, 500)), max(1, min(rows, 200))

    def controls(self, client: Client) -> dict:
        return {
            sid: {"cols": session.cols, "rows": session.rows, "owner": session.resize_owner == client.key,
                  "busy": session.resize_owner not in {None, client.key}}
            for sid in client.subscriptions
            if (session := self.sessions.get(sid)) is not None and self._channel_ok(client, session)
        }

    def resize(self, client: Client, cols: int, rows: int) -> None:
        session = self.view(client)
        if session is None:
            return
        with session.io_lock:
            if session.resize_owner is not None and session.resize_owner != client.key:
                return
            cols, rows = self._terminal_size(cols, rows)
            changed = (session.cols, session.rows) != (cols, rows)
            if changed:
                session.pty.resize(cols, rows)
                session.cols, session.rows = cols, rows
        if changed:
            self._catalog()

    def close_session(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if session is None:
            return
        session.stop.set()
        session.pty.close()
        window = self.windows.get(session.window_id)
        watchers = []
        with self._lock:
            if window is not None:
                window.sessions.pop(session_id, None)
                if window.focused == session_id:
                    window.focused = next(iter(window.sessions), None)
                for client in self.clients.values():
                    client.subscriptions.discard(session_id)
                    if client.session_id == session_id:
                        client.session_id = window.focused
                        watchers.append(client)
        for client in watchers:
            self._send(client, self._feed_snapshot(client))
            self._send(client, self._hello(client))
        self._catalog()

    def close_window(self, window_id: str) -> None:
        window = self.windows.pop(window_id, None)
        if window is None:
            return
        for session_id in list(window.sessions):
            session = self.sessions.pop(session_id, None)
            if session is None:
                continue
            session.stop.set()
            session.pty.close()
        affected = []
        with self._lock:
            for client in self.clients.values():
                if client.window_id == window_id:
                    client.window_id = None
                    client.session_id = None
                    affected.append(client)
        self._catalog()
        for client in affected:
            self._send(client, pack(OP_SNAPSHOT, b""))
            self._send(client, self._hello(client))

    def snapshot(self, client: Client) -> bytes:
        session = self.view(client)
        if session is None:
            return b""
        return fit_payload(session.pump.snapshot(), snapshot=True)

    def view(self, client: Client) -> Session | None:
        with self._lock:
            return self._view_locked(client)

    def _spawn(self, shell: str, cwd: str):
        if self.pty_factory is not None:
            return self.pty_factory(shell, cwd)
        return WinPty(shell_command(shell), cwd)

    def _view_locked(self, client: Client) -> Session | None:
        if client.authorize is not None and not client.authorize():
            return None
        window = self.windows.get(client.window_id or "")
        if window is None or not self.owns(client.token, window.id):
            return None
        if client.session_id in window.sessions:
            return window.sessions[client.session_id]
        if window.focused in window.sessions:
            return window.sessions[window.focused]
        return None

    def _watched(self, session: Session) -> bool:
        with self._lock:
            return any(self._view_locked(client) is session for client in self.clients.values())

    def _watchers(self, session: Session) -> list[Client]:
        with self._lock:
            return [client for client in self.clients.values() if self._view_locked(client) is session]

    def _hello(self, client: Client) -> bytes:
        return pack_json(
            OP_HELLO_OK,
            {**self.summary(client.token), "selected": client.window_id or "", "session": client.session_id or "",
             **({"controls": self.controls(client)} if client.multiplex else {})},
        )

    def _read(self, session: Session) -> None:
        while not session.stop.is_set():
            try:
                chunk = session.pty.read(4096)
            except PtyClosed:
                break
            except Exception:
                break
            if not chunk:
                break
            # Observe fresh bytes before overflow/history coalescing, even when
            # no terminal viewer has subscribed to this session.
            self.loop.call_soon_threadsafe(self._task_output, session.id, chunk)
            session.pump.remember(chunk)
            session.pump.stage(
                chunk,
                self.loop,
                lambda data, overflow, sid=session.id: self._flushed(sid, data, overflow),
            )
        self.loop.call_soon_threadsafe(self.close_session, session.id)

    def subscribe_task_events(self, client: Client, after: str = "", replay: bool = True) -> list[bytes]:
        if (self.clients.get(client.key) is not client or client.failure
                or (client.authorize is not None and not client.authorize())):
            return []
        client.task_subscribed = True
        channel = client.channel if client.key.startswith("relay-") else ""
        events = self.task_events.replay(client.token, after if replay else "", channel)
        if not replay:
            return [pack_json(OP_TASK_EVENT, {"type": "cursor", "cursor": events[-1]["event_id"] if events else ""})]
        return [pack_json(OP_TASK_EVENT, event) for event in events]

    def _task_output(self, session_id: str, data: bytes) -> None:
        session = self.sessions.get(session_id)
        if session is None:
            return
        previous_app = session.task_detector.app
        completed = session.task_detector.feed(data)
        if session.task_detector.app != previous_app:
            self._catalog()
        window = self.windows.get(session.window_id)
        group = self.groups.get(window.group_id) if window is not None else None
        if group is None:
            return
        for _ in range(completed):
            event = self.task_events.append(group.owner, {
                "session": session.id, "window": window.id, "title": window.title,
                "machine": self.machine, "project": self.project,
                "status": "completed", "summary": DEFAULT_SUMMARY,
                "agent": session.task_detector.app,
            }, group.channel)
            for client in list(self.clients.values()):
                if client.task_subscribed and self._channel_ok(client, session):
                    self._send(client, pack_json(OP_TASK_EVENT, event))

    def _feed_frame(self, client: Client, op: int, session_id: str, data: bytes) -> bytes:
        # The session id is only for a relay client that receives every window.
        # A direct pane must get raw terminal bytes, or the id is painted on the prompt.
        if client.multiplex or client.key.startswith("relay-"):
            return pack_feed(op, session_id, data)
        return pack(op, fit_payload(data, snapshot=op == OP_SNAPSHOT))

    def _feed_snapshot(self, client: Client) -> bytes:
        session = self.view(client)
        if session is None:
            return pack(OP_SNAPSHOT, b"")
        return self._session_snapshot(client, session)

    def _session_snapshot(self, client: Client, session: Session) -> bytes:
        data, position = session.pump.snapshot_position()
        if client.multiplex:
            client.feed_offsets[session.id] = position
        return self._feed_frame(client, OP_SNAPSHOT, session.id, data)

    def _channel_ok(self, client: Client, session: Session) -> bool:
        window = self.windows.get(session.window_id)
        group = self.groups.get(window.group_id) if window is not None else None
        if group is None or group.owner != client.token:
            return False
        if not client.key.startswith("relay-") or not client.channel:
            return True
        return group.channel == client.channel

    def _feed_targets(self, session: Session) -> list[Client]:
        with self._lock:
            targets = []
            for client in self.clients.values():
                if client.multiplex:
                    if session.id in client.subscriptions and self._channel_ok(client, session):
                        targets.append(client)
                    continue
                # A relay client receives every open session on its channel.
                if client.key.startswith("relay-") and client.window_id:
                    if self._channel_ok(client, session):
                        targets.append(client)
                elif self._view_locked(client) is session:
                    targets.append(client)
            return targets

    def _flushed(self, session_id: str, data: bytes, overflow: bool) -> None:
        session = self.sessions.get(session_id)
        if session is None:
            return
        targets = self._feed_targets(session)
        if not targets:
            return
        op = OP_SNAPSHOT if overflow else OP_OUTPUT
        for client in targets:
            if overflow and client.reliable:
                self.fail_client(client, "PTY output overflow; reconnect for bounded history replay")
                continue
            chunk = data
            end = session.pump.flushed_position
            if client.multiplex and not overflow and end:
                # A new subscriber's snapshot can already contain this pending batch.
                seen = client.feed_offsets.get(session_id, 0)
                if seen >= end:
                    continue
                chunk = data[max(0, seen - (end - len(data))):]
                client.feed_offsets[session_id] = end
            self._send(client, self._feed_frame(client, op, session_id, chunk))

    def _catalog(self) -> None:
        for client in list(self.clients.values()):
            summary = self.summary(client.token)
            if client.multiplex:
                summary["controls"] = self.controls(client)
            self._send(client, pack_json(OP_SESSIONS, summary))

    def _send(self, client: Client, frame: bytes) -> None:
        if client.failure or (client.authorize is not None and not client.authorize()):
            return
        try:
            client.queue.put_nowait(frame)
        except asyncio.QueueFull:
            if client.reliable:
                self.fail_client(client, "Slow client output queue overflow; reconnect for bounded history replay")
                return
            try:
                client.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                client.queue.put_nowait(frame)
            except asyncio.QueueFull:
                self.detach(client.key)

    def fail_client(self, client: Client, reason: str) -> None:
        if client.failure:
            return
        client.failure = reason
        self.detach(client.key)
        while not client.queue.empty():
            client.queue.get_nowait()
        client.queue.put_nowait(None)
