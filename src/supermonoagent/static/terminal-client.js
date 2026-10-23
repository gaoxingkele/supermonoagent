/* Byte-only terminal framing. Shared by the workspace and its regression tests. */
(function (root) {
  'use strict';
  const encoder = new TextEncoder();
  // Decorate parsed cells instead of rewriting the PTY stream. In particular,
  // split UTF-8, SGR/OSC sequences and cursor-addressed TUI output stay intact.
  const PLAIN_TOKEN=/`[^`\n]+`|\*\*[^*\n]+\*\*|"[^"\n]*"|'[^'\n]*'|(?:错误|失败|异常|警告)|\b(?:Error|Exception|ERROR|FAIL|FAILED|Warning)\b|[\p{L}\p{N}_./\\:-]+\.(?:py|js|jsx|ts|tsx|css|html|json|md|exe|ps1|sh|yaml|yml|toml|java|go|rs|cpp|h)(?::\d+)?|\b[A-Za-z_][\w]*\(\)|\b\d+(?:\.\d+)?(?:\s?(?:KiB|MiB|ms|字节))?/gu;
  function highlightSpans(line,cols){
    let text='';const columns=[],plain=[];
    for(let x=0;x<cols;x++){
      const cell=line.getCell(x);if(!cell||!cell.getWidth())continue;
      const chars=cell.getChars()||' ',width=cell.getWidth();
      for(let i=0;i<chars.length;i++)columns[text.length+i]=x;
      text+=chars;columns[text.length]=x+width;
      const eligible=cell.isFgDefault()&&cell.isBgDefault()&&!cell.isInverse()&&!cell.isInvisible();
      for(let i=0;i<width;i++)plain[x+i]=eligible;
    }
    const spans=[];
    for(const match of text.matchAll(PLAIN_TOKEN)){
      const token=match[0],start=columns[match.index],end=columns[match.index+token.length];
      let color='#c792ea';
      if(token[0]==='`'||token.endsWith('()'))color='#89ddff';
      else if(token[0]==='"'||token[0]==="'")color='#c3e88d';
      else if(token[0]==='*'||/^(?:错误|失败|异常|警告|Error|Exception|ERROR|FAIL|FAILED|Warning)$/.test(token))color='#f07178';
      else if(/\.(?:[a-z]+)/i.test(token))color='#ffcb6b';
      // Split around explicitly styled cells; never replace application colors.
      let run=start;
      while(run<end){while(run<end&&!plain[run])run++;let stop=run;while(stop<end&&plain[stop])stop++;
        if(stop>run)spans.push({x:run,width:stop-run,foregroundColor:color});run=stop;}
    }
    return spans;
  }
  function createHighlighter(term,{enabled=true,schedule=requestAnimationFrame,cancel=cancelAnimationFrame}={}){
    let entries=[],buffer=null,frame=null,dead=false;
    const remove=entry=>{for(const decoration of entry.decorations)decoration.dispose();entry.marker.dispose();};
    function clear(){for(const entry of entries)remove(entry);entries=[];}
    function refresh(){
      frame=null;if(dead)return;
      const active=term.buffer.active;if(buffer!==active){clear();buffer=active;}
      if(!enabled){clear();term.refresh(0,term.rows-1);return;}
      const previous=new Map(entries.filter(e=>!e.marker.isDisposed).map(e=>[e.marker.line,e]));
      entries=[];let changed=false;
      for(let row=active.viewportY;row<Math.min(active.length,active.viewportY+term.rows);row++){
        const line=active.getLine(row);if(!line)continue;
        const spans=highlightSpans(line,term.cols),signature=JSON.stringify(spans),old=previous.get(row);previous.delete(row);
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
    return {setEnabled(value){enabled=!!value;request();},dispose(){dead=true;if(frame!==null)cancel(frame);for(const listener of listeners)listener.dispose();clear();}};
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
    // Restrict this to complete DA/DSR answers; arrows, paste, Esc and Ctrl remain intact.
    return text.replace(/\x1b\[(?:[?>]?[0-9;]*c|[0-9]+;[0-9]+R|[0-9]+n)/g,'');
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
  const api = {highlightSpans, createHighlighter, pack, unpack, targetFrame, sessionPayload, terminalInput, snapshotBytes, terminalSize, ConnectionMeter, formatRate, redrawSizes, disposePane, socketURL, secureEndpoint,
    retryDelay: attempt => Math.min(15000, 500 * 2 ** Math.min(10, attempt)),
    rendererName: name => ['auto', 'dom', 'webgl'].includes(name) ? name : 'auto'};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.TerminalClient = api;
})(typeof window === 'undefined' ? globalThis : window);
