'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {createManagedController}=require('../src/supermonoagent/static/terminal-client.js');
const target={window_id:'window-a',session_id:'session-a',process_generation:'generation-a',journal_id:'journal-a'};
const ready=(extra={})=>({...target,state:'ready',provider:'codex',provider_version:'synthetic',provider_root_id:'root-a',policy:'native',active_submission:null,permissions:[],...extra});
const receipt=(params,extra={})=>({...target,nonce:params.nonce,submission_id:'submission-a',event_id:'event-a',seq:1,state:'accepted',dispatch_state:'dispatch_intent',...extra});
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};}
function make(request,options={}){return createManagedController({target,targetKey:'host-a:auth-a',isCurrent:()=>true,request,...options});}

test('managed factory imports without DOM and does not perform implicit RPC',()=>{let calls=0;const c=make(async()=>{calls++;});assert.equal(calls,0);assert.equal(c.snapshot().pending,null);assert.equal(c.snapshot().state,'unknown');assert.equal(typeof c.exportPending,'function');});
test('durable matching receipt alone accepts exact immutable text and notifies once',async()=>{const calls=[],accepted=[],text='原始中文😀\nsynthetic-secret';const c=make(async(action,params)=>{calls.push([action,params]);return receipt(params);},{onAccepted:(ack,pending)=>accepted.push([ack,pending])});const snap=await c.submit(text);assert.equal(snap.pending,null);assert.equal(snap.receipt.event_id,'event-a');assert.equal(accepted.length,1);assert.equal(accepted[0][1].text,text);assert.equal(calls[0][0],'submit_managed');assert.deepEqual(Object.keys(calls[0][1]).sort(),[...Object.keys(target),'nonce','text'].sort());assert.match(calls[0][1].nonce,/^[a-f0-9-]{36}$/);assert.equal(calls[0][1].text,text);assert.equal(snap.active_submission.submission_id,'submission-a');});
test('duplicate clicks share pending nonce and never queue a new prompt',async()=>{const hold=deferred(),calls=[];const c=make(async(action,params)=>{calls.push([action,params]);return hold.promise;});const first=c.submit('first');await c.submit('different draft');assert.equal(calls.length,1);const pending=c.snapshot().pending;assert.equal(pending.text,'first');hold.resolve(receipt(calls[0][1]));await first;assert.equal(c.snapshot().pending,null);assert.equal(calls.length,1);});
test('lost ACK keeps draft and nonce; reconcile queries receipt without prompt replay',async()=>{const calls=[],accepted=[];let original;const c=make(async(action,params)=>{calls.push([action,params]);if(action==='submit_managed'){original=params;throw Error('private transport token');}return ready({receipt:receipt(original)});},{onAccepted:(ack,pending)=>accepted.push(pending.text)});await c.submit('保留草稿');const nonce=c.snapshot().pending.nonce;assert.equal(c.snapshot().pending.phase,'unknown');await c.submit('other text');assert.equal(calls.length,1);await c.reconcile();assert.equal(calls.length,2);assert.equal(calls[1][0],'managed_status');assert.equal(calls[1][1].nonce,nonce);assert.deepEqual(accepted,['保留草稿']);assert.equal(c.snapshot().pending,null);assert.ok(!JSON.stringify(c.snapshot()).includes('private transport token'));});
test('missing receipt remains unknown; explicit null permits only explicit same-nonce retry',async()=>{const calls=[];let status=ready(),attempt=0;const c=make(async(action,params)=>{calls.push([action,params]);if(action==='managed_status')return status;if(++attempt===1)throw Error('lost');return receipt(params);});await c.submit('same immutable body');const nonce=c.snapshot().pending.nonce;await c.reconcile();await c.retry();assert.equal(calls.filter(x=>x[0]==='submit_managed').length,1);status=ready({receipt:null});await c.reconcile();assert.equal(c.snapshot().pending.phase,'absent');assert.equal(calls.filter(x=>x[0]==='submit_managed').length,1);await c.retry();assert.equal(calls.filter(x=>x[0]==='submit_managed').length,2);const submits=calls.filter(x=>x[0]==='submit_managed');assert.equal(submits[1][1].nonce,nonce);assert.equal(submits[1][1].text,'same immutable body');});
test('receipt must match exact target, nonce, accepted event and positive sequence',async()=>{for(const extra of [{journal_id:'other'},{process_generation:'other'},{nonce:'other'},{event_id:''},{seq:0},{seq:1.1},{state:'queued'},{submission_id:''}]){let notified=0;const c=make(async(action,params)=>receipt(params,extra),{onAccepted:()=>notified++});await c.submit('draft');assert.equal(notified,0);assert.equal(c.snapshot().pending.phase,'unknown');assert.equal(c.snapshot().receipt,null);}});
test('UTF8 64KiB bound preserves Chinese, emoji and newlines without truncating',async()=>{const calls=[];const c=make(async(action,params)=>{calls.push(params);return receipt(params);});const exact='😀'.repeat(16384);await c.submit(exact);assert.equal(calls[0].text,exact);const d=make(async()=>assert.fail('oversized RPC'));await d.submit(exact+'中');assert.equal(d.snapshot().error,'invalid_text');assert.equal(d.snapshot().pending,null);await d.submit('');assert.equal(d.snapshot().error,'invalid_text');});
test('context switches and current-target failure discard late receipt callbacks',async()=>{for(const switchContext of [true,false]){const hold=deferred();let accepted=0,current=true,params;const c=make(async(action,p)=>{params=p;return hold.promise;},{isCurrent:()=>current,onAccepted:()=>accepted++});const pending=c.submit('old draft');const exported=c.exportPending();assert.equal(exported.text,'old draft');if(switchContext)c.setContext({target:{...target,session_id:'session-b'},targetKey:'host-a:auth-a'});else current=false;hold.resolve(receipt(params));await pending;assert.equal(accepted,0);assert.equal(c.snapshot().receipt,null);}});
test('pending export is a clone; restore needs stable auth/host plus exact target and never auto replays',async()=>{let calls=0;const c=make(async()=>{throw Error('lost');});await c.submit('keep this');const record=c.exportPending();record.text='edited clone';assert.equal(c.exportPending().text,'keep this');const good=c.exportPending(),d=make(async(action,params)=>{calls++;return ready({receipt:null});});assert.equal(d.restorePending(good),true);assert.equal(calls,0);assert.equal(d.snapshot().pending.phase,'unknown');const different=make(async()=>{}, {targetKey:'host-a:auth-b'});assert.equal(different.restorePending(good),false);const wrong=make(async()=>{}, {target:{...target,process_generation:'other'}});assert.equal(wrong.restorePending(good),false);await d.reconcile();assert.equal(calls,1);});
test('permission accepts only registered once/deny choices for active exact turn',async()=>{const calls=[],permission={permission_request_id:'p1',submission_id:'s1',provider_turn_id:'t1',tool_name:'tool',details:'<img> synthetic',choices:['allow_once','deny'],state:'pending'};const c=make(async(action,params)=>{calls.push([action,params]);if(action==='managed_status')return ready({state:'waiting_permission',active_submission:{submission_id:'s1',provider_turn_id:'t1',cancel_version:0},permissions:[permission]});return {permission_request_id:'p1',state:'decided',decision:params.decision};});await c.reconcile();await c.respondPermission('p1','allow_always');assert.equal(calls.length,1);await c.respondPermission('other','allow_once');assert.equal(calls.length,1);await c.respondPermission('p1','allow_once');assert.equal(calls.length,2);assert.equal(calls[1][0],'respond_managed_permission');assert.equal(calls[1][1].submission_id,'s1');assert.equal(calls[1][1].provider_turn_id,'t1');assert.equal(calls[1][1].decision,'allow_once');await c.respondPermission('p1','deny');assert.equal(calls.length,2);});
test('interrupt captures exact submission and late response cannot change the next turn',async()=>{let active='s1';const hold=deferred(),calls=[];const c=make(async(action,params)=>{calls.push([action,params]);if(action==='managed_status')return ready({state:'running',active_submission:{submission_id:active,provider_turn_id:active+'-turn',cancel_version:0}});return hold.promise;});await c.reconcile();const interrupt=c.interrupt();assert.equal(calls[1][1].submission_id,'s1');active='s2';await c.reconcile();hold.resolve({submission_id:'s1',state:'interrupt_requested',cancel_version:1});await interrupt;assert.equal(c.snapshot().active_submission.submission_id,'s2');assert.equal(c.snapshot().state,'running');});
test('interrupt invalidates pending allow callbacks and closing never accepts new prompts',async()=>{const allow=deferred(),calls=[];const c=make(async(action,params)=>{calls.push([action,params]);if(action==='managed_status')return ready({state:'waiting_permission',active_submission:{submission_id:'s',provider_turn_id:'t',cancel_version:0},permissions:[{permission_request_id:'p',submission_id:'s',provider_turn_id:'t',state:'pending',choices:['allow_once','deny']}]});if(action==='respond_managed_permission')return allow.promise;if(action==='interrupt_managed')return {submission_id:'s',state:'interrupt_requested',cancel_version:1};return {...target,state:'closing'};});await c.reconcile();const response=c.respondPermission('p','allow_once');await c.interrupt();allow.resolve({permission_request_id:'p',decision:'allow_once',state:'decided'});await response;assert.equal(c.snapshot().state,'interrupting');assert.equal(c.snapshot().permissions.length,0);await c.closeSession();await c.submit('must stay draft');assert.equal(calls.filter(x=>x[0]==='submit_managed').length,0);assert.equal(c.snapshot().state,'closing');});
test('dispose invalidates controls and retains an explicit pending export only as return value',async()=>{const hold=deferred();let accepted=0,params;const c=make(async(action,p)=>{params=p;return hold.promise;},{onAccepted:()=>accepted++});const work=c.submit('pending');const record=c.dispose();assert.equal(record.text,'pending');hold.resolve(receipt(params));await work;assert.equal(accepted,0);assert.equal(c.snapshot().state,'disposed');await c.reconcile();assert.equal(c.snapshot().state,'disposed');});

