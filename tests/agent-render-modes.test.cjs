const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const C=require('../src/supermonoagent/static/terminal-client.js');
const {Terminal}=require('../src/supermonoagent/static/xterm.js');
const directory=path.join(__dirname,'../src/supermonoagent/static/agent-modes');
const raw=id=>JSON.parse(fs.readFileSync(path.join(directory,id+'.json'),'utf8'));
const profile=id=>C.validateAgentProfile(raw(id),id);
const write=(t,text)=>new Promise(done=>t.write(text,done));

test('six independent files have distinct readable palettes and agent rules',()=>{
  assert.deepEqual(C.agentProfiles.map(p=>p.id),['codex','claude-code','grok','kimi','pi-dev','cursor-cli']);
  const profiles=C.agentProfiles.map(p=>profile(p.id));
  assert.equal(new Set(profiles.map(p=>p.theme.background)).size,6);
  assert.equal(new Set(profiles.map(p=>JSON.stringify(p.theme))).size,6);
  assert.equal(new Set(profiles.map(p=>JSON.stringify(p.tokens))).size,6);
  assert.equal(new Set(profiles.map(p=>p.tokens[2].pattern)).size,6);
  for(const p of profiles){assert.ok(Object.isFrozen(p)&&Object.isFrozen(p.tokens)&&Object.isFrozen(p.theme));assert.ok(p.tokens.length>=4);}
  assert.equal(C.commonAgentProfile.label,'自动');
});

test('profiles load individually with exact identity and bounded same-origin fetch',async()=>{
  const beforeFetch=globalThis.fetch,beforeLocation=globalThis.location;let calls=0;
  globalThis.location={href:'https://ui.example/workspace',origin:'https://ui.example'};
  try{
    globalThis.fetch=async(url,options)=>{calls++;assert.equal(options.credentials,'omit');assert.equal(options.redirect,'error');const id=new URL(url).pathname.split('/').pop().replace('.json','');return new Response(JSON.stringify(raw(id)));};
    for(const item of C.agentProfiles){const result=await C.loadAgentProfile('/agent-modes/'+item.id+'.json',item.id);assert.equal(result.id,item.id);}
    assert.equal(calls,6);
    await assert.rejects(C.loadAgentProfile('https://other.example/codex.json','codex'),/origin/);
    assert.equal(calls,6);
    await assert.rejects(C.loadAgentProfile('/agent-modes/kimi.json','codex'),/profile/);
    globalThis.fetch=async()=>new Response('X'.repeat(32769));
    await assert.rejects(C.loadAgentProfile('/agent-modes/codex.json','codex'),/large/);
    globalThis.fetch=async()=>new Response('{}',{headers:{'content-length':'32769'}});
    await assert.rejects(C.loadAgentProfile('/agent-modes/codex.json','codex'),/rejected/);
  }finally{globalThis.fetch=beforeFetch;if(beforeLocation===undefined)delete globalThis.location;else globalThis.location=beforeLocation;}
});

test('inference is precise for cursor CLI, pi.dev and explicit learned agents',()=>{
  for(const [session,title,id] of [
    [{shell:'codex'},'','codex'],[{shell:'claude --continue'},'','claude-code'],[{shell:'grok.exe'},'','grok'],
    [{shell:'kimi'},'','kimi'],[{shell:'pi'},'','pi-dev'],[{},'pi.dev coding','pi-dev'],
    [{shell:'C:\\bin\\cursor-agent.exe'},'','cursor-cli'],[{},'Cursor CLI','cursor-cli'],
    [{learnedAgent:'pi-dev',shell:'powershell'},'','pi-dev'],[{learnedAgent:'grok'},'renamed','grok'],
    [{agent:'kimi',title:'Codex'},'','kimi'],[{learnedAgent:'grok',agent:'codex'},'Kimi','grok'],
    [{shell:'powershell'},'cursor movement','auto'],[{shell:'cursor.exe'},'Cursor editor','auto'],
    [{shell:'python'},'Pi calculation','auto'],[{},'generic terminal','auto']
  ])assert.equal(C.agentModeFromSession(session,title),id,JSON.stringify(session)+' '+title);
});

