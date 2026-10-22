const test = require('node:test');
const assert = require('node:assert/strict');
const client = require('../src/supermonoagent/static/terminal-client.js');
const {presets: tabThemes} = require('../src/supermonoagent/static/tab-themes.js');

test('seven window palettes all have strong text contrast', () => {
  const luminance = hex => hex.match(/[0-9a-f]{2}/gi).map(value => parseInt(value,16)/255)
    .map(value => value<=0.04045?value/12.92:((value+0.055)/1.055)**2.4)
    .reduce((sum,value,index)=>sum+value*[0.2126,0.7152,0.0722][index],0);
  assert.equal(tabThemes.length,7);
  assert.equal(new Set(tabThemes.map(theme=>theme.id)).size,7);
  for(const theme of tabThemes){
    const values=[luminance(theme.background),luminance(theme.foreground)].sort((a,b)=>a-b);
    assert.ok((values[1]+0.05)/(values[0]+0.05)>=7,theme.label);
  }
});
test('targeted input preserves session identity and UTF8 bytes', () => {
  const frame = client.targetFrame(17, 'session-a', '中文\u001b[A');
  const unpacked = client.unpack(frame);
  const decoded = client.sessionPayload(unpacked.payload);
  assert.equal(unpacked.op, 17);
  assert.equal(decoded.session, 'session-a');
  assert.equal(new TextDecoder().decode(decoded.bytes), '中文\u001b[A');
});
test('wire framing matches Python pack golden bytes and rejects malformed lengths', () => {
  assert.deepEqual([...client.pack(10,new Uint8Array([97,98,99]))], [4,0,0,0,10,97,98,99]);
  const output=client.unpack(new Uint8Array([4,0,0,0,3,97,98,99]));
  assert.equal(output.op,3);assert.deepEqual([...output.payload],[97,98,99]);
  assert.throws(()=>client.unpack(new Uint8Array([1,0,0,0])), /Short/);
  assert.throws(()=>client.unpack(new Uint8Array([2,0,0,0,3])), /length/);
  assert.throws(()=>client.pack(3,new Uint8Array(262144)), /Oversized/);
});
test('binary output stays bytes across split unicode sequences', () => {
  const data = new Uint8Array([115,0,0xe4,0xb8]);
  assert.deepEqual([...client.sessionPayload(data).bytes], [0xe4,0xb8]);
  assert.throws(() => client.sessionPayload(new Uint8Array([1,2])), /session/);
});
test('relay URL validation strips credentials and selects websocket scheme', () => {
  assert.equal(client.socketURL('https://example.test', 'http://local'), 'wss://example.test/ws');
  assert.equal(client.socketURL('', 'https://local.test'), 'wss://local.test/ws');
  assert.throws(() => client.socketURL('javascript:alert(1)', 'http://local'), /URL/);
  assert.throws(() => client.socketURL('ws://user:pass@example.test', 'http://local'), /credentials/);
});
test('retry is bounded and renderer choice is constrained', () => {
  assert.equal(client.retryDelay(0), 500);
  assert.equal(client.retryDelay(50), 15000);
  assert.equal(client.rendererName('webgl'), 'webgl');
  assert.equal(client.rendererName('invalid'), 'auto');
});
test('redraw size bounce stays within daemon limits and restores the size', () => {
  assert.deepEqual(client.redrawSizes(100, 30), [{cols:99,rows:30},{cols:100,rows:30}]);
  assert.deepEqual(client.redrawSizes(500, 200), [{cols:499,rows:200},{cols:500,rows:200}]);
  assert.deepEqual(client.redrawSizes(20, 5), [{cols:19,rows:5},{cols:20,rows:5}]);
  assert.deepEqual(client.redrawSizes(8, 3), [{cols:7,rows:3},{cols:8,rows:3}]);
  assert.deepEqual(client.redrawSizes(700, 300), [{cols:499,rows:200},{cols:500,rows:200}]);
  assert.deepEqual(client.redrawSizes(20,5,20),[{cols:21,rows:5},{cols:20,rows:5}]);
});
test('device answers and replay cannot become Windows shell input', () => {
  assert.equal(client.terminalInput('\x1b[?1;2c'),'');
  assert.equal(client.terminalInput('\x1b[>0;276;0c\x1b[24;80R\x1b[0n'),'');
  assert.equal(client.terminalInput('\x1b[?1;2cecho ok\r'),'echo ok\r');
  for(const key of ['\x1b[A','\x1b[B','\x1b[C','\x1b[D','\x1b','\x03','\t','你好','\x1b[200~paste\x1b[201~'])assert.equal(client.terminalInput(key),key);
  assert.equal(client.terminalInput('\x1b[A',true),'');
});

test('bounded snapshots start at a complete terminal token', () => {
  const prefix=new TextEncoder().encode('38;49m broken prefix\x1b[0mFOOTER');
  const bytes=new Uint8Array(262144-10);bytes.set(prefix);bytes.fill(32,prefix.length);
  const aligned=client.snapshotBytes(bytes,'12345678');
  assert.equal(new TextDecoder().decode(aligned.subarray(0,11)),'\x1b[0mFOOTER ');
  const complete=new TextEncoder().encode('normal beginning\r\n');
  assert.equal(client.snapshotBytes(complete,'12345678'),complete);
});
test('bundled xterm preserves split UTF8 and split ANSI state as byte writes', async () => {
  const {Terminal}=require('../src/supermonoagent/static/xterm.js');
  const term=new Terminal({cols:40,rows:5});
  try {
    term.write(new Uint8Array([0xe4,0xb8]));
    term.write(new Uint8Array([0xad,0xe6,0x96,0x87,0x1b,0x5b]));
    await new Promise(resolve=>term.write(new TextEncoder().encode('31mR\x1b[0m'),resolve));
    const line=term.buffer.active.getLine(0);
    assert.equal(line.translateToString(true),'中文R');
    assert.equal(line.getCell(0).getWidth(),2);
    assert.equal(line.getCell(4).getFgColor(),1);
  } finally {term.dispose();}
});
test('real xterm device query answers are filtered while arrow input remains valid', async () => {
  const {Terminal}=require('../src/supermonoagent/static/xterm.js');
  const term=new Terminal({cols:40,rows:5});const replies=[];
  term.onData(data=>replies.push(data));
  try {
    await new Promise(resolve=>term.write('\x1b[c\x1b[6n',resolve));
    assert.ok(replies.length>=2);
    assert.equal(replies.map(data=>client.terminalInput(data)).join(''),'');
    assert.equal(client.terminalInput('\x1b[A'),'\x1b[A');
  } finally {term.dispose();}
});
test('closing a view releases every resource once and keeps other open parsers', () => {
  const calls=[];
  const closed={ready:true,observer:{disconnect:()=>calls.push('observer')},webgl:{dispose:()=>calls.push('webgl')},term:{dispose:()=>calls.push('terminal')},el:{remove:()=>calls.push('element')}};
  const other={term:{dispose:()=>assert.fail('other session parser must remain alive')}};
  const panes=new Map([['closed',closed],['other',other]]);
  assert.equal(client.disposePane(panes,'closed'),true);
  assert.deepEqual(calls,['observer','webgl','terminal','element']);
  assert.equal(closed.ready,false);
  assert.equal(closed.webgl,null);
  assert.equal(panes.has('closed'),false);
  assert.equal(panes.get('other'),other);
  assert.equal(client.disposePane(panes,'closed'),false);
  assert.equal(calls.length,4);
});
