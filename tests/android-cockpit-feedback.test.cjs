'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const C=require('../src/supermonoagent/static/terminal-client.js');
const app=fs.readFileSync(path.join(__dirname,'../android/assets/mobile/app.js'),'utf8');
const block=app.split('// MOBILE_FEEDBACK_QA_START')[1].split('// MOBILE_FEEDBACK_QA_END')[0];

function fixture({native=false,profileChange={},controller=true}={}){
  const profile={endpoint:'ws://127.0.0.1:18824/hub/view',room:'qa-room',project:'test',host_installation_id:'install',...profileChange};
  const input={type:'',checked:false},draft={value:'已有草稿'},settings={append(){}},label={append(){}};
  const listeners={},calls=[];
  const session={id:'session',window:'win',title:'同名窗口',provider:'codex',process_generation:'process',
    journal_id:'journal',interaction_mode:native?'native':'managed'};
  const source={connected:true,socket:{readyState:1},generation:7,cockpitInstallationId:'install',capabilities:{cockpit_feedback:1},inventory:[session]};
  const attempt={attempt_id:'attempt',window_id:'win',terminal_session_id:'session',generation:'process',host_installation_id:'install',
    tool:'codex',account:'account',role:'primary',journal_id:'journal'};
  const context={C,encoder:new TextEncoder(),term:{},paused:false,composing:false,uploadJob:null,modes:new Map(),uncertainDrafts:new Map(),uncertainModelCommands:new Map(),Event,
    document:{hidden:false,createElement:tag=>tag==='input'?input:label,createTextNode:t=>t},
    window:{SuperMonoCockpitFeedback:controller?{open(){}}:null,
      screen:{orientation:{addEventListener(name,fn){listeners.orientation=fn;}}},
      addEventListener(name,fn){listeners[name]=fn;},dispatchEvent(e){calls.push([e.type]);}},
    $:id=>id==='settings-dialog'?settings:draft,
    managedID:s=>typeof s==='string'&&s.length>0,managedTargetFields:['window_id','session_id','journal_id','process_generation'],
    selected:()=>source,managedSession:()=>session,
    managedTarget:()=>({host:source,room:'qa-room',endpoint:'wss://source/ws',token:'synthetic-source',generation:source.generation,
      window_id:session.window,session_id:session.id,process_generation:session.process_generation,journal_id:session.journal_id,interaction_mode:session.interaction_mode}),
    journalTargetCurrent:t=>t.generation===source.generation,managedCurrent:t=>source.connected&&t.generation===source.generation&&session.interaction_mode==='managed',
    journalContextKey:t=>t.generation+' '+t.process_generation,journalError:code=>Object.assign(Error(code),{code}),
    managedRPC:async(t,name,params)=>{calls.push([name,params]);return {fixture:true};},
    submissionPending:()=>false,attachmentsPending:()=>false,sessionKey:()=>'test',
    syncDraftFields:text=>{draft.value=text;},setMode:(focus,open)=>calls.push(['edit',focus,open])};
  vm.createContext(context);
  vm.runInContext(block+'\nthis.make=createMobileFeedbackQa;',context);
  const qa=context.make(profile);
  const enable=()=>{input.checked=true;input.onchange();return qa.select('test','ws://127.0.0.1:18824/hub/view');};
  return {qa,enable,profile,input,source,session,attempt,context,calls,draft,listeners};
}

test('ordinary/mobile configurations cannot expose a feedback host; QA opt-in is per document and scope',()=>{
  assert.equal(fixture({controller:false}).qa,null);
  assert.equal(fixture({profileChange:{token:'unexpected'}}).qa,null);
  assert.equal(fixture({profileChange:{project:'*'}}).qa,null);
  assert.equal(fixture({profileChange:{endpoint:'ws://public.example/hub/view'}}).qa,null);
  const f=fixture();assert.ok(f.qa);assert.equal(f.qa.select(f.profile.project,f.profile.endpoint),null);
  const host=f.enable();assert.ok(host);assert.equal(f.qa.select('another',f.profile.endpoint),null);
  assert.equal(f.qa.select('test','ws://localhost:18824/hub/view'),null);
  assert.equal(host.describe({...f.attempt,generation:'another'}),null);
  assert.equal(host.describe({...f.attempt,host_installation_id:'another'}),null);
  assert.equal(host.describe({...f.attempt,terminal_session_id:'another'}),null);
  assert.equal(host.describe({...f.attempt,window_id:'another'}),null);
  f.listeners.orientation();
  assert.equal(f.calls.at(-1)[0],'sma-cockpit-context-invalidated');
  f.source.capabilities.cockpit_feedback=0;
  assert.equal(host.describe(f.attempt).canSend,false);
  f.qa.disable();assert.equal(f.input.checked,false);
  assert.equal(f.qa.select('test',f.profile.endpoint),null);
});

test('only a captured managed source receives an exact correlated OP24; stale and forged captures are rejected',async()=>{
  const f=fixture(),host=f.enable(),captured=host.describe(f.attempt);
  const fields={nonce:'test-nonce',feedback:{project:'test',tool:'codex',role:'primary',account:'account',attempt_id:'attempt',host_installation_id:'install'}};
  await assert.rejects(host.request({...captured},'submit_cockpit_feedback',fields),{code:'stale_target'});
  await assert.rejects(host.request(captured,'submit_managed',fields),{code:'stale_target'});
  await assert.rejects(host.request(captured,'submit_cockpit_feedback',{...fields,feedback:{...fields.feedback,project:'other'}}),{code:'stale_target'});
  assert.equal(f.calls.length,0);
  await host.request(captured,'cockpit_feedback_status',fields);
  assert.equal(f.calls[0][0],'cockpit_feedback_status');
  for(const [k,v] of Object.entries({window_id:'win',session_id:'session',process_generation:'process',journal_id:'journal'}))
    assert.equal(f.calls[0][1][k],v);
  f.listeners.orientation();
  await assert.rejects(host.request(captured,'submit_cockpit_feedback',fields),{code:'stale_target'});
  const fresh=host.describe(f.attempt);
  f.source.generation++;f.qa.checkBinding();
  await assert.rejects(host.request(fresh,'submit_cockpit_feedback',fields),{code:'stale_target'});
  assert.equal(f.calls.filter(([op])=>op==='submit_cockpit_feedback').length,0);
});

test('matched ordinary terminal gets an appended local draft only; stale or oversized requests preserve it',()=>{
  const f=fixture({native:true}),host=f.enable(),ref=host.describe(f.attempt);
  assert.equal(ref.canSend,false);assert.equal(host.prepareDraft(ref,'x'.repeat(65536)),false);
  assert.equal(f.draft.value,'已有草稿');
  assert.equal(host.prepareDraft(ref,'待确认内容'),true);assert.equal(f.draft.value,'已有草稿\n待确认内容');
  assert.deepEqual(f.calls,[['edit',true,true]]);
  const before=f.draft.value;
  f.session.process_generation='new';f.qa.checkBinding();
  assert.equal(host.prepareDraft(ref,'旧窗口'),false);
  assert.equal(f.draft.value,before);
});
