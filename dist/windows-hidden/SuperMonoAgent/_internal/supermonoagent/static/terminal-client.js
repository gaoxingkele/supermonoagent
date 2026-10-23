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
  function disposePane(panes, session) {
    const pane=panes.get(session);if(!pane)return false;
    // This only releases the local view. It has no socket or process-close operation.
    panes.delete(session);pane.ready=false;pane.observer.disconnect();
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
  const api = {pack, unpack, targetFrame, sessionPayload, terminalInput, snapshotBytes, disposePane, socketURL,
    retryDelay: attempt => Math.min(15000, 500 * 2 ** Math.min(10, attempt)),
    rendererName: name => ['auto', 'dom', 'webgl'].includes(name) ? name : 'auto'};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.TerminalClient = api;
})(typeof window === 'undefined' ? globalThis : window);
