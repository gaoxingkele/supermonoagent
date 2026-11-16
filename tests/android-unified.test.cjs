'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {createHash}=require('node:crypto');
const C=require('../src/supermonoagent/static/terminal-client.js');
const M=require('../android/assets/mobile/mobile-core.js');
const app=fs.readFileSync(path.join(__dirname,'../android/assets/mobile/app.js'),'utf8');

test('Raw mode enables native input without focus, remembers explicit choice, and cannot open Managed input',()=>{
  let managed=false,focuses=0;
  const elements=new Map(),$=id=>{if(!elements.has(id))elements.set(id,{attributes:{},setAttribute(k,v){this.attributes[k]=v;},focus(){focuses++;}});return elements.get(id);};
  const context={nativeRawInput:false,rawInputPreferences:new Map(),sessionKey:()=> 'session-a',isManagedSession:()=>managed,
    ready:true,connected:true,submissionPending:()=>false,$,showJournalRaw:()=>{},term:{textarea:{setAttribute(){}},focus(){focuses++;}}};
  vm.createContext(context);vm.runInContext(app.slice(app.indexOf('  function setRawInput('),app.indexOf('  function syncDraftFields(')),context);
  context.setRawInput(true);
  assert.equal(context.term.textarea.readOnly,false);assert.equal(focuses,0);
  assert.equal($('raw-input').textContent,'原始');assert.equal($('raw-input').attributes['aria-pressed'],'true');
  context.setRawInput(false,false,true);assert.equal(context.rawInputPreferences.get('session-a'),false);
  assert.equal(context.term.textarea.readOnly,true);assert.equal($('raw-input').textContent,'草稿');
  context.setRawInput(true,true,true);assert.equal(context.rawInputPreferences.get('session-a'),true);assert.equal(focuses,1);
  context.ready=false;context.setRawInput(true);assert.equal(context.term.textarea.readOnly,true);
  context.ready=true;context.setRawInput(context.rawInputPreferences.get('session-a')!==false);assert.equal(context.term.textarea.readOnly,false);
  managed=true;context.setRawInput(true,false,true);
  assert.equal(context.term.textarea.readOnly,true);assert.equal($('raw-input').hidden,true);assert.equal(context.nativeRawInput,false);
});

