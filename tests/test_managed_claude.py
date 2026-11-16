"""Synthetic installed-schema projections are classifiers, not runtime evidence."""
import asyncio
import subprocess
import sys
import uuid

import pytest

from supermonoagent import managed_claude as engine


class Transport:
    def __init__(self, argv, cwd, **options):
        self.argv, self.options = argv, options
        self.sent = []
        self.exit_code = None
        self.failure_reason = None
        self.alive = True
        self._write_lock = asyncio.Lock()

    async def start(self): pass

    async def send(self, frame, context):
        async with self._write_lock:
            if not self.options["can_send"](context): return False
            self.sent.append(frame)
            if frame.get("request", {}).get("subtype") == "initialize":
                await self.options["on_frame"]({"type": "system", "subtype": "init",
                    "session_id": self.argv[-1], "claude_code_version": "2.1.310", "permissionMode": "default",
                    "capabilities": ["interrupt_cancel_queued_v1", "interrupt_receipt_v1"]})
                await self.options["on_frame"]({"type": "control_response", "response": {"subtype": "success",
                    "request_id": frame["request_id"], "response": {}}})
            return True

    async def close(self):
        if self.alive:
            self.alive = False
            self.exit_code = 0
            await self.options["on_exit"](0, self.failure_reason)
        return 0

    async def wait_closed(self): return await self.close()


async def make_driver(monkeypatch, tmp_path):
    events = []
    async def emit(event): events.append(event)
    async def version(*_, **__): return "2.1.310"
    monkeypatch.setattr(engine, "JsonLineProcess", Transport)
    monkeypatch.setattr(engine, "verify_version", version)
    monkeypatch.setattr(engine, "resolve_native_binary", lambda *_, **__: "C:/native/claude.exe")
    driver = engine.ClaudeManagedDriver(tmp_path, {"binding_id": "binding", "journal_id": "journal",
        "session_id": "app-session", "process_generation": "generation", "provider": "claude"}, emit, lambda _: True)
    await driver.start()
    return driver, events


async def submit(driver, index=1):
    message = {"submission_id": f"submission-{index}", "user_message_uuid": str(uuid.uuid4()),
               "text": "same original\ntext", "cancel_version": 0}
    await driver.submit(message)
    return message


def result(driver, message, **changes):
    frame = {"type": "result", "subtype": "success", "session_id": driver.snapshot()["provider_root_id"],
        "uuid": str(uuid.uuid4()), "result_index": 0, "user_message_uuid": message["user_message_uuid"],
        "origin": {"kind": "human"}, "is_error": False, "num_turns": 1, "terminal_reason": "completed",
        "result": "verbatim\nfinal"}
    frame.update(changes)
    return frame


def test_launch_and_two_turns_preserve_exact_final(monkeypatch, tmp_path):
    async def run():
        driver, events = await make_driver(monkeypatch, tmp_path)
        command = driver._process.argv
        assert "--permission-prompt-tool=stdio" in command
        assert command[command.index("--permission-prompts") + 1] == "host"
        assert not any(x in command for x in ["--tools", "--bare", "--resume", "--dangerously-skip-permissions"])
        for index in range(2):
            message = await submit(driver, index)
            sent = driver._process.sent[-1]
            assert sent["origin"] == {"kind": "human"} and sent["uuid"] == message["user_message_uuid"]
            await driver._on_frame(result(driver, message, result_index=index))
        settlements = [event for event in events if event["kind"] == "settled"]
        assert len(settlements) == 2
        assert all(event["outcome"] == "completed" and event["finals"][0]["text"] == "verbatim\nfinal" for event in settlements)
        await driver.close()
    asyncio.run(run())


@pytest.mark.parametrize("change", [
    {"num_turns": 0}, {"terminal_reason": "hook_stopped"}, {"terminal_reason": None},
    {"is_error": True}, {"interrupted_by_shutdown": True}, {"origin": {"kind": "peer", "from": "x"}},
    {"parent_tool_use_id": "child"}, {"agent_id": "child"}, {"session_id": "foreign"},
    {"user_message_uuids": ["foreign"]}, {"result_index": 4}, {"uuid": "not-a-uuid"},
])
def test_negative_final_proofs(monkeypatch, tmp_path, change):
    async def run():
        driver, events = await make_driver(monkeypatch, tmp_path)
        message = await submit(driver)
        await driver._on_frame(result(driver, message, **change))
        assert not any(event["kind"] == "settled" and event["finals"] for event in events)
        await driver.close()
    asyncio.run(run())


