const {test}=require('node:test');
const assert=require('node:assert/strict');
const {createScreenHistory,visibleBufferLine}=require('../assets/mobile/mobile-core.js');
const {Terminal}=require('../../src/supermonoagent/static/xterm.js');

const frame=text=>({cols:20,rows:2,lines:[text,'']});

test('history text keeps wide cells aligned and never reveals concealed characters',async()=>{
  const terminal=new Terminal({cols:30,rows:2});
  await new Promise(done=>terminal.write('中A\x1b[8msecret\x1b[28mB',done));
  assert.equal(visibleBufferLine(terminal.buffer.active.getLine(0),30),'中A      B');
  terminal.dispose();
});

test('screen history preserves blank lines, Unicode and immutable snapshots',()=>{
  let clock=10;
  const history=createScreenHistory({now:()=>clock});
  const source={cols:12,rows:3,lines:['中文 🙂','', 'last']};
  assert.equal(history.record(source),true);
  source.lines[0]='changed outside';
  assert.equal(history.record({cols:12,rows:3,lines:['中文 🙂','', 'last']}),false);
  const saved=history.top();
  assert.deepEqual(saved.lines,['中文 🙂','','last']);
  assert.equal(saved.timestamp,10);
  assert.ok(Object.isFrozen(saved)&&Object.isFrozen(saved.lines));
  assert.equal(history.length,1);
  assert.equal(history.bytes,new TextEncoder().encode(JSON.stringify(saved)).length);
});

test('previous, next, top and bottom navigate chronologically and return to live',()=>{
  let clock=0;
  const history=createScreenHistory({now:()=>clock,minInterval:0});
  assert.equal(history.previous(),null);
  assert.equal(history.current,null);
  assert.equal(history.atTop,true);
  assert.equal(history.atBottom,true);
  for(const text of ['first','second','latest']){clock++;history.record(frame(text));}
  assert.equal(history.previous().lines[0],'second');
  assert.equal(history.atBottom,false);
  assert.equal(history.previous().lines[0],'first');
  assert.equal(history.atTop,true);
  assert.equal(history.previous().lines[0],'first');
  assert.equal(history.next().lines[0],'second');
  assert.equal(history.next().lines[0],'latest');
  assert.equal(history.next(),null);
  assert.equal(history.atBottom,true);
  assert.equal(history.top().lines[0],'first');
  assert.equal(history.bottom(),null);
});

test('a sole recorded frame can be frozen and then returned to live',()=>{
  const history=createScreenHistory({now:()=>0});
  history.record(frame('only'));
  assert.equal(history.previous().lines[0],'only');
  assert.equal(history.atTop,true);
  assert.equal(history.next(),null);
});

test('throttling retains the first frame and the freshest tail without unbounded growth',()=>{
  let clock=0;
  const history=createScreenHistory({now:()=>clock,minInterval:750});
  history.record(frame('first'));
  clock=10;history.record(frame('second'));
  clock=20;history.record(frame('third'));
  clock=30;history.record(frame('fourth'));
  assert.equal(history.length,2);
  assert.equal(history.previous().lines[0],'first');
  assert.equal(history.next().lines[0],'fourth');
  history.bottom();clock=800;history.record(frame('checkpoint'));
  assert.equal(history.length,3);
});

test('incoming output never replaces a frame being read or jumps back to live',()=>{
  let clock=0;
  const history=createScreenHistory({now:()=>clock,minInterval:750});
  history.record(frame('first'));clock=10;history.record(frame('second'));
  history.top();history.next();
  const pinned=history.current;
  clock=20;history.record(frame('third'));
  clock=30;history.record(frame('fourth'));
  assert.equal(history.current,pinned);
  assert.equal(history.current.lines[0],'second');
  assert.equal(history.atBottom,false);
  assert.equal(history.next().lines[0],'fourth');
});

test('frame limits evict old unpinned frames and keep a valid reading cursor',()=>{
  let clock=0;
  const history=createScreenHistory({now:()=>clock,minInterval:0,maxFrames:3});
  history.record(frame('first'));clock++;history.record(frame('second'));
  history.top();const pinned=history.current;
  for(const text of ['third','fourth','fifth']){clock++;history.record(frame(text));}
  assert.equal(history.length,3);
  assert.equal(history.current,pinned);
  assert.equal(history.next().lines[0],'fourth');
  assert.equal(history.next().lines[0],'fifth');
  const single=createScreenHistory({now:()=>clock,maxFrames:1});
  single.record(frame('old'));single.top();clock++;single.record(frame('new'));
  assert.equal(single.length,1);
  assert.equal(single.current.lines[0],'new');
  assert.equal(single.atTop,true);
});

test('byte budgets stay bounded and oversized records do not erase existing history',()=>{
  let clock=0;
  const history=createScreenHistory({now:()=>clock,minInterval:0,maxBytes:200});
  for(let index=0;index<40;index++){clock++;history.record(frame('row '+index));assert.ok(history.bytes<=200);}
  const previous=history.top(),length=history.length,bytes=history.bytes;
  assert.equal(history.record({cols:500,rows:1,lines:['字'.repeat(500)]}),false);
  assert.equal(history.current,previous);
  assert.equal(history.length,length);
  assert.equal(history.bytes,bytes);
});

test('malformed frames and ANSI controls are rejected; clearing releases data and cursor',()=>{
  const history=createScreenHistory({now:()=>10});
  for(const value of [null,{},frame(7),{cols:1,rows:2,lines:['','']},
    {cols:501,rows:1,lines:['']},{cols:20,rows:201,lines:[]},
    {cols:20,rows:2,lines:['only one']},{cols:20,rows:2,lines:new Array(2)},{cols:2,rows:1,lines:['12345']},
    frame('\x1b[31mred'),frame('a\nb'),frame('a\tb')]){
    assert.equal(history.record(value),false);
  }
  history.record(frame('valid'));const oldID=history.top().id;history.clear();
  assert.equal(history.length,0);assert.equal(history.bytes,0);assert.equal(history.current,null);
  assert.equal(history.atBottom,true);
  history.record(frame('after clear'));assert.ok(history.top().id>oldID);
  assert.throws(()=>createScreenHistory({maxFrames:0}));
  assert.throws(()=>createScreenHistory({maxBytes:-1}));
  assert.throws(()=>createScreenHistory({minInterval:NaN}));
});

test('grid changes are distinct frames and a backward clock cannot reorder timestamps',()=>{
  let clock=100;
  const history=createScreenHistory({now:()=>clock,minInterval:0});
  history.record({cols:20,rows:2,lines:['same','']});
  clock=50;history.record({cols:30,rows:2,lines:['same','']});
  assert.equal(history.length,2);
  assert.equal(history.top().cols,20);
  assert.equal(history.next().cols,30);
  assert.equal(history.current.timestamp,100);
  clock=NaN;assert.equal(history.record(frame('bad clock')),false);
});

test('a byte budget too small for both frames clamps a pinned reader to valid remaining data',()=>{
  let clock=0;
  const history=createScreenHistory({now:()=>clock,minInterval:0,maxBytes:90});
  history.record(frame('old'));history.top();clock++;
  assert.equal(history.record(frame('new')),true);
  assert.equal(history.length,1);
  assert.equal(history.current.lines[0],'new');
  assert.ok(history.bytes<=90);
});
