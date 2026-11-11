'use strict';
// Execute the production integration against synthetic transports, without a CLI or device.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const shared=require('../src/supermonoagent/static/terminal-client.js');
const app=fs.readFileSync(path.join(__dirname,'../android/assets/mobile/app.js'),'utf8');
const block=app.split('// JOURNAL_INTEGRATION_START:')[1].split('// JOURNAL_INTEGRATION_END')[0].split('\n').slice(1).join('\n');
class Element{
  constructor(){this.children=[];this.hidden=true;this.value='';this.attributes={};this.listeners={};this.textContent='';}
  append(child){this.children.push(child);}
  replaceChildren(...children){this.children=children;}
  setAttribute(key,value){this.attributes[key]=value;}
  addEventListener(type,fn){this.listeners[type]=fn;}
  contains(node){return this.children.includes(node);}
  close(){this.open=false;}
  showModal(){this.open=true;}
}
function fixture(){
  const elements=new Map(),sockets=[],sent=[],timers=new Map(),viewers=[],notices=[];let sequence=0;
  const $=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const c={host:{id:'home',endpoint:'https://relay.example/base',code:'room-a'},generation:7,connected:true,capabilities:{conversation_journal:1},inventory:[{id:'s1',window:'w1',journal_id:'j1'},{id:'s2',window:'w2',journal_id:'j2'}]};
  class Socket{
    constructor(url){this.url=url;this.sent=[];this.readyState=1;sockets.push(this);}
    send(value){this.sent.push(JSON.parse(value));}
    close(){this.readyState=3;}
    reply(value){this.onmessage({data:JSON.stringify(value)});}
  }
  const context={console,TextEncoder,URL,Map,Set,Error,Promise,WebSocket:Socket,encoder:new TextEncoder(),$: $,document:{body:new Element(),createElement:()=>new Element(),getSelection:()=>null},
    selected:()=>c,connections:new Map([['home',c]]),hostTokens:new Map([['home','synthetic-token']]),safeEndpoint:h=>h.endpoint,
    endpoint:h=>{const u=new URL(h.endpoint);u.protocol=u.protocol==='https:'?'wss:':'ws:';u.pathname+='/ws';return u.toString();},
    active:'s1',paused:false,composing:false,composerSubmit:{pending:false},uploadJob:null,drafts:new Map(),sessionKey:()=>JSON.stringify(['home',context.active]),
    notice:text=>notices.push(text),updateComposer:()=>{},syncDraftFields:text=>{$('draft').value=text;$('quick-draft').value=text;},
    setTimeout:fn=>{timers.set(++sequence,fn);return sequence;},clearTimeout:id=>timers.delete(id),
    sendHost:(host,op,value)=>{sent.push({host,op,value});return host.connected;},
    C:{...shared,createConversationViewer:options=>{const v={options,opens:[],closed:0,refreshes:0,setContext(o){this.options=o;},async open(value){this.opens.push(value);},async refresh(){this.refreshes++;return true;},setCodeVisible(value){this.codeVisible=value;},close(){this.closed++;}};viewers.push(v);return v;}}
  };
  vm.createContext(context);vm.runInContext(block+'\nglobalThis.api={journalTarget,journalTargetCurrent,journalHostRequest,journalHostResponse,journalArchiveRequest,journalArchiveURL,journalRequest,journalQuote,journalCanFold,refreshJournalStatus,resetJournalContext,openJournal,showJournalRaw,loadJournalSources,preserveJournalArchive};',context);
  const reply=result=>{const request=sent.at(-1).value;context.api.journalHostResponse(c,{request_id:request.request_id,ok:true,result});};
  const tick=async()=>{for(let n=0;n<5;n++)await Promise.resolve();};
  return {api:context.api,context,c,$,sockets,sent,timers,viewers,notices,reply,tick};
}
const healthy={journal_id:'j1',process_generation:'pty-generation-4',last_seq:4,capture_state:'capturing',source:{bound:true,supported:true,healthy:true},storage:{available:true}};