test('JSON envelope expansion is rejected without shortening a body that fits UTF8 limit',async()=>{let calls=0;const c=make(async()=>{calls++;});await c.submit('\0'.repeat(65536));assert.equal(calls,0);assert.equal(c.snapshot().error,'request_too_large');assert.equal(c.snapshot().pending,null);});
test('query cannot mark an in-flight send absent or fire accepted twice on late ACK',async()=>{const hold=deferred();let params,accepted=0;const c=make(async(action,p)=>{if(action==='submit_managed'){params=p;return hold.promise;}return ready({receipt:receipt(params)});},{onAccepted:()=>accepted++});const sending=c.submit('draft');await c.reconcile();assert.equal(accepted,1);hold.resolve(receipt(params));await sending;assert.equal(accepted,1);assert.equal(c.snapshot().state,'ready');});
test('explicit absent receipt during an in-flight send cannot enable a concurrent retry',async()=>{const hold=deferred();let calls=0;const c=make(async(action,params)=>{if(action==='submit_managed'){calls++;return hold.promise;}return ready({receipt:null});});const sending=c.submit('draft');await c.reconcile();assert.equal(c.snapshot().pending.phase,'sending');await c.retry();assert.equal(calls,1);hold.reject(Error('offline'));await sending;assert.equal(c.snapshot().pending.phase,'unknown');});
test('closing while a submit ACK is pending accepts its committed input without reopening the session',async()=>{const hold=deferred();let params,accepted=0;const c=make(async(action,p)=>{if(action==='submit_managed'){params=p;return hold.promise;}return {...target,state:'ended'};},{onAccepted:()=>accepted++});const send=c.submit('input');await c.closeSession();hold.resolve(receipt(params));await send;assert.equal(accepted,1);assert.equal(c.snapshot().state,'ended');assert.equal(c.snapshot().active_submission,null);});
test('lost control ACK retries retain exact nonce and captured target only on explicit action',async()=>{const calls=[];let attempts=0;const c=make(async(action,params)=>{calls.push([action,params]);if(action==='managed_status')return ready({state:'running',active_submission:{submission_id:'s1',provider_turn_id:'t1',cancel_version:0}});if(++attempts===1)throw Error('lost');return {submission_id:'s1',state:'interrupt_requested',cancel_version:1};});await c.reconcile();await c.interrupt();assert.equal(c.snapshot().operation.phase,'unknown');await c.interrupt();const interrupts=calls.filter(x=>x[0]==='interrupt_managed');assert.equal(interrupts.length,2);assert.deepEqual(interrupts[0][1],interrupts[1][1]);await c.reconcile();assert.equal(c.snapshot().state,'interrupting');});
test('cancelled submission cannot regain approval through an old status snapshot',async()=>{const calls=[];const permission={permission_request_id:'p',submission_id:'s',provider_turn_id:'t',state:'pending',choices:['allow_once','deny']};const c=make(async(action,params)=>{calls.push(action);if(action==='managed_status')return ready({state:'waiting_permission',active_submission:{submission_id:'s',provider_turn_id:'t',cancel_version:0},permissions:[permission]});return {submission_id:'s',state:'interrupt_requested',cancel_version:1};});await c.reconcile();await c.interrupt();await c.reconcile();await c.respondPermission('p','allow_once');assert.equal(c.snapshot().permissions.length,0);assert.equal(c.snapshot().state,'interrupting');assert.equal(calls.filter(x=>x==='respond_managed_permission').length,0);});
test('interactive unsupported permissions never expose a generic affirmative and malformed status fails closed',async()=>{const c=make(async()=>ready({state:'waiting_permission',active_submission:{submission_id:'s',provider_turn_id:'t'},permissions:[{permission_request_id:'p',submission_id:'s',provider_turn_id:'t',state:'pending',choices:['allow_once','deny'],requires_user_interaction:true}]}));await c.reconcile();await c.respondPermission('p','allow_once');assert.equal(c.snapshot().error,'stale_permission');const d=make(async()=>ready({journal_id:'wrong'}));await d.reconcile();assert.equal(d.snapshot().error,'status_unavailable');assert.equal(d.snapshot().canSubmit,false);});


