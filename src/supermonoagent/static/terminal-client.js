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
  const commonAgentProfile=validateAgentProfile({id:'common',label:'自动',theme:{background:'#0c1017',foreground:'#e6edf3'},tokens:[
    {pattern:'"[^"\\n]+"|\'[^\'\\n]+\'',flags:'gu',color:'#c3e88d'},
    {pattern:'(?:错误|失败|异常|警告)|\\b(?:Error|Exception|ERROR|FAIL|FAILED|Warning)\\b|\\*\\*[^*\\n]+\\*\\*',flags:'gu',color:'#f07178'},
    {pattern:'[\\p{L}\\p{N}_./\\\\:-]+\\.(?:py|js|jsx|ts|tsx|css|html|json|md|exe|ps1|sh|yaml|yml|toml|java|go|rs|cpp|h)(?::\\d+)?',flags:'gu',color:'#ffcb6b'},
    {pattern:'`[^`\\n]+`|\\b[A-Za-z_][\\w]*\\(\\)',flags:'gu',color:'#89ddff'},
    {pattern:'\\b\\d+(?:\\.\\d+)?(?:\\s?(?:KiB|MiB|ms|字节))?',flags:'gu',color:'#c792ea'}
  ]});
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
  function highlightSpans(line,cols,profile=commonAgentProfile){
    profile=validateAgentProfile(profile);
    cols=Math.min(500,Math.max(0,Math.floor(cols)||0));
    let text='';const columns=[],plain=[];
    for(let x=0;x<cols;x++){
      const cell=line.getCell(x);if(!cell||!cell.getWidth())continue;
      const chars=cell.getChars()||' ',width=cell.getWidth();
      if(text.length+chars.length>4096)return [];
      for(let i=0;i<chars.length;i++)columns[text.length+i]=x;
      text+=chars;columns[text.length]=x+width;
      const eligible=cell.isFgDefault()&&cell.isBgDefault()&&!cell.isInverse()&&!cell.isInvisible()&&!cell.isDim();
      for(let i=0;i<width;i++)plain[x+i]=eligible;
    }
    const spans=[];
    const occupied=[];
    for(const {expression,color} of compiledProfiles.get(profile))for(const match of text.matchAll(expression)){
      const start=columns[match.index],end=columns[match.index+match[0].length];
      // Faint text is application styling too (for example CLI suggestions).
      // Split around it and explicit colors instead of replacing either.
      let run=start;
      while(run<end){while(run<end&&(!plain[run]||occupied[run]))run++;let stop=run;while(stop<end&&plain[stop]&&!occupied[stop])stop++;
        if(stop>run){spans.push({x:run,width:stop-run,foregroundColor:color});for(let x=run;x<stop;x++)occupied[x]=true;}run=stop;}
    }
    return spans;
  }
  function createHighlighter(term,{enabled=true,profile=commonAgentProfile,schedule=root.requestAnimationFrame||((fn)=>setTimeout(fn,0)),cancel=root.cancelAnimationFrame||clearTimeout}={}){
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
      for(let row=active.viewportY;row<Math.min(active.length,active.viewportY+Math.min(200,term.rows));row++){
        const line=active.getLine(row);if(!line)continue;
        const spans=highlightSpans(line,term.cols,profile),signature=JSON.stringify(spans),old=previous.get(row);previous.delete(row);
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
    return {setEnabled(value){enabled=!!value;request();},setProfile(value){if(dead)return;profile=validateAgentProfile(value);clear();buffer=null;term.refresh(0,term.rows-1);request();},dispose(){if(dead)return;dead=true;if(frame!==null)cancel(frame);for(const listener of listeners)listener.dispose();clear();}};
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
    pane.highlighter?.dispose();pane.term.dispose();pane.el.remove();return true;
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
  const api = {agentProfiles,commonAgentProfile,validateAgentProfile,loadAgentProfile,agentModeFromSession,highlightSpans,createHighlighter,pack, unpack, targetFrame, sessionPayload, terminalInput, cursorKey, snapshotBytes, terminalSize, ConnectionMeter, formatRate, redrawSizes, disposePane, socketURL, secureEndpoint,
    retryDelay: attempt => Math.min(15000, 500 * 2 ** Math.min(10, attempt)),
    rendererName: name => ['auto', 'dom', 'webgl'].includes(name) ? name : 'auto'};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.TerminalClient = api;
})(typeof window === 'undefined' ? globalThis : window);
