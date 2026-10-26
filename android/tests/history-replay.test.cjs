const {test}=require('node:test');
const assert=require('node:assert/strict');
const {Terminal}=require('../../src/supermonoagent/static/xterm.js');
const {safeHistoryReplay,historyPayload,terminalInput}=require('../assets/mobile/mobile-core.js');
const encode=text=>new TextEncoder().encode(text);
const decode=bytes=>new TextDecoder().decode(bytes);
const mixed='\x1b[31m'+('\x1b[54;1H旧尺寸片段'.repeat(4));

test('view focus notifications cannot become terminal input, while keys and bracketed text are unchanged',()=>{
  assert.equal(terminalInput('\x1b[I'),'');assert.equal(terminalInput('\x1b[O'),'');
  for(const text of ['中文','\r','\x1bOQ','\x1b[A','\x1b[200~literal \x1b[I\x1b[201~'])assert.equal(terminalInput(text),text);
});

test('mixed geometry replays start at a complete repaint and keep rendition, without guessing old columns',async()=>{
  const bytes=encode(mixed+'\x1b[2J\x1b[H当前记录\r\n下一行');
  const result=safeHistoryReplay(bytes,{cols:113,rows:31});
  assert.equal(result.recovered,true);
  assert.equal(result.partial,true);
  assert.doesNotMatch(decode(result.bytes),/旧尺寸|54;1H/);
  const terminal=new Terminal({cols:113,rows:31});
  await new Promise(done=>terminal.write(result.bytes,done));
  assert.match(terminal.buffer.active.getLine(0).translateToString(true),/^当前记录/);
  assert.equal(terminal.buffer.active.getLine(0).getCell(0).getFgColor(),1);
  terminal.dispose();
});

test('uniform replay, deliberate huge cursor clamps and one outlier are not silently cut',()=>{
  for(const text of ['\x1b[54;1Hone outlier\x1b[2J\x1b[Hnew','\x1b[999;999H'.repeat(4)+'\x1b[2J\x1b[Hnormal','a\r\nb']){
    const bytes=encode(text),result=safeHistoryReplay(bytes,{cols:113,rows:31});
    assert.equal(result.bytes,bytes);
    assert.equal(result.recovered,false);
  }
});

test('ambiguous history is labelled partial when there is no safe repaint or unsupported state',()=>{
  assert.equal(safeHistoryReplay(encode(mixed+'\x1b[31;1Htail'),{cols:113,rows:31}).reason,'no_safe_repaint');
  for(const state of ['\x1b[5;20r','\x1b[?1049h','\x1b[?6h']){
    const result=safeHistoryReplay(encode(state+mixed+'\x1b[2J\x1b[Htail'),{cols:113,rows:31});
    assert.equal(result.recovered,false);assert.equal(result.partial,true);
  }
  assert.equal(safeHistoryReplay(encode('\x1b[?1049h'+mixed+'\x1bctail'),{cols:113,rows:31}).recovered,true);
});

test('OSC/DCS contents and unterminated control strings cannot forge geometry or clear boundaries',()=>{
  for(const wrapper of [text=>'\x1b]0;'+text+'\x07',text=>'\x1bP'+text+'\x1b\\',text=>'\x1b]0;'+text]){
    const bytes=encode(wrapper(mixed+'\x1b[2J\x1b[Hfake'));
    assert.equal(safeHistoryReplay(bytes,{cols:113,rows:31}).bytes,bytes);
  }
});

test('partial context leaves plain log output available without fabricating a new baseline',()=>{
  const bytes=encode('log one\r\nlog two');
  const result=safeHistoryReplay(bytes,{cols:80,rows:24},true);
  assert.equal(result.bytes,bytes);assert.equal(result.partial,true);assert.equal(result.recovered,false);
});

const metadata={v:1,kind:'snapshot',cols:113,rows:31,epoch:2,partial:true,reason:'resize_boundary'};
function payload(context=metadata,tail='中文 payload'){
  const header=encode(JSON.stringify(context)),session=encode('session\0'),bytes=new Uint8Array(session.length+2+header.length+encode(tail).length);
  bytes.set(session);new DataView(bytes.buffer).setUint16(session.length,header.length,true);bytes.set(header,session.length+2);bytes.set(encode(tail),session.length+2+header.length);return bytes;
}
test('context envelope validates its complete bounded schema and preserves UTF8 bytes',()=>{
  const result=historyPayload(payload());assert.equal(result.session,'session');assert.deepEqual(result.context,metadata);assert.equal(decode(result.bytes),'中文 payload');
  for(const bad of [{...metadata,v:2},{...metadata,rows:999},{...metadata,cols:true},{...metadata,epoch:-1},{...metadata,partial:false},{...metadata,reason:'a path / secret'},{...metadata,extra:'ignored?'}])assert.throws(()=>historyPayload(payload(bad)));
  const short=payload().subarray(0,9);assert.throws(()=>historyPayload(short));
  const oversized=payload();oversized[8]=255;oversized[9]=255;assert.throws(()=>historyPayload(oversized));
});
