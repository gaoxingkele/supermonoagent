const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const client=require('../src/supermonoagent/static/terminal-client.js');
const source=fs.readFileSync(require.resolve('../src/supermonoagent/static/workspace.js'),'utf8');

function harness(){
  const host={id:'host-a',generation:7,ws:{},connected:true,supported:true,capabilities:{conversation_journal:1},
    config:{token:'synthetic-only',relay:'wss://relay.fixture/ws',code:'room-a',authMode:'manual'}};
  const pane={sid:'pane-a',host,generation:7},panes=new Map([[pane.sid,pane]]),hosts=new Map([[host.id,host]]),sent=[],params=new URLSearchParams();
  const scope={URL,Promise,Error,Map,Uint8Array,JSON,TextEncoder,encoder:new TextEncoder(),setTimeout,clearTimeout,
    params,C:client,location:{origin:'https://workspace.fixture'},panes,hosts,active:pane.sid,
    livePane:p=>panes.get(p?.sid)===p&&p.generation===p.host.generation,
    accountFor:()=>null,send:(p,op,value)=>{sent.push({p,op,value});return true;},window:{}};
  vm.createContext(scope);
  vm.runInContext(source.slice(source.indexOf('  // Journal reads'),source.indexOf('  function connectionName(host)'))+
    '\nthis.api={journalTarget,journalCurrent,journalArchiveCurrent,journalRequest,receiveJournal,centralJournalURL,quoteJournal,refreshPaneJournal};',scope);
  return {host,pane,scope,sent,params,...scope.api};
}

test('OP24 requires negotiated capability and correlates only the captured host',async()=>{
  const h=harness(),target=h.journalTarget(h.pane);
  const request=h.journalRequest(target,'status',{session_id:'session-a'});
  assert.equal(h.sent[0].op,24);assert.equal(h.sent[0].value.action,'status');
  h.receiveJournal({...h.host},{request_id:h.sent[0].value.request_id,ok:true,result:{wrong:true}});
  h.receiveJournal(h.host,{request_id:h.sent[0].value.request_id,ok:true,result:{journal_id:'owned'}});
  assert.equal((await request).journal_id,'owned');
  h.host.capabilities={};
  await assert.rejects(h.journalRequest(target,'list',{}),/unavailable/);
  assert.equal(h.sent.length,1);
});

test('late RPC and quotes reject changed auth or pane generation',async()=>{
  const h=harness(),target=h.journalTarget(h.pane);
  const request=h.journalRequest(target,'status',{});
  h.host.config.token='rotated-synthetic';
  h.receiveJournal(h.host,{request_id:h.sent[0].value.request_id,ok:true,result:{journal_id:'must-not-read'}});
  await assert.rejects(request,/target_changed/);
  let quotes=0;h.scope.window.SuperMonoTerminalComposer={appendQuote:()=>{quotes++;return true;}};
  assert.equal(h.quoteJournal(target,'owned',{}),false);assert.equal(quotes,0);
  const fresh=h.journalTarget(h.pane);assert.equal(h.quoteJournal(fresh,'owned',{}),true);
  h.scope.active='another-pane';assert.equal(h.quoteJournal(fresh,'owned',{}),false);assert.equal(quotes,1);
  h.scope.active=h.pane.sid;h.host.generation++;
  assert.equal(h.quoteJournal(fresh,'owned',{}),false);
});

test('central archives require explicit same-authority exact path and no query credentials',()=>{
  const h=harness(),target=h.journalTarget(h.pane);
  assert.throws(()=>h.centralJournalURL(target),/unavailable/);
  for(const url of ['wss://other.fixture/journals/ws','wss://relay.fixture:444/journals/ws',
    'wss://relay.fixture/ws','wss://relay.fixture/journals/ws?token=synthetic',
    'wss://relay.fixture/journals/ws#fragment','wss://u:p@relay.fixture/journals/ws','ws://relay.fixture/journals/ws']){
    h.params.set('journal_url',url);assert.throws(()=>h.centralJournalURL(target),/unavailable/);
  }
  h.params.set('journal_url','wss://relay.fixture/journals/ws');
  assert.equal(h.centralJournalURL(target),'wss://relay.fixture/journals/ws');
  h.host.generation++;assert.equal(h.journalCurrent(target),false);
  assert.equal(h.journalArchiveCurrent(target),true);
  h.host.config.code='changed-room';assert.equal(h.journalArchiveCurrent(target),false);
});

test('explicit central reader authenticates in hello and survives host reconnect without PTY RPC',async()=>{
  const h=harness(),target=h.journalTarget(h.pane),wire=[];
  h.params.set('journal_url','wss://relay.fixture/journals/ws');
  h.host.connected=false;h.host.generation++;
  h.scope.WebSocket=class {
    constructor(url){assert.equal(url,'wss://relay.fixture/journals/ws');Promise.resolve().then(()=>this.onopen());}
    close(){}
    send(text){const message=JSON.parse(text);wire.push(message);
      Promise.resolve().then(()=>this.onmessage({data:JSON.stringify(message.op==='hello'?{op:'hello',ok:true}:
        {op:'response',request_id:message.request_id,ok:true,result:{journals:[{journal_id:'archived-owned'}],next_cursor:null}})}));
    }
  };
  const result=await h.journalRequest(target,'list',{limit:50});
  assert.equal(result.journals[0].journal_id,'archived-owned');assert.equal(h.sent.length,0);
  assert.equal(wire[0].op,'hello');assert.equal(wire[0].protocol,'sma-journal-1');assert.equal(wire[0].role,'viewer');
  assert.equal(wire[0].token,'synthetic-only');assert.equal(wire[0].room,'room-a');
  assert.equal(wire[1].op,'request');assert.equal(wire[1].action,'list');assert.equal(wire[1].token,undefined);
  await assert.rejects(h.journalRequest(target,'bind_source',{}),/unavailable/);
});

test('health polling refreshes changed sequence once and discards stale viewer completions',async()=>{
  const h=harness();let refreshes=0,finish;
  h.pane.journalReading=true;h.pane.journalManifest={journal_id:'owned',last_seq:1};
  h.pane.journalInline={refresh:()=>{refreshes++;return new Promise(resolve=>{finish=resolve;});}};
  const manifest={journal_id:'owned',last_seq:2,source:{healthy:true,bound:true,supported:true},storage:{available:true},capture_state:'capturing'};
  const pending=h.refreshPaneJournal(h.pane);await h.refreshPaneJournal(h.pane);
  assert.equal(h.sent.length,1);
  h.receiveJournal(h.host,{request_id:h.sent[0].value.request_id,ok:true,result:manifest});
  await new Promise(setImmediate);assert.equal(refreshes,1);
  let currentRefreshes=0;h.pane.journalInline={refresh:async()=>{currentRefreshes++;return true;}};finish(true);await pending;
  assert.equal(h.pane.journalManifest.last_seq,1);assert.equal(h.pane.journalHealthPending,false);
  const next=h.refreshPaneJournal(h.pane);
  h.receiveJournal(h.host,{request_id:h.sent[1].value.request_id,ok:true,result:manifest});await next;
  assert.equal(h.pane.journalManifest.last_seq,2);
  const unchanged=h.refreshPaneJournal(h.pane);
  h.receiveJournal(h.host,{request_id:h.sent[2].value.request_id,ok:true,result:manifest});await unchanged;
  assert.equal(refreshes,1);assert.equal(currentRefreshes,1);
});
