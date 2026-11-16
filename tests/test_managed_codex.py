"""Exact 0.167 protocol fixtures; no provider requests or authentication."""
import asyncio
import copy

import pytest

from supermonoagent import managed_codex as engine


BINDING = dict(binding_id="app-binding", journal_id="journal", session_id="app-session",
               process_generation="generation", provider="codex")


class Transport:
    def __init__(self, argv, cwd, **kwargs):
        self.argv, self.cwd, self.kw = argv, cwd, kwargs
        self.sent = []
        self.exit_code = None
        self.alive = False
        self.failure_reason = None
        self.hold_start = False
        self.before_ack = []
        self.block = None

    async def start(self):
        self.alive = True

    async def send(self, frame, context):
        if self.block:
            await self.block.wait()
        if not self.kw["can_send"](context):
            return False
        self.sent.append(copy.deepcopy(frame))
        method = frame.get("method")
        if method == "initialize":
            await self.frame({"id": frame["id"], "result": {"userAgent": "supermono-managed-conversation/0.167.0 (Windows)"}})
        elif method == "thread/start":
            await self.frame({"id": frame["id"], "result": {"thread": {"id": "thread"},
                "approvalPolicy": "on-request", "approvalsReviewer": "user", "sandbox": {"type": "workspaceWrite"},
                "model": "configured", "modelProvider": "configured", "cwd": self.cwd}})
        elif method == "turn/start":
            self.start_request = frame
            for event in self.before_ack:
                await self.frame(event)
            if not self.hold_start:
                await self.ack()
        elif method == "turn/interrupt":
            await self.frame({"id": frame["id"], "result": {}})
        return True

    async def ack(self):
        await self.frame({"id": self.start_request["id"], "result": {"turn": {
            "id": "turn", "status": "inProgress", "items": []}}})

    async def frame(self, event):
        await self.kw["on_frame"](event)

    async def close(self):
        self.alive = False
        self.exit_code = 0
        await self.kw["on_exit"](0, "closed")
        return 0


def item(value, thread="thread", turn="turn"):
    return {"method": "item/completed", "params": {"threadId": thread, "turnId": turn,
        "completedAtMs": 1, "item": value}}


def user(client="user"):
    return item({"id": "input", "type": "userMessage", "clientId": client, "content": []})


def final(phase="final_answer", identity="answer", text="Answer"):
    return item({"id": identity, "type": "agentMessage", "phase": phase, "text": text})


def completed(status="completed", error=None, items=None):
    return {"method": "turn/completed", "params": {"threadId": "thread", "turn": {
        "id": "turn", "status": status, "error": error, "items": items or []}}}


async def setup(monkeypatch, fence=lambda c: True):
    async def version(binary, expected, **kwargs):
        assert binary == "verified-codex.exe" and expected == "0.167.0"
        return expected
    monkeypatch.setattr(engine, "verify_version", version)
    monkeypatch.setattr(engine, "resolve_native_binary", lambda *a, **k: "verified-codex.exe")
    monkeypatch.setattr(engine, "JsonLineProcess", Transport)
    events = []
    async def emit(event):
        events.append(event)
    driver = engine.CodexManagedDriver("project", BINDING, emit, fence)
    await driver.start()
    return driver, events, driver._process


async def submit(driver):
    return await driver.submit(dict(submission_id="submission", user_message_uuid="user", text="Hello", cancel_version=0))


def outcomes(events):
    return [e for e in events if e["kind"] == "settled"]


def test_launch_preserves_native_settings_and_exact_ids(monkeypatch):
    async def run():
        driver, events, process = await setup(monkeypatch)
        assert process.argv == ["verified-codex.exe", "--no-daemon", "app-server", "--listen", "stdio://"]
        assert process.cwd == "project"
        assert events[0]["provider_root_id"] == "thread" != BINDING["binding_id"]
        assert driver.snapshot()["policy"]["approvalPolicy"] == "on-request"
        await submit(driver)
        params = process.start_request["params"]
        assert params == {"threadId": "thread", "clientUserMessageId": "user", "input": [
            {"type": "text", "text": "Hello", "text_elements": []}]}
        await driver.close()
    asyncio.run(run())


