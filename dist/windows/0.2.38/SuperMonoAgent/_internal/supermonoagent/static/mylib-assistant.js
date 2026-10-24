/* Desktop-owned, read-only sidecar. No terminal input or transport hooks. */
(function installMylib(){
  'use strict';
  if(document.getElementById('mylib-toggle')||!window.smaWorkspace)return;
  if(!window.pywebview?.api?.recommend_skills){addEventListener('pywebviewready',installMylib,{once:true});return;}
  const style=document.createElement('style');
  style.textContent=`#mylib-panel{position:fixed;right:0;top:44px;bottom:0;width:350px;z-index:20;background:#191919;border-left:1px solid #444;padding:14px;overflow:auto;overflow-wrap:anywhere}body.mylib-open #app{margin-right:350px}#mylib-panel h2{font-size:15px}#mylib-panel p{font-size:12px;line-height:1.6;color:#bbb}#mylib-panel article{border:1px solid #444;border-radius:6px;padding:10px;margin:10px 0}#mylib-panel code{font-size:11px}#mylib-panel button{border:1px solid #444;margin:3px}#mylib-context{max-height:140px;overflow:auto;white-space:pre-wrap;font-size:11px;color:#aaa}@media(max-width:1050px){body.mylib-open #app{margin-right:0}#mylib-panel{width:min(350px,100vw)}}`;
  style.textContent+='#mylib-panel{top:var(--workspace-titlebar-height,44px)}';
  document.head.append(style);
  const toggle=document.createElement('button');toggle.id='mylib-toggle';toggle.textContent='mylib 助理';toggle.setAttribute('aria-expanded','false');
  document.querySelector('.titlebar .header-spacer')?.before(toggle);
  const panel=document.createElement('aside');panel.id='mylib-panel';panel.hidden=true;panel.setAttribute('aria-label','mylib 助理');
  panel.innerHTML='<h2>mylib 助理</h2><p id="mylib-target"></p><button id="mylib-refresh">重新分析</button><button id="mylib-pause">暂停感知</button><p>本机技能建议使用本地匹配；模型对话使用所选入口。</p><details><summary>本次上下文</summary><pre id="mylib-context"></pre></details><div id="mylib-results"></div><details><summary>metaRSI 进化</summary><p>重复失败可登记为改进信号。需关联目标电脑的可信项目路径及证据账本后，才能展示项目模式、登记实验和验证进化；当前不会自动修改规则。</p></details>';
  const evolution=document.createElement('section');evolution.id='mylib-evolution';evolution.setAttribute('aria-label','metaRSI 进化建议');panel.append(evolution);
  const project=document.createElement('details'),projectHeading=document.createElement('summary'),projectNote=document.createElement('p'),ledger=document.createElement('pre');project.id='mylib-project';projectHeading.textContent='目标项目与证据账本';ledger.style.cssText='white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px';project.append(projectHeading,projectNote,ledger);panel.append(project);
  document.body.append(panel);
  if(window.pywebview.api.get_assistant_preferences&&window.pywebview.api.save_assistant_preferences){
    const settings=document.createElement('dialog');settings.id='mylib-settings';
    settings.innerHTML='<form id="mylib-settings-form"><h2>助手模型配置</h2><p>保存配置不会发起模型调用。发送问题时才调用所选入口。</p><label>模型入口<select id="mylib-provider"><option value="unconfigured">稍后配置</option><option value="codex">本机 Codex 登录</option><option value="api">已有模型 API</option></select></label><label>模型名称<input id="mylib-model" type="text" maxlength="128" autocomplete="off"></label><label id="mylib-endpoint-label">API 地址<input id="mylib-endpoint" type="url" maxlength="2048" autocomplete="off" placeholder="https://…/v1"></label><label id="mylib-key-label">API 密钥（仅本次运行保留）<input id="mylib-api-key" type="password" maxlength="8192" autocomplete="off" placeholder="本机无鉴权服务可留空"></label><label class="check"><input id="mylib-auto-context" type="checkbox">对话时自动附带当前窗口上下文</label><label>上下文字符上限<input id="mylib-context-limit" type="number" min="512" max="32768" step="1"></label><p id="mylib-settings-status" role="status"></p><div class="dialog-actions"><button id="mylib-settings-cancel" type="button">取消</button><button id="mylib-settings-save" type="submit">保存配置</button></div></form>';
    const extra=document.createElement('style');extra.textContent='#mylib-settings{max-height:85dvh;overflow:auto}#mylib-settings input:not([type=checkbox]),#mylib-settings select{width:100%;max-width:100%}#mylib-settings label{overflow-wrap:anywhere}';document.head.append(extra);document.body.append(settings);
    const $=id=>settings.querySelector('#'+id),button=document.createElement('button');button.id='mylib-configure';button.textContent='模型配置';panel.querySelector('#mylib-pause').after(button);
    let loading=false;
    const refreshFields=()=>{const api=$('mylib-provider').value==='api',key=api&&!!window.pywebview.api.set_assistant_api_key;$('mylib-endpoint-label').hidden=!api;$('mylib-endpoint').disabled=!api;$('mylib-key-label').hidden=!key;$('mylib-api-key').disabled=!key;$('mylib-model').placeholder=$('mylib-provider').value==='codex'?'留空使用登录配置':'填写模型标识';};
    $('mylib-provider').onchange=refreshFields;
    button.onclick=async()=>{if(loading)return;loading=true;button.disabled=true;try{const result=await pywebview.api.get_assistant_preferences(),p=result.preferences;$('mylib-provider').value=p.provider;$('mylib-model').value=p.model;$('mylib-endpoint').value=p.endpoint;$('mylib-api-key').value='';$('mylib-api-key').placeholder=result.api_key_present?'已设置；留空保持，本次运行有效':'本机无鉴权服务可留空';$('mylib-auto-context').checked=p.auto_context;$('mylib-context-limit').value=p.max_context_chars;refreshFields();$('mylib-settings-status').textContent=result.status==='invalid'?'原配置暂不可读，已显示默认值；保存前请检查。':'';settings.showModal();}catch{panel.querySelector('#mylib-results').textContent='模型配置暂不可读。';}finally{loading=false;button.disabled=false;}};
    $('mylib-settings-cancel').onclick=()=>{ $('mylib-api-key').value='';settings.close();};settings.addEventListener('cancel',()=>{$('mylib-api-key').value='';});
    $('mylib-settings-form').onsubmit=async event=>{event.preventDefault();const save=$('mylib-settings-save');if(save.disabled)return;save.disabled=true;let saved=false;try{await pywebview.api.save_assistant_preferences({provider:$('mylib-provider').value,model:$('mylib-model').value,endpoint:$('mylib-provider').value==='api'?$('mylib-endpoint').value:'',auto_context:$('mylib-auto-context').checked,max_context_chars:Number($('mylib-context-limit').value)});saved=true;dispatchEvent(new Event('mylib-preferences-changed'));if($('mylib-provider').value==='api'&&$('mylib-api-key').value&&pywebview.api.set_assistant_api_key)await pywebview.api.set_assistant_api_key($('mylib-api-key').value);$('mylib-settings-status').textContent='配置已保存；发送问题时才调用模型。';}catch{$('mylib-settings-status').textContent=saved?'入口已保存，但密钥未接受，请检查后重新输入。':'配置未保存，请检查模型名称、地址和上下文上限。';}finally{$('mylib-api-key').value='';save.disabled=false;}};
  }
  let paused=false,revision=0,last='',timer;
  const content=panel.querySelector('#mylib-results');
  function capture(){
    const w=window.smaWorkspace,p=w.activePane;if(!p)return null;
    const b=p.term.buffer.active,lines=[];
    for(let i=Math.max(0,b.baseY+b.cursorY-199);i<=b.baseY+b.cursorY;i++)lines.push(b.getLine(i)?.translateToString(true)||'');
    return {pane:p,generation:p.generation,id:w.activeSession,text:lines.join('\n').slice(-32768),target:(p.host.name||p.host.id)+' / '+(p.title?.textContent||p.raw)};
  }
  let contextSequence=0;
  function targetContext(c){return new Promise(resolve=>{
    const host=c.pane.host,ws=host.ws;if(!host.connected||!host.capabilities.assistant_context){resolve({note:'目标后台尚不支持项目账本读取。'});return;}
    const request='assistant-'+(++contextSequence)+'-'+Date.now().toString(36);let timeout;
    const finish=result=>{clearTimeout(timeout);ws.removeEventListener('message',receive);ws.removeEventListener('close',closed);resolve(result);};
    const closed=()=>finish({note:'目标连接已断开。'});
    const receive=event=>{if(!(event.data instanceof ArrayBuffer))return;try{const f=TerminalClient.unpack(new Uint8Array(event.data));if(f.op!==22)return;const r=JSON.parse(new TextDecoder().decode(f.payload));if(r.request===request)finish(r.ok?r:{note:r.error||'项目账本暂不可用。'});}catch{}};
    ws.addEventListener('message',receive);ws.addEventListener('close',closed);timeout=setTimeout(()=>finish({note:'项目账本读取超时。'}),8000);
    try{const frame=TerminalClient.pack(22,new TextEncoder().encode(JSON.stringify({request,session:c.pane.raw})));ws.send(frame);window.smaWorkspace.metrics.transmittedBytes+=host.meter.sent(frame);}catch{closed();}
  });}
  async function analyze(force=false){
    if(panel.hidden||paused)return;
    const c=capture();if(!c){content.textContent='请选择一个终端窗口。';return;}
    const signature=c.id+'|'+c.generation+'|'+c.text;if(!force&&signature===last)return;
    last=signature;const request=++revision;
    panel.querySelector('#mylib-target').textContent=c.target;
    panel.querySelector('#mylib-context').textContent=c.text;
    content.textContent='正在匹配本机技能…';
    evolution.textContent='';
    projectNote.textContent='正在查询目标电脑的启动目录…';ledger.textContent='';
    try{
      const api=window.pywebview?.api;if(!api?.recommend_skills)throw Error('本机助理接口尚未就绪，请稍后重新分析。');
      const [result,context]=await Promise.all([api.recommend_skills(c.text),targetContext(c)]),w=window.smaWorkspace;
      if(request!==revision||panel.hidden||w.activePane!==c.pane||c.pane.generation!==c.generation)return;
      content.replaceChildren();
      projectNote.textContent=context.project?'终端启动目录：'+context.project+'（不跟踪 shell 内的 cd）'+(context.ledger?.enabled?' · 已关联证据账本。':context.ledger?.status==='not_enabled'?' · 此目录未启用 metaRSI。':' · 项目证据暂不可读。'):context.note||'未获得可信项目目录。';
      ledger.textContent=Object.entries(context.ledger?.pages||{}).map(([name,page])=>name+' ['+({ready:'已读取',missing:'无记录',unavailable:'无法读取'}[page.status]||'未知')+']\n'+(typeof page==='string'?page:page.text||'')+(page.truncated?'\n[内容已截断]':'')).join('\n\n');
      for(const idea of result.evolution||[]){const article=document.createElement('article'),heading=document.createElement('strong'),evidence=document.createElement('p'),next=document.createElement('p'),falsifier=document.createElement('p');heading.textContent=idea.name+' · 待验证';evidence.textContent='上下文线索：'+idea.evidence.join('；');next.textContent=idea.next_step;falsifier.textContent='可证伪条件：'+idea.falsifier;article.append(heading,evidence,next,falsifier);evolution.append(article);}
      if(!result.available){content.textContent='未找到本机 mylib。可通过 SUPERMONO_MYLIB 配置目录后重新启动客户端。';return;}
      if(!result.skills.length){content.textContent='没有匹配到技能；已索引 '+result.indexed+' 项。';return;}
      for(const card of result.skills){
        const article=document.createElement('article'),title=document.createElement('strong'),description=document.createElement('p'),reason=document.createElement('p'),source=document.createElement('code'),copy=document.createElement('button');
        title.textContent=card.name;description.textContent=card.description;reason.textContent=card.reason;source.textContent=card.source;
        copy.textContent='复制调用指令';copy.onclick=async()=>{try{await navigator.clipboard.writeText('请使用 '+card.name+' 技能；先读取对应 SKILL.md，并确认当前电脑已安装该技能。');copy.textContent='已复制';}catch{copy.textContent='复制失败';}};
        article.append(title,description,reason,source,document.createElement('br'),copy);content.append(article);
        if(context.inventory?.available){const availability=document.createElement('p');availability.textContent=context.inventory.skill_names.includes(card.name)?'目标电脑技能库中可找到此技能。':'目标电脑技能库中未找到此技能；使用前需配置。';article.append(availability);}
      }
    }catch(error){if(request===revision){content.textContent=String(error.message||error);last='';}}
  }
  toggle.onclick=()=>{panel.hidden=!panel.hidden;revision++;document.body.classList.toggle('mylib-open',!panel.hidden);toggle.setAttribute('aria-expanded',String(!panel.hidden));if(!panel.hidden)analyze(true);};
  panel.querySelector('#mylib-refresh').onclick=()=>analyze(true);
  panel.querySelector('#mylib-pause').onclick=event=>{paused=!paused;revision++;event.target.textContent=paused?'继续感知':'暂停感知';if(!paused)analyze(true);};
  timer=setInterval(()=>analyze(),4000);
  addEventListener('beforeunload',()=>{clearInterval(timer);revision++;},{once:true});
})();
