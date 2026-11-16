const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require.resolve('../src/supermonoagent/static/workspace.js'),'utf8');
function fixture(){
  const host={connected:true,supported:true,capabilities:{conversation_journal:1,managed_conversation:1}};
  const target={window_id:'w',session_id:'s',process_generation:'g',journal_id:'j'};
  const session={raw:'s',interaction_mode:'managed',process_generation:'g',journal_id:'j'};
  const pane={sid:'pane',raw:'s',host,ready:true,term:{options:{disableStdin:false},textarea:{readOnly:false}}};
  const sent=[],scope={Map,Set,Promise,Error,JSON,Uint8Array,TextEncoder,encoder:new TextEncoder(),setTimeout,clearTimeout,
    C:{},sessionInfo:()=>({session,win:{raw:'w'}}),livePane:p=>p===pane,panes:new Map([['pane',pane]]),
    journalCurrent:()=>scope.current,journalTarget:()=>({pane,host}),journalPending:new Map(),journalSequence:0,
    send:(p,op,value)=>{sent.push({p,op,value});return true;},current:true};
  vm.createContext(scope);
  vm.runInContext(source.slice(source.indexOf('  // Managed mode controls'),source.indexOf('  function connectionName(host)'))+
    '\nthis.api={isManagedPane,managedTuple,setPaneInput,managedRequest,managedOutcomeText};',scope);
  return {host,target,session,pane,sent,scope,...scope.api};
}
test('managed readonly remains independent of ready and native panes stay writable',()=>{
  const f=fixture();f.setPaneInput(f.pane);assert.equal(f.pane.term.options.disableStdin,true);assert.equal(f.pane.term.textarea.readOnly,true);
  f.session.interaction_mode='native';f.setPaneInput(f.pane);assert.equal(f.pane.term.options.disableStdin,false);assert.equal(f.pane.term.textarea.readOnly,false);
});
test('managed controls require exact mode/tuple/capability and never use archive fallback',async()=>{
  const f=fixture();delete f.host.capabilities.managed_conversation;
  await assert.rejects(f.managedRequest(f.pane,f.target,'managed_status',f.target),/unavailable/);assert.equal(f.sent.length,0);
  f.host.capabilities.managed_conversation=1;f.session.process_generation='changed';
  await assert.rejects(f.managedRequest(f.pane,f.target,'managed_status',f.target),/target_changed/);
  f.session.process_generation='g';f.scope.current=false;
  await assert.rejects(f.managedRequest(f.pane,f.target,'managed_status',f.target),/target_changed/);assert.equal(f.sent.length,0);
});

test('previous managed outcome is separate from current pending or transport uncertainty',()=>{
  const f=fixture(),base={last_outcome:{submission_id:'previous',outcome:'failed',reason:'authentication_failed',has_final:false}};
  assert.match(f.managedOutcomeText(base),/请检查所选工具的接入配置与连接/);
  assert.doesNotMatch(f.managedOutcomeText(base),/未确认|助手|登录/);
  assert.equal(f.managedOutcomeText({...base,pending:{nonce:'new'}}),'');
  assert.equal(f.managedOutcomeText({...base,error:'status_unavailable'}),'');
  assert.equal(f.managedOutcomeText({...base,active_submission:{submission_id:'new'}}),'');
  assert.match(f.managedOutcomeText({last_outcome:{outcome:'blocked'}}),/原输入已保留/);
  assert.match(f.managedOutcomeText({last_outcome:{outcome:'ambiguous'}}),/执行结果待核对/);
  assert.match(f.managedOutcomeText({last_outcome:{outcome:'interrupted'}}),/已中断/);
  assert.match(f.managedOutcomeText({last_outcome:{outcome:'completed',has_final:false}}),/未发现可确认结论/);
  assert.equal(f.managedOutcomeText({last_outcome:{outcome:'completed',has_final:true}}),'');
});
