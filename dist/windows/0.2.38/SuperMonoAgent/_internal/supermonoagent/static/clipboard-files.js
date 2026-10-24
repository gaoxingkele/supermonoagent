/* File-only sidecar: normal text paste stays with xterm. */
(()=>{
  'use strict';
  if(window.SuperMonoFilePaste||!window.smaWorkspace)return;
  const W=window.smaWorkspace,C=window.TerminalClient,encode=new TextEncoder();
  let busy=false,cancelled=false,sequence=0;
  const box=document.createElement('aside');box.id='file-transfer-progress';box.hidden=true;
  box.style.cssText='position:fixed;right:12px;bottom:38px;z-index:1300;max-width:calc(100vw - 24px);width:360px;background:#222;color:#eee;border:1px solid #666;border-radius:7px;padding:12px;overflow-wrap:anywhere';
  const text=document.createElement('p'),cancel=document.createElement('button');cancel.textContent='取消传输';cancel.onclick=()=>{cancelled=true;cancel.disabled=true;};box.append(text,cancel);document.body.append(box);
  function status(message){box.hidden=false;text.textContent=message;}
  function target(){const p=W.activePane;if(!p||!p.host.connected)throw Error('请先选择已连接的终端窗口。');return {pane:p,host:p.host,generation:p.host.generation,ws:p.host.ws,session:p.raw};}
  function valid(t){return t.host.ws===t.ws&&t.host.generation===t.generation&&t.host.connected;}
  function rpc(t,body){return new Promise((resolve,reject)=>{
    if(!valid(t)){reject(Error('连接已改变，传输已停止。'));return;}
    const request='file-'+(++sequence)+'-'+Date.now().toString(36);let timer;
    const done=(error,result)=>{clearTimeout(timer);t.ws.removeEventListener('message',receive);t.ws.removeEventListener('close',closed);error?reject(error):resolve(result);};
    const closed=()=>done(Error('连接已断开，未完成文件不会提交。'));
    const receive=event=>{if(!(event.data instanceof ArrayBuffer))return;try{const f=C.unpack(new Uint8Array(event.data));if(f.op!==21)return;const r=JSON.parse(new TextDecoder().decode(f.payload));if(r.request!==request)return;if(!valid(t))done(Error('连接已改变。'));else if(!r.ok)done(Error(r.error||'文件传输失败。'));else done(null,r);}catch{}};
    t.ws.addEventListener('message',receive);t.ws.addEventListener('close',closed);timer=setTimeout(()=>done(Error('文件传输响应超时。')),15000);
    try{const frame=C.pack(21,encode.encode(JSON.stringify({request,session:t.session,...body})));t.ws.send(frame);W.metrics.transmittedBytes+=t.host.meter.sent(frame);}catch(error){done(error);}
  });}
  function checkCancel(){if(cancelled)throw Error('文件传输已取消。');}
  async function transfer(t,sources){
    if(!t.host.capabilities.file_transfer)throw Error('目标电脑后台需升级后才能接收文件。');
    for(const source of sources){let id;
      try{
        checkCancel();status('准备传输 '+source.name+'…');
        const sha256=source.prepare?await source.prepare():source.sha256;checkCancel();
        const begin=await rpc(t,{action:'begin',name:source.name,size:source.size,sha256});id=begin.id;
        let offset=0;
        while(offset<source.size){checkCancel();const chunk=await source.read(offset);checkCancel();if(!chunk.data||chunk.next<=offset||chunk.next>source.size)throw Error('源文件分块无效。');const response=await rpc(t,{action:'chunk',id,offset,data:chunk.data});if(response.received!==chunk.next)throw Error('接收进度不一致。');offset=chunk.next;status(source.name+' · '+Math.floor(offset/source.size*100)+'%');}
        checkCancel();const result=await rpc(t,{action:'finish',id});id=null;status('已保存到目标电脑：'+result.path);
      }finally{if(id&&valid(t))try{await rpc(t,{action:'cancel',id});}catch{}try{await source.release?.();}catch{}}
    }
  }
  async function run(getSources,fallback,pinned){
    if(busy){status('已有文件正在传输，请等待或取消。');return;}
    let sources=[];
    try{const t=pinned||target();busy=true;cancelled=false;cancel.disabled=false;cancel.textContent='取消传输';status('正在读取粘贴文件…');sources=await getSources();if(!sources.length){if(fallback)fallback();box.hidden=true;return;}await transfer(t,sources);}
    catch(error){status(String(error.message||error));}
    finally{for(const s of sources)try{await s.release?.();}catch{}busy=false;cancel.disabled=false;cancel.textContent='关闭';cancel.onclick=()=>{if(busy){cancelled=true;cancel.disabled=true;}else box.hidden=true;};}
  }
  async function nativeSources(){const api=window.pywebview?.api;if(!api?.clipboard_files)throw Error('当前外壳尚未提供文件剪贴板入口。');const snapshot=await api.clipboard_files();return snapshot.items.map(item=>({...item,read:offset=>api.clipboard_chunk(item.id,offset),release:()=>api.clipboard_release(item.id)}));}
  async function browserSources(files){if(files.length>32)throw Error('一次最多粘贴 32 个文件。');return files.map(f=>{if(f.size>64*1024*1024)throw Error('浏览器粘贴单文件限 64 MiB；较大文件请使用 Windows 原生文件粘贴。');let bytes;return {name:f.name,size:f.size,prepare:async()=>{bytes=new Uint8Array(await f.arrayBuffer());const digest=await crypto.subtle.digest('SHA-256',bytes);return [...new Uint8Array(digest)].map(v=>v.toString(16).padStart(2,'0')).join('');},read:async offset=>{const part=bytes.subarray(offset,offset+96*1024);let value='';for(const v of part)value+=String.fromCharCode(v);return {data:btoa(value),next:offset+part.length};},release:()=>{bytes=null;}};});}
  document.addEventListener('paste',event=>{
    if(!event.target.closest?.('.terminal-screen'))return;
    const files=[...(event.clipboardData?.files||[])],native=!!window.supermonoClipboardHasFiles;
    if(!files.length&&!native)return;
    const pane=W.activePane,plain=event.clipboardData?.getData('text/plain')||'';
    let pinned;try{pinned=target();}catch(error){event.preventDefault();status(error.message);return;}
    event.preventDefault();event.stopImmediatePropagation();
    const fallback=()=>{if(plain&&W.panes.get(pane?.sid)===pane)pane.term.paste(plain);};
    const api=window.pywebview?.api;
    if(api?.clipboard_has_files){
      api.clipboard_has_files().then(has=>{if(has)run(nativeSources,fallback,pinned);else if(files.length)run(()=>browserSources(files),fallback,pinned);else fallback();}).catch(()=>{if(files.length)run(()=>browserSources(files),fallback,pinned);else fallback();});
    }else if(files.length)run(()=>browserSources(files),fallback,pinned);
    else if(busy){if(plain)fallback();else status('已有文件正在传输，请等待或取消。');}
    else run(nativeSources,fallback,pinned);
  },true);
  function nativeMenu(){const menu=document.getElementById('app-menu');if(menu&&window.pywebview?.api?.clipboard_files&&!document.getElementById('paste-native-files')){const item=document.createElement('button');item.id='paste-native-files';item.type='button';item.setAttribute('role','menuitem');item.textContent='粘贴文件到当前窗口';item.onclick=()=>run(nativeSources);menu.append(item);}}
  nativeMenu();addEventListener('pywebviewready',nativeMenu,{once:true});
  window.SuperMonoFilePaste={paste:()=>run(nativeSources),get busy(){return busy;}};
})();
