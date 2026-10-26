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
  function target(){const p=W.activePane;if(!p||!p.ready||!p.host.connected)throw Error('请先选择已连接的终端窗口。');return Object.freeze({pane:p,key:p.sid,host:p.host,generation:p.host.generation,ws:p.host.ws,session:p.raw,token:p.host.config?.token,room:p.host.config?.code});}
  function valid(t){return !!t&&W.panes.get(t.key||t.pane?.sid)===t.pane&&t.pane.raw===t.session&&t.pane.ready&&t.host.ws===t.ws&&t.host.generation===t.generation&&t.host.connected&&t.host.config?.token===t.token&&t.host.config?.code===t.room;}
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
  async function transfer(t,sources,options={}){
    if(!t.host.capabilities.file_transfer)throw Error('目标电脑后台需升级后才能接收文件。');
    const receipts=[];
    const check=()=>{checkCancel();if(options.signal?.aborted)throw Error('文件传输已取消。');if(!valid(t))throw Error('目标窗口或连接已变化，文件传输已停止。');};
    const progress=value=>{try{options.onProgress?.(value);}catch{}};
    try{for(const [index,source] of sources.entries()){let id;
      try{
        check();status('准备传输 '+source.name+'…');progress({phase:'preparing',name:source.name,index,total:sources.length,loaded:0,size:source.size});
        const sha256=source.prepare?await source.prepare():source.sha256;check();
        const begin=await rpc(t,{action:'begin',name:source.name,size:source.size,sha256});id=begin.id;
        let offset=0;
        while(offset<source.size){check();const chunk=await source.read(offset);check();if(!chunk.data||chunk.next<=offset||chunk.next>source.size)throw Error('源文件分块无效。');const response=await rpc(t,{action:'chunk',id,offset,data:chunk.data});if(response.received!==chunk.next)throw Error('接收进度不一致。');offset=chunk.next;status(source.name+' · '+Math.floor(offset/source.size*100)+'%');progress({phase:'uploading',name:source.name,index,total:sources.length,loaded:offset,size:source.size});}
        check();const result=await rpc(t,{action:'finish',id});id=null;
        if(typeof result.path!=='string'||!result.path||/[\u0000-\u001f\u007f]/.test(result.path))throw Error('目标电脑没有返回有效保存路径。');
        const receipt=Object.freeze({name:source.name,path:result.path,size:source.size,sha256});receipts.push(receipt);status('已保存到目标电脑：'+result.path);progress({phase:'saved',name:source.name,index,total:sources.length,loaded:source.size,size:source.size,receipt});
      }finally{if(id&&valid(t))try{await rpc(t,{action:'cancel',id});}catch{}try{await source.release?.();}catch{}}
    }}catch(error){error.receipts=[...receipts];throw error;}
    return receipts;
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
    const plain=event.clipboardData?.getData('text/plain')||'';
    // The current paste event is authoritative for text. A stale file-format
    // beacon or an unavailable native bridge must not consume or delay it.
    if(plain&&!files.length)return;
    if(!files.length&&!native)return;
    const pane=W.activePane;
    let pinned;try{pinned=target();}catch(error){event.preventDefault();status(error.message);return;}
    event.preventDefault();event.stopImmediatePropagation();
    const fallback=()=>{if(plain&&valid(pinned)&&W.activePane===pane)pane.term.paste(plain);};
    const api=window.pywebview?.api;
    if(api?.clipboard_has_files){
      api.clipboard_has_files().then(has=>{if(has)run(nativeSources,fallback,pinned);else if(files.length)run(()=>browserSources(files),fallback,pinned);else fallback();}).catch(()=>{if(files.length)run(()=>browserSources(files),fallback,pinned);else fallback();});
    }else if(files.length)run(()=>browserSources(files),fallback,pinned);
    else if(busy){if(plain)fallback();else status('已有文件正在传输，请等待或取消。');}
    else run(nativeSources,fallback,pinned);
  },true);
  function nativeMenu(){const menu=document.getElementById('app-menu');if(menu&&window.pywebview?.api?.clipboard_files&&!document.getElementById('paste-native-files')){const item=document.createElement('button');item.id='paste-native-files';item.type='button';item.setAttribute('role','menuitem');item.textContent='粘贴文件到当前窗口';item.onclick=()=>run(nativeSources);menu.append(item);}}
  nativeMenu();addEventListener('pywebviewready',nativeMenu,{once:true});
  async function uploadSources(getSources,options={}){
    if(busy)throw Error('已有文件正在传输，请等待或取消。');
    const pinned=options.target||target();if(!valid(pinned))throw Error('目标窗口或连接已变化。');
    if(options.signal?.aborted)throw Error('文件传输已取消。');
    let sources=[];busy=true;cancelled=false;cancel.disabled=false;cancel.textContent='取消传输';
    try{sources=await getSources();if(!sources.length)throw Error('没有可上传的文件或图片。');return await transferExchange(pinned,sources,options);}
    finally{for(const source of sources)try{await source.release?.();}catch{}busy=false;cancel.disabled=false;cancel.textContent='关闭';cancel.onclick=()=>{if(busy){cancelled=true;cancel.disabled=true;}else box.hidden=true;};}
  }
  async function transferExchange(pinned,sources,options){
    const F=window.SuperMonoFileWorkspaces;
    if(!F)throw Error('文件交换助手尚未安装，请更新完整客户端并在接收电脑启用文件助手。');
    const target=F.captureTarget();
    if(!target||target.pane!==pinned.pane||target.ws!==pinned.ws||target.generation!==pinned.generation)throw Error('目标窗口已变化，请重新打开编辑框。');
    const client=F.getClient(target),receipts=[];
    const check=()=>{checkCancel();if(options.signal?.aborted)throw Error('文件传输已取消。');if(!valid(pinned)||!F.isCurrent(target))throw Error('目标窗口或连接已变化。');};
    const progress=value=>{try{options.onProgress?.(value);}catch{}};
    try{for(const [index,source] of sources.entries()){
      let upload_id,file_id;
      try{
        check();const workspace=await F.getWorkspace(target);check();
        progress({phase:'preparing',name:source.name,index,total:sources.length,loaded:0,size:source.size});
        const sha256=source.prepare?await source.prepare():source.sha256;check();
        const begin=await F.request(target,'upload.begin',{original_name:source.name,size:source.size,sha256,window_id:workspace.window_id,daemon_epoch:workspace.daemon_epoch,workspace_revision:workspace.revision,timezone_offset_minutes:-new Date().getTimezoneOffset()});upload_id=begin.upload_id;
        let offset=0;
        while(offset<source.size){check();const chunk=await source.read(offset);check();if(!chunk.data||chunk.next<=offset||chunk.next>source.size)throw Error('源文件分块无效。');const response=await F.request(target,'upload.chunk',{upload_id,offset,data:chunk.data});if(response.received!==chunk.next)throw Error('接收进度不一致。');offset=chunk.next;progress({phase:'uploading',name:source.name,index,total:sources.length,loaded:offset,size:source.size});}
        check();const queued=await F.request(target,'upload.finish',{upload_id});upload_id=null;file_id=queued.file_id;
        progress({phase:'receiving',name:source.name,index,total:sources.length,loaded:source.size,size:source.size,file_id});
        const deadline=performance.now()+180000;let result;
        // A committed relay job is durable. Wait for its receipt even if the
        // caller cancels, then stop before the next file; never call it saved early.
        while(performance.now()<deadline){
          if(!valid(pinned)||!F.isCurrent(target))throw Object.assign(Error('文件已存入中继，但目标连接已变化；请核对接收状态。'),{pendingFileId:queued.file_id});
          result=await F.request(target,'file.status',{file_id:queued.file_id});
          if(result.state==='saved')break;
          if(['failed','cancelled'].includes(result.state))throw Error('目标电脑未能保存文件，请检查文件助手。');
          await new Promise(resolve=>setTimeout(resolve,250));
        }
        if(result?.state!=='saved')throw Object.assign(Error('文件已存入中继，尚未收到目标保存确认。'),{pendingFileId:queued.file_id});
        const saved=result.receipt;if(!saved||typeof saved.path!=='string'||!saved.path||/[\u0000-\u001f\u007f]/.test(saved.path)||saved.name!==begin.name||saved.size!==source.size||saved.sha256!==sha256)throw Error('目标保存回执无效。');
        const receipt=Object.freeze({name:source.name,savedName:saved.name,path:saved.path,size:saved.size,sha256});receipts.push(receipt);file_id=null;progress({phase:'saved',name:source.name,index,total:sources.length,loaded:source.size,size:source.size,receipt});
      }catch(error){if(file_id&&!error.pendingFileId)error.pendingFileId=file_id;throw error;}
      finally{if(upload_id)try{await client.request('upload.cancel',target.session,{upload_id});}catch{}try{await source.release?.();}catch{}}
    }}catch(error){error.receipts=[...receipts];throw error;}
    return receipts;
  }
  window.SuperMonoFilePaste={paste:()=>run(nativeSources),captureTarget:target,isCurrent:valid,
    upload:(files,options={})=>uploadSources(()=>browserSources([...files]),options),
    uploadClipboard:(options={})=>uploadSources(nativeSources,options),get busy(){return busy;}};
})();
