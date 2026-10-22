const {test}=require('node:test');
const assert=require('node:assert/strict');
const M=require('../android/assets/mobile/mobile-core.js');

test('session dots follow live output, idle timeout and disconnection',()=>{
  assert.equal(M.sessionState(true,undefined,100),'waiting');
  assert.equal(M.sessionState(true,0,0),'refreshing');
  assert.equal(M.sessionState(true,100,1899),'refreshing');
  assert.equal(M.sessionState(true,100,1900),'waiting');
  assert.equal(M.sessionState(false,1900,1900),'disconnected');
  assert.equal(M.agentType({title:'Work Codex',shell:'powershell'}),'Codex');
  assert.equal(M.agentType({title:'Task',shell:'claude'}),'Claude');
  assert.equal(M.agentType({title:'Build',shell:'cmd'}),'cmd');
});
test('recovery uses a close code accepted by the actual WebSocket API',()=>{
  const ws=new WebSocket('ws://127.0.0.1:9');ws.addEventListener('error',()=>{});
  assert.doesNotThrow(()=>M.closeForRecovery(ws,'output backlog'));
  const source=require('node:fs').readFileSync(require('node:path').join(__dirname,'../android/assets/mobile/app.js'),'utf8');
  assert.ok(!source.includes('.close(1013'));
});
test('small echo is immediate; busy writes coalesce and stay bounded',()=>{
  const writes=[],frames=[];let done;
  const q=M.createWriter({write:(bytes,reset,cb)=>{writes.push([...bytes]);done=cb;},schedule:cb=>{frames.push(cb);return frames.length;},cancel:()=>{},limit:8,chunk:8,onOverflow:()=>writes.push('overflow')});
  assert.equal(q.push(Uint8Array.of(1)),true);assert.deepEqual(writes,[[1]]);
  q.push(Uint8Array.of(2));q.push(Uint8Array.of(3));done();frames.shift()();
  assert.deepEqual(writes,[[1],[2,3]]);assert.equal(q.push(new Uint8Array(9)),false);assert.equal(writes.at(-1),'overflow');q.dispose();
});
test('1000 queued fragments retain every byte using bounded parse batches',()=>{
  const frames=[],callbacks=[],output=[];let calls=0;
  const q=M.createWriter({write:(bytes,reset,done)=>{calls++;assert.ok(bytes.length<=32768);output.push(...bytes);callbacks.push(done);},schedule:cb=>{frames.push(cb);return frames.length;},cancel:()=>{}});
  for(let i=0;i<1000;i++)assert.equal(q.push(new Uint8Array(512).fill(i%251)),true);
  while(callbacks.length||frames.length){while(callbacks.length)callbacks.shift()();if(frames.length)frames.shift()();}
  assert.equal(output.length,512000);assert.ok(calls<20);
  for(let i=0;i<1000;i++)assert.equal(output[i*512],i%251);
  assert.equal(q.pending,0);
});
test('reset is an ordered boundary and dead callbacks cannot revive queues',()=>{
  let done;const writes=[],frames=[];
  const q=M.createWriter({write:(b,r,cb)=>{writes.push({bytes:[...b],reset:r});done=cb;},schedule:cb=>{frames.push(cb);return 1;},cancel:()=>{}});
  q.push(Uint8Array.of(1));q.push(Uint8Array.of(2),true);q.push(Uint8Array.of(3));done();frames.shift()();
  assert.deepEqual(writes[1],{bytes:[2,3],reset:true});q.dispose();done();assert.equal(frames.length,0);
});
test('touch threshold and fractional row accumulation separate tap from drag',()=>{
  const g=M.createGesture(10);g.start(5,30);assert.deepEqual(g.move(5,25,10),{rows:0,drag:false});
  assert.deepEqual(g.move(5,8,10),{rows:2,drag:true});assert.equal(g.end(),false);
  g.start(5,30);g.move(5,28,10);assert.equal(g.end(),true);
});
test('composer and control keys preserve Unicode; never send preedit',()=>{
  assert.equal(M.controlKey('c'),'\x03');assert.equal(M.controlKey('中'),'中');
  assert.equal(M.composerAction({key:'Enter',shiftKey:false,isComposing:true}),'none');
  assert.equal(M.composerAction({key:'Enter',shiftKey:true,isComposing:false}),'newline');
  assert.equal(M.composerAction({key:'Enter',shiftKey:false,isComposing:false}),'send');
  assert.equal(M.alternateScroll(1,false),'\x1b[B');assert.equal(M.alternateScroll(-1,true),'\x1bOA');
});

test('Android committed Enter and beforeinput submit without sending preedit or Shift+Enter',()=>{
  assert.equal(M.composerAction({key:'Enter',keyCode:229,isComposing:false}),'send');
  assert.equal(M.composerAction({key:'Enter',keyCode:229,isComposing:false},true),'none');
  assert.equal(M.composerAction({type:'beforeinput',inputType:'insertLineBreak',isComposing:false}),'send');
  assert.equal(M.composerAction({type:'beforeinput',inputType:'insertParagraph',isComposing:false}),'send');
  assert.equal(M.composerAction({type:'beforeinput',inputType:'insertLineBreak',isComposing:true}),'none');
  assert.equal(M.composerAction({type:'beforeinput',inputType:'insertCompositionText',isComposing:false}),'none');
  assert.equal(M.composerAction({key:'Enter',shiftKey:true}),'newline');
});

