"""25-cell grid, observed activity sorting and touch paging; isolated relay only."""
import asyncio
import importlib.util
import json
import mimetypes
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import async_playwright
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

spec=importlib.util.spec_from_file_location('grid',Path(__file__).with_name('debug-grid-ui.py'))
grid=importlib.util.module_from_spec(spec);spec.loader.exec_module(grid)
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'.omx/artifacts/android-debug-grid25'


async def main():
    clients,operations,errors=[],[],[]
    windows={pc:[{'id':'w'+str(i),'title':pc+' window '+str(i),'sessions':[{'id':'s'+str(i),'agent':'codex'}]}
                 for i in range(count)] for pc,count in [('pc1',30),('pc2',25)]}

    async def socket(ws):
        client={'ws':ws,'pc':None,'sub':set(),'history':False};clients.append(client)
        try:
            async for message in ws:
                if isinstance(message,str):
                    value=json.loads(message)
                    if value['op']=='phone':client['pc']=value['code'];await ws.send(json.dumps({'op':'ready'}))
                    elif value['op']=='watch':await ws.send(json.dumps({'op':'directory','revision':1,'hosts':[]}))
                    continue
                op,data=message[4],message[5:];operations.append(op)
                if op==1:
                    client['history']=json.loads(data).get('history_context')==1
                    await ws.send(grid.frame(2,state(client)))
                elif op==16:
                    wanted=set(json.loads(data)['sessions']);assert len(wanted)<=256
                    for sid in wanted-client['sub']:
                        await ws.send(vt(client,8,sid,'\x1b[2J\x1b[H'+client['pc']+' '+sid+'\x1b[4;1H提示：正在等待下一条指令。'))
                    client['sub']=wanted;await ws.send(grid.frame(7,state(client)))
                elif op==10:await ws.send(grid.frame(11,data))
        finally:
            clients.remove(client)

    def state(client):
        return {'capabilities':{'multiplex':True,'snapshot_sgr':True,'history_context':1},'machine':client['pc'],
                'windows':windows[client['pc']],
                'controls':{sid:{'cols':158,'rows':43,'owner':False,'history_epoch':0} for sid in client['sub']}}

    def vt(client,op,sid,text):
        if not client['history']:return grid.feed(op,sid,text)
        metadata=json.dumps({'v':1,'kind':'snapshot' if op==8 else 'output','cols':158,'rows':43,
                             'epoch':0,'partial':False,'reason':''},separators=(',',':')).encode()
        return grid.frame(23,sid.encode()+b'\0'+len(metadata).to_bytes(2,'little')+metadata+text.encode())

    async def assets(connection,request):
        path=request.path.split('?')[0]
        if not path.startswith('/mobile/'):return None
        name=path[len('/mobile/'):]
        base=ROOT/('src/supermonoagent/static' if name.startswith('vendor/') else 'android/assets/mobile')
        file=(base/name.removeprefix('vendor/')).resolve()
        if not file.is_relative_to(base) or not file.is_file():return Response(404,'Not Found',Headers(),b'')
        return Response(200,'OK',Headers({'Content-Type':mimetypes.guess_type(name)[0] or 'application/octet-stream'}),file.read_bytes())

    async with serve(socket,'127.0.0.1',0,process_request=assets) as server:
        endpoint='http://127.0.0.1:'+str(server.sockets[0].getsockname()[1])
        async with async_playwright() as runtime:
            browser=await runtime.chromium.launch(channel='msedge')
            try:
                context=await browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
                await context.add_init_script('localStorage.setItem("sma.mobile.hosts",'+json.dumps(json.dumps([{'id':'pc2','name':'pc2','endpoint':endpoint,'code':'pc2'}]))+');')
                page=await context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                config={'endpoint':endpoint,'code':'pc1','token':'fixture-only','renderer':'dom'}
                await page.goto(endpoint+'/mobile/index.html#'+quote(json.dumps(config)))
                await grid.wait(page,'MobileTerminal.ready&&MobileTerminal.hosts.every(h=>h.connected)')
                baseline=await page.evaluate('MobileTerminal.activeKey')
                await page.evaluate("()=>{window.debugParsers=0;window.debugMaxParsers=0;const original=MobileCore.createDebugPreview;MobileCore.createDebugPreview=options=>{debugParsers++;debugMaxParsers=Math.max(debugMaxParsers,debugParsers);const value=original(options),dispose=value.dispose;let dead=false;value.dispose=()=>{if(!dead){dead=true;debugParsers--;dispose();}};return value;};}")
                await page.locator('#debug').click();await page.select_option('#debug-density','25')
                await grid.wait(page,'document.querySelectorAll(".debug-tile").length===25')
                assert await page.locator('#debug-page').inner_text()=='1 / 3'
                for width,height in [(390,844),(280,740),(844,390),(1024,768)]:
                    await page.set_viewport_size({'width':width,'height':height});await asyncio.sleep(.2)
                    checks=await page.evaluate("""()=>[...document.querySelectorAll('.debug-tile')].map(e=>{const r=e.getBoundingClientRect(),p=e.querySelector('.debug-marquee').getBoundingClientRect();return Math.abs(r.width-r.height)<1&&p.top>=r.top&&p.bottom<=r.bottom+1})""")
                    OUT.mkdir(parents=True,exist_ok=True);await page.screenshot(path=OUT/(str(width)+'x'+str(height)+'.png'))
                    assert all(checks),(width,height,checks)
                await page.set_viewport_size({'width':390,'height':844})
                touch=await page.context.new_cdp_session(page)

                async def swipe(dx,dy=0,cancel=False):
                    box=await page.locator('#debug-grid').bounding_box();x=box['x']+box['width']*.7;y=box['y']+box['height']*.5
                    await touch.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':x,'y':y}]})
                    for step in range(1,5):await touch.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':x+dx*step/4,'y':y+dy*step/4}]})
                    await touch.send('Input.dispatchTouchEvent',{'type':'touchCancel' if cancel else 'touchEnd','touchPoints':[]})
                    await asyncio.sleep(.2)

                await swipe(-150);assert await page.locator('#debug-page').inner_text()=='2 / 3'
                await swipe(-150);assert await page.locator('#debug-page').inner_text()=='3 / 3'
                await swipe(-150);assert await page.locator('#debug-page').inner_text()=='3 / 3'
                await swipe(150);assert await page.locator('#debug-page').inner_text()=='2 / 3'
                await swipe(0,-100);assert await page.locator('#debug-page').inner_text()=='2 / 3'
                await swipe(-20);assert await page.locator('#debug-page').inner_text()=='2 / 3'
                assert await page.evaluate('MobileTerminal.activeKey')==baseline
                assert await page.locator('#debug-dialog').evaluate('e=>e.open')
                await page.locator('#debug-prev').click();await asyncio.sleep(.6)

                async def update(pc,sid,text='live output',recovery=False):
                    for client in list(clients):
                        if client['pc']==pc and sid in client['sub']:await client['ws'].send(grid.feed(3,sid,text) if recovery else vt(client,3,sid,text))

                await update('pc2','s24')
                await grid.wait(page,'document.querySelector(".debug-tile-name").textContent==="pc2 window 24"')
                await update('pc1','s2');await asyncio.sleep(.1);await update('pc1','s3')
                await asyncio.sleep(2.2)
                names=await page.locator('.debug-tile-name').all_text_contents()
                assert names[:3]==['pc1 window 3','pc1 window 2','pc2 window 24'],names[:3]
                footer='\x1b]7501;app=codex\x07\x1b7\x1b[?6lContext 50% Weekly 70%\x1b[0m\x1b8'
                await update('pc2','s24',footer,recovery=True)
                await update('pc1','s0',footer,recovery=True)
                await asyncio.sleep(1.2);assert (await page.locator('.debug-tile-name').all_text_contents())[:3]==names[:3]
                box=await page.locator('#debug-grid').bounding_box();point={'x':box['x']+10,'y':box['y']+10}
                await touch.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[point]})
                await update('pc1','s29');await asyncio.sleep(1.2)
                assert await page.locator('.debug-tile-name').first.inner_text()=='pc1 window 3','Cards moved under a pressed finger'
                await touch.send('Input.dispatchTouchEvent',{'type':'touchCancel','touchPoints':[]})
                await grid.wait(page,'document.querySelector(".debug-tile-name").textContent==="pc1 window 29"')
                await touch.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[point]})
                await page.evaluate('MobileTerminal.pause()')
                async with asyncio.timeout(5):
                    while clients:await asyncio.sleep(.05)
                await page.evaluate('MobileTerminal.resume()')
                await grid.wait(page,'MobileTerminal.ready&&document.querySelectorAll(".debug-tile").length===25')
                async with asyncio.timeout(5):
                    while not any(client['pc']=='pc2' and len(client['sub'])==25 for client in clients):await asyncio.sleep(.05)
                await update('pc2','s23')
                await grid.wait(page,'document.querySelector(".debug-tile-name").textContent==="pc2 window 23"')
                assert await page.evaluate('debugMaxParsers')<=25
                await page.locator('#debug-close').click();await asyncio.sleep(.3)
                assert await page.evaluate('debugParsers')==0
                assert all(len(client['sub'])<=1 for client in clients),'Activity or preview connections leaked'
                assert await page.evaluate('MobileTerminal.activeKey')==baseline
                assert all(op in [1,10,16] for op in operations),operations
                assert not errors,errors
                report={'passed':True,'layouts_25':4,'targets':55,'offpage_activity_sorted':True,
                        'idle_updates_descending':True,'recovery_not_activity':True,'context_23':True,'swipe_paging':True,
                        'pressed_card_pinned':True,'pressed_pause_resume_recovers_sort':True,
                        'parser_limit':25,'readonly':True,'connections_released':True}
                (OUT/'verification.json').write_text(json.dumps(report,indent=2),'utf-8');print(json.dumps(report))
            finally:
                await browser.close()


if __name__=='__main__':asyncio.run(main())