def test_notifications_before_ack_are_correlated_and_settle_once(monkeypatch):
    async def run():
        driver, events, process = await setup(monkeypatch)
        process.before_ack = [user(), final(), completed()]
        await submit(driver)
        assert outcomes(events)[0]["finals"][0]["text"] == "Answer"
        assert [e["kind"] for e in events].index("turn_started") < [e["kind"] for e in events].index("settled")
        await process.frame(completed())
        assert len(outcomes(events)) == 1
        await driver.close()
    asyncio.run(run())


@pytest.mark.parametrize("phase", [None, "commentary", "partial_answer", "future_phase"])
def test_unknown_or_nonfinal_phase_never_becomes_final(monkeypatch, phase):
    async def run():
        driver, events, process = await setup(monkeypatch)
        await submit(driver)
        for event in [user(), final(phase), completed()]:
            await process.frame(event)
        assert outcomes(events)[0]["outcome"] == "ambiguous"
        assert outcomes(events)[0]["finals"] == []
        await driver.close()
    asyncio.run(run())


@pytest.mark.parametrize("status,error", [("future", None), ("failed", None), ("interrupted", None), ("completed", {"message": "private"})])
def test_error_or_unknown_terminal_status_is_not_completion(monkeypatch, status, error):
    async def run():
        driver, events, process = await setup(monkeypatch)
        await submit(driver)
        for event in [user(), final(), completed(status, error)]:
            await process.frame(event)
        assert outcomes(events)[0]["outcome"] != "completed"
        assert outcomes(events)[0]["finals"] == []
        await driver.close()
    asyncio.run(run())


@pytest.mark.parametrize("client", [None, "foreign-user"])
def test_input_client_id_must_match(monkeypatch, client):
    async def run():
        driver, events, process = await setup(monkeypatch)
        await submit(driver)
        for event in [user(client), final(), completed()]:
            await process.frame(event)
        assert outcomes(events)[0]["outcome"] == "ambiguous"
        await driver.close()
    asyncio.run(run())


def test_foreign_thread_turn_and_duplicate_item_do_not_pollute(monkeypatch):
    async def run():
        driver, events, process = await setup(monkeypatch)
        await submit(driver)
        foreign = final(text="Foreign")
        foreign["params"]["threadId"] = "foreign"
        wrong = final(text="Wrong")
        wrong["params"]["turnId"] = "wrong"
        for event in [foreign, wrong, user(), final(), final(), final(identity="second", text="Second"), completed()]:
            await process.frame(event)
        assert [f["text"] for f in outcomes(events)[0]["finals"]] == ["Answer", "Second"]
        await driver.close()
    asyncio.run(run())


def approval(method="item/commandExecution/requestApproval", identity="approval"):
    return {"id": identity, "method": method, "params": {"threadId": "thread", "turnId": "turn",
        "itemId": "command", "startedAtMs": 1, "command": "git status", "cwd": "project"}}


@pytest.mark.parametrize("method", ["item/commandExecution/requestApproval", "item/fileChange/requestApproval"])
@pytest.mark.parametrize("decision,wire", [("allow_once", "accept"), ("deny", "decline")])
def test_exact_once_only_approval_shapes(monkeypatch, method, decision, wire):
    async def run():
        driver, events, process = await setup(monkeypatch)
        await submit(driver)
        await process.frame(approval(method))
        permission = next(e for e in events if e["kind"] == "permission")
        await driver.respond_permission(permission["permission_request_id"], decision, submission_id="submission", cancel_version=0)
        assert process.sent[-1] == {"id": "approval", "result": {"decision": wire}}
        with pytest.raises(engine.ManagedError):
            await driver.respond_permission(permission["permission_request_id"], decision, submission_id="submission", cancel_version=0)
        await driver.close()
    asyncio.run(run())


