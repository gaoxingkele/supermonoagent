"""Desktop cockpit confirmation using an isolated real daemon and a synthetic model."""
import asyncio
import json
import hashlib
import os
import sys
from pathlib import Path
from unittest.mock import patch
import pytest

from websockets.asyncio.server import serve

from supermonoagent.managed_conversation import ManagedConversationService
from supermonoagent.protocol import OP_JOURNAL
from test_client_integration import isolated_host
from test_managed_conversation import FakeDriver
from test_workspace_profile_rooms import EDGE, run


def test_cockpit_confirm_and_lost_receipt_use_bound_source_once():
    hub_root=Path(os.environ.get('MYLIB_AGENT_HUB_ROOT',Path(__file__).resolve().parents[5]/'mylib/tools/agent-hub'))
    if not (hub_root/'agent_hub/feedback.py').is_file():
        pytest.skip('Provide MYLIB_AGENT_HUB_ROOT for the cross-repository integration')
    with patch.object(sys,'path',[str(hub_root),*sys.path]):
        run(check_browser)


async def check_browser(playwright):
    from agent_hub.feedback import Confirmations
    from agent_hub.storage import Store
    async with isolated_host() as (daemon, _direct, _relay):
        manager = daemon.journals
        await daemon.managed.aclose()
        drivers = []
        def factory(*args, **kwargs):
            drivers.append(FakeDriver(*args, **kwargs))
            return drivers[-1]
        service = ManagedConversationService(daemon.hub, manager,
            credential_authorized=daemon._managed_credential_authorized, driver_factory=factory)
        daemon.managed = daemon.hub.managed_service = manager.managed_sources = service
        native = daemon.hub.open_window('Same title', owner=daemon.token)
        anchor = daemon.hub.open_cli(native.id,'cmd')
        owner = manager.owner_scope(daemon.token, default_token=daemon.token, accounts=daemon.accounts)
        created = await service.handle('create_managed',
            dict(session_id=anchor.id, process_generation=anchor.process_generation, provider='codex', nonce='test-create'),
            owner_scope=owner, owner_token=daemon.token)
        target = {k:created[k] for k in ('window_id','session_id','process_generation','journal_id')}
        feedback = dict(project='demo',task_id='task',attempt_id='attempt',tool='codex',account='fixture',role='primary',
                        host_installation_id=manager.store.host_installation_id)
        config = daemon.log_state_dir / 'cockpit-feedback.json'
        entries = [
            dict(feedback, host='host-A',window='Same title',window_id=target['window_id'],
                 terminal_session_id=target['session_id'],generation=target['process_generation'],
                 journal_id=target['journal_id'],role='primary',summary='Managed fixture',skills=['sample'],tool_version='1.0'),
            dict(feedback, host='host-A',window='Same title',window_id=native.id,tool='shell',
                 attempt_id='verify',terminal_session_id=anchor.id,generation=anchor.process_generation,
                 role='verify',summary='Native fixture')
        ]
        store=Store(daemon.log_state_dir/'fixture-hub.sqlite3')
        event={k:v for k,v in entries[0].items() if k!='project'}
        rec=store.report_step('demo','hook-A',dict(event,event_id='fixture-step',status='reported'))
        entries[0]['seq']=rec['seq']
        hash_token=lambda text:hashlib.sha256(text.encode()).hexdigest()
        class Grants:
            def resolve(self,digest,project):
                for token,subject,scopes in (
                    ('human-pilot-fixture','alice',['tasks:read','feedback:confirm']),
                    ('other-device-fixture','alice',['tasks:read','feedback:confirm']),
                    ('source-pilot-fixture','host',['feedback:source'])):
                    if digest==hash_token(token) and project=='demo':
                        return dict(subject=subject,project=project,accounts=['fixture'],
                            feedback_hosts=[manager.store.host_installation_id],scopes=scopes)
                raise PermissionError()
        ledger=Confirmations(store,Grants())
        changes=[]
        held = []
        original = daemon._op
        async def drop_acceptance(client, op, payload):
            result = await original(client, op, payload)
            if op == OP_JOURNAL and json.loads(payload).get('action')=='submit_cockpit_feedback':
                held.append(json.loads(payload)['params']['nonce'])
                return [], None
            return result
        daemon._op = drop_acceptance

        async def hub(ws):
            if ws.request.path.endswith('/hub/feedback'):
                try:
                    req=json.loads(await ws.recv())
                    if req['op'] in {'evolution_preview','evolution_confirm'}:
                        assert req['token']=='evolution-human-fixture'
                        v=req['params']
                        assert v['target']==dict(project='demo',task_id='task',attempt_id='attempt',generation=target['process_generation'])
                        out=dict(target=dict(v['target'],**{k:entries[0][k] for k in ('host','host_installation_id','window_id','tool','tool_version','role')}),
                                 skill_id='sample',asset=dict(kind='skill',path='sample.md',sha256='a'*64,version='b'*64,revision='fixture'),state='preview')
                        if req['op']=='evolution_confirm':
                            assert v['version']=='b'*64 and v['asset_sha256']=='a'*64
                            changes.append(v['request_id']);out['state']='confirmed'
                    else:
                        out=ledger.request(hash_token(req['token']),req['op'],req['params'])
                    await ws.send(json.dumps({'ok':True,'result':out}))
                except (PermissionError,ValueError):
                    await ws.close(code=1008)
                return
            await ws.recv()
            await ws.send(json.dumps({'op':'ready','project':'demo','projects':['demo']}))
            async for raw in ws:
                req = json.loads(raw)
                if req['tool']=='list_tasks':
                    result = {'tasks':[{'project':'demo','task_id':'task','status':'blocked','summary':'Fixture'}]}
                elif req['tool']=='get_task_status':
                    result = {'task_id':'task','primary_attempt_id':'attempt','status':'blocked','attempts':entries}
                elif req['tool']=='list_evolution':
                    result={'items':[dict(project='demo',tool='codex',tool_version='1.0',skill_id='sample',
                        published_version='b'*64,candidate_id='fixture-candidate')]}
                elif req['tool']=='get_evolution_status':
                    result={'has_update':True,'published_version':'b'*64,'loaded_version':'c'*64,
                        'loading_state':'loaded','assessment':{'evaluator':'fixture','splits':{}}}
                else:
                    result = {}
                await ws.send(json.dumps({'op':'response','id':req['id'],'ok':True,'result':result}))
        async with serve(hub, '127.0.0.1',0) as reader, serve(daemon.handle_socket,'127.0.0.1',0,process_request=daemon.serve_http) as host:
            config.write_text(json.dumps({'bindings':[{**feedback, **target, 'owner_scope':owner}],
                'authority':{'token':'source-pilot-fixture','origin':'https://source.example',
                             'endpoint':f'ws://127.0.0.1:{reader.sockets[0].getsockname()[1]}/hub/feedback'}}),encoding='utf-8')
            config.chmod(0o600)
            async with playwright.async_playwright() as runtime:
                browser = await runtime.chromium.launch(executable_path=str(EDGE),headless=True)
                errors=[]
                async def open_task():
                    tab=await browser.new_page(viewport={'width':1280,'height':800})
                    tab.on('pageerror',lambda error: errors.append(str(error)))
                    await tab.goto(f'http://127.0.0.1:{host.sockets[0].getsockname()[1]}/workspace?direct=1#token={daemon.token}')
                    await tab.wait_for_function('smaWorkspace?.activePane?.ready')
                    await tab.evaluate('sid=>smaWorkspace.openSession(sid,false)',target['session_id'])
                    await tab.wait_for_function('smaWorkspace.managed.captureTarget(smaWorkspace.activePane)')
                    await tab.locator('#app-menu-toggle').click()
                    await tab.locator('#cockpit-open').click()
                    cockpit=tab.locator('#sma-cockpit')
                    await cockpit.get_by_label('驾驶舱服务地址').fill(f'ws://127.0.0.1:{reader.sockets[0].getsockname()[1]}/hub/view')
                    await cockpit.get_by_label('项目',exact=True).fill('demo')
                    await cockpit.get_by_label('只读访问凭据').fill('synthetic-only')
                    await cockpit.get_by_role('button',name='连接项目').click()
                    await cockpit.get_by_role('button',name='待处理',exact=True).click()
                    await cockpit.get_by_role('button',name='查看任务').click()
                    return tab,cockpit
                try:
                    page,panel=await open_task()
                    launch=panel.get_by_role('button',name='反馈到此执行')
                    await launch.first.click()
                    dialog=page.locator('.cockpit-feedback-dialog')
                    await dialog.get_by_label('反馈全文').fill('unsent')
                    await dialog.get_by_role('button',name='关闭',exact=True).click()
                    assert not drivers[0].sent
                    await launch.first.click()
                    await dialog.get_by_label('反馈全文').fill('approved synthetic text')
                    await dialog.get_by_label('窗口反馈授权').fill('human-pilot-fixture')
                    await dialog.get_by_role('button',name='确认发送到',exact=False).click()
                    await dialog.get_by_text('发送结果尚未确认，请核对原请求。',exact=True).wait_for(timeout=15000)
                    await dialog.get_by_role('button',name='关闭',exact=True).click()
                    other_page,other_panel=await open_task()
                    await other_panel.get_by_role('button',name='反馈到此执行').first.click()
                    other_dialog=other_page.locator('.cockpit-feedback-dialog')
                    await other_dialog.get_by_label('窗口反馈授权').fill('other-device-fixture')
                    await other_dialog.get_by_role('button',name='核对原请求').click()
                    await other_dialog.get_by_text('目标已持久接收反馈。',exact=True).wait_for()
                    await other_page.close()
                    assert len(drivers[0].sent)==len(held)==1
                    await launch.last.click()
                    await dialog.get_by_label('反馈全文').fill('review this native draft')
                    await dialog.get_by_role('button',name='准备草稿').click()
                    await page.locator('#terminal-composer[open]').wait_for()
                    assert await page.locator('#terminal-composer-text').input_value()=='review this native draft'
                    assert not anchor.pty.written
                    await page.locator('#terminal-composer-close').click()
                    await page.locator('#cockpit-shortcut').click()
                    await panel.get_by_role('button',name='待处理',exact=True).click()
                    await panel.get_by_role('button',name='查看任务').click()
                    await panel.get_by_role('button',name='此执行的 mylib 版本').click()
                    await panel.get_by_role('button',name='查看版本证据').click()
                    await panel.get_by_role('button',name='申请应用到此执行').click()
                    evo=page.locator('.cockpit-evolution-confirm')
                    await evo.get_by_label('版本变更授权').fill('evolution-human-fixture')
                    await evo.get_by_role('button',name='预览目标版本').click()
                    await evo.get_by_text('请核对目标与版本后确认。').wait_for()
                    assert not changes
                    await evo.get_by_role('button',name='确认应用到',exact=False).click()
                    await evo.get_by_text('已登记版本变更，请核对宿主加载状态。').wait_for()
                    assert len(changes)==1
                    assert len(drivers[0].sent)==1 and not anchor.pty.written
                    assert not errors
                finally:
                    await browser.close()
