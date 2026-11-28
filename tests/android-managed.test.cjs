'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {webcrypto}=require('node:crypto');
const shared=require('../src/supermonoagent/static/terminal-client.js');
const app=fs.readFileSync(path.join(__dirname,'../android/assets/mobile/app.js'),'utf8');
const block=app.split('// MANAGED_INTEGRATION_START:')[1]?.split('// MANAGED_INTEGRATION_END')[0]?.split('\n').slice(1).join('\n');
class Element{
  constructor(doc,tag='div'){this.doc=doc;this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.style={};this.attributes={};this.hidden=false;this.disabled=false;this.value='';this._text='';}
  append(...nodes){for(const node of nodes){node.parentNode=this;this.children.push(node);}}
  replaceChildren(...nodes){this.children=[];this._text='';this.append(...nodes);}
  set textContent(value){this.replaceChildren();this._text=String(value);}get textContent(){return this._text+this.children.map(node=>node.textContent).join('');}
  setAttribute(key,value){this.attributes[key]=String(value);}
  querySelectorAll(selector){return this.children.flatMap(node=>[...(selector==='button'&&node.tagName==='BUTTON'?[node]:[]),...node.querySelectorAll(selector)]);}
  remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(node=>node!==this);}
  close(){this.open=false;}focus(){this.doc.activeElement=this;}
}
function fixture({native=false,storage=new Map(),events=false}={}){
  const doc={activeElement:null,querySelector:()=>null,querySelectorAll:()=>[],createElement(tag){return new Element(doc,tag);}};
  const elements=new Map(),$=id=>{if(!elements.has(id))elements.set(id,new Element(doc));return elements.get(id);};
  const session={id:native?'native1':'managed1',window:native?'native-window':'managed-window',process_generation:native?'native-generation':'managed-generation',journal_id:native?'native-journal':'managed-journal',interaction_mode:native?'native':'managed',managed_state:'ready',provider:'claude',cwd:'D:\\synthetic'};
  const c={host:{id:'home',endpoint:'https://relay.example',code:'room'},connected:true,generation:3,capabilities:{managed_conversation:1,...(events?{managed_events:1}:{})},inventory:[session],cockpitInstallationId:'host-install-1'};
  const sent=[],timers=new Map(),notices=[],activations=[];let count=0,now=0;
  const context={TextEncoder,Uint8Array,crypto:webcrypto,Map,Set,JSON,Promise,Error,encoder:new TextEncoder(),C:shared,$,document:doc,term:{options:{disableStdin:false}},active:session.id,ready:false,connected:true,createPending:false,paused:false,composing:false,uploadJob:null,composerSubmit:{pending:false},uncertainModelCommands:new Set(),journalRequestSequence:0,
    performance:{now:()=>now},metrics:{managedStatusRequests:0,managedStatusBytes:0,managedEventsReceived:0,managedEventGaps:0,managedReconciles:0,managedLastEventLatency:null},
    selected:()=>c,hostTokens:new Map([['home','synthetic-token']]),drafts:new Map(),sessionKey:(sid=context.active,hid='home')=>JSON.stringify([hid,sid]),
    journalTarget:()=>{const s=c.inventory.find(s=>s.id===context.active);return {host:c,endpoint:c.host.endpoint,room:c.host.code,token:context.hostTokens.get('home'),generation:c.generation,session:s?.id||'',window:s?.window||'',process_generation:s?.process_generation||'',journalId:s?.journal_id||''};},
    journalTargetCurrent:t=>!!t&&!context.paused&&context.active===t.session&&t.host===c&&t.endpoint===c.host.endpoint&&t.room===c.host.code&&t.token===context.hostTokens.get('home')&&t.generation===c.generation,
    journalError:code=>Object.assign(new Error('fixed journal failure'),{code}),sendHost:(host,op,value)=>{sent.push({op,value});return host.connected;},
    sessionStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},notice:text=>notices.push(text),updateComposer:()=>{},attachmentsPending:()=>false,
    syncDraftFields:text=>{$('draft').value=text;$('quick-draft').value=text;context.drafts.set(context.sessionKey(),text);},refreshJournalStatus:()=>{},
    setTimeout:(fn,delay)=>{timers.set(++count,{fn,at:now+delay});return count;},clearTimeout:id=>timers.delete(id),loadSelected:()=>{},renderList:()=>{},activate:id=>{activations.push(id);context.active=id;}
  };
  $('new-interaction-mode').value='managed';$('managed-provider').value='claude';
  vm.createContext(context);vm.runInContext(block+'\nglobalThis.api={managedTarget,managedCurrent,syncManagedContext,releaseManagedContext,submitManaged,managedPending,renderManagedState,createManaged,mergeManagedCatalog,updateManagedCreation,rememberManagedPending,managedRuntimeEvent,savedManagedCursor,get snapshot(){return managedSnapshot;},get controller(){return managedController;},get records(){return managedPendingRecords;}};',context);
  const tick=()=>new Promise(resolve=>setImmediate(resolve));
  const wait=async predicate=>{const deadline=Date.now()+3000;while(Date.now()<deadline){if(predicate())return;await new Promise(resolve=>setTimeout(resolve,1));}throw Error('fixture wait exceeded');};
  const reply=(result,error)=>{const request=sent.at(-1).value;c.journalPending.get(request.request_id).finish(error||null,result);};
  const status=extra=>({window_id:session.window,session_id:session.id,process_generation:session.process_generation,journal_id:session.journal_id,state:'ready',provider:'claude',provider_version:'synthetic',provider_root_id:'root-1',policy:{mode:'normal'},active_submission:null,permissions:[],...(events?{host_installation_id:c.cockpitInstallationId,runtime_epoch:'boot-1',revision:1}:{}),...extra});
  const mount=async extra=>{await context.api.syncManagedContext();await wait(()=>sent.at(-1)?.value.action==='managed_status');reply(status(extra));await tick();};
  const advance=delta=>{
    now+=delta;
    for(const [id,timer] of [...timers].sort((a,b)=>a[1].at-b[1].at))if(timer.at<=now&&timers.delete(id))timer.fn();
  };
  return {context,api:context.api,c,session,$,doc,sent,timers,notices,activations,storage,tick,wait,reply,status,mount,advance};
}