test('composer submission separates paste from exactly one Enter and blocks duplicate submissions',()=>{
  const events=[],timers=[],states=[];
  const sender=M.createComposerSubmitter({schedule:(fn,ms)=>{timers.push({fn,ms});return timers.length;},cancel:()=>{},onPending:p=>states.push(p)});
  const callbacks={paste:text=>events.push(text),enter:()=>events.push('\r'),isCurrent:()=>true};
  assert.equal(sender.submit('中文 draft',callbacks),true);
  assert.deepEqual(events,['中文 draft']);assert.equal(sender.pending,true);
  assert.equal(sender.submit('duplicate',callbacks),false);
  assert.ok(timers[0].ms>=100&&timers[0].ms<=250,'only a calibrated short paste-burst separation');
  timers[0].fn();timers[0].fn();
  assert.deepEqual(events,['中文 draft','\r']);assert.equal(sender.pending,false);
  assert.deepEqual(states,[true,false]);
});

test('pending Enter cannot follow a switched host, session or reconnected socket',()=>{
  for(const reason of ['host','session','generation','pause','drop']){
    let current=true;const events=[],timers=[];
    const sender=M.createComposerSubmitter({schedule:fn=>{timers.push(fn);return 1;},cancel:id=>events.push('cancel:'+id)});
    sender.submit('already pasted',{paste:text=>events.push(text),enter:()=>events.push('\r'),isCurrent:()=>current});
    current=false;if(['pause','drop'].includes(reason))sender.cancel();
    timers[0]();assert.ok(!events.includes('\r'),reason);assert.equal(sender.pending,false);
    sender.cancel();
  }
});

test('a stalled application must echo the paste before the single Enter is armed',()=>{
  const events=[],timers=[];let echoed=false;
  const sender=M.createComposerSubmitter({schedule:(fn,ms)=>{const timer={fn,ms,canceled:false};timers.push(timer);return timer;},cancel:t=>{t.canceled=true;}});
  sender.submit('/quit',{paste:text=>events.push(text),enter:()=>events.push('\r'),isCurrent:()=>true,echo:()=>echoed});
  assert.deepEqual(events,['/quit']);
  assert.ok(!timers.some(t=>t.ms===200),'sending paste does not start the Enter timer');
  sender.output();assert.ok(!timers.some(t=>t.ms===200),'unrelated output is not an acknowledgment');
  echoed=true;sender.output();
  const enter=timers.find(t=>t.ms===200);assert.ok(enter);
  assert.ok(timers.find(t=>t.ms===5000).canceled);
  enter.fn();enter.fn();assert.deepEqual(events,['/quit','\r']);
});

test('echo matching uses newly rendered editor text and recognizes collapsed large pastes',()=>{
  let lines=['old /quit','prompt '],cursorX=7,cursorY=1;
  const term={rows:30,buffer:{get active(){return {baseY:0,cursorY,cursorX,getLine:i=>lines[i]===undefined?null:{translateToString:(trim,start=0,end)=>lines[i].slice(start,end)}};}}};
  const echo=M.createComposerEcho(term,'/quit');
  assert.equal(echo(),false,'an old transcript match cannot acknowledge a new paste');
  lines[1]='prompt /quit';cursorX=12;assert.equal(echo(),true);
  const chinese=M.createComposerEcho(term,'中文\n命令');lines=['prompt 中文','命令'];cursorX=2;assert.equal(chinese(),true);
  const continuation=M.createComposerEcho(term,'first\nsecond');lines=['PS> first','>> second'];cursorX=9;assert.equal(continuation(),true,'continuation prompts may appear between pasted lines');
  lines=['>> second','PS> '];cursorX=4;
  const scrolled=M.createComposerEcho(term,'second');lines=['PS> second'];cursorY=0;cursorX=10;assert.equal(scrolled(),true,'a previous copy scrolling out must not mask the new cursor tail');
  const large=M.createComposerEcho(term,'x'.repeat(2000));lines=['prompt [Pasted Content 2000 chars]'];cursorY=0;cursorX=34;assert.equal(large(),true);
});

test('missing paste acknowledgment times out without emitting Enter',()=>{
  const events=[],timers=[];
  const sender=M.createComposerSubmitter({schedule:(fn,ms)=>{timers.push({fn,ms});return timers.length;},cancel:()=>{}});
  sender.submit('secretless test',{paste:()=>{},enter:()=>events.push('enter'),isCurrent:()=>true,echo:()=>false,onTimeout:()=>events.push('timeout')});
  timers.find(t=>t.ms===5000).fn();assert.deepEqual(events,['timeout']);assert.equal(sender.pending,false);
  sender.submit('canceled test',{paste:()=>{},enter:()=>events.push('enter'),isCurrent:()=>true,echo:()=>false,onTimeout:()=>events.push('late timeout')});
  sender.cancel();sender.output();timers.at(-1).fn();assert.deepEqual(events,['timeout']);
});
test('grid changes remain ordered boundaries during a busy output stream',()=>{
  const writes=[],frames=[],callbacks=[];
  const q=M.createWriter({write:(b,r,done,grid)=>{writes.push({bytes:[...b],grid});callbacks.push(done);},schedule:cb=>{frames.push(cb);return frames.length;},cancel:()=>{}});
  q.push(Uint8Array.of(1),false,()=>{},{cols:120,rows:30});
  q.push(Uint8Array.of(2),false,()=>{},{cols:120,rows:30});
  q.push(Uint8Array.of(3),false,()=>{},{cols:40,rows:20});
  q.push(Uint8Array.of(4),false,()=>{},{cols:40,rows:20});
  while(callbacks.length||frames.length){while(callbacks.length)callbacks.shift()();if(frames.length)frames.shift()();}
  assert.deepEqual(writes,[{bytes:[1],grid:{cols:120,rows:30}},{bytes:[2],grid:{cols:120,rows:30}},{bytes:[3,4],grid:{cols:40,rows:20}}]);
  assert.equal(q.idle,true);
});
