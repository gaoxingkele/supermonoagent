/* Assistant conversation sidecar: no terminal input, resize, or direct network. */
(function installAssistantChat(){
  'use strict';
  if(document.getElementById('assistant-chat-section'))return;
  const panel=document.getElementById('mylib-panel'),api=window.pywebview?.api,W=window.smaWorkspace;
  if(!panel||!api?.assistant_chat||!W){addEventListener('pywebviewready',installAssistantChat,{once:true});return;}
  const create=(tag,text)=>{const element=document.createElement(tag);if(text!==undefined)element.textContent=text;return element;};
  const section=create('section');section.id='assistant-chat-section';section.setAttribute('aria-label','助手对话');
  const heading=create('h3','助手对话'),history=create('div');history.id='assistant-chat-history';history.setAttribute('role','log');history.setAttribute('aria-live','polite');
  const form=create('form'),label=create('label','给当前窗口助手提问'),question=create('textarea');form.id='assistant-chat-form';question.id='assistant-chat-question';question.maxLength=4000;question.rows=4;question.placeholder='输入问题，或分析当前窗口';label.htmlFor=question.id;
  const attachLabel=create('label'),attach=create('input');attach.type='checkbox';attach.id='assistant-chat-context';attachLabel.append(attach,document.createTextNode('附带当前窗口最近输出'));
  const ledgerLabel=create('label'),attachLedger=create('input');attachLedger.type='checkbox';attachLedger.id='assistant-chat-ledger';ledgerLabel.append(attachLedger,document.createTextNode('附带项目账本片段，发送到所选模型'));
  const actions=create('div'),send=create('button','发送'),analyze=create('button','分析当前窗口'),cancel=create('button','取消');send.id='assistant-chat-send';send.type='submit';analyze.id='assistant-chat-analyze';analyze.type='button';cancel.id='assistant-chat-cancel';cancel.type='button';actions.append(send,analyze,cancel);
  const status=create('p');status.id='assistant-chat-status';status.setAttribute('role','status');
  form.append(label,question,attachLabel,ledgerLabel,actions);section.append(heading,history,form,status);panel.append(section);
  const style=create('style');style.textContent='#assistant-chat-section{min-width:0;border-top:1px solid #444;margin-top:14px;padding-top:10px}#assistant-chat-section h3{font-size:14px}#assistant-chat-form>label{display:block;font-size:12px;margin:9px 0}#assistant-chat-question{box-sizing:border-box;width:100%;min-width:0;max-width:100%;resize:vertical;min-height:80px;max-height:220px;background:#111;color:#eee;border:1px solid #555;border-radius:5px;padding:8px;font-family:inherit;font-size:12px;line-height:1.5}#assistant-chat-form input[type=checkbox]{min-height:0;margin-right:6px}#assistant-chat-form>div{display:flex;flex-wrap:wrap;gap:3px}#assistant-chat-history{max-height:360px;overflow:auto;overflow-wrap:anywhere}#assistant-chat-history article{margin:8px 0;padding:8px}#assistant-chat-history p{white-space:pre-wrap;margin:5px 0;font-size:12px}#assistant-chat-status{white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px}';document.head.append(style);
  const threads=new Map();let current=null,inFlight=null,nonce=0,preferencesEpoch=0,preferencesRead=0,autoContext=false,contextLimit=8192;
  const identity=pane=>({pane,host:pane.host,ws:pane.host.ws,session:pane.raw,generation:pane.host.generation,paneGeneration:pane.generation,credential:typeof pane.host.config?.token==='string'?pane.host.config.token:''});
  const sameIdentity=(a,b)=>!!a&&!!b&&a.pane===b.pane&&a.host===b.host&&a.ws===b.ws&&a.session===b.session&&a.generation===b.generation&&a.paneGeneration===b.paneGeneration&&a.credential===b.credential;
  const redact=(text,credential)=>credential?text.split(credential).join('[凭据已隐藏]'):text;
  const selected=()=>{const pane=W.activePane,key=W.activeSession;return pane&&key?{key,...identity(pane)}:null;};
  function threadFor(key,pane=W.panes.get(key)){
    let thread=threads.get(key);
    const binding=pane?identity(pane):null;
    if(thread){
      if(!sameIdentity(thread.binding,binding)){
        thread.draft=redact(thread.draft,thread.binding?.credential||'');thread.messages=[];thread.ledger=false;thread.binding=binding;
        if(inFlight?.thread===thread)cancelRequest('连接身份已变化，正在取消原请求。');
        thread.status='连接身份已变化，已开始新对话。';
      }
      threads.delete(key);threads.set(key,thread);return thread;
    }
    thread={draft:'',messages:[],status:'',attach:autoContext,attachTouched:false,ledger:false,binding};threads.set(key,thread);
    while(threads.size>64){const old=[...threads.keys()].find(id=>id!==key&&id!==inFlight?.key);if(old===undefined)break;threads.delete(old);}
    return thread;
  }
  function live(request){return W.panes.get(request.key)===request.pane&&request.host.connected&&sameIdentity(request,identity(request.pane));}
  function push(thread,role,content){thread.messages.push({role,content});thread.messages=thread.messages.slice(-8);}
  function render(){
    const thread=current?threads.get(current.key):null;
    const follow=history.scrollHeight-history.clientHeight-history.scrollTop<=8,position=history.scrollTop;
    history.replaceChildren();
    if(thread?.messages.length){for(const message of thread.messages){const item=create('article'),title=create('strong',message.role==='user'?'你':'助手'),body=create('p',message.content);item.dataset.role=message.role;item.append(title,body);history.append(item);}}
    else history.append(create('p','可提问或分析当前窗口。默认不附带终端内容。'));
    history.scrollTop=follow?history.scrollHeight:position;
    question.disabled=!current;send.disabled=!current||!!inFlight;analyze.disabled=!current||!!inFlight;cancel.disabled=!inFlight||inFlight.cancelled;
    analyze.textContent=attachLedger.checked?'分析窗口与账本':'分析当前窗口';
    status.textContent=!current?'请先选择一个终端窗口。':inFlight&&inFlight.key!==current.key?'另一窗口的助手请求尚未结束，可以取消。':thread?.status||'';
  }
  function sync(){
    const next=selected();
    if(!current&&!next||current?.key===next?.key&&sameIdentity(current,next))return false;
    if(current&&threads.has(current.key))threads.get(current.key).draft=question.value;
    current=next;
    const thread=current?threadFor(current.key):null;question.value=thread?.draft||'';attach.checked=thread?.attach??autoContext;attachLedger.checked=thread?.ledger===true;render();history.scrollTop=history.scrollHeight;return true;
  }
  function context(pane){
    const buffer=pane.term.buffer.active,end=buffer.baseY+buffer.cursorY,start=Math.max(0,end-199),lines=[];
    for(let i=start;i<=end;i++){
      const line=buffer.getLine(i),text=line?.translateToString(true)||'';
      if(line?.isWrapped&&lines.length)lines[lines.length-1]+=text;else lines.push(text);
    }
    const text=lines.join('\n'),complete=buffer.getLine(start)?.isWrapped?text.replace(/^\S+\s*/u,''):text;
    return clip(complete,32768,true);
  }
  function clip(text,limit,tail=false){
    text=String(text||'');let value=tail?text.slice(-limit):text.slice(0,limit);
    if(text.length>limit){
      const boundary=tail?text.length-limit:limit;
      if(boundary>0&&!/\s/.test(text[boundary-1])&&!/\s/.test(text[boundary])){
        value=tail?value.replace(/^\S+\s*/u,''):value.replace(/\S+$/u,'');
      }
    }
    if(value&&/[\uDC00-\uDFFF]/.test(value[0]))value=value.slice(1);
    if(value&&/[\uD800-\uDBFF]/.test(value[value.length-1]))value=value.slice(0,-1);
    return value;
  }
  function evidenceContext(terminal,evidence){
    const limit=Math.min(contextLimit,8192),pages=[];
    const allowed=['negatives.md','trials.md','nodes.md','ideas.md','README.md'];
    const clean=text=>String(text||'').replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g,' ');
    const source=evidence?.project_source==='session-startup-directory';
    let path=source?clip(clean(evidence.project).replace(/[\r\n\t]/g,' '),96):'';
    for(const name of allowed){
      const page=source&&evidence?.ledger?.pages?.[name];
      if(page?.status==='ready'&&typeof page.text==='string'&&page.text){
        const raw=clean(page.text),complete=page.truncated?raw.replace(/\S+$/u,''):raw;
        pages.push({name,text:clip(complete,limit<2048?128:1024),mtime:Number.isFinite(page.mtime)?String(page.mtime):'未知'});
      }
      if(limit<2048&&pages.length===2)break;
    }
    let output=clip(clean(terminal),Math.floor(limit/2),true);
    const renderContext=()=>('[项目账本：只读片段，未重新核验模式或证据]\n'+(path?'终端启动目录：'+path+'\n':'')+(pages.length?'':'没有可读取的账本页面；不能据此判断已有试验。\n')+pages.map(page=>'[来源 .metarsi/wiki/'+page.name+'；修改时间 '+page.mtime+'；片段]\n'+page.text).join('\n\n')+(output?'\n\n[终端最近输出片段]\n'+output:''));
    let result=renderContext();
    while(result.length>limit){
      const longest=pages.reduce((best,page)=>page.text.length>(best?.text.length||0)?page:best,null);
      if(output.length>32&&output.length>=(longest?.text.length||0))output=clip(output,Math.floor(output.length/2),true);
      else if(longest&&longest.text.length>32)longest.text=clip(longest.text,Math.floor(longest.text.length/2));
      else if(pages.length)pages.pop();
      else if(path)path='';
      else output='';
      result=renderContext();
    }
    // Keep quoted material as plain text: native credential redaction must run
    // before any JSON encoding can escape quotes or backslashes in a secret.
    return result;
  }
  async function refreshPreferences(){
    if(!api.get_assistant_preferences)return;
    const read=++preferencesRead;
    try{const result=await api.get_assistant_preferences();if(read!==preferencesRead)return;autoContext=result?.preferences?.auto_context===true;const maximum=result?.preferences?.max_context_chars;contextLimit=Number.isInteger(maximum)&&maximum>=512&&maximum<=32768?maximum:8192;for(const thread of threads.values())if(!thread.attachTouched)thread.attach=autoContext;const thread=current&&threads.get(current.key);if(thread&&!thread.attachTouched)attach.checked=thread.attach;}catch{}
  }
  function cancelRequest(message='已取消当前请求。'){
    if(!inFlight||inFlight.cancelled)return;
    const request=inFlight;request.cancelled=true;nonce++;
    if(request.epoch===preferencesEpoch)request.thread.status=message+' 等待原请求结束。';
    try{Promise.resolve(api.cancel_assistant_chat?.(request.id)).catch(()=>{});}catch{}
    render();
  }
  async function submit(analysis=false){
    if(sync()){if(current)threads.get(current.key).status='窗口已切换或连接身份已变化，请检查当前草稿后重新发送。';render();return;}
    if(!current||inFlight)return;
    const ledgerRequested=attachLedger.checked;
    const text=analysis?'请分析当前窗口的内容，推荐适用的 mylib 技能，并说明是否值得登记 metaRSI 改进假设；区分已知证据与待验证建议。'+(ledgerRequested?' 结合附带的只读账本，引用相关试验或否决记录，避免重复失败方案；给出具体问题、可检验假设、独立验收方法及可证伪条件。账本缺失或片段不足时明确说明，不声称重新运行过 tick 或验证。':''):question.value.trim();
    const thread=threadFor(current.key);
    if(!text||text.length>4000){thread.status='请输入不超过 4000 字符的问题。';render();return;}
    const pane=current.pane,request={key:current.key,...identity(pane),thread,nonce:++nonce,epoch:preferencesEpoch,cancelled:false};
    request.id=globalThis.crypto?.randomUUID?.()||('chat-'+Date.now().toString(36)+'-'+request.nonce+'-'+Math.random().toString(36).slice(2));
    if(!live(request)){thread.status='当前窗口尚未连接，请连接后再发送。';render();return;}
    const attachContext=analysis||attach.checked;
    let output='';
    try{if(attachContext)output=context(pane);}catch{thread.status='当前窗口上下文暂不可读。';render();return;}
    const token=request.credential,safeText=redact(text,token);
    const previous=thread.messages.slice(-6).map(message=>({role:message.role,content:redact(message.content,token).slice(0,4000)}));
    if(!analysis){thread.draft='';question.value='';}
    thread.status=ledgerRequested?'正在读取当前项目账本…':attachContext?'正在分析当前窗口…':'正在请求助手（未附带终端输出）…';push(thread,'user',safeText);inFlight=request;render();
    try{
      if(ledgerRequested){
        let evidence=null;
        try{evidence=await window.SuperMonoAssistantEvidence?.read(pane,{force:true});}catch{}
        if(request.cancelled||request.nonce!==nonce||request.epoch!==preferencesEpoch||!sameIdentity(thread.binding,request)||!live(request))return;
        request.ledgerAvailable=evidence?.project_source==='session-startup-directory'&&evidence?.ledger?.enabled===true&&['README.md','negatives.md','trials.md','nodes.md','ideas.md'].some(name=>evidence.ledger.pages?.[name]?.status==='ready'&&evidence.ledger.pages[name].text);
        output=evidenceContext(output,evidence);
        thread.status=request.ledgerAvailable?'正在分析项目账本片段…':'项目账本不可读，正在结合可用资料分析…';render();
      }
      const payload={request_id:request.id,question:safeText,context:output,history:previous,attach_context:attachContext||ledgerRequested,redactions:token?[token]:[]};
      const result=await api.assistant_chat(payload);
      if(request.cancelled||request.nonce!==nonce||request.epoch!==preferencesEpoch||threads.get(request.key)!==thread||!sameIdentity(thread.binding,request)||!live(request))return;
      if(!result||typeof result.text!=='string'||result.text.length>16384)throw Error('Invalid assistant response');
      push(thread,'assistant',redact(result.text,token));
      const provider=typeof result.provider==='string'?result.provider.slice(0,64):'',model=typeof result.model==='string'?result.model.slice(0,128):'';
      thread.status='已回复'+(provider?' · '+provider:'')+(model?' / '+model:'')+(Number.isInteger(result.context_used_chars)?' · 上下文 '+result.context_used_chars+' 字符':'')+(result.context_truncated?'（已截断）':'')+(ledgerRequested?(request.ledgerAvailable?' · 项目账本为未核验片段':' · 项目账本不可读'):'');
    }catch{
      if(!request.cancelled&&request.nonce===nonce&&request.epoch===preferencesEpoch&&live(request))thread.status='助手暂时无法响应，请检查模型配置或稍后重试。';
    }finally{
      if(inFlight===request)inFlight=null;
      if(request.epoch===preferencesEpoch&&sameIdentity(thread.binding,request)){if(request.cancelled)thread.status='请求已取消，未采用返回内容。';else if(!live(request))thread.status='窗口连接已变化，已丢弃旧请求结果。';}
      sync();render();
    }
  }
  question.addEventListener('input',()=>{sync();if(current)threadFor(current.key).draft=question.value;});
  attach.addEventListener('change',()=>{if(sync())return;if(current){const thread=threadFor(current.key);thread.attach=attach.checked;thread.attachTouched=true;}});
  attachLedger.addEventListener('change',()=>{if(sync())return;if(current){threadFor(current.key).ledger=attachLedger.checked;render();}});
  form.addEventListener('submit',event=>{event.preventDefault();submit();});analyze.onclick=()=>submit(true);cancel.onclick=()=>cancelRequest();
  function preferencesChanged(){
    preferencesEpoch++;cancelRequest('设置已更新，正在取消原请求。');
    for(const thread of threads.values()){thread.messages=[];thread.status='模型设置已更新，已开始新对话。';thread.attachTouched=false;thread.attach=false;thread.ledger=false;}
    attach.checked=false;attachLedger.checked=false;render();refreshPreferences();
  }
  addEventListener('mylib-preferences-changed',preferencesChanged);
  const timer=setInterval(()=>{if(panel.hidden)return;sync();if(inFlight&&!live(inFlight)){const request=inFlight,pane=W.panes.get(request.key);if(pane)threadFor(request.key,pane);else{request.thread.draft=redact(request.thread.draft,request.thread.binding?.credential||'');request.thread.messages=[];request.thread.binding=null;}cancelRequest('窗口连接已变化，正在取消原请求。');}},300);
  addEventListener('beforeunload',()=>{clearInterval(timer);cancelRequest();nonce++;removeEventListener('mylib-preferences-changed',preferencesChanged);},{once:true});
  sync();render();refreshPreferences();
})();