def test_unknown_server_request_returns_error_and_visible_capability_failure(monkeypatch):
    async def run():
        driver, events, process = await setup(monkeypatch)
        await submit(driver)
        await process.frame(approval("item/tool/requestUserInput"))
        assert process.sent[-1]["error"]["code"] == -32601
        assert any(e["kind"] == "error" and e["reason"] == "unsupported_server_request" for e in events)
        assert not any(e["kind"] == "permission" for e in events)
        await driver.close()
    asyncio.run(run())


def test_cancel_before_dispatch_writes_no_turn_start(monkeypatch):
    async def run():
        driver, events, process = await setup(monkeypatch, lambda c: c["operation"] != "submit")
        await submit(driver)
        assert not any(f.get("method") == "turn/start" for f in process.sent)
        assert outcomes(events)[0]["reason"] == "not_dispatched"
        await driver.close()
    asyncio.run(run())


def test_cancel_pending_ack_interrupts_exact_turn_without_finalizing_ack(monkeypatch):
    async def run():
        driver, events, process = await setup(monkeypatch)
        process.hold_start = True
        task = asyncio.create_task(submit(driver))
        await asyncio.sleep(0)
        await driver.interrupt("submission", 1)
        assert not any(f.get("method") == "turn/interrupt" for f in process.sent)
        await process.ack()
        await task
        interrupts = [f for f in process.sent if f.get("method") == "turn/interrupt"]
        assert interrupts[0]["params"] == {"threadId": "thread", "turnId": "turn"}
        assert outcomes(events) == []
        await process.frame(completed("interrupted"))
        assert outcomes(events)[0]["outcome"] == "interrupted"
        await driver.close()
    asyncio.run(run())


@pytest.mark.parametrize("status,error,client,expected", [
    ("completed", None, "user", "completed"),
    ("interrupted", None, "user", "interrupted"),
    ("failed", None, "user", "failed"),
    ("completed", {"message": "synthetic error"}, "user", "ambiguous"),
    ("completed", None, "foreign-user", "ambiguous"),
])
def test_pending_cancel_preserves_actual_provider_terminal_evidence(monkeypatch, status, error, client, expected):
    async def run():
        driver, events, process = await setup(monkeypatch)
        process.hold_start = True
        dispatch = asyncio.create_task(submit(driver))
        await asyncio.sleep(0)
        await driver.interrupt("submission", 1)
        await process.ack()
        await dispatch
        assert any(f.get("method") == "turn/interrupt" for f in process.sent)
        assert outcomes(events) == []  # An interrupt ACK is a request receipt.
        for event in [user(client), final(), completed(status, error)]:
            await process.frame(event)
        result = outcomes(events)
        assert len(result) == 1
        assert result[0]["outcome"] == expected
        if expected == "completed":
            assert [entry["text"] for entry in result[0]["finals"]] == ["Answer"]
            assert result[0]["finals"][0]["proof"]["terminal_status"] == "completed"
        else:
            assert result[0]["finals"] == []
        await driver.close()
    asyncio.run(run())


def test_withdrawal_and_cancel_invalidate_queued_allow_at_write(monkeypatch):
    async def run():
        driver, events, process = await setup(monkeypatch)
        await submit(driver)
        await process.frame(approval())
        permission = next(e for e in events if e["kind"] == "permission")
        process.block = asyncio.Event()
        answer = asyncio.create_task(driver.respond_permission(permission["permission_request_id"], "allow_once", submission_id="submission", cancel_version=0))
        await asyncio.sleep(0)
        await process.frame({"method": "serverRequest/resolved", "params": {"threadId": "thread", "requestId": "approval"}})
        process.block.set()
        with pytest.raises(engine.ManagedError):
            await answer
        assert not any(f.get("result", {}).get("decision") == "accept" for f in process.sent)
        await driver.close()
    asyncio.run(run())