test('managed observational terminal rejects raw OP17 before any parser/input state',()=>{
  const sent=[],session={id:'m1',window:'w1',interaction_mode:'managed'},host={inventory:[session],socket:{readyState:1,send:value=>sent.push(value)}};
  const context={active:'m1',ready:true,paused:false,connected:true,selected:()=>host,historyRenderer:null,
    uncertainModelCommands:new Set(),sessionKey:()=> 'm1',C:{targetFrame:(...args)=>args},notice:()=>{},
    isManagedSession:()=>true};
  vm.createContext(context);vm.runInContext(app.slice(app.indexOf('  function input(text'),app.indexOf('  function key(text){'))+'\nglobalThis.run=input;',context);
  assert.equal(context.run('must not reach provider'),false);assert.equal(sent.length,0);
});

test('explicit independent creation and typed managed controller are wired into the actual Android assets',()=>{
  const html=fs.readFileSync(path.join(__dirname,'../android/assets/mobile/index.html'),'utf8');
  assert.match(html,/id="new-interaction-mode"/);assert.match(html,/id="managed-provider"/);
  assert.match(app,/createManagedController/);assert.match(app,/create_managed/);
});

test('Android preserves two-second legacy polling and stops idle polling with managed events',async()=>{
  const old=fixture();await old.mount();old.advance(1999);assert.equal(old.sent.length,1);
  old.advance(1);assert.equal(old.sent.length,2);assert.equal(old.sent.at(-1).value.action,'managed_status');
  const modern=fixture({events:true});await modern.mount();modern.advance(600000);
  assert.equal(modern.sent.length,1);assert.equal(modern.context.metrics.managedStatusRequests,1);
  assert.match(app,/managed_conversation:1,managed_events:1/);assert.match(app,/op===26/);
});

