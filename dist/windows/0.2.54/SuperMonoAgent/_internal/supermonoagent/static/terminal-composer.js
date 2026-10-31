/* Additive, target-bound text draft and attachment editor. No global paste hooks. */
(()=>{
  'use strict';
  if(window.SuperMonoTerminalComposer||!window.smaWorkspace)return;
  const W=window.smaWorkspace,toolbar=document.querySelector('main>footer');
  if(!toolbar)return;
  const node=(tag,text)=>{const el=document.createElement(tag);if(text!==undefined)el.textContent=text;return el;};
  const button=(id,text)=>{const el=node('button',text);el.id=id;el.type='button';return el;};
  const plain=(value,max)=>typeof value==='string'&&value.length>0&&value.length<=max&&!/[\u0000-\u001f\u007f]/u.test(value);
  const style=node('style');style.id='terminal-composer-style';
  style.textContent=`#terminal-composer-open{flex:none;white-space:nowrap;font-size:10px}footer:has(>#terminal-composer-open){min-width:0;overflow-x:auto}#terminal-composer{width:620px;max-width:calc(100vw - 24px);max-height:calc(100dvh - 24px);padding:18px;overflow:auto;overflow-wrap:anywhere}#terminal-composer h2{font-size:16px;overflow-wrap:anywhere;line-height:1.5}#terminal-composer label{display:block;margin:10px 0 5px}#terminal-composer-text{box-sizing:border-box;width:100%;min-width:0;max-width:100%;min-height:120px;height:220px;max-height:40dvh;resize:vertical;border:1px solid #555;border-radius:5px;padding:9px;background:#141414;color:inherit;font:inherit;font-size:13px;line-height:1.6;white-space:pre-wrap;overflow:auto}#terminal-composer .composer-actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}#terminal-composer input[type=file]{max-width:100%;min-width:0;width:100%}#terminal-composer-files,#terminal-composer-receipts{list-style:none;padding:0;margin:8px 0;max-height:160px;overflow:auto}#terminal-composer li{display:flex;gap:6px;align-items:center;min-width:0;padding:4px 0;border-bottom:1px solid #383838}#terminal-composer li>span{flex:1;min-width:0;overflow-wrap:anywhere;white-space:pre-wrap}#terminal-composer li>button{flex:none;font-size:11px}#terminal-composer p{margin:8px 0;font-size:12px;line-height:1.5;white-space:pre-wrap}#terminal-composer-state{color:#ddd}#terminal-composer-progress{width:100%;height:10px}#terminal-composer-hint,#terminal-composer-capability{color:#aaa}@media(max-width:600px){#terminal-composer{padding:13px}#terminal-composer-text{height:170px;min-height:90px}#terminal-composer-files,#terminal-composer-receipts{max-height:110px}}`;
  document.head.append(style);
  const openButton=button('terminal-composer-open','编辑');openButton.title='编辑文本与附件';toolbar.prepend(openButton);
  const dialog=node('dialog');dialog.id='terminal-composer';dialog.setAttribute('aria-label','编辑文本与附件');
  const heading=node('h2','编辑文本与附件'),label=node('label','多行文本');label.htmlFor='terminal-composer-text';
  const textarea=node('textarea');textarea.id='terminal-composer-text';textarea.rows=8;textarea.spellcheck=false;
  textarea.placeholder='自由编辑多行文本；支持正常 Ctrl+V';
  const hint=node('p','编辑后粘贴到当前终端，不额外按 Enter；提交行为由终端程序决定。草稿会保留。');hint.id='terminal-composer-hint';
  const paste=button('terminal-composer-paste','粘贴文本'),fileLabel=node('label','选择附件（每批最多 32 个，每个不超过 64 MiB）');
  const picker=node('input');picker.id='terminal-composer-files-input';picker.type='file';picker.multiple=true;fileLabel.htmlFor=picker.id;
  const files=node('ul');files.id='terminal-composer-files';const receipts=node('ul');receipts.id='terminal-composer-receipts';
  const capability=node('p');capability.id='terminal-composer-capability';
  const progress=node('progress');progress.id='terminal-composer-progress';progress.max=100;progress.value=0;progress.hidden=true;
  const state=node('p');state.id='terminal-composer-state';state.setAttribute('role','status');state.setAttribute('aria-live','polite');
  const upload=button('terminal-composer-upload','上传附件'),cancel=button('terminal-composer-cancel','取消上传'),insert=button('terminal-composer-insert','插入输入行'),close=button('terminal-composer-close','关闭');
  insert.className='primary';
  const actions=node('div');actions.className='composer-actions';actions.append(paste,upload,cancel,insert,close);
  dialog.append(heading,label,textarea,hint,fileLabel,picker,files,receipts,capability,progress,state,actions);document.body.append(dialog);
  const drafts=[],transfers=new Set();let current=null,inflight=null,nextFile=0,destroyed=false,openError='',listDraft=null,listSignature='';
  function windowOf(host,session){return host.catalog?.find(win=>(win.sessions||[]).some(item=>item.id===session));}
  function capture(){
    const api=window.SuperMonoFilePaste;
    const bound=typeof api?.captureTarget==='function'?api.captureTarget():null;
    if(typeof api?.captureTarget==='function'&&!bound)throw Error('请先选择已连接的目标窗口。');
    const pane=bound?.pane||W.activePane,host=pane?.host,key=pane?.sid||W.activeSession,win=host&&windowOf(host,pane.raw);
    if(!pane||!host||!key||!host.connected||!pane.ready||pane.generation!==host.generation||W.panes.get(key)!==pane||host.ws?.readyState!==1)throw Error('请先选择已连接的目标窗口。');
    return {bound,api,pane,term:pane.term,host,key,ws:host.ws,generation:host.generation,paneGeneration:pane.generation,
      hostID:host.id,session:pane.raw,windowID:win?.id,token:host.config?.token,relay:host.config?.relay,
      code:host.config?.code,authMode:host.config?.authMode,room:bound?.room,endpoint:bound?.endpoint||bound?.broker_url,
      machine:String(host.name||host.id).slice(0,128),
      title:String(win?.title||pane.title?.textContent||pane.raw).slice(0,128)};
  }
  function live(target){
    if(!target||destroyed||W.panes.get(target.key)!==target.pane||target.pane.host!==target.host
      ||target.pane.raw!==target.session||target.host.ws!==target.ws||target.ws?.readyState!==1
      ||!target.host.connected||target.host.generation!==target.generation||target.pane.generation!==target.paneGeneration
      ||target.host.id!==target.hostID||target.host.config?.token!==target.token||target.host.config?.relay!==target.relay
      ||target.host.config?.code!==target.code||target.host.config?.authMode!==target.authMode
      ||windowOf(target.host,target.session)?.id!==target.windowID)return false;
    if(W.connections?.get&&W.connections.get(target.hostID)!==target.host)return false;
    if(target.bound){try{return window.SuperMonoFilePaste===target.api&&typeof target.api.isCurrent==='function'&&target.api.isCurrent(target.bound)===true;}catch{return false;}}
    return true;
  }
  const active=target=>live(target)&&W.activePane===target.pane&&W.activeSession===target.key&&target.pane.ready&&target.pane.term===target.term;
  const canInsert=target=>active(target)&&typeof target.term?.paste==='function'&&target.term.options?.disableStdin!==true&&target.term.textarea?.readOnly!==true;
  const sameDraft=(a,b)=>['host','hostID','session','windowID','token','relay','code','authMode','room','endpoint'].every(key=>a[key]===b[key]);
  function draftFor(target){
    let draft=drafts.find(item=>sameDraft(item.target,target));
    if(!draft){
      if(drafts.length>=64){const empty=drafts.findIndex(item=>!item.text&&!item.files.length&&!item.receipts.length&&inflight?.draft!==item);if(empty<0)throw Error('已保留 64 个会话草稿；请先处理已有草稿。');drafts.splice(empty,1);}
      draft={target,text:'',revision:0,inserted:-1,files:[],receipts:[],message:''};drafts.push(draft);
    }
    draft.target=target;
    return draft;
  }
  function remember(){if(current&&textarea.value!==current.draft.text){current.draft.text=textarea.value;current.draft.revision++;}}
  function usableUpload(target){return live(target)&&!!target.bound&&typeof target.api?.upload==='function'&&typeof target.api?.captureTarget==='function'&&typeof target.api?.isCurrent==='function';}
  function receipt(value){return value&&plain(value.name,256)&&plain(value.path,4096)&&!value.path.includes('://')
    &&Number.isSafeInteger(value.size)&&value.size>=0&&typeof value.sha256==='string'&&/^[a-fA-F0-9]{64}$/.test(value.sha256);}
  function adopt(draft,values){
    for(const value of values||[]){
      if(!receipt(value))continue;
      if(!draft.receipts.some(old=>old.path===value.path&&old.sha256===value.sha256))draft.receipts.push(Object.freeze({name:value.name,path:value.path,size:value.size,sha256:value.sha256}));
    }
  }
  function render(){
    const ctx=current,draft=ctx?.draft,target=ctx?.target;
    insert.disabled=!ctx||!canInsert(target)||!draft.text||draft.inserted===draft.revision;
    paste.disabled=!ctx;picker.disabled=!ctx||inflight?.draft===draft;
    upload.disabled=!ctx||!!inflight||!draft.files.some(item=>!item.pending&&!item.nativeClipboard)||!usableUpload(target)||!active(target);
    cancel.disabled=!inflight||inflight.controller.signal.aborted;
    capability.textContent=ctx&&!usableUpload(target)?'附件上传不可用：当前客户端缺少目标绑定上传入口，或目标连接已变化。文本草稿仍保留。':'附件仅上传保存到目标目录；不会自动导入模型。';
    if(ctx&&!active(target))capability.textContent+=' 当前活动窗口已变化；返回此窗口或重新选择编辑目标后再插入。';
    const signature=JSON.stringify([(draft?.files||[]).map(item=>[item.id,item.pending,item.pendingFileId,item.pendingUploadId,item.unknownCommit]),(draft?.receipts||[]).map(item=>[item.path,item.sha256]),inflight?.draft===draft]);
    if(listDraft!==draft||listSignature!==signature){listDraft=draft;listSignature=signature;files.replaceChildren();receipts.replaceChildren();
    for(const item of draft?.files||[]){
      const pending=item.pending?(item.unknownCommit?' · 提交结果待确认，勿重复':' · 中继已暂存，待目标确认')+(item.pendingFileId?' · 任务 '+item.pendingFileId:item.pendingUploadId?' · 上传 '+item.pendingUploadId:''):item.nativeClipboard?' · 原生附件未完成，请重新复制后粘贴':'';
      const row=node('li'),name=node('span',item.file.name+' · '+item.file.size+' 字节'+pending),remove=button('',item.pending?'待确认':'移除');
      remove.removeAttribute('id');remove.disabled=inflight?.draft===draft||item.pending===true;
      remove.onclick=()=>{draft.files=draft.files.filter(value=>value!==item);render();};row.append(name,remove);files.append(row);
    }
    for(const saved of draft?.receipts||[]){
      const row=node('li'),name=node('span','已保存: '+saved.path),add=button('','路径加到草稿');add.removeAttribute('id');
      add.onclick=()=>{if(!current||current.draft!==draft)return;remember();draft.text+=(draft.text&&!draft.text.endsWith('\n')?'\n':'')+saved.path;draft.revision++;textarea.value=draft.text;draft.message='路径已加到草稿，尚未插入终端或导入模型。';render();};
      row.append(name,add);receipts.append(row);
    }
    }
    state.textContent=draft?.message||openError;
    if(inflight&&inflight.draft!==draft)state.textContent+='\n'+inflight.target.machine+' / '+inflight.target.title+' 仍在上传；不会改投当前窗口。';
    progress.hidden=!inflight;
  }
  function open(){
    if(destroyed)return;remember();
    try{const target=capture(),draft=draftFor(target);current={target,draft};openError='';heading.textContent='编辑 · '+target.machine+' / '+target.title;textarea.value=draft.text;textarea.disabled=false;}
    catch(error){current=null;heading.textContent='编辑文本与附件';textarea.value='';textarea.disabled=true;openError=String(error.message||'目标窗口不可用。');}
    if(!dialog.open)dialog.showModal();render();textarea.focus();
  }
  function dismiss(){remember();dialog.close();}
  openButton.onclick=open;close.onclick=dismiss;dialog.addEventListener('cancel',()=>remember());
  textarea.addEventListener('input',()=>{remember();render();});
  async function pasteClipboard(){
    const ctx=current;if(!ctx||!dialog.open||textarea.disabled||textarea.readOnly||!active(ctx.target))return false;
    remember();const revision=ctx.draft.revision,start=textarea.selectionStart,end=textarea.selectionEnd;
    try{
      const api=window.pywebview?.api;
      let files=false;try{files=typeof api?.clipboard_has_files==='function'&&await api.clipboard_has_files()===true;}catch{}
      if(!dialog.open||current!==ctx||!active(ctx.target))return false;
      if(files){
        if(typeof ctx.target.api?.uploadClipboard!=='function'){ctx.draft.message='当前客户端缺少原生附件上传入口，请更新完整客户端。';render();return false;}
        return startUpload(true,ctx);
      }
      let value;
      try{if(typeof api?.clipboard_read_text!=='function')throw Error();value=await api.clipboard_read_text();}
      catch{if(!navigator.clipboard?.readText)throw Error();value=await navigator.clipboard.readText();}
      if(!dialog.open||current!==ctx||!active(ctx.target))return false;
      if(textarea.disabled||textarea.readOnly||ctx.draft.revision!==revision||textarea.selectionStart!==start||textarea.selectionEnd!==end){ctx.draft.message='草稿、光标或编辑权限已变化，请在所需光标位置重新粘贴。';render();return false;}
      if(typeof value!=='string'||!value){ctx.draft.message='剪贴板没有可用文本；文件或图片请使用下方选择附件。';render();return false;}
      textarea.setRangeText(value,start,end,'end');textarea.dispatchEvent(new Event('input',{bubbles:true}));ctx.draft.message='文本已放入草稿。';render();textarea.focus();return true;
    }catch{if(current===ctx){ctx.draft.message='无法读取剪贴板，请在文本框中使用原生 Ctrl+V。';render();textarea.focus();}return false;}
  }
  paste.textContent='粘贴';paste.onclick=pasteClipboard;
  function addFiles(values){
    const draft=current?.draft,chosen=[...(values||[])];if(!draft||!dialog.open||!active(current.target))return false;
    if(draft.files.length+chosen.length>32){draft.message='每批最多选择 32 个文件。';render();return false;}
    if(chosen.some(file=>!plain(file.name,256)||!Number.isSafeInteger(file.size)||file.size<0||file.size>64*1024*1024||file.webkitRelativePath)){
      draft.message='请选择普通文件；每个文件不超过 64 MiB，暂不支持整个文件夹。';render();return false;
    }
    draft.files.push(...chosen.map(file=>({id:++nextFile,file})));draft.message='已选择附件，尚未上传保存。';render();return true;
  }
  picker.addEventListener('change',()=>{const chosen=[...(picker.files||[])];picker.value='';addFiles(chosen);});
  async function startUpload(native=false,ctx=current){
    if(!ctx||(!native&&inflight)||transfers.size>=32||!usableUpload(ctx.target)||!active(ctx.target)||(!native&&!ctx.draft.files.some(item=>!item.pending&&!item.nativeClipboard)))return false;
    const transfer={target:ctx.target,draft:ctx.draft,native,files:native?[]:ctx.draft.files.filter(item=>!item.pending&&!item.nativeClipboard),controller:new AbortController(),saved:new Set(),phase:'',index:0,pendingId:null};transfers.add(transfer);if(!inflight)inflight=transfer;
    transfer.draft.message='正在上传到 '+transfer.target.machine+' / '+transfer.target.title+'…';progress.value=0;render();
    const markSaved=(value,index)=>{
      if(!receipt(value))return;
      const item=Number.isInteger(index)?transfer.files[index]:transfer.files.find(file=>file.file.name===value.name&&file.file.size===value.size&&!transfer.saved.has(file.id));
      if(item&&item.file.size===value.size&&!transfer.saved.has(item.id)){adopt(transfer.draft,[value]);transfer.saved.add(item.id);transfer.draft.files=transfer.draft.files.filter(file=>file.id!==item.id);}
    };
    try{
      const options={target:transfer.target.bound,signal:transfer.controller.signal,onProgress:value=>{
        if(!transfers.has(transfer)||destroyed)return;
        inflight=transfer;
        if(value&&typeof value==='object'){
          if(native&&value.phase==='preparing'&&Number.isInteger(value.index)&&value.index>=0&&value.index<32&&!transfer.files[value.index]){
            if(!plain(value.name,256)||!Number.isSafeInteger(value.size)||value.size<0||transfer.draft.files.length>=32){transfer.limitExceeded=true;transfer.controller.abort();return;}
            const item={id:++nextFile,file:{name:value.name,size:value.size},nativeClipboard:true};transfer.files[value.index]=item;transfer.draft.files.push(item);
          }
          if(Number.isInteger(value.index)&&value.index>=0&&value.index<transfer.files.length)transfer.index=value.index;
          if(typeof value.phase==='string')transfer.phase=value.phase;
          if(value.phase==='preparing')transfer.pendingId=null;
          if(value.phase==='receiving'&&typeof value.file_id==='string'&&/^[a-f0-9]{48}$/.test(value.file_id))transfer.pendingId=value.file_id;
          if(value.phase==='saved')markSaved(value.receipt,value.index);
          const loaded=Number(value.loaded),size=Number(value.size),index=Number(value.index),total=Number(value.total);
          if(Number.isFinite(loaded)&&size>0&&Number.isInteger(index)&&total>0)progress.value=Math.max(0,Math.min(value.phase==='saved'?100:99,(index+loaded/size)/total*100));
          const name=typeof value.name==='string'?value.name.slice(0,256):'';
          transfer.draft.message=(value.phase==='saved'?'已收到目标保存确认: ':value.phase==='submitting'?'正在提交中继，等待结果确认: ':value.phase==='receiving'?'中继已暂存，等待目标保存确认: ':'正在传输: ')+name;
        }
        render();
      }};
      const values=await (native?transfer.target.api.uploadClipboard(options):transfer.target.api.upload(transfer.files.map(item=>item.file),options));
      if(!live(transfer.target))throw Error('目标连接已变化。');
      if(!Array.isArray(values)||values.length!==transfer.files.length||values.some(value=>!receipt(value)))throw Error('目标未返回有效保存凭据。');
      values.forEach((value,index)=>markSaved(value,index));
      if(transfer.saved.size!==transfer.files.length)throw Error('目标未返回完整保存凭据。');
      transfer.draft.message='已保存 '+values.length+' 个附件到捕获目标；'+(transfer.controller.signal.aborted?'已提交项保存完成，取消后不会继续新文件。':'')+'尚未导入模型。可将路径显式加到草稿。';
      return true;
    }catch(error){
      if(Array.isArray(error?.receipts))error.receipts.forEach((value,index)=>markSaved(value,index));
      const pending=transfer.files[transfer.index],pendingId=typeof error?.pendingFileId==='string'&&/^[a-f0-9]{48}$/.test(error.pendingFileId)?error.pendingFileId:transfer.pendingId;
      const unknownCommit=error?.unknownCommit===true||transfer.phase==='submitting';
      const uncertain=pending&&!transfer.saved.has(pending.id)&&(pendingId||transfer.phase==='receiving'||unknownCommit);
      if(uncertain){pending.pending=true;pending.pendingFileId=pendingId;pending.unknownCommit=unknownCommit;
        if(typeof error?.pendingUploadId==='string'&&/^[a-f0-9]{48}$/.test(error.pendingUploadId))pending.pendingUploadId=error.pendingUploadId;}
      const missing=/文件交换助手尚未安装|更新完整客户端/.test(typeof error?.message==='string'?error.message:'');
      transfer.draft.message=transfer.files.length>0&&transfer.saved.size===transfer.files.length?'附件均已收到保存确认；捕获目标状态已变化，尚未导入模型。草稿和凭据保留。':
        (transfer.saved.size?'已保存 '+transfer.saved.size+' 项；':'')+(uncertain?(unknownCommit?'提交结果待确认，勿重复；已挂起且不会再次上传，请先核对目标项目 tmp。':'另有文件已中继暂存但未获目标保存确认，已挂起且不会再次上传。请核对任务状态。'):
        missing?'文件交换助手尚未安装，请更新完整客户端及接收机助手。':transfer.limitExceeded?'附件队列最多32项，请先移除或处理已有项目。':transfer.controller.signal.aborted?'未提交项上传已取消。':'其余附件未完成，请检查目标后台能力或连接。')+' 文本、附件记录及保存凭据均保留。';return false;
    }finally{transfers.delete(transfer);if(inflight===transfer)inflight=transfers.values().next().value||null;if(!destroyed)render();}
  }
  upload.onclick=()=>startUpload();
  cancel.onclick=()=>{if(inflight){for(const item of transfers)item.controller.abort();inflight.draft.message=inflight.phase==='receiving'?
    '本项已提交中继，不能撤销；正在等待目标确认，之后停止后续附件。':inflight.phase==='submitting'?
    '提交结果尚待确认；取消后不会继续后续附件，请勿重复上传。':'正在取消未提交上传；已保存项会保留。';render();}};
  insert.onclick=()=>{
    const ctx=current;if(!ctx)return;remember();
    if(!canInsert(ctx.target)){ctx.draft.message='当前活动窗口或输入能力已变化；请返回捕获窗口并检查输入权限。';render();return;}
    if(!ctx.draft.text||ctx.draft.inserted===ctx.draft.revision)return;
    try{ctx.target.term.paste(ctx.draft.text);ctx.draft.inserted=ctx.draft.revision;ctx.draft.message='已通过终端粘贴插入；未额外发送 Enter。草稿与附件保存凭据保留。';}
    catch{ctx.draft.message='无法插入当前输入行；草稿已保留，请检查终端连接。';}
    render();
  };
  const timer=setInterval(()=>{
    for(const item of transfers)if(!live(item.target)&&!item.controller.signal.aborted)item.controller.abort();
    if(dialog.open)render();
    openButton.disabled=!W.activePane?.host?.connected;
  },200);
  function destroy(){
    if(destroyed)return;destroyed=true;for(const item of transfers)item.controller.abort();clearInterval(timer);window.removeEventListener('beforeunload',destroy);
    dialog.remove();openButton.remove();style.remove();drafts.length=0;current=null;delete window.SuperMonoTerminalComposer;
  }
  window.SuperMonoTerminalComposer={open,close:dismiss,destroy,pasteClipboard,addFiles,get busy(){return transfers.size>0;}};
  window.addEventListener('beforeunload',destroy,{once:true});render();
})();
