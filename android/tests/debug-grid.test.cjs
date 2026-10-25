const {test}=require('node:test');
const assert=require('node:assert/strict');
const {debugGridPage,createDebugPreview,debugPrompt}=require('../assets/mobile/mobile-core.js');
const {Terminal}=require('../../src/supermonoagent/static/xterm.js');

test('grid pages clamp after removal and keep the same session on different computers',()=>{
  const rows=Array.from({length:19},(_,i)=>({key:JSON.stringify(['pc'+i,'same-session'])}));
  assert.deepEqual(debugGridPage([...rows,rows[0]],0,9).items,rows.slice(0,9));
  assert.deepEqual(debugGridPage(rows,1,16).items,rows.slice(16));
  assert.equal(debugGridPage(rows.slice(0,3),8,9).page,0);
  assert.equal(debugGridPage([],1,16).pages,1);
  assert.equal(debugGridPage(rows,0).items.length,16);
  assert.equal(debugGridPage(rows,0,99).items.length,16);
});

test('preview waits for canonical dimensions and parses split Unicode, cursor moves and erases',async()=>{
  const seen=[],created=[];
  const preview=createDebugPreview({createTerminal:options=>{const t=new Terminal(options);created.push(t);return t;},onText:text=>seen.push(text)});
  const bytes=Buffer.from('\x1b[2J\x1b[1;1H旧提示\x1b[1;1H\x1b[2K等待确认：继续？\x1b[3;1H任务运行中');
  preview.push(bytes.subarray(0,31),true);preview.push(bytes.subarray(31));
  assert.equal(created.length,0);
  preview.setGrid({cols:158,rows:43});
  await new Promise(resolve=>setTimeout(resolve,80));
  assert.equal(created[0].cols,158);
  assert.match(seen.at(-1),/等待确认：继续？/);
  assert.match(seen.at(-1),/任务运行中/);
  assert.doesNotMatch(seen.at(-1),/旧提示|\x1b/);
  preview.push(Buffer.from('\x1b[2J\x1b[H已完成'),true);
  await new Promise(resolve=>setTimeout(resolve,50));
  assert.equal(seen.at(-1),'已完成');
  preview.dispose();
});

test('preview limits queued bytes and ignores callbacks after disposal',()=>{
  let errors=0,callback,notifications=0;
  const preview=createDebugPreview({limit:32,createTerminal:()=>({cols:80,rows:24,write:(bytes,done)=>{callback=done;},reset(){},dispose(){}}),onText:()=>notifications++,onError:()=>errors++});
  preview.push(new Uint8Array(33),true);
  assert.equal(errors,1);
  preview.setGrid({cols:80,rows:24});preview.push(Buffer.from('test'));
  preview.dispose();callback?.();
  assert.equal(notifications,0);
  assert.equal(preview.push(Buffer.from('ignored')),false);
});

test('prompt text is bounded and extracted from the visible buffer only',()=>{
  const lines=Array.from({length:100},(_,i)=>({translateToString:()=>i<90?'old history':('line '+i+' '+'.'.repeat(1000))}));
  const text=debugPrompt({rows:10,buffer:{active:{baseY:90,getLine:i=>lines[i]}}});
  assert.ok(text.length<=512);
  assert.doesNotMatch(text,/old history/);
  assert.match(text,/line 99/);
});