test('Android event gaps calibrate, and a tool descriptor stays deny-only until OP24 provides details',async()=>{
  const f=fixture({events:true});await f.mount();
  f.api.managedRuntimeEvent(f.c,f.status({revision:2,host_installation_id:'different-host',state:'running'}));
  assert.equal(f.sent.length,1);assert.equal(f.api.snapshot.state,'ready');
  f.api.managedRuntimeEvent(f.c,f.status({revision:3,state:'running'}));
  assert.equal(f.context.metrics.managedEventGaps,1);assert.equal(f.sent.at(-1).value.action,'managed_status');
  f.reply(f.status({revision:3}));await f.tick();
  const descriptor={permission_request_id:'tool-permission',submission_id:'sub',provider_turn_id:'turn',state:'pending',tool_name:'Command',choices:['allow_once','deny']};
  const state={revision:4,state:'waiting_permission',active_submission:{submission_id:'sub',provider_turn_id:'turn'},permissions:[descriptor]};
  f.api.managedRuntimeEvent(f.c,f.status(state));
  assert.equal(f.$('managed-permissions').children[0].querySelectorAll('button').length,1);
  assert.match(f.$('managed-permissions').textContent,/正在核对工具请求详情/);
  assert.equal(f.sent.at(-1).value.action,'managed_status');
  f.reply(f.status({...state,permissions:[{...descriptor,details:'Exact tool arguments'}]}));await f.tick();
  assert.equal(f.$('managed-permissions').children[0].querySelectorAll('button').length,2);
  assert.match(f.$('managed-permissions').textContent,/Exact tool arguments/);
  f.api.releaseManagedContext();const cursor=f.api.savedManagedCursor(f.api.managedTarget());
  assert.equal(cursor.revision,4);assert.equal(cursor.runtime_epoch,'boot-1');
  f.context.connections=new Map([['home',f.c]]);f.context.composerSubmit.cancel=()=>{};f.context.clearNativeUncertainty=()=>{};
  vm.runInContext(app.slice(app.indexOf('  function setHostToken('),app.indexOf('  function captureNativeScope(')),f.context);
  f.context.setHostToken('home','new-owner-token');assert.equal(f.api.savedManagedCursor(f.api.managedTarget()),null);
});

test('Android reconciles a lost submission ACK at 3, 10 and 30 seconds without resending',async()=>{
  const f=fixture({events:true});await f.mount();f.context.syncDraftFields('retained after ACK loss');f.api.submitManaged();
  const nonce=f.sent.at(-1).value.params.nonce;f.reply(null,new Error('lost ACK'));await f.tick();
  const before=f.sent.length;
  for(const [delta,attempt] of [[3000,1],[7000,2],[20000,3]]){
    f.advance(delta-1);assert.equal(f.sent.length,before+attempt-1);
    f.advance(1);assert.equal(f.sent.length,before+attempt);
    assert.equal(f.sent.at(-1).value.action,'managed_status');assert.equal(f.sent.at(-1).value.params.nonce,nonce);
    assert.ok(!Object.hasOwn(f.sent.at(-1).value.params,'text'));
    f.reply(f.status());await f.tick();
  }
  f.advance(60000);assert.equal(f.sent.length,before+3);
  assert.equal(f.sent.filter(entry=>entry.value.action==='submit_managed').length,1);
  assert.equal(f.$('draft').value,'retained after ACK loss');
});

