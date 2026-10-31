/* Selected-host local worktree sidecar. No terminal input or MCP. */
(function installMylib(){
  'use strict';
  if(document.getElementById('mylib-toggle')||!window.smaWorkspace)return;
  if(!window.pywebview?.api?.recommend_skills){addEventListener('pywebviewready',installMylib,{once:true});return;}
  const libraryContext=(pane=window.smaWorkspace.activePane)=>{
    if(!pane)throw Error('请先选择一个终端窗口。');
    if(pane.host.id==='local')return {local:true,pane};
    const F=window.SuperMonoFileWorkspaces,target=F?.captureTarget?.();
    if(!target||target.pane!==pane)throw Error('host_unavailable');
    return {local:false,pane,target};
  };
  const libraryCurrent=ctx=>!!ctx&&window.smaWorkspace.activePane===ctx.pane&&(ctx.local||window.SuperMonoFileWorkspaces?.isCurrent(ctx.target)===true);
  async function libraryCall(method,argument,ctx=libraryContext()){
    const api=window.pywebview.api;
    if(ctx.local)return argument===undefined?api[method]():api[method](argument);
    const F=window.SuperMonoFileWorkspaces,target=ctx.target;
    if(!F.isCurrent(target))throw Error('target_changed');
    if(method==='get_assistant_preferences'){
      const [preferences,library]=await Promise.all([api.get_assistant_preferences(),F.request(target,'library.get',{})]);
      return {...preferences,preferences:{...preferences.preferences,library_root:library.mylib_root,native_library_root:preferences.preferences.library_root},library_revision:library.revision};
    }
    if(method==='save_assistant_preferences'){
      const [preferences,current]=await Promise.all([api.get_assistant_preferences(),F.request(target,'library.get',{})]);
      const result=await F.request(target,'library.set',{mylib_root:argument.library_root,expected_revision:current.revision});
      return {...preferences,preferences:{...preferences.preferences,library_root:result.mylib_root},library_revision:result.revision};
    }
    const actions={library_status:'library.status',pull_library:'library.pull',commit_library_preview:'library.commit_preview',
      run_library_evolution:'library.run_evolution',apply_library_evolution:'library.apply_evolution'};
    if(method==='recommend_skills')return F.request(target,'library.recommend',{context:argument});
    if(method==='read_assistant_skill')return F.request(target,'library.read_skill',{source:argument.source});
    if(method==='commit_library')return F.request(target,'library.commit',argument);
    if(actions[method])return F.request(target,actions[method],method==='library_status'||method==='pull_library'||method==='commit_library_preview'?{}:argument);
    throw Error('invalid_request');
  }
  window.SuperMonoMylib={recommend:text=>libraryCall('recommend_skills',text),read:request=>libraryCall('read_assistant_skill',request),context:libraryContext};
  const style=document.createElement('style');
  style.textContent=`#mylib-panel{position:fixed;right:0;top:44px;bottom:0;width:350px;z-index:20;background:#191919;border-left:1px solid #444;padding:14px;overflow:auto;overflow-wrap:anywhere}body.mylib-open #app{margin-right:350px}#mylib-panel h2{font-size:15px}#mylib-panel p{font-size:12px;line-height:1.6;color:#bbb}#mylib-panel article{border:1px solid #444;border-radius:6px;padding:10px;margin:10px 0}#mylib-panel code{font-size:11px}#mylib-panel button{border:1px solid #444;margin:3px}#mylib-context{max-height:140px;overflow:auto;white-space:pre-wrap;font-size:11px;color:#aaa}.mylib-actions{display:flex;flex-wrap:wrap;gap:3px}.mylib-status{color:#aaa!important;font-size:11px!important}.mylib-change-list{max-height:220px;overflow:auto;font-size:11px}.mylib-candidate{border-color:#668!important}@media(max-width:1050px){body.mylib-open #app{margin-right:0}#mylib-panel{width:min(350px,100vw)}}`;
  style.textContent+='#mylib-panel{top:var(--workspace-titlebar-height,44px)}#mylib-library-source{display:flex;align-items:flex-start;gap:7px}#mylib-library-source::before{content:"";flex:0 0 8px;width:8px;height:8px;margin-top:5px;border-radius:50%;background:#666}#mylib-library-source[data-accessible="true"]::before{background:#65c466;box-shadow:0 0 5px #65c46666}';
  document.head.append(style);
  const toggle=document.createElement('button');toggle.id='mylib-toggle';toggle.textContent='mylib 助理';toggle.setAttribute('aria-expanded','false');
  document.querySelector('.titlebar .header-spacer')?.before(toggle);
  const panel=document.createElement('aside');panel.id='mylib-panel';panel.hidden=true;panel.setAttribute('aria-label','mylib 助理');
  panel.innerHTML='<h2>mylib 助理</h2><p id="mylib-target"></p><div class="mylib-actions"><button id="mylib-directory">目录设置</button><button id="mylib-pull">拉取</button><button id="mylib-commit">提交</button><button id="mylib-evolve">进化评审</button><button id="mylib-refresh">重新分析</button><button id="mylib-pause">暂停感知</button></div><p id="mylib-library-source">使用本地 mylib。</p><p id="mylib-sync-status" class="mylib-status" role="status"></p><details><summary>本次上下文</summary><pre id="mylib-context"></pre></details><div id="mylib-results"></div><details><summary>metaRSI 进化</summary><p>当前窗口信号只用于本地评审。候选必须显式采纳，提交必须再次确认，不自动修改或批准规则。</p></details>';
  const evolution=document.createElement('section');evolution.id='mylib-evolution';evolution.setAttribute('aria-label','metaRSI 进化建议');panel.append(evolution);
  const project=document.createElement('details'),projectHeading=document.createElement('summary'),projectNote=document.createElement('p'),ledger=document.createElement('pre');project.id='mylib-project';projectHeading.textContent='目标项目与证据账本';ledger.style.cssText='white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px';project.append(projectHeading,projectNote,ledger);panel.append(project);
  document.body.append(panel);
  const preview=document.createElement('dialog');preview.id='mylib-skill-preview';preview.setAttribute('aria-label','本地技能或候选正文');
  preview.style.cssText='width:min(760px,90vw);max-height:85dvh;overflow:auto';
  const previewTitle=document.createElement('h2'),previewSource=document.createElement('p'),previewText=document.createElement('pre'),previewApply=document.createElement('button'),previewClose=document.createElement('button');
  previewText.style.cssText='white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px';previewApply.textContent='采纳候选';previewApply.hidden=true;previewClose.textContent='关闭';previewClose.onclick=()=>preview.close();preview.append(previewTitle,previewSource,previewText,previewApply,previewClose);document.body.append(preview);
  let previewRevision=0;
  preview.addEventListener('close',()=>{previewRevision++;previewApply.hidden=true;});
  const directoryDialog=document.createElement('dialog');directoryDialog.id='mylib-directory-dialog';
  directoryDialog.innerHTML='<form id="mylib-directory-form"><h2>mylib 目录设置</h2><p>默认 D:/aicoding/mylib。目录在当前窗口所属电脑上读取，不是手机目录；可修改为该电脑的其他本地磁盘位置。</p><label>目标电脑的本地目录<input id="mylib-directory-root" type="text" maxlength="4096" required autocomplete="off" spellcheck="false"></label><p id="mylib-directory-status" role="status"></p><div class="dialog-actions"><button id="mylib-directory-cancel" type="button">取消</button><button type="submit">保存并应用</button></div></form>';
  document.body.append(directoryDialog);
  const commitDialog=document.createElement('dialog');commitDialog.id='mylib-commit-dialog';
  commitDialog.innerHTML='<form id="mylib-commit-form"><h2>提交并推送到 GitHub</h2><p>先检查变更。确认后才会暂存、提交并推送，敏感文件和本地状态不会加入。</p><div id="mylib-commit-files" class="mylib-change-list"></div><pre id="mylib-commit-stat"></pre><label>提交说明<input id="mylib-commit-message" type="text" maxlength="200" value="Update local skills" required></label><p id="mylib-commit-status" role="status"></p><div class="dialog-actions"><button id="mylib-commit-cancel" type="button">取消</button><button id="mylib-commit-confirm" type="submit">确认提交并推送</button></div></form>';
  document.body.append(commitDialog);
  let syncBusy=false,currentCommit=null,currentCandidate=null,directoryContext=null;
  const syncStatus=panel.querySelector('#mylib-sync-status');
  const syncError=error=>{const code=String(error?.message||error||'unknown'),labels={dirty_worktree:'工作区有未提交修改，请先提交或清理后再拉取。',non_fast_forward:'上游历史已分叉，不能执行 fast-forward。',changes_changed:'变更清单已变化，请重新预览后再提交。',source_drift:'技能文件已变化，请重新进行进化评审。',unknown_suggestion:'候选不存在或已失效，请重新评审。',no_changes:'没有可提交的变更。',source_unavailable:'目录或 Git 工作副本不可用。',directory_unavailable:'目录不存在或不可访问，请检查路径和权限。',invalid_path:'仓库包含不支持的路径，请检查工作副本。',host_unavailable:'请先启用当前窗口所属电脑的文件助手。',target_changed:'窗口已切换或连接已变化，请检查原窗口状态后重试。',sync_busy:'另一个 mylib 操作正在执行，请稍后重试。',cancelled:'操作已取消。',git_failed:'Git 操作失败，请检查网络、认证和仓库配置。',invalid_config:'目录或配置无效。'};return labels[code]||'操作未完成：'+code;};
  function renderLibrarySource({root='',accessible=false,detail=''},ctx=null){
    const source=panel.querySelector('#mylib-library-source');source.dataset.root=root;source.dataset.accessible=String(accessible===true);source.title=accessible?'目标电脑的目录存在且可读取':'目标电脑的目录未验证或不可访问';source.textContent=(ctx?.local===false?ctx.target.machine+' 电脑':'本机')+' mylib · '+(root||'目录未配置')+(detail?' · '+detail:'');
  }
  async function refreshLibraryStatus(ctx=null){
    try{ctx=ctx||libraryContext();const status=await libraryCall('library_status',undefined,ctx),detail=status.accessible?(status.git?(status.revision||'').slice(0,12)+(status.changes.length?' · '+status.changes.length+' 项待提交':' · 工作区干净'):'非 Git 目录'):'目录不可访问';renderLibrarySource({root:status.root,accessible:status.accessible,detail},ctx);return status;}catch(error){const source=panel.querySelector('#mylib-library-source');renderLibrarySource({root:source.dataset.root,accessible:false,detail:'目录状态读取失败'},ctx);syncStatus.textContent=syncError(error);return null;}
  }
  if(window.pywebview.api.get_assistant_preferences&&window.pywebview.api.save_assistant_preferences){
    const settings=document.createElement('dialog');settings.id='mylib-settings';
    settings.innerHTML='<form id="mylib-settings-form"><h2>助手模型配置</h2><p>保存配置不会发起模型调用。发送问题时才调用所选入口。</p><label>模型入口<select id="mylib-provider"><option value="unconfigured">稍后配置</option><option value="codex">本机 Codex 登录</option><option value="api">已有模型 API</option></select></label><label>模型名称<input id="mylib-model" type="text" maxlength="128" autocomplete="off"></label><label id="mylib-endpoint-label">API 地址<input id="mylib-endpoint" type="url" maxlength="2048" autocomplete="off" placeholder="https://…/v1"></label><label id="mylib-key-label">API 密钥（仅本次运行保留）<input id="mylib-api-key" type="password" maxlength="8192" autocomplete="off" placeholder="本机无鉴权服务可留空"></label><label class="check"><input id="mylib-auto-context" type="checkbox">对话时自动附带当前窗口上下文</label><label>上下文字符上限<input id="mylib-context-limit" type="number" min="512" max="32768" step="1"></label><p id="mylib-settings-status" role="status"></p><div class="dialog-actions"><button id="mylib-settings-cancel" type="button">取消</button><button id="mylib-settings-save" type="submit">保存配置</button></div></form>';
    const extra=document.createElement('style');extra.textContent='#mylib-settings{max-height:85dvh;overflow:auto}#mylib-settings input:not([type=checkbox]),#mylib-settings select{width:100%;max-width:100%}#mylib-settings label{overflow-wrap:anywhere}';document.head.append(extra);document.body.append(settings);
    const $=id=>settings.querySelector('#'+id),button=document.createElement('button');button.id='mylib-configure';button.textContent='模型配置';panel.querySelector('#mylib-pause').after(button);
    let loading=false;
    const refreshFields=()=>{const api=$('mylib-provider').value==='api',key=api&&!!window.pywebview.api.set_assistant_api_key;$('mylib-endpoint-label').hidden=!api;$('mylib-endpoint').disabled=!api;$('mylib-key-label').hidden=!key;$('mylib-api-key').disabled=!key;$('mylib-model').placeholder=$('mylib-provider').value==='codex'?'留空使用登录配置':'填写模型标识';};
    $('mylib-provider').onchange=refreshFields;
    button.onclick=async()=>{if(loading)return;loading=true;button.disabled=true;try{const result=await pywebview.api.get_assistant_preferences(),p=result.preferences;$('mylib-provider').value=p.provider;$('mylib-model').value=p.model;$('mylib-endpoint').value=p.endpoint;$('mylib-api-key').value='';$('mylib-api-key').placeholder=result.api_key_present?'已设置；留空保持，本次运行有效':'本机无鉴权服务可留空';$('mylib-auto-context').checked=p.auto_context;$('mylib-context-limit').value=p.max_context_chars;settings.dataset.libraryRoot=p.native_library_root||p.library_root;refreshFields();$('mylib-settings-status').textContent=result.status==='invalid'?'原配置暂不可读，已显示默认值；保存前请检查。':'';settings.showModal();}catch{panel.querySelector('#mylib-results').textContent='模型配置暂不可读。';}finally{loading=false;button.disabled=false;}};
    $('mylib-settings-cancel').onclick=()=>{ $('mylib-api-key').value='';settings.close();};settings.addEventListener('cancel',()=>{$('mylib-api-key').value='';});
    $('mylib-settings-form').onsubmit=async event=>{event.preventDefault();const save=$('mylib-settings-save');if(save.disabled)return;save.disabled=true;let saved=false;try{await pywebview.api.save_assistant_preferences({provider:$('mylib-provider').value,model:$('mylib-model').value,endpoint:$('mylib-provider').value==='api'?$('mylib-endpoint').value:'',auto_context:$('mylib-auto-context').checked,max_context_chars:Number($('mylib-context-limit').value),library_root:settings.dataset.libraryRoot});saved=true;dispatchEvent(new Event('mylib-preferences-changed'));if($('mylib-provider').value==='api'&&$('mylib-api-key').value&&pywebview.api.set_assistant_api_key)await pywebview.api.set_assistant_api_key($('mylib-api-key').value);$('mylib-settings-status').textContent='配置已保存；发送问题时才调用模型。';}catch{$('mylib-settings-status').textContent=saved?'入口已保存，但密钥未接受，请检查后重新输入。':'配置未保存，请检查模型名称、地址和上下文上限。';}finally{$('mylib-api-key').value='';save.disabled=false;}};
  }
  async function withSync(action,label,ctx=null){if(syncBusy)return;syncBusy=true;panel.dataset.syncBusy='true';syncStatus.textContent=label;try{ctx=ctx||libraryContext();const result=await action(ctx);if(!libraryCurrent(ctx))throw Error('target_changed');return result;}catch(error){syncStatus.textContent=syncError(error);return null;}finally{syncBusy=false;panel.dataset.syncBusy='false';}}
  panel.querySelector('#mylib-directory').onclick=async()=>{try{directoryContext=libraryContext();const result=await libraryCall('get_assistant_preferences',undefined,directoryContext),root=directoryDialog.querySelector('#mylib-directory-root');root.value=result.preferences.library_root;directoryDialog.querySelector('#mylib-directory-status').textContent='';directoryDialog.showModal();}catch(error){syncStatus.textContent=syncError(error);}};
  directoryDialog.querySelector('#mylib-directory-cancel').onclick=()=>directoryDialog.close();
  directoryDialog.querySelector('#mylib-directory-form').onsubmit=event=>{event.preventDefault();withSync(async ctx=>{
    const status=directoryDialog.querySelector('#mylib-directory-status');
    try{const result=await libraryCall('get_assistant_preferences',undefined,ctx),preferences={...result.preferences,library_root:directoryDialog.querySelector('#mylib-directory-root').value};await libraryCall('save_assistant_preferences',preferences,ctx);}catch(error){status.textContent=syncError(error);return;}
    const library=await refreshLibraryStatus(ctx);if(!library?.accessible){status.textContent=syncError(Error('directory_unavailable'));return;}
    directoryDialog.close();await analyze(true);
  },'正在检查并切换目录…',directoryContext);};
  panel.querySelector('#mylib-pull').onclick=()=>withSync(async ctx=>{const result=await libraryCall('pull_library',undefined,ctx);if(!result)return;syncStatus.textContent=result.changed?'已拉取新版本 '+(result.revision||'').slice(0,12):'已经是最新版本。';await refreshLibraryStatus(ctx);await analyze(true);},'正在从 GitHub 拉取…');
  panel.querySelector('#mylib-commit').onclick=()=>withSync(async ctx=>{currentCommit=await libraryCall('commit_library_preview',undefined,ctx);if(!currentCommit?.can_commit){syncStatus.textContent='没有可提交的变更。';return;}currentCommit.context=ctx;const files=commitDialog.querySelector('#mylib-commit-files');files.replaceChildren();for(const item of currentCommit.items){const row=document.createElement('p');row.textContent=(item.excluded?'排除：':'包含：')+item.code+' '+item.path;files.append(row);}commitDialog.querySelector('#mylib-commit-stat').textContent=currentCommit.stat||'仅包含新增或删除文件。';commitDialog.querySelector('#mylib-commit-status').textContent=currentCommit.excluded_count?'已自动排除 '+currentCommit.excluded_count+' 项本地状态或敏感文件。':'';commitDialog.showModal();},'正在检查变更…');
  commitDialog.querySelector('#mylib-commit-cancel').onclick=()=>commitDialog.close();
  commitDialog.querySelector('#mylib-commit-form').onsubmit=event=>{event.preventDefault();if(!currentCommit)return;withSync(async ctx=>{const result=await libraryCall('commit_library',{fingerprint:currentCommit.fingerprint,message:commitDialog.querySelector('#mylib-commit-message').value},ctx);if(!result)return;commitDialog.close();currentCommit=null;syncStatus.textContent='已提交并推送 '+(result.revision||'').slice(0,12);await refreshLibraryStatus(ctx);await analyze(true);},'正在提交并推送…',currentCommit.context);};
  panel.querySelector('#mylib-evolve').onclick=()=>withSync(async ctx=>{const c=capture();if(!c){syncStatus.textContent='请先选择一个终端窗口。';return;}const result=await libraryCall('run_library_evolution',{text:c.text,target:c.target},ctx);if(!result)return;evolution.replaceChildren();syncStatus.textContent='评审完成：'+result.count+' 个候选；采纳后仍需手动提交。';if(!result.items.length){const empty=document.createElement('p');empty.textContent='当前没有新的结构化候选。';evolution.append(empty);}for(const item of result.items){item.context=ctx;const article=document.createElement('article');article.className='mylib-candidate';const title=document.createElement('strong'),problem=document.createElement('p'),hypothesis=document.createElement('p'),falsifier=document.createElement('p'),validation=document.createElement('p'),previewButton=document.createElement('button'),applyButton=document.createElement('button');title.textContent=item.title;problem.textContent=item.problem;hypothesis.textContent=item.hypothesis;falsifier.textContent='可证伪条件：'+item.falsifier;validation.textContent='独立校验：'+item.validation.scope+' · '+(item.validation.passed?'结构通过':'未通过')+'；语义效果未验证。';previewButton.textContent='预览候选';previewButton.onclick=()=>{currentCandidate=item;previewTitle.textContent=item.title;previewSource.textContent=item.source.path+' · '+item.source.sha256.slice(0,12);previewText.textContent=item.candidate_text;previewApply.hidden=false;preview.showModal();};applyButton.textContent='采纳候选';applyButton.onclick=()=>withSync(async ctx=>{const applied=await libraryCall('apply_library_evolution',{suggestion_id:item.suggestion_id},ctx);if(!applied)return;syncStatus.textContent='已采纳到目标电脑的工作副本；请点击“提交”预览并推送。';await refreshLibraryStatus(ctx);},'正在采纳候选…',item.context);article.append(title,problem,hypothesis,falsifier,validation,previewButton,applyButton);evolution.append(article);}},'正在进行本地进化评审…');
  previewApply.onclick=()=>{if(!currentCandidate)return;preview.close();withSync(async ctx=>{const applied=await libraryCall('apply_library_evolution',{suggestion_id:currentCandidate.suggestion_id},ctx);if(!applied)return;syncStatus.textContent='已采纳到目标电脑的工作副本；请点击“提交”预览并推送。';await refreshLibraryStatus(ctx);},'正在采纳候选…',currentCandidate.context);};
  let paused=false,revision=0,last='',lastIdentity=null,lastReadiness='',analysisState=null,timer,libraryStatusIdentity='';
  const content=panel.querySelector('#mylib-results');
  function capture(){
    const w=window.smaWorkspace,p=w.activePane;if(!p)return null;
    const b=p.term.buffer.active,lines=[];
    const end=b.baseY+b.cursorY;
    for(let i=Math.max(0,end-199);i<=end;i++){const line=b.getLine(i),continues=i<end&&b.getLine(i+1)?.isWrapped,text=line?.translateToString(!continues)||'';if(line?.isWrapped&&lines.length)lines[lines.length-1]+=text;else lines.push(text);}
    return {pane:p,generation:p.generation,id:w.activeSession,identity:evidenceIdentity(p),readiness:[p.host.connected,p.host.ws?.readyState,p.ready,p.host.phase,p.host.capabilities.assistant_context===true].join('|'),text:lines.join('\n').slice(-32768),target:(p.host.name||p.host.id)+' / '+(p.title?.textContent||p.raw)};
  }
  let contextSequence=0;
  const evidenceCache=new WeakMap(),evidencePending=new Set(),evidencePages=['README.md','negatives.md','trials.md','nodes.md','ideas.md'];
  const bounded=(value,limit)=>{if(typeof value!=='string')return '';let text=value.slice(0,limit);const last=text.charCodeAt(text.length-1);if(last>=0xd800&&last<=0xdbff)text=text.slice(0,-1);return text;};
  const evidenceIdentity=pane=>({pane,host:pane.host,generation:pane.host.generation,paneGeneration:pane.generation,ws:pane.host.ws,token:typeof pane.host.config?.token==='string'?pane.host.config.token:'',session:pane.raw});
  const sameEvidenceIdentity=(a,b)=>!!a&&!!b&&a.pane===b.pane&&a.host===b.host&&a.generation===b.generation&&a.paneGeneration===b.paneGeneration&&a.ws===b.ws&&a.token===b.token&&a.session===b.session;
  function currentEvidence(identity){
    try{const w=window.smaWorkspace;return !!identity&&w.panes.get(identity.pane.sid)===identity.pane&&w.connections.get(identity.host.id)===identity.host&&identity.host.connected&&identity.ws?.readyState===WebSocket.OPEN&&typeof identity.session==='string'&&identity.session.length>0&&identity.session.length<=256&&sameEvidenceIdentity(identity,evidenceIdentity(identity.pane));}catch{return false;}
  }
  function evidenceFailure(note){return {ok:false,available:false,captured_at:Date.now(),project:null,project_truncated:false,project_source:'',project_status:'unavailable',note,inventory:{available:false,status:'unavailable',indexed:0,skill_names:[],truncated:false},ledger:{enabled:false,status:'unavailable',pages:{}}};}
  const copyEvidence=value=>JSON.parse(JSON.stringify(value));
  function normalizeEvidence(value){
    if(!value||typeof value!=='object'||Array.isArray(value)||value.project_source!=='session-startup-directory'||!value.ledger||typeof value.ledger!=='object'||Array.isArray(value.ledger)||typeof value.ledger.enabled!=='boolean')throw Error('Invalid evidence');
    const pages={};
    for(const name of evidencePages){
      const page=value.ledger.pages?.[name];if(page===undefined)continue;
      if(typeof page==='string'){const text=bounded(page,8192);pages[name]={status:'ready',text,truncated:text.length<page.length};continue;}
      if(!page||typeof page!=='object'||Array.isArray(page))continue;
      const text=bounded(page.text,8192),status=['ready','missing','unavailable'].includes(page.status)?page.status:'unavailable';
      const included=status==='ready'?text:'';
      pages[name]={status,text:included,truncated:page.truncated===true||typeof page.text==='string'&&included.length<page.text.length};
      if(typeof page.mtime==='number'&&Number.isFinite(page.mtime)&&page.mtime>=0&&page.mtime<=253402300799)pages[name].mtime=page.mtime;
    }
    const inventory=value.inventory&&typeof value.inventory==='object'&&!Array.isArray(value.inventory)?value.inventory:{},names=Array.isArray(inventory.skill_names)?inventory.skill_names.filter(name=>typeof name==='string').slice(0,256).map(name=>bounded(name,100)):[];
    const path=typeof value.project==='string'?bounded(value.project,4096):null;
    return {ok:true,available:true,captured_at:Date.now(),project:path,project_truncated:value.project_truncated===true||typeof value.project==='string'&&path.length<value.project.length,project_source:'session-startup-directory',project_status:['ready','missing','unavailable'].includes(value.project_status)?value.project_status:'unavailable',note:bounded(value.note,512),inventory:{available:inventory.available===true,status:['ready','empty','missing','unavailable'].includes(inventory.status)?inventory.status:'unavailable',indexed:Number.isSafeInteger(inventory.indexed)&&inventory.indexed>=0&&inventory.indexed<=100000?inventory.indexed:names.length,skill_names:names,truncated:inventory.truncated===true||Array.isArray(inventory.skill_names)&&(inventory.skill_names.length>256||inventory.skill_names.some(name=>typeof name==='string'&&bounded(name,100).length<name.length))},ledger:{enabled:value.ledger.enabled,status:['ready','partial','unavailable','not_enabled'].includes(value.ledger.status)?value.ledger.status:'unavailable',pages}};
  }
  function readEvidence(pane,{force=false}={}){
    let identity;try{identity=evidenceIdentity(pane);}catch{return Promise.resolve(evidenceFailure('未获得有效终端窗口。'));}
    const deliver=value=>currentEvidence(identity)?copyEvidence(value):evidenceFailure('窗口连接身份已改变，请重新读取账本。');
    let entry=evidenceCache.get(pane);
    if(entry&&!sameEvidenceIdentity(entry.identity,identity)){entry.pending?.cancel('窗口连接身份已改变，请重新读取账本。');evidenceCache.delete(pane);entry=null;}
    if(!currentEvidence(identity))return Promise.resolve(evidenceFailure('目标窗口未连接或已经关闭。'));
    if(!identity.host.capabilities.assistant_context)return Promise.resolve(evidenceFailure('目标后台尚不支持项目账本读取。'));
    if(entry?.pending)return entry.pending.promise.then(deliver);
    if(!force&&entry?.value&&performance.now()<entry.expires)return Promise.resolve().then(()=>deliver(entry.value));
    entry={identity,value:null,expires:0,pending:null};evidenceCache.set(pane,entry);
    const request='assistant-'+(++contextSequence)+'-'+Date.now().toString(36),ws=identity.ws,deadline=performance.now()+8000,control={promise:null,cancel:null};
    control.promise=new Promise(resolve=>{
      let settled=false,timeout;
      const finish=value=>{if(settled)return;settled=true;clearTimeout(timeout);ws.removeEventListener('message',receive);ws.removeEventListener('close',closed);ws.removeEventListener('error',closed);evidencePending.delete(control);entry.pending=null;if(performance.now()>=deadline)value=evidenceFailure('项目账本读取超时。');if(!currentEvidence(identity))value=evidenceFailure('窗口连接身份已改变，请重新读取账本。');else if(value.available&&evidenceCache.get(pane)===entry){entry.value=value;entry.expires=performance.now()+15000;}resolve(value);};
      control.cancel=note=>finish(evidenceFailure(note));
      const closed=()=>finish(evidenceFailure('目标连接已断开。'));
      const receive=event=>{
        if(!(event.data instanceof ArrayBuffer))return;
        const bytes=new Uint8Array(event.data);if(bytes.length<5||bytes[4]!==22)return;
        if(bytes.length-5>256*1024){finish(evidenceFailure('项目账本响应超过读取上限。'));return;}
        try{const frame=TerminalClient.unpack(bytes);if(frame.op!==22)return;const response=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(frame.payload));if(!response||typeof response!=='object'||Array.isArray(response))throw Error('Invalid evidence');if(response.request!==request)return;if(response.ok!==true){finish(evidenceFailure('项目账本暂不可用。'));return;}finish(normalizeEvidence(response));}catch{finish(evidenceFailure('项目账本响应无效，请稍后重试。'));}
      };
      ws.addEventListener('message',receive);ws.addEventListener('close',closed);ws.addEventListener('error',closed);timeout=setTimeout(()=>finish(evidenceFailure('项目账本读取超时。')),8000);evidencePending.add(control);
      try{const frame=TerminalClient.pack(22,new TextEncoder().encode(JSON.stringify({request,session:identity.session})));ws.send(frame);window.smaWorkspace.metrics.transmittedBytes+=identity.host.meter.sent(frame);}catch{closed();}
    });
    if(evidencePending.has(control))entry.pending=control;
    return control.promise.then(deliver);
  }
  window.SuperMonoAssistantEvidence={read:readEvidence};
  function recordMatches(idea,context){
    const faultLines=Array.isArray(idea.evidence)?idea.evidence.filter(line=>typeof line==='string').slice(0,3).map(line=>line.slice(0,240)):[],terms=new Set(),stop=new Set(['error','exception','traceback','failed','failure','running','task','test','tests','file','line','失败','异常','故障','记录','建议','验证']);
    for(const line of faultLines){for(const word of line.toLowerCase().match(/[a-z][a-z0-9_-]{2,}/g)||[])if(!stop.has(word))terms.add(word);for(const phrase of line.match(/[\u4e00-\u9fff]+/g)||[])for(let i=0;i<phrase.length-1;i++){const word=phrase.slice(i,i+2);if(!stop.has(word))terms.add(word);}}
    const words=[...terms].slice(0,64),matches=[];
    for(const name of ['negatives.md','trials.md','nodes.md','ideas.md','README.md']){
      const page=context.ledger?.pages?.[name];if(page?.status!=='ready'||!page.text)continue;
      const lines=page.text.split('\n');let best=null,heading='';
      for(let i=0;i<lines.length;i++){if(/^#{1,6}\s/.test(lines[i]))heading=lines[i];const lower=lines[i].toLowerCase(),hit=words.filter(word=>lower.includes(word));if(!hit.length||hit.length<2&&!hit.some(word=>word.length>=6))continue;if(!best||hit.length>best.score)best={score:hit.length,text:[heading,...lines.slice(Math.max(0,i-1),i+2)].filter((line,index,all)=>line&&all.indexOf(line)===index).join('\n').slice(0,600)};}
      if(best){const category=name==='negatives.md'?'否决记录':name==='trials.md'?'试验记录':name==='ideas.md'?'想法记录':name==='nodes.md'&&/shadow|影子/i.test(best.text)?'影子规则节点':name==='nodes.md'?'证据节点':'账本入口';matches.push({source:'.metarsi/wiki/'+name,category,text:best.text});}
    }
    return {faultLines,matches:matches.slice(0,3)};
  }
  async function analyze(force=false){
    if(panel.hidden||paused)return;
    const c=capture();if(!c){content.textContent='请选择一个终端窗口。';evolution.replaceChildren();projectNote.textContent='未选择终端窗口。';ledger.textContent='';last='';lastIdentity=null;lastReadiness='';analysisState=null;return;}
    const signature=c.id+'|'+c.generation+'|'+c.text,sameContext=signature===last&&sameEvidenceIdentity(lastIdentity,c.identity),sameState=sameContext&&c.readiness===lastReadiness;
    if(sameContext&&analysisState?.pending&&analysisState.revision===revision)return;
    if(!force&&sameState&&(!analysisState?.failed||performance.now()<analysisState.retryAt))return;
    const run={pending:true,failed:false,failures:sameState?analysisState?.failures||0:0,retryAt:0};analysisState=run;
    last=signature;lastIdentity=c.identity;lastReadiness=c.readiness;const request=++revision;run.revision=request;
    const retry=()=>{run.failed=true;run.failures++;run.retryAt=performance.now()+Math.min(30000,4000*2**Math.min(run.failures-1,3));};
    panel.querySelector('#mylib-target').textContent=c.target;
    panel.querySelector('#mylib-context').textContent=c.text;
    content.textContent='正在读取技能目录并匹配…';
    evolution.textContent='';
    projectNote.textContent='正在查询目标电脑的启动目录…';ledger.textContent='';
    try{
      const api=window.pywebview?.api;if(!api?.recommend_skills)throw Error('本机助理接口尚未就绪，请稍后重新分析。');
      const libraryBinding=libraryContext(c.pane),statusKey=c.id+'|'+c.generation;
      const statusRefresh=force||statusKey!==libraryStatusIdentity?refreshLibraryStatus(libraryBinding):null;
      libraryStatusIdentity=statusKey;
      const [result,context]=await Promise.all([libraryCall('recommend_skills',c.text,libraryBinding),readEvidence(c.pane,{force}),statusRefresh]),w=window.smaWorkspace;
      if(request!==revision||panel.hidden||w.activePane!==c.pane||w.panes.get(c.pane.sid)!==c.pane||!sameEvidenceIdentity(c.identity,evidenceIdentity(c.pane))){run.failed=true;run.retryAt=0;return;}
      if(!context.available&&currentEvidence(c.identity)&&c.identity.host.capabilities.assistant_context)retry();else run.failures=0;
      content.replaceChildren();
      const library=result.library||{},source=panel.querySelector('#mylib-library-source'),root=library.root||'';
      renderLibrarySource({root,accessible:source.dataset.root===root&&source.dataset.accessible==='true',detail:'匹配 '+result.indexed+' 项'},libraryBinding);
      projectNote.textContent=context.project?'终端启动目录：'+context.project+'（不跟踪 shell 内的 cd）'+(context.ledger?.enabled?' · 已关联证据账本。':context.ledger?.status==='not_enabled'?' · 此目录未启用 metaRSI。':' · 项目证据暂不可读。'):context.note||'未获得可信项目目录。';
      ledger.textContent=Object.entries(context.ledger?.pages||{}).map(([name,page])=>name+' ['+({ready:'已读取',missing:'无记录',unavailable:'无法读取'}[page.status]||'未知')+']\n'+(typeof page==='string'?page:page.text||'')+(page.truncated?'\n[内容已截断]':'')).join('\n\n');
      for(const idea of result.evolution||[]){const article=document.createElement('article'),heading=document.createElement('strong'),evidence=document.createElement('p'),next=document.createElement('p'),falsifier=document.createElement('p'),boundary=document.createElement('p'),related=recordMatches(idea,context);heading.textContent=bounded(idea.name,240)+' · 未验证（待验证建议）';evidence.textContent='具体故障输出行：'+related.faultLines.join('；');next.textContent=bounded(idea.next_step,1024);falsifier.textContent='可证伪条件：'+bounded(idea.falsifier,1024);boundary.textContent='账本静态快照；输出行数不是独立故障计数。未执行 tick、独立验证或自动准入。';article.append(heading,evidence,next,falsifier,boundary);for(const record of related.matches){const source=document.createElement('code'),snippet=document.createElement('p');source.textContent=record.category+'片段 · '+record.source;snippet.textContent=record.text;article.append(source,snippet);}if(!related.matches.length){const missing=document.createElement('p');missing.textContent=context.ledger?.enabled?'可读账本快照中没有可靠匹配的记录，暂不推断已有否决、试验或影子规则。':'未获得可读账本，不能判断是否已有同类否决、试验或影子规则。';article.append(missing);}evolution.append(article);}
      if(!result.available){content.textContent='未找到本机 mylib，请通过“目录设置”选择有效目录。';return;}
      if(!result.skills.length){content.textContent='没有匹配到技能；已索引 '+result.indexed+' 项。';return;}
      for(const card of result.skills){
        const article=document.createElement('article'),title=document.createElement('strong'),description=document.createElement('p'),reason=document.createElement('p'),source=document.createElement('code'),copy=document.createElement('button');
        title.textContent=card.name;description.textContent=card.description;reason.textContent=card.reason;source.textContent=card.source;
        copy.textContent='复制调用指令';copy.onclick=async()=>{try{await navigator.clipboard.writeText('请使用 '+card.name+' 技能；先读取 '+card.source+'，再按技能要求处理当前任务，并确认本机适用条件。');copy.textContent='已复制';}catch{copy.textContent='复制失败';}};
        article.append(title,description,reason,source,document.createElement('br'),copy);content.append(article);
        if(context.inventory?.available){const availability=document.createElement('p');availability.textContent=context.inventory.skill_names.includes(card.name)?'目标电脑技能库中可找到此技能。':'目标电脑技能库中未找到此技能；使用前需配置。';article.append(availability);}
      }
    }catch(error){run.failed=true;run.retryAt=0;if(request===revision&&!panel.hidden&&window.smaWorkspace.activePane===c.pane&&sameEvidenceIdentity(c.identity,evidenceIdentity(c.pane))){content.textContent=String(error.message||error);if(currentEvidence(c.identity))retry();}}
    finally{run.pending=false;}
  }
  toggle.onclick=()=>{panel.hidden=!panel.hidden;revision++;if(panel.hidden&&preview.open)preview.close();document.body.classList.toggle('mylib-open',!panel.hidden);toggle.setAttribute('aria-expanded',String(!panel.hidden));if(!panel.hidden){analyze(true);}};
  panel.querySelector('#mylib-refresh').onclick=()=>analyze(true);
  panel.querySelector('#mylib-pause').onclick=event=>{paused=!paused;revision++;event.target.textContent=paused?'继续感知':'暂停感知';if(!paused)analyze(true);};
  timer=setInterval(()=>analyze(),4000);
  addEventListener('beforeunload',()=>{clearInterval(timer);revision++;for(const pending of [...evidencePending])pending.cancel('页面已关闭，账本读取已停止。');},{once:true});
})();