test('unsupported interaction snapshots expose deny only and stale targets cannot notify callbacks',async()=>{let notifications=0,current=true;const c=make(async()=>ready({state:'waiting_permission',active_submission:{submission_id:'s',provider_turn_id:'t'},permissions:[{permission_request_id:'p',submission_id:'s',provider_turn_id:'t',state:'pending',choices:['allow_once','deny'],requires_user_interaction:true}]}),{onState:()=>notifications++,isCurrent:()=>current});await c.reconcile();assert.deepEqual(c.snapshot().permissions[0].choices,['deny']);const prior=notifications;current=false;await c.submit('no send');await c.interrupt();assert.equal(notifications,prior);});
test('restored records must satisfy envelope and valid Unicode without lossy conversion',()=>{const c=make(async()=>assert.fail('RPC'));const record={schema:1,targetKey:'host-a:auth-a',target,nonce:'nonce',text:'\0'.repeat(65536),phase:'unknown'};assert.equal(c.restorePending(record),false);assert.equal(c.restorePending({...record,text:'\ud800'}),false);assert.equal(c.snapshot().pending,null);});
test('permission acknowledgement must be durable decided and cannot confuse the active submission',async()=>{for(const extra of [{state:'queued'},{state:'decided',submission_id:'wrong'},{state:'decided',cancel_version:1}]){const c=make(async(action,params)=>action==='managed_status'?ready({state:'waiting_permission',active_submission:{submission_id:'s',provider_turn_id:'t',cancel_version:0},permissions:[{permission_request_id:'p',submission_id:'s',provider_turn_id:'t',state:'pending',choices:['deny']}]}):{permission_request_id:'p',decision:'deny',...extra});await c.reconcile();await c.respondPermission('p','deny');assert.equal(c.snapshot().error,'control_unknown');assert.equal(c.snapshot().permissions.length,1);}});

