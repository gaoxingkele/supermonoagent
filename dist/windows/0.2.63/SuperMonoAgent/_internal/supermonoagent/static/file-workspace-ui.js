/* Optional file-workspace footer. Captured transfers survive ordinary tab changes. */
(function(){
  'use strict';
  if(typeof window==='undefined')return;
  const root=window;let installing=false,stopped=false;
  const object=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
  const text=value=>typeof value==='string'&&value.length<=4096&&!/[\u0000-\u001f\u007f]/u.test(value);
  const integer=value=>Number.isSafeInteger(value)&&value>=0;
  const codeError=code=>Object.assign(new Error(code),{code});
  const messages={host_unavailable:'目标电脑未安装或未连接文件助手。',authentication_failed:'文件服务 Token 验证失败。',
    connection_failed:'无法连接文件服务，请检查服务是否安装。',disconnected:'文件服务连接已断开，请重试。',
    ready_timeout:'文件服务连接超时，请检查服务是否安装。',request_timeout:'目标文件助手响应超时。',
    route_timeout:'目标文件助手未响应。',unauthorized:'目标电脑未确认窗口所有者权限。',
    session_unavailable:'目标窗口已关闭或尚未就绪。',target_changed:'窗口或连接已变化，请重新打开设置。',
    revision_conflict:'目录配置已被修改，请重新加载后再保存。',directory_unavailable:'目标目录不可用；交换目录必须在项目目录内。',
    project_required:'请先在底部选择当前项目目录；附件将保存到该项目的 tmp，不能使用用户主目录。',
    missing_token:'当前连接缺少文件服务 Token。',insecure_endpoint:'文件服务需要 WSS 加密连接。',
    invalid_endpoint:'文件服务地址无效。',unsupported:'当前客户端不支持文件工作区。'};
  const message=error=>Object.hasOwn(messages,error?.code)?messages[error.code]:'文件工作区暂不可用，请检查目标电脑的文件助手。';
  const create=(tag,content)=>{const element=document.createElement(tag);if(content!==undefined)element.textContent=content;return element;};
  const joinTmp=value=>{const separator=value.includes('\\')?'\\':'/';return value.replace(/[\\/]+$/,'')+separator+'tmp';};
  const normal=value=>value.replace(/\\/g,'/').replace(/\/+$/,'');
  async function boot(){
    if(stopped||installing||root.SuperMonoFileWorkspaces||!root.smaWorkspace||!root.SuperMonoFileClient)return;
    const native=root.pywebview?.api?.file_exchange_config;
    if(!object(root.SuperMonoFileWorkspaceConfig)&&typeof native!=='function'){if(!stopped)setTimeout(boot,2000);return;}
    installing=true;
    let nativeConfig;
    try{nativeConfig=object(root.SuperMonoFileWorkspaceConfig)?root.SuperMonoFileWorkspaceConfig:await native();}
    catch{installing=false;if(!stopped)setTimeout(boot,2000);return;}
    installing=false;
    if(stopped||!object(nativeConfig)||nativeConfig.enabled!==true){if(!stopped)setTimeout(boot,2000);return;}
    const W=root.smaWorkspace,F=root.SuperMonoFileClient,footer=document.querySelector('main footer')||document.querySelector('footer');
    if(!footer)return;
    let destroyed=false,active=null,reading=0,modalTarget=null,modalRevision=null,browseSequence=0,browseField='',browsePage=null,autoExchange=false;
    const clients=[],originalStatus=document.getElementById('status'),statusHidden=originalStatus?.hidden;
    const originalFooter=footer.getAttribute('data-file-workspaces');
    function config(){return object(root.SuperMonoFileWorkspaceConfig)?root.SuperMonoFileWorkspaceConfig:nativeConfig;}
    function transport(host){
      const cfg=config(),settings=host?.config;
      if(!cfg||cfg.enabled!==true||!settings||typeof settings.token!=='string'||!settings.token)throw codeError('missing_token');
      const code=typeof settings.code==='string'?settings.code:'';
      const local=!code&&host.id==='local';
      const room=local?cfg.local_room:code;
      if(typeof room!=='string'||!room||room.length>128||room!==room.trim())throw codeError('unsupported');
      const endpoint=F.brokerURL(local?cfg.broker_url:settings.relay);
      return {host,hostID:host.id,ws:host.ws,generation:host.generation,token:settings.token,room,endpoint,
              relay:settings.relay,code:settings.code,authMode:settings.authMode};
    }
    function sameTransport(a,b){return !!a&&!!b&&['host','hostID','ws','generation','token','room','endpoint','relay','code','authMode'].every(key=>a[key]===b[key]);}
    function liveTransport(value){
      try{return !destroyed&&value.host.connected&&value.ws?.readyState===1
        &&W.connections.get(value.hostID)===value.host&&sameTransport(value,transport(value.host));}catch{return false;}
    }
    function windowFor(host,session){return host.catalog?.find(win=>(win.sessions||[]).some(item=>item.id===session));}
    function captureTarget(){
      const pane=W.activePane,key=W.activeSession;
      if(!pane||!key||W.panes.get(key)!==pane||!pane.ready||pane.generation!==pane.host?.generation)return null;
      const binding=transport(pane.host),win=windowFor(pane.host,pane.raw);
      if(!liveTransport(binding)||!win||typeof win.id!=='string'||typeof pane.raw!=='string')return null;
      return Object.freeze({...binding,pane,key,session:pane.raw,window_id:win.id,paneGeneration:pane.generation,
        machine:String(pane.host.name||pane.host.id).slice(0,128),title:String(win.title||pane.raw).slice(0,128)});
    }
    function isCurrent(target){return !!target&&liveTransport(target)&&W.panes.get(target.key)===target.pane
      &&target.pane.host===target.host&&target.pane.raw===target.session&&target.pane.ready
      &&target.pane.generation===target.paneGeneration&&target.paneGeneration===target.generation
      &&windowFor(target.host,target.session)?.id===target.window_id;}
    const isActive=target=>isCurrent(target)&&W.activePane===target.pane&&W.activeSession===target.key;
    function prune(){for(let i=clients.length-1;i>=0;i--)if(!liveTransport(clients[i])){clients[i].client.close();clients.splice(i,1);}}
    function getClient(target){
      if(!isCurrent(target))throw codeError('target_changed');
      prune();let entry=clients.find(item=>sameTransport(item,target));
      if(!entry){if(clients.length>=128)throw codeError('service_busy');entry={...target,client:F.createClient({url:target.endpoint,room:target.room,token:target.token})};clients.push(entry);}
      return entry.client;
    }
    async function request(target,action,data={}){
      if(!isCurrent(target))throw codeError('target_changed');
      const client=getClient(target);await client.connect();
      if(!isCurrent(target))throw codeError('target_changed');
      const result=await client.request(action,target.session,data);
      if(!isCurrent(target))throw codeError('target_changed');
      return result;
    }
    async function uiRequest(target,action,data={}){
      if(!isActive(target))throw codeError('target_changed');
      const client=getClient(target);await client.connect();
      if(!isActive(target))throw codeError('target_changed');
      const result=await client.request(action,target.session,data);
      if(!isActive(target))throw codeError('target_changed');
      return result;
    }
    function workspace(target,value){
      if(!object(value)||value.window_id!==target.window_id||value.session_id!==target.session
        ||typeof value.daemon_epoch!=='string'||!value.daemon_epoch||value.daemon_epoch.length>128
        ||!text(value.project_dir)||!value.project_dir||!text(value.exchange_dir)||!value.exchange_dir||!integer(value.revision))throw codeError('invalid_response');
      return {...value};
    }
    async function getWorkspace(target){return workspace(target,await request(target,'workspace.get',{}));}
    const style=create('style');style.id='file-workspace-style';
    style.textContent=`footer[data-file-workspaces]{min-width:0;height:auto;min-height:29px;flex-wrap:wrap;gap:6px}footer[data-file-workspaces]>.footer-spacer{display:none}#file-workspace-footer{display:grid;grid-template-columns:minmax(0,1fr);flex:1 1 180px;min-width:0;max-width:100%;line-height:1.2}#file-workspace-footer button{min-width:0;max-width:100%;width:100%;min-height:12px;height:12px;padding:0;text-align:left;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:10px;line-height:12px}#file-workspace-dialog{width:580px;max-width:calc(100vw - 24px);max-height:calc(100dvh - 24px);overflow:auto;overflow-wrap:anywhere}#file-workspace-dialog label{margin:12px 0 4px}#file-workspace-dialog input{width:100%;min-width:0;max-width:100%}#file-workspace-dialog .file-path-row{display:flex;gap:6px;min-width:0}#file-workspace-dialog .file-path-row input{flex:1}#file-workspace-dialog .file-path-row button{flex:none}#file-workspace-browser{margin-top:12px;padding-top:10px;border-top:1px solid #444}#file-workspace-browser nav{display:flex;flex-wrap:wrap;gap:5px}#file-browse-list{display:grid;gap:3px;max-height:220px;overflow:auto;margin:8px 0}#file-browse-list button{text-align:left;overflow-wrap:anywhere;min-width:0;white-space:normal}#file-workspace-state,#file-browse-path{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}#file-workspace-dialog .dialog-actions{flex-wrap:wrap;margin-top:12px}@media(max-width:600px){#file-workspace-footer{flex-basis:100%}#file-workspace-dialog{padding:14px}}`;
    document.head.append(style);
    const paths=create('div');paths.id='file-workspace-footer';paths.setAttribute('aria-label','当前窗口文件目录');
    const projectButton=create('button','项目: 未选择窗口'),exchangeButton=create('button','交换: 未选择窗口');
    projectButton.id='file-workspace-project';exchangeButton.id='file-workspace-exchange';
    projectButton.disabled=exchangeButton.disabled=true;
    projectButton.type=exchangeButton.type='button';paths.append(projectButton,exchangeButton);footer.prepend(paths);
    footer.setAttribute('data-file-workspaces','true');if(originalStatus)originalStatus.hidden=true;
    const dialog=create('dialog');dialog.id='file-workspace-dialog';dialog.setAttribute('aria-label','窗口文件目录');
    const form=create('form'),heading=create('h2','窗口文件目录'),targetLabel=create('p'),note=create('p','交换目录必须位于项目内；留空使用项目/tmp。此设置不会改变终端当前目录。');
    targetLabel.id='file-workspace-target';
    const project=create('input'),exchange=create('input');project.id='file-project';exchange.id='file-exchange';
    project.type=exchange.type='text';project.maxLength=exchange.maxLength=4096;project.autocomplete=exchange.autocomplete='off';project.required=true;
    const projectLabel=create('label','项目目录'),exchangeLabel=create('label','交换目录');projectLabel.htmlFor=project.id;exchangeLabel.htmlFor=exchange.id;
    const projectRow=create('div'),exchangeRow=create('div');projectRow.className=exchangeRow.className='file-path-row';
    const browseProject=create('button','浏览…'),browseExchange=create('button','浏览…');browseProject.id='file-browse-project';browseExchange.id='file-browse-exchange';
    browseProject.type=browseExchange.type='button';projectRow.append(project,browseProject);exchangeRow.append(exchange,browseExchange);
    const browser=create('section');browser.id='file-workspace-browser';browser.hidden=true;
    const browsePath=create('p');browsePath.id='file-browse-path';const nav=create('nav');
    const roots=create('button','驱动器'),parent=create('button','上级目录'),use=create('button','使用此目录'),more=create('button','下一页');
    roots.id='file-browse-roots';parent.id='file-browse-parent';use.id='file-browse-use';more.id='file-browse-more';
    for(const button of [roots,parent,use,more])button.type='button';nav.append(roots,parent,use,more);
    const list=create('div');list.id='file-browse-list';browser.append(browsePath,nav,list);
    const state=create('p');state.id='file-workspace-state';state.setAttribute('role','status');
    const actions=create('div');actions.className='dialog-actions';
    const reload=create('button','重新加载'),cancel=create('button','关闭'),save=create('button','保存目录');
    reload.id='file-workspace-reload';cancel.id='file-workspace-close';save.id='file-workspace-save';
    reload.type=cancel.type='button';save.type='submit';actions.append(reload,cancel,save);
    form.append(heading,targetLabel,note,projectLabel,projectRow,exchangeLabel,exchangeRow,browser,state,actions);dialog.append(form);document.body.append(dialog);
    function display(value,error){
      for(const button of [projectButton,exchangeButton])button.disabled=!active;
      const required=value?.project_status==='selection_required';
      projectButton.textContent='项目: '+(required?'请选择当前项目目录':value?.project_dir||error||'未选择窗口');
      exchangeButton.textContent='交换: '+(required?'选择项目后使用 tmp':value?.exchange_dir||(error?'不可用':'未选择窗口'));
      projectButton.title=exchangeButton.title=required?messages.project_required:'';
      if(!required){projectButton.title=value?.project_dir||error||'';exchangeButton.title=value?.exchange_dir||error||'';}
    }
    async function refresh(){
      let target;try{target=captureTarget();}catch(error){active=null;display(null,message(error));return;}
      active=target;const sequence=++reading;
      if(!target){display(null);return;}
      display(null,'正在读取…');
      try{const value=await getWorkspace(target);if(sequence===reading&&isActive(target))display(value);}
      catch(error){if(sequence===reading&&isActive(target))display(null,message(error));}
    }
    function staleModal(){
      if(dialog.open&&!isActive(modalTarget)){save.disabled=browseProject.disabled=browseExchange.disabled=true;state.textContent=messages.target_changed;browseSequence++;}
    }
    async function loadModal(){
      const target=modalTarget;save.disabled=browseProject.disabled=browseExchange.disabled=true;state.textContent='正在读取目标目录…';
      try{
        const value=workspace(target,await uiRequest(target,'workspace.get',{}));
        if(!dialog.open||modalTarget!==target||!isActive(target))return;
        modalRevision=value.revision;project.value=value.project_dir;exchange.value=value.exchange_dir;
        autoExchange=normal(value.exchange_dir)===normal(joinTmp(value.project_dir));
        state.textContent=value.project_status==='selection_required'?messages.project_required:'';save.disabled=browseProject.disabled=browseExchange.disabled=false;
      }catch(error){if(dialog.open&&modalTarget===target)state.textContent=message(error);}
    }
    function open(){
      let target;try{target=captureTarget();}catch{return;}
      if(!target)return;modalTarget=target;modalRevision=null;browseSequence++;browser.hidden=true;
      targetLabel.textContent=target.machine+' / '+target.title;project.value=exchange.value='';dialog.showModal();loadModal();
    }
    projectButton.onclick=exchangeButton.onclick=open;
    cancel.onclick=()=>{browseSequence++;modalTarget=null;dialog.close();};
    dialog.addEventListener('cancel',()=>{browseSequence++;modalTarget=null;});reload.onclick=()=>{browser.hidden=true;loadModal();};
    project.addEventListener('input',()=>{if(autoExchange)exchange.value=joinTmp(project.value);});
    exchange.addEventListener('input',()=>{autoExchange=false;});
    function validateBrowse(value){
      if(!object(value)||!(value.path===null||text(value.path))||!(value.parent===null||text(value.parent))
        ||!Array.isArray(value.roots)||value.roots.length>26||!value.roots.every(text)
        ||!Array.isArray(value.directories)||value.directories.length>200||!value.directories.every(item=>object(item)&&text(item.name)&&text(item.path))
        ||!integer(value.offset)||!(value.next_offset===null||integer(value.next_offset)&&value.next_offset>value.offset))throw codeError('invalid_response');
      return value;
    }
    async function browse(path=null,offset=0){
      const target=modalTarget,sequence=++browseSequence;browser.hidden=false;list.replaceChildren();browsePath.textContent='正在读取目标电脑目录…';
      for(const button of [roots,parent,use,more])button.disabled=true;
      try{
        const value=validateBrowse(await uiRequest(target,'workspace.browse',{path,offset,limit:100}));
        if(sequence!==browseSequence||!dialog.open||modalTarget!==target||!isActive(target))return;
        browsePage=value;browsePath.textContent=value.path||'目标电脑的本地驱动器';
        const items=value.path===null?value.roots.map(path=>({name:path,path})):value.directories;
        for(const item of items){const button=create('button',item.name);button.type='button';button.title=item.path;button.onclick=()=>browse(item.path);list.append(button);}
        roots.disabled=false;parent.disabled=!value.parent;use.disabled=!value.path;more.disabled=value.next_offset===null;
      }catch(error){if(sequence===browseSequence&&dialog.open){browsePath.textContent=message(error);roots.disabled=!isActive(target);}}
    }
    browseProject.onclick=()=>{browseField='project';browse(project.value||null);};
    browseExchange.onclick=()=>{browseField='exchange';browse(exchange.value||project.value||null);};
    roots.onclick=()=>browse();parent.onclick=()=>browse(browsePage?.parent||null);more.onclick=()=>browse(browsePage?.path,browsePage?.next_offset||0);
    use.onclick=()=>{
      if(!isActive(modalTarget)||!browsePage?.path)return;
      if(browseField==='project'){project.value=browsePage.path;if(autoExchange)exchange.value=joinTmp(project.value);}
      else{exchange.value=browsePage.path;autoExchange=false;}
      browser.hidden=true;browseSequence++;
    };
    form.addEventListener('submit',async event=>{
      event.preventDefault();const target=modalTarget;
      if(save.disabled||!integer(modalRevision)||!isActive(target)){state.textContent=messages.target_changed;return;}
      save.disabled=true;state.textContent='正在保存到目标电脑…';
      try{
        const value=workspace(target,await uiRequest(target,'workspace.set',{project_dir:project.value,
          exchange_dir:exchange.value||joinTmp(project.value),expected_revision:modalRevision}));
        if(!dialog.open||modalTarget!==target||!isActive(target))return;
        modalRevision=value.revision;project.value=value.project_dir;exchange.value=value.exchange_dir;
        display(value);state.textContent='目录已保存。';
        root.dispatchEvent(new CustomEvent('sma-file-workspace-changed',{detail:{host_id:target.hostID,room:target.room,
          session_id:target.session,window_id:target.window_id,revision:value.revision,project_dir:value.project_dir,exchange_dir:value.exchange_dir}}));
      }catch(error){if(dialog.open&&modalTarget===target)state.textContent=message(error);}
      finally{if(modalTarget===target&&isActive(target))save.disabled=false;}
    });
    let observed=null;
    function sync(){
      if(destroyed)return;
      if(config()?.enabled!==true){destroy();return;}
      prune();let next;try{next=captureTarget();}catch(error){active=null;reading++;display(null,message(error));staleModal();return;}
      const changed=!next!==!observed||next&&(!observed||next.pane!==observed.pane||next.key!==observed.key||next.window_id!==observed.window_id||!sameTransport(next,observed));
      if(changed){observed=next;refresh();}staleModal();
    }
    const timer=setInterval(sync,250);
    async function refreshConfig(){
      if(typeof native==='function'&&!object(root.SuperMonoFileWorkspaceConfig)){
        try{nativeConfig=await native();}catch{return;}
      }
      prune();observed=null;sync();
    }
    function destroy(){
      if(destroyed)return;destroyed=true;stopped=true;clearInterval(timer);reading++;browseSequence++;
      for(const entry of clients)entry.client.close();clients.length=0;
      dialog.remove();paths.remove();style.remove();
      if(originalStatus)originalStatus.hidden=statusHidden;
      if(originalFooter===null)footer.removeAttribute('data-file-workspaces');else footer.setAttribute('data-file-workspaces',originalFooter);
      root.removeEventListener('beforeunload',destroy);root.removeEventListener('pywebviewready',boot);
      root.removeEventListener('sma-file-exchange-config-changed',refreshConfig);delete root.SuperMonoFileWorkspaces;
    }
    root.SuperMonoFileWorkspaces={captureTarget,getWorkspace,request,isCurrent,getClient,refresh,refreshConfig,destroy};
    root.addEventListener('beforeunload',destroy,{once:true});root.addEventListener('sma-file-exchange-config-changed',refreshConfig);
    sync();
  }
  root.addEventListener('pywebviewready',boot);boot();
})();
