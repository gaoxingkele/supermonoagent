"""Local-only AVD harness with temporary stores, controlled PTYs, and a synthetic Codex driver."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from urllib.parse import urlsplit

import uvicorn
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from agent_hub.server import create_app, token_digest
from agent_hub.storage import Store
from supermonoagent.daemon import Daemon, relay_uplink
from supermonoagent.managed_conversation import ManagedConversationService
from supermonoagent.protocol import OP_JOURNAL
from supermonoagent.relay import Relay
from test_client_integration import ControlledPty
from test_managed_conversation import FakeDriver


HUB = '127.0.0.1:18824'
RELAY = '127.0.0.1:18825'
INSTALLATION = 'synthetic-mobile-feedback'
PROJECT = 'qa-mobile'


async def fixture(state,drop_first_ack):
    journal = state/'journals'
    journal.mkdir()
    (journal/'.host-installation-id').write_text(INSTALLATION,encoding='ascii')
    daemon = Daemon('qa-mobile-terminal-token')
    daemon.hub.persist = False
    daemon.hub.pty_factory = lambda *_:ControlledPty()
    daemon.hub.attach_loop(asyncio.get_running_loop())
    daemon.hub.set_identity('QA mobile host','Android test')
    managers=daemon.journals
    assert managers.store.host_installation_id==INSTALLATION
    await daemon.managed.aclose()
    drivers=[]
    def factory(*args,**kwargs):
        drivers.append(FakeDriver(*args,**kwargs))
        return drivers[-1]
    managed=ManagedConversationService(daemon.hub,managers,
        credential_authorized=daemon._managed_credential_authorized,driver_factory=factory)
    daemon.managed=daemon.hub.managed_service=managers.managed_sources=managed
    window=daemon.hub.open_window('QA Native Draft',owner=daemon.token)
    native=daemon.hub.open_cli(window.id,'cmd')
    owner=managers.owner_scope(daemon.token,default_token=daemon.token,accounts=daemon.accounts)
    result=await managed.handle('create_managed',dict(session_id=native.id,process_generation=native.process_generation,
        provider='codex',nonce='qa-avd-create'),owner_scope=owner,owner_token=daemon.token)
    target={k:result[k] for k in ('window_id','session_id','process_generation','journal_id')}
    daemon.hub.rename_window(target['window_id'],'QA Managed Feedback')
    binding=dict(project=PROJECT,task_id='avd-task',attempt_id='managed-A',tool='codex',
                 host_installation_id=INSTALLATION,role='primary',account='qa-seat')
    source_origin='https://qa-source.example'
    config=daemon.log_state_dir/'cockpit-feedback.json'
    config.write_text(json.dumps({'bindings':[dict(binding,**target,owner_scope=owner)],
        'authority':{'endpoint':f'ws://{HUB}/hub/feedback','origin':source_origin,'token':'qa-mobile-source-fixture'}}))
    config.chmod(0o600)
    store=Store(state/'hub.sqlite')
    entry={k:v for k,v in binding.items() if k!='project'}
    entry.update(host='QA mobile host',window='QA Managed Feedback',window_id=target['window_id'],
                 session_id='synthetic-provider-session',terminal_session_id=target['session_id'],
                 generation=target['process_generation'],journal_id=target['journal_id'],
                 event_id='managed-ready',status='blocked',summary='Synthetic decision awaiting explicit feedback.')
    store.report_step(PROJECT,'qa-source',entry)
    store.report_step(PROJECT,'qa-native',dict(entry,attempt_id='native-B',role='verify',tool='shell',
        window='QA Native Draft',window_id=window.id,terminal_session_id=native.id,generation=native.process_generation,
        event_id='native-ready',journal_id=native.journal_id,summary='Synthetic ordinary terminal; prepare a draft.'))
    grants=[]
    for token,subject,scopes in [('qa-mobile-read-only-fixture','reader',['tasks:read','usage:read']),
            ('qa-mobile-human-confirm','alice',['tasks:read','feedback:confirm']),
            ('qa-mobile-source-fixture','qa-source',['feedback:source'])]:
        grants.append(dict(sha256=token_digest(token),subject=subject,project=PROJECT,scopes=scopes,
            feedback_hosts=[INSTALLATION],accounts=['qa-seat']))
    auth=state/'grants.json';auth.write_text(json.dumps({'grants':grants}));auth.chmod(0o600)
    origins=[f'http://{RELAY}',source_origin]
    app=create_app(store,auth,hosts=[HUB],origins=origins,feedback_enabled=True)
    hub=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=18824,log_level='critical',access_log=False))
    relay=Relay(development=True,allowed_origins=origins)
    metrics={'raw_input_frames':0,'resize_frames':0,'submit_requests':0,'status_requests':0}
    original=daemon._op
    async def observe(client,op,payload):
        result=await original(client,op,payload)
        if op in {4,17}:metrics['raw_input_frames']+=1
        if op==5:metrics['resize_frames']+=1
        if op==OP_JOURNAL:
            action=json.loads(payload).get('action')
            if action=='submit_cockpit_feedback':
                metrics['submit_requests']+=1
                if drop_first_ack and metrics['submit_requests']==1:
                    return [],None
            if action=='cockpit_feedback_status':metrics['status_requests']+=1
        return result
    daemon._op=observe
    async def http(connection,request):
        if urlsplit(request.path).path=='/fixture/metrics':
            data=json.dumps(dict(metrics,provider_dispatches=sum(len(d.sent) for d in drivers),
                                 native_pty_bytes=len(native.pty.written))).encode()
            return Response(200,'OK',Headers({'Content-Type':'application/json','Content-Length':str(len(data))}),data)
        return await relay.serve_page(connection,request)
    jobs=[]
    try:
        async with serve(relay.handler,'127.0.0.1',18825,process_request=http):
            jobs=[asyncio.create_task(hub.serve()),asyncio.create_task(relay_uplink(daemon,f'ws://{RELAY}/ws','qa-room','qa'))]
            async with asyncio.timeout(8):
                while not hub.started or not relay.rooms.get('qa-room') or not relay.rooms['qa-room'].host:
                    await asyncio.sleep(.04)
            print('READY qa-mobile/avd-task hub=18824 relay=18825 stop='+str(state/'stop-request'),flush=True)
            while not (state/'stop-request').exists():
                if any(job.done() for job in jobs):
                    raise RuntimeError('fixture server exited')
                await asyncio.sleep(.25)
    finally:
        hub.should_exit=True
        for task in jobs:task.cancel()
        await asyncio.gather(*jobs,return_exceptions=True)
        for wid in list(daemon.hub.windows):daemon.hub.close_window(wid)
        await daemon.close_sidecars()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--drop-first-ack',action='store_true')
    options=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='agent-hub-avd-') as tmp:
        state=Path(tmp)
        with patch('supermonoagent.daemon.STATE',state),patch.dict(os.environ,{'SUPERMONO_STATE_DIR':str(state)}):
            asyncio.run(fixture(state,options.drop_first_ack))