def test_shutdown_settles_active_without_final_and_emits_closed(monkeypatch):
    async def run():
        driver, events, process = await setup(monkeypatch)
        await submit(driver)
        await process.frame(final())
        await driver.close()
        assert outcomes(events)[0]["outcome"] != "completed"
        assert outcomes(events)[0]["finals"] == []
        assert events[-1]["kind"] == "closed"
        assert not process.alive
    asyncio.run(run())


def test_generation_fence_and_one_active_submission(monkeypatch):
    async def run():
        driver, events, process = await setup(monkeypatch)
        await submit(driver)
        with pytest.raises(engine.ManagedError, match="busy"):
            await submit(driver)
        assert not driver._fence({"operation": "interrupt", "process_generation": "foreign",
                                  "submission_id": "submission", "cancel_version": 0})
        await driver.close()
    asyncio.run(run())


def test_close_during_pending_ack_preserves_terminal_event_order(monkeypatch):
    async def run():
        driver, events, process = await setup(monkeypatch)
        process.hold_start = True
        dispatch = asyncio.create_task(submit(driver))
        await asyncio.sleep(0)
        await driver.close()
        with pytest.raises(engine.ManagedError, match="process_closed"):
            await dispatch
        assert outcomes(events)[0]["outcome"] == "ambiguous"
        assert events[-1]["kind"] == "closed"
        assert sum(e["kind"] == "settled" for e in events) == 1
    asyncio.run(run())


@pytest.mark.parametrize("method", ["item/permissions/requestApproval", "mcpServer/elicitation/request", "unknown/action"])
def test_unsupported_typed_controls_cannot_get_generic_allow(monkeypatch, method):
    async def run():
        driver, events, process = await setup(monkeypatch)
        await submit(driver)
        await process.frame(approval(method))
        assert process.sent[-1] == {"id": "approval", "error": {
            "code": -32601, "message": "Unsupported managed request"}}
        await driver.close()
    asyncio.run(run())


def test_rpc_server_request_during_pending_turn_ack_is_not_dropped(monkeypatch):
    async def run():
        driver, events, process = await setup(monkeypatch)
        process.before_ack = [approval()]
        await submit(driver)
        permission = next(e for e in events if e["kind"] == "permission")
        await driver.respond_permission(permission["permission_request_id"], "deny", submission_id="submission", cancel_version=0)
        assert process.sent[-1]["result"] == {"decision": "decline"}
        await driver.close()
    asyncio.run(run())


def test_permission_cancel_race_drops_queued_allow(monkeypatch):
    async def run():
        driver, events, process = await setup(monkeypatch)
        await submit(driver)
        await process.frame(approval())
        permission = next(e for e in events if e["kind"] == "permission")
        process.block = asyncio.Event()
        answer = asyncio.create_task(driver.respond_permission(permission["permission_request_id"], "allow_once", submission_id="submission", cancel_version=0))
        await asyncio.sleep(0)
        interrupt = asyncio.create_task(driver.interrupt("submission", 1))
        await asyncio.sleep(0)
        process.block.set()
        with pytest.raises(engine.ManagedError, match="stale_permission"):
            await answer
        await interrupt
        assert not any(f.get("result", {}).get("decision") == "accept" for f in process.sent)
        await driver.close()
    asyncio.run(run())


