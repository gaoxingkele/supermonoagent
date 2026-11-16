"""Persistent Claude 2.1.310 coding transport for explicitly managed sessions.

Only a correlated, nonzero root terminal result is a final. All approval inputs
remain transient; the emit boundary contains no raw control envelopes.
"""
from __future__ import annotations

import asyncio
from collections import deque
import json
import uuid

from .managed_process import (
    JsonLineProcess, ManagedProcessError, inherited_environment,
    resolve_native_binary, sanitize_render_text, verify_version,
)


VERSION = "2.1.310"
PERMISSION_SECONDS = 300
_PERMISSION_MODES = {"default", "acceptEdits", "bypassPermissions", "plan", "dontAsk", "auto"}


def _uuid(value):
    try: return isinstance(value, str) and str(uuid.UUID(value)) == value.lower()
    except (ValueError, AttributeError): return False


class ClaudeManagedDriver:
    def __init__(self, cwd, binding, emit, can_send, *, binary=None, process_factory=None):
        self.cwd, self.binding = str(cwd), dict(binding)
        self._emit_callback, self._external_can_send = emit, can_send
        self.binary, self.process_factory = binary, process_factory
        self._process = None
        self._state = "starting"
        self._requested_root = str(uuid.uuid4())
        self._root = None
        self._epoch = self._requested_root
        self._version = None
        self._policy = {"permission_mode": "unknown", "source": "inherited_cli_settings", "host_prompts": True}
        self._capabilities = set()
        self._active = None
        self._permissions = {}
        self._permission_timers = {}
        self._finished_permissions = deque(maxlen=128)
        self._controls = {}
        self._control_counter = 0
        self._initialized = asyncio.Event()
        self._initialize_error = None
        self._last_result_index = -1
        self._seen_results = deque(maxlen=128)
        self._failed = False
        self._closing = False
        self._close_reason = "closed"
        self._close_task = None
        self._fatal_task = None

    def snapshot(self):
        return {"state": self._state, "provider": "claude", "provider_version": self._version,
                "provider_root_id": self._root, "requested_root_id": self._requested_root,
                "provider_root_confirmed": self._root is not None, "policy": dict(self._policy)}

    async def _emit(self, event):
        if self._failed and event["kind"] not in {"closed", "error", "settled", "permission_withdrawn"}:
            raise ManagedProcessError("managed_event_persistence_failed")
        try:
            await self._emit_callback(event)
        except Exception:
            self._failed = True
            self._state = "error"
            if self._process is not None and self._fatal_task is None:
                self._fatal_task = asyncio.create_task(self._process.close())
            raise ManagedProcessError("managed_event_persistence_failed") from None

    async def start(self):
        if self._process is not None: raise ManagedProcessError("managed_already_started")
        binary = resolve_native_binary("claude", self.binary)
        self._version = await verify_version(binary, VERSION, process_factory=self.process_factory)
        argv = [binary, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--permission-prompt-tool=stdio", "--permission-prompts", "host", "--replay-user-messages",
                "--session-id", self._requested_root]
        self._process = JsonLineProcess(argv, self.cwd, on_frame=self._on_frame, on_exit=self._on_exit,
            can_send=self._can_send, process_factory=self.process_factory, env=inherited_environment())
        try:
            await self._process.start()
            if not await self._control({"subtype": "initialize"}, {"operation": "initialize"}):
                raise ManagedProcessError("managed_dispatch_rejected")
            await asyncio.wait_for(self._initialized.wait(), 30)
            if self._initialize_error:
                raise ManagedProcessError(self._initialize_error)
            if self._policy["permission_mode"] == "unknown":
                raise ManagedProcessError("managed_policy_unsupported")
            self._state = "ready"
            await self._emit_ready()
            return self.snapshot()
        except BaseException:
            self._closing = True
            await self._process.close()
            raise

    async def _emit_ready(self):
        await self._emit({"kind": "ready", "provider": "claude", "provider_version": self._version,
            "provider_root_id": self._root, "requested_root_id": self._requested_root,
            "provider_root_confirmed": self._root is not None, "policy": dict(self._policy)})

    def _can_send(self, context):
        if self._failed or self._closing: return False
        operation = context.get("operation")
        active = self._active
        if operation in {"submit", "interrupt", "permission"}:
            if not active or active["submission_id"] != context.get("submission_id"): return False
            if active["cancel_version"] != context.get("cancel_version"): return False
            if operation == "submit":
                if active["interrupted"]: return False
            elif operation == "permission":
                pending = self._permissions.get(context.get("permission_request_id"))
                if (active["interrupted"] or not pending or pending["state"] != "responding"
                        or pending["decision"] != context.get("decision")): return False
                if context.get("decision") == "allow_once" and self._root != self._requested_root: return False
        if self._external_can_send(context) is not True: return False
        # This marker is set at the physical writer fence, never while enqueuing.
        if operation == "submit": active["dispatch_started"] = True
        return True

    async def _control(self, request, context):
        if len(self._controls) >= 32:
            raise ManagedProcessError("managed_control_limit")
        self._control_counter += 1
        request_id = "host-" + str(self._control_counter)
        self._controls[request_id] = {"subtype": request["subtype"], **context}
        sent = await self._process.send({"type": "control_request", "request_id": request_id, "request": request}, context)
        if not sent: self._controls.pop(request_id, None)
        return sent

    async def submit(self, submission):
        if self._state != "ready" or self._active is not None or self._closing or self._failed:
            raise ManagedProcessError("managed_not_ready")
        message = dict(submission)
        if (not isinstance(message.get("submission_id"), str) or not _uuid(message.get("user_message_uuid"))
                or not isinstance(message.get("text"), str) or not message["text"].strip()
                or len(message["text"].encode("utf-8")) > 65536
                or type(message.get("cancel_version")) is not int or message["cancel_version"] < 0):
            raise ManagedProcessError("managed_invalid_submission")
        message.update(dispatch_started=False, started=False, interrupted=False)
        self._active = message
        self._state = "running"
        context = {"operation": "submit", "submission_id": message["submission_id"], "cancel_version": message["cancel_version"]}
        try:
            sent = await self._process.send({"type": "user", "uuid": message["user_message_uuid"],
                "session_id": self._requested_root, "message": {"role": "user", "content": message["text"]},
                "parent_tool_use_id": None, "origin": {"kind": "human"}, "priority": "later"}, context)
        except ManagedProcessError:
            await self._fatal("managed_submission_delivery_unknown")
            raise
        if not sent and self._active is message:
            await self._settle("interrupted", "not_dispatched")
        return {"dispatch_state": "dispatched" if sent else "not_dispatched",
                "submission_id": message["submission_id"], "provider_root_id": self._root,
                "provider_turn_id": message["user_message_uuid"] if message["started"] else None}

    async def _turn_started(self):
        active = self._active
        if active and self._root and not active["started"]:
            active["started"] = True
            await self._emit({"kind": "turn_started", "submission_id": active["submission_id"],
                "user_message_uuid": active["user_message_uuid"], "provider_root_id": self._root,
                "provider_turn_id": active["user_message_uuid"]})

    async def interrupt(self, submission_id, cancel_version):
        active = self._active
        if not active or active["submission_id"] != submission_id:
            raise ManagedProcessError("managed_stale_submission")
        if type(cancel_version) is not int or cancel_version < active["cancel_version"]:
            raise ManagedProcessError("managed_stale_cancel")
        active["cancel_version"] = cancel_version
        active["interrupted"] = True
        self._state = "interrupting"
        await self._withdraw_all()
        if not active["dispatch_started"]:
            # The pending writer observes the latch and settles not_dispatched.
            return {"state": "interrupt_requested"}
        request = {"subtype": "interrupt"}
        if "interrupt_cancel_queued_v1" in self._capabilities:
            request["cancel_queued"] = True
        await self._control(request, {"operation": "interrupt", "submission_id": submission_id, "cancel_version": cancel_version})
        if "interrupt_cancel_queued_v1" not in self._capabilities and self._active is active:
            await self._control({"subtype": "cancel_async_message", "message_uuid": active["user_message_uuid"]},
                {"operation": "interrupt", "submission_id": submission_id, "cancel_version": cancel_version})
        return {"state": "interrupt_requested"}

    async def _on_frame(self, frame):
        if self._failed: return
        kind = frame.get("type")
        if kind == "control_response":
            response = frame.get("response", {})
            if not isinstance(response, dict): return
            control = self._controls.pop(response.get("request_id"), None)
            if not control: return
            if control["subtype"] == "initialize":
                if response.get("subtype") != "success": self._initialize_error = "managed_initialize_rejected"
                payload = response.get("response", {})
                if not isinstance(payload, dict):
                    self._initialize_error = "managed_invalid_control"
                else:
                    # Initialize advertises policy before the first per-turn
                    # system/init; identity remains unconfirmed until init.
                    if "claude_code_version" in payload and payload["claude_code_version"] != VERSION:
                        self._initialize_error = "managed_version_unsupported"
                    mode = payload.get("current_permission_mode")
                    if mode is not None:
                        if mode not in _PERMISSION_MODES: self._initialize_error = "managed_policy_unsupported"
                        else: self._policy["permission_mode"] = mode
                    if "capabilities" in payload:
                        capabilities = payload["capabilities"]
                        if isinstance(capabilities, list) and len(capabilities) <= 256:
                            self._capabilities = {value for value in capabilities if isinstance(value, str)}
                        else: self._initialize_error = "managed_invalid_capabilities"
                self._initialized.set()
                for field in ("pending_permission_requests", "pending_user_dialog_requests"):
                    pending = response.get(field, [])
                    if not isinstance(pending, list) or len(pending) > 32:
                        await self._fatal("managed_invalid_control")
                        return
                    for request in pending:
                        if isinstance(request, dict): await self._permission(request)
                return
            active = self._active
            if not active or active["submission_id"] != control.get("submission_id"): return
            payload = response.get("response", {})
            if not isinstance(payload, dict) or response.get("subtype") != "success": return
            cancelled = payload.get("cancelled")
            if (control["subtype"] == "cancel_async_message" and cancelled is True
                    or control["subtype"] == "interrupt" and isinstance(cancelled, list)
                    and active["user_message_uuid"] in cancelled):
                await self._settle("interrupted", "provider_cancelled")
            elif control["subtype"] == "interrupt" and isinstance(payload.get("still_queued"), list):
                if active["user_message_uuid"] in payload["still_queued"]:
                    await self._control({"subtype": "cancel_async_message", "message_uuid": active["user_message_uuid"]},
                        {"operation": "interrupt", "submission_id": active["submission_id"], "cancel_version": active["cancel_version"]})
            return
        if kind == "control_cancel_request":
            await self._withdraw(frame.get("request_id"))
            return
        if kind == "control_request":
            await self._permission(frame)
            return
        if frame.get("parent_tool_use_id") is not None or frame.get("agent_id") is not None: return
        if kind == "system" and frame.get("subtype") == "init":
            if (frame.get("session_id") != self._requested_root or frame.get("claude_code_version") != VERSION
                    or frame.get("permissionMode") not in _PERMISSION_MODES):
                await self._fatal("managed_root_or_policy_mismatch")
                return
            self._root = self._requested_root
            self._policy["permission_mode"] = frame["permissionMode"]
            capabilities = frame.get("capabilities", [])
            if not isinstance(capabilities, list) or len(capabilities) > 256:
                await self._fatal("managed_invalid_capabilities")
                return
            self._capabilities = {value for value in capabilities if isinstance(value, str)}
            await self._emit_ready()
            return
        if kind == "conversation_reset":
            if frame.get("session_id") == self._root and _uuid(frame.get("new_conversation_id")):
                if self._active: await self._settle("ambiguous", "managed_conversation_reset")
                self._epoch = frame["new_conversation_id"]
                # result_index counts physical writes across the whole run;
                # conversation resets do not restart its delivery sequence.
            return
        if kind == "user":
            active = self._active
            if (active and frame.get("session_id") == self._root and frame.get("uuid") == active["user_message_uuid"]
                    and frame.get("origin") == {"kind": "human"}):
                await self._turn_started()
            return
        if kind == "result": await self._result(frame)
        # Assistant, thinking, hooks, tools and deltas never become final text.

    async def _result(self, frame):
        if self._root is None:
            active = self._active
            if not active or frame.get("session_id") != self._requested_root: return
            consumed = frame.get("user_message_uuids", [frame.get("user_message_uuid")])
            correlated = consumed == [active["user_message_uuid"]] and frame.get("origin") == {"kind": "human"}
            startup_failure = (frame.get("num_turns") == 0 and frame.get("is_error") is True
                               and frame.get("origin") is None and consumed == [None])
            if not correlated and not startup_failure: return
            # Startup/hook rejection can be diagnostic before per-turn init.
            # It closes an attempt without claiming the requested UUID as root.
            if frame.get("is_error") is True or frame.get("num_turns") == 0:
                identity, index = frame.get("uuid"), frame.get("result_index")
                if _uuid(identity) and type(index) is int and index == self._last_result_index + 1:
                    self._seen_results.append(identity)
                    self._last_result_index = index
                await self._settle("failed" if frame.get("is_error") is True else "blocked", "provider_before_root_confirmation")
            return
        if frame.get("session_id") != self._root: return
        identity, index = frame.get("uuid"), frame.get("result_index")
        if identity in self._seen_results: return
        if not _uuid(identity) or type(index) is not int or index != self._last_result_index + 1:
            await self._fatal("managed_result_sequence_unknown")
            return
        self._seen_results.append(identity)
        self._last_result_index = index
        active = self._active
        if not active or not active["dispatch_started"]: return
        consumed = frame.get("user_message_uuids", [frame.get("user_message_uuid")])
        if (not isinstance(consumed, list) or len(consumed) > 64 or consumed != [active["user_message_uuid"]]
                or frame.get("origin") != {"kind": "human"}): return
        await self._turn_started()
        completed = (frame.get("subtype") == "success" and frame.get("is_error") is False
            and type(frame.get("num_turns")) is int and frame["num_turns"] > 0
            and frame.get("terminal_reason") == "completed" and frame.get("interrupted_by_shutdown") is not True
            and isinstance(frame.get("result"), str) and bool(frame["result"].strip()))
        if completed:
            proof = {"kind": "provider_terminal_result", "provider": "claude", "provider_version": VERSION,
                "provider_root_id": self._root, "submission_id": active["submission_id"],
                "user_message_uuid": active["user_message_uuid"], "provider_turn_id": active["user_message_uuid"],
                "result_uuid": identity, "result_index": index, "conversation_epoch": self._epoch,
                "terminal_status": "completed", "terminal_reason": "completed", "num_turns": frame["num_turns"], "origin": "human"}
            await self._settle("completed", None, [{"provider_record_id": identity, "text": frame["result"], "proof": proof}])
        elif frame.get("terminal_reason") in {"aborted_streaming", "aborted_tools"} or frame.get("interrupted_by_shutdown") is True:
            await self._settle("interrupted", "provider_interrupted")
        elif frame.get("num_turns") == 0:
            await self._settle("blocked", "provider_zero_turn")
        elif frame.get("is_error") is True or str(frame.get("subtype", "")).startswith("error"):
            await self._settle("failed", "provider_turn_failed")
        else:
            await self._settle("ambiguous", "managed_final_proof_missing")

    async def _permission(self, frame):
        request_id, request = frame.get("request_id"), frame.get("request")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 256 or not isinstance(request, dict):
            await self._fatal("managed_invalid_control")
            return
        if request_id in self._permissions or request_id in self._finished_permissions: return
        active = self._active
        if not active or active["interrupted"] or not active["dispatch_started"]:
            await self._process.send({"type": "control_response", "response": {"subtype": "error",
                "request_id": request_id, "error": "No active application submission."}}, {"operation": "cleanup"})
            return
        if len(self._permissions) >= 32:
            await self._fatal("managed_permission_limit")
            return
        supported = (request.get("subtype") == "can_use_tool" and isinstance(request.get("input"), dict)
                     and isinstance(request.get("tool_name"), str) and bool(request["tool_name"]))
        interactive = (request.get("requires_user_interaction", False) is not False or not supported
                       or request.get("tool_name") == "AskUserQuestion")
        choices = ["deny"] if interactive else ["allow_once", "deny"]
        detail = {key: request[key] for key in ("input", "description", "agent_id", "tool_use_id", "decision_reason",
            "decision_reason_type", "decision_reason_code", "decision_reason_rule_source", "decision_reason_hook_event",
            "decision_reason_hook_source", "decision_reason_hook_plugin_id", "default_to_no", "suppress_always_allow_rule",
            "mcp_server", "mcp_server_name", "mcp_tool_name", "blocked_path") if key in request}
        details = sanitize_render_text(json.dumps(detail, ensure_ascii=False)) if supported else "Unsupported interactive request."
        if len(details.encode("utf-8")) > 65536:
            await self._fatal("managed_permission_detail_limit")
            return
        descriptor = {"kind": "permission", "permission_request_id": request_id, "submission_id": active["submission_id"],
            "provider_turn_id": active["user_message_uuid"] if active["started"] else None,
            "tool_name": sanitize_render_text(str(request.get("tool_name", request.get("subtype", "unsupported"))))[:256],
            "details": details, "choices": choices, "requires_user_interaction": interactive}
        self._permissions[request_id] = {"request": request, "submission_id": active["submission_id"],
            "state": "pending", "choices": choices, "descriptor": descriptor}
        self._state = "waiting_permission"
        await self._emit(descriptor)
        self._permission_timers[request_id] = asyncio.create_task(self._expire_permission(request_id))

    async def _expire_permission(self, request_id):
        await asyncio.sleep(PERMISSION_SECONDS)
        pending, active = self._permissions.get(request_id), self._active
        if pending and active:
            await self.respond_permission(request_id, "deny", submission_id=active["submission_id"], cancel_version=active["cancel_version"])

    async def respond_permission(self, permission_request_id, decision, *, submission_id, cancel_version):
        pending, active = self._permissions.get(permission_request_id), self._active
        if (not pending or not active or pending["state"] != "pending" or pending["submission_id"] != submission_id
                or active["submission_id"] != submission_id or active["cancel_version"] != cancel_version
                or active["interrupted"] or decision not in pending["choices"]):
            raise ManagedProcessError("managed_stale_permission")
        if decision == "allow_once" and self._root != self._requested_root:
            raise ManagedProcessError("managed_root_unconfirmed")
        pending.update(state="responding", decision=decision)
        request = pending["request"]
        if request.get("subtype") != "can_use_tool":
            response = {"subtype": "error", "request_id": permission_request_id, "error": "Unsupported interactive request."}
        else:
            payload = {"behavior": "allow", "updatedInput": request["input"]} if decision == "allow_once" else {
                "behavior": "deny", "message": "Declined by the user."}
            if "tool_use_id" in request: payload["toolUseID"] = request["tool_use_id"]
            response = {"subtype": "success", "request_id": permission_request_id, "response": payload}
        sent = await self._process.send({"type": "control_response", "response": response}, {
            "operation": "permission", "submission_id": submission_id, "cancel_version": cancel_version,
            "permission_request_id": permission_request_id, "decision": decision})
        await self._withdraw(permission_request_id)
        return {"state": "answered" if sent else "stale", "decision": decision, "permission_request_id": permission_request_id}

    async def _withdraw(self, request_id):
        pending = self._permissions.pop(request_id, None)
        timer = self._permission_timers.pop(request_id, None)
        if timer is not None and timer is not asyncio.current_task(): timer.cancel()
        if pending:
            self._finished_permissions.append(request_id)
            await self._emit({"kind": "permission_withdrawn", "permission_request_id": request_id})
            if self._state == "waiting_permission" and not self._permissions: self._state = "running"

    async def _withdraw_all(self):
        for request_id in list(self._permissions): await self._withdraw(request_id)

    async def _settle(self, outcome, reason=None, finals=None):
        active = self._active
        if active is None: return
        await self._withdraw_all()
        event = {"kind": "settled", "submission_id": active["submission_id"], "user_message_uuid": active["user_message_uuid"],
            "provider_root_id": self._root, "outcome": outcome, "finals": finals or []}
        if active["started"]: event["provider_turn_id"] = active["user_message_uuid"]
        if reason: event["reason"] = reason
        await self._emit(event)
        # Persistence succeeds before another turn may start.
        if self._active is active: self._active = None
        if not self._failed and not self._closing: self._state = "ready"

    async def _fatal(self, reason):
        self._failed = True
        self._state = "error"
        self._initialize_error = reason
        self._initialized.set()
        await self._emit({"kind": "error", "reason": reason, "fatal": True,
            **({"submission_id": self._active["submission_id"]} if self._active else {})})
        if self._active: await self._settle("ambiguous", reason)
        if self._process and self._fatal_task is None: self._fatal_task = asyncio.create_task(self._process.close())

    async def _on_exit(self, exit_code, reason):
        self._initialize_error = self._initialize_error or "managed_process_closed"
        self._initialized.set()
        if self._active:
            if not self._active["dispatch_started"]:
                await self._settle("interrupted", "not_dispatched")
            else:
                await self._settle("interrupted" if self._active["interrupted"] else "ambiguous", reason or "managed_process_closed")
        await self._withdraw_all()
        self._state = "error" if reason or self._failed else "ended"
        await self._emit({"kind": "closed", "reason": reason or self._close_reason, "exit_code": exit_code})

    async def close(self, reason="closed"):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(reason))
        try: return await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            await asyncio.shield(self._close_task)
            raise

    async def _close(self, reason):
        self._close_reason = reason
        self._closing = True
        if not self._failed: self._state = "closing"
        await self._withdraw_all()
        if self._process: return await self._process.close()
        self._state = "ended"