test('Android backs off a failed gap calibration and clears its timer once idle state is authoritative',async()=>{
  const f=fixture({events:true});await f.mount();
  f.api.managedRuntimeEvent(f.c,f.status({revision:3,resync:true}));
  f.reply(null,new Error('temporary status error'));await f.tick();
  f.advance(29999);assert.equal(f.sent.length,2);
  f.advance(1);assert.equal(f.sent.length,3);
  f.reply(f.status({revision:3}));await f.tick();
  f.advance(60000);assert.equal(f.sent.length,3);
  assert.equal(f.api.snapshot.canSubmit,true);
});
test('managed submit uses durable OP24 once even when terminal replay is not ready',async()=>{
  const f=fixture();await f.mount();f.context.syncDraftFields('中文😀 original request');
  assert.equal(f.context.ready,false);assert.equal(f.api.submitManaged(),true);assert.equal(f.api.submitManaged(),false);
  const request=f.sent.at(-1);assert.equal(request.op,24);assert.equal(request.value.action,'submit_managed');assert.equal(request.value.params.text,'中文😀 original request');
  assert.equal(f.$('draft').value,'中文😀 original request');assert.equal(f.context.term.options.disableStdin,true);
  f.reply({...f.status(),nonce:request.value.params.nonce,submission_id:'submission-1',event_id:'event-1',seq:2,state:'accepted',dispatch_state:'pending'});await f.tick();
  assert.equal(f.$('draft').value,'');assert.equal(f.sent.filter(row=>row.value.action==='submit_managed').length,1);assert.ok(f.sent.every(row=>row.op===24));
});
test('unknown submit ACK persists nonce and text across reconnect; only explicit absent retry resends',async()=>{
  const f=fixture();await f.mount();f.context.syncDraftFields('immutable original');f.api.submitManaged();const original=f.sent.at(-1).value.params;
  f.reply(null,new Error('synthetic disconnect'));await f.tick();assert.equal(f.api.snapshot.pending.phase,'unknown');
  f.api.releaseManagedContext();f.c.generation++;assert.ok(!JSON.stringify([...f.storage]).includes('synthetic-token'));
  await f.mount({receipt:null});assert.equal(f.sent.at(-1).value.params.nonce,original.nonce);assert.equal(f.api.snapshot.pending.phase,'absent');
  assert.equal(f.sent.filter(row=>row.value.action==='submit_managed').length,1);assert.equal(f.api.submitManaged(),false);
  f.$('managed-retry').onclick();const retry=f.sent.at(-1).value;assert.equal(retry.action,'submit_managed');assert.equal(retry.params.nonce,original.nonce);assert.equal(retry.params.text,original.text);
});
test('omitted receipt stays unknown and rotated credentials do not adopt old pending submission',async()=>{
  const f=fixture();await f.mount();f.context.syncDraftFields('keep');f.api.submitManaged();f.reply(null,new Error('lost ACK'));await f.tick();
  f.api.releaseManagedContext();await f.mount();assert.equal(f.api.snapshot.pending.phase,'unknown');assert.equal(f.$('managed-retry').hidden,true);
  f.api.releaseManagedContext();f.context.hostTokens.set('home','different-auth');await f.mount();assert.equal(f.api.snapshot.pending,null);assert.equal(f.$('draft').value,'keep');
});
test('durable acceptance never clears a newer draft and forged receipt never clears original',async()=>{
  for(const forged of [false,true]){const f=fixture();await f.mount();f.context.syncDraftFields('accepted original');f.api.submitManaged();const request=f.sent.at(-1).value;
    if(!forged)f.context.syncDraftFields('newer local draft');
    f.reply({...f.status(),nonce:request.params.nonce,submission_id:'sub',event_id:'event',seq:forged?0:3,state:'accepted',dispatch_state:'pending'});await f.tick();
    assert.equal(f.$('draft').value,forged?'accepted original':'newer local draft');assert.equal(f.api.snapshot.pending?.phase,forged?'unknown':undefined);
  }
});
test('permissions originate from exact status and require one explicit once-or-deny response',async()=>{
  const f=fixture(),permission={permission_request_id:'permission-1',submission_id:'sub-1',provider_turn_id:'turn-1',state:'pending',tool_name:'Bash <script>',details:'<img src=x onerror=unsafe()>',choices:['allow_once','deny']};
  await f.mount({state:'waiting_permission',active_submission:{submission_id:'sub-1',provider_turn_id:'turn-1'},permissions:[permission]});
  const card=f.$('managed-permissions').children[0],buttons=card.querySelectorAll('button');assert.equal(buttons.length,2);assert.equal(f.doc.activeElement,buttons[0]);assert.equal(buttons[0].textContent,'拒绝');assert.ok(card.textContent.includes('<img'));
  buttons[1].onclick();const request=f.sent.at(-1).value;assert.equal(request.action,'respond_managed_permission');assert.equal(request.params.decision,'allow_once');assert.equal(request.params.permission_request_id,'permission-1');
  const before=f.sent.length;buttons[1].onclick();assert.equal(f.sent.length,before);
  f.reply({permission_request_id:'permission-1',state:'decided',decision:'allow_once'});await f.tick();assert.equal(f.$('managed-permissions').children.length,0);
});
test('unsupported interactive permission offers deny only and interrupt uses typed action',async()=>{
  const f=fixture();await f.mount({state:'waiting_permission',active_submission:{submission_id:'sub-1',provider_turn_id:'turn-1'},permissions:[{permission_request_id:'ask',submission_id:'sub-1',provider_turn_id:'turn-1',state:'pending',tool_name:'AskUserQuestion',details:'unsupported question',choices:['allow_once','deny'],requires_user_interaction:true}]});
  assert.equal(f.$('managed-permissions').children[0].querySelectorAll('button').length,1);
  f.$('managed-interrupt').onclick();assert.equal(f.sent.at(-1).value.action,'interrupt_managed');assert.equal(f.sent.at(-1).value.params.submission_id,'sub-1');assert.ok(f.sent.every(row=>row.op===24));
});
test('creation derives launch context from native anchor and activates exact ACK before catalog arrives',async()=>{
  const f=fixture({native:true}),creating=f.api.createManaged();await f.wait(()=>f.sent.at(-1)?.value.action==='create_managed');
  const request=f.sent.at(-1).value;assert.deepEqual(Object.keys(request.params).sort(),['nonce','process_generation','provider','session_id']);assert.equal(request.params.session_id,'native1');
  const result={window_id:'new-window',session_id:'new-session',process_generation:'new-generation',journal_id:'new-journal',interaction_mode:'managed',provider:'claude',state:'starting',cwd:'D:\\server-derived'};
  f.reply(result);await creating;assert.deepEqual(f.activations,['new-session']);assert.ok(f.c.inventory.some(s=>s.id==='new-session'&&s.window==='new-window'));
  const merged=f.api.mergeManagedCatalog(f.c,[f.session]);assert.ok(merged.some(s=>s.id==='new-session'));assert.equal(f.c.managedCreatedTargets.size,1);
  f.api.mergeManagedCatalog(f.c,merged);assert.equal(f.c.managedCreatedTargets.size,0);
});
test('managed controls never fall back to an archive and old capability cannot create',async()=>{
  const f=fixture({native:true});f.c.capabilities={conversation_journal:1};await f.api.createManaged();assert.equal(f.sent.length,0);
  assert.ok(!/journalArchiveRequest|\/journals\/ws|\b(?:paste|input)\(/.test(block));
  assert.match(app,/history_chunks:1,conversation_journal:1,managed_conversation:1/);
});
test('unknown creation uses its immutable nonce and cannot replay under rotated auth',async()=>{
  const f=fixture({native:true});let creating=f.api.createManaged();await f.wait(()=>f.sent.at(-1)?.value.action==='create_managed');const original=f.sent.at(-1).value;
  f.reply(null,new Error('lost create ACK'));await creating;assert.equal(f.c.managedCreation.phase,'unknown');
  creating=f.api.createManaged();await f.wait(()=>f.sent.length===2);assert.equal(f.sent.at(-1).value.params.nonce,original.params.nonce);f.reply(null,new Error('lost again'));await creating;
  f.context.hostTokens.set('home','rotated-auth');const count=f.sent.length,crossAuth=f.api.createManaged();await f.wait(()=>f.sent.length!==count||f.c.managedCreation.phase==='unknown');const after=f.sent.length;if(after!==count)f.reply(null,new Error('must not send'));await crossAuth;assert.equal(after,count);
});
test('changed permission descriptor removes an obsolete allow button and remains deny-only',async()=>{
  const f=fixture(),base={permission_request_id:'same-id',submission_id:'sub',provider_turn_id:'turn',state:'pending',tool_name:'tool',details:'first',choices:['allow_once','deny']};
  await f.mount({state:'waiting_permission',active_submission:{submission_id:'sub',provider_turn_id:'turn'},permissions:[base]});
  assert.equal(f.$('managed-permissions').children[0].querySelectorAll('button').length,2);
  const reconciling=f.api.controller.reconcile();f.reply(f.status({state:'waiting_permission',active_submission:{submission_id:'sub',provider_turn_id:'turn'},permissions:[{...base,requires_user_interaction:true,details:'unknown interaction'}]}));await reconciling;
  assert.equal(f.$('managed-permissions').children[0].querySelectorAll('button').length,1);
});
test('managed quote appends attribution without submission and rejects pending or changed-generation targets',async()=>{
  const f=fixture();await f.mount();f.context.syncDraftFields('existing managed draft');
  vm.runInContext(app.slice(app.indexOf('  function journalQuote('),app.indexOf('  function journalViewerOptions('))+'\nglobalThis.quote=journalQuote;',f.context);
  const target=f.context.journalTarget();assert.equal(f.context.quote(target,'中文😀\nquoted code',{journalId:'old-archive',observedAt:'2026-11-11'}),true);
  const combined=f.$('draft').value;assert.ok(combined.startsWith('existing managed draft\n\n'));assert.ok(combined.includes('old-archive'));assert.ok(combined.includes('> 中文😀\n> quoted code'));assert.equal(f.sent.filter(row=>row.value.action==='submit_managed').length,0);
  f.api.submitManaged();assert.equal(f.context.quote(target,'pending must refuse',{}),false);assert.equal(f.$('draft').value,combined);
  f.reply(null,new Error('unknown ACK'));await f.tick();f.c.generation++;assert.equal(f.context.quote(target,'stale must refuse',{}),false);
});
test('returning to native mode restores editable composer after unknown managed ACK',async()=>{
  const f=fixture();await f.mount();f.context.syncDraftFields('pending');f.api.submitManaged();f.reply(null,new Error('lost'));await f.tick();
  f.context.uncertainDrafts=new Map();f.context.updateAttachments=()=>{};
  vm.runInContext(app.slice(app.indexOf('  function updateComposer('),app.indexOf('  function setMode(')),f.context);
  f.context.updateComposer();assert.equal(f.$('draft').readOnly,true);assert.equal(f.$('quick-draft').readOnly,true);
  f.api.releaseManagedContext();f.c.inventory.push({id:'native-back',window:'native-window',interaction_mode:'native'});f.context.active='native-back';f.context.updateComposer();
  assert.equal(f.$('draft').readOnly,false);assert.equal(f.$('quick-draft').readOnly,false);assert.equal(f.$('composer-close').disabled,false);
});
test('unknown permission decision exposes explicit exact-nonce retry only',async()=>{
  const f=fixture(),permission={permission_request_id:'permission-retry',submission_id:'sub',provider_turn_id:'turn',state:'pending',tool_name:'tool',details:'scope',choices:['allow_once','deny']};
  const state={state:'waiting_permission',active_submission:{submission_id:'sub',provider_turn_id:'turn'},permissions:[permission]};await f.mount(state);
  f.$('managed-permissions').children[0].querySelectorAll('button')[0].onclick();const first=f.sent.at(-1).value;f.reply(null,new Error('lost decision ACK'));await f.tick();
  assert.equal(f.sent.at(-1).value.action,'managed_status');f.reply(f.status(state));await f.tick();
  assert.equal(f.api.snapshot.operation.phase,'unknown');assert.equal(f.$('managed-retry-control').hidden,false);
  f.$('managed-retry-control').onclick();const retry=f.sent.at(-1).value;assert.equal(retry.action,'respond_managed_permission');assert.equal(retry.params.nonce,first.params.nonce);assert.equal(retry.params.decision,'deny');assert.equal(retry.params.permission_request_id,'permission-retry');
});
test('disconnected managed pane never labels cached ready status as sendable',async()=>{
  const f=fixture();await f.mount();f.c.connected=false;f.context.connected=false;f.api.renderManagedState();
  assert.ok(f.$('managed-state').textContent.includes('待核对'));assert.ok(f.$('managed-message').textContent.includes('连接已断开'));
});
test('managed close handler uses exact target and nonce through OP24, never legacy OP9',async()=>{
  const f=fixture();await f.mount();let confirmation=null;
  f.context.openTerminalAction=kind=>{confirmation=kind;};
  const before=f.sent.length;f.$('managed-close').onclick();assert.equal(confirmation,'close');assert.equal(f.sent.length,before);
  const closing=f.api.controller.closeSession(),request=f.sent.at(-1);
  assert.equal(request.op,24);assert.equal(request.value.action,'close_managed');assert.match(request.value.params.nonce,/^[A-Za-z0-9_-]{1,64}$/);
  for(const field of ['window_id','session_id','process_generation','journal_id'])assert.equal(request.value.params[field],f.status()[field]);
  assert.ok(f.sent.every(frame=>frame.op!==9));f.reply({...f.status(),state:'closing'});await closing;
  assert.equal(f.api.snapshot.state,'closing');assert.ok(!/\b(?:send|sendHost|targetFrame)\(\s*(?:\w+,\s*)?9\b/.test(block));
});
test('native creation cannot issue OP6 from a managed window but original native path stays OP6',()=>{
  for(const native of [false,true]){
    const f=fixture({native}),nativeFrames=[];f.$('new-interaction-mode').value='native';f.$('shell').value='powershell';f.context.inventory=f.c.inventory;
    f.context.send=(op,params)=>{nativeFrames.push({op,params});return true;};
    vm.runInContext(app.slice(app.indexOf("  $('new').onclick="),app.indexOf("  $('control').onclick=")),f.context);
    f.$('new').onclick();
    if(native){assert.equal(nativeFrames.length,1);assert.equal(nativeFrames[0].op,6);assert.equal(nativeFrames[0].params.shell,'powershell');}
    else{assert.equal(nativeFrames.length,0);f.api.updateManagedCreation();assert.equal(f.$('new').disabled,true);assert.ok(f.notices.some(text=>text.includes('原始 CLI')));}
  }
});
test('prior failed or incomplete outcome has its own Chinese note while ready remains sendable',async()=>{
  for(const [outcome,expected] of [['failed','请检查所选工具的接入配置与连接'],['blocked','输入记录已保留'],['ambiguous','结果待核对'],['interrupted','已中断'],['completed','未发现可确认结论']]){
    const f=fixture();await f.mount({last_outcome:{submission_id:'previous-submission',outcome,reason:'synthetic_outcome',has_final:false}});
    assert.equal(f.api.snapshot.state,'ready');assert.equal(f.api.snapshot.error,null);assert.equal(f.api.snapshot.canSubmit,true);
    assert.equal(f.$('managed-outcome').hidden,false);assert.ok(f.$('managed-outcome').textContent.includes(expected));
    assert.ok(f.$('managed-state').textContent.includes('可发送'));assert.ok(!f.$('managed-message').textContent.includes(expected));assert.ok(!f.notices.some(text=>text.includes(expected)));
  }
  const f=fixture();await f.mount({last_outcome:{submission_id:'previous-submission',outcome:'completed',reason:null,has_final:true}});assert.equal(f.$('managed-outcome').hidden,true);assert.equal(f.$('managed-outcome').textContent,'');
});
test('prior outcome never replaces a current pending permission or transport status',async()=>{
  const f=fixture();await f.mount({state:'waiting_permission',active_submission:{submission_id:'current-sub',provider_turn_id:'turn'},
    last_outcome:{submission_id:'previous-sub',outcome:'failed',reason:'provider_failed',has_final:false},
    permissions:[{permission_request_id:'permission',submission_id:'current-sub',provider_turn_id:'turn',state:'pending',tool_name:'tool',details:'current request detail',choices:['deny']}]});
  assert.ok(f.$('managed-outcome').textContent.includes('请检查所选工具的接入配置与连接'));assert.ok(f.$('managed-outcome').textContent.includes('原输入已保留'));assert.equal(f.$('managed-permissions').children.length,1);assert.ok(f.$('managed-permissions').textContent.includes('current request detail'));
  f.c.connected=false;f.context.connected=false;f.api.renderManagedState();assert.ok(f.$('managed-message').textContent.includes('连接已断开'));assert.ok(f.$('managed-outcome').textContent.includes('请检查所选工具的接入配置与连接'));
});