test('Ready status retains a known failed last outcome across polls without inventing a final or transport error',async()=>{let include=true;const outcome={submission_id:'prior-submission',outcome:'failed',reason:'provider_auth_failed',has_final:false};const c=make(async()=>ready(include?{last_outcome:outcome}:{}));await c.reconcile();assert.deepEqual(c.snapshot().last_outcome,outcome);assert.equal(c.snapshot().state,'ready');assert.equal(c.snapshot().error,null);assert.equal(c.snapshot().receipt,null);assert.equal(c.snapshot().active_submission,null);assert.equal(c.snapshot().canSubmit,true);include=false;await c.reconcile();assert.deepEqual(c.snapshot().last_outcome,outcome);const changed=c.snapshot();changed.last_outcome.reason='mutated';assert.equal(c.snapshot().last_outcome.reason,'provider_auth_failed');});
test('last outcome resets on context/new acceptance and invalid outcome metadata cannot claim a final',async()=>{const outcome={submission_id:'prior',outcome:'blocked',reason:'provider_denied',has_final:false};const c=make(async(action,params)=>action==='managed_status'?ready({last_outcome:outcome}):receipt(params));await c.reconcile();await c.submit('next input');assert.equal(c.snapshot().last_outcome,null);c.setContext({target:{...target,journal_id:'other'},targetKey:'host-a:auth-b'});assert.equal(c.snapshot().last_outcome,null);for(const bad of [{...outcome,reason:'x'.repeat(129)},{...outcome,has_final:'yes'},{...outcome,outcome:'guessed_final'},{...outcome,has_final:true}]){const d=make(async()=>ready({last_outcome:bad}));await d.reconcile();assert.equal(d.snapshot().last_outcome,null);assert.equal(d.snapshot().receipt,null);assert.equal(d.snapshot().error,'status_unavailable');}});
test('normalized dictionary permission details remain literal transient text',async()=>{const details='{"command":"echo <script>synthetic</script>","cwd":"D:/synthetic","reason":"synthetic command approval","requiresNetworkApproval":false}',c=make(async()=>ready({state:'waiting_permission',active_submission:{submission_id:'s',provider_turn_id:'t'},permissions:[{permission_request_id:'p',submission_id:'s',provider_turn_id:'t',state:'pending',choices:['allow_once','deny'],details}]}));await c.reconcile();assert.equal(c.snapshot().permissions[0].details,details);assert.equal(c.snapshot().permissions[0].permission_request_id,'p');assert.equal(c.exportPending(),null);});

