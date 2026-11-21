"""Synthetic source host: one durable acceptance and revocation on receipt lookup."""
import asyncio
import hashlib
import json
import time
from unittest.mock import AsyncMock, patch

from supermonoagent.managed_conversation import ManagedConversationService
from supermonoagent.protocol import OP_HELLO, OP_JOURNAL, unpack
from test_client_integration import isolated_host
from test_managed_conversation import FakeDriver


def test_feedback_accepts_once_and_rechecks_assignment_on_each_operation():
    with patch('supermonoagent.cockpit_feedback.authorize_at_hub', new=AsyncMock(return_value=True)):
        asyncio.run(check_source())


async def check_source():
    async with isolated_host() as (daemon, _direct, _relay):
        await daemon.managed.aclose()
        drivers = []
        def factory(*args, **kwargs):
            drivers.append(FakeDriver(*args, **kwargs))
            return drivers[-1]
        managed = ManagedConversationService(daemon.hub, daemon.journals,
            credential_authorized=daemon._managed_credential_authorized, driver_factory=factory)
        daemon.managed = daemon.hub.managed_service = daemon.journals.managed_sources = managed
        win = daemon.hub.open_window('fixture', owner=daemon.token)
        anchor = daemon.hub.open_cli(win.id, 'cmd')
        client = daemon.hub.attach(asyncio.Queue(), token=daemon.token)
        async def rpc(action, params):
            req = json.dumps(dict(request_id='req', action=action, params=params)).encode()
            frames, _ = await daemon._op(client, OP_JOURNAL, req)
            return json.loads(unpack(frames[0])[1])

        await daemon._op(client, OP_HELLO, json.dumps(dict(token=daemon.token, multiplex=True,
            conversation_journal=1, managed_conversation=1)).encode())
        created = (await rpc('create_managed', dict(session_id=anchor.id,
            process_generation=anchor.process_generation, provider='codex', nonce='create')))['result']
        target = {k: created[k] for k in ('window_id','session_id','process_generation','journal_id')}
        for _ in range(200):
            if (await rpc('managed_status', target))['result']['state'] == 'ready':
                break
            await asyncio.sleep(.01)
        manager = daemon.journals
        feedback = dict(project='project', task_id='task', attempt_id='attempt',role='primary',account='fixture',
                        host_installation_id=manager.store.host_installation_id, tool='codex')
        receipt_params = dict(target, nonce='confirmed-once', feedback=feedback)
        text = 'Reviewed by the fixture operator.'
        msg = dict(receipt_params, text=text, text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                   expires_at=int(time.time())+90)
        assert not (await rpc('submit_cockpit_feedback', msg))['ok']
        config = daemon.log_state_dir / 'cockpit-feedback.json'
        owner = manager.owner_scope(daemon.token, default_token=daemon.token, accounts=daemon.accounts)
        config.write_text(json.dumps({'bindings':[dict(target, **feedback, owner_scope=owner)]}),encoding='utf-8')
        config.chmod(0o600)
        assert not (await rpc('submit_cockpit_feedback', dict(msg, expires_at=1)))['ok']
        assert not (await rpc('submit_cockpit_feedback', dict(msg, text='edited')))['ok']
        assert not (await rpc('submit_cockpit_feedback', dict(msg, feedback=dict(feedback, attempt_id='stale'))))['ok']
        accepted = await rpc('submit_cockpit_feedback', msg)
        assert accepted['ok'], accepted
        repeated = await rpc('submit_cockpit_feedback', msg)
        assert repeated['result']['event_id'] == accepted['result']['event_id']
        await asyncio.wait_for(drivers[0].entered.wait(),3)
        assert len(drivers[0].sent)==1
        checked = await rpc('cockpit_feedback_status', receipt_params)
        assert checked['result']['receipt']['nonce']=='confirmed-once'
        config.write_text('{"bindings":[]}',encoding='utf-8')
        refused = await rpc('cockpit_feedback_status', receipt_params)
        assert not refused['ok'] and 'result' not in refused
        assert not anchor.pty.written
