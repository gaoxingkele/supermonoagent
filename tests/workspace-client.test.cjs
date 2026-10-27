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
test('update hints validate public metadata without trusting a download', () => {
  assert.equal(client.updateHintRevision({op:'ui_update',revision:'0.2.50',sequence:50}),'0.2.50');
  for(const message of [{op:'ui_update',revision:'../secret',sequence:50},{op:'ui_update',revision:'ok',sequence:-1},{op:'ui_update',revision:'ok',sequence:1.5},{op:'other',revision:'ok',sequence:50},null])assert.equal(client.updateHintRevision(message),'');
  const fs=require('node:fs');
  for(const path of ['../src/supermonoagent/static/workspace.js','../android/assets/mobile/app.js']){
    const source=fs.readFileSync(require.resolve(path),'utf8');
    assert.match(source,/(?:message|m)\.op==='ui_update'\)/);
    assert.match(source,/Promise\.resolve\(window\.NativeUpdates\.check\(\)\)/);
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

test('known relay migrates to fixed WSS and other insecure network URLs fail closed',()=>{
  assert.equal(client.socketURL('ws://117.50.48.22:8817/ws','http://localhost'),'wss://117.50.48.22/ws');
  assert.equal(client.secureEndpoint('http://117.50.48.22:8817'),'https://117.50.48.22/');
  assert.equal(client.socketURL('http://127.0.0.1:9123','http://localhost'),'ws://127.0.0.1:9123/ws');
  for(const value of ['ws://other.example/ws','http://192.168.1.8:8817','ws://@117.50.48.22:8817/ws','ws://117.50.48.22:8817/ws?token=x'])assert.throws(()=>client.socketURL(value,'http://localhost'));
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
  for(const reply of ['\x1b]10;rgb:eeee/f2f2/ffff\x1b\\','\x1b]11;rgb:1212/1717/2323\x07','\x1b]12;rgb:aa/bb/cc\x1b\\','\x1b]4;7;rgb:aa/bb/cc\x1b\\'])assert.equal(client.terminalInput(reply),'');
  assert.equal(client.terminalInput('\x1b]10;rgb:eeee/f2f2/ffff\x1b\\/quit\r'),'/quit\r');
  const clipboard='\x1b[200~literal \x1b]10;rgb:aa/bb/cc\x1b\\\x1b[201~';
  assert.equal(client.terminalInput(clipboard),clipboard);
  assert.equal(client.terminalInput('echo rgb:aa/bb/cc'),'echo rgb:aa/bb/cc');
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
  // New backends supply a restored SGR prefix and an aligned tail already.
  const styled=new Uint8Array(bytes.length);styled.set(new TextEncoder().encode('\x1b[0;31mRED\n'));
  assert.equal(client.snapshotBytes(styled,'12345678',true),styled);
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

test('desktop history parser validates metadata and preserves raw bytes',()=>{
  const context={v:2,kind:'resume',cols:100,rows:30,epoch:2,partial:false,reason:'',end_position:105,stream_id:'0123456789abcdef01234567'};
  const metadata=new TextEncoder().encode(JSON.stringify(context)),prefix=new TextEncoder().encode('12345678\0');
  const payload=new Uint8Array(prefix.length+2+metadata.length+5);payload.set(prefix);payload[prefix.length]=metadata.length&255;payload[prefix.length+1]=metadata.length>>8;
  payload.set(metadata,prefix.length+2);payload.set(new TextEncoder().encode('hello'),prefix.length+2+metadata.length);
  const parsed=client.historyPayload(payload);
  assert.equal(parsed.session,'12345678');assert.deepEqual(parsed.context,context);assert.equal(new TextDecoder().decode(parsed.bytes),'hello');
  const malformed=payload.slice();malformed[prefix.length]=1;malformed[prefix.length+1]=0;
  assert.throws(()=>client.historyPayload(malformed));
});

test('desktop history cursor rejects gaps and trims duplicate output',()=>{
  const cursor={position:100,epoch:2,stream_id:'0123456789abcdef01234567'};
  const context={v:2,kind:'output',end_position:105,epoch:2,stream_id:cursor.stream_id};
  const bytes=new TextEncoder().encode('hello');
  assert.equal(new TextDecoder().decode(client.historyFrameBytes(context,bytes,cursor).bytes),'hello');
  assert.equal(new TextDecoder().decode(client.historyFrameBytes(context,bytes,{...cursor,position:103}).bytes),'lo');
  assert.equal(client.historyFrameBytes(context,bytes,{...cursor,position:105}).bytes.length,0);
  assert.equal(client.historyFrameBytes(context,bytes,{...cursor,position:99}),null);
  assert.equal(client.historyFrameBytes(context,bytes,{...cursor,epoch:3}),null);
  assert.equal(client.historyFrameBytes({...context,kind:'snapshot'},bytes,null).reset,true);
});