const runtimeStatus=(revision,extra={})=>ready({host_installation_id:'installation-a',runtime_epoch:'epoch-a',revision,...extra});
const cursor=(revision,extra={})=>({host_installation_id:'installation-a',runtime_epoch:'epoch-a',revision,
  journal_id:target.journal_id,process_generation:target.process_generation,...extra});
const turn={submission_id:'submission-a',provider_turn_id:'turn-a',cancel_version:0};

test('runtime events require negotiation, the exact target and installation, and a safe revision',async()=>{
  const legacy=make(async()=>ready());
  assert.equal(legacy.applyRuntimeEvent(runtimeStatus(1)),'ignored');
  const c=make(async()=>assert.fail('Unexpected RPC'),{runtimeEvents:true,hostInstallationId:'installation-a'});
  for(const wrong of [{host_installation_id:'other'},{process_generation:'other'},{window_id:'other'},{revision:-1},{revision:1.5},{revision:0}]){
    assert.equal(c.applyRuntimeEvent(runtimeStatus(1,wrong)),'ignored');
  }
  assert.equal(c.snapshot().canSubmit,false);
  assert.equal(c.applyRuntimeEvent(runtimeStatus(18)),'applied');
  assert.equal(c.snapshot().canSubmit,true);
  assert.deepEqual(c.runtimeCursor(),cursor(18));
  c.runtimeCursor().revision=0;
  assert.equal(c.applyRuntimeEvent(runtimeStatus(18,{state:'error'})),'ignored');
  assert.equal(c.applyRuntimeEvent(runtimeStatus(17,{state:'error'})),'ignored');
  assert.equal(c.snapshot().state,'ready');
});

test('a reconnect seed detects gaps and epoch changes and fences controls until a fresh snapshot',async()=>{
  let status=runtimeStatus(9),requests=0;
  const c=make(async()=>{requests++;return status;},{runtimeEvents:true,runtimeCursor:cursor(7)});
  assert.deepEqual(c.runtimeCursor(),cursor(7));
  assert.equal(c.snapshot().awaiting_runtime,true);
  await c.submit('seeded state cannot send');
  assert.equal(requests,0);
  assert.equal(c.applyRuntimeEvent(runtimeStatus(9)),'reconcile');
  status=runtimeStatus(8);
  await c.reconcile();
  assert.equal(c.snapshot().canSubmit,false);
  status=runtimeStatus(9);
  await c.reconcile();
  assert.equal(c.snapshot().canSubmit,true);
  assert.equal(c.snapshot().awaiting_runtime,false);
  const newEpoch={runtime_epoch:'epoch-b'};
  assert.equal(c.applyRuntimeEvent({...target,...cursor(0,newEpoch),resync:true}),'reconcile');
  await c.reconcile();
  assert.equal(c.snapshot().canSubmit,false);
  status=runtimeStatus(0,newEpoch);
  await c.reconcile();
  assert.deepEqual(c.runtimeCursor(),cursor(0,newEpoch));
  assert.equal(c.applyRuntimeEvent(runtimeStatus(10,{state:'error'})),'ignored');
  assert.equal(c.applyRuntimeEvent(runtimeStatus(1,{...newEpoch,state:'running',active_submission:turn})),'applied');
  assert.equal(c.snapshot().state,'running');
});