test('invalid and oversized profiles fail closed before rendering',()=>{
  for(const mutate of [p=>p.id='other',p=>p.label='X'.repeat(65),p=>p.theme.foreground=p.theme.background,
    p=>p.theme.background='red',p=>p.theme.bad='#ffffff',p=>p.tokens=[],p=>p.tokens=Array(33).fill(p.tokens[0]),
    p=>p.tokens[0].pattern='X'.repeat(257),p=>p.tokens[0].flags='giy',p=>p.tokens[0].flags='gg',
    p=>p.tokens[0].pattern='(a+)+$',p=>p.tokens[0].pattern='(?=x)',p=>p.tokens[0].pattern='a*',
    p=>p.tokens[0].pattern='[',p=>p.tokens[0].color='url(https://other)',p=>p.extra=true]){
    const p=raw('codex');mutate(p);assert.throws(()=>C.validateAgentProfile(p,'codex'));
  }
});

test('highlight work is bounded for oversized grids and combining-cell content',()=>{
  let reads=0;
  const cell={getWidth:()=>1,getChars:()=> 'a',isFgDefault:()=>true,isBgDefault:()=>true,isInverse:()=>false,isInvisible:()=>false,isDim:()=>false};
  C.highlightSpans({getCell(){reads++;return cell;}},100000,profile('codex'));
  assert.equal(reads,500);
  assert.deepEqual(C.highlightSpans({getCell(){return {...cell,getChars:()=> 'a'.repeat(4097)};}},80),[]);
});

test('plain cells receive profile colors while native ANSI and Unicode stay intact',async()=>{
  const term=new Terminal({cols:80,rows:8,allowProposedApi:true});
  const {Unicode11Addon}=require('../src/supermonoagent/static/xterm-unicode11.js');
  term.loadAddon(new Unicode11Addon());term.unicode.activeVersion='11';
  try{
    const bytes=new TextEncoder().encode('中文😀 app.py 42 "text"\r\n\x1b[31mError 7\x1b[0m\r\n\x1b[44mapp.py\x1b[0m\r\n\x1b[7mapp.py\x1b[0m');
    for(const byte of bytes)await write(term,Uint8Array.of(byte));
    const buffer=term.buffer.active;
    for(const p of C.agentProfiles.map(p=>profile(p.id))){
      const spans=C.highlightSpans(buffer.getLine(0),term.cols,p);
      assert.ok(spans.some(span=>span.x===7&&span.width===6));
      for(let y=1;y<=3;y++)assert.deepEqual(C.highlightSpans(buffer.getLine(y),term.cols,p),[]);
    }
    assert.equal(buffer.getLine(0).translateToString(true),'中文😀 app.py 42 "text"');
    assert.equal(buffer.getLine(1).getCell(0).getFgColor(),1);
    assert.equal(buffer.getLine(0).getCell(7).isFgDefault(),true);
  }finally{term.dispose();}
});

test('CLI faint ghost suggestions retain native styling with the cursor before them',async()=>{
  const term=new Terminal({cols:80,rows:4,allowProposedApi:true});
  const suggestion='src/app.py 42 "next"';
  try{
    await write(term,'> \x1b[2m'+suggestion+'\x1b[0m\x1b['+suggestion.length+'D');
    const buffer=term.buffer.active,line=buffer.getLine(0),cell=line.getCell(2);
    assert.equal(!!cell.isDim(),true);assert.equal(cell.isFgDefault(),true);assert.equal(buffer.cursorX,2);
    for(const p of [C.commonAgentProfile,...C.agentProfiles.map(p=>profile(p.id))]){
      assert.deepEqual(C.highlightSpans(line,term.cols,p),[],p.id+' must not recolor native faint text');
    }
    assert.equal(line.translateToString(true),'> '+suggestion);assert.equal(buffer.cursorX,2);
    assert.equal(!!line.getCell(2).isDim(),true);
  }finally{term.dispose();}
});

test('explicit ANSI90/RGB gray suggestions remain untouched; ordinary white still highlights',async()=>{
  const term=new Terminal({cols:80,rows:5,allowProposedApi:true}),suggestion='src/app.py 42 "next"';
  try{
    for(const sgr of ['90','38;2;96;96;96','2;38;2;160;160;160'])await write(term,'> \x1b['+sgr+'m'+suggestion+'\x1b[0m\r\n');
    await write(term,'> '+suggestion+'\x1b['+suggestion.length+'D');
    const b=term.buffer.active;
    assert.equal(b.cursorX,2);
    for(const p of [C.commonAgentProfile,...C.agentProfiles.map(p=>profile(p.id))]){
      for(let y=0;y<3;y++)assert.deepEqual(C.highlightSpans(b.getLine(y),term.cols,p),[]);
      assert.ok(C.highlightSpans(b.getLine(3),term.cols,p).length>0,'plain white is not inferred to be a suggestion');
    }
    assert.equal(b.getLine(0).getCell(2).getFgColor(),8);
    assert.equal(b.getLine(1).getCell(2).getFgColor(),0x606060);
    assert.equal(b.getLine(2).getCell(2).getFgColor(),0xa0a0a0);
    assert.equal(!!b.getLine(2).getCell(2).isDim(),true);
  }finally{term.dispose();}
});

