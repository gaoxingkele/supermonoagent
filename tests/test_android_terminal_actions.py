"""Test the regular Android workspace management buttons with controlled source catalogs."""
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import quote

import pytest
from websockets.asyncio.server import serve

from android_journal_fixture import Fixture, ROOM, TOKEN, packed
from test_workspace_profile_rooms import EDGE, run


class ActionFixture(Fixture):
    def __init__(self,guarded):
        super().__init__()
        self.guarded=guarded
        self.title='同名窗口'
        self.generation='generation-1'
        self.seen=[]
        self.commands=asyncio.Queue()
        self.removed=False

    def catalog(self):
        value=super().catalog()
        value['capabilities'].pop('conversation_journal',None)
        if self.guarded:value['capabilities']['close_ack']=1
        w1,w2=value['windows']
        w2['title']='同名窗口'
        w1['title']=self.title
        w1['sessions'].append(dict(id='sibling',shell='powershell'))
        if self.guarded:
            for s in w1['sessions']:s['process_generation']=self.generation if s['id']=='s1' else 'sibling-pid'
        if self.removed:w1['sessions']=[s for s in w1['sessions'] if s['id']!='s1']
        return value

    async def socket(self,ws):
        owner=self
        class Capture:
            def __getattr__(self,key):return getattr(ws,key)
            def __aiter__(self):
                async def messages():
                    async for frame in ws:
                        if isinstance(frame,bytes) and len(frame)>=5 and frame[4] in (9,15):
                            op,payload=frame[4],json.loads(frame[5:])
                            owner.seen.append((op,payload))
                            await owner.commands.put((ws,op,payload))
                        else:yield frame
                return messages()
        await super().socket(Capture())


@pytest.mark.parametrize('guarded',[False,True],ids=['legacy-catalog','process-exit-ack'])
def test_android_native_window_actions_use_selected_session_only(guarded):
    run(lambda pw: check_actions(pw,guarded))


async def check_actions(pw,guarded):
    source=ActionFixture(guarded)
    async with serve(source.socket,'127.0.0.1',0,process_request=source.http) as svc:
        source.origin=f'http://127.0.0.1:{svc.sockets[0].getsockname()[1]}'
        async with pw.async_playwright() as runtime:
            browser=await runtime.chromium.launch(executable_path=str(EDGE),headless=True)
            try:
                page=await browser.new_page(viewport={'width':390,'height':844},is_mobile=True)
                page.set_default_timeout(5000)
                errors=[]
                page.on('pageerror',lambda err:errors.append(str(err)))
                config=dict(endpoint=source.origin,code=ROOM,token=TOKEN,renderer='dom')
                await page.goto(source.origin+'/mobile/index.html#'+quote(json.dumps(config),safe=''))
                await page.wait_for_function("window.MobileTerminal?.ready&&MobileTerminal.active==='s1'")
                await page.locator('#sessions').click()
                dialog=page.locator('#terminal-action-dialog')
                confirm=dialog.locator('#terminal-action-confirm')
                cancel=dialog.locator('#terminal-action-cancel')
                rename=page.locator('#rename-current-window')
                close=page.locator('#close-current-process')
                await rename.click()
                name=dialog.get_by_label('新窗口名称')
                assert await name.input_value()=='同名窗口'
                artifacts=os.environ.get('SMA_ANDROID_ACTIONS_VISUAL_DIR')
                if artifacts and guarded:
                    directory=Path(artifacts);directory.mkdir(parents=True,exist_ok=True)
                    await page.screenshot(path=str(directory/'rename-dialog.png'))
                await name.fill('未提交')
                await cancel.click()
                await close.click()
                assert 's1' in await dialog.locator('#terminal-action-target').inner_text()
                assert await dialog.evaluate('el=>el.scrollWidth<=el.clientWidth+1&&el.getBoundingClientRect().right<=innerWidth+1')
                if artifacts and guarded:await page.screenshot(path=str(directory/'end-process-dialog.png'))
                await cancel.click()
                assert source.seen==[]

                await rename.click()
                await name.fill('测试改名')
                await confirm.click()
                ws,op,args=await asyncio.wait_for(source.commands.get(),2)
                assert (op,args)==(15,dict(action='title',window='w1',title='测试改名'))
                assert not await dialog.get_by_text('主机已确认窗口名称',exact=False).count()
                assert await confirm.is_disabled()
                source.title='测试改名'
                await ws.send(packed(7,source.catalog()))
                await dialog.get_by_text('主机已确认窗口名称：测试改名',exact=True).wait_for()
                assert '测试改名' in await page.locator('#title').inner_text()
                await cancel.click()

                if guarded:
                    await close.click()
                    source.generation='replaced'
                    await ws.send(packed(7,source.catalog()))
                    await dialog.get_by_text('窗口或终端身份已变化',exact=False).wait_for()
                    assert await confirm.is_disabled() and len(source.seen)==1
                    await cancel.click()
                await close.click()
                await confirm.click()
                ws,op,args=await asyncio.wait_for(source.commands.get(),2)
                assert op==9 and args['session']=='s1' and 'window' not in args
                assert not source.removed and await confirm.is_disabled()
                if guarded:
                    assert set(args)=={'session','request','process_generation','window_id'}
                    assert args['window_id']=='w1' and args['process_generation']=='replaced'
                    ack={**args,'closed':False,'error':'process_still_alive'}
                    await ws.send(packed(2,{**source.catalog(),'close_ack':{**ack,'request':'wrong-request'}}))
                    await asyncio.sleep(.05)
                    assert '等待主机确认' in await dialog.inner_text()
                    await ws.send(packed(2,{**source.catalog(),'close_ack':ack}))
                    await dialog.get_by_text('主机尚未确认进程退出',exact=False).wait_for()
                    await cancel.click()
                    await close.click();await confirm.click()
                    ws,op,new_args=await asyncio.wait_for(source.commands.get(),2)
                    assert new_args['request']!=args['request']
                    args=new_args
                else:
                    assert args=={'session':'s1'}
                source.removed=True
                await ws.send(packed(7,source.catalog()))
                if guarded:
                    assert '主机已确认结束' not in await dialog.inner_text()
                    await ws.send(packed(2,{**source.catalog(),'close_ack':{**args,'closed':True}}))
                    await dialog.get_by_text('主机已确认结束选定终端进程。',exact=True).wait_for()
                else:
                    await dialog.get_by_text('主机目录已移除选定终端。',exact=True).wait_for()
                await cancel.click()
                assert await page.locator('#session-list [data-session="sibling"]').count()==1
                assert await page.locator('#session-list [data-session="s2"]').count()==1
                assert not source.input_frames and not errors
            finally:
                await browser.close()