test('an ambiguous archived snapshot can settle an exact receipt without regaining process authority',async()=>{
  const requests=[],accepted=[],nonce='archived-nonce';
  const c=make(async(action,params)=>{
    requests.push(action);
    return runtimeStatus(0,{state:'ambiguous',active_submission:turn,receipt:receipt(params)});
  },{runtimeEvents:true,onAccepted:value=>accepted.push(value.nonce)});
  assert.equal(c.restorePending({schema:1,targetKey:'host-a:auth-a',target,nonce,text:'draft',phase:'unknown'}),true);
  await c.reconcile();
  assert.deepEqual(accepted,[nonce]);
  assert.deepEqual(c.runtimeCursor(),cursor(0));
  assert.equal(c.snapshot().state,'unknown');
  assert.equal(c.snapshot().awaiting_runtime,false);
  assert.equal(c.snapshot().active_submission,null);
  await c.submit('must stay local');
  await c.closeSession();
  assert.deepEqual(requests,['managed_status']);
});

test('a recovery arriving during a query coalesces callers and rechecks once after a stale answer',async()=>{
  const earlier=deferred(),later=deferred();
  let requests=0;
  const c=make(async()=>++requests===1?earlier.promise:later.promise,{runtimeEvents:true});
  assert.equal(c.applyRuntimeEvent(runtimeStatus(1)),'applied');
  const running=c.reconcile();
  assert.equal(c.applyRuntimeEvent(runtimeStatus(3,{permissions:null})),'ignored');
  assert.equal(c.snapshot().canSubmit,true);
  assert.equal(c.applyRuntimeEvent(runtimeStatus(3)),'reconcile');
  const covered=c.reconcile();
  assert.equal(requests,1);
  earlier.resolve(runtimeStatus(1));
  later.resolve(runtimeStatus(3));
  await Promise.all([running,covered]);
  assert.equal(requests,2);
  assert.deepEqual(c.runtimeCursor(),cursor(3));
  assert.equal(c.snapshot().canSubmit,true);
});

test('an event wins over an older status while its exact receipt can still settle the draft once',async()=>{
  const response=deferred(),ack=deferred(),accepted=[];
  let submitted,queried;
  const c=make(async(action,params)=>{
    if(action==='submit_managed'){submitted=params;return ack.promise;}
    queried=params;return response.promise;
  },{runtimeEvents:true,onAccepted:(value,record)=>accepted.push(record.text)});
  assert.equal(c.applyRuntimeEvent(runtimeStatus(1)),'applied');
  const sending=c.submit('once');
  const reconciling=c.reconcile();
  assert.equal(queried.nonce,submitted.nonce);
  assert.equal(c.applyRuntimeEvent(runtimeStatus(2,{state:'running',active_submission:turn})),'applied');
  response.resolve(runtimeStatus(1,{receipt:receipt(submitted)}));
  await reconciling;
  ack.resolve(receipt(submitted));
  await sending;
  assert.deepEqual(accepted,['once']);
  assert.equal(c.snapshot().state,'running');
  assert.deepEqual(c.runtimeCursor(),cursor(2));
});

test('a delayed absent status cannot make an event-ready draft retryable or replace a new outcome',async()=>{
  const response=deferred();
  let submitted;
  const c=make(async(action,params)=>{
    if(action==='submit_managed'){submitted=params;throw Error('lost ACK');}
    return response.promise;
  },{runtimeEvents:true});
  assert.equal(c.applyRuntimeEvent(runtimeStatus(1)),'applied');
  await c.submit('once');
  const reconciling=c.reconcile(),outcome={submission_id:turn.submission_id,outcome:'completed',reason:null,has_final:true};
  assert.equal(c.applyRuntimeEvent(runtimeStatus(2,{last_outcome:outcome})),'applied');
  response.resolve(runtimeStatus(1,{receipt:null,last_outcome:null}));
  await reconciling;
  assert.equal(c.snapshot().pending.phase,'unknown');
  assert.equal(c.snapshot().pending.nonce,submitted.nonce);
  assert.deepEqual(c.snapshot().last_outcome,outcome);
});