test('OP24 uses negotiated capability and correlated bounded requests',async()=>{
  const f=fixture(),target=f.api.journalTarget(),waiting=f.api.journalRequest(target,'status',{session_id:'s1'});
  assert.equal(f.sent[0].op,24);assert.equal(f.sent[0].value.action,'status');assert.ok(f.sent[0].value.request_id.length<=64);
  f.api.journalHostResponse(f.c,{request_id:'not-the-request',ok:true,result:{wrong:true}});assert.equal(f.c.journalPending.size,1);
  f.reply(healthy);assert.equal((await waiting).journal_id,'j1');assert.equal(f.c.journalPending.size,0);
});
test('host response after process connection or selected terminal change is rejected',async()=>{
  for(const change of [f=>{f.c.generation++;},f=>{f.context.active='s2';},f=>{f.context.hostTokens.set('home','rotated-token');},f=>{f.c.inventory[0].journal_id='new-pty';}]){
    const f=fixture(),waiting=f.api.journalRequest(f.api.journalTarget(),'read_page',{journal_id:'j1'});change(f);f.reply({events:[]});await assert.rejects(waiting,{code:'stale_target'});
  }
});
test('old host never receives OP24; archived reads use same relay authority and private hello',async()=>{
  const f=fixture();f.c.capabilities={};f.c.connected=false;
  const result=f.api.journalRequest(f.api.journalTarget(),'list',{}),ws=f.sockets[0];
  assert.equal(f.sent.length,0);assert.equal(ws.url,'wss://relay.example/journals/ws');assert.ok(!ws.url.includes('synthetic-token'));
  ws.onopen();assert.deepEqual(ws.sent[0],{op:'hello',protocol:'sma-journal-1',role:'viewer',token:'synthetic-token',room:'room-a'});
  ws.reply({op:'hello',protocol:'sma-journal-1',ok:true});await f.tick();
  assert.equal(ws.sent[1].action,'list');assert.equal(ws.sent[1].op,'request');assert.ok(!JSON.stringify(ws.sent[1]).includes('token'));
  ws.reply({op:'response',request_id:ws.sent[1].request_id,ok:true,result:{journals:[{journal_id:'sealed-archive'}]}});
  assert.equal((await result).journals[0].journal_id,'sealed-archive');
});
test('archive fallback refuses insecure remote, missing relay room and all write actions',async()=>{
  for(const edit of [f=>{f.c.host.endpoint='http://relay.example';},f=>{f.c.host.code='';}]){
    const f=fixture();edit(f);f.c.capabilities={};await assert.rejects(f.api.journalRequest(f.api.journalTarget(),'list',{}),{code:'unsupported'});assert.equal(f.sockets.length,0);
  }
  const f=fixture();f.c.capabilities={};await assert.rejects(f.api.journalRequest(f.api.journalTarget(),'bind_source',{source_ref:'opaque'}),{code:'unsupported'});assert.equal(f.sockets.length,0);
});
test('offline archive stays readable across failed terminal retries while quote generation stays strict',async()=>{
  const f=fixture();f.c.connected=false;const target=f.api.journalTarget();target.archive=true;
  const first=f.api.journalRequest(target,'read_page',{journal_id:'j1'}),ws=f.sockets[0];ws.onopen();ws.reply({op:'hello',protocol:'sma-journal-1',ok:true});await f.tick();
  f.c.generation++;assert.equal(f.api.preserveJournalArchive(f.c),true);
  ws.reply({op:'response',request_id:ws.sent[1].request_id,ok:true,result:{events:[],next_cursor:'next'}});assert.equal((await first).next_cursor,'next');
  const next=f.api.journalRequest(target,'read_page',{journal_id:'j1',cursor:'next'});await f.tick();ws.reply({op:'response',request_id:ws.sent[2].request_id,ok:true,result:{events:[],next_cursor:null}});assert.equal((await next).next_cursor,null);
  assert.equal(f.api.journalQuote(target,'old target quote',{}),false);assert.equal(f.sent.length,0);
  f.context.hostTokens.set('home','rotated');assert.equal(f.api.preserveJournalArchive(f.c),false);await assert.rejects(f.api.journalRequest(target,'list',{}),{code:'stale_target'});
});
test('settings archive chooser lists closed records without borrowing the active journal ID',async()=>{
  const f=fixture();f.$('settings-dialog').children.find(button=>button.id==='journal-archives').onclick();await f.tick();
  assert.equal(f.viewers[0].opens[0].journalId,undefined);assert.equal(f.viewers[0].opens[0].projection,'history');
});
test('archive disconnect and authentication failure reject pending reads without exposing errors',async()=>{
  for(const deny of [false,true]){
    const f=fixture();f.c.connected=false;
    const result=f.api.journalRequest(f.api.journalTarget(),'list',{}),ws=f.sockets[0];ws.onopen();
    if(deny)ws.reply({op:'hello',ok:false,error:'synthetic-token'});
    else{ws.reply({op:'hello',protocol:'sma-journal-1',ok:true});await f.tick();ws.onclose();}
    await assert.rejects(result,error=>error.code==='disconnected'&&!error.message.includes('synthetic-token'));
  }
});
test('quote appends original selection and provenance to current draft, never touches transport',()=>{
  const f=fixture(),target=f.api.journalTarget(),selection='中文😀\nline two';f.$('draft').value='existing draft';
  assert.equal(f.api.journalQuote(target,selection,{journalId:'archive',observedAt:'2026-11-11'}),true);
  assert.ok(f.$('draft').value.startsWith('existing draft\n\n'));assert.ok(f.$('draft').value.includes('> 中文😀\n> line two'));assert.ok(f.$('draft').value.includes('archive'));
  assert.equal(f.$('quick-draft').value,f.$('draft').value);assert.equal(f.sent.length,0);assert.equal(f.sockets.length,0);
  const saved=f.$('draft').value;f.context.active='s2';assert.equal(f.api.journalQuote(target,'must refuse',{}),false);assert.equal(f.$('draft').value,saved);
});
test('quote rejects stale auth, busy editors, and oversized selection without truncation or send',()=>{
  for(const change of [f=>{f.context.composing=true;},f=>{f.context.composerSubmit.pending=true;},f=>{f.context.uploadJob={};},f=>{f.context.hostTokens.set('home','new-token');},f=>{f.c.generation++;}]){
    const f=fixture(),target=f.api.journalTarget();f.$('draft').value='keep';change(f);assert.equal(f.api.journalQuote(target,'selection',{}),false);assert.equal(f.$('draft').value,'keep');assert.equal(f.sent.length,0);
  }
  const f=fixture();f.$('draft').value='keep';assert.equal(f.api.journalQuote(f.api.journalTarget(),'x'.repeat(65536),{}),false);assert.equal(f.$('draft').value,'keep');assert.ok(f.notices[0].includes('未截断'));assert.equal(f.sent.length,0);
});
test('fold requires bound, verified, healthy capture and available local storage',()=>{
  const f=fixture();assert.equal(f.api.journalCanFold(healthy),true);
  for(const manifest of [{...healthy,source:{bound:false,supported:true,healthy:true}},{...healthy,source:{bound:true,supported:false,healthy:true}},{...healthy,source:{bound:true,supported:true,healthy:false}},{...healthy,capture_state:'suspended'},{...healthy,storage:{available:false}},{...healthy,local_error:'disk_full'}])assert.equal(f.api.journalCanFold(manifest),false);
});
test('CodeOFF uses independent read-only viewer and restores raw without terminal operations',async()=>{
  const f=fixture(),pending=f.api.refreshJournalStatus(true);f.reply(healthy);await pending;
  assert.equal(f.$('journal-reading').hidden,false);assert.equal(f.$('journal-code').attributes['aria-pressed'],'false');assert.equal(f.viewers[0].options.inline,true);assert.equal(f.viewers[0].opens[0].projection,'history');assert.equal(f.viewers[0].codeVisible,false);
  f.api.showJournalRaw(true);assert.equal(f.$('journal-reading').hidden,true);assert.equal(f.$('journal-code').attributes['aria-pressed'],'true');assert.equal(f.sent.length,1);
});
test('unsupported source keeps raw visible with an explicit explanation',async()=>{
  const f=fixture(),pending=f.api.refreshJournalStatus(true);f.reply({...healthy,source:{bound:true,supported:false,healthy:false}});await pending;
  assert.equal(f.$('journal-reading').hidden,true);assert.equal(f.viewers.length,0);assert.ok(f.notices[0].includes('尚未证明'));
});
test('new journal sequence refreshes existing inline reader without reopening its history page',async()=>{
  const f=fixture();let pending=f.api.refreshJournalStatus(true);f.reply(healthy);await pending;
  const viewer=f.viewers[0];pending=f.api.refreshJournalStatus();f.reply({...healthy,last_seq:5});await pending;
  assert.equal(viewer.opens.length,1);assert.equal(viewer.refreshes,1);assert.equal(f.viewers.length,1);assert.equal(f.$('journal-reading').hidden,false);
  pending=f.api.refreshJournalStatus();f.reply({...healthy,last_seq:5});await pending;assert.equal(viewer.refreshes,1);
});
test('raw terminal escape wins over pending live refresh and failed refresh is retried without reopen',async()=>{
  const f=fixture();let pending=f.api.refreshJournalStatus(true);f.reply(healthy);await pending;
  const viewer=f.viewers[0];viewer.refresh=async()=>false;
  pending=f.api.refreshJournalStatus();f.reply({...healthy,last_seq:5});await pending;assert.equal(viewer.opens.length,1);
  let release;viewer.refresh=()=>new Promise(resolve=>release=resolve);
  pending=f.api.refreshJournalStatus();f.reply({...healthy,last_seq:5});await f.tick();assert.equal(typeof release,'function');
  f.api.showJournalRaw(true);release(true);await pending;assert.equal(f.$('journal-reading').hidden,true);assert.equal(f.$('journal-code').attributes['aria-pressed'],'true');
});
test('original terminal escape wins over a pending inline page',async()=>{
  const f=fixture();let release;
  const page=new Promise(resolve=>release=resolve);
  f.context.C.createConversationViewer=()=>({setContext(){},open:()=>page,setCodeVisible(){},close(){}});
  const pending=f.api.refreshJournalStatus(true);f.reply(healthy);await f.tick();
  f.api.showJournalRaw(true);release();await pending;
  assert.equal(f.$('journal-reading').hidden,true);assert.equal(f.$('journal-code').attributes['aria-pressed'],'true');
});
test('source picker binds only opaque choice with authoritative process generation',async()=>{
  const f=fixture();f.$('journal-provider').value='codex';f.$('settings-dialog').children.find(button=>button.id==='journal-source').onclick();
  const loading=f.api.loadJournalSources();f.reply(healthy);await f.tick();
  assert.equal(f.sent.at(-1).value.action,'list_sources');assert.equal(f.sent.at(-1).value.params.provider,'codex');
  f.reply({sources:[{source_ref:'opaque-choice',provider:'codex',provider_session_id:'exact-root',provider_version:'0.167',supported:true}]});await loading;
  const binding=f.$('journal-sources').children[0].onclick();
  const request=f.sent.at(-1).value;assert.equal(request.action,'bind_source');assert.equal(request.params.source_ref,'opaque-choice');assert.equal(request.params.terminal_generation,'pty-generation-4');assert.ok(!('path' in request.params));
  f.reply({journal_id:'j1'});await f.tick();f.reply(healthy);await binding;
});
test('HTML retains both bottom records buttons and raw parser remains outside journal integration',()=>{
  const html=fs.readFileSync(path.join(__dirname,'../android/assets/mobile/index.html'),'utf8');
  assert.match(html,/id="journal-history"[^>]*>历史记录/);assert.match(html,/id="journal-conversation"[^>]*>对话记录/);
  assert.match(html,/id="journal-reading"[^>]*hidden/);assert.match(html,/id="screen" aria-label="远端终端"/);
  assert.ok(!/\b(?:input|paste|resize|reset|write)\(/.test(block));
});
