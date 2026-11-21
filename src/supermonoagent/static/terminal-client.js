/* Byte-only terminal framing. Shared by the workspace and its regression tests. */
(function (root) {
  'use strict';
  const encoder = new TextEncoder();
  // Decorate parsed cells instead of rewriting the PTY stream. In particular,
  // split UTF-8, SGR/OSC sequences and cursor-addressed TUI output stay intact.
  const agentProfiles=Object.freeze([
    ['codex','Codex'],['claude-code','Claude Code'],['grok','Grok'],
    ['kimi','Kimi'],['pi-dev','pi.dev'],['cursor-cli','Cursor CLI']
  ].map(([id,label])=>Object.freeze({id,label})));
  const profileIds=new Set(['common',...agentProfiles.map(item=>item.id)]);
  const compiledProfiles=new WeakMap();
  const themeKeys=new Set(['background','foreground','cursor','cursorAccent','selectionBackground','selectionInactiveBackground','selectionForeground',
    'black','red','green','yellow','blue','magenta','cyan','white','brightBlack','brightRed','brightGreen','brightYellow','brightBlue','brightMagenta','brightCyan','brightWhite']);
  function colorValue(value){if(typeof value!=='string'||!/^#[0-9a-f]{6}$/i.test(value))throw new Error('Invalid profile color');return value.toLowerCase();}
  function luminance(value){return value.slice(1).match(/../g).map(h=>parseInt(h,16)/255).map(c=>c<=.04045?c/12.92:((c+.055)/1.055)**2.4).reduce((sum,c,i)=>sum+c*[.2126,.7152,.0722][i],0);}
  function validateAgentProfile(raw,expectedId=raw?.id){
    if(!raw||typeof raw!=='object'||Array.isArray(raw)||!profileIds.has(raw.id)||raw.id!==expectedId
      ||Object.keys(raw).some(key=>!['id','label','theme','tokens'].includes(key))
      ||typeof raw.label!=='string'||!raw.label.trim()||raw.label.length>64)throw new Error('Invalid agent profile');
    if(compiledProfiles.has(raw))return raw;
    if(!raw.theme||typeof raw.theme!=='object'||Array.isArray(raw.theme)||Object.keys(raw.theme).some(key=>!themeKeys.has(key)))throw new Error('Invalid profile theme');
    const theme={};for(const [key,value] of Object.entries(raw.theme))theme[key]=colorValue(value);
    if(!theme.background||!theme.foreground)throw new Error('Profile needs foreground and background');
    const levels=[luminance(theme.background),luminance(theme.foreground)].sort((a,b)=>a-b);
    if((levels[1]+.05)/(levels[0]+.05)<7)throw new Error('Profile foreground contrast is too low');
    if(!Array.isArray(raw.tokens)||!raw.tokens.length||raw.tokens.length>32)throw new Error('Invalid profile token limit');
    const rules=[],tokens=raw.tokens.map(token=>{
      if(!token||typeof token!=='object'||Object.keys(token).some(key=>!['pattern','flags','color'].includes(key))
        ||typeof token.pattern!=='string'||!token.pattern||token.pattern.length>256||typeof token.flags!=='string'
        ||!/^[gimu]*$/.test(token.flags)||new Set(token.flags).size!==token.flags.length
        ||/\\[1-9]|\(\?[=!<]|\)[+*{]/.test(token.pattern))throw new Error('Invalid profile token rule');
      const flags=[...new Set('g'+token.flags)].join(''),color=colorValue(token.color);
      let expression;try{expression=new RegExp(token.pattern,flags);}catch{throw new Error('Invalid profile regex');}
      if(expression.test(''))throw new Error('Profile regex must consume text');expression.lastIndex=0;
      rules.push({expression,color});return Object.freeze({pattern:token.pattern,flags,color});
    });
    const profile=Object.freeze({id:raw.id,label:raw.label,theme:Object.freeze(theme),tokens:Object.freeze(tokens)});
    compiledProfiles.set(profile,rules);return profile;
  }
  const commonAgentProfile=validateAgentProfile({id:'common',label:'自动',theme:{
    background:'#11151b',foreground:'#d6d9df',cursor:'#7ee787',cursorAccent:'#11151b',
    selectionBackground:'#344253',selectionForeground:'#d6d9df',
    black:'#8b929c',red:'#e06c75',green:'#98c379',yellow:'#e5c07b',blue:'#82cfff',magenta:'#82cfff',cyan:'#82cfff',white:'#d6d9df',
    brightBlack:'#8b929c',brightRed:'#e06c75',brightGreen:'#98c379',brightYellow:'#e5c07b',brightBlue:'#82cfff',brightMagenta:'#82cfff',brightCyan:'#82cfff',brightWhite:'#d6d9df'
  },tokens:[
    {pattern:'"[^"\\n]+"|\'[^\'\\n]+\'',flags:'gu',color:'#e5c07b'},
    {pattern:'(?:错误|失败|异常|警告)|\\b(?:Error|Exception|ERROR|FAIL|FAILED|Warning)\\b',flags:'gu',color:'#e06c75'},
    {pattern:'[\\p{L}\\p{N}_./\\\\:-]+\\.(?:py|js|jsx|ts|tsx|css|html|json|md|exe|ps1|sh|yaml|yml|toml|java|go|rs|cpp|h)(?::\\d+)?',flags:'gu',color:'#e5c07b'},
    {pattern:'`[^`\\n]+`|\\b[A-Za-z_][\\w]*\\(\\)',flags:'gu',color:'#e5c07b'},
    {pattern:'\\b\\d+(?:\\.\\d+)?(?:\\s?(?:KiB|MiB|ms|tokens|字节))?',flags:'gu',color:'#e5c07b'},
    {pattern:'\\b(?:Success|SUCCESS|Passed|PASSED)\\b|(?:成功|已完成)',flags:'gu',color:'#98c379'},
    {pattern:'^#{1,6} [^\\n]+|\\*\\*[^*\\n]+\\*\\*',flags:'gu',color:'#82cfff'}
  ]});
  const presentationProfiles=new WeakMap();
  const neutralPresentationRGB=new Set(['foreground','black','white','brightBlack','brightWhite'].map(key=>parseInt(commonAgentProfile.theme[key].slice(1),16)));
  const codeKeywordPattern='\\b(?:const|let|var|function|async|await|class|def|import|from|return|export|interface|type|public|private|static|void|new|throw|try|catch)\\b';
  function presentationPolicy(profile=commonAgentProfile,actualAgent='auto'){
    profile=validateAgentProfile(profile);
    let shared=presentationProfiles.get(profile);
    if(!shared){
      const seen=new Set(),tokens=[...profile.tokens,...commonAgentProfile.tokens,
        {pattern:codeKeywordPattern,flags:'gu',color:'#c678dd'}]
        .filter(token=>{const key=token.pattern+'\0'+token.flags;if(seen.has(key))return false;seen.add(key);return true;}).slice(0,32);
      // Keep the original CLI's recognition rules and label. This derived
      // profile changes presentation, never executable identity or controls.
      shared=validateAgentProfile({id:profile.id,label:profile.label,theme:{...commonAgentProfile.theme},tokens});presentationProfiles.set(profile,shared);
    }
    return {actualAgent,profile:shared,terminalOptions:{theme:{...commonAgentProfile.theme},cursorStyle:'block',cursorBlink:false,
      fontFamily:'"Cascadia Mono", "Cascadia Code", Consolas, monospace',minimumContrastRatio:4.5}};
  }
  function composerAction(event,composing=false){
    // Android can report a committed Enter as 229. Composition state, rather
    // than that neutral key marker, determines whether text is still preedit.
    if(composing||event.isComposing)return 'none';
    if(event.type==='beforeinput')return ['insertLineBreak','insertParagraph'].includes(event.inputType)?'send':'none';
    return event.key!=='Enter'?'none':event.shiftKey?'newline':'send';
  }
  function createComposerSubmitter({schedule=setTimeout,cancel=clearTimeout,onPending=()=>{}}={}){
    let pending=null;
    const current=job=>{try{return !!job.isCurrent();}catch{return false;}};
    function uncertain(job,reason){
      if(!job.pasted||job.uncertain)return;job.uncertain=true;
      // This callback deliberately belongs to the captured operation, including
      // after a target switch. Adapters retain its original target/text only.
      try{job.onUncertain(reason);}catch{}
    }
    function clear(reason=null){
      if(!pending)return;const job=pending;pending=null;
      for(const timer of [job.timer,job.timeout])if(timer!==null)cancel(timer);
      if(reason){job.cancelReason=reason;uncertain(job,reason);}onPending(false);
    }
    function timeout(job){
      if(job.timeout!==null)return;
      job.timeout=schedule(()=>{if(pending!==job)return;const live=current(job);clear('timeout');if(live&&!pending&&current(job))job.onTimeout();},5000);
    }
    function arm(job){
      if(pending!==job||job.timer!==null||job.replayBlocked)return;
      if(!current(job)){clear('stale_target');return;}
      if(job.timeout!==null){cancel(job.timeout);job.timeout=null;}
      job.timer=schedule(()=>{
        if(pending!==job||job.replayBlocked)return;
        if(!current(job)){clear('stale_target');return;}clear();
        if(pending||!current(job)){uncertain(job,'stale_target');return;}
        let sent;try{sent=job.enter();}catch{uncertain(job,'enter_failed');return;}
        if(sent===false){uncertain(job,'enter_failed');return;}
        if(pending||!current(job)){uncertain(job,'stale_target');return;}job.onSent();
      },200);
    }
    function output({replaying=false}={}){
      const job=pending;if(!job)return;
      if(!current(job)){clear('stale_target');return;}
      if(replaying){
        // Replay can contain a matching old paste. Latch uncertainty so a later
        // unrelated live batch cannot turn that replay into receipt evidence.
        job.replayBlocked=true;uncertain(job,'replay');if(pending!==job)return;
        if(job.timer!==null){cancel(job.timer);job.timer=null;}timeout(job);return;
      }
      if(!job.echo||job.timer!==null||job.replayBlocked)return;
      if(job.echo())arm(job);
    }
    return {submit(text,{paste,enter,isCurrent,echo=null,onTimeout=()=>{},onSent=()=>{},onUncertain=()=>{}}){
      if(typeof text!=='string'||!text||pending||typeof paste!=='function'||typeof enter!=='function'||typeof isCurrent!=='function')return false;
      const job={timer:null,timeout:null,enter,isCurrent,echo,onSent,onTimeout,onUncertain,replayBlocked:false,pasted:false,uncertain:false,cancelReason:null};
      if(!current(job))return false;
      pending=job;onPending(true);
      if(pending!==job||!current(job)){if(pending===job)clear();return false;}
      let pasted;try{pasted=paste(text);}catch(error){job.pasted=true;uncertain(job,'paste_failed');if(pending===job)clear();throw error;}
      if(pasted===false){if(pending===job)clear();return false;}
      job.pasted=true;
      if(pending!==job){uncertain(job,job.cancelReason||'cancelled');return false;}
      if(!current(job)){clear('stale_target');return false;}
      // Only the native adapter uses this echo receipt. Managed submissions
      // retain their independent durable nonce/event acknowledgement path.
      if(echo){timeout(job);output();}else arm(job);
      return true;
    },output,cancel(reason='cancelled'){clear(typeof reason==='string'&&reason?reason:'cancelled');},get pending(){return pending!==null;}};
  }
  function createComposerEcho(term,text){
    const parts=text.replace(/\r\n?/g,'\n').split('\n').map(line=>line.replace(/\s/g,'')).filter(Boolean);
    const needle=(parts[parts.length-1]||'').slice(-128);
    function read(){
      const buffer=term.buffer.active,end=buffer.baseY+buffer.cursorY,lines=[];
      for(let row=Math.max(buffer.baseY,end-100);row<=end;row++){
        const line=buffer.getLine(row);if(line)lines.push(line.translateToString(true,0,row===end?buffer.cursorX:undefined));
      }
      return lines.join('').replace(/\s/g,'');
    }
    const count=(value,part)=>part?value.split(part).length-1:0;
    const placeholders=value=>[...value.matchAll(/\[Pasted(?:Content|text)[^\]]{0,64}\]/gi)].map(match=>match[0]).join('|');
    const before=read(),copies=count(before,needle),previousPlaceholder=placeholders(before);
    return ()=>{const after=read();return !needle||count(after,needle)>copies||(after!==before&&after.endsWith(needle))
      ||(text.length>1000&&placeholders(after)!==previousPlaceholder&&placeholders(after)!=='');};
  }
  function localHistoryPolicy(action,context={}){
    let follow=context.following!==false;
    if(action==='output'||action==='observe'){
      if(action==='observe'&&(context.userInitiated===true||['wheel','touch','button'].includes(context.source)))follow=!!context.atBottom;
      return {route:'none',follow};
    }
    if(!['up','down','start','end'].includes(action))return {route:'none',follow};
    if(action==='up'||action==='start')follow=false;else if(action==='end')follow=true;
    if(context.historyActive)return {route:'archive',follow};
    if(context.bufferType==='normal'&&context.baseY>0)return {route:'terminal',follow};
    // Claude Code announces itself (OSC 7501) and pages its own transcript with
    // page keys or SGR mouse; Codex native paging stays button-only as before.
    const nativeSources=context.actualAgent==='codex'?['button']:['button','wheel','touch'];
    if(nativeSources.includes(context.source)&&['codex','claude','claude-code'].includes(context.actualAgent)&&context.nativePaging===true&&context.ready===true&&!context.replaying&&!context.managed)return {route:'native',follow};
    return {route:context.archiveAvailable?'archive':'unavailable',follow};
  }
  async function loadAgentProfile(value,id){
    if(!agentProfiles.some(item=>item.id===id))throw new Error('Unknown agent mode');
    const url=new URL(value,root.location?.href);
    if(!['http:','https:'].includes(url.protocol)||url.username||url.password||url.hash
      ||root.location&&url.origin!==root.location.origin)throw new Error('Profile must use the document origin');
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),5000);
    try{
      const response=await root.fetch(url.toString(),{credentials:'omit',redirect:'error',signal:controller.signal});
      if(!response.ok||Number(response.headers.get('content-length')||0)>32768)throw new Error('Profile download rejected');
      const chunks=[];let size=0;
      if(response.body?.getReader){const reader=response.body.getReader();try{while(true){const {value,done}=await reader.read();if(done)break;size+=value.length;if(size>32768){await reader.cancel();throw new Error('Profile is too large');}chunks.push(value);}}finally{reader.releaseLock();}}
      else{const bytes=new Uint8Array(await response.arrayBuffer());size=bytes.length;if(size>32768)throw new Error('Profile is too large');chunks.push(bytes);}
      const bytes=new Uint8Array(size);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}
      return validateAgentProfile(JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes)),id);
    }finally{clearTimeout(timer);}
  }
  function agentModeFromSession(session={},title=''){
    function classify(text,command=false){
      if(typeof text!=='string')return null;
      if(/\bcursor(?:[-_ ]+(?:cli|agent))\b/i.test(text))return 'cursor-cli';
      if(/\bpi(?:\.dev|[-_ ]dev)\b|@mariozechner\/pi-coding-agent/i.test(text)
        ||command&&/(?:^|[\\/\s])pi(?:\.exe)?(?:\s|$)/i.test(text))return 'pi-dev';
      if(/\bclaude(?:[-_ ]code)?\b/i.test(text))return 'claude-code';
      for(const id of ['codex','grok','kimi'])if(new RegExp('\\b'+id+'\\b','i').test(text))return id;
      return null;
    }
    for(const value of [session.learnedAgent,session.agent,session.agentType,session.shell,session.command]){
      const id=classify(value,true);if(id)return id;
    }
    for(const value of [session.title,title]){const id=classify(value);if(id)return id;}
    return 'auto';
  }
  function inferAgentContent(text){
    if(typeof text!=='string'||!text||text.length>32768)return '';
    const finished=/^[ \t]*Finished[ \t]+\S/m.test(text),followUp=/^[ \t]*(?:→[ \t]+)?Add a follow-up[ \t]*$/m.test(text);
    const auto=/\bAuto[ \t]+·[ \t]*\d+(?:\.\d+)?%[ \t]+·[ \t]*\d+[ \t]+files edited\b/.test(text);
    const thinking=/\bthinking[ \t]*:[ \t]*(?:max|high|medium|low)\b/i.test(text);
    const context=/\bcontext[ \t]*:[ \t]*\d+(?:\.\d+)?%[ \t]*\([^)\n]*\)/i.test(text);
    return (finished&&(followUp||auto))||(thinking&&context)?'cursor-cli':'';
  }
  function createAgentDetector(term,{isUnknown,onDetected,delay=750,schedule=root.setTimeout,cancel=root.clearTimeout}={}){
    if(typeof isUnknown!=='function'||typeof onDetected!=='function')throw new Error('Invalid agent detector');
    let dead=false,timer=null;
    function scan(){timer=null;if(dead||!isUnknown())return;const buffer=term.buffer.active,lines=[];
      for(let row=Math.max(0,buffer.length-48);row<buffer.length;row++)lines.push(buffer.getLine(row)?.translateToString(true)||'');
      const mode=inferAgentContent(lines.join('\n').slice(-32768));if(mode)onDetected(mode);
    }
    return {request(){if(!dead&&timer===null&&isUnknown())timer=schedule(scan,delay);},dispose(){dead=true;if(timer!==null)cancel(timer);timer=null;}};
  }
  function highlightSpans(line,cols,profile=commonAgentProfile,{presentationStyle='native'}={}){
    profile=validateAgentProfile(profile);
    cols=Math.min(500,Math.max(0,Math.floor(cols)||0));
    let text='';const columns=[],plain=[];
    for(let x=0;x<cols;x++){
      const cell=line.getCell(x);if(!cell||!cell.getWidth())continue;
      const chars=cell.getChars()||' ',width=cell.getWidth();
      if(text.length+chars.length>4096)return [];
      for(let i=0;i<chars.length;i++)columns[text.length+i]=x;
      text+=chars;columns[text.length]=x+width;
      let neutral=false;
      if(presentationStyle==='codex'){
        if(cell.isFgPalette?.())neutral=[7,8,15].includes(cell.getFgColor());
        else if(cell.isFgRGB?.()){
          const value=cell.getFgColor(),channels=[value>>>16&255,value>>>8&255,value&255];
          neutral=channels[0]===channels[1]&&channels[1]===channels[2]||neutralPresentationRGB.has(value);
        }
      }
      const eligible=(cell.isFgDefault()||neutral)&&cell.isBgDefault()&&!cell.isInverse()&&!cell.isInvisible()&&!cell.isDim();
      for(let i=0;i<width;i++)plain[x+i]=eligible;
    }
    const spans=[];
    const occupied=[];
    for(const {expression,color} of compiledProfiles.get(profile)){
      // Generic words such as "return" are code only with code-shaped context.
      if(expression.source===codeKeywordPattern&&!/[=;{}()]|^\s*(?:import\s+[\w.*]+|from\s+[\w.]+\s+import|(?:def|class)\s+\w+.*:)/.test(text))continue;
      for(const match of text.matchAll(expression)){
      const start=columns[match.index],end=columns[match.index+match[0].length];
      // Faint text is application styling too (for example CLI suggestions).
      // Split around it and explicit colors instead of replacing either.
      let run=start;
      while(run<end){while(run<end&&(!plain[run]||occupied[run]))run++;let stop=run;while(stop<end&&plain[stop]&&!occupied[stop])stop++;
        if(stop>run){spans.push({x:run,width:stop-run,foregroundColor:color});for(let x=run;x<stop;x++)occupied[x]=true;}run=stop;}
      }
    }
    return spans;
  }
  function createHighlighter(term,{enabled=true,profile=commonAgentProfile,presentationStyle='native',schedule=root.requestAnimationFrame||((fn)=>setTimeout(fn,0)),cancel=root.cancelAnimationFrame||clearTimeout}={}){
    profile=validateAgentProfile(profile);
    let entries=[],buffer=null,frame=null,dead=false;
    const remove=entry=>{for(const decoration of entry.decorations)decoration.dispose();entry.marker.dispose();};
    function clear(){for(const entry of entries)remove(entry);entries=[];}
    function refresh(){
      frame=null;if(dead)return;
      const active=term.buffer.active;if(buffer!==active){clear();buffer=active;}
      if(!enabled){clear();term.refresh(0,term.rows-1);return;}
      const previous=new Map();for(const entry of entries){if(entry.marker.isDisposed)remove(entry);else previous.set(entry.marker.line,entry);}
      entries=[];let changed=false;
      // Cursor paragraphs can span the full scrollback. Inspect only the capped
      // viewport plus eight context rows per side; a missing boundary means the
      // unresolved continuation remains input, not permission to decorate it.
      const visibleStart=Math.max(0,active.viewportY),visibleEnd=Math.min(active.length,visibleStart+Math.min(200,term.rows));
      const lower=Math.max(0,visibleStart-8),upper=Math.min(active.length,visibleEnd+8),lines=new Map();
      const lineAt=row=>{if(!lines.has(row))lines.set(row,active.getLine(row));return lines.get(row);};
      const cursor=active.baseY+active.cursorY;
      let inputStart=lower,inputEnd=upper-1;
      if(cursor>=upper){
        for(let row=upper-1;row>=lower;row--){const line=lineAt(row);if(line&&!line.isWrapped){inputStart=row;break;}}
      }else if(cursor<lower){
        for(let row=lower;row<upper;row++){const line=lineAt(row);if(line&&!line.isWrapped){inputEnd=row-1;break;}}
      }else{
        inputStart=cursor;inputEnd=cursor;
        while(inputStart>lower){const line=lineAt(inputStart);if(line&&!line.isWrapped)break;inputStart--;}
        while(inputEnd+1<upper){const line=lineAt(inputEnd+1);if(line&&!line.isWrapped)break;inputEnd++;}
      }
      for(let row=visibleStart;row<visibleEnd;row++){
        const line=lineAt(row);if(!line)continue;
        const spans=row>=inputStart&&row<=inputEnd?[]:highlightSpans(line,term.cols,profile,{presentationStyle}),signature=JSON.stringify(spans),old=previous.get(row);previous.delete(row);
        if(old&&old.signature===signature){entries.push(old);continue;}
        if(old){remove(old);changed=true;}if(!spans.length)continue;
        const marker=term.registerMarker(row-active.baseY-active.cursorY);if(!marker)continue;
        const decorations=spans.map(span=>term.registerDecoration({marker,...span})).filter(Boolean);
        entries.push({marker,decorations,signature});changed=true;
      }
      for(const entry of previous.values()){remove(entry);changed=true;}
      if(changed)term.refresh(0,term.rows-1);
    }
    function request(){if(!dead&&frame===null)frame=schedule(refresh);}
    const listeners=[term.onWriteParsed(request),term.onScroll(request),term.onResize(request),term.buffer.onBufferChange(()=>{clear();buffer=null;request();})];
    request();
    return {setEnabled(value){enabled=!!value;request();},setPresentation(value){if(dead)return;presentationStyle=value==='codex'?'codex':'native';clear();buffer=null;request();},setProfile(value){if(dead)return;profile=validateAgentProfile(value);clear();buffer=null;term.refresh(0,term.rows-1);request();},dispose(){if(dead)return;dead=true;if(frame!==null)cancel(frame);for(const listener of listeners)listener.dispose();clear();}};
  }

  function pack(op, payload = new Uint8Array()) {
    if(payload.length + 1 > 262144)throw new Error('Oversized frame');
    const result=new Uint8Array(payload.length+5);
    new DataView(result.buffer).setUint32(0,payload.length+1,true);
    result[4]=op;result.set(payload,5);return result;
  }
  function unpack(input) {
    const frame=input instanceof Uint8Array?input:new Uint8Array(input);
    if(frame.length<5)throw new Error('Short frame');
    const length=new DataView(frame.buffer,frame.byteOffset,frame.byteLength).getUint32(0,true);
    if(length<1||length>262144||length+4!==frame.length)throw new Error('Bad frame length');
    return {op:frame[4],payload:frame.subarray(5)};
  }
  function targetFrame(op, session, input) {
    if (!session || session.includes('\0')) throw new Error('Invalid session');
    const prefix = encoder.encode(session + '\0');
    const bytes = typeof input === 'string' ? encoder.encode(input) : input;
    const payload = new Uint8Array(prefix.length + bytes.length);
    payload.set(prefix);payload.set(bytes,prefix.length);return pack(op,payload);
  }
  function sessionPayload(bytes) {
    const separator = bytes.indexOf(0);
    if (separator < 1) throw new Error('Missing session delimiter');
    return {session: new TextDecoder().decode(bytes.subarray(0, separator)), bytes: bytes.subarray(separator + 1)};
  }
  function historyPayload(payload) {
    if(!(payload instanceof Uint8Array)||payload.length>262143)throw new Error('Invalid history frame');
    const end=payload.indexOf(0);if(end<1||end>256||end+3>payload.length)throw new Error('Invalid history header');
    const decode=new TextDecoder('utf-8',{fatal:true}),session=decode.decode(payload.subarray(0,end));
    if(/[\x00-\x1f\x7f]/.test(session))throw new Error('Invalid history session');
    const length=payload[end+1]|payload[end+2]<<8,start=end+3;
    if(length<2||length>4096||start+length>payload.length)throw new Error('Invalid history metadata length');
    const context=JSON.parse(decode.decode(payload.subarray(start,start+length)));
    const fields=context?.v===2?'cols,end_position,epoch,kind,partial,reason,rows,stream_id,v':'cols,epoch,kind,partial,reason,rows,v';
    if(!context||Array.isArray(context)||Object.keys(context).sort().join(',')!==fields||![1,2].includes(context.v)
      ||!(context.v===2?['snapshot','output','resume']:['snapshot','output']).includes(context.kind)
      ||!Number.isInteger(context.cols)||context.cols<2||context.cols>500||!Number.isInteger(context.rows)||context.rows<1||context.rows>200
      ||!Number.isSafeInteger(context.epoch)||context.epoch<0||typeof context.partial!=='boolean'||typeof context.reason!=='string'
      ||!/^[a-z_]{0,64}$/.test(context.reason)||context.partial!==!!context.reason
      ||context.v===2&&(!Number.isSafeInteger(context.end_position)||context.end_position<0
        ||typeof context.stream_id!=='string'||!/^[a-f0-9]{24}$/.test(context.stream_id)))throw new Error('Invalid history metadata');
    return {session,context,bytes:payload.subarray(start+length)};
  }
  function historyFrameBytes(context,bytes,cursor) {
    if(context.v!==2)return {bytes,position:null,reset:context.kind==='snapshot'};
    if(context.kind==='snapshot'&&(!cursor||cursor.stream_id!==context.stream_id||cursor.epoch!==context.epoch))
      return {bytes,position:context.end_position,reset:true};
    if(!cursor||cursor.stream_id!==context.stream_id||context.kind!=='output'&&cursor.epoch!==context.epoch)return null;
    const start=context.end_position-bytes.length;
    if(start<0||cursor.position<start)return context.kind==='snapshot'?{bytes,position:context.end_position,reset:true}:null;
    if(cursor.position>=context.end_position)return {bytes:new Uint8Array(),position:cursor.position,reset:false};
    return {bytes:bytes.subarray(cursor.position-start),position:context.end_position,reset:false};
  }
  function terminalInput(text, replaying = false) {
    if(replaying)return '';
    // ConPTY owns device negotiation. Its echoed queries must not become shell keystrokes.
    // Complete DA/DSR and OSC color reports are replies, not keystrokes.
    // Keep ordinary text, arrows, and bracketed clipboard payloads untouched.
    if(text.startsWith('\x1b[200~'))return text;
    return text.replace(/\x1b\[(?:[?>]?[0-9;]*c|[0-9]+;[0-9]+R|[0-9]+n)/g,'')
      .replace(/\x1b\](?:(?:10|11|12);rgb:[0-9a-f]{1,4}\/[0-9a-f]{1,4}\/[0-9a-f]{1,4}|4;\d+;rgb:[0-9a-f]{1,4}\/[0-9a-f]{1,4}\/[0-9a-f]{1,4})(?:\x07|\x1b\\)/gi,'');
  }
  function cursorKey(term,direction){
    const codes={up:'A',down:'B',left:'D',right:'C'};
    if(typeof direction!=='string'||!Object.prototype.hasOwnProperty.call(codes,direction))return '';
    return '\x1b'+(term?.modes?.applicationCursorKeysMode?'O':'[')+codes[direction];
  }
  function snapshotBytes(bytes, session, preservesStyle=false) {
    if(preservesStyle)return bytes;
    // A full bounded history may begin midway through UTF-8, CSI or OSC output.
    const limit=262144-2-encoder.encode(session).length;
    if(bytes.length<limit-3)return bytes;
    const boundary=bytes.findIndex(byte=>byte===0x1b||byte===10);
    return boundary<0?bytes:bytes.subarray(boundary+(bytes[boundary]===10?1:0));
  }
  function terminalSize(cols,rows){
    return {cols:Math.max(2,Math.min(500,Math.floor(cols)||2)),rows:Math.max(1,Math.min(200,Math.floor(rows)||1))};
  }
  function messageBytes(message){return typeof message==='string'?encoder.encode(message).length:message.byteLength;}
  function formatRate(bytes){
    const value=Number.isFinite(bytes)?Math.max(0,bytes):0;
    if(value<1024)return Math.round(value)+' B/s';
    if(value<1048576)return (value/1024).toFixed(1)+' KiB/s';
    return (value/1048576).toFixed(1)+' MiB/s';
  }
  class ConnectionMeter{
    constructor(now=0){this.reset(now);}
    reset(now,retries=0){this.tx=0;this.rx=0;this.previousTx=0;this.previousRx=0;this.sampleAt=now;this.sessionAt=now;this.retries=retries;this.rtt=null;this.pongAt=null;}
    sent(message){const bytes=messageBytes(message);this.tx+=bytes;return bytes;}
    received(message){const bytes=messageBytes(message);this.rx+=bytes;return bytes;}
    sample(now){
      const elapsed=now-this.sampleAt;
      const result=elapsed>0?{tx:Math.max(0,this.tx-this.previousTx)*1000/elapsed,rx:Math.max(0,this.rx-this.previousRx)*1000/elapsed}:{tx:0,rx:0};
      this.previousTx=this.tx;this.previousRx=this.rx;this.sampleAt=now;return result;
    }
    pong(rtt,now){if(Number.isFinite(rtt)&&rtt>=0){this.rtt=rtt;this.pongAt=now;}}
    quality(now,phase){
      if(phase!=='connected')return {bars:0,label:{connecting:'连接中',reconnecting:'重连中','auth-failed':'验证失败',offline:'离线',unsupported:'协议不兼容'}[phase]||'未连接'};
      if(this.pongAt===null)return {bars:0,label:'等待测量'};
      if(now-this.pongAt>35000)return {bars:1,label:'响应超时'};
      let bars=this.rtt<=100?4:this.rtt<=250?3:this.rtt<=800?2:1;
      if(this.retries>=2&&now-this.sessionAt<30000)return {bars:Math.min(2,bars),label:'刚恢复'};
      return {bars,label:['','较慢','一般','良好','稳定'][bars]};
    }
  }
  function redrawSizes(cols, rows, minCols=2) {
    const final=terminalSize(cols,rows);
    let alternate;
    alternate={cols:final.cols>minCols?final.cols-1:Math.min(500,final.cols+1),rows:final.rows};
    return [alternate,final];
  }
  function disposePane(panes, session) {
    const pane=panes.get(session);if(!pane)return false;
    // This only releases the local view. It has no socket or process-close operation.
    panes.delete(session);pane.ready=false;if(pane.redrawTimer)clearTimeout(pane.redrawTimer);if(pane.resizeRequest)clearTimeout(pane.resizeRequest.timer);pane.observer.disconnect();
    const addon=pane.webgl;pane.webgl=null;if(addon)addon.dispose();
    pane.highlighter?.dispose();pane.agentDetector?.dispose();pane.questions?.dispose();
    try{pane.term._core?.viewport?.dispose?.();}catch{}
    pane.term.dispose();pane.el.remove();return true;
  }
  function secureEndpoint(value) {
    const url=new URL(value),authority=String(value).match(/^[a-z]+:\/\/([^/?#]*)/i)?.[1]||'';
    if (!['http:', 'https:', 'ws:', 'wss:'].includes(url.protocol)) throw new Error('Invalid relay URL');
    if (authority.includes('@')||url.username||url.password) throw new Error('URL credentials are not supported');
    if(url.search||url.hash)throw new Error('Relay URL must not contain query or fragment credentials');
    const insecure=['http:','ws:'].includes(url.protocol);
    if(insecure&&url.hostname==='117.50.48.22'&&url.port==='8817'&&['/','/ws','/ws/'].includes(url.pathname)){
      url.protocol=url.protocol==='http:'?'https:':'wss:';url.port='';url.pathname='/';
    }
    const local=['localhost','127.0.0.1','[::1]','::1'].includes(url.hostname)||/^127(?:\.\d{1,3}){3}$/.test(url.hostname);
    if(['http:','ws:'].includes(url.protocol)&&!local)throw new Error('Network connections require HTTPS/WSS; plaintext is allowed only on loopback');
    return url.toString();
  }
  function socketURL(relay, origin) {
    const url = new URL(secureEndpoint(relay || origin));
    url.protocol = ['https:', 'wss:'].includes(url.protocol) ? 'wss:' : 'ws:';
    if (url.pathname === '/' || !url.pathname) url.pathname = '/ws';
    url.search = ''; url.hash = '';
    return url.toString();
  }
  function updateHintRevision(message) {
    return message?.op==='ui_update'&&typeof message.revision==='string'&&/^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/.test(message.revision)&&!message.revision.includes('..')&&Number.isSafeInteger(message.sequence)&&message.sequence>0?message.revision:'';
  }
  function createScreenHistory({maxFrames=120,maxBytes=1048576,minInterval=750,now=()=>performance.now()}={}){
    if(!Number.isSafeInteger(maxFrames)||maxFrames<1||!Number.isSafeInteger(maxBytes)||maxBytes<1||!Number.isFinite(minInterval)||minInterval<0||typeof now!=='function')throw new TypeError('Invalid screen history limits');
    const encoder=new TextEncoder();
    let frames=[],bytes=0,selected=null,nextID=0,lastCheckpoint=0;
    const index=()=>selected===null?-1:frames.findIndex(entry=>entry.frame.id===selected);
    const current=()=>frames[index()]?.frame||null;
    function trim(){
      while(frames.length>maxFrames||bytes>maxBytes){
        let at=frames.findIndex((entry,i)=>entry.frame.id!==selected&&i<frames.length-1);
        if(at<0)at=0;
        bytes-=frames.splice(at,1)[0].size;
      }
      if(selected!==null&&index()<0)selected=frames[0]?.frame.id??null;
    }
    return {
      record(value){
        const cols=value?.cols,rows=value?.rows,lines=value?.lines;
        if(!Number.isInteger(cols)||cols<2||cols>500||!Number.isInteger(rows)||rows<1||rows>200||!Array.isArray(lines)||lines.length!==rows||Array.from(lines).some(line=>typeof line!=='string'||line.length>cols*2||/[\x00-\x1f\x7f]/.test(line)))return false;
        const latest=frames[frames.length-1]?.frame;
        if(latest&&latest.cols===cols&&latest.rows===rows&&latest.lines.every((line,i)=>line===lines[i]))return false;
        const time=now();if(!Number.isFinite(time)||time<0)return false;
        const timestamp=Math.max(time,latest?.timestamp||0);
        const frame=Object.freeze({id:nextID+1,cols,rows,lines:Object.freeze([...lines]),timestamp});
        const size=encoder.encode(JSON.stringify(frame)).length;
        if(size>maxBytes)return false;
        if(frames.length>1&&latest.id!==selected&&timestamp-lastCheckpoint<minInterval)bytes-=frames.pop().size;
        else lastCheckpoint=timestamp;
        frames.push({frame,size});bytes+=size;nextID++;trim();return true;
      },
      previous(){
        if(!frames.length)return null;
        const at=index();selected=frames[at<0?Math.max(0,frames.length-2):Math.max(0,at-1)].frame.id;return current();
      },
      next(){
        const at=index();if(at<0)return null;
        selected=frames[at+1]?.frame.id??null;return current();
      },
      top(){selected=frames[0]?.frame.id??null;return current();},
      bottom(){selected=null;return null;},
      clear(){frames=[];bytes=0;selected=null;lastCheckpoint=0;},
      get length(){return frames.length;},
      get bytes(){return bytes;},
      get current(){return current();},
      get atTop(){return !frames.length||selected===frames[0].frame.id;},
      get atBottom(){return selected===null;}
    };
  }
  // These are display derivatives only. The journal's immutable bytes and hashes
  // never pass through the control-character or Markdown presentation helpers.
  function visibleJournalText(value){
    return String(value??'').replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f]/g,c=>
      '\\x'+c.charCodeAt(0).toString(16).padStart(2,'0'));
  }
  function parseCodeFences(value){
    const text=String(value),parts=[];
    const lines=text.match(/[^\n]*\n|[^\n]+$/g)||[];
    let pending='',code=null,language='',fence='';
    for(const line of lines){
      const match=line.match(/^ {0,3}(`{3,}|~{3,})([^\r\n]*)\r?\n?$/);
      if(code!==null){
        if(match&&match[1][0]===fence[0]&&match[1].length>=fence.length&&!match[2].trim()){
          parts.push({type:'code',language,text:code});code=null;
        }else code+=line;
      }else if(match){
        if(pending){parts.push({type:'text',text:pending});pending='';}
        code='';language=match[2].trim();fence=match[1];
      }else pending+=line;
    }
    if(code!==null)parts.push({type:'code',language,text:code});
    if(pending)parts.push({type:'text',text:pending});
    return parts;
  }
  function formatQuote(text,source={}){
    const meta=value=>visibleJournalText(value).replace(/[\r\n\t]/g,' ');
    return '[引用 '+meta(source.journalId)+' · '+meta(source.observedAt||source.sourceCreatedAt)+']\n'
      +String(text).split('\n').map(line=>'> '+line).join('\n');
  }
  function conversationEligible(event){
    const proof=typeof event.proof==='object'?event.proof?.kind:event.proof;
    return event.kind==='user_input'&&event.state==='accepted'&&['human','user'].includes(event.actor)
      &&['app_submission','provider_user_item','provider_human_input','user_attestation'].includes(proof)
      ||event.kind==='assistant_final'&&['confirmed','completed'].includes(event.state)
      &&['provider_final_phase','provider_terminal_result','user_attestation'].includes(proof);
  }
  function createConversationViewer(options={}){
    let context={...options},generation=0,disposed=false,host=null,doc=null,ui=null;
    let projection='history',layout='history',journal=null,journalId='',windowTitle='',cursor=null,loading=false;
    let events=[],pendingCount=0,codeVisible=false,pageStarts=[null],pageIndex=-1,listCursor=null,readThrough=0,newerPending=false;
    const bodies=new Map(),preferences=new Map();
    const stale=version=>disposed||version!==generation;
    const node=(tag,text)=>{const el=doc.createElement(tag);if(text!==undefined)el.textContent=text;return el;};
    function button(text,action){const el=node('button',text);el.type='button';el.addEventListener('click',action);return el;}
    function selectionButton(text,quote){
      const el=button(text,()=>selectionAction(quote));
      // Focusing the toolbar on pointer press can collapse the page's selection
      // before click. Keep the live, contained selection; do not cache a range
      // that another pane/context could later reuse. Keyboard clicks still work.
      el.addEventListener('pointerdown',event=>{if(event.button===0&&selected())event.preventDefault();});
      return el;
    }
    function message(text){if(ui)ui.message.textContent=text;}
    function clearSelection(){doc?.getSelection?.()?.removeAllRanges();}
    function clear(){generation++;loading=false;cursor=null;events=[];pendingCount=0;pageStarts=[null];pageIndex=-1;listCursor=null;readThrough=0;newerPending=false;bodies.clear();journal=null;clearSelection();if(ui){ui.cards.replaceChildren();ui.badges.textContent='';ui.picker.replaceChildren();ui.newer.hidden=true;message('');}}
    function ensure(){
      if(host)return;
      doc=context.root?.ownerDocument||root.document;
      if(!doc)throw new Error('Conversation viewer requires a document');
      host=node(context.inline?'section':'dialog');host.className='conversation-viewer';
      host.setAttribute('aria-label','历史记录与对话记录');
      Object.assign(host.style,{background:'#151b23',color:'#e6edf3',padding:'12px',boxSizing:'border-box',font:'14px/1.6 system-ui, sans-serif',width:context.inline?'100%':'min(960px, 96vw)',maxHeight:context.inline?'100%':'90vh',border:'1px solid #465365',borderRadius:'8px',display:'flex',flexDirection:'column',gap:'8px',minHeight:'0'});
      const toolbar=node('div'),title=node('strong'),badges=node('div'),picker=node('div'),notice=node('div'),cards=node('div');
      Object.assign(toolbar.style,{display:'flex',flexWrap:'wrap',gap:'8px',alignItems:'center'});
      Object.assign(title.style,{minWidth:'0',overflowWrap:'anywhere'});
      Object.assign(cards.style,{overflow:'auto',minHeight:'0',flex:'1',overscrollBehavior:'contain'});
      notice.setAttribute('role','status');notice.setAttribute('aria-live','polite');
      const mode=button('模式：历史',()=>{layout=layout==='history'?'dialog':'history';render();});
      const codes=button('显示代码',()=>setCodeVisible(!codeVisible));codes.setAttribute('aria-pressed','false');
      toolbar.append(title,mode,codes,selectionButton('复制选中',false),selectionButton('引用选中',true),button('原始终端',()=>context.onOriginalTerminal?.()));
      if(!context.inline)toolbar.append(button('关闭',()=>close()));
      const more=button('加载更多记录',()=>loadPage()),previous=button('加载更早记录',()=>loadPage(pageIndex-1)),newer=button('有新记录，加载更新',()=>cursor?loadPage():refresh(true)),navigation=node('div');navigation.append(previous,more,newer);newer.hidden=true;
      host.append(toolbar,badges,picker,notice,cards,navigation);
      (context.inline?context.root:doc.body)?.append(host);
      host.addEventListener('cancel',event=>{event.preventDefault();close();});
      ui={title,mode,codes,badges,picker,message:notice,cards,more,previous,newer};
    }
    function show(){host.hidden=false;host.style.display='flex';if(!context.inline&&!host.open){if(host.showModal)host.showModal();else host.setAttribute('open','');}}
    function renderBadges(){
      if(!journal)return;
      const mirror=journal.mirror||{},through=Number(mirror.through_seq??mirror.acked_through_seq??0),last=Number(journal.last_seq||0);
      const mirrored=!!(mirror.destination?.destination_id||mirror.destination_id)&&!!mirror.last_receipt&&through>=last;
      ui.badges.textContent='本地：'+(journal.local_error?'保存异常':('已记录至 #'+last))
        +' · 服务器：'+(mirrored?'已确认至 #'+through:through>0?'已确认至 #'+through:'未确认')
        +' · 覆盖：'+(journal.coverage==='full'?'完整起点':'部分 / '+visibleJournalText(journal.coverage||'未知'))
        +' · 终端：'+visibleJournalText(journal.terminal_state||'unknown')+' · 采集：'+visibleJournalText(journal.capture_state||'unknown');
    }
    function source(event){return {targetKey:context.targetKey,journalId,eventId:event?.event_id??null,seq:event?.seq??null,
      hostInstallationId:journal?.host_installation_id,sessionId:journal?.session_id,
      observedAt:event?.observed_at,sourceCreatedAt:event?.source_created_at,projection};}
    function selected(){
      const selection=doc.getSelection?.();if(!selection?.rangeCount)return null;
      // All selected ranges must stay inside the card pane, including their
      // boundaries. Outside page/editor selections are never clipboard input.
      for(let i=0;i<selection.rangeCount;i++){
        const range=selection.getRangeAt(i);
        if(!ui.cards.contains(range.startContainer)||!ui.cards.contains(range.endContainer))return null;
      }
      if(!ui.cards.contains(selection.anchorNode)||!ui.cards.contains(selection.focusNode))return null;
      const text=selection.toString();if(!text)return null;
      let target=selection.anchorNode;while(target&&target.parentNode!==ui.cards)target=target.parentNode;
      return {text,event:events.find(item=>item.event_id===target?.dataset.eventId)};
    }
    async function copy(text,version){
      if(typeof context.onCopy==='function'){if(await context.onCopy(text)===false)throw new Error('copy failed');}
      else{if(!root.navigator?.clipboard?.writeText)throw new Error('clipboard unavailable');await root.navigator.clipboard.writeText(text);}
      if(!stale(version))message('已复制');
    }
    async function selectionAction(quote){
      const value=selected(),version=generation;if(!value){message('请在记录中选择文字');return;}
      try{
        if(!quote){await copy(value.text,version);return;}
        if(context.isTargetCurrent&& !context.isTargetCurrent()){message('目标已改变，请重新打开记录');return;}
        if(typeof context.onQuote!=='function'){message('当前目标无法引用');return;}
        const accepted=await context.onQuote(value.text,source(value.event));
        if(!stale(version))message(accepted===false?'目标已改变，请重新打开记录':'已添加到草稿');
      }catch{if(!stale(version))message(quote?'引用失败，草稿未确认更新':'复制失败，请检查剪贴板权限');}
    }
    function renderBody(container,text,event){
      container.replaceChildren();
      const raw=['raw_output','bootstrap'].includes(event.kind);
      const blocks=raw?[{type:'code',language:'终端/工具输出（可见控制字符衍生视图）',text}]:parseCodeFences(text);
      for(const block of blocks){
        if(block.type==='text'){
          const paragraph=node('div',visibleJournalText(block.text));paragraph.style.whiteSpace='pre-wrap';container.append(paragraph);
        }else{
          const details=node('details'),summary=node('summary',raw?block.language:'代码'+(block.language?' · '+visibleJournalText(block.language):'')),pre=node('pre',visibleJournalText(block.text));
          details.open=codeVisible;pre.style.whiteSpace='pre-wrap';pre.style.overflowWrap='anywhere';
          details.append(summary,pre,button('复制'+(raw?'输出':'代码'),async()=>{const version=generation;try{await copy(visibleJournalText(block.text),version);}catch{if(!stale(version))message('复制失败，请检查剪贴板权限');}}));container.append(details);
        }
      }
    }
    async function hydrate(event,container,load){
      const version=generation,request=context.request,id=journalId,key=event.event_id;
      load.disabled=true;
      try{
        let text=bodies.get(key);
        if(text===undefined){
          let offset=0,total=null,hash=null;const chunks=[];let size=0;
          while(true){
            const result=await request('read_content',{journal_id:id,content_id:event.content_id,offset,limit:49152});
            if(stale(version))return;
            if(result?.content_id!==event.content_id||result.offset!==offset||!Number.isSafeInteger(result.total_bytes)||result.total_bytes<0
              ||typeof result.data_base64!=='string'||total!==null&&total!==result.total_bytes||hash!==null&&hash!==result.sha256
              ||event.content_sha256&&result.sha256!==event.content_sha256)throw new Error('Invalid content');
            total=result.total_bytes;hash=result.sha256;
            const bytes=Uint8Array.from(root.atob(result.data_base64),c=>c.charCodeAt(0));
            if(bytes.length>49152||offset+bytes.length>total||event.content_bytes!==undefined&&total!==event.content_bytes)throw new Error('Invalid bytes');
            chunks.push(bytes);size+=bytes.length;
            if(result.next_offset===null){if(size!==total)throw new Error('Incomplete content');break;}
            if(!bytes.length||result.next_offset!==offset+bytes.length)throw new Error('Invalid offset');offset=result.next_offset;
          }
          const bytes=new Uint8Array(size);let at=0;for(const chunk of chunks){bytes.set(chunk,at);at+=chunk.length;}
          if(/^[a-f0-9]{64}$/i.test(hash)&&root.crypto?.subtle){
            const digest=new Uint8Array(await root.crypto.subtle.digest('SHA-256',bytes));
            if(stale(version))return;
            if(Array.from(digest,x=>x.toString(16).padStart(2,'0')).join('')!==hash.toLowerCase())throw new Error('Content hash mismatch');
          }
          text=new TextDecoder('utf-8',{fatal:event.encoding!=='binary'}).decode(bytes);bodies.set(key,text);
        }
        if(stale(version))return;renderBody(container,text,event);load.remove();
      }catch{if(!stale(version)){load.disabled=false;message('无法读取完整内容；原始记录仍保留，请重试');}}
    }
    function makeCard(event){
        const card=node('article');card.dataset.eventId=event.event_id;
        Object.assign(card.style,{border:'1px solid #465365',borderRadius:'6px',padding:'10px',margin:'8px 0',overflowWrap:'anywhere',marginLeft:layout==='dialog'&&event.kind==='user_input'?'8%':'0',marginRight:layout==='dialog'&&event.kind==='assistant_final'?'8%':'0'});
        const label=event.kind==='user_input'?(conversationEligible(event)?'用户':'CLI 输入（来源未确认）'):
          event.kind==='assistant_final'?(conversationEligible(event)?'助手 · 已确认回复':'助手 · 证据未确认'):
          event.kind==='assistant_candidate'?'助手 · 候选（未确认）':['raw_output','bootstrap'].includes(event.kind)?'终端/工具输出':event.kind==='capture_gap'?'采集缺口':'状态 / 工具';
        const heading=node('div',label+' · #'+event.seq+' · '+visibleJournalText(event.source_created_at||event.observed_at||'时间未知')+' · '+visibleJournalText(event.origin||'来源未知')+' · '+visibleJournalText(event.state||'状态未知'));
        const body=node('div');renderBody(body,bodies.get(event.event_id)??event.preview??'',event);card.append(heading,body);
        if(event.content_id&&!bodies.has(event.event_id)){
          const load=button('读取完整内容',()=>hydrate(event,body,load));card.append(load);
        }
        return card;
    }
    function navigation(){
      ui.more.hidden=!cursor;ui.more.disabled=loading;ui.previous.hidden=pageIndex<=0;ui.previous.disabled=loading;ui.newer.hidden=!newerPending;ui.newer.disabled=loading;
    }
    function render(){
      if(!ui)return;
      const recordTitle=projection==='conversation'?'对话记录':'历史记录';
      ui.title.textContent=windowTitle||recordTitle;
      host.setAttribute('aria-label',windowTitle?windowTitle+' · '+recordTitle:'历史记录与对话记录');
      ui.mode.textContent='模式：'+(layout==='dialog'?'对话':'历史');
      ui.codes.setAttribute('aria-pressed',String(codeVisible));ui.cards.dataset.layout=layout;
      ui.cards.replaceChildren();for(const event of events)ui.cards.append(makeCard(event));
      navigation();renderBadges();
    }
    async function loadPage(previousIndex){
      if(loading||disposed)return;
      const back=Number.isInteger(previousIndex);if(back&&(previousIndex<0||previousIndex>=pageStarts.length))return;
      loading=true;const version=generation,request=context.request,requestCursor=back?pageStarts[previousIndex]:cursor;
      const viewportTop=ui.cards.getBoundingClientRect?.().top;
      const anchor=Array.from(ui.cards.children).find(card=>card.getBoundingClientRect?.().bottom>viewportTop)||ui.cards.children[0];
      const top=anchor?.getBoundingClientRect?.().top,scroll=ui.cards.scrollTop;
      ui.more.disabled=true;
      try{
        const result=await request('read_page',{journal_id:journalId,projection,cursor:requestCursor,limit:100});
        if(stale(version))return;
        if(!result?.journal||result.journal.journal_id!==journalId||!Array.isArray(result.events)||result.events.length>100
          ||result.next_cursor!==null&&typeof result.next_cursor!=='string'||result.next_cursor!==null&&result.next_cursor===requestCursor
          ||journal?.host_installation_id!==result.journal.host_installation_id)throw new Error('Invalid page');
        if(back)pageIndex=previousIndex;
        else{pageIndex++;pageStarts[pageIndex]=requestCursor;}
        journal=result.journal;cursor=result.next_cursor;readThrough=Number(journal.last_seq||0);newerPending=false;
        for(const event of result.events){
          if(projection==='conversation'&&!conversationEligible(event)){pendingCount++;continue;}
          if(events.some(item=>item.event_id===event.event_id))continue;
          events.push(event);
        }
        events.sort((a,b)=>a.seq-b.seq);events=back?events.slice(0,120):events.slice(-120);
        const kept=new Set(events.map(event=>event.event_id));for(const key of bodies.keys())if(!kept.has(key))bodies.delete(key);
        render();
        const replacement=Array.from(ui.cards.children).find(card=>card.dataset.eventId===anchor?.dataset.eventId);
        if(replacement&&top!==undefined)ui.cards.scrollTop+=replacement.getBoundingClientRect().top-top;
        else ui.cards.scrollTop=scroll;
        message(projection==='conversation'?'仅显示已接受输入与已确认回复；候选或来源未确认内容请查看历史记录'+(pendingCount?'（存在未确认记录）':''):
          events.length?'完整正文按需读取；终端控制字符仅显示为衍生文本':'尚无记录；请检查绑定、采集状态与覆盖范围');
      }catch{if(!stale(version))message('无法读取记录；请检查连接、权限、来源绑定或升级主机');}
      finally{if(!stale(version)){loading=false;navigation();}}
    }
    const atBottom=()=>ui.cards.scrollHeight-ui.cards.scrollTop-ui.cards.clientHeight<=24;
    async function refresh(force=false){
      if(disposed||!host||host.hidden||!journal||loading||pageIndex<0)return false;
      loading=true;const version=generation,request=context.request;
      try{
        const status=await request('status',{journal_id:journalId});if(stale(version))return false;
        if(status?.journal_id!==journalId||status.host_installation_id!==journal.host_installation_id)throw new Error('Invalid status');
        journal=status;renderBadges();
        if(Number(status.last_seq||0)<=readThrough&&!newerPending)return true;
        newerPending=true;navigation();
        // Refresh is deliberately bounded. An archive cursor is opaque, so an
        // older reader advances explicitly instead of scanning to its end.
        if(!force&&(cursor||selected()||!atBottom())){message('有新记录；当前阅读位置与选择已保留');return true;}
        let requestCursor=pageStarts[pageIndex],result=null,added=[];
        for(let count=0;count<2;count++){
          result=await request('read_page',{journal_id:journalId,projection,cursor:requestCursor,limit:100});
          if(stale(version))return false;
          if(result?.journal?.journal_id!==journalId||result.journal.host_installation_id!==journal.host_installation_id
            ||!Array.isArray(result.events)||result.events.length>100||result.next_cursor!==null&&typeof result.next_cursor!=='string'
            ||result.next_cursor!==null&&result.next_cursor===requestCursor)throw new Error('Invalid page');
          added=result.events.filter(event=>(projection!=='conversation'||conversationEligible(event))&&!events.some(old=>old.event_id===event.event_id));
          // An exactly-full old tail page returns only its original rows. One
          // additional page reaches the first new rows without an unbounded scan.
          if(added.length||!result.next_cursor||count===1)break;
          requestCursor=result.next_cursor;
        }
        if(!force&&(selected()||!atBottom())){message('有新记录；当前阅读位置与选择已保留');return true;}
        const viewportTop=ui.cards.getBoundingClientRect?.().top;
        const anchor=Array.from(ui.cards.children).find(card=>card.getBoundingClientRect?.().bottom>viewportTop)||ui.cards.children[0];
        const anchorTop=anchor?.getBoundingClientRect?.().top,follow=atBottom();
        if(requestCursor!==pageStarts[pageIndex]){pageIndex++;pageStarts[pageIndex]=requestCursor;}
        journal=result.journal;cursor=result.next_cursor;readThrough=Number(journal.last_seq||0);newerPending=!!cursor;
        for(const event of added){events.push(event);ui.cards.append(makeCard(event));}
        events.sort((a,b)=>a.seq-b.seq);
        while(events.length>120){const removed=events.shift();bodies.delete(removed.event_id);Array.from(ui.cards.children).find(card=>card.dataset.eventId===removed.event_id)?.remove();}
        if(follow)ui.cards.scrollTop=Math.max(0,ui.cards.scrollHeight-ui.cards.clientHeight);
        else if(anchor&&ui.cards.contains(anchor)&&anchorTop!==undefined)ui.cards.scrollTop+=anchor.getBoundingClientRect().top-anchorTop;
        renderBadges();message(newerPending?'还有较新记录，请逐页加载；当前正文与阅读位置已保留':'记录已更新；完整正文按需读取');
        return true;
      }catch{if(!stale(version))message('无法刷新记录；已显示的正文与阅读位置已保留');return false;}
      finally{if(!stale(version)){loading=false;navigation();}}
    }
    async function listJournals(value,version,request){
      const result=await request('list',{limit:100,cursor:listCursor});if(stale(version))return;
      if(!Array.isArray(result?.journals)||result.journals.length>100||result.next_cursor!==null&&typeof result.next_cursor!=='string'
        ||result.next_cursor!==null&&result.next_cursor===listCursor)throw new Error('Invalid list');
      listCursor=result.next_cursor;
      if(result.journals.length===1&&!listCursor&&!ui.picker.children.length){await open({...value,journalId:result.journals[0].journal_id});return;}
      for(const item of result.journals)ui.picker.append(button(visibleJournalText((item.title||item.session_id||'记录')+' · '+item.journal_id+' · '+item.host_installation_id),()=>open({...value,journalId:item.journal_id})));
      // The picker is bounded independently of event cards; pagination exposes
      // the next archive page without keeping a host's whole catalog in DOM.
      while(ui.picker.children.length>100)ui.picker.children[0].remove();
      if(listCursor){const more=button('更多终端记录',async()=>{more.disabled=true;try{await listJournals(value,version,request);if(!stale(version))more.remove();}catch{if(!stale(version)){more.disabled=false;message('无法读取更多终端记录');}}});ui.picker.append(more);}
      message(result.journals.length?'请选择独立终端记录':'暂无可用记录；请检查来源绑定和采集状态');
    }
    async function open(value={}){
      if(disposed)throw new Error('Conversation viewer is disposed');
      ensure();clear();projection=value.projection==='conversation'?'conversation':'history';layout=value.layout==='dialog'?'dialog':'history';
      windowTitle=visibleJournalText(value.windowTitle||'');
      journalId=value.journalId||'';codeVisible=preferences.get(journalId)||false;show();render();
      const version=generation,request=context.request;message('正在读取记录…');
      try{
        if(typeof request!=='function')throw new Error('Unsupported');
        if(!journalId&&!value.sessionId){
          await listJournals(value,version,request);return;
        }
        const status=await request('status',journalId?{journal_id:journalId}:{session_id:value.sessionId});if(stale(version))return;
        if(!status?.journal_id||(journalId&&status.journal_id!==journalId)
          ||(value.sessionId&&status.session_id!==value.sessionId))throw new Error('Invalid status');
        journalId=status.journal_id;journal=status;codeVisible=preferences.get(journalId)||false;renderBadges();
        await loadPage();
      }catch{if(!stale(version))message('无法读取记录；当前来源可能未绑定、离线、不支持或无权限');}
    }
    function close(){clear();if(host){host.hidden=true;if(!context.inline&&host.close)host.close();host.style.display='none';}}
    function setCodeVisible(value){codeVisible=!!value;if(journalId)preferences.set(journalId,codeVisible);render();}
    return {open,close,refresh:()=>refresh(),setCodeVisible,setContext(value){clear();context={...context,...value};preferences.clear();},dispose(){if(disposed)return;clear();disposed=true;preferences.clear();host?.remove();host=null;ui=null;}};
  }
  // Pure source-daemon control state. Transport/authentication and draft storage
  // belong to the invoking frontend; this helper never opens a socket or PTY.
  function createManagedController(options={}){
    const fields=['window_id','session_id','process_generation','journal_id'];
    const states=new Set(['starting','ready','idle','running','waiting_permission','interrupting','closing','ended','error','unknown']);
    const copy=value=>value===undefined?null:JSON.parse(JSON.stringify(value));
    const id=value=>typeof value==='string'&&value.length>0&&value.length<=256&&!/[\x00-\x1f\x7f]/.test(value);
    const textOK=value=>{if(typeof value!=='string'||!value.length)return false;const bytes=encoder.encode(value);return bytes.length<=65536&&new TextDecoder().decode(bytes)===value;};
    const fitsEnvelope=record=>encoder.encode(JSON.stringify({request_id:'x'.repeat(64),action:'submit_managed',params:{...record.target,nonce:record.nonce,text:record.text}})).length<=196608;
    const sameTarget=(a,b)=>!!a&&!!b&&fields.every(key=>a[key]===b[key]);
    function targetOf(value){if(!value||!fields.every(key=>id(value[key])))throw new Error('Invalid managed target');return Object.fromEntries(fields.map(key=>[key,value[key]]));}
    function nonce(){
      if(root.crypto?.randomUUID)return root.crypto.randomUUID();
      if(!root.crypto?.getRandomValues)throw new Error('Secure random unavailable');
      const bytes=root.crypto.getRandomValues(new Uint8Array(16));bytes[6]=bytes[6]&15|64;bytes[8]=bytes[8]&63|128;
      return Array.from(bytes,(byte,index)=>([4,6,8,10].includes(index)?'-':'')+byte.toString(16).padStart(2,'0')).join('');
    }
    let context={...options},target=targetOf(options.target),targetKey=options.targetKey??options.target?.targetKey??'';
    let version=0,dead=false,state='unknown',pending=null,receipt=null,active=null,permissions=[],policy=null,lastOutcome=null;
    let provider=null,providerVersion=null,providerRoot=null,error=null,operation=null,query=null,submissionFlight=null,cancelEpoch=0;
    const accepted=new Set(),cancelledSubmissions=new Set(),controls=new Map();
    const current=()=>{try{return !dead&&typeof context.request==='function'&&typeof context.isCurrent==='function'&&!!context.isCurrent();}catch{return false;}};
    const valid=epoch=>epoch===version&&current();
    const idle=()=>['ready','idle'].includes(state)&&!active;
    function snapshot(){return copy({target,targetKey,state:dead?'disposed':state,pending,receipt,active_submission:active,permissions,provider,
      provider_version:providerVersion,provider_root_id:providerRoot,policy,last_outcome:lastOutcome,operation,error,
      canSubmit:current()&&!pending&&!operation&&idle()});}
    function notify(){const value=snapshot();if(current()&&typeof context.onState==='function'){try{Promise.resolve(context.onState(value)).catch(()=>{});}catch{}}return value;}
    function fail(code){error=code;return notify();}
    function exportPending(){return pending?copy(pending):null;}
    function restorePending(record){
      if(dead||pending||typeof targetKey!=='string'||!targetKey||record?.schema!==1||record.targetKey!==targetKey
        ||!sameTarget(record.target,target)||typeof record.nonce!=='string'||!/^[A-Za-z0-9_-]{1,64}$/.test(record.nonce)||!textOK(record.text)||!fitsEnvelope(record))return false;
      pending={schema:1,targetKey,target:{...target},nonce:record.nonce,text:record.text,phase:'unknown'};error=null;notify();return true;
    }
    function accept(value,record,fromStatus=false){
      if(!value||!sameTarget(value,record.target)||value.nonce!==record.nonce||value.state!=='accepted'
        ||!id(value.submission_id)||!id(value.event_id)||!Number.isSafeInteger(value.seq)||value.seq<1
        ||typeof value.dispatch_state!=='string'||value.dispatch_state.length>64)return false;
      receipt=Object.fromEntries([...fields,'nonce','submission_id','event_id','seq','state','dispatch_state']
        .filter(key=>value[key]!==undefined).map(key=>[key,value[key]]));
      if(typeof value.replayed==='boolean')receipt.replayed=value.replayed;
      pending=null;error=null;
      if(!fromStatus&&lastOutcome?.submission_id!==value.submission_id)lastOutcome=null;
      if(!fromStatus&&!['closing','ended'].includes(state)){state='running';active={submission_id:value.submission_id,cancel_version:0};permissions=[];}
      if(!accepted.has(record.nonce)){
        accepted.add(record.nonce);
        if(typeof context.onAccepted==='function'){try{Promise.resolve(context.onAccepted(copy(receipt),copy(record))).catch(()=>{});}catch{}}
      }
      return true;
    }
    async function sendPending(){
      if(!current()||!pending)return fail('stale_target');
      const epoch=version,record=pending,request=context.request;submissionFlight=record;record.phase='sending';error=null;notify();
      try{
        if(!valid(epoch)||pending!==record)return snapshot();
        const result=await request('submit_managed',{...record.target,nonce:record.nonce,text:record.text});
        if(!valid(epoch)||pending!==record)return snapshot();
        if(!accept(result,record)){record.phase='unknown';error='acceptance_unknown';}
      }catch{if(valid(epoch)&&pending===record){record.phase='unknown';error='acceptance_unknown';}}
      finally{if(submissionFlight===record)submissionFlight=null;}
      return valid(epoch)?notify():snapshot();
    }
    async function submit(text){
      if(!current())return fail('stale_target');
      if(pending)return snapshot();
      if(active||operation||['starting','running','waiting_permission','interrupting','closing','ended','error'].includes(state))return fail('session_busy');
      if(!textOK(text))return fail('invalid_text');
      let token;try{token=nonce();}catch{return fail('secure_random_unavailable');}
      if(!fitsEnvelope({target,nonce:token,text}))return fail('request_too_large');
      pending={schema:1,targetKey,target:{...target},nonce:token,text,phase:'sending'};
      return sendPending();
    }
    async function retry(){
      if(!current())return fail('stale_target');
      if(!pending||pending.phase!=='absent'||!idle()||operation)return fail('retry_not_confirmed');
      return sendPending();
    }
    function applyStatus(value){
      if(!sameTarget(value,target)||!states.has(value.state)||!Array.isArray(value.permissions)||value.permissions.length>32
        ||encoder.encode(JSON.stringify(value)).length>196608)throw new Error('Invalid managed status');
      const outcome=value.last_outcome;
      if(outcome!==null&&outcome!==undefined&&(!id(outcome.submission_id)
        ||!['completed','failed','interrupted','blocked','ambiguous','not_dispatched','cancelled'].includes(outcome.outcome)
        ||typeof outcome.has_final!=='boolean'||outcome.has_final&&outcome.outcome!=='completed'
        ||outcome.reason!==null&&outcome.reason!==undefined&&(typeof outcome.reason!=='string'||!/^[A-Za-z0-9_:-]{0,128}$/.test(outcome.reason))))throw new Error('Invalid managed outcome');
      const next=value.active_submission;
      if(next!==null&&next!==undefined&&(!id(next.submission_id)||next.provider_turn_id!==undefined&&next.provider_turn_id!==null&&!id(next.provider_turn_id)
        ||next.cancel_version!==undefined&&(!Number.isSafeInteger(next.cancel_version)||next.cancel_version<0)))throw new Error('Invalid active submission');
      if(active?.submission_id!==next?.submission_id||active?.provider_turn_id!==next?.provider_turn_id
        ||Number(next?.cancel_version||0)>Number(active?.cancel_version||0))cancelEpoch++;
      active=next?Object.fromEntries(['submission_id','provider_turn_id','cancel_version','nonce'].filter(key=>next[key]!==undefined).map(key=>[key,next[key]])):null;
      if(operation?.submission_id&&operation.submission_id!==active?.submission_id)operation=null;
      permissions=value.permissions.filter(item=>item&&item.state==='pending'&&id(item.permission_request_id)&&item.submission_id===active?.submission_id
        &&(item.provider_turn_id??null)===(active?.provider_turn_id??null)
        &&typeof(item.details??'')==='string'&&encoder.encode(item.details??'').length<=65536).map(item=>({
          permission_request_id:item.permission_request_id,submission_id:item.submission_id,...(item.provider_turn_id?{provider_turn_id:item.provider_turn_id}:{}),
          tool_name:typeof item.tool_name==='string'?item.tool_name:'',details:item.details||'',state:'pending',
          choices:Array.isArray(item.choices)?item.choices.filter(choice=>choice==='deny'||choice==='allow_once'&&!item.requires_user_interaction):['deny'],
          ...(item.requires_user_interaction?{requires_user_interaction:true}:{})}));
      state=value.state;provider=typeof value.provider==='string'?value.provider:null;providerVersion=typeof value.provider_version==='string'?value.provider_version:null;
      providerRoot=typeof value.provider_root_id==='string'?value.provider_root_id:null;policy=copy(value.policy);error=null;
      if(outcome!==undefined)lastOutcome=outcome===null?null:{submission_id:outcome.submission_id,outcome:outcome.outcome,reason:outcome.reason??null,has_final:outcome.has_final};
      if(active&&(cancelledSubmissions.has(active.submission_id)||Number(active.cancel_version||0)>0)){
        cancelledSubmissions.add(active.submission_id);if(!['closing','ended','error'].includes(state))state='interrupting';
      }
      if(['interrupting','closing','ended','error'].includes(state))permissions=[];
      if(operation?.permission_request_id&&!permissions.some(item=>item.permission_request_id===operation.permission_request_id))operation=null;
      if(operation?.action==='close_managed'&&['closing','ended'].includes(state))operation=null;
    }
    async function reconcile(){
      if(!current())return fail('stale_target');
      if(query)return snapshot();
      const epoch=version,request=context.request,record=pending,marker={};query=marker;
      try{
        const result=await request('managed_status',{...target,...(record?{nonce:record.nonce}:{})});
        if(!valid(epoch))return snapshot();
        applyStatus(result);
        if(record&&pending===record){
          if(result.receipt){if(!accept(result.receipt,record,true)){record.phase='unknown';error='acceptance_unknown';}}
          else record.phase=submissionFlight===record?'sending':result.receipt===null&&idle()?'absent':'unknown';
        }
      }catch{if(valid(epoch))error='status_unavailable';}
      finally{if(query===marker)query=null;}
      return valid(epoch)?notify():snapshot();
    }
    function controlKey(action,params){return action==='close_managed'?'close':action+':'+params.submission_id+':'+(params.permission_request_id||'');}
    async function control(action,params,guard,onSuccess){
      if(!current())return fail('stale_target');
      const key=controlKey(action,params),prior=controls.get(key);
      if(prior?.params.decision!==undefined&&prior.params.decision!==params.decision)return fail('conflicting_control');
      if(prior&&prior.phase!=='unknown')return snapshot();
      let token;try{token=prior?.nonce||nonce();}catch{return fail('secure_random_unavailable');}
      const epoch=version,request=context.request,entry=prior||{action,nonce:token,params:{...params},phase:'sending'};entry.phase='sending';params=entry.params;controls.set(key,entry);
      while(controls.size>64)controls.delete(controls.keys().next().value);
      operation={action,nonce:token,phase:'sending',...params};error=null;notify();
      try{
        if(!valid(epoch)||!guard())return snapshot();
        const result=await request(action,{...target,...params,nonce:token});
        if(!valid(epoch)||!guard())return snapshot();
        if(result?.nonce!==undefined&&result.nonce!==token||fields.some(key=>result?.[key]!==undefined)&&!sameTarget(result,target))throw new Error('Control target mismatch');
        if(!onSuccess(result))throw new Error('Invalid control acknowledgement');
        entry.phase='acknowledged';if(operation?.nonce===token)operation=null;
      }catch{if(valid(epoch)&&guard()){entry.phase='unknown';if(operation?.nonce===token)operation={...operation,phase:'unknown'};error='control_unknown';}}
      return valid(epoch)?notify():snapshot();
    }
    async function interrupt(){
      if(!current())return fail('stale_target');
      if(!active||['closing','ended','error'].includes(state))return fail('no_active_submission');
      const captured=copy(active),submission=captured.submission_id;
      cancelEpoch++;cancelledSubmissions.add(submission);permissions=[];state='interrupting';
      return control('interrupt_managed',{submission_id:submission,...(captured.provider_turn_id?{provider_turn_id:captured.provider_turn_id}:{})},
        ()=>active?.submission_id===submission&&!['closing','ended','error'].includes(state),result=>{
          if(result?.submission_id!==submission||!['interrupt_requested','not_dispatched'].includes(result.state)
            ||!Number.isSafeInteger(result.cancel_version)||result.cancel_version<Number(captured.cancel_version||0))return false;
          active={...active,cancel_version:result.cancel_version};state='interrupting';return true;
        });
    }
    async function respondPermission(permissionID,decision){
      if(!current())return fail('stale_target');
      if(!['allow_once','deny'].includes(decision))return fail('invalid_decision');
      const permission=permissions.find(item=>item.permission_request_id===permissionID);
      if(!permission||!active||!permission.choices.includes(decision)||decision==='allow_once'&&permission.requires_user_interaction
        ||['interrupting','closing','ended','error'].includes(state))return fail('stale_permission');
      const captured=copy(active),epoch=cancelEpoch;
      return control('respond_managed_permission',{submission_id:captured.submission_id,...(captured.provider_turn_id?{provider_turn_id:captured.provider_turn_id}:{}),permission_request_id:permissionID,decision},
        ()=>epoch===cancelEpoch&&active?.submission_id===captured.submission_id
          &&(active?.provider_turn_id??null)===(captured.provider_turn_id??null)&&permissions.some(item=>item.permission_request_id===permissionID),result=>{
          if(result?.permission_request_id!==permissionID||result.decision!==decision||result.state!=='decided'
            ||result.submission_id!==undefined&&result.submission_id!==captured.submission_id
            ||result.cancel_version!==undefined&&result.cancel_version!==Number(captured.cancel_version||0))return false;
          permissions=permissions.filter(item=>item.permission_request_id!==permissionID);return true;
        });
    }
    async function closeSession(){
      if(!current())return fail('stale_target');
      if(state==='ended')return snapshot();
      cancelEpoch++;permissions=[];state='closing';
      return control('close_managed',{},()=>state==='closing',result=>{
        if(!sameTarget(result,target)||!['closing','ended'].includes(result.state))return false;
        state=result.state;if(state==='ended')active=null;return true;
      });
    }
    function setContext(value={}){
      const old=exportPending(),next={...context,...value},nextTarget=targetOf(next.target);
      version++;context=next;target=nextTarget;targetKey=next.targetKey??next.target?.targetKey??'';
      state='unknown';pending=null;receipt=null;active=null;permissions=[];policy=null;lastOutcome=null;provider=null;providerVersion=null;providerRoot=null;
      error=null;operation=null;query=null;submissionFlight=null;cancelEpoch++;accepted.clear();cancelledSubmissions.clear();controls.clear();
      if(value.pendingState)restorePending(value.pendingState);else notify();return old;
    }
    if(options.pendingState)restorePending(options.pendingState);
    return {snapshot,submit,reconcile,interrupt,respondPermission,closeSession,retry,exportPending,restorePending,setContext,
      dispose(){const old=exportPending();version++;dead=true;pending=null;receipt=null;active=null;permissions=[];lastOutcome=null;operation=null;query=null;submissionFlight=null;controls.clear();accepted.clear();cancelledSubmissions.clear();return old;}};
  }
  const api = {composerAction,createComposerSubmitter,createComposerEcho,presentationPolicy,localHistoryPolicy,createManagedController,createConversationViewer,parseCodeFences,formatQuote,agentProfiles,commonAgentProfile,validateAgentProfile,loadAgentProfile,agentModeFromSession,inferAgentContent,createAgentDetector,highlightSpans,createHighlighter,createScreenHistory,pack, unpack, targetFrame, sessionPayload, historyPayload, historyFrameBytes, terminalInput, cursorKey, snapshotBytes, terminalSize, ConnectionMeter, formatRate, redrawSizes, disposePane, socketURL, secureEndpoint,updateHintRevision,
    retryDelay: attempt => Math.min(15000, 500 * 2 ** Math.min(10, attempt)),
    rendererName: name => ['auto', 'dom', 'webgl'].includes(name) ? name : 'auto'};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.TerminalClient = api;
})(typeof window === 'undefined' ? globalThis : window);

/* Shared read-only cockpit. Mount explicitly from a terminal client after bootstrap. */
(function(root) {
  'use strict';
  const shared = typeof module !== 'undefined' && module.exports ? module.exports : root.TerminalClient;
  const readTools = new Set(['list_tasks', 'get_task_status', 'get_wiki_entry', 'search_task_events', 'get_usage_overview',
    'search_skills', 'read_skill', 'list_evolution','get_evolution_status','chat']);
  const aggregateTools = new Set(['list_tasks','search_task_events','get_usage_overview','list_evolution','chat']);

  function cockpitEndpoint(value) {
    if (typeof value !== 'string' || !/^wss?:\/\/[^\\\s]+$/i.test(value) || value.length > 1024) {
      throw Error('请填写完整的 WSS 地址');
    }
    const url = new URL(value);
    const loopback = ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
    if ((url.protocol !== 'wss:' && !(loopback && url.protocol === 'ws:')) ||
        url.username || url.password || url.search || url.hash || !['/', '/hub/view'].includes(url.pathname)) {
      throw Error('使用 wss://主机/hub/view；本机可使用 ws://127.0.0.1');
    }
    url.pathname = '/hub/view';
    return url.href;
  }

  function createCockpitReader({endpoint, project, token, onClose, Socket = root.WebSocket}) {
    const address = cockpitEndpoint(endpoint);
    if (typeof project !== 'string' || !project.trim() || project.length > 128 || /[\x00-\x1f\x7f]/.test(project)) throw Error('项目无效');
    if (typeof token !== 'string' || !token || token.length > 8192 || /[\s\x00-\x1f\x7f]/.test(token)) throw Error('请输入只读访问凭据');
    const ws = new Socket(address), pending = new Map();
    let connected = false, closed = false, sequence = 0, resolveReady, rejectReady, projects=[project];
    const ready = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject; });
    const connectionTimer = setTimeout(() => close('连接超时'), 10000);
    function close(reason = '连接已关闭') {
      if (closed) return;
      closed = true;
      clearTimeout(connectionTimer);
      token = '';
      rejectReady(Error(reason));
      for (const job of pending.values()) { clearTimeout(job.timer); job.reject(Error(reason)); }
      pending.clear();
      ws.close();
      if (onClose) onClose(reason);
    }
    ws.onopen = () => {
      if (closed) return;
      try { ws.send(JSON.stringify({op:'hello', token, project})); }
      catch { close('连接未能发送'); }
      token = '';
    };
    ws.onmessage = event => {
      if (closed) return;
      try {
        if (typeof event.data !== 'string' || event.data.length > 524288) throw Error();
        const data = JSON.parse(event.data);
        if (!connected) {
          if (data.op !== 'ready' || data.project !== project) throw Error();
          if (Array.isArray(data.projects)) {
            if(!data.projects.length || data.projects.length>512 || (project!=='*'&&!data.projects.includes(project)) ||
              data.projects.some(p=>typeof p!=='string'||!p||p==='*'||p.length>256)) throw Error();
            projects=[...new Set(data.projects)];
          }
          connected = true;
          clearTimeout(connectionTimer);
          resolveReady();
          return;
        }
        if (data.op !== 'response' || typeof data.id !== 'string') throw Error();
        const job = pending.get(data.id);
        if (!job) return;
        pending.delete(data.id);
        clearTimeout(job.timer);
        if (data.ok === true && data.result && typeof data.result === 'object') job.resolve(data.result);
        else job.reject(Error('查询失败，请核对项目权限和服务状态'));
      } catch { close('无法验证驾驶舱连接'); }
    };
    ws.onclose = event => close(event?.code===1008?'项目授权或证据访问已变化，请重新连接核对':'连接已断开，数据已过期');
    ws.onerror = () => close('驾驶舱服务不可用');
    function request(tool, args = {}, scope = project) {
      if (!readTools.has(tool) || closed || !connected || pending.size >= 8 ||
          !(projects.includes(scope) || (scope==='*' && aggregateTools.has(tool)))) return Promise.reject(Error('当前查询不可用'));
      const id = 'view-' + (++sequence);
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          pending.delete(id);
          reject(Error('查询超时，可重新查询'));
        }, tool === 'chat' ? 45000 : 12000);
        pending.set(id, {resolve, reject, timer});
        try { ws.send(JSON.stringify({op:'request', id, tool, project:scope, arguments:args})); }
        catch { close('连接未能发送'); }
      });
    }
    return {ready, request, close, get projects(){return [...projects];}, get idle(){return !pending.size;}};
  }

  function mountCockpit({menu, headerBefore, mobile = false, beforeOpen}) {
    if (!menu || !headerBefore || document.getElementById('cockpit-open')) return null;
    const doc = menu.ownerDocument;
    const key = 'sma.cockpit.read-settings';
    const defaults = {endpoint:'', project:''};
    let preferences = {...defaults};
    try {
      const saved = JSON.parse(root.localStorage.getItem(key) || '{}');
      if (typeof saved.project === 'string' && saved.project.length <= 128 && saved.endpoint) {
        preferences = {endpoint:cockpitEndpoint(saved.endpoint), project:saved.project};
      }
    } catch {}
    const element = (tag, text, className) => {
      const node = doc.createElement(tag);
      if (text !== undefined) node.textContent = text;
      if (className) node.className = className;
      return node;
    };
    function button(label, click) {
      const b = element('button', label);
      b.type = 'button';
      b.addEventListener('click', click);
      return b;
    }
    let dialog, nav, main, message, projectLabel, scopePicker, sourceLabel, opener, reader = null, live = false;
    let revision = 0, tab = 'chat', detail = false, configuration = false, refreshTimer = 0;
    let taskId = '', taskProject = '', scopeProject = '', query = '', librarySource = '', chatHistory = [], chatDraft = '', backTo = null;
    let taskFilter='attention', authorizationTimer=0;
    const headings = {chat:'对话', tasks:'待处理', usage:'额度', library:'mylib'};
    const menuEntry = button('驾驶舱', () => open(menuEntry));
    menuEntry.id = 'cockpit-open';
    menuEntry.setAttribute('aria-haspopup', 'dialog');
    menu.append(menuEntry);
    const shortcut = button('驾驶舱', () => open(shortcut));
    shortcut.id = 'cockpit-shortcut';
    shortcut.hidden = !preferences.endpoint;
    shortcut.setAttribute('aria-haspopup', 'dialog');
    headerBefore.before(shortcut);
    const native = () => mobile ? null : root.pywebview?.api;
    const printable = value => typeof value === 'string' || typeof value === 'number' ? String(value) : '';
    function info(text) { if (message) message.textContent = text; }
    function valid(version) { return dialog?.open && revision === version; }
    function refreshTitle() {
      projectLabel.textContent = live && reader.projects.length>1 ? '' : live ? reader.projects[0] : '未连接';
      projectLabel.title = live ? preferences.endpoint : '';
      scopePicker.hidden=!live || reader.projects.length<2;
      if(!live)scopePicker.replaceChildren();
      if(!scopePicker.hidden){
        scopePicker.replaceChildren(new Option('全部授权项目','*'),...reader.projects.map(p=>new Option(p,p)));
        scopePicker.value=scopeProject;
      }
    }
    function field(label, value = '', type = 'text') {
      const el = element('label', label);
      const input = element('input');
      input.type = type;
      input.value = value;
      input.autocomplete = 'off';
      el.append(input);
      return {el, input};
    }
    function clearContent() {
      revision++;
      clearTimeout(refreshTimer);
      backTo = null;
      if (main) main.replaceChildren();
      info('');
      detail = false;
      configuration = false;
    }
    function wipeSession() {
      chatHistory = [];
      chatDraft = '';
      taskId = '';
      taskProject = '';
      query = '';
      root.dispatchEvent(new Event('sma-cockpit-context-invalidated'));
      clearContent();
    }
    function disconnect(reason) {
      const old = reader;
      reader = null;
      live = false;
      scopeProject = '';
      clearInterval(authorizationTimer);
      old?.close();
      wipeSession();
      if (dialog?.open) { render(); info(reason); }
    }
    function style() {
      const sheet = element('style');
      sheet.textContent = [
        '#sma-cockpit{position:fixed;inset:0 0 0 auto;margin:0;width:min(480px,100%);height:100%;height:100dvh;max-width:100vw;max-height:100dvh;box-sizing:border-box;border:0;border-left:1px solid #465365;border-radius:0;background:#141a21;color:#edf2f7;padding:14px;font:14px/1.5 system-ui,sans-serif;overflow:hidden}',
        '#sma-cockpit[open]{display:flex;flex-direction:column;gap:10px}#sma-cockpit::backdrop{background:rgba(0,0,0,.2)}',
        '#sma-cockpit .hub-top,#sma-cockpit .hub-tabs,#sma-cockpit .hub-row{display:flex;align-items:center;gap:6px;flex-wrap:wrap}',
        '#sma-cockpit .hub-top h2{font-size:17px;margin:0}#sma-cockpit .hub-project{flex:1;min-width:0;overflow-wrap:anywhere;color:#b8c9d8}',
        '#sma-cockpit .hub-body{overflow:auto;overscroll-behavior:contain;min-height:0;flex:1;display:flex;flex-direction:column;gap:10px}#sma-cockpit .hub-body>*{flex:none}',
        '#sma-cockpit button{padding:5px 9px;min-height:32px;border:1px solid #465365;border-radius:4px;color:inherit;background:#232e38;font:inherit;cursor:pointer}',
        '#sma-cockpit button[aria-current=true]{border-color:#7fbde9;color:#b7e0ff}#sma-cockpit button:disabled{opacity:.6}',
        '#sma-cockpit input,#sma-cockpit textarea,#sma-cockpit select{box-sizing:border-box;display:block;width:100%;min-width:0;color:inherit;background:#0e141a;border:1px solid #465365;border-radius:4px;padding:7px;font:inherit}',
        '#sma-cockpit label{display:block;margin:4px 0}#sma-cockpit textarea{min-height:64px;resize:vertical}#sma-cockpit p{margin:2px 0;white-space:pre-wrap;overflow-wrap:anywhere}',
        '#sma-cockpit .hub-status{color:#ced9a9;font-size:12px;overflow-wrap:anywhere}#sma-cockpit .hub-card{border-top:1px solid #34414d;padding:8px 0;overflow-wrap:anywhere}#sma-cockpit .hub-meta{font-size:12px;color:#b2c2cf}#sma-cockpit .hub-chat{flex:1;overflow:auto;min-height:65px}',
        '#sma-cockpit [hidden]{display:none!important}#sma-cockpit .hub-skill{white-space:pre-wrap;overflow-wrap:anywhere;max-width:100%;font:inherit;margin:0}',
        '#sma-cockpit.hub-phone{width:100%;padding:12px}#sma-cockpit.hub-mobile button,#sma-cockpit.hub-mobile select{min-height:44px}#sma-cockpit.hub-mobile .hub-tabs button{flex:1}',
        '@media(max-width:650px){#sma-cockpit{width:100%;padding:12px}#sma-cockpit button{min-height:40px}#sma-cockpit .hub-tabs button{flex:1}}'
      ].join('\n');
      doc.head.append(sheet);
    }
    function build() {
      style();
      dialog = element('dialog');
      dialog.id = 'sma-cockpit';
      if(mobile){
        dialog.classList.add('hub-mobile');
        if(Math.min(root.screen.width,root.screen.height)<600)dialog.classList.add('hub-phone');
      }
      dialog.setAttribute('aria-label', '驾驶舱');
      const top = element('div', undefined, 'hub-top');
      projectLabel = element('span', '', 'hub-project');
      scopePicker=element('select');
      scopePicker.setAttribute('aria-label','选择授权项目');
      scopePicker.style.width='auto';scopePicker.style.maxWidth='55%';
      scopePicker.onchange=()=>{scopeProject=scopePicker.value;wipeSession();render();};
      top.append(element('h2', '驾驶舱'), projectLabel, scopePicker, button('连接设置', settings), button('关闭', close));
      nav = element('nav', undefined, 'hub-tabs');
      nav.setAttribute('aria-label', '驾驶舱视图');
      for (const [id, label] of Object.entries(headings)) {
        const item = button(label, () => { tab = id; render(); });
        item.dataset.tab = id;
        nav.append(item);
      }
      main = element('div', undefined, 'hub-body');
      message = element('p', '', 'hub-status');
      message.setAttribute('role','status');
      dialog.append(top, nav, message, main);
      doc.body.append(dialog);
      dialog.addEventListener('cancel', event => { event.preventDefault(); back(); });
      dialog.addEventListener('keydown', e => {
        if (e.key === 'Escape' && !e.isComposing) {e.preventDefault(); back();}
        e.stopPropagation();
      });
      dialog.addEventListener('close', () => {
        clearContent();
        root.dispatchEvent(new Event('sma-cockpit-context-invalidated'));
        const fallback=mobile?headerBefore:doc.getElementById('app-menu-toggle');
        (opener?.getClientRects().length?opener:fallback)?.focus();
      });
    }
    function open(trigger) {
      if (!dialog) build();
      if (dialog.open) return;
      if (beforeOpen) beforeOpen();
      opener = trigger;
      dialog.showModal();
      render();
    }
    function close() { if (dialog?.open) dialog.close(); }
    function back() {
      if (!dialog?.open) return false;
      if (backTo) backTo();
      else if (detail || (configuration && live)) render();
      else close();
      return true;
    }
    function settings() {
      clearContent();
      configuration = true;
      refreshTitle();
      const form = element('form');
      const endpoint = field('驾驶舱服务地址', preferences.endpoint);
      endpoint.input.placeholder = 'wss://host/hub/view';
      const project = field('项目', preferences.project||'*');
      project.input.title='项目 ID 或 *（全部获准项目）';
      const secret = field('只读访问凭据', '', 'password');
      secret.input.maxLength = 8192;
      project.input.maxLength = 128;
      endpoint.input.maxLength = 1024;
      for (const f of [endpoint,project,secret]) f.input.required = true;
      const connect = element('button', '连接项目');
      connect.type = 'submit';
      form.append(endpoint.el, project.el, secret.el, connect);
      const forget = button('清除连接设置', () => {
        preferences = {...defaults};
        try { root.localStorage.removeItem(key); } catch {}
        shortcut.hidden = true;
        disconnect('已清除驾驶舱连接设置');
      });
      if (preferences.endpoint) form.append(forget);
      form.onsubmit = async event => {
        event.preventDefault();
        let address;
        try { address = cockpitEndpoint(endpoint.input.value.trim()); }
        catch (error) { info(error.message); return; }
        const selectedProject = project.input.value.trim(), credential = secret.input.value;
        secret.input.value = '';
        const previous = reader;
        reader = null;
        previous?.close();
        live = false;
        wipeSession();
        const version = revision;
        info('正在连接…');
        try {
          const next = createCockpitReader({endpoint:address,project:selectedProject,token:credential,
            onClose:reason => {if (reader===next) disconnect(reason);}});
          reader = next;
          await next.ready;
          if (reader !== next) return;
          preferences = {endpoint:address, project:selectedProject};
          live = true;
          scopeProject=next.projects.length>1?'*':next.projects[0];
          clearInterval(authorizationTimer);
          authorizationTimer=setInterval(()=>{
            if (next===reader && next.idle && dialog.open && !doc.hidden)
              next.request('list_tasks',{limit:1},scopeProject).catch(()=>{
                if(next===reader)disconnect('访问状态已变化，请重新连接');
              });
          },15000);
          try { root.localStorage.setItem(key, JSON.stringify(preferences)); } catch {}
          shortcut.hidden = false;
          if (dialog.open) render();
        } catch (error) {
          if (valid(version)) { settings(); info(error.message); }
        }
      };
      main.append(form);
    }
    async function load(version, operation, view, keepControls=false) {
      try {
        const result = await operation();
        if (valid(version)) view(result);
      } catch (err) {
        if (valid(version)) {
          if (!keepControls) main.replaceChildren();
          main.append(button('重新查询',render));
          info(err.message || '读取失败');
        }
      }
    }
    function card(title, body, meta) {
      const el = element('article', undefined, 'hub-card');
      el.append(element('strong', printable(title)));
      if (body) el.append(element('p',printable(body)));
      if (meta) el.append(element('p',printable(meta),'hub-meta'));
      return el;
    }
    function statusLabel(value) {
      const labels = {running:'执行中',completed:'已完成',blocked:'遇到阻塞',failed:'失败',
        passed:'验证通过',none:'',conflict:'结论冲突',pending:'待处理',awaiting_verification:'待验证',
        needs_review:'待裁决',baseline_unaligned:'待对齐基线',ok:'已更新',unsupported:'来源不支持',
        unavailable:'采集不可用',rate_limited:'受限，待重试',auth_required:'需要重新授权'};
      return Object.hasOwn(labels,value) ? labels[value] : printable(value);
    }
    function showWiki(ref,id,project) {
      clearContent();detail=true;backTo=()=>showTask(id,project);
      const version=revision;
      main.append(button('返回任务',backTo));
      info('正在读取来源记录…');
      load(version,()=>reader.request('get_wiki_entry',{entry_id:ref.entry_id,version:ref.version},project),result=>{
        main.append(card(result.path || ref.entry_id,'',result.version),element('pre',printable(result.body),'hub-skill'));
        info('');
      });
    }
    function showTask(id,project) {
      clearContent();
      detail = true;
      const version = revision;
      main.append(button('返回', render));
      info('正在读取任务…');
      load(version, () => reader.request('get_task_status', {task_id:id},project), result => {
        const task=result.task || result;
        main.append(card(id, task.summary, [statusLabel(task.status),statusLabel(task.verification_status)].filter(Boolean).join(' · ')));
        const scope = button('围绕此任务提问', () => {
          if(taskId!==id||taskProject!==project){chatHistory=[];chatDraft='';}taskId=id;taskProject=project;tab='chat';render();
        });
        main.append(scope);
        for (const item of (Array.isArray(result.attempts)?result.attempts:[])) {
          const row=card(item.window||item.attempt_id,item.summary,
            [item.host,item.tool,item.role==='verify'?'验证执行':'主执行',statusLabel(item.status),item.attempt_id].filter(Boolean).join(' · '));
          if(item.wiki?.entry_id && item.wiki.version)row.append(button('查看 wiki 证据',()=>showWiki(item.wiki,id,project)));
          if(item.tool_version && item.generation)row.append(button('此执行的 mylib 版本',()=>evolution({
            taskId:id,project,attempt:item
          })));
          if(!mobile && root.SuperMonoCockpitFeedback?.open)row.append(button('反馈到此执行',()=>{
            const boundReader=reader;
            root.SuperMonoCockpitFeedback.open({endpoint:preferences.endpoint,project,taskId:id,attempt:item,
              refreshTask:()=>boundReader.request('get_task_status',{task_id:id},project),
              isCurrent:()=>valid(version)&&live&&reader===boundReader,dismiss:close});
          }));
          main.append(row);
        }
        for (const event of (Array.isArray(result.events)?result.events:[])) {
          main.append(card(event.summary||event.event_id, '', [event.event_id,event.status].filter(Boolean).join(' · ')));
        }
        info('');
      });
    }
    function evolution(target=null) {
      clearContent();detail=true;backTo=target?()=>showTask(target.taskId,target.project):render;
      const version=revision;
      main.append(button(target?'返回任务':'返回 mylib',backTo));
      info('正在读取获准查看的进化记录…');
      load(version,()=>reader.request('list_evolution',{limit:50},target?target.project:scopeProject),result=>{
        const items=(Array.isArray(result.items)?result.items:[]).filter(item=>
          !target || (item.tool===target.attempt.tool && item.tool_version===target.attempt.tool_version));
        for(const item of items){
          const project=target?.project||item.project || (scopeProject==='*'?'':scopeProject);
          const row=card(item.skill_id, item.published_version?'发布版本 '+item.published_version:'待评估',
            [project,item.tool,item.tool_version].filter(Boolean).join(' · '));
          if(project)row.append(button('查看版本证据',()=>showEvolution({...item,project},()=>evolution(target),target)));
          main.append(row);
        }
        info(items.length?'':'暂无可查看的进化记录');
      });
    }
    function showEvolution(item,previous,target=null) {
      clearContent();detail=true;backTo=previous;
      const version=revision;
      main.append(button('返回',previous));
      const params={tool:item.tool,tool_version:item.tool_version,skill_id:item.skill_id};
      if(item.candidate_id)params.candidate_id=item.candidate_id;
      if(target)Object.assign(params,{task_id:target.taskId,attempt_id:target.attempt.attempt_id,generation:target.attempt.generation});
      load(version,()=>reader.request('get_evolution_status',params,item.project),result=>{
        const state={unknown:'加载状态未知',confirmed:'已确认',dispatched:'已提交',pending_restart:'待重启',
          loaded:'已加载',failed:'加载失败',expired:'请求过期'}[result.loading_state]||'加载状态未知';
        const row=card(item.skill_id,'发布版本 '+(result.published_version||'尚无'),[item.tool,item.tool_version].join(' · '));
        if(target){
          row.append(element('p','执行 '+target.attempt.attempt_id+' · '+state));
          if(result.loaded_version)row.append(element('p','实际版本 '+result.loaded_version));
          if(!mobile && result.has_update && root.SuperMonoCockpitFeedback?.openEvolution){
            row.append(button('申请应用到此执行',()=>{
              const boundReader=reader;
              root.SuperMonoCockpitFeedback.openEvolution({endpoint:preferences.endpoint,project:target.project,
                taskId:target.taskId,attempt:target.attempt,skillId:item.skill_id,
                readStatus:()=>boundReader.request('get_evolution_status',params,target.project),
                refreshTask:()=>boundReader.request('get_task_status',{task_id:target.taskId},target.project),
                isCurrent:()=>valid(version)&&live&&reader===boundReader,dismiss:close});
            }));
          }
        }
        if(result.assessment){
          row.append(element('p','评估器 '+printable(result.assessment.evaluator),'hub-meta'));
          for(const [group,entry] of Object.entries(result.assessment.splits||{})){
            const verdict={passed:'通过',insufficient_evidence:'证据不足',rejected:'未通过'}[entry?.verdict]||'评估待核对';
            row.append(element('p',group+' · '+(entry?.evidence_revoked?'证据已撤回':verdict),'hub-meta'));
          }
        }
        main.append(row);info('');
      });
    }
    function tasks(version) {
      info('正在读取任务…');
      let before = null, pages = 0, signature='';
      const filter=element('select');filter.setAttribute('aria-label','任务筛选');
      filter.append(new Option('需处理','attention'),new Option('全部任务','all'));filter.value=taskFilter;
      filter.onchange=()=>{taskFilter=filter.value;render();};
      const list = element('div');
      const more = button('加载更多任务', () => fetchPage(false));
      main.append(filter,list,more);
      function scheduleRefresh(){
        refreshTimer=setTimeout(()=>{
          if(!valid(version))return;
          if(doc.hidden)scheduleRefresh();else fetchPage(true);
        },1500);
      }
      async function fetchPage(refresh) {
        more.disabled = true;
        clearTimeout(refreshTimer);
        await load(version, () => reader.request('list_tasks', {limit:40, ...(taskFilter==='attention'?{status:'attention'}:{}), ...(!refresh && before ? {before}: {})},scopeProject), result => {
          const rows = Array.isArray(result.tasks)?result.tasks:[];
          const nextSignature=JSON.stringify(rows);
          if(!refresh || signature!==nextSignature){
            if(refresh)list.replaceChildren();
            for (const item of rows) {
              const project=item.project||(scopeProject==='*'?'':scopeProject);
              const row = card(item.summary || item.task_id, '', [project,item.task_id,statusLabel(item.status),statusLabel(item.verification_status)].filter(Boolean).join(' · '));
              if(project)row.append(button('查看任务', () => showTask(item.task_id,project)));
              list.append(row);
            }
          }
          signature=nextSignature;
          if(!refresh)pages++;
          before = result.next_before;
          more.disabled = false;
          more.hidden = before == null;
          info(result.partial?'已显示部分结果，选择具体项目可继续分页':list.childElementCount?(scopeProject==='*'?'按项目汇总最近任务':''):(taskFilter==='attention'?'暂无待处理任务':'暂无任务'));
          if(pages<=1)scheduleRefresh();
        });
      }
      fetchPage(false);
    }
    function quota(version) {
      info('正在查询额度…');
      load(version, () => reader.request('get_usage_overview',{},scopeProject), result => {
        main.replaceChildren(button('刷新额度', render));
        const accounts=Array.isArray(result.accounts)?result.accounts:[];
        const periods={weekly:'本周',short:'短周期',daily:'每日',monthly:'每月',balance:'余额'};
        const formatAmount=(value,unit)=>value==null?'余量未知':'剩余 '+value+' '+(unit==='percent'?'%':unit||'');
        const freshness={fresh:'已更新',stale:'数据过期',period_ended:'周期已结束',missing:'待查询',query_failed:'查询失败'};
        const eligibility={available:'可用',blocked:'额度已受限',unknown:'可用状态未知'};
        for(const group of (Array.isArray(result.recommendations)?result.recommendations:[])){
          const ranked=Array.isArray(group.accounts)?group.accounts:[];
          if(ranked.length<2)continue;
          const row=card('可优先使用',[group.provider,group.pool_class,periods[group.period]||group.period,group.unit].filter(Boolean).join(' · '));
          for(const item of ranked.slice(0,5))row.append(element('p',[item.account,item.pool,formatAmount(item.remaining,group.unit)].filter(Boolean).join(' · ')));
          main.append(row);
        }
        for (const rec of accounts) {
          const latest=rec.latest||{}, good=rec.last_success;
          const capacity=rec.capacity||{};
          const projects=Array.isArray(rec.projects)?rec.projects.join(' / '):rec.project;
          const row=card(rec.account || '未知账号','',[projects,rec.pool,statusLabel(latest.status||'pending'),
            freshness[capacity.freshness],eligibility[capacity.eligibility]].filter(Boolean).join(' · '));
          const windows=Array.isArray(capacity.windows)?capacity.windows.slice():[];
          windows.sort((a,b)=>Number(b.period==='weekly')-Number(a.period==='weekly'));
          if(!windows.length)row.append(element('p','当前余量未知'));
          for(const item of windows){
            const period=periods[item.period]||item.period;
            row.append(element('p',period+' · '+formatAmount(item.state==='current'?item.remaining:null,item.unit)),
              element('p',(item.state==='period_ended'?'上期重置 ':'下次重置 ')+time(item.reset_at),'hub-meta'));
          }
          if(capacity.earliest_possible_at)row.append(element('p','预计最早可用 '+time(capacity.earliest_possible_at)));
          if(good && (!windows.length||windows.some(w=>w.state!=='current'))){
            const history=element('details');
            history.append(element('summary','历史快照 · '+time(good.observed_at)));
            for(const item of (Array.isArray(good.windows)?good.windows:[])){
              history.append(element('p',(periods[item.period]||item.period)+' · '+formatAmount(item.remaining,item.unit)+' · 重置 '+time(item.reset_at)));
            }
            row.append(history);
          }
          row.append(element('p','最近采集 '+time(latest.observed_at),'hub-meta'));
          main.append(row);
        }
        info(accounts.length?'':'尚无额度快照');
        refreshTimer=setTimeout(()=>{if(valid(version))render();},15000);
      });
    }
    function time(value) {
      if (!value) return '未知';
      const date=new Date(value);
      return Number.isFinite(date.getTime()) ? date.toLocaleString() : '未知';
    }
    function chat(version) {
      const history=element('div',undefined,'hub-chat');
      history.setAttribute('role','log');
      const scope=element('div',undefined,'hub-row');
      scope.append(element('span',taskId?taskProject+' · '+taskId:scopeProject==='*'?'所有授权项目':'当前项目 · 全部任务','hub-meta'));
      if (taskId) scope.append(button('切回项目',()=>{taskId='';taskProject='';chatHistory=[];render();}));
      const form=element('form');
      const label=element('label','向总管提问');
      const question=element('textarea');
      question.maxLength=2000;
      question.required=true;
      question.value=chatDraft;
      question.oninput=()=>{chatDraft=question.value;};
      label.append(question);
      const ask=element('button','发送给总管');
      ask.type='submit';
      form.append(label,ask);
      function draw() {
        history.replaceChildren();
        for (const item of chatHistory) {
          history.append(card('你',item.question));
          const refs=(Array.isArray(item.evidence)?item.evidence:[]).slice(0,10).map(ref=>
            typeof ref==='string'?ref:[ref.project,ref.event_id].filter(Boolean).join('/'));
          history.append(card(item.mode==='model'?'总管':'项目查询',item.answer,refs.join(' · ')));
        }
        history.scrollTop=history.scrollHeight;
      }
      form.onsubmit=async e=>{
        e.preventDefault();
        const content=question.value.trim();
        if (!content || ask.disabled) return;
        ask.disabled=true;info('正在查询项目证据…');
        try {
          const result=await reader.request('chat',{question:content,...(taskId?{task_id:taskId}:{})},taskId?taskProject:scopeProject);
          if (!valid(version)) return;
          chatHistory.push({question:content,answer:printable(result.answer),evidence:result.evidence,mode:result.mode});
          chatHistory=chatHistory.slice(-15);
          if (question.value.trim()===content){question.value='';chatDraft='';}
          draw();info('');
        } catch(error){if(valid(version))info(error.message);}
        finally {if(valid(version))ask.disabled=false;}
      };
      main.append(scope,history,form);
      draw();
    }
    function library(version) {
      const localApi=native(), hasLocal=!!localApi?.cockpit_library_search;
      if (!librarySource) librarySource=hasLocal?'local':'central';
      const isLocal=librarySource==='local' && hasLocal;
      const sourceBar=element('div',undefined,'hub-row');
      sourceLabel=element('span',isLocal?'此桌面电脑':'中央 mylib','hub-meta');
      sourceBar.append(sourceLabel);
      if (hasLocal) sourceBar.append(button(isLocal?'查看中央库':'查看本机库',()=>{
        librarySource=isLocal?'central':'local';query='';render();
      }));
      if (isLocal) sourceBar.append(button('目录设置',()=>directory(localApi)));
      if(live)sourceBar.append(button('进化记录',()=>evolution()));
      const form=element('form'), search=field('检索技能',query), submit=element('button','查询技能');
      submit.type='submit';search.input.maxLength=256;
      form.append(search.el,submit);form.onsubmit=e=>{e.preventDefault();query=search.input.value.trim();render();};
      main.append(sourceBar,form);
      if (!isLocal&&!live){main.append(button('连接中央服务',settings));return;}
      info('正在读取技能目录…');
      const project=scopeProject==='*'?reader?.projects[0]:scopeProject;
      load(version,()=>isLocal?localApi.cockpit_library_search(query):reader.request('search_skills',{query,limit:30},project),result=>{
        if (isLocal) sourceLabel.textContent=[result.machine||'此桌面电脑',result.root].filter(Boolean).join(' · ');
        for(const item of (Array.isArray(result.skills)?result.skills:[])){
          const itemRevision=item.revision||result.revision;
          const row=card(item.title||item.name||item.id,item.description,item.path||itemRevision);
          row.append(button('阅读技能',()=>skill(item.id,itemRevision,isLocal,project)));
          main.append(row);
        }
        info(result.skills?.length?'':'没有匹配的技能');
      },true);
    }
    function skill(id,rev,isLocal,project) {
      clearContent();detail=true;
      const version=revision;
      main.append(button('返回 mylib',render));
      load(version,()=>isLocal?native().cockpit_library_read(id,rev):reader.request('read_skill',{skill_id:id,revision:rev},project),result=>{
        main.append(card(result.name||id,'',result.revision||rev),element('pre',printable(result.text),'hub-skill'));
        info('');
      });
    }
    function directory(api) {
      clearContent(); detail=true;
      const version=revision;
      main.append(button('返回 mylib',render));
      load(version,()=>api.get_assistant_preferences(),result=>{
        const input=field('此桌面电脑的 mylib 目录',result.preferences.library_root);
        const reset=button('恢复默认路径',()=>{input.input.value='D:/aicoding/mylib';});
        const save=button('保存本机目录',async()=>{
          save.disabled=true;
          try {
            const current=await api.get_assistant_preferences();
            if (!valid(version))return;
            await api.save_assistant_preferences({...current.preferences,library_root:input.input.value.trim()});
            root.dispatchEvent(new Event('mylib-preferences-changed'));
            if(valid(version)){render();info('本机目录已保存');}
          } catch { if(valid(version))info('目录无法读取或设置已变更，请检查本机文件夹'); }
          finally{if(valid(version))save.disabled=false;}
        });
        main.append(input.el,reset,save);
      });
    }
    function render() {
      if (!dialog?.open)return;
      clearContent();
      refreshTitle();
      nav.querySelectorAll('button').forEach(b=>b.setAttribute('aria-current',String(b.dataset.tab===tab)));
      if (tab==='library'){library(revision);return;}
      if (!live){settings();return;}
      if(tab==='chat')chat(revision);
      else if(tab==='tasks')tasks(revision);
      else quota(revision);
    }
    root.addEventListener('pagehide',()=>disconnect('页面连接已关闭'));
    return {open:()=>open(shortcut.hidden?menuEntry:shortcut), back, close, disconnect};
  }
  shared.mountCockpit = mountCockpit;
  shared.createCockpitReader = createCockpitReader;
  shared.cockpitEndpoint = cockpitEndpoint;
})(typeof window === 'undefined' ? globalThis : window);
