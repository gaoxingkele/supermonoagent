/* Byte-only terminal framing. Shared by the workspace and its regression tests. */
(function (root) {
  'use strict';
  const encoder = new TextEncoder();
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
  function snapshotBytes(bytes, session) {
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
    reset(now){this.tx=0;this.rx=0;this.previousTx=0;this.previousRx=0;this.sampleAt=now;this.rtt=null;this.pongAt=null;}
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
      const bars=this.rtt<=100?4:this.rtt<=250?3:this.rtt<=800?2:1;
      return {bars,label:['','较慢','一般','良好','稳定'][bars]};
    }
  }
  function redrawSizes(cols, rows) {
    const final=terminalSize(cols,rows);
    let alternate;
    if(final.rows<200)alternate={cols:final.cols,rows:final.rows+1};
    else alternate={cols:final.cols,rows:final.rows-1};
    if(final.rows===1&&final.cols<500)alternate={cols:final.cols+1,rows:final.rows};
    return [alternate,final];
  }
  function disposePane(panes, session) {
    const pane=panes.get(session);if(!pane)return false;
    // This only releases the local view. It has no socket or process-close operation.
    panes.delete(session);pane.ready=false;if(pane.redrawTimer)clearTimeout(pane.redrawTimer);pane.observer.disconnect();
    const addon=pane.webgl;pane.webgl=null;if(addon)addon.dispose();
    pane.term.dispose();pane.el.remove();return true;
  }
  function socketURL(relay, origin) {
    const url = new URL(relay || origin);
    if (!['http:', 'https:', 'ws:', 'wss:'].includes(url.protocol)) throw new Error('Invalid relay URL');
    if (url.username || url.password) throw new Error('URL credentials are not supported');
    url.protocol = ['https:', 'wss:'].includes(url.protocol) ? 'wss:' : 'ws:';
    if (url.pathname === '/' || !url.pathname) url.pathname = '/ws';
    url.search = ''; url.hash = '';
    return url.toString();
  }
  const api = {pack, unpack, targetFrame, sessionPayload, terminalInput, snapshotBytes, terminalSize, ConnectionMeter, formatRate, redrawSizes, disposePane, socketURL,
    retryDelay: attempt => Math.min(15000, 500 * 2 ** Math.min(10, attempt)),
    rendererName: name => ['auto', 'dom', 'webgl'].includes(name) ? name : 'auto'};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.TerminalClient = api;
})(typeof window === 'undefined' ? globalThis : window);