def test_android_managed_end_button_confirms_before_scoped_close_and_handles_offline():
    run(check_managed_close)


async def check_managed_close(pw):
    from test_android_managed import ManagedFixture
    fixture=ManagedFixture()
    fixture.created=dict(window_id='managed-window',session_id='managed-session',process_generation='managed-generation',
                         journal_id='managed-journal',interaction_mode='managed',provider='claude',state='ready',cwd='D:\\synthetic')
    original=fixture.query
    closes=[]
    def record(action,params):
        if action=='close_managed':closes.append(dict(params))
        return original(action,params)
    fixture.query=record
    async with serve(fixture.socket,'127.0.0.1',0,process_request=fixture.http) as server:
        fixture.origin=f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        async with pw.async_playwright() as runtime:
            browser=await runtime.chromium.launch(executable_path=str(EDGE),headless=True)
            try:
                page=await browser.new_page(viewport={'width':390,'height':844},is_mobile=True);page.set_default_timeout(5000)
                await page.add_init_script('''window.observedCloses=[];const send=WebSocket.prototype.send;
                    WebSocket.prototype.send=function(frame){if(typeof frame!=='string'&&new Uint8Array(frame)[4]===9)observedCloses.push(9);return send.call(this,frame);};''')
                cfg=dict(endpoint=fixture.origin,code=ROOM,token=TOKEN,renderer='dom')
                await page.goto(fixture.origin+'/mobile/index.html#'+quote(json.dumps(cfg),safe=''))
                await page.wait_for_function('window.MobileTerminal?.ready')
                await page.locator('#sessions').click()
                await page.locator('#session-list [data-session="managed-session"]').click()
                await page.wait_for_function("MobileTerminal.active==='managed-session'&&!document.querySelector('#managed-close').disabled")
                await page.locator('#managed-close').click()
                dialog=page.locator('#terminal-action-dialog')
                await dialog.get_by_role('button',name='取消').click()
                assert closes==[] and await page.evaluate('observedCloses')==[]
                await page.locator('#managed-close').click()
                await page.evaluate('MobileTerminal.pause()')
                assert await dialog.locator('#terminal-action-confirm').is_disabled()
                assert closes==[]
                await dialog.get_by_role('button',name='关闭').click()
                await page.evaluate('MobileTerminal.resume()')
                await page.wait_for_function('MobileTerminal.ready&&!document.querySelector("#managed-close").disabled')
                await page.locator('#managed-close').click()
                await dialog.get_by_role('button',name='确认结束进程').click()
                await dialog.get_by_text('主机已关闭所选独立会话。',exact=True).wait_for()
                assert len(closes)==1
                assert all(closes[0][k]==fixture.created[k] for k in ('session_id','window_id','journal_id','process_generation'))
                assert await page.evaluate('observedCloses')==[] and not fixture.input_frames
            finally: await browser.close()
