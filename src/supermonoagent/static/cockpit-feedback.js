'use strict';
(function(root){
  const textBytes=new TextEncoder();
  const identity=['attempt_id','host_installation_id','window_id','terminal_session_id','generation','tool','role','journal_id','account'];
  const sameAttempt=(a,b)=>a&&b&&identity.every(key=>a[key]===b[key]);
  const required=['attempt_id','host_installation_id','window_id','terminal_session_id','generation','tool'];
  const valid=id=>typeof id==='string'&&id.length>0&&id.length<=256&&!/[\x00-\x1f\x7f]/.test(id);
  function describeTarget(workspace,attempt){
    if(!required.every(key=>valid(attempt?.[key])))return null;
    const candidates=[];
    for(const host of workspace.connections.values()){
      if(!host.connected||host.cockpitInstallationId!==attempt.host_installation_id)continue;
      for(const win of host.catalog||[]){
        if(win.id!==attempt.window_id)continue;
        const session=win.sessions?.find(s=>s.id===attempt.terminal_session_id&&s.process_generation===attempt.generation);
        if(!session)continue;
        const pane=workspace.panes.get(JSON.stringify([host.id,session.id]));
        if(!pane||pane.host!==host||pane.generation!==host.generation)continue;
        const managed=session.interaction_mode==='managed';
        if(managed&&(session.provider!==attempt.tool||!attempt.journal_id||session.journal_id!==attempt.journal_id))continue;
        const capture=managed?workspace.managed.captureTarget(pane):null;
        candidates.push({host,ws:host.ws,hostGeneration:host.generation,pane,sessionId:session.id,title:win.title||win.id,
          managed,capture,canSend:!!capture&&host.capabilities.cockpit_feedback===1,
          token:host.config.token,room:host.config.code,relay:host.config.relay});
      }
    }
    return candidates.length===1?candidates[0]:null;
  }
  function sameSource(a,b){
    return a&&b&&a.host===b.host&&a.pane===b.pane&&a.ws===b.ws&&a.hostGeneration===b.hostGeneration
      &&a.token===b.token&&a.room===b.room&&a.relay===b.relay&&a.title===b.title
      &&a.canSend===b.canSend&&(!a.managed||a.capture?.targetKey===b.capture?.targetKey);
  }
  function activeAttempt(task,initial){
    if(!task||!Array.isArray(task.attempts))return null;
    const entry=task.attempts.find(item=>item.attempt_id===initial.attempt_id);
    if(!sameAttempt(initial,entry))return null;
    if(entry.role!=='verify'&&task.primary_attempt_id!==entry.attempt_id)return null;
    return entry;
  }
  function matchesReceipt(value,nonce,target){
    return value?.state==='accepted'&&value.nonce===nonce&&typeof value.submission_id==='string'&&value.submission_id
      &&typeof value.event_id==='string'&&Number.isSafeInteger(value.seq)&&value.seq>0
      &&['window_id','session_id','journal_id','process_generation'].every(k=>value[k]===target[k]);
  }
  function ledger(endpoint,credential,op,params){
    return new Promise((resolve,reject)=>{
      let url;
      try{url=new URL(endpoint);}catch{reject(Error('invalid_server'));return;}
      const local=['localhost','127.0.0.1','[::1]'].includes(url.hostname);
      if(!['/','/hub/view'].includes(url.pathname)||url.username||url.password||url.search||url.hash
        ||(url.protocol!=='wss:'&&!(local&&url.protocol==='ws:'))||!credential){
        reject(Error('invalid_confirmation_session'));return;
      }
      url.pathname='/hub/feedback';
      const socket=new root.WebSocket(url.href);
      const timer=setTimeout(()=>{socket.close();reject(Error('central_timeout'));},8000);
      socket.onopen=()=>socket.send(JSON.stringify({op,token:credential,params}));
      socket.onmessage=event=>{
        try{const message=JSON.parse(event.data);if(message.ok!==true||!message.result)throw Error();
          resolve(message.result);}catch{reject(Error('central_refused'));}finally{clearTimeout(timer);socket.close();}
      };
      socket.onclose=event=>{clearTimeout(timer);reject(Error(event.code===1008?'central_refused':'central_closed'));};
      socket.onerror=()=>{clearTimeout(timer);socket.close();reject(Error('central_unavailable'));};
    });
  }
  async function bodyHash(text){
    const bytes=await root.crypto.subtle.digest('SHA-256',textBytes.encode(text));
    return Array.from(new Uint8Array(bytes),x=>x.toString(16).padStart(2,'0')).join('');
  }
  function desktopHost(w){
    return {
      describe:attempt=>describeTarget(w,attempt),
      request:(target,action,fields)=>w.cockpitFeedbackRequest(target.capture,action,fields),
      prepareDraft:(target,text,stillCurrent)=>{
        w.openHostSession(target.host.id,target.sessionId,false);
        return stillCurrent()&&w.activePane===target.pane&&root.SuperMonoTerminalComposer?.appendFeedbackDraft(
          text,{pane:target.pane,generation:target.hostGeneration})===true;
      }
    };
  }
  function open({project,taskId,attempt,endpoint,refreshTask,isCurrent,dismiss,hostInterface}){
    if(!isCurrent()||!valid(project)||!valid(taskId)||!attempt)return;
    const host=hostInterface||(root.smaWorkspace&&desktopHost(root.smaWorkspace)),doc=root.document;
    if(!host)return;
    const candidate={...attempt},initial=host.describe(candidate);
    const dialog=doc.createElement('dialog');
    dialog.className='cockpit-feedback-dialog';dialog.setAttribute('aria-label','核对目标窗口与反馈正文');
    Object.assign(dialog.style,{maxWidth:'min(560px,94vw)',maxHeight:'90vh',overflow:'auto'});
    function element(tag,text){const node=doc.createElement(tag);node.textContent=text;return node;}
    const heading=element('h2','反馈到窗口');
    const targetLine=element('p',[project,taskId,candidate.host,initial?.title||candidate.window,candidate.tool,
      candidate.role==='verify'?'验证':'主执行',candidate.attempt_id].join(' · '));
    const body=doc.createElement('textarea');body.rows=7;body.setAttribute('aria-label','反馈全文');
    body.style.width='100%';body.style.boxSizing='border-box';
    const state=element('p','');state.setAttribute('role','status');
    const credential=doc.createElement('input');credential.type='password';credential.autocomplete='off';
    credential.setAttribute('aria-label','窗口反馈授权');credential.placeholder='窗口反馈授权';
    credential.hidden=!initial?.canSend;
    const primary=element('button',initial?.canSend?'确认发送到 '+initial.title:'准备草稿');
    const check=element('button','核对原请求');check.hidden=!initial?.canSend;
    const cancel=element('button','关闭');
    for(const button of [primary,check,cancel])button.type='button';
    dialog.append(heading,targetLine,body,credential,state,primary,check,cancel);
    let live=true,busy=false,request=null;
    const guards=()=>live&&isCurrent()&&sameSource(initial,host.describe(candidate));
    const cleanup=()=>{if(!live)return;live=false;body.value='';credential.value='';dialog.remove();
      root.removeEventListener('sma-cockpit-context-invalidated',invalidate);};
    function invalidate(){cleanup();}
    function lock(){primary.disabled=true;body.readOnly=true;}
    function invalidateSelection(){lock();check.hidden=true;state.textContent='目标或授权已变化，请重新选择窗口。';}
    doc.body.append(dialog);dialog.showModal();
    root.addEventListener('sma-cockpit-context-invalidated',invalidate);
    dialog.addEventListener('close',cleanup,{once:true});cancel.onclick=()=>dialog.close();
    if(!initial||initial.managed&&!initial.canSend){
      lock();state.textContent='此执行尚未绑定可核对身份的终端，或主机尚未启用反馈。';return;
    }
    const feedback={project,task_id:taskId,attempt_id:candidate.attempt_id,host_installation_id:candidate.host_installation_id,
      tool:candidate.tool,role:candidate.role,account:candidate.account};
    const destination={...feedback,window_id:candidate.window_id,terminal_session_id:candidate.terminal_session_id,
      generation:candidate.generation,journal_id:candidate.journal_id};
    async function authorized(){
      if(!guards())return false;
      const current=await refreshTask();
      return guards()&&current?.task_id===taskId&&!!activeAttempt(current,candidate);
    }
    check.onclick=async()=>{
      if(busy||!initial.canSend)return;
      if(!credential.value){state.textContent='请输入本人的窗口反馈授权。';credential.focus();return;}
      busy=true;check.disabled=true;
      try{
        if(!await authorized()){invalidateSelection();return;}
        request=await ledger(endpoint,credential.value,'pending',{target:destination});
        if(!guards()){invalidateSelection();return;}
        if(!request.nonce){request=null;state.textContent='当前执行没有已登记的反馈。';return;}
        const result=await host.request(initial,'cockpit_feedback_status',{feedback,nonce:request.nonce});
        if(!guards()){invalidateSelection();return;}
        if(matchesReceipt(result?.receipt,request.nonce,initial.capture.target)){
          check.hidden=true;state.textContent='目标已持久接收反馈。';
        }else if(result?.receipt===null&&Date.now()/1000>request.expires_at){
          check.hidden=true;state.textContent='原请求已过期，目标尚无接收回执。';
        }else state.textContent='尚无可确认的接收回执。';
      }catch{if(live)state.textContent='当前无法核对原请求。';}
      finally{busy=false;check.disabled=false;}
    };
    primary.onclick=async()=>{
      if(busy||request||primary.disabled)return;
      const text=body.value;
      if(!text.trim()||textBytes.encode(text).length>65536){state.textContent='请输入 1 至 65536 字节的反馈正文。';return;}
      if(initial.canSend&&!credential.value){state.textContent='请输入本人的窗口反馈授权。';credential.focus();return;}
      busy=true;lock();
      try{
        if(!await authorized()){invalidateSelection();return;}
        if(!initial.managed){
          if(!host.prepareDraft(initial,text,guards)){
            primary.disabled=false;body.readOnly=false;state.textContent='草稿忙或长度超限，反馈正文已保留。';return;
          }
          dialog.close();dismiss();
          return;
        }
        const sha=await bodyHash(text);
        if(!guards()){invalidateSelection();return;}
        request=await ledger(endpoint,credential.value,'confirm',{target:destination,text_sha256:sha,baseline_seq:candidate.seq});
        if(!guards()){invalidateSelection();return;}
        if(request.may_send!==true){state.textContent='已有反馈登记，请核对原请求。';return;}
        const result=await host.request(initial,'submit_cockpit_feedback',
          {feedback,nonce:request.nonce,expires_at:request.expires_at,text,text_sha256:sha});
        if(!guards()){invalidateSelection();return;}
        if(!matchesReceipt(result,request.nonce,initial.capture.target))throw Error('receipt_unknown');
        check.hidden=true;state.textContent='目标已持久接收反馈。';
      }catch{
        if(live){state.textContent=initial.canSend?'发送结果尚未确认，请核对原请求。':'目标或草稿不可用，请重新核对。';check.hidden=!initial.canSend;}
      }finally{busy=false;}
    };
    if(!request)body.focus();
  }
  function openEvolution({endpoint,project,taskId,attempt,skillId,isCurrent,refreshTask,readStatus,hostInterface}){
    if(!isCurrent()||!valid(skillId)||!valid(project)||!valid(taskId)||!valid(attempt?.attempt_id))return;
    const initial=hostInterface?.describe(attempt);
    if(hostInterface&&!initial)return;
    const doc=root.document,dialog=doc.createElement('dialog');
    dialog.className='cockpit-evolution-confirm';dialog.setAttribute('aria-label','核对版本与目标执行');
    Object.assign(dialog.style,{maxWidth:'min(620px,94vw)',maxHeight:'90vh',overflow:'auto'});
    const make=(tag,text)=>{const e=doc.createElement(tag);e.textContent=text;return e;};
    const summary=make('p',[project,taskId,attempt.host,attempt.window,attempt.tool,attempt.role,attempt.attempt_id].join(' · '));
    const asset=make('pre','');asset.style.whiteSpace='pre-wrap';asset.style.overflowWrap='anywhere';
    const message=make('p','');message.setAttribute('role','status');
    const token=doc.createElement('input');token.type='password';token.autocomplete='off';token.placeholder='版本变更授权';token.setAttribute('aria-label','版本变更授权');
    const previewBtn=make('button','预览目标版本'),confirm=make('button','确认应用到 '+(attempt.window||attempt.attempt_id));
    const status=make('button','核对加载状态'),close=make('button','关闭');
    for(const b of [previewBtn,confirm,status,close])b.type='button';
    confirm.disabled=true;
    dialog.append(make('h2','应用 mylib 版本'),summary,make('p',skillId),asset,token,previewBtn,confirm,status,message,close);
    doc.body.append(dialog);dialog.showModal();
    const target={project,task_id:taskId,attempt_id:attempt.attempt_id,generation:attempt.generation};
    let live=true,busy=false,frozen=null,submitted=false;
    const current=()=>live&&isCurrent()&&(!hostInterface||sameSource(initial,hostInterface.describe(attempt)));
    const remove=()=>{if(!live)return;live=false;token.value='';asset.textContent='';dialog.remove();
      root.removeEventListener('sma-cockpit-context-invalidated',remove);};
    root.addEventListener('sma-cockpit-context-invalidated',remove);
    close.onclick=()=>dialog.close();dialog.addEventListener('close',remove,{once:true});
    const authorized=async()=>{if(!current())return false;const t=await refreshTask();
      return current()&&t?.task_id===taskId&&!!activeAttempt(t,attempt);};
    function matches(value){
      return value?.skill_id===skillId&&Object.entries(target).every(([k,v])=>value.target?.[k]===v)
        &&['host','host_installation_id','window_id','tool','tool_version','role'].every(k=>value.target[k]===attempt[k]);
    }
    previewBtn.onclick=async()=>{
      if(busy||submitted)return;
      if(!token.value){message.textContent='请输入本人的版本变更授权。';return;}
      busy=true;frozen=null;asset.textContent='';confirm.disabled=true;
      try{
        if(!await authorized())throw Error();
        const value=await ledger(endpoint,token.value,'evolution_preview',{target,skill_id:skillId});
        if(!current()||!matches(value)||value.state!=='preview'||!/^([a-f0-9]{64})$/.test(value.asset?.sha256)
          ||!/^([a-f0-9]{64})$/.test(value.asset?.version))throw Error();
        frozen={version:value.asset.version,asset_sha256:value.asset.sha256};
        asset.textContent=['实际已加载：'+(value.loaded_version||'未知'),'申请版本：'+value.asset.version,
          '类型：'+value.asset.kind,'来源：'+value.asset.revision+' / '+value.asset.path,
          'SHA-256：'+value.asset.sha256].join('\n');
        confirm.disabled=false;message.textContent='请核对目标与版本后确认。';
      }catch{if(current())message.textContent='目标、版本或授权发生变化，请重新核对。';}
      finally{busy=false;}
    };
    confirm.onclick=async()=>{
      if(busy||submitted||!frozen||confirm.disabled)return;
      busy=true;submitted=true;confirm.disabled=true;previewBtn.disabled=true;
      try{
        if(!await authorized())throw Error();
        const value=await ledger(endpoint,token.value,'evolution_confirm',{target,skill_id:skillId,...frozen,request_id:root.crypto.randomUUID()});
        if(!current()||!matches(value)||value.asset?.version!==frozen.version||value.asset?.sha256!==frozen.asset_sha256)throw Error();
        message.textContent='已登记版本变更，请核对宿主加载状态。';
      }catch{if(current())message.textContent='变更结果待核对，请查询加载状态。';}
      finally{busy=false;}
    };
    status.onclick=async()=>{
      if(busy||!current())return;
      busy=true;
      try{
        if(!await authorized())throw Error();
        const value=await readStatus();
        if(!current())return;
        const states={loaded:'已加载',pending_restart:'待重启',confirmed:'已确认',dispatched:'等待宿主回执',failed:'加载失败',expired:'已过期'};
        message.textContent=(states[value.loading_state]||'加载状态未知')+' · 实际版本 '+(value.loaded_version||'未知');
      }catch{if(current())message.textContent='当前权限或连接无法核对加载状态。';}
      finally{busy=false;}
    };
  }
  const api={open,openEvolution,describeTarget,sameSource,activeAttempt,matchesReceipt};
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.SuperMonoCockpitFeedback=api;
})(typeof window==='object'?window:globalThis);