@pytest.mark.parametrize("bad", ["duplicate_rpc", "duplicate_request", "conflicting_item", "early_overflow"])
def test_protocol_identity_and_buffer_failures_revoke_dispatch(monkeypatch, bad):
    async def run():
        driver, events, process = await setup(monkeypatch)
        if bad == "early_overflow":
            process.before_ack = [final(identity=str(i)) for i in range(33)]
            with pytest.raises(engine.ManagedError):
                await submit(driver)
        else:
            await submit(driver)
            if bad == "duplicate_rpc":
                await process.frame({"id": 1, "result": {}})
            elif bad == "duplicate_request":
                await process.frame(approval())
                await process.frame(approval())
            else:
                await process.frame(final())
                await process.frame(final(text="Changed"))
        assert any(e["kind"] == "error" and e["fatal"] for e in events)
        assert not driver._fence(driver._context("submit", submission_id="submission", cancel_version=0))
        await driver.close()
    asyncio.run(run())


def test_emit_failure_revokes_sends_and_never_retries_final_callback(monkeypatch):
    async def run():
        driver, events, process = await setup(monkeypatch)
        await submit(driver)
        calls = []
        async def broken(event):
            calls.append(event)
            raise RuntimeError("disk unavailable")
        driver._emit = broken
        await process.frame(user())
        await process.frame(final())
        with pytest.raises(RuntimeError):
            await process.frame(completed())
        await driver.close()
        assert [event["kind"] for event in calls] == ["settled"]
        assert not driver._fence(driver._context("permission", submission_id="submission", cancel_version=0))
    asyncio.run(run())


def test_exact_version_rejection_prevents_app_server_launch(monkeypatch):
    async def run():
        monkeypatch.setattr(engine, "resolve_native_binary", lambda *a: "path-codex.exe")
        async def unsupported(binary, expected, **kwargs):
            assert binary == "path-codex.exe"
            raise engine.ManagedError("managed_version_unsupported")
        monkeypatch.setattr(engine, "verify_version", unsupported)
        def forbidden(*args, **kwargs):
            raise AssertionError("app-server launched after version failure")
        monkeypatch.setattr(engine, "JsonLineProcess", forbidden)
        driver = engine.CodexManagedDriver("project", BINDING, None, lambda c: True)
        with pytest.raises(engine.ManagedError, match="version_unsupported"):
            await driver.start()
    asyncio.run(run())


@pytest.mark.parametrize("blocked_method,repeat_cancel", [
    ("initialize", False), ("thread/start", False), ("initialize", True),
])
def test_cancel_start_joins_owned_child_and_readers_before_reraising(monkeypatch, tmp_path, blocked_method, repeat_cancel):
    import subprocess
    import sys
    from supermonoagent.managed_process import JsonLineProcess
    marker = tmp_path / "request-received"
    script = r'''
import sys,json,pathlib
for line in sys.stdin:
 f=json.loads(line); method=f.get('method')
 if method==sys.argv[2]:
  pathlib.Path(sys.argv[1]).write_text(method)
 elif method=='initialize':
  print(json.dumps({'id':f['id'],'result':{'userAgent':'supermono-managed-conversation/0.167.0 (fixture)'}}),flush=True)
'''
    async def run():
        close_entered, release_close = asyncio.Event(), asyncio.Event()
        if not repeat_cancel:
            release_close.set()
        writes, events = [], []
        class ObservedProcess(JsonLineProcess):
            async def send(self, frame, context):
                writes.append(frame.get("method"))
                return await super().send(frame, context)
            async def close(self):
                close_entered.set()
                await release_close.wait()
                return await super().close()
        async def version(*args, **kwargs): return "0.167.0"
        async def emit(event): events.append(event)
        def factory(argv, **kwargs):
            assert argv[1:] == ["--no-daemon", "app-server", "--listen", "stdio://"]
            return subprocess.Popen([sys.executable, "-u", "-c", script, str(marker), blocked_method], **kwargs)
        monkeypatch.setattr(engine, "resolve_native_binary", lambda *a: "verified-codex.exe")
        monkeypatch.setattr(engine, "verify_version", version)
        monkeypatch.setattr(engine, "JsonLineProcess", ObservedProcess)
        driver = engine.CodexManagedDriver(tmp_path, BINDING, emit, lambda c: True, process_factory=factory)
        startup = asyncio.create_task(driver.start())
        try:
            async def reached_barrier():
                while not marker.exists(): await asyncio.sleep(.005)
            await asyncio.wait_for(reached_barrier(), 3)
            assert driver._process.alive
            startup.cancel()
            if repeat_cancel:
                await asyncio.wait_for(close_entered.wait(), 1)
                startup.cancel()
                await asyncio.sleep(0)
                assert not startup.done()
                release_close.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(startup, 3)
            assert not driver._process.alive
            assert all(not thread.is_alive() for thread in driver._process._threads)
            assert not driver._process._waiter.is_alive()
            assert driver._process._dispatcher.done()
            assert not driver._process._writers
            assert driver.snapshot()["state"] != "starting"
            assert [event["kind"] for event in events] == ["closed"]
            assert "turn/start" not in writes
        finally:
            release_close.set()
            await driver.close()
    asyncio.run(run())


