const test=require('node:test');
const assert=require('node:assert/strict');
const C=require('../src/supermonoagent/static/terminal-client.js');

test('connection meter counts UTF8 text and binary bytes and uses elapsed time',()=>{
  const meter=new C.ConnectionMeter(100);
  meter.sent('中');meter.sent(new Uint8Array(1021));meter.received(new ArrayBuffer(4096));
  assert.deepEqual(meter.sample(2100),{tx:512,rx:2048});
  assert.deepEqual(meter.sample(3100),{tx:0,rx:0});
  meter.sent('old');meter.reset(4100);assert.deepEqual(meter.sample(5100),{tx:0,rx:0});
  assert.equal(meter.rtt,null);assert.equal(meter.pongAt,null);
});
test('quality uses fresh RTT and phase, never idle traffic or stale RTT',()=>{
  const meter=new C.ConnectionMeter(0);
  assert.equal(meter.quality(0,'connected').label,'等待测量');
  meter.pong(45,1000);assert.equal(meter.quality(2000,'connected').bars,4);
  meter.pong(500,3000);assert.equal(meter.quality(3500,'connected').bars,2);
  assert.equal(meter.quality(40000,'connected').label,'响应超时');
  assert.equal(meter.quality(40000,'auth-failed').bars,0);
  meter.reset(41000);assert.equal(meter.quality(42000,'connected').label,'等待测量');
  assert.equal(C.formatRate(0),'0 B/s');assert.equal(C.formatRate(2048),'2.0 KiB/s');
});
test('effective terminal dimensions support narrow panes and cap extreme requests',()=>{
  assert.deepEqual(C.terminalSize(8,3),{cols:8,rows:3});
  assert.deepEqual(C.terminalSize(0,0),{cols:2,rows:1});
  assert.deepEqual(C.terminalSize(999,999),{cols:500,rows:200});
});
test('real xterm wraps ASCII and wide text without changing input bytes',async()=>{
  const {Terminal}=require('../src/supermonoagent/static/xterm.js');
  for(const text of ['a'.repeat(53),'\u4e2d'.repeat(32)]){
    const term=new Terminal({cols:20,rows:8});
    try{
      await new Promise(resolve=>term.write(new TextEncoder().encode(text),resolve));
      assert.ok(term.buffer.active.cursorY>0);
      assert.equal(term.buffer.active.getLine(1).isWrapped,true);
      const input=C.terminalInput('\x1b[200~'+text+'\x1b[201~');
      assert.equal(input.includes('\r')||input.includes('\n'),false);
      assert.equal(new TextDecoder().decode(C.sessionPayload(C.unpack(C.targetFrame(17,'12345678',input)).payload).bytes),input);
    }finally{term.dispose();}
  }
});
