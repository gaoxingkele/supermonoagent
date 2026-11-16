"""Owned Codex 0.167 app-server conversation, separate from native sessions.

The coordinator owns durability. This adapter supplies only correlated protocol
evidence, and sends every mutation through the transport's physical write fence.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
import uuid

from .managed_process import (JsonLineProcess, ManagedError, inherited_environment,
                              resolve_native_binary, verify_version)

VERSION = "0.167.0"
_FRAME_LIMIT = 4 * 1024 * 1024
_PENDING_LIMIT = 8 * 1024 * 1024
_IDENTITIES_LIMIT = 4096
_APPROVALS = {"item/commandExecution/requestApproval": "commandExecution",
              "item/fileChange/requestApproval": "fileChange"}


def _identity(value):
    return isinstance(value, str) and 0 < len(value) <= 512


def _rpc_key(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ManagedError("invalid_protocol")
    if isinstance(value, str) and not _identity(value):
        raise ManagedError("invalid_protocol")
    return type(value).__name__, value


class CodexManagedDriver:
    def __init__(self, cwd, binding, emit, can_send, *, binary=None, process_factory=None):
        self.cwd, self.binding = str(cwd), dict(binding)
        self._emit, self._external_fence = emit, can_send
        self._binary, self._process_factory = binary, process_factory
        self._process = None
        self._state = "starting"
        self._root = None
        self._version = None
        self._policy = {}
        self._pending = {}
        self._next_id = 0
        self._active = None
        self._permissions = {}
        self._seen_requests = set()
        self._seen_turns = set()
        self._seen_submissions = set()
        self._early = []
        self._early_bytes = 0
        self._fatal = False
        self._emit_failed = False
        self._closed = False
        self._exiting = False
        self._close_task = None

    def snapshot(self):
        return {"state": self._state, "provider": "codex", "provider_version": self._version,
                "provider_root_id": self._root, "policy": copy.deepcopy(self._policy),
                "verification": "static_verified/runtime_unverified"}

    async def _publish(self, event):
        if self._emit_failed:
            raise ManagedError("event_persistence_failed")
        try:
            await self._emit(event)
        except Exception:
            # A rejected persistence callback must revoke all physical writes,
            # including a write already queued behind another writer.
            self._fatal = True
            self._emit_failed = True
            self._state = "error"
            self._schedule_close()
            raise

    def _schedule_close(self):
        if self._process is not None and self._close_task is None:
            self._close_task = asyncio.create_task(self.close("protocol_failure"))

    async def _error(self, reason, *, fatal=False):
        if fatal:
            self._fatal = True
            self._state = "error"
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ManagedError(reason))
            self._schedule_close()
        event = {"kind": "error", "reason": reason, "fatal": fatal}
        if self._active:
            event["submission_id"] = self._active["submission_id"]
        await self._publish(event)

    def _context(self, operation, **extra):
        return {"operation": operation, "process_generation": self.binding["process_generation"],
                **extra}

    def _fence(self, context):
        operation = context.get("operation")
        if context.get("process_generation") != self.binding["process_generation"]:
            return False
        if self._fatal and operation != "cleanup":
            return False
        if self._state in {"closing", "ended"} and operation != "cleanup":
            return False
        if operation in {"submit", "interrupt", "permission"}:
            active = self._active
            if not active or active["settling"] or context.get("submission_id") != active["submission_id"]:
                return False
            if context.get("cancel_version") != active["cancel_version"]:
                return False
            if operation == "submit" and active["cancelled"]:
                return False
            if operation == "permission":
                pending = self._permissions.get(context.get("permission_request_id"))
                if not pending or pending["expires"] <= time.monotonic():
                    return False
                if context.get("decision") == "allow_once" and active["cancelled"]:
                    return False
        return bool(self._external_fence(context))

    async def _rpc(self, method, params, context):
        if len(self._pending) >= 32:
            raise ManagedError("protocol_overflow")
        self._next_id += 1
        identity = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[_rpc_key(identity)] = future
        try:
            sent = await self._process.send({"id": identity, "method": method, "params": params}, context)
            if not sent:
                raise ManagedError("send_fenced")
            # Startup/control ACKs must not hold a session indefinitely. A lost
            # turn/start ACK is ambiguous and is never automatically replayed.
            return await asyncio.wait_for(asyncio.shield(future), timeout=30)
        finally:
            self._pending.pop(_rpc_key(identity), None)
            if not future.done():
                future.cancel()

    async def start(self):
        if self._process is not None or self._closed:
            raise ManagedError("already_started")
        binary = resolve_native_binary("codex", self._binary)
        self._version = await verify_version(binary, VERSION, process_factory=self._process_factory)
        self._process = JsonLineProcess(
            [binary, "--no-daemon", "app-server", "--listen", "stdio://"], self.cwd,
            on_frame=self._on_frame, on_exit=self._on_exit, can_send=self._fence,
            process_factory=self._process_factory, env=inherited_environment())
        try:
            await self._process.start()
            initialized = await self._rpc("initialize", {
                "clientInfo": {"name": "supermono-managed-conversation", "version": "1"},
                "capabilities": {"experimentalApi": True}}, self._context("initialize"))
            if not initialized.get("userAgent", "").startswith("supermono-managed-conversation/0.167.0 ("):
                raise ManagedError("unsupported_version")
            if not await self._process.send({"method": "initialized"}, self._context("initialize")):
                raise ManagedError("send_fenced")
            started = await self._rpc("thread/start", {"cwd": self.cwd, "ephemeral": False},
                                      self._context("initialize"))
            root = started.get("thread", {}).get("id")
            if not _identity(root) or not all(key in started for key in
                    ("approvalPolicy", "approvalsReviewer", "sandbox", "model", "modelProvider")):
                raise ManagedError("invalid_thread_binding")
            self._root = root
            self._policy = {key: copy.deepcopy(started[key]) for key in
                            ("approvalPolicy", "approvalsReviewer", "sandbox", "model", "modelProvider")}
            if len(json.dumps(self._policy).encode()) > 65536:
                raise ManagedError("protocol_overflow")
            self._state = "ready"
            await self._publish({"kind": "ready", "provider": "codex", "provider_version": self._version,
                                 "provider_root_id": self._root, "policy": copy.deepcopy(self._policy)})
            return self.snapshot()
        except BaseException:
            self._fatal = True
            self._state = "error"
            # Cancellation after spawn still owns the child and its readers.
            # Keep cleanup alive through repeated caller cancellation, and do
            # not return/rethrow until actual process termination is joined.
            cleanup = asyncio.create_task(self.close("startup_failed"))
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()
            raise

    async def submit(self, submission):
        if self._state != "ready" or self._active is not None or self._fatal:
            raise ManagedError("busy_or_unavailable")
        value = dict(submission)
        if (not _identity(value.get("submission_id")) or not _identity(value.get("user_message_uuid"))
                or not isinstance(value.get("text"), str) or not value["text"].strip()
                or len(value["text"].encode("utf-8")) > 65536
                or type(value.get("cancel_version")) is not int or value["cancel_version"] < 0):
            raise ManagedError("invalid_submission")
        if value["submission_id"] in self._seen_submissions:
            raise ManagedError("stale_submission")
        if len(self._seen_submissions) >= _IDENTITIES_LIMIT:
            raise ManagedError("identity_limit")
        self._seen_submissions.add(value["submission_id"])
        self._active = {**value, "turn_id": None, "cancelled": False, "interrupt_sent": False,
                        "items": {}, "finals": [], "final_bytes": 0, "client_seen": False,
                        "invalid_client": False, "settling": False}
        self._state = "running"
        try:
            result = await self._rpc("turn/start", {"threadId": self._root,
                "clientUserMessageId": value["user_message_uuid"],
                "input": [{"type": "text", "text": value["text"], "text_elements": []}]},
                self._context("submit", submission_id=value["submission_id"], cancel_version=value["cancel_version"]))
            turn = result.get("turn", {})
            turn_id = turn.get("id")
            if not _identity(turn_id) or turn_id in self._seen_turns:
                raise ManagedError("invalid_turn_binding")
            self._seen_turns.add(turn_id)
            active = self._active
            if active is None:
                raise ManagedError("invalid_turn_binding")
            active["turn_id"] = turn_id
            await self._publish({"kind": "turn_started", "submission_id": value["submission_id"],
                "user_message_uuid": value["user_message_uuid"], "provider_root_id": self._root,
                "provider_turn_id": turn_id})
            for entry in turn.get("items", []):
                await self._item(entry)
            early, self._early = self._early, []
            self._early_bytes = 0
            for frame in early:
                await self._dispatch(frame)
            if self._active and active["cancelled"]:
                await self._send_interrupt(active)
            return {"state": "dispatched", "submission_id": value["submission_id"], "provider_turn_id": turn_id}
        except ManagedError as exc:
            if self._exiting or self._closed:
                raise
            if str(exc) == "send_fenced":
                await self._settle("interrupted", "not_dispatched")
                return {"state": "not_dispatched", "submission_id": value["submission_id"]}
            await self._settle("ambiguous", "dispatch_failed")
            await self._error("dispatch_failed", fatal=True)
            raise
        except Exception:
            if not self._fatal and not self._exiting and not self._closed:
                await self._settle("ambiguous", "dispatch_failed")
                await self._error("dispatch_failed", fatal=True)
            raise

    async def _on_frame(self, frame):
        if self._fatal or self._closed:
            return
        try:
            if not isinstance(frame, dict) or len(json.dumps(frame).encode()) > _FRAME_LIMIT:
                raise ManagedError("invalid_protocol")
            if "id" in frame and "method" not in frame:
                key = _rpc_key(frame["id"])
                future = self._pending.get(key)
                if future is None or future.done():
                    raise ManagedError("unknown_response_id")
                if "error" in frame:
                    future.set_exception(ManagedError("provider_rpc_failed"))
                elif isinstance(frame.get("result"), dict):
                    future.set_result(frame["result"])
                else:
                    raise ManagedError("invalid_protocol")
                return
            if not isinstance(frame.get("method"), str) or not isinstance(frame.get("params", {}), dict):
                raise ManagedError("invalid_protocol")
            active = self._active
            params = frame.get("params", {})
            # Never adopt a notification's thread/turn identity as an ACK.
            # Retain bounded frames until the exact turn/start response arrives.
            if active and active["turn_id"] is None and params.get("threadId") == self._root:
                size = len(json.dumps(frame).encode())
                if len(self._early) >= 32 or self._early_bytes + size > _PENDING_LIMIT:
                    raise ManagedError("protocol_overflow")
                self._early.append(copy.deepcopy(frame))
                self._early_bytes += size
                return
            await self._dispatch(frame)
        except ManagedError as exc:
            await self._error(str(exc), fatal=True)
        except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
            if self._emit_failed:
                raise
            await self._error("invalid_protocol", fatal=True)

    async def _dispatch(self, frame):
        method, params = frame["method"], frame.get("params", {})
        if "id" in frame:
            await self._request(frame)
            return
        if params.get("threadId") != self._root:
            return
        if method == "serverRequest/resolved":
            request_id = params.get("requestId")
            for public_id, pending in list(self._permissions.items()):
                if _rpc_key(pending["wire_id"]) == _rpc_key(request_id):
                    await self._withdraw(public_id)
            return
        active = self._active
        turn = params.get("turn", {})
        turn_id = turn.get("id") if method == "turn/completed" else params.get("turnId")
        if not active or turn_id != active["turn_id"]:
            return
        if method == "item/completed":
            await self._item(params.get("item"))
        elif method == "turn/completed":
            for entry in turn.get("items", []):
                await self._item(entry)
            status = turn.get("status")
            if status == "inProgress":
                await self._error("invalid_terminal_status", fatal=True)
                return
            # An interrupt request/ACK cannot override a completed turn that
            # won the race. Provider terminal evidence remains authoritative.
            if status == "completed" and turn.get("error") is None:
                if active["client_seen"] and not active["invalid_client"] and active["finals"]:
                    await self._settle("completed", finals=active["finals"])
                else:
                    await self._settle("ambiguous", "missing_completion_proof")
            elif status in {"interrupted", "failed"}:
                await self._settle(status, "provider_terminal")
            else:
                await self._settle("ambiguous", "unknown_terminal_status")
        elif method == "error":
            await self._error("provider_error", fatal=False)

    async def _item(self, item):
        active = self._active
        if active is None or not isinstance(item, dict) or not _identity(item.get("id")):
            raise ManagedError("invalid_item")
        identity = item["id"]
        signature = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
        if identity in active["items"]:
            if active["items"][identity] != signature:
                raise ManagedError("conflicting_item")
            return
        if len(active["items"]) >= 4096:
            raise ManagedError("protocol_overflow")
        # Store only bounded signatures, never tool argument bodies or reasoning.
        active["items"][identity] = signature
        if item.get("type") == "userMessage":
            if item.get("clientId") == active["user_message_uuid"]:
                active["client_seen"] = True
            else:
                active["invalid_client"] = True
        elif item.get("type") == "agentMessage":
            text = item.get("text")
            if item.get("phase") == "final_answer" and isinstance(text, str) and text.strip():
                active["final_bytes"] += len(text.encode())
                if active["final_bytes"] > _FRAME_LIMIT or len(active["finals"]) >= 32:
                    raise ManagedError("protocol_overflow")
                active["finals"].append({"provider_record_id": identity, "text": text, "proof": {
                    "kind": "provider_final_phase", "provider": "codex", "provider_version": self._version,
                    "provider_root_id": self._root, "provider_turn_id": active["turn_id"],
                    "submission_id": active["submission_id"], "user_message_uuid": active["user_message_uuid"],
                    "client_user_message_id": active["user_message_uuid"], "provider_record_id": identity,
                    "phase": "final_answer", "terminal_status": "completed"}})
            elif item.get("phase") == "commentary" and isinstance(text, str):
                await self._publish({"kind": "progress", "submission_id": active["submission_id"],
                    "provider_turn_id": active["turn_id"], "source_id": identity,
                    "category": "candidate", "text": text[:4096]})
        elif item.get("type") in {"commandExecution", "fileChange", "mcpToolCall"}:
            await self._publish({"kind": "progress", "submission_id": active["submission_id"],
                "provider_turn_id": active["turn_id"], "source_id": identity,
                "category": "tool", "text": item["type"]})

    async def _request(self, frame):
        key = _rpc_key(frame["id"])
        if key in self._seen_requests:
            raise ManagedError("duplicate_request_id")
        if len(self._seen_requests) >= _IDENTITIES_LIMIT:
            raise ManagedError("identity_limit")
        self._seen_requests.add(key)
        params, active = frame.get("params", {}), self._active
        valid = (active and params.get("threadId") == self._root
                 and params.get("turnId") == active["turn_id"] and _identity(params.get("itemId")))
        supported = frame["method"] in _APPROVALS and params.get("kind", "command") == "command"
        if not valid or not supported or active["cancelled"]:
            await self._process.send({"id": frame["id"], "error": {
                "code": -32601, "message": "Unsupported managed request"}}, self._context("cleanup"))
            await self._error("unsupported_server_request" if valid else "foreign_server_request")
            return
        if len(self._permissions) >= 32 or len(json.dumps(params).encode()) > 65536:
            raise ManagedError("protocol_overflow")
        public_id = str(uuid.uuid4())
        pending = {"wire_id": frame["id"], "submission_id": active["submission_id"],
                   "turn_id": active["turn_id"], "cancel_version": active["cancel_version"],
                   "expires": time.monotonic() + 300}
        self._permissions[public_id] = pending
        self._state = "waiting_permission"
        await self._publish({"kind": "permission", "permission_request_id": public_id,
            "submission_id": active["submission_id"], "provider_turn_id": active["turn_id"],
            "tool_name": _APPROVALS[frame["method"]], "details": copy.deepcopy(params),
            "choices": ["allow_once", "deny"], "requires_user_interaction": False})

    async def _withdraw(self, public_id):
        if self._permissions.pop(public_id, None) is not None:
            await self._publish({"kind": "permission_withdrawn", "permission_request_id": public_id})

    async def respond_permission(self, permission_request_id, decision, *, submission_id, cancel_version):
        pending = self._permissions.get(permission_request_id)
        active = self._active
        if (decision not in {"allow_once", "deny"} or not pending or not active
                or submission_id != pending["submission_id"] or cancel_version != pending["cancel_version"]
                or pending.get("responding")):
            raise ManagedError("stale_permission")
        pending["responding"] = True
        context = self._context("permission", submission_id=submission_id, cancel_version=cancel_version,
                                permission_request_id=permission_request_id, decision=decision)
        sent = await self._process.send({"id": pending["wire_id"], "result": {
            "decision": "accept" if decision == "allow_once" else "decline"}}, context)
        if not sent:
            await self._withdraw(permission_request_id)
            raise ManagedError("stale_permission")
        await self._withdraw(permission_request_id)
        if self._active and not self._permissions:
            self._state = "interrupting" if active["cancelled"] else "running"
        return {"permission_request_id": permission_request_id, "state": "answered", "decision": decision}

    async def interrupt(self, submission_id, cancel_version):
        active = self._active
        if (not active or active["settling"] or active["submission_id"] != submission_id or type(cancel_version) is not int
                or cancel_version < active["cancel_version"]):
            raise ManagedError("stale_submission")
        active["cancelled"], active["cancel_version"] = True, cancel_version
        self._state = "interrupting"
        for public_id in list(self._permissions):
            await self._withdraw(public_id)
        if active["turn_id"] is not None:
            await self._send_interrupt(active)
        return {"submission_id": submission_id, "state": "interrupt_requested", "cancel_version": cancel_version}

    async def _send_interrupt(self, active):
        if active["interrupt_sent"] or self._active is not active:
            return
        active["interrupt_sent"] = True
        await self._rpc("turn/interrupt", {"threadId": self._root, "turnId": active["turn_id"]},
                        self._context("interrupt", submission_id=active["submission_id"],
                                      cancel_version=active["cancel_version"]))

    async def _settle(self, outcome, reason=None, finals=None):
        active = self._active
        if active is None or active["settling"]:
            return
        active["settling"] = True
        # The one callback is the transaction boundary: no independent final
        # callback or active clear can race canonical outcome persistence.
        event = {"kind": "settled", "submission_id": active["submission_id"],
                 "user_message_uuid": active["user_message_uuid"], "provider_root_id": self._root,
                 "provider_turn_id": active["turn_id"], "outcome": outcome, "finals": finals or []}
        if reason:
            event["reason"] = reason
        for public_id in list(self._permissions):
            await self._withdraw(public_id)
        await self._publish(event)
        self._active = None
        self._early.clear()
        self._early_bytes = 0
        if self._state not in {"closing", "ended", "error"}:
            self._state = "ready"

    async def _on_exit(self, exit_code, reason):
        if self._closed:
            return
        self._exiting = True
        try:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ManagedError("process_closed"))
            if self._emit_failed:
                self._state = "error"
                return
            if self._active:
                await self._settle("interrupted" if self._active["cancelled"] else "ambiguous", "process_closed")
            self._closed = True
            self._state = "ended"
            await self._publish({"kind": "closed", "reason": "closed" if reason == "closed" else "process_closed",
                                 "exit_code": exit_code})
        finally:
            self._closed = True

    async def close(self, reason="closed"):
        if self._closed:
            return
        self._state = "closing"
        if self._process is None:
            await self._on_exit(None, reason)
            return
        await self._process.close()