test('mobile composer exports are exact shared aliases and preserve committed229',()=>{
  for(const name of ['composerAction','createComposerSubmitter','createComposerEcho'])assert.equal(M[name],C[name],name);
  assert.equal(M.composerAction({key:'Enter',keyCode:229,isComposing:false}), 'send');
  assert.equal(M.composerAction({key:'Enter',keyCode:229,isComposing:true}), 'none');
});
test('Android presentation applies common block cursor without relabeling actual Cursor or changing font size',async()=>{
  const profile=JSON.parse(fs.readFileSync(path.join(__dirname,'../src/supermonoagent/static/agent-modes/cursor-cli.json'),'utf8'));
  const elements=new Map(),$=id=>{if(!elements.has(id))elements.set(id,{dataset:{},value:'',textContent:''});return elements.get(id);};
  const session={id:'s',agent:'cursor-cli'},host={generation:3,inventory:[session]},terminal={options:{fontSize:17}},decorator={setProfile(value){this.profile=value;}};
  const context={C:{...C,loadAgentProfile:async()=>C.validateAgentProfile(profile)},term:terminal,highlighter:decorator,active:'s',selected:()=>host,agentModeKey:()=> 'scope',agentModeRevision:()=> 'fixture',agentModePreferences:new Map(),agentModeIds:new Set(['cursor-cli','auto']),agentModeRequest:null,agentModeGeneration:0,$,URL,location:{href:'https://fixture.invalid/mobile/index.html'}};
  vm.createContext(context);vm.runInContext(app.slice(app.indexOf('  function refreshAgentMode('),app.indexOf("  $('agent-mode').disabled=true;"))+'\nglobalThis.applyMode=refreshAgentMode;',context);
  context.applyMode(true);await Promise.resolve();await Promise.resolve();
  assert.equal(terminal.options.cursorStyle,'block');assert.equal(terminal.options.fontSize,17);assert.equal(terminal.options.theme.background,C.commonAgentProfile.theme.background);
  assert.equal(session.agent,'cursor-cli');assert.equal($('screen').dataset.actualAgent,'cursor-cli');assert.equal(decorator.profile.id,'cursor-cli');
});
test('touch scrolling cannot send native Codex paging keys',()=>{
  const handlers={},local=[],remote=[];
  const screen={dataset:{viewportMode:'readable',scale:'1'},scrollLeft:0,addEventListener:(type,fn)=>handlers[type]=fn};
  const context={$:()=>screen,term:{_core:{_renderService:{dimensions:{css:{cell:{height:18}}}}}},performance:{now:()=>100},gesture:M.createGesture(),panTouch:null,codexPagingAvailable:()=>true,cancelTouchMomentum(){},beginTerminalPress(){},moveTerminalPress(){},cancelTerminalPress(){},localScroll:value=>local.push(value),scroll:value=>local.push(value),remoteHistoryPage:value=>remote.push(value),beginTouchMomentum(){}};
  vm.createContext(context);vm.runInContext(app.slice(app.indexOf("  $('screen').addEventListener('touchstart'"),app.indexOf("  $('screen-history').addEventListener('touchstart'")),context);
  handlers.touchstart({touches:[{clientX:20,clientY:180}],preventDefault(){}});
  handlers.touchmove({touches:[{clientX:20,clientY:60}],preventDefault(){}});
  handlers.touchend({preventDefault(){}});
  assert.equal(remote.length,0);assert.ok(local.length>0);
});
test('existing quick draft has a native raw escape without a second editor',()=>{
  const html=fs.readFileSync(path.join(__dirname,'../android/assets/mobile/index.html'),'utf8');
  assert.match(html,/id="raw-input"/);assert.equal((html.match(/id="quick-draft"/g)||[]).length,1);assert.equal((html.match(/id="draft"/g)||[]).length,1);
});
test('write adapter marks replay and mixed batches before native echo receipt',()=>{
  for(const progress of [[{grid:{boundaryKind:'resume'}}],[{grid:{boundaryKind:''}},{grid:{boundaryKind:'resume'}}]]){
    const observed=[],current={options:{},write:(bytes,done)=>done()},context={M:{createWriter:options=>options},C,term:current,metrics:{writes:0},terminalWrites:new WeakMap(),writeStamps:new WeakMap(),settledSubscriptions:new WeakMap(),agentDetector:null,
      displayGrid(){},parsedTerminalProgress(){},updateTerminalStatus(){},captureReadingAnchor:()=>null,restoreReadingAnchor(){},recordLocalOutput(){},composerSubmit:{output:value=>observed.push(value)},requestAnimationFrame:()=>1,cancelAnimationFrame(){}};
    vm.createContext(context);vm.runInContext(app.slice(app.indexOf('  function activeWriter('),app.indexOf('  function disposeTerminal('))+'\nglobalThis.writer=activeWriter;',context);
    context.writer(current).write(Uint8Array.of(65),false,()=>{},null,progress);assert.equal(observed[0]?.replaying,true);
  }
});
function nativeSubmission({pasteResult,managed=false}={}){
  const writes=[],notices=[],timers=[],fields=new Map();let now=0;
  const $=id=>{if(!fields.has(id))fields.set(id,{value:'中文😀 local draft',textContent:'',blur(){}});return fields.get(id);};
  const terminal={paste:text=>{writes.push(text);return pasteResult;}},socket={readyState:1},host={host:{id:'host',endpoint:'https://fixture.invalid',code:'room'},socket,generation:3,inventory:[{id:'session',window:'window',process_generation:'pty-generation'}]};
  const context={C,M:{createComposerEcho:()=>()=>context.echoed},$: $,term:terminal,ready:true,connected:true,paused:false,composing:false,uploadJob:null,echoed:false,authValid:true,selected:()=>host,scopeKey:'host-session',active:'session',hostId:'host',hostTokens:new Map([['host','synthetic-auth']]),connections:new Map([['host',host]]),safeEndpoint:h=>h.endpoint,managedAuthKey:async t=>createHash('sha256').update(JSON.stringify([t.host.host.id,t.endpoint,t.room,t.token])).digest('hex'),
    sessionKey:()=>context.scopeKey,terminalBindings:new WeakMap([[terminal,{endpoint:host.host.endpoint,code:'room',token:'synthetic-auth',generation:'pty-generation'}]]),validTerminalBinding:()=>context.authValid,isManagedSession:()=>managed,
    attachmentsPending:()=>false,submissionPending:()=>context.composerSubmit.pending,syncDraftFields:text=>{$('draft').value=text;},submitManaged:()=>writes.push('typed-managed'),
    uncertainDrafts:new Map(),uncertainModelCommands:new Set(),drafts:new Map(),modes:new Map(),notice:text=>notices.push(text),setMode(){},closeComposer(){},updateComposer(){},input:text=>{writes.push(text);return true;},historyRenderer:null};
  context.composerSubmit=C.createComposerSubmitter({schedule:(fn,delay)=>{const timer={fn,at:now+delay,cancelled:false};timers.push(timer);return timer;},cancel:timer=>timer.cancelled=true});
  vm.createContext(context);const receiptBlock=app.split('// NATIVE_RECEIPT_START:')[1]?.split('// NATIVE_RECEIPT_END')[0]?.split('\n').slice(1).join('\n');if(receiptBlock)vm.runInContext(receiptBlock,context);
  vm.runInContext(app.slice(app.indexOf('  function submit(){'),app.indexOf('  function openModelMenu(){'))+'\nglobalThis.send=submit;',context);
  const advance=ms=>{const until=now+ms;while(true){const next=timers.filter(timer=>!timer.cancelled&&timer.at<=until).sort((a,b)=>a.at-b.at)[0];if(!next)break;next.cancelled=true;now=next.at;next.fn();}now=until;};
  return {context,$,writes,notices,advance};
}
test('native Android Send waits simulated500ms live receipt then one Enter, never typing or duplicate writes',()=>{
  const f=nativeSubmission();assert.deepEqual(f.writes,[]);f.context.send();f.context.send();assert.deepEqual(f.writes,['中文😀 local draft']);
  f.advance(500);assert.equal(f.writes.length,1);f.context.echoed=true;f.context.composerSubmit.output();f.advance(199);assert.equal(f.writes.length,1);f.advance(1);f.advance(1000);
  assert.deepEqual(f.writes,['中文😀 local draft','\r']);assert.equal(f.$('draft').value,'');
});
test('false paste, changed auth, closed transport, and replay cannot produce Android native Enter',()=>{
  const refused=nativeSubmission({pasteResult:false});refused.context.send();refused.advance(6000);assert.deepEqual(refused.writes,['中文😀 local draft']);assert.equal(refused.$('draft').value,'中文😀 local draft');
  for(const change of [f=>{f.context.authValid=false;},f=>{f.context.selected().socket.readyState=3;}]){const f=nativeSubmission();change(f);f.context.send();f.advance(6000);assert.deepEqual(f.writes,[]);assert.equal(f.$('draft').value,'中文😀 local draft');}
  const stale=nativeSubmission();stale.context.send();stale.context.authValid=false;stale.context.echoed=true;stale.context.composerSubmit.output();stale.advance(6000);assert.deepEqual(stale.writes,['中文😀 local draft']);
  const replay=nativeSubmission();replay.context.send();replay.context.echoed=true;replay.context.composerSubmit.output({replaying:true});replay.context.composerSubmit.output();replay.advance(6000);assert.deepEqual(replay.writes,['中文😀 local draft']);assert.equal(replay.context.uncertainDrafts.get('host-session'),'中文😀 local draft');
});
test('managed Android submission still bypasses all native paste/Enter helpers',()=>{
  const f=nativeSubmission({managed:true});f.context.send();f.advance(6000);assert.deepEqual(f.writes,['typed-managed']);assert.equal(f.$('draft').value,'中文😀 local draft');
});
test('post-paste stale auth or cancellation retains original duplicate guard when target returns',()=>{
  for(const invalidate of [f=>{f.context.authValid=false;f.context.composerSubmit.output();f.context.authValid=true;},f=>f.context.composerSubmit.cancel('target_changed'),f=>f.context.composerSubmit.cancel('disconnected')]){
    const f=nativeSubmission();f.context.send();invalidate(f);assert.equal(f.context.composerSubmit.pending,false);f.context.send();f.advance(6000);
    assert.deepEqual(f.writes,['中文😀 local draft']);assert.equal(f.context.uncertainDrafts.get('host-session'),'中文😀 local draft');
  }
});
test('auxiliary Escape cancels the old receipt before bytes and can never inherit its scheduled Enter',()=>{
  for(const armed of [false,true]){
  const f=nativeSubmission();f.context.selected().socket.send=frame=>{const decoded=C.unpack(frame);f.writes.push(new TextDecoder().decode(C.sessionPayload(decoded.payload).bytes));};
  vm.runInContext(app.slice(app.indexOf('  function input(text'),app.indexOf('  function key(text'))+'\nglobalThis.external=input;',f.context);
  f.context.send();if(armed){f.context.echoed=true;f.context.composerSubmit.output();}f.context.external('\x1b');f.context.echoed=true;f.context.composerSubmit.output();f.advance(6000);
  assert.deepEqual(f.writes,['中文😀 local draft','\x1b']);assert.equal(f.context.uncertainDrafts.get('host-session'),'中文😀 local draft');
  }
});
test('cancelled model command keeps its own guard across reconnect and cannot be pasted twice',()=>{
  const f=nativeSubmission();vm.runInContext(app.slice(app.indexOf('  function openModelMenu(){'),app.indexOf('  function attachmentTarget(){'))+'\nglobalThis.model=openModelMenu;',f.context);
  f.context.model();f.context.composerSubmit.cancel('disconnected');f.context.model();f.advance(6000);
  assert.deepEqual(f.writes,['/model']);assert.equal(f.context.uncertainModelCommands.has('host-session'),true);
});
test('captured uncertainty stays on original draft after target switch, while false paste stays confirmed unsent',()=>{
  const f=nativeSubmission();f.context.send();f.context.scopeKey='other-session';f.context.active='other';f.$('draft').value='new target draft';f.context.composerSubmit.cancel('target_changed');
  assert.equal(f.context.uncertainDrafts.get('host-session'),'中文😀 local draft');assert.equal(f.context.uncertainDrafts.has('other-session'),false);assert.equal(f.$('draft').value,'new target draft');
  f.context.scopeKey='host-session';f.context.active='session';f.$('draft').value='中文😀 local draft';f.context.send();assert.equal(f.writes.length,1);
  const refused=nativeSubmission({pasteResult:false});refused.context.send();refused.advance(6000);assert.equal(refused.context.uncertainDrafts.size,0);
  const thrown=nativeSubmission();thrown.context.term.paste=()=>{throw Error('partial send');};thrown.context.send();thrown.advance(6000);assert.equal(thrown.context.uncertainDrafts.get('host-session'),'中文😀 local draft');assert.ok(!thrown.writes.includes('\r'));
});
test('own synthetic paste and scheduled verified Enter do not self-cancel the native job',()=>{
  const f=nativeSubmission();f.context.selected().socket.send=frame=>{const decoded=C.unpack(frame);f.writes.push(new TextDecoder().decode(C.sessionPayload(decoded.payload).bytes));};
  vm.runInContext(app.slice(app.indexOf('  function input(text'),app.indexOf('  function key(text')),f.context);
  f.context.term.paste=text=>f.context.input(text);f.context.send();assert.equal(f.context.composerSubmit.pending,true);f.context.echoed=true;f.context.composerSubmit.output();f.advance(200);
  assert.deepEqual(f.writes,['中文😀 local draft','\r']);assert.equal(f.context.uncertainDrafts.size,0);
});
test('native uncertainty survives reload with opaque auth proof, but real credential changes clear only that host',async()=>{
  const f=nativeSubmission();f.context.send();f.context.composerSubmit.cancel('disconnected');await new Promise(resolve=>setImmediate(resolve));
  const rows=JSON.parse(JSON.stringify(f.context.nativeUncertaintyTargets()));assert.equal(rows.length,1);assert.ok(!JSON.stringify(rows).includes('synthetic-auth'));
  const restored=nativeSubmission();restored.context.uncertainDrafts.set('host-session','中文😀 local draft');restored.context.restoreNativeUncertaintyTargets(rows);await new Promise(resolve=>setImmediate(resolve));restored.context.send();assert.equal(restored.writes.length,0);
  restored.context.setHostToken('host','different-auth');assert.equal(restored.context.uncertainDrafts.size,0);
  const wrong=nativeSubmission();wrong.context.hostTokens.set('host','different-auth');wrong.context.uncertainDrafts.set('host-session','中文😀 local draft');wrong.context.restoreNativeUncertaintyTargets(rows);await new Promise(resolve=>setImmediate(resolve));assert.equal(wrong.context.uncertainDrafts.size,0);
});
