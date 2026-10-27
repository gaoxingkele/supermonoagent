/* Explicit folder selection is independent of terminal input and file paste. */
(()=>{
  'use strict';
  const C=window.SuperMonoTerminalComposer,dialog=document.getElementById('terminal-composer');
  if(window.SuperMonoFolderComposer||!C?.getTarget||!dialog)return;
  const make=(tag,text)=>{const el=document.createElement(tag);if(text!==undefined)el.textContent=text;return el;};
  const button=(id,text)=>{const el=make('button',text);el.type='button';el.id=id;return el;};
  const section=make('section');section.id='terminal-folders';
  section.append(make('h3','文件夹'));
  const actions=make('div');actions.className='composer-actions';
  const choose=button('terminal-folder-choose','选择文件夹'),paste=button('terminal-folder-paste','粘贴文件夹');
  const upload=button('terminal-folder-upload','上传文件夹'),cancel=button('terminal-folder-cancel','取消后续文件');
  actions.append(choose,paste,upload,cancel);
  const list=make('ul');list.id='terminal-folder-list';list.style.cssText='list-style:none;padding:0;max-height:160px;overflow:auto';
  const state=make('p');state.id='terminal-folder-state';state.setAttribute('role','status');
  section.append(make('p','保留目录层级和空目录；保存到目标电脑当前项目的交换目录。'),actions,list,state);
  dialog.insertBefore(section,document.getElementById('terminal-composer-capability'));
  const drafts=[];let selecting=false,inflight=null,next=0,dead=false,rendered='';
  const plain=(value,max)=>typeof value==='string'&&value.length>0&&value.length<=max&&!/[\u0000-\u001f\u007f]/u.test(value);
  const same=(a,b)=>['host','hostID','session','windowID','token','relay','code','authMode','room','endpoint'].every(key=>a[key]===b[key]);
  const current=()=>C.getTarget();
  function live(target){try{return !dead&&C.isTargetLive(target.composer)&&window.SuperMonoFileWorkspaces?.isCurrent(target.file)===true;}catch{return false;}}
  function capture(){
    const composer=current(),F=window.SuperMonoFileWorkspaces;
    if(!composer||!F||!window.SuperMonoFolderClient)throw Error('请更新完整客户端及目标电脑文件助手后选择文件夹。');
    const file=F.captureTarget();
    if(!file||file.pane!==composer.pane||file.ws!==composer.ws||file.session!==composer.session||file.generation!==composer.generation)throw Error('目标窗口已变化。');
    const result={composer,file};if(!live(result))throw Error('目标窗口已变化。');return result;
  }
  function draft(target,create=true){
    let value=drafts.find(item=>same(item.target.composer,target.composer));
    if(!value&&create){
      if(drafts.length>=64)throw Error('已保留64个文件夹会话记录，请先移除已有记录。');
      value={target,items:[],message:''};drafts.push(value);
    }
    return value;
  }
  const count=()=>drafts.reduce((sum,item)=>sum+item.items.length,0);
  const controlling=()=>drafts.some(value=>value.items.some(item=>item.controlBusy));
  async function release(snapshot){for(const folder of snapshot?.kind==='folders'?snapshot.folders||[]:[snapshot])for(const item of folder?.items||[])try{await window.pywebview?.api?.clipboard_release?.(item.id);}catch{}}
  async function releaseSource(item,id){if(item.released.has(id))return;item.released.add(id);try{await window.pywebview?.api?.clipboard_release?.(id);}catch{}}
  async function releaseItem(item){for(const source of item.snapshot.items)await releaseSource(item,source.id);}
  function render(){
    if(dead)return;let value,target;
    try{target=capture();value=draft(target,false);}catch{}
    choose.disabled=!target||selecting||!!inflight||controlling()||typeof window.pywebview?.api?.choose_folder!=='function';
    paste.disabled=!target||selecting||!!inflight||controlling()||typeof window.pywebview?.api?.clipboard_folders!=='function';
    upload.disabled=!target||!!inflight||selecting||controlling()||!value?.items.some(item=>item.status==='selected');
    cancel.disabled=!inflight||inflight.controller.signal.aborted;
    state.textContent=value?.message||(!target?'请连接目标电脑，并更新完整客户端及接收端文件助手。':'尚未选择文件夹。');
    if(inflight&&inflight.draft!==value)state.textContent+=' 另一窗口仍在传输，不会切换目标。';
    const signature=JSON.stringify([drafts.indexOf(value),(value?.items||[]).map(item=>[item.id,item.status,item.message,item.root,!!item.controlBusy]),!!inflight,controlling()]);
    if(signature===rendered)return;rendered=signature;list.replaceChildren();
    for(const item of value?.items||[]){
      const row=make('li');row.style.cssText='display:flex;gap:6px;flex-wrap:wrap;overflow-wrap:anywhere;padding:5px 0';
      const label=make('span',item.snapshot.manifest.root+' · '+item.snapshot.manifest.files.length+'个文件 · '+item.message+(item.root?' · '+item.root:''));
      row.append(label);
      if(item.transferState&&item.status!=='saved'){
        const query=button('','查询状态');query.removeAttribute('id');query.disabled=!!inflight||controlling();
        query.onclick=()=>reconcile(value,item);row.append(query);
        const stop=button('','停止此批次');stop.removeAttribute('id');stop.disabled=!!inflight||controlling();
        stop.onclick=()=>reconcile(value,item,true);row.append(stop);
      }
      const remove=button('','移除记录');remove.removeAttribute('id');remove.disabled=!!inflight||controlling()||item.status==='unknown'||item.status==='pending';
      remove.onclick=async()=>{value.items=value.items.filter(entry=>entry!==item);await releaseItem(item);render();};row.append(remove);list.append(row);
    }
  }
  async function select(kind){
    if(dead||selecting||inflight||controlling())return;let target,value,snapshot;
    try{
      target=capture();value=draft(target);if(count()>=16||value.items.length>=4)throw Error('最多保留16条文件夹记录，每个会话最多4条；请先处理或移除。');
      selecting=true;value.message='正在确认目标文件夹传输能力…';render();
      const caps=await window.SuperMonoFileWorkspaces.request(target.file,'folder.capabilities',{});
      if(caps?.version!==1)throw Error('目标电脑文件助手不支持文件夹，请升级后重试。');
      if(!live(target)||current()!==target.composer)throw Error('选择目标已变化。');
      const api=window.pywebview?.api,method=kind==='clipboard'?api?.clipboard_folders:api?.choose_folder;
      if(typeof method!=='function')throw Error('请更新完整客户端以选择文件夹。');
      snapshot=await method();
      if(!live(target)||current()!==target.composer)throw Error('选择目标已变化，文件夹未加入其他窗口。');
      if(snapshot?.kind==='empty'){value.message='未选择文件夹。';return;}
      const choices=snapshot?.kind==='folders'?snapshot.folders:[snapshot];
      if(!Array.isArray(choices)||!choices.length||value.items.length+choices.length>4||count()+choices.length>16)throw Error('文件夹记录已满。');
      for(const item of choices){
        if(item?.kind!=='folder'||!plain(item.manifest?.root,200)||!Array.isArray(item.manifest.files)||item.manifest.files.length>32||!Array.isArray(item.items)||item.items.length!==item.manifest.files.length)throw Error('文件夹清单无效。');
        if(value.items.some(old=>JSON.stringify(old.snapshot.manifest)===JSON.stringify(item.manifest)&&['unknown','pending'].includes(old.status)))throw Error('此文件夹有待确认批次，请先查询状态，不要重复上传。');
      }
      for(const item of choices)value.items.push({id:++next,target,snapshot:item,released:new Set(),status:'selected',message:'已选择，尚未上传',transferState:null,root:''});
      snapshot=null;value.message='文件夹已选择，尚未上传或导入模型。';
    }catch(error){if(value)value.message=['folder_unsupported','action_forbidden','invalid_request','file_protocol_required'].includes(error?.code)
      ?'目标电脑文件助手暂不支持文件夹，请升级完整客户端和接收端后重试。':String(error.message||'文件夹选择失败。');}
    finally{if(snapshot)await release(snapshot);selecting=false;render();}
  }
  async function runUpload(){
    if(inflight||dead||controlling())return;let target,value;
    try{target=capture();value=draft(target,false);if(!value)return;
      const transfer={target,draft:value,controller:new AbortController()};inflight=transfer;render();
      for(const item of value.items.filter(entry=>entry.status==='selected')){
        if(transfer.controller.signal.aborted)break;
        target=item.target;transfer.target=target;
        if(!live(target)){item.status='failed';item.message='原连接已变化，请重新选择';await releaseItem(item);throw Error('原目标连接已变化，未转投其他窗口。');}
        const workspace=await window.SuperMonoFileWorkspaces.getWorkspace(target.file);
        if(!live(target))throw Error('原目标连接已变化。');
        item.status='uploading';item.message='正在传输';value.message='正在上传文件夹 '+item.snapshot.manifest.root;render();
        const api=window.pywebview.api;
        let result;
        try{result=await window.SuperMonoFolderClient.uploadFolder({
          request:(action,data)=>window.SuperMonoFileWorkspaces.request(target.file,action,data),
          current:()=>live(target),manifest:item.snapshot.manifest,workspace,
          sources:item.snapshot.items.map(source=>({...source,read:offset=>api.clipboard_chunk(source.id,offset),release:()=>releaseSource(item,source.id)})),
          signal:transfer.controller.signal,onProgress:event=>{if(dead)return;item.message=String(event?.phase||'传输中');value.message='文件夹 '+item.snapshot.manifest.root+' · '+item.message;render();}
        });}catch(error){
          item.transferState=error.transferState||null;
          const pending=!!item.transferState?.batch_id||error.unknownCommit===true;
          if(!pending)item.transferState=null;
          item.status=pending?'unknown':'failed';item.message=pending?'结果待确认，请先查询状态，勿重复上传':'未完成，请重新选择';
          value.message=item.message;throw error;
        }finally{await releaseItem(item);
        }
        item.transferState=result.transferState;item.root=result.batch?.receipt?.root_path||'';item.status='saved';item.message='已保存';value.message='已保存文件夹 '+item.snapshot.manifest.root+'；尚未导入模型。';render();
      }
    }catch(error){if(value)value.message=(value.message||'')+' '+String(error.message||'文件夹未完成。');}
    finally{inflight=null;render();}
  }
  async function reconcile(value,item,stop=false){
    if(inflight||controlling()||dead||!item.transferState)return;
    item.controlBusy=true;render();
    try{
      const target=item.target;if(!live(target))throw Error('原目标连接已变化，请先恢复原连接。');
      // Status reconciliation reads the original batch, never adopts a newly
      // selected project or a post-create HMAC after tmp was allocated.
      const workspace=item.transferState.workspace;
      const action=stop?window.SuperMonoFolderClient.cancelFolder:window.SuperMonoFolderClient.reconcileFolder;
      const result=await action({request:(action,data)=>window.SuperMonoFileWorkspaces.request(target.file,action,data),current:()=>live(target),manifest:item.snapshot.manifest,workspace,state:item.transferState});
      item.transferState=result.transferState;item.root=result.batch?.receipt?.root_path||'';
      item.status=result.batch?.state==='complete'?'saved':['cancelled'].includes(result.batch?.state)?'failed':'pending';item.message=item.status==='saved'?'已保存':item.status==='failed'?'已停止后续文件':'原批次仍待确认';value.message=item.message;
    }catch(error){if(error.transferState)item.transferState=error.transferState;if(error.unknownCommit)item.status='unknown';value.message=String(error.message||'无法查询原批次。');}
    finally{item.controlBusy=false;render();}
  }
  choose.onclick=()=>select('picker');paste.onclick=()=>select('clipboard');upload.onclick=runUpload;
  cancel.onclick=()=>{if(inflight){inflight.controller.abort();inflight.draft.message='停止后续文件；已提交或保存的文件会保留。';render();}};
  const timer=setInterval(()=>{if(inflight&&!live(inflight.target))inflight.controller.abort();if(dialog.open)render();},200);
  function destroy(){if(dead)return;dead=true;inflight?.controller.abort();clearInterval(timer);for(const value of drafts)for(const item of value.items)releaseItem(item);section.remove();window.removeEventListener('beforeunload',destroy);delete window.SuperMonoFolderComposer;}
  window.SuperMonoFolderComposer={destroy,get busy(){return !!inflight||selecting||controlling();}};
  window.addEventListener('beforeunload',destroy,{once:true});render();
})();
