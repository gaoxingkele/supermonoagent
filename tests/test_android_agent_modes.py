"""Android-owned agent profiles and actual mobile/xterm mode selection, with isolated PTYs."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from unittest.mock import patch
from urllib.parse import parse_qs,quote,urlsplit

import pytest
from websockets.asyncio.server import serve
from supermonoagent.daemon import Daemon,relay_uplink
from supermonoagent.relay import Relay
from supermonoagent import web
from test_client_integration import ControlledPty

ROOT=Path(__file__).resolve().parents[1]
PROFILES=ROOT/'android/assets/mobile/agent-modes'
EDGE=Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
IDS=['codex','claude-code','grok','kimi','pi-dev','cursor-cli']
BRANDS={'codex':'Codex','claude-code':'Claude Code','grok':'Grok','kimi':'Kimi','pi-dev':'pi.dev','cursor-cli':'Cursor CLI'}
ROW='Codex Claude Code Grok Kimi pi.dev Cursor CLI'
PAPER_ROW='论文复核 src/paper.md 42 ms "证据" `holdout_once`'


def test_six_android_owned_profiles_match_shared_schema_and_are_independent():
    node=shutil.which('node')
    if not node:pytest.skip('Node required for actual shared profile validator')
    script=r'''
const fs=require('fs'),assert=require('assert/strict'),C=require('./src/supermonoagent/static/terminal-client.js');
for(const {id} of C.agentProfiles){const raw=JSON.parse(fs.readFileSync('android/assets/mobile/agent-modes/'+id+'.json','utf8'));const p=C.validateAgentProfile(raw,id);assert.equal(p.id,id);assert.ok(p.tokens.length>=4);assert.ok(Object.isFrozen(p));}
assert.equal(C.agentModeFromSession({agent:'kimi'},'Codex workspace'),'kimi');
assert.equal(C.agentModeFromSession({shell:'powershell'},'ordinary cursor position'),'auto');
'''
    subprocess.run([node,'-e',script],cwd=ROOT,check=True,capture_output=True)
    assert {p.stem for p in PROFILES.glob('*.json')}==set(IDS)
    assert len({hashlib.sha256((PROFILES/(id+'.json')).read_bytes()).hexdigest() for id in IDS})==6
    for id in IDS:
        raw=json.loads((PROFILES/(id+'.json')).read_text('utf-8'))
        assert set(raw)=={'id','label','theme','tokens'} and raw['id']==id
        assert {'background','foreground','red','green','blue','brightWhite'}<=raw['theme'].keys()
        desktop=ROOT/'src/supermonoagent/static/agent-modes'/(id+'.json')
        if desktop.exists():assert desktop.read_bytes()!=(PROFILES/(id+'.json')).read_bytes()


def test_android_codex_covers_research_text_tokens_without_overriding_cli_styles():
    node=shutil.which('node')
    if not node:pytest.skip('Node required for real xterm profile coverage')
    script=r'''
const fs=require('fs'),assert=require('assert/strict'),C=require('./src/supermonoagent/static/terminal-client.js'),{Terminal}=require('./src/supermonoagent/static/xterm.js');
const raw=JSON.parse(fs.readFileSync('android/assets/mobile/agent-modes/codex.json','utf8'));
assert.deepEqual(raw.tokens.slice(0,4),[
 {pattern:'\\b(?:Codex|apply_patch)\\b',flags:'iu',color:'#aecfff'},
 {pattern:'\\b(?:Thinking|Reasoning|Planning)\\b',flags:'iu',color:'#dbc4ff'},
 {pattern:'\\b(?:Added|Updated|Created)\\b',flags:'iu',color:'#b2efbd'},
 {pattern:'^\\s*[›>]\\s',flags:'iu',color:'#ffe0a8'}
]);
const profile=C.validateAgentProfile(raw,'codex'),term=new Terminal({cols:180,rows:10,allowProposedApi:true});
const plain='论文复核：查看 src/paper.md，采用 "独立验证" 与 “证据边界”，标记 `holdout_once`，耗时 42 ms。';
const write=value=>new Promise(done=>term.write(value,done));
function column(line,needle){const target=line.translateToString(true).indexOf(needle);assert.ok(target>=0);let offset=0;for(let x=0;x<term.cols;x++){const cell=line.getCell(x);if(!cell?.getWidth())continue;if(offset===target)return x;offset+=cell.getChars().length;}throw Error('fixture column missing');}
(async()=>{try{
 await write(plain+'\r\n\x1b[2m'+plain+'\x1b[0m\r\n\x1b[38;2;12;34;56m'+plain+'\x1b[0m\r\n\x1b[32m'+plain+'\x1b[0m\r\n\x1b[48;2;45;55;65m'+plain+'\x1b[0m\r\n\x1b[7m'+plain+'\x1b[0m\r\n本文整理研究背景与方法，不包含代码标记。\r\n错误：独立验证失败；Warning: check evidence.\r\n## 研究设计与验证');
 const buffer=term.buffer.active,line=buffer.getLine(0),spans=C.highlightSpans(line,term.cols,profile);
 for(const token of ['src/paper.md','"独立验证"','“证据边界”','`holdout_once`','42 ms']){const x=column(line,token);assert.ok(spans.some(span=>span.x<=x&&span.x+span.width>x),'research token lacks highlighting: '+token);}
 for(let y=1;y<=5;y++)assert.deepEqual(C.highlightSpans(buffer.getLine(y),term.cols,profile),[],'native SGR row must be preserved');
 assert.deepEqual(C.highlightSpans(buffer.getLine(6),term.cols,profile),[],'ordinary prose is not painted as an entire paragraph');
 assert.ok(C.highlightSpans(buffer.getLine(7),term.cols,profile).some(span=>span.foregroundColor===raw.theme.red));
 const heading=C.highlightSpans(buffer.getLine(8),term.cols,profile);assert.equal(heading.length,1);assert.equal(heading[0].x,0);assert.equal(heading[0].width,3);
 assert.equal(line.translateToString(true),plain);assert.equal(buffer.getLine(2).getCell(0).getFgColor(),0x0c2238);assert.equal(!!buffer.getLine(1).getCell(0).isDim(),true);
}finally{term.dispose();}})().catch(error=>{process.stderr.write(error.message);process.exitCode=1;});
'''
    result=subprocess.run([node,'-e',script],cwd=ROOT,capture_output=True)
    assert result.returncode==0,result.stderr.decode('utf-8','replace')


@pytest.mark.parametrize('renderer',['dom','webgl'])
def test_actual_mobile_modes_scope_persistence_late_fetch_native_ansi_and_disposal(renderer):
    playwright=pytest.importorskip('playwright.async_api')
    if not EDGE.exists():pytest.skip('Hidden Chromium required for actual mobile/xterm rendering')
    asyncio.run(check_modes(playwright,renderer))


async def check_modes(playwright,renderer):
    with tempfile.TemporaryDirectory() as temporary,patch('supermonoagent.daemon.STATE',Path(temporary)),patch.dict(os.environ,{'SUPERMONO_STATE_DIR':temporary}):
        daemons=[];sessions=[];jobs=[];relay=Relay(development=True)
        for index in range(2):
            daemon=Daemon('mode-fixture-owner-'+str(index));daemon.hub.persist=False;daemon.hub.pty_factory=lambda *_:ControlledPty();daemon.hub.attach_loop(asyncio.get_running_loop())
            window=daemon.hub.open_window(owner=daemon.token,title=('Codex' if index==0 else 'Kimi')+' demo')
            with patch('supermonoagent.hub.secrets.token_hex',return_value='aabbccdd'):session=daemon.hub.open_cli(window.id,'cmd')
            session.pump.remember(('\x1b[H\x1b[2J'+ROW+'\r\n\x1b[38;2;12;34;56mCodex native RGB\x1b[0m\r\n\x1b[32mKimi native indexed\x1b[0m\r\n'+PAPER_ROW+'\r\n\x1b[2m'+PAPER_ROW+'\x1b[0m\r\n\x1b[48;2;45;55;65m'+PAPER_ROW+'\x1b[0m\r\n本文整理背景与方法，不包含代码标记。\r\n## 研究设计').encode())
            daemons.append(daemon);sessions.append(session)
        profile_requests=[]
        async def http(connection,request):
            path=request.path.split('?',1)[0]
            if path.startswith('/mobile/'):
                name=path[len('/mobile/'):];resource=ROOT/'src/supermonoagent/static'/name[7:] if name.startswith('vendor/') else ROOT/'android/assets/mobile'/name
                if resource.is_file():
                    if name.startswith('agent-modes/'):profile_requests.append(request.path)
                    mime={'.html':'text/html','.css':'text/css','.json':'application/json'}.get(resource.suffix,'text/javascript')
                    return web.text_response(connection,200,resource.read_text('utf-8'),mime+'; charset=utf-8')
            return await relay.serve_page(connection,request)
        try:
            async with serve(relay.handler,'127.0.0.1',0,process_request=http) as server:
                origin='http://127.0.0.1:'+str(server.sockets[0].getsockname()[1]);url=origin.replace('http:','ws:')+'/ws'
                for index,daemon in enumerate(daemons):jobs.append(asyncio.create_task(relay_uplink(daemon,url,'mode-room-'+str(index),str(index))))
                async with asyncio.timeout(5):
                    while sum(room.host is not None for room in relay.rooms.values())<2:await asyncio.sleep(.01)
                config={'endpoint':origin,'code':'mode-room-0','token':daemons[0].token,'renderer':renderer,'uiRevision':'wrong-fallback-revision'}
                document=origin+'/mobile/index.html?ui_revision=mobile-mode-fixture#'+quote(json.dumps(config))
                async with playwright.async_playwright() as runtime:
                    browser=await runtime.chromium.launch(executable_path=str(EDGE),headless=True)
                    try:
                        page=await browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
                        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                        await page.add_init_script('''try{localStorage.setItem('sma.mobile.sizing','shared');}catch{}
window.modeDecorations=[];window.disposedModeTerminals=[];window.outboundModeOps=[];
Object.defineProperty(window,'Terminal',{configurable:true,set(T){const decorate=T.prototype.registerDecoration,dispose=T.prototype.dispose;
 T.prototype.registerDecoration=function(options){const d=decorate.call(this,options);if(d)modeDecorations.push({term:this,decoration:d});return d;};
 T.prototype.dispose=function(){disposedModeTerminals.push(this);return dispose.call(this);};Object.defineProperty(window,'Terminal',{value:T,writable:true,configurable:true});}});
const send=WebSocket.prototype.send;WebSocket.prototype.send=function(value){if(typeof value!=='string'){const bytes=new Uint8Array(value);if(bytes.length>4)outboundModeOps.push(bytes[4]);}return send.call(this,value);};''')
                        await page.add_init_script('try{if(!localStorage.getItem("sma.mobile.hosts"))localStorage.setItem("sma.mobile.hosts",'+json.dumps(json.dumps([{'id':'host:other','name':'Other fixture','endpoint':origin,'code':'mode-room-1'}]))+');}catch{}')
                        await page.goto(document);await page.wait_for_function('MobileTerminal.ready && document.getElementById("screen").dataset.agentMode==="codex"')
                        assert await page.locator('#render-status').text_content()=='实际渲染：'+renderer.upper()
                        assert await page.locator('#agent-mode').input_value()=='auto'
                        assert await page.locator('#agent-mode option').evaluate_all('elements=>elements.map(e=>e.value)')==['auto',*IDS]
                        evidence=[]
                        before=await page.evaluate('({text:MobileTerminal.terminal.buffer.active.getLine(0).translateToString(true),writes:MobileTerminal.metrics.writes,bad:outboundModeOps.filter(op=>op===5||op===17).length})')
                        for id in IDS:
                            await choose_mode(page,id)
                            profile=json.loads((PROFILES/(id+'.json')).read_text('utf-8'));brand=BRANDS[id];color=profile['tokens'][0]['color'];start=ROW.index(brand)
                            await page.wait_for_function('arg=>modeDecorations.some(e=>e.term===MobileTerminal.terminal&&!e.decoration.isDisposed&&e.decoration.marker.line===0&&e.decoration.options.x<=arg.x&&e.decoration.options.x+e.decoration.options.width>arg.x&&e.decoration.options.foregroundColor===arg.color)',arg={'x':start,'color':color})
                            cells=await page.evaluate('''()=>{const t=MobileTerminal.terminal,b=t.buffer.active;return {text:b.getLine(0).translateToString(true),defaultForeground:b.getLine(0).getCell(0).isFgDefault(),nativeRGB:b.getLine(1).getCell(0).getFgColor(),nativeIndexed:b.getLine(2).getCell(0).getFgColor(),background:t.options.theme.background,foreground:t.options.theme.foreground,writes:MobileTerminal.metrics.writes,bad:outboundModeOps.filter(op=>op===5||op===17).length};}''')
                            assert cells['text']==ROW and cells['defaultForeground'];assert cells['nativeRGB']==0x0c2238 and cells['nativeIndexed']==2
                            assert cells['background']==profile['theme']['background'] and cells['foreground']==profile['theme']['foreground']
                            assert cells['writes']==before['writes'] and cells['bad']==before['bad']
                            assert not await page.evaluate('modeDecorations.some(e=>e.term===MobileTerminal.terminal&&!e.decoration.isDisposed&&[1,2].includes(e.decoration.marker.line))')
                            if id=='codex':
                                await page.wait_for_function('modeDecorations.some(e=>e.term===MobileTerminal.terminal&&!e.decoration.isDisposed&&e.decoration.marker.line===3)')
                                assert not await page.evaluate('modeDecorations.some(e=>e.term===MobileTerminal.terminal&&!e.decoration.isDisposed&&[4,5,6].includes(e.decoration.marker.line))')
                                research=await page.evaluate('''()=>{const b=MobileTerminal.terminal.buffer.active;return {plain:b.getLine(3).translateToString(true),dim:!!b.getLine(4).getCell(0).isDim(),nativeBackground:b.getLine(5).getCell(0).getBgColor(),heading:modeDecorations.filter(e=>e.term===MobileTerminal.terminal&&!e.decoration.isDisposed&&e.decoration.marker.line===7).map(e=>({x:e.decoration.options.x,width:e.decoration.options.width}))};}''')
                                assert research['plain']==PAPER_ROW and research['dim'] and research['nativeBackground']==0x2d3741
                                assert research['heading']==[{'x':0,'width':3}]
                            await page.locator('#settings-close').click();artifacts=ROOT/'.omx/artifacts/android-agent-modes';artifacts.mkdir(parents=True,exist_ok=True)
                            await page.screenshot(path=str(artifacts/(renderer+'-'+id+'.png')));evidence.append({'mode':id,**cells})
                        # Auto follows explicit runtime agent metadata, then falls back to the title.
                        await choose_mode(page,'auto',effective='codex');await page.screenshot(path=str(artifacts/(renderer+'-selector.png')));await page.locator('#settings-close').click()
                        sessions[0].pty.inbox.put(b'\x1b]7501;app=pi-dev;state=idle\x07');await page.wait_for_function('document.getElementById("screen").dataset.agentMode==="pi-dev"')
                        sessions[0].pty.inbox.put(b'\x1b]7501;state=clear\x07');await page.wait_for_function('document.getElementById("screen").dataset.agentMode==="codex"')
                        await choose_mode(page,'codex');await page.locator('#settings-close').click()
                        # Same backend session ID belongs to a different host/room preference.
                        await page.locator('#sessions').click();await page.locator('#host-list .host-row').nth(1).locator('button').nth(1).click();await page.locator('#host-token').fill(daemons[1].token);await page.locator('#host-form button[type=submit]').click()
                        await page.wait_for_function('MobileTerminal.ready && document.getElementById("screen").dataset.agentMode==="kimi"')
                        assert await page.locator('#agent-mode').input_value()=='auto'
                        await choose_mode(page,'pi-dev');await page.locator('#settings-close').click()
                        await page.evaluate('MobileTerminal.chooseHost("home")');await page.wait_for_function('MobileTerminal.ready && document.getElementById("screen").dataset.agentMode==="codex"')
                        # A slower response cannot replace a more recently selected profile.
                        gate=asyncio.Event();entered=asyncio.Event()
                        async def delayed_grok(route):entered.set();await gate.wait();await route.fulfill(status=200,body=(PROFILES/'grok.json').read_text('utf-8'),content_type='application/json')
                        await page.route('**/agent-modes/grok.json?*',delayed_grok)
                        await page.locator('#options').click();await page.locator('#agent-mode').select_option('grok');await asyncio.wait_for(entered.wait(),3)
                        await page.locator('#agent-mode').select_option('kimi');await page.wait_for_function('document.getElementById("screen").dataset.agentMode==="kimi"')
                        gate.set();await page.wait_for_timeout(100);assert await page.locator('#screen').get_attribute('data-agent-mode')=='kimi';await page.unroute('**/agent-modes/grok.json?*',delayed_grok)
                        await page.locator('#settings-close').click()
                        # The owning renderer is disposed before this pending host response returns.
                        old_gate=asyncio.Event();old_entered=asyncio.Event()
                        async def delayed_claude(route):old_entered.set();await old_gate.wait();await route.fulfill(status=200,body=(PROFILES/'claude-code.json').read_text('utf-8'),content_type='application/json')
                        await page.route('**/agent-modes/claude-code.json?*',delayed_claude)
                        await page.evaluate('window.retiredModeTerminal=MobileTerminal.terminal')
                        await page.locator('#options').click();await page.locator('#agent-mode').select_option('claude-code');await asyncio.wait_for(old_entered.wait(),3);await page.locator('#settings-close').click()
                        await page.evaluate('MobileTerminal.chooseHost("host:other")');await page.wait_for_function('MobileTerminal.ready && document.getElementById("screen").dataset.agentMode==="pi-dev"')
                        old_gate.set();await page.wait_for_timeout(100)
                        assert await page.locator('#screen').get_attribute('data-agent-mode')=='pi-dev'
                        assert await page.evaluate('disposedModeTerminals.includes(retiredModeTerminal)&&modeDecorations.filter(e=>e.term===retiredModeTerminal).every(e=>e.decoration.isDisposed)')
                        await page.unroute('**/agent-modes/claude-code.json?*',delayed_claude)
                        stored=await page.evaluate('JSON.parse(localStorage.getItem("sma.mobile.agent-modes"))')
                        scoped={json.loads(key)[2]:mode for key,mode in stored}
                        assert scoped['home']=='claude-code' and scoped['host:other']=='pi-dev'
                        assert all(json.loads(key)[3]=='aabbccdd' for key,mode in stored)
                        assert all(daemon.token not in json.dumps(stored) for daemon in daemons)
                        await page.goto('about:blank');await page.goto(document);await page.wait_for_function('MobileTerminal.ready && document.getElementById("screen").dataset.agentMode==="claude-code"')
                        assert await page.locator('#agent-mode').input_value()=='claude-code'
                        await page.evaluate('MobileTerminal.chooseHost("host:other")');await page.wait_for_function('MobileTerminal.ready && document.getElementById("screen").dataset.agentMode==="pi-dev"')
                        assert await page.locator('#agent-mode').input_value()=='pi-dev'
                        # Disable removes decorations; changing mode still selects its independent palette.
                        await page.locator('#options').click();await page.locator('#highlight').uncheck();await page.wait_for_function('modeDecorations.every(e=>e.decoration.isDisposed)')
                        await page.locator('#agent-mode').select_option('grok');await page.wait_for_function('document.getElementById("screen").dataset.agentMode==="grok"');assert await page.evaluate('modeDecorations.every(e=>e.decoration.isDisposed)')
                        await page.locator('#highlight').check();await page.wait_for_function('modeDecorations.some(e=>!e.decoration.isDisposed)');await page.locator('#settings-close').click()
                        # Reusing a profile ID for a different room clears preferences AND learned agent identity.
                        await page.evaluate('MobileTerminal.terminal.parser.registerOscHandler(7501,data=>{window.observedModeOsc=data;return false;})')
                        sessions[1].pty.inbox.put(b'\x1b]7501;app=pi-dev;state=idle\x07');await page.wait_for_function('window.observedModeOsc?.includes("pi-dev")')
                        await page.locator('#sessions').click();await page.locator('#host-list .host-row').nth(1).locator('button').nth(1).click()
                        await page.locator('#host-code').fill('mode-room-0');await page.locator('#host-token').fill(daemons[0].token);await page.locator('#host-form button[type=submit]').click()
                        await page.wait_for_function('MobileTerminal.ready && document.getElementById("screen").dataset.agentMode==="codex"')
                        assert await page.locator('#agent-mode').input_value()=='auto'
                        assert all(json.loads(key)[2]!='host:other' for key,mode in await page.evaluate('JSON.parse(localStorage.getItem("sma.mobile.agent-modes"))'))
                        assert all(not session.pty.written for session in sessions)
                        assert not await page.evaluate('outboundModeOps.some(op=>op===5||op===17)')
                        assert profile_requests and {Path(urlsplit(path).path).stem for path in profile_requests}==set(IDS)
                        assert all(parse_qs(urlsplit(path).query)=={'ui_revision':['mobile-mode-fixture']} for path in profile_requests)
                        assert not errors,errors
                        (artifacts/(renderer+'-cells.json')).write_text(json.dumps(evidence,indent=2),'utf-8')
                        await page.evaluate('MobileTerminal.pause()')
                    finally:await browser.close()
        finally:
            for job in jobs:job.cancel()
            await asyncio.gather(*jobs,return_exceptions=True)
            for daemon in daemons:
                for window in list(daemon.hub.windows):daemon.hub.close_window(window)


async def choose_mode(page,id,effective=None):
    if not await page.locator('#settings-dialog').evaluate('element=>element.open'):await page.locator('#options').click()
    await page.locator('#agent-mode').select_option(id)
    await page.wait_for_function('mode=>document.getElementById("screen").dataset.agentMode===mode',arg=effective or id)
    assert await page.locator('#agent-mode').input_value()==id
