/* File-only sidecar: normal text paste stays with xterm. */
(()=>{
  'use strict';
  if(window.SuperMonoFilePaste||!window.smaWorkspace)return;
  const W=window.smaWorkspace;
  let busy=false;const jobs=[];let activeJob=null;const MAX_JOBS=32,MAX_QUEUED_BYTES=512*1024*1024;
  const box=document.createElement('aside');box.id='file-transfer-progress';box.hidden=true;
  box.style.cssText='position:fixed;right:12px;bottom:38px;z-index:1300;max-width:calc(100vw - 24px);width:360px;background:#222;color:#eee;border:1px solid #666;border-radius:7px;padding:12px;overflow-wrap:anywhere';
  const text=document.createElement('p'),cancel=document.createElement('button');cancel.textContent='取消传输';cancel.onclick=cancelAll;const list=document.createElement('ul');list.id='file-transfer-queue';list.style.cssText='padding:0;list-style:none;max-height:160px;overflow:auto';box.append(text,list,cancel);document.body.append(box);
  function status(message){box.hidden=false;text.textContent=message;}
  const windowOf=(host,session)=>host.catalog?.find(w=>(w.sessions||[]).some(s=>s.id===session))?.id;
  function target(){const p=W.activePane;if(!p||!p.ready||!p.host.connected)throw Error('请先选择已连接的终端窗口。');return Object.freeze({pane:p,key:p.sid,host:p.host,generation:p.host.generation,paneGeneration:p.generation,ws:p.host.ws,session:p.raw,window_id:windowOf(p.host,p.raw),token:p.host.config?.token,room:p.host.config?.code,relay:p.host.config?.relay,authMode:p.host.config?.authMode});}
  function valid(t){return !!t&&W.panes.get(t.key||t.pane?.sid)===t.pane&&t.pane.host===t.host&&t.pane.raw===t.session&&windowOf(t.host,t.session)===t.window_id&&t.pane.generation===t.paneGeneration&&t.pane.ready&&t.host.ws===t.ws&&t.host.generation===t.generation&&t.host.connected&&t.host.config?.token===t.token&&t.host.config?.code===t.room&&t.host.config?.relay===t.relay&&t.host.config?.authMode===t.authMode;}
  function exchangeTarget(pinned){const F=window.SuperMonoFileWorkspaces;if(!F)return null;const t=F.captureTarget();if(!t||t.pane!==pinned.pane||t.ws!==pinned.ws||t.generation!==pinned.generation||t.session!==pinned.session)throw Error('目标窗口已变化，请重新选择附件。');return t;}
  function renderQueue(){list.replaceChildren();for(const job of jobs){const row=document.createElement('li');row.style.cssText='margin:5px 0;overflow-wrap:anywhere';const label=document.createElement('span');label.textContent=job.sources?.map(s=>s.name).join('、')||'剪贴板附件';row.append(label,document.createTextNode(' · '+({queued:'排队',reading:'读取附件',sending:'传输中',failed:'失败',pending:'中继待确认，请勿重复上传',unknown:'提交结果待确认，勿重复'}[job.state]||job.state)+(job.pendingFileId?' '+job.pendingFileId:job.pendingUploadId?' 上传 '+job.pendingUploadId:'')));if(job!==activeJob){if(job.state==='failed'){const retry=document.createElement('button');retry.textContent='重试';retry.onclick=()=>{if(!valid(job.pinned)){status('原窗口连接已变化，请移除后在正确窗口重新粘贴。');return;}if(!job.exchange)try{job.exchange=exchangeTarget(job.pinned);}catch(error){status(error.message);return;}job.state='queued';renderQueue();drain();};row.append(retry);}const remove=document.createElement('button');remove.textContent='移除';remove.onclick=()=>{job.controller.abort();forget(job);};row.append(remove);}list.append(row);}cancel.textContent=busy?'取消队列':'关闭';cancel.disabled=false;}
  async function forget(job){const index=jobs.indexOf(job);if(index>=0)jobs.splice(index,1);await job.ready;for(const source of job.sources||[])try{await source.release?.();}catch{}job.options.signal?.removeEventListener('abort',job.abort);renderQueue();}
  function cancelAll(){if(!busy){box.hidden=true;return;}for(const job of [...jobs]){job.controller.abort();if(job!==activeJob){job.reject(Error('文件传输已取消。'));forget(job);}}status('正在取消；已提交中继的文件会继续等待保存确认，不再发送下一项。');}
  function enqueue(getSources,options={},retain=false,fallback){
    if(jobs.length>=MAX_JOBS){status('附件队列已满（最多32次粘贴），请先传输或移除现有项目。');return Promise.reject(Error('附件队列已满。'));}
    let pinned,exchange;try{pinned=options.target||target();if(!valid(pinned))throw Error('目标窗口或连接已变化。');exchange=exchangeTarget(pinned);}catch(error){return Promise.reject(error);}
    let resolve,reject;const result=new Promise((yes,no)=>{resolve=yes;reject=no;});
    const job={pinned,exchange,options,retain,fallback,controller:new AbortController(),resolve,reject,state:'queued',sources:[]};
    job.abort=()=>{job.controller.abort();if(job!==activeJob){job.reject(Error('文件传输已取消。'));forget(job);}};
    // Snapshot now, before any await or FIFO wait: later clipboard contents
    // must never replace this event's attachments or its captured destination.
    try{job.ready=Promise.resolve(getSources()).then(async sources=>{
      const total=Array.isArray(sources)?sources.reduce((n,s)=>n+(Number.isSafeInteger(s.size)&&s.size>=0?s.size:Infinity),0):Infinity;
      const existing=jobs.reduce((n,j)=>n+j.sources.reduce((sum,s)=>sum+s.size,0),0);
      if(!Array.isArray(sources)||sources.length>32||total+existing>MAX_QUEUED_BYTES){for(const s of sources||[])try{await s.release?.();}catch{}job.sourceError=Error('附件队列总量超过512 MiB或单次超过32个文件，请分批粘贴。');return;}
      job.sources=sources;renderQueue();
    },e=>{job.sourceError=e;});}catch(error){job.sourceError=error;job.ready=Promise.resolve();}
    jobs.push(job);options.signal?.addEventListener('abort',job.abort,{once:true});if(options.signal?.aborted)job.controller.abort();
    renderQueue();drain();return result;
  }
  async function drain(){if(activeJob)return;const job=jobs.find(j=>j.state==='queued');if(!job){busy=false;renderQueue();return;}activeJob=job;busy=true;job.state='reading';status('正在读取粘贴文件…');renderQueue();
    try{await job.ready;if(job.controller.signal.aborted)throw Error('文件传输已取消。');if(job.sourceError)throw job.sourceError;if(!job.sources.length){if(!job.retain)throw Error('没有可上传的文件或图片。');job.fallback?.();job.resolve([]);await forget(job);}
      else{job.state='sending';renderQueue();const receipts=await transferExchange(job.pinned,job.sources.filter(s=>!s.saved),{...job.options,signal:job.controller.signal,exchangeTarget:job.exchange});job.resolve(receipts);status('已保存到目标项目 tmp：'+receipts.map(r=>r.path).join('、'));await forget(job);}}
    catch(error){if(error.code==='project_required')error.message='请先在底部选择当前项目目录，附件将保存到该项目的 tmp。';job.reject(error);status(String(error.message||error));if(job.retain&&(!job.controller.signal.aborted||error.unknownCommit||error.pendingFileId)&&job.sources.length){job.state=error.unknownCommit?'unknown':error.pendingFileId?'pending':'failed';job.pendingFileId=error.pendingFileId;job.pendingUploadId=error.pendingUploadId;renderQueue();}else await forget(job);}
    finally{activeJob=null;busy=false;drain();}
  }
  function run(getSources,fallback,pinned){return enqueue(getSources,{target:pinned},true,fallback).catch(error=>{status(String(error.message||error));});}
  async function nativeSources(){const api=window.pywebview?.api;if(!api?.clipboard_files)throw Error('当前外壳尚未提供文件剪贴板入口。');const snapshot=await api.clipboard_files();return snapshot.items.map(item=>({...item,read:offset=>api.clipboard_chunk(item.id,offset),release:()=>api.clipboard_release(item.id)}));}
  async function browserSources(files){if(files.length>32)throw Error('一次最多粘贴 32 个文件。');return files.map(f=>{if(f.size>64*1024*1024)throw Error('浏览器粘贴单文件限 64 MiB；较大文件请使用 Windows 原生文件粘贴。');let bytes;return {name:f.name,size:f.size,prepare:async()=>{bytes=new Uint8Array(await f.arrayBuffer());const digest=await crypto.subtle.digest('SHA-256',bytes);return [...new Uint8Array(digest)].map(v=>v.toString(16).padStart(2,'0')).join('');},read:async offset=>{const part=bytes.subarray(offset,offset+96*1024);let value='';for(const v of part)value+=String.fromCharCode(v);return {data:btoa(value),next:offset+part.length};},release:()=>{bytes=null;},dropPrepared:()=>{bytes=null;}};});}
  document.addEventListener('paste',event=>{
    if(!event.target.closest?.('.terminal-screen'))return;
    const files=[...(event.clipboardData?.files||[])],native=!!window.supermonoClipboardHasFiles;
    const plain=event.clipboardData?.getData('text/plain')||'';
    // The current paste event is authoritative for text. A stale file-format
    // beacon or an unavailable native bridge must not consume or delay it.
    if(plain&&!files.length)return;
    if(!files.length&&!native)return;
    const pane=W.activePane;
    if(pane?.term?.textarea&&event.target.closest('.terminal-screen')!==pane.term.textarea.closest('.terminal-screen')){event.preventDefault();event.stopImmediatePropagation();status('请选择该终端窗口后再粘贴附件。');return;}
    let pinned;try{pinned=target();}catch(error){event.preventDefault();status(error.message);return;}
    event.preventDefault();event.stopImmediatePropagation();
    const fallback=()=>{if(plain&&valid(pinned)&&W.activePane===pane)pane.term.paste(plain);};
    // Do not await clipboard_has_files: it can race with another Ctrl+V.
    // Native snapshots are immutable handles, browser Files belong to this event.
    if(files.length)run(()=>browserSources(files),fallback,pinned);
    else run(nativeSources,fallback,pinned);
  },true);
  function nativeMenu(){const menu=document.getElementById('app-menu');if(menu&&window.pywebview?.api?.clipboard_files&&!document.getElementById('paste-native-files')){const item=document.createElement('button');item.id='paste-native-files';item.type='button';item.setAttribute('role','menuitem');item.textContent='粘贴文件到当前窗口';item.onclick=()=>run(nativeSources);menu.append(item);}}
  nativeMenu();addEventListener('pywebviewready',nativeMenu,{once:true});
  function uploadSources(getSources,options={}){return enqueue(getSources,options);}
  async function transferExchange(pinned,sources,options){
    const F=window.SuperMonoFileWorkspaces;
    if(!F)throw Error('文件交换助手尚未安装，请更新完整客户端并在接收电脑启用文件助手。');
    const target=options.exchangeTarget;
    if(!target||target.pane!==pinned.pane||target.ws!==pinned.ws||target.generation!==pinned.generation)throw Error('目标窗口已变化，请重新打开编辑框。');
    const client=F.getClient(target),receipts=[];
    const check=()=>{if(options.signal?.aborted)throw Error('文件传输已取消。');if(!valid(pinned)||!F.isCurrent(target))throw Error('目标窗口或连接已变化。');};
    const progress=value=>{
      if(value.phase==='preparing')status('准备传输 '+value.name+'…');
      else if(value.phase==='uploading')status(value.name+' · '+(value.size?Math.floor(value.loaded/value.size*100):100)+'%');
      else if(value.phase==='receiving')status('已存入中继，正在等待目标项目 tmp 保存确认：'+value.name);
      else if(value.phase==='submitting')status('正在提交中继，请等待结果确认：'+value.name);
      try{options.onProgress?.(value);}catch{}
    };
    try{for(const [index,source] of sources.entries()){
      let upload_id,file_id,finishSent=false;
      try{
        check();const workspace=await F.getWorkspace(target);check();
        if(workspace.project_status==='selection_required')throw Object.assign(Error('请先在底部选择当前项目目录，附件将保存到该项目的 tmp。'),{code:'project_required'});
        progress({phase:'preparing',name:source.name,index,total:sources.length,loaded:0,size:source.size});
        const sha256=source.prepare?await source.prepare():source.sha256;check();
        const begin=await F.request(target,'upload.begin',{original_name:source.name,size:source.size,sha256,window_id:workspace.window_id,daemon_epoch:workspace.daemon_epoch,workspace_revision:workspace.revision,timezone_offset_minutes:-new Date().getTimezoneOffset()});upload_id=begin.upload_id;
        let offset=0;
        while(offset<source.size){check();const chunk=await source.read(offset);check();if(!chunk.data||chunk.next<=offset||chunk.next>source.size)throw Error('源文件分块无效。');const response=await F.request(target,'upload.chunk',{upload_id,offset,data:chunk.data});if(response.received!==chunk.next)throw Error('接收进度不一致。');offset=chunk.next;progress({phase:'uploading',name:source.name,index,total:sources.length,loaded:offset,size:source.size});}
        check();finishSent=true;
        progress({phase:'submitting',name:source.name,index,total:sources.length,loaded:source.size,size:source.size,upload_id});
        const queued=await F.request(target,'upload.finish',{upload_id});
        if(typeof queued?.file_id!=='string'||!/^[a-f0-9]{48}$/.test(queued.file_id))throw Error('提交结果无效。');
        upload_id=null;file_id=queued.file_id;
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
        const receipt=Object.freeze({name:source.name,savedName:saved.name,path:saved.path,size:saved.size,sha256});receipts.push(receipt);source.saved=true;file_id=null;progress({phase:'saved',name:source.name,index,total:sources.length,loaded:source.size,size:source.size,receipt});
      }catch(error){if(file_id&&!error.pendingFileId)error.pendingFileId=file_id;
        if(finishSent&&!file_id){error.unknownCommit=true;error.pendingUploadId=upload_id;error.message='提交结果待确认，勿重复上传；请先核对目标项目 tmp 的接收情况。';}throw error;}
      finally{if(upload_id&&!finishSent)try{await client.request('upload.cancel',target.session,{upload_id});}catch{}try{await source.dropPrepared?.();}catch{}}
    }}catch(error){error.receipts=[...receipts];throw error;}
    return receipts;
  }
  window.SuperMonoFilePaste={paste:()=>run(nativeSources),captureTarget:target,isCurrent:valid,
    upload:(files,options={})=>uploadSources(()=>browserSources([...files]),options),
    uploadClipboard:(options={})=>uploadSources(nativeSources,options),get busy(){return busy||jobs.some(job=>job.state==='queued');},get queued(){return jobs.length;}};
  addEventListener('beforeunload',()=>{for(const job of [...jobs]){job.controller.abort();job.reject(Error('窗口已关闭。'));forget(job);}},{once:true});
})();