test('a durable receipt on an event wins even if the ACK is lost, and a duplicate cannot notify again',async()=>{
  const ack=deferred(),accepted=[];
  let submitted;
  const c=make(async(action,params)=>{submitted=params;return ack.promise;},
    {runtimeEvents:true,onAccepted:(value,record)=>accepted.push([value.nonce,record.text])});
  assert.equal(c.applyRuntimeEvent(runtimeStatus(1)),'applied');
  const sending=c.submit('immutable draft');
  const pushed=runtimeStatus(2,{state:'running',active_submission:turn,receipt:receipt(submitted)});
  assert.equal(c.applyRuntimeEvent(pushed),'applied');
  assert.equal(c.applyRuntimeEvent(pushed),'ignored');
  ack.reject(Error('socket lost'));
  await sending;
  assert.deepEqual(accepted,[[submitted.nonce,'immutable draft']]);
  assert.equal(c.snapshot().pending,null);
  assert.equal(c.snapshot().state,'running');
});

test('a delayed submit ACK keeps a completed event outcome and does not reopen its turn',async()=>{
  const ack=deferred(),accepted=[];
  let submitted;
  const c=make(async(action,params)=>{submitted=params;return ack.promise;},
    {runtimeEvents:true,onAccepted:value=>accepted.push(value.submission_id)});
  assert.equal(c.applyRuntimeEvent(runtimeStatus(1)),'applied');
  const sending=c.submit('immutable draft');
  const outcome={submission_id:turn.submission_id,outcome:'completed',reason:null,has_final:true};
  assert.equal(c.applyRuntimeEvent(runtimeStatus(2,{last_outcome:outcome})),'applied');
  ack.resolve(receipt(submitted));
  await sending;
  assert.deepEqual(accepted,[turn.submission_id]);
  assert.equal(c.snapshot().state,'ready');
  assert.equal(c.snapshot().active_submission,null);
  assert.deepEqual(c.snapshot().last_outcome,outcome);
});

test('event permission descriptors stay deny-only until an equal-revision status hydrates details',async()=>{
  const descriptor={permission_request_id:'p',submission_id:turn.submission_id,provider_turn_id:turn.provider_turn_id,
    tool_name:'synthetic',state:'pending',choices:['allow_once','deny']};
  const publicStatus={state:'waiting_permission',active_submission:turn,permissions:[descriptor]};
  const details='{"command":"echo synthetic"}';
  const calls=[];
  let permissionStatus=descriptor;
  const c=make(async(action,params)=>{
    calls.push(action);
    if(action==='managed_status')return runtimeStatus(2,{...publicStatus,permissions:[permissionStatus]});
    return {permission_request_id:'p',state:'decided',decision:params.decision};
  },{runtimeEvents:true});
  assert.equal(c.applyRuntimeEvent(runtimeStatus(2,publicStatus)),'applied');
  assert.deepEqual(c.snapshot().permissions[0].choices,['deny']);
  assert.equal(c.snapshot().permissions[0].details_required,true);
  await c.respondPermission('p','allow_once');
  assert.deepEqual(calls,[]);
  await c.reconcile();
  assert.equal(c.snapshot().permissions[0].details_required,true);
  permissionStatus={...descriptor,details};
  await c.reconcile();
  assert.equal(c.snapshot().permissions[0].details,details);
  assert.equal(c.snapshot().permissions[0].details_required,undefined);
  await c.respondPermission('p','allow_once');
  assert.deepEqual(calls,['managed_status','managed_status','respond_managed_permission']);
});

test('a permission descriptor arriving during a stale query requests fresh details once',async()=>{
  const older=deferred(),details='synthetic details';
  const descriptor={permission_request_id:'p',submission_id:turn.submission_id,provider_turn_id:turn.provider_turn_id,
    tool_name:'synthetic',state:'pending',choices:['allow_once','deny']};
  const publicStatus={state:'waiting_permission',active_submission:turn,permissions:[descriptor]};
  let requests=0;
  const c=make(async()=>++requests===1?older.promise:runtimeStatus(2,{...publicStatus,permissions:[{...descriptor,details}]}),
    {runtimeEvents:true});
  assert.equal(c.applyRuntimeEvent(runtimeStatus(1)),'applied');
  const reconciling=c.reconcile();
  assert.equal(c.applyRuntimeEvent(runtimeStatus(2,publicStatus)),'applied');
  const hydrate=c.reconcile();
  older.resolve(runtimeStatus(1));
  await Promise.all([reconciling,hydrate]);
  assert.equal(requests,2);
  assert.equal(c.snapshot().permissions[0].details,details);
});