def test_withdrawn_queued_allow_is_fenced_at_physical_send(monkeypatch, tmp_path):
    async def run():
        driver, events = await make_driver(monkeypatch, tmp_path)
        message = await submit(driver)
        await driver._on_frame({"type": "control_request", "request_id": "ask", "request": {
            "subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "SECRET"}, "tool_use_id": "tool"}})
        assert events[-1]["choices"] == ["allow_once", "deny"]
        await driver._process._write_lock.acquire()
        response = asyncio.create_task(driver.respond_permission("ask", "allow_once",
            submission_id=message["submission_id"], cancel_version=0))
        await asyncio.sleep(0)
        await driver._on_frame({"type": "control_cancel_request", "request_id": "ask"})
        driver._process._write_lock.release()
        await response
        assert not any(frame.get("response", {}).get("request_id") == "ask" for frame in driver._process.sent)
        await driver.close()
        assert "SECRET" not in str([event for event in events if event["kind"] != "permission"])
    asyncio.run(run())


def test_interrupt_before_submit_write_and_pending_ack(monkeypatch, tmp_path):
    async def run():
        driver, events = await make_driver(monkeypatch, tmp_path)
        await driver._process._write_lock.acquire()
        message = {"submission_id": "submission", "user_message_uuid": str(uuid.uuid4()), "text": "hello", "cancel_version": 0}
        send = asyncio.create_task(driver.submit(message))
        await asyncio.sleep(0)
        interrupt = asyncio.create_task(driver.interrupt("submission", 1))
        await asyncio.sleep(0)
        driver._process._write_lock.release()
        await asyncio.gather(send, interrupt)
        assert not any(frame.get("type") == "user" for frame in driver._process.sent)
        assert any(event["kind"] == "settled" and event["reason"] == "not_dispatched" for event in events)
        await driver.close()
    asyncio.run(run())


def test_initialize_ack_bootstraps_without_claiming_confirmed_root(monkeypatch, tmp_path):
    async def run():
        driver, events = await make_driver(monkeypatch, tmp_path)
        await driver.close()
        original = Transport.send
        async def no_init(self, frame, context):
            if frame.get("request", {}).get("subtype") != "initialize": return await original(self, frame, context)
            self.sent.append(frame)
            await self.options["on_frame"]({"type": "control_response", "response": {"subtype": "success",
                "request_id": frame["request_id"], "response": {"current_permission_mode": "default"}}})
            return True
        monkeypatch.setattr(Transport, "send", no_init)
        driver = engine.ClaudeManagedDriver(tmp_path, {"provider": "claude"}, driver._emit_callback, lambda _: True)
        snapshot = await driver.start()
        assert snapshot["provider_root_id"] is None and snapshot["provider_root_confirmed"] is False
        message = await submit(driver)
        unconfirmed = result(driver, message, session_id=driver._requested_root)
        await driver._on_frame(unconfirmed)
        assert driver._active is not None
        await driver._on_frame({"type": "system", "subtype": "init", "session_id": driver._requested_root,
            "claude_code_version": "2.1.310", "permissionMode": "acceptEdits"})
        await driver._on_frame(unconfirmed)
        assert events[-1]["kind"] == "settled" and events[-1]["outcome"] == "completed"
        assert driver.snapshot()["policy"]["permission_mode"] == "acceptEdits"
        await driver.close()
    asyncio.run(run())


@pytest.mark.parametrize("control_request", [
    {"subtype": "can_use_tool", "tool_name": "AskUserQuestion", "input": {}},
    {"subtype": "can_use_tool", "tool_name": "Tool", "input": {}, "requires_user_interaction": True},
    {"subtype": "can_use_tool", "tool_name": "Tool", "input": {}, "requires_user_interaction": "unknown"},
    {"subtype": "request_user_dialog", "questions": []},
    {"subtype": "mcp_unknown", "input": {}},
])
def test_interactive_and_unknown_controls_are_deny_only(monkeypatch, tmp_path, control_request):
    async def run():
        driver, events = await make_driver(monkeypatch, tmp_path)
        message = await submit(driver)
        await driver._on_frame({"type": "control_request", "request_id": "ask", "request": control_request})
        assert events[-1]["choices"] == ["deny"]
        with pytest.raises(engine.ManagedProcessError):
            await driver.respond_permission("ask", "allow_once", submission_id=message["submission_id"], cancel_version=0)
        await driver.respond_permission("ask", "deny", submission_id=message["submission_id"], cancel_version=0)
        reply = driver._process.sent[-1]
        assert "allow" not in str(reply) and "updatedPermissions" not in str(reply)
        await driver.close()
    asyncio.run(run())


def test_once_approval_omits_persistent_rules_and_duplicate_request(monkeypatch, tmp_path):
    async def run():
        driver, events = await make_driver(monkeypatch, tmp_path)
        message = await submit(driver)
        request = {"type": "control_request", "request_id": "ask", "request": {
            "subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "git status"},
            "tool_use_id": "tool", "permission_suggestions": [{"type": "allow forever"}]}}
        await driver._on_frame(request)
        await driver.respond_permission("ask", "allow_once", submission_id=message["submission_id"], cancel_version=0)
        response = driver._process.sent[-1]["response"]["response"]
        assert response == {"behavior": "allow", "updatedInput": {"command": "git status"}, "toolUseID": "tool"}
        await driver._on_frame(request)
        assert len([event for event in events if event["kind"] == "permission"]) == 1
        await driver.close()
    asyncio.run(run())


def test_cancel_pending_ack_does_not_complete_and_invalidates_queued_permission(monkeypatch, tmp_path):
    async def run():
        driver, events = await make_driver(monkeypatch, tmp_path)
        message = await submit(driver)
        await driver._on_frame({"type": "control_request", "request_id": "ask", "request": {
            "subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "git status"}}})
        await driver._process._write_lock.acquire()
        approval = asyncio.create_task(driver.respond_permission("ask", "allow_once", submission_id=message["submission_id"], cancel_version=0))
        await asyncio.sleep(0)
        interrupt = asyncio.create_task(driver.interrupt(message["submission_id"], 1))
        await asyncio.sleep(0)
        driver._process._write_lock.release()
        await asyncio.gather(approval, interrupt)
        assert driver._active is not None and driver.snapshot()["state"] == "interrupting"
        assert not any(frame.get("response", {}).get("request_id") == "ask" for frame in driver._process.sent)
        control = driver._process.sent[-1]
        assert control["request"] == {"subtype": "interrupt", "cancel_queued": True}
        await driver._on_frame({"type": "control_response", "response": {"subtype": "success", "request_id": control["request_id"],
            "response": {"still_queued": [], "cancelled": []}}})
        assert driver._active is not None
        await driver._on_frame(result(driver, message, terminal_reason="aborted_streaming"))
        assert events[-1]["outcome"] == "interrupted" and events[-1]["finals"] == []
        await driver.close()
    asyncio.run(run())


def test_actual_transport_with_local_schema_peer_and_inherited_environment(monkeypatch, tmp_path):
    """Actual pipes/threads, local Python protocol peer; no provider/model run."""
    peer = r'''
import json,sys,uuid
root=sys.argv[1]
index=0
def out(frame): print(json.dumps(frame),flush=True)
for line in sys.stdin:
    frame=json.loads(line)
    if frame.get('type')=='control_request':
        out({'type':'control_response','response':{'subtype':'success','request_id':frame['request_id'],
            'response':{'claude_code_version':'2.1.310','current_permission_mode':'default'}}})
    if frame.get('type')=='user':
        out({'type':'system','subtype':'init','session_id':root,'claude_code_version':'2.1.310','permissionMode':'default'})
        out(frame)
        out({'type':'result','subtype':'success','session_id':root,'uuid':str(uuid.uuid4()),'result_index':index,
             'user_message_uuid':frame['uuid'],'origin':{'kind':'human'},'is_error':False,'num_turns':1,
             'terminal_reason':'completed','result':'local schema peer\nverbatim'})
        index+=1
'''
    async def run():
        events, launches = [], []
        async def emit(event): events.append(event)
        def factory(argv, **kwargs):
            launches.append((argv, kwargs))
            command = [sys.executable, "-u", "-c", "print('2.1.310 (Claude Code)')"] if argv[-1] == "--version" else [
                sys.executable, "-u", "-c", peer, argv[-1]]
            return subprocess.Popen(command, **kwargs)
        monkeypatch.setattr(engine, "resolve_native_binary", lambda *_, **__: sys.executable)
        monkeypatch.setenv("CLAUDECODE", "parent")
        monkeypatch.setenv("HTTPS_PROXY", "http://inherited.example")
        monkeypatch.setenv("MANAGED_SYNTHETIC_SETTING", "preserved")
        driver = engine.ClaudeManagedDriver(tmp_path, {"provider": "claude"}, emit, lambda _: True, process_factory=factory)
        ready = await driver.start()
        assert ready["provider_root_id"] is None
        assert ready["policy"]["permission_mode"] == "default"
        for turn in range(2):
            await submit(driver, turn)
            for _ in range(500):
                if len([event for event in events if event["kind"] == "settled"]) > turn: break
                await asyncio.sleep(.005)
            assert driver.snapshot()["state"] == "ready"
        await driver.close()
        assert len(launches) == 2
        argv, options = launches[1]
        assert options["cwd"] == str(tmp_path)
        assert options["env"]["HTTPS_PROXY"] == "http://inherited.example"
        assert options["env"]["MANAGED_SYNTHETIC_SETTING"] == "preserved"
        assert "CLAUDECODE" not in options["env"]
        assert all(event["finals"][0]["text"] == "local schema peer\nverbatim"
                   for event in events if event["kind"] == "settled")
        assert events[-1]["kind"] == "closed" and not driver._process.alive
        assert all(not thread.is_alive() for thread in driver._process._threads)
    asyncio.run(run())


def test_version_failure_prevents_any_runtime_launch(monkeypatch, tmp_path):
    async def run():
        launches = []
        async def emit(_): pass
        def factory(argv, **kwargs):
            launches.append(argv)
            return subprocess.Popen([sys.executable, "-u", "-c", "print('2.1.311 (Claude Code)')"], **kwargs)
        monkeypatch.setattr(engine, "resolve_native_binary", lambda *_, **__: sys.executable)
        driver = engine.ClaudeManagedDriver(tmp_path, {"provider": "claude"}, emit, lambda _: True, process_factory=factory)
        with pytest.raises(engine.ManagedProcessError, match="managed_version_unsupported"):
            await driver.start()
        assert len(launches) == 1 and launches[0][-1] == "--version"
    asyncio.run(run())


def test_conversation_reset_preserves_run_delivery_sequence(monkeypatch, tmp_path):
    async def run():
        driver, events = await make_driver(monkeypatch, tmp_path)
        message = await submit(driver)
        await driver._on_frame(result(driver, message))
        epoch = str(uuid.uuid4())
        await driver._on_frame({"type": "conversation_reset", "session_id": driver._root,
            "new_conversation_id": epoch})
        message = await submit(driver, 2)
        await driver._on_frame(result(driver, message, result_index=1))
        assert events[-1]["outcome"] == "completed"
        assert events[-1]["finals"][0]["proof"]["conversation_epoch"] == epoch
        await driver.close()
    asyncio.run(run())


@pytest.mark.parametrize("changes,expected", [
    ({"num_turns": 0, "terminal_reason": None}, "blocked"),
    ({"num_turns": 0, "is_error": True, "user_message_uuid": None, "origin": None}, "failed"),
])
def test_pre_init_negative_outcome_never_claims_requested_root(monkeypatch, tmp_path, changes, expected):
    original = Transport.send
    async def ack_only(self, frame, context):
        if frame.get("request", {}).get("subtype") != "initialize": return await original(self, frame, context)
        self.sent.append(frame)
        await self.options["on_frame"]({"type": "control_response", "response": {"subtype": "success",
            "request_id": frame["request_id"], "response": {"current_permission_mode": "default"}}})
        return True
    monkeypatch.setattr(Transport, "send", ack_only)
    async def run():
        driver, events = await make_driver(monkeypatch, tmp_path)
        message = await submit(driver)
        await driver._on_frame(result(driver, message, session_id=driver._requested_root, **changes))
        assert events[-1]["outcome"] == expected and events[-1]["provider_root_id"] is None
        assert events[-1]["finals"] == []
        await driver._on_frame({"type": "system", "subtype": "init", "session_id": driver._requested_root,
            "claude_code_version": "2.1.310", "permissionMode": "default"})
        message = await submit(driver, 2)
        await driver._on_frame(result(driver, message, result_index=1))
        assert events[-1]["outcome"] == "completed"
        await driver.close()
    asyncio.run(run())


def test_valid_completed_result_wins_race_with_interrupt_request(monkeypatch, tmp_path):
    async def run():
        driver, events = await make_driver(monkeypatch, tmp_path)
        message = await submit(driver)
        await driver.interrupt(message["submission_id"], 1)
        assert driver.snapshot()["state"] == "interrupting"
        await driver._on_frame(result(driver, message))
        assert events[-1]["kind"] == "settled"
        assert events[-1]["outcome"] == "completed"
        assert events[-1]["finals"][0]["text"] == "verbatim\nfinal"
        assert events[-1]["finals"][0]["proof"]["terminal_reason"] == "completed"
        await driver.close()
    asyncio.run(run())


def test_positive_permission_requires_confirmed_root_and_rearms_same_pending_request(monkeypatch, tmp_path):
    original = Transport.send
    async def ack_only(self, frame, context):
        if frame.get("request", {}).get("subtype") != "initialize": return await original(self, frame, context)
        self.sent.append(frame)
        await self.options["on_frame"]({"type": "control_response", "response": {"subtype": "success",
            "request_id": frame["request_id"], "response": {"current_permission_mode": "default"}}})
        return True
    monkeypatch.setattr(Transport, "send", ack_only)
    async def run():
        driver, events = await make_driver(monkeypatch, tmp_path)
        message = await submit(driver)
        async def ask(request_id):
            await driver._on_frame({"type": "control_request", "request_id": request_id, "request": {
                "subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "git status"}}})
        await ask("deny-before-init")
        pending = driver._permissions["deny-before-init"]
        pending.update(state="responding", decision="allow_once")
        assert driver._can_send({"operation": "permission", "submission_id": message["submission_id"],
            "cancel_version": 0, "permission_request_id": "deny-before-init", "decision": "allow_once"}) is False
        pending["state"] = "pending"
        before = len(driver._process.sent)
        with pytest.raises(engine.ManagedProcessError, match="managed_root_unconfirmed"):
            await driver.respond_permission("deny-before-init", "allow_once", submission_id=message["submission_id"], cancel_version=0)
        assert len(driver._process.sent) == before
        assert driver._permissions["deny-before-init"]["state"] == "pending"
        await driver.respond_permission("deny-before-init", "deny", submission_id=message["submission_id"], cancel_version=0)
        assert driver._process.sent[-1]["response"]["response"]["behavior"] == "deny"
        await ask("pending-through-init")
        before = len(driver._process.sent)
        with pytest.raises(engine.ManagedProcessError, match="managed_root_unconfirmed"):
            await driver.respond_permission("pending-through-init", "allow_once", submission_id=message["submission_id"], cancel_version=0)
        assert len(driver._process.sent) == before
        await driver._on_frame({"type": "system", "subtype": "init", "session_id": driver._requested_root,
            "claude_code_version": "2.1.310", "permissionMode": "default"})
        await driver.respond_permission("pending-through-init", "allow_once", submission_id=message["submission_id"], cancel_version=0)
        assert driver._process.sent[-1]["response"]["request_id"] == "pending-through-init"
        assert driver._process.sent[-1]["response"]["response"]["behavior"] == "allow"
        await driver.close()
    asyncio.run(run())