test('cursorKey follows real terminal DECCKM without touching Chinese ghost text or input filtering',async()=>{
  const term=new Terminal({cols:80,rows:4,allowProposedApi:true}),suggestion='继续完善接口 src/app.py 42';
  const expected={up:'A',down:'B',left:'D',right:'C'};
  try{
    await write(term,'> \x1b[2m'+suggestion+'\x1b[0m');
    await write(term,'\x1b['+(term.buffer.active.cursorX-2)+'D');
    assert.equal(term.buffer.active.cursorX,2);
    for(const [control,prefix,application] of [['','[',false],['\x1b[?1h','O',true],['\x1b[?1l','[',false]]){
      if(control)await write(term,control);
      assert.equal(term.modes.applicationCursorKeysMode,application);
      for(const [direction,letter] of Object.entries(expected)){
        const key=C.cursorKey(term,direction);assert.equal(key,'\x1b'+prefix+letter);assert.equal(C.terminalInput(key),key);
      }
      assert.equal(term.buffer.active.cursorX,2);assert.equal(term.buffer.active.getLine(0).translateToString(true),'> '+suggestion);
      assert.equal(!!term.buffer.active.getLine(0).getCell(2).isDim(),true);
    }
    assert.equal(C.cursorKey(undefined,'right'),'\x1b[C');
    for(const bad of ['end','ArrowRight','constructor','toString','__proto__','',null,{},3])assert.equal(C.cursorKey(term,bad),'');
    assert.equal(C.terminalInput('普通文本 plain text'),'普通文本 plain text');
    assert.deepEqual(C.highlightSpans(term.buffer.active.getLine(0),term.cols,profile('claude-code')),[]);
  }finally{term.dispose();}
});

test('setProfile disposes and repaints without PTY writes; dispose releases listeners and timers',async()=>{
  const actual=new Terminal({cols:80,rows:4});await write(actual,'Codex app.py 42');
  const events={},jobs=new Map(),decorations=[],markers=[];let next=0,listenerDisposals=0;
  const listen=name=>fn=>{events[name]=fn;return{dispose(){listenerDisposals++;}};};
  const term={cols:80,rows:4,buffer:{active:actual.buffer.active,onBufferChange:listen('buffer')},
    onWriteParsed:listen('write'),onScroll:listen('scroll'),onResize:listen('resize'),refresh(){},
    write(){throw Error('Renderer must not write terminal bytes');},paste(){throw Error('Renderer must not send input');},
    registerMarker(offset){const b=actual.buffer.active;const marker={line:b.baseY+b.cursorY+offset,isDisposed:false,dispose(){this.isDisposed=true;}};markers.push(marker);return marker;},
    registerDecoration(options){const d={options,isDisposed:false,dispose(){this.isDisposed=true;}};decorations.push(d);return d;}};
  const flush=()=>{while(jobs.size){const [id,fn]=jobs.entries().next().value;jobs.delete(id);fn();}};
  const h=C.createHighlighter(term,{profile:profile('codex'),schedule:fn=>{jobs.set(++next,fn);return next;},cancel:id=>jobs.delete(id)});
  try{
    flush();const original=decorations.slice();assert.ok(original.length);
    h.setProfile(profile('claude-code'));assert.ok(original.every(d=>d.isDisposed));flush();
    assert.ok(decorations.some(d=>!d.isDisposed&&d.options.foregroundColor===profile('claude-code').tokens[3].color));
    h.setEnabled(false);flush();assert.ok(decorations.every(d=>d.isDisposed));
    h.setEnabled(true);flush();markers.at(-1).isDisposed=true;events.write();flush();
    events.resize();assert.equal(jobs.size,1);h.dispose();h.dispose();
    assert.equal(jobs.size,0);assert.equal(listenerDisposals,4);assert.ok(decorations.every(d=>d.isDisposed));assert.ok(markers.every(m=>m.isDisposed));
    assert.equal(actual.buffer.active.getLine(0).translateToString(true),'Codex app.py 42');
  }finally{h.dispose();actual.dispose();}
});