def test_real_jsonl_dispatcher_handles_notifications_and_request_before_ack(monkeypatch, tmp_path):
    import subprocess
    import sys
    from supermonoagent.managed_process import JsonLineProcess
    script = r'''
import sys,json
def send(x): print(json.dumps(x),flush=True)
for line in sys.stdin:
 f=json.loads(line); m=f.get('method')
 if m=='initialize': send({'id':f['id'],'result':{'userAgent':'supermono-managed-conversation/0.167.0 (fixture)'}})
 elif m=='thread/start': send({'id':f['id'],'result':{'thread':{'id':'thread'},'approvalPolicy':'on-request','approvalsReviewer':'user','sandbox':{'type':'workspaceWrite'},'model':'configured','modelProvider':'configured'}})
 elif m=='turn/start':
  send({'method':'item/completed','params':{'threadId':'thread','turnId':'turn','completedAtMs':1,'item':{'id':'input','type':'userMessage','clientId':f['params']['clientUserMessageId'],'content':[]}}})
  send({'id':'approval','method':'item/commandExecution/requestApproval','params':{'threadId':'thread','turnId':'turn','itemId':'command','startedAtMs':1,'command':'synthetic','cwd':'.'}})
  send({'id':f['id'],'result':{'turn':{'id':'turn','items':[],'status':'inProgress'}}})
 elif f.get('id')=='approval':
  send({'method':'item/completed','params':{'threadId':'thread','turnId':'turn','completedAtMs':1,'item':{'id':'answer','type':'agentMessage','phase':'final_answer','text':'Synthetic answer'}}})
  send({'method':'turn/completed','params':{'threadId':'thread','turn':{'id':'turn','items':[],'status':'completed','error':None}}})
'''
    async def run():
        driver, events, _ = await setup(monkeypatch)
        await driver.close()
        events.clear()
        monkeypatch.setattr(engine, "JsonLineProcess", JsonLineProcess)
        async def emit(event): events.append(event)
        def factory(argv, **kwargs):
            assert argv[1:] == ["--no-daemon", "app-server", "--listen", "stdio://"]
            return subprocess.Popen([sys.executable, "-u", "-c", script], **kwargs)
        driver = engine.CodexManagedDriver(tmp_path, BINDING, emit, lambda c: True, process_factory=factory)
        try:
            await driver.start()
            await submit(driver)
            permission = next(e for e in events if e["kind"] == "permission")
            await driver.respond_permission(permission["permission_request_id"], "deny", submission_id="submission", cancel_version=0)
            async def wait_final():
                while not outcomes(events): await asyncio.sleep(.005)
            await asyncio.wait_for(wait_final(), 3)
            assert outcomes(events)[0]["outcome"] == "completed"
            assert outcomes(events)[0]["finals"][0]["text"] == "Synthetic answer"
        finally:
            await driver.close()
        assert events[-1]["kind"] == "closed"
        assert not driver._process.alive
    asyncio.run(run())
