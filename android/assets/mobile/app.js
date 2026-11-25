/* Android-only terminal document. The daemon remains the owner of every shell. SMA_ROOM_SELECTION_V1 SMA_ROOM_SELECTION_V2 */
(() => {
  'use strict';
  const $=id=>document.getElementById(id),C=TerminalClient,M=MobileCore;
  const encoder=new TextEncoder(),decoder=new TextDecoder();
  let config;
  let terminalAction=null;
  let mobileFeedbackQa=null;
  try{config=JSON.parse(decodeURIComponent(location.hash.slice(1)));history.replaceState(null,'',location.pathname+location.search);}
  catch{ $('status').textContent='连接配置缺失，请返回连接设置';$('notice').hidden=false;$('notice').textContent='请从安卓应用打开此页面';return; }
  $('ui-revision').textContent='界面版本：'+(config.uiRevision||'bundled');
  let selectedRooms=null;
  if(config.selectedRooms!==undefined){
    const seen=new Set();
    if(!Array.isArray(config.selectedRooms)||!config.selectedRooms.length||config.selectedRooms.length>64||config.selectedRooms.some(room=>{
      if(!room||typeof room.code!=='string'||!room.code||room.code.length>128||seen.has(room.code))return true;
      seen.add(room.code);return false;
    })){stateSelectionError();return;}
    selectedRooms=config.selectedRooms.map(room=>({code:room.code,machine:String(room.machine||'').slice(0,40),project:String(room.project||'').slice(0,40)}));
  }
  const discoverAllRooms=config.discoverAllRooms===true,excludedRoomCodes=new Set();
  if(config.discoverAllRooms!==undefined&&typeof config.discoverAllRooms!=='boolean'){stateSelectionError();return;}
  if(discoverAllRooms){
    if(!selectedRooms||!Array.isArray(config.excludedRoomCodes)||config.excludedRoomCodes.length>64||config.excludedRoomCodes.some(code=>{
      if(typeof code!=='string'||!code||code.length>128||/[\u0000-\u001f\u007f]/.test(code)||excludedRoomCodes.has(code)||selectedRooms.some(room=>room.code===code))return true;
      excludedRoomCodes.add(code);return false;
    })){stateSelectionError();return;}
  }
  function stateSelectionError(){$('status').textContent='房间选择无效，请返回连接设置重新测试';$('notice').hidden=false;$('notice').textContent='请至少选择一个可用房间。';}
  const home={id:'home',name:selectedRooms?.[0].machine||'默认电脑',endpoint:config.endpoint,code:selectedRooms?selectedRooms[0].code:config.code||''};
  const selectedRoomCodes=selectedRooms?new Set(selectedRooms.map(room=>room.code)):null;
  const selectedRoomEndpoint=selectedRooms?safeEndpoint(home):'';
  if(selectedRooms&&!selectedRoomEndpoint){stateSelectionError();return;}
  const hostTokens=new Map([['home',config.token||'']]);
  let hosts=[],hiddenHosts=[],hostId='home',machine='',project='';
  try{const saved=JSON.parse(localStorage.getItem('sma.mobile.hosts')||'[]');if(Array.isArray(saved))hosts=saved.filter(h=>h&&typeof h.id==='string'&&h.id!=='home'&&typeof h.endpoint==='string').slice(0,30).map(h=>({id:h.id,name:String(h.name||'电脑').slice(0,60),endpoint:h.endpoint,code:String(h.code||'')}));}catch{}
  if(selectedRooms){
    // Native selection shadows this relay's saved profiles for this connection,
    // while retaining their metadata unchanged for a later ordinary connection.
    hiddenHosts=hosts.filter(h=>safeEndpoint(h)===selectedRoomEndpoint);
    hosts=hosts.filter(h=>safeEndpoint(h)!==selectedRoomEndpoint);
    const signature=JSON.stringify([[home.id,home.code,hostTokens.get(home.id)]]);
    for(const room of selectedRooms.slice(1)){
      const id='discovered:'+JSON.stringify([selectedRoomEndpoint,room.code]);
      hosts.push({id,name:room.machine||room.code,endpoint:home.endpoint,code:room.code,autoDirectory:selectedRoomEndpoint,discoveryScope:signature});
      hostTokens.set(id,config.token||'');
    }
  }
  function roomCodeAllowed(endpoint,code){return endpoint!==selectedRoomEndpoint||!selectedRoomCodes||(discoverAllRooms?!excludedRoomCodes.has(code):selectedRoomCodes.has(code));}
  function roomAllowed(h){return roomCodeAllowed(safeEndpoint(h),h.code);}
  const connections=new Map(),watchers=new Map();
  const sessionKey=(id=active,hid=hostId)=>JSON.stringify([hid,id]);
  const targetKey=h=>JSON.stringify([new URL(h.endpoint).toString().replace(/\/+$/,''),h.code||'']);
  // Page-private session credentials survive an update navigation, never UI-state export.
  function saveSessionCredentials(){try{sessionStorage.setItem('sma.mobile.credentials',JSON.stringify([home,...hosts].filter(h=>!h.autoDirectory&&hostTokens.has(h.id)).map(h=>[h.id,targetKey(h),hostTokens.get(h.id)])));}catch{}}
  try{for(const row of JSON.parse(sessionStorage.getItem('sma.mobile.credentials')||'[]')){const h=[home,...hosts].find(h=>h.id===row[0]);if(h&&row.length===3&&row[1]===targetKey(h)&&typeof row[2]==='string'&&h.id!=='home')hostTokens.set(h.id,row[2]);}}catch{}
  let restoringUI=null;

  function connection(h){let c=connections.get(h.id);if(!c){c={host:h,socket:null,generation:0,retry:0,retryTimer:null,pingTimer:null,lastPing:0,connected:false,inventory:[],groups:[],activity:new Map(),agents:new Map(),historyContexts:new Map(),historyQuality:new Map(),controls:{},machine:'',project:'',subscription:'',status:'等待连接',createPending:false,beforeCreate:new Set()};connections.set(h.id,c);}return c;}
  let paused=false,connected=false;
  let inventory=[],active='',term=null,fit=null,writer=null,highlighter=null,webgl=null,renderer=config.renderer||'auto';
  const agentModeIds=new Set(['auto','codex','claude-code','grok','kimi','pi-dev','cursor-cli','hermes']);
  const agentModePreferences=new Map();
  let agentModeGeneration=0,agentModeRequest=null,agentDetector=null;
  try{for(const row of JSON.parse(localStorage.getItem('sma.mobile.agent-modes')||'[]')){
    if(!Array.isArray(row)||row.length!==2||typeof row[0]!=='string'||row[0].length>4096||!agentModeIds.has(row[1]))continue;
    const tuple=JSON.parse(row[0]);if(Array.isArray(tuple)&&tuple.length===4&&tuple.every(part=>typeof part==='string'))agentModePreferences.set(row[0],row[1]);
  }}catch{}
  function agentModeKey(){return JSON.stringify([safeEndpoint(selected().host),selected().host.code||'',hostId,active]);}
  function agentModeRevision(){return new URL(location.href).searchParams.get('ui_revision')||config.uiRevision||'bundled';}
  function saveAgentModes(){try{localStorage.setItem('sma.mobile.agent-modes',JSON.stringify([...agentModePreferences]));}catch{notice('Unable to save this terminal mode.');}}
  function clearAgentModes(id){for(const key of agentModePreferences.keys())if(JSON.parse(key)[2]===id)agentModePreferences.delete(key);saveAgentModes();}
  function refreshAgentMode(force=false){
    if(!term||!highlighter||!active)return;
    const current=term,decorator=highlighter,host=selected(),hostGeneration=host.generation,key=agentModeKey(),revision=agentModeRevision();
    const preference=agentModePreferences.get(key)||'auto',session=host.inventory.find(item=>item.id===active);
    const detected=C.agentModeFromSession(session||{},session?.title||'');
    const mode=preference==='auto'?(agentModeIds.has(detected)?detected:'auto'):preference;
    $('agent-mode').disabled=false;$('agent-mode').value=preference;
    if(!force&&agentModeRequest?.term===current&&agentModeRequest.mode===mode&&agentModeRequest.key===key&&agentModeRequest.hostGeneration===hostGeneration&&agentModeRequest.revision===revision)return;
    const generation=++agentModeGeneration;agentModeRequest={term:current,mode,key,hostGeneration,revision};
    const valid=()=>generation===agentModeGeneration&&term===current&&highlighter===decorator&&selected()===host&&host.generation===hostGeneration&&agentModeKey()===key&&agentModeRevision()===revision;
    const apply=profile=>{if(!valid())return;const policy=C.presentationPolicy(profile,detected);Object.assign(current.options,policy.terminalOptions);decorator.setProfile(policy.profile);decorator.setPresentation?.('codex');$('screen').dataset.agentMode=mode;$('screen').dataset.actualAgent=policy.actualAgent;$('screen').dataset.presentationStyle='codex';$('screen').dataset.agentPreference=preference;$('agent-mode-status').textContent=(preference==='auto'?'自动识别 · ':'高亮规则 · ')+profile.label+' · Codex 风格';};
    if(mode==='auto'){apply(C.commonAgentProfile);return;}
    const url=new URL('agent-modes/'+mode+'.json',location.href);url.search='';url.hash='';url.searchParams.set('ui_revision',revision);
    $('agent-mode-status').textContent='Loading '+mode+'…';
    C.loadAgentProfile(url.toString(),mode).then(apply).catch(()=>{
      if(!valid())return;apply(C.commonAgentProfile);$('agent-mode-status').textContent='规则暂不可用 · Codex 风格 · '+detected;
    });
  }
  $('agent-mode').disabled=true;
  $('agent-mode').onchange=()=>{if(!term||!active)return;const value=$('agent-mode').value;if(!agentModeIds.has(value))return;agentModePreferences.set(agentModeKey(),value);saveAgentModes();refreshAgentMode(true);};
  $('highlight').checked=localStorage.getItem('sma.mobile.highlight')!=='off';
  $('highlight').onchange=()=>{localStorage.setItem('sma.mobile.highlight',$('highlight').checked?'on':'off');highlighter?.setEnabled($('highlight').checked);};
  let ready=false,replaying=0,fitFrame=null,lastSize='',control=false,ctrl=false,composing=false,pendingKeys=[],chunkedReplay=false;
  let controlAction=null,followIntent=false,followReplay=null,historyStatusFrame=0;
  let nativeRawInput=false;
  const rawInputPreferences=new Map();
  function scheduleHistoryStatus(){if(historyStatusFrame)return;historyStatusFrame=requestAnimationFrame(()=>{historyStatusFrame=0;historyStatus();});}
  const uncertainDrafts=new Map();
  const uncertainModelCommands=new Set();
  // NATIVE_RECEIPT_START: cancellation records the captured paste, never the new pane.
  const nativeReceiptScopes=new Map();let nativeInputDepth=0;
  function nativeScopeHost(scope){return connections.get(scope.hostId);}
  function nativeScopeMatches(scope){
    const c=nativeScopeHost(scope);if(!c)return true;
    if(safeEndpoint(c.host)!==scope.endpoint||(c.host.code||'')!==scope.room||scope.token!==undefined&&hostTokens.get(scope.hostId)!==scope.token)return false;
    const session=c.inventory.find(item=>item.id===scope.session);
    return !session||(!scope.window||session.window===scope.window)&&(!scope.process_generation||!session.process_generation||session.process_generation===scope.process_generation);
  }
  function forgetNativeScope(key){uncertainDrafts.delete(key);uncertainModelCommands.delete(key);nativeReceiptScopes.delete(key);}
  function verifyNativeScope(scope){
    const c=nativeScopeHost(scope);if(scope.token!==undefined||scope.proofPending||scope.proofFailed||!c||!hostTokens.has(scope.hostId))return;
    const token=hostTokens.get(scope.hostId);scope.proofPending=true;
    Promise.resolve(managedAuthKey({host:c,endpoint:scope.endpoint,room:scope.room,token})).then(proof=>{
      if(nativeReceiptScopes.get(scope.key)!==scope)return;
      if(proof!==scope.authKey||!nativeScopeMatches(scope))forgetNativeScope(scope.key);else scope.token=token;
    }).catch(()=>{scope.proofFailed=true;}).finally(()=>{scope.proofPending=false;if(sessionKey()===scope.key)updateComposer();});
  }
  function pruneNativeUncertainty(){for(const [key,scope] of nativeReceiptScopes){if(!nativeScopeMatches(scope))forgetNativeScope(key);else verifyNativeScope(scope);}}
  function clearNativeUncertainty(host){
    for(const [key,scope] of nativeReceiptScopes)if(scope.hostId===host)forgetNativeScope(key);
    for(const key of [...uncertainDrafts.keys(),...uncertainModelCommands]){try{if(JSON.parse(key)[0]===host)forgetNativeScope(key);}catch{}}
  }
  function setHostToken(host,token){
    if(hostTokens.has(host)&&hostTokens.get(host)!==token){if(selected().host.id===host){composerSubmit.cancel('authorization_changed');if(typeof mobileFeedbackQa!=='undefined')mobileFeedbackQa?.invalidate();}clearNativeUncertainty(host);}
    hostTokens.set(host,token);
  }
  function captureNativeScope(c,current,key){
    const session=c.inventory.find(item=>item.id===active)||{},binding=terminalBindings.get(current)||{};
    const scope={key,hostId:c.host.id,session:active,window:session.window||'',process_generation:session.process_generation||'',endpoint:binding.endpoint||safeEndpoint(c.host),room:binding.code??c.host.code??'',token:binding.token??hostTokens.get(c.host.id),authKey:'',proofPending:true};
    Promise.resolve(managedAuthKey({host:c,endpoint:scope.endpoint,room:scope.room,token:scope.token})).then(proof=>{scope.authKey=proof;}).catch(()=>{}).finally(()=>{scope.proofPending=false;});return scope;
  }
  function markNativeUncertain(job){
    if(job.uncertain)return;job.uncertain=true;const scope=job.scope;
    if(!nativeScopeMatches(scope))return;
    nativeReceiptScopes.set(scope.key,scope);
    if(job.kind==='model')uncertainModelCommands.add(scope.key);else uncertainDrafts.set(scope.key,job.text);
    if(sessionKey()===scope.key&&!isManagedSession()){$('composer-status').textContent=job.kind==='model'?'上次 /model 的输入状态未确认，已停止自动回车；请检查原始终端。':'粘贴可能已到达原始终端，已停止自动回车；原草稿保留，请先检查终端。';notice($('composer-status').textContent);updateComposer();}
  }
  function ownNativeInput(action){nativeInputDepth++;try{return action();}finally{nativeInputDepth--;}}
  function sendNativeComposer(text,kind,onSent){
    const current=term,c=selected(),socket=c.socket,generation=c.generation,key=sessionKey(),binding=terminalBindings.get(current),scope=captureNativeScope(c,current,key),job={scope,text,kind,uncertain:false};
    const stillCurrent=()=>!paused&&ready&&connected&&socket?.readyState===1&&term===current&&selected()===c&&c.socket===socket&&c.generation===generation&&sessionKey()===key&&validTerminalBinding(binding);
    try{return composerSubmit.submit(text,{paste:value=>stillCurrent()?ownNativeInput(()=>current.paste(value)):false,enter:()=>ownNativeInput(()=>input('\r','composer')),echo:M.createComposerEcho(current,text),isCurrent:stillCurrent,
      onUncertain:()=>markNativeUncertain(job),onSent:()=>{onSent();if(!uncertainDrafts.has(key)&&!uncertainModelCommands.has(key))nativeReceiptScopes.delete(key);}});
    }catch{markNativeUncertain(job);return false;}
  }
  function nativeUncertaintyTargets(){return [...nativeReceiptScopes].filter(([key])=>uncertainDrafts.has(key)||uncertainModelCommands.has(key)).map(([key,scope])=>[key,{hostId:scope.hostId,session:scope.session,window:scope.window,process_generation:scope.process_generation,endpoint:scope.endpoint,room:scope.room,authKey:scope.authKey}]);}
  function restoreNativeUncertaintyTargets(rows){
    for(const row of (rows||[]).slice(0,64)){
      const [key,value]=Array.isArray(row)?row:[];
      if(typeof key!=='string'||!value||!['hostId','session','window','process_generation','endpoint','room','authKey'].every(field=>typeof value[field]==='string')||!/^[a-f0-9]{64}$/.test(value.authKey)||!uncertainDrafts.has(key)&&!uncertainModelCommands.has(key))continue;
      const scope={key,...value,proofPending:false};nativeReceiptScopes.set(key,scope);verifyNativeScope(scope);
    }
    pruneNativeUncertainty();
  }
  // NATIVE_RECEIPT_END
  const attachmentDrafts=new Map();
  let attachmentPickerTarget=null,attachmentReturnKey=null,uploadJob=null,fileRequestSequence=0;
  let copyTargetKey=null;
  const composerSubmit=M.createComposerSubmitter({onPending:pending=>{updateComposer(pending);$('draft').readOnly=pending;$('composer-close').disabled=pending;}});
  let controls={};
  let waitingFeeds=[],waitingBytes=0;
  let sizing=localStorage.getItem('sma.mobile.sizing')==='shared'?'shared':'phone';
  $('sizing').value=sizing;
  let createPending=false;
  const modes=new Map(),drafts=new Map();
  const metrics={connections:0,frames:0,bytes:0,writes:0,fallbacks:0,historySamples:0,historySkips:0,historyCells:0,historyMilliseconds:0,listBuilds:0,listSkips:0};
  let replayNoticeTimer=null,pendingRefresh=null,refreshFeedbackTimer=null;
  const replayFeeds=new WeakMap();
  const replayGrids=new WeakMap(),drawEpochs=new WeakMap();
  const terminalCache=new Map(),terminalBindings=new WeakMap(),initializedTerms=new WeakSet(),restoringTerms=new WeakSet();
  const streamPositions=new WeakMap(),queuedPositions=new WeakMap(),terminalWrites=new WeakMap(),settledSubscriptions=new WeakMap();
  const writeStamps=new WeakMap();
  const screenHistories=new Map();
  let historyRenderer=null,historyFrameID=null;
  let debugPage=0,debugCapacity=16,debugSignature='',debugFrame=null,debugTimer=null;
  const debugMonitors=new Map(),debugCards=new Map();
  const debugActivity=new Map(),debugFirstSeen=new Map(),debugActivityMonitors=new Map();
  let debugOrder=[],debugOrderAt=0,debugInteractionUntil=0,debugPointer=null,debugSuppressClickUntil=0,debugPages=1,debugNextOrder=0;
  const notice=text=>{clearTimeout(replayNoticeTimer);replayNoticeTimer=null;$('notice').textContent=text;$('notice').hidden=!text;};
  function replayNotice(text){notice(text);if(text)replayNoticeTimer=setTimeout(()=>{replayNoticeTimer=null;if($('notice').textContent===text){$('notice').textContent='';$('notice').hidden=true;}},3000);}
  function cancelRefresh(){clearTimeout(refreshFeedbackTimer);refreshFeedbackTimer=null;if(!pendingRefresh){$('refresh-screen').textContent='\u5237\u65b0';return;}clearTimeout(pendingRefresh.timer);pendingRefresh=null;$('refresh-screen').textContent='\u5237\u65b0';$('refresh-screen').removeAttribute('aria-busy');updateControlButton();}
  function refreshScreen(){
    if(pendingRefresh||controlAction||followReplay||!term||!active||!connected||paused)return;
    const c=selected(),buffer=term.buffer.active,screen=$('screen');
    const request={host:c,generation:c.generation,sid:active,terminal:term,distance:buffer.baseY-buffer.viewportY,left:screen.scrollLeft,top:screen.scrollTop,bottom:localView().bottom};
    pendingRefresh=request;updateControlButton();
    let sent=false;
    if(c.capabilities?.snapshot_refresh===true)sent=sendHost(c,16,{sessions:[active],refresh:true});
    else{
      const session=inventory.find(item=>item.id===active);
      // Legacy selection only changes this client's read pointer. It never
      // unsubscribes output, releases a size lease, or sends terminal input.
      if(session?.window&&session.window===c.serverWindow)sent=sendHost(c,14,{session:active});
      else if(session?.window){sent=sendHost(c,13,{window:session.window});if(sent&&session.windowFocused!==active)sent=sendHost(c,14,{session:active});}
    }
    if(!sent){cancelRefresh();return;}
    $('refresh-screen').textContent='\u5237\u65b0\u4e2d\u2026';$('refresh-screen').setAttribute('aria-busy','true');
    request.timer=setTimeout(()=>{if(pendingRefresh!==request)return;cancelRefresh();notice('\u5237\u65b0\u8d85\u65f6\uff0c\u8bf7\u68c0\u67e5\u8fde\u63a5\u540e\u91cd\u8bd5');},6000);
    term.refresh(0,term.rows-1);
  }
  function updateTerminalStatus(){
    const strip=$('terminal-status');if(!strip)return;
    if(historyRenderer){strip.hidden=true;return;}
    let text='';
    if(term&&C.agentModeFromSession(inventory.find(item=>item.id===active)||{},inventory.find(item=>item.id===active)?.title||'')==='codex'){
      const buffer=term.buffer.active,lines=[];
      for(let row=Math.max(0,term.rows-4);row<term.rows;row++)lines.push(buffer.getLine(buffer.baseY+row)?.translateToString(true)||'');
      const footer=lines.filter(line=>/(?:context|\u4e0a\u4e0b\u6587|weekly|week\s+limit|\u5468\u989d\u5ea6|\u6bcf\u5468|\u5468\u9650\u989d)/i.test(line)).join(' ').replace(/\s+/g,' ').trim();
      const cell=term._core?._renderService?.dimensions.css.cell;
      if(cell&&term.cols*cell.width>$('screen').clientWidth&&/(?:context|\u4e0a\u4e0b\u6587)/i.test(footer)&&/(?:weekly|week\s+limit|\u5468\u989d\u5ea6|\u6bcf\u5468|\u5468\u9650\u989d)/i.test(footer))text=footer;
    }
    if(strip.textContent!==text)strip.textContent=text;
    if(strip.hidden!==!text)strip.hidden=!text;
  }
  const state=text=>{$('status').textContent=text;};
  function saveHosts(){try{localStorage.setItem('sma.mobile.hosts',JSON.stringify([...hiddenHosts,...hosts.filter(h=>!h.autoDirectory)]));return true;}catch{notice('电脑列表无法保存；请检查设备存储空间');return false;}}
  function selected(){return connection([home,...hosts].find(h=>h.id===hostId)||home);}
  function loadSelected(){const c=selected();connected=c.connected;inventory=c.inventory;controls=c.controls;machine=c.machine;project=c.project;createPending=c.createPending;state(c.status);}
  let lastHostSignature='';
  function renderHosts(){
    const signature=JSON.stringify([hostId,machine,project,[home,...hosts].map(h=>{const c=connection(h);return [h.id,h.name,!!h.autoDirectory,c.machine,c.project,c.status];})]);
    if(signature===lastHostSignature)return;lastHostSignature=signature;
    $('host-list').replaceChildren();
    for(const h of [home,...hosts]){
      const c=connection(h),row=document.createElement('div');row.className='host-row';
      const b=document.createElement('button');b.className='host-button';b.dataset.hostId=h.id;b.setAttribute('aria-current',String(h.id===hostId));
      b.textContent=[c.project,c.machine||h.name,c.status].filter(Boolean).join(' · ');b.onclick=()=>chooseHost(h.id);row.append(b);
      if(!h.autoDirectory){const edit=document.createElement('button');edit.textContent='设置';edit.onclick=()=>editHost(h);row.append(edit);}
      const retryButton=document.createElement('button');retryButton.textContent='重连';retryButton.onclick=()=>connect(c);row.append(retryButton);
      if(h.id!=='home'&&!h.autoDirectory){const remove=document.createElement('button');remove.className='host-remove';remove.textContent='移除';remove.setAttribute('aria-label','移除电脑 '+h.name);remove.onclick=()=>{stopSocket(c);connections.delete(h.id);hosts=hosts.filter(x=>x.id!==h.id);hostTokens.delete(h.id);clearAgentModes(h.id);saveSessionCredentials();for(const key of [...drafts.keys()])if(JSON.parse(key)[0]===h.id)drafts.delete(key);for(const key of [...modes.keys()])if(JSON.parse(key)[0]===h.id)modes.delete(key);if(h.id===hostId)chooseHost('home');saveHosts();refreshWatchers();renderHosts();renderList();};row.append(remove);}
      $('host-list').append(row);
    }
    $('host-identity').textContent=[machine||selected().host.name,project].filter(Boolean).join(' · ');
  }
  function chooseHost(id,restoring=false){
    if(!restoring)restoringUI=null;
    const h=[home,...hosts].find(x=>x.id===id);if(!h||id===hostId)return;
    mobileFeedbackQa?.invalidate();
    if(active)drafts.set(sessionKey(),$('draft').value);
    const previous=selected();sendHost(previous,16,{sessions:[]});previous.subscription='';
    disposeTerminal();active='';hostId=id;loadSelected();$('draft').value='';$('quick-draft').value='';notice('');
    if(inventory.length)activate(inventory[0].id);renderHosts();renderList();
  }
  function endpoint(h){
    const u=new URL(C.secureEndpoint(h.endpoint));
    if(!['http:','https:'].includes(u.protocol)||u.username||u.password||u.search||u.hash)throw new Error('无效的服务器地址');
    u.pathname=u.pathname.replace(/\/+$/,'')+'/ws';u.protocol=u.protocol==='https:'?'wss:':'ws:';return u.toString();
  }
  function sendHost(c,op,value){if(!c.connected||paused||c.socket?.readyState!==1)return false;c.socket.send(C.pack(op,value instanceof Uint8Array?value:encoder.encode(JSON.stringify(value))));return true;}
  function send(op,value){return sendHost(selected(),op,value);}
  // JOURNAL_INTEGRATION_START: transport and draft guards are independent of xterm.
  let journalRequestSequence=0,journalContextVersion=0,journalStatusPending=false,journalRefreshTimer=null;
  let journalViewer=null,journalInlineViewer=null,journalSourceTarget=null,journalInlineSeq=-1,journalFoldRequested=false;
  const journalCodePreferences=new Map(),journalReadActions=new Set(['status','list','read_page','read_content']);
  function journalError(code){const error=new Error('记录请求未完成');error.code=code;return error;}
  function journalTarget(){
    const c=selected(),session=c.inventory.find(s=>s.id===active);
    return {host:c,endpoint:safeEndpoint(c.host),room:c.host.code||'',token:hostTokens.get(c.host.id)||'',generation:c.generation,session:session?.id||'',window:session?.window||'',windowTitle:session?.title||session?.shell||'',process_generation:session?.process_generation||'',journalId:session?.journal_id||c.journalManifests?.get(active)?.journal_id||''};
  }
  function journalTargetCurrent(t,archiveRead=false){
    if(!t||paused||selected()!==t.host||connections.get(t.host.host.id)!==t.host||!archiveRead&&t.host.generation!==t.generation||safeEndpoint(t.host.host)!==t.endpoint||(t.host.host.code||'')!==t.room||(hostTokens.get(t.host.host.id)||'')!==t.token)return false;
    if(!t.session)return !active;
    const session=t.host.inventory.find(s=>s.id===t.session&&s.window===t.window);
    return active===t.session&&!!session&&!(t.journalId&&session.journal_id&&t.journalId!==session.journal_id)&&!(t.process_generation&&session.process_generation!==t.process_generation);
  }
  function journalContextKey(t){return JSON.stringify([t.endpoint,t.room,t.session,t.window,t.journalId,t.generation,journalContextVersion]);}
  function journalCodeKey(t,manifest){return JSON.stringify([t.endpoint,t.room,t.window,manifest.journal_id]);}
  function journalHostRequest(t,action,params){
    if(!journalTargetCurrent(t))return Promise.reject(journalError('stale_target'));
    if(t.host.capabilities?.conversation_journal!==1&&t.host.capabilities?.conversation_journal!==true)return Promise.reject(journalError('unsupported'));
    return new Promise((resolve,reject)=>{
      const c=t.host,request_id='mobile-journal-'+(++journalRequestSequence);
      const pending=c.journalPending||(c.journalPending=new Map());
      const finish=(error,result)=>{const entry=pending.get(request_id);if(!entry)return;pending.delete(request_id);clearTimeout(entry.timer);if(error)reject(error);else if(!journalTargetCurrent(t))reject(journalError('stale_target'));else resolve(result);};
      const timer=setTimeout(()=>finish(journalError('timeout')),8000);pending.set(request_id,{timer,finish});
      try{if(!sendHost(c,24,{request_id,action,params}))finish(journalError('disconnected'));}catch{finish(journalError('disconnected'));}
    });
  }
  function journalHostResponse(c,value){
    if(!value||typeof value.request_id!=='string')return;
    c.journalPending?.get(value.request_id)?.finish(value.ok===true?null:journalError('request_refused'),value.result);
  }
  function journalArchiveURL(t){
    if(!t.room||!t.token)throw journalError('unsupported');
    const url=new URL(endpoint(t.host.host));
    if(url.protocol!=='wss:'&&!(url.protocol==='ws:'&&['127.0.0.1','localhost','[::1]'].includes(url.hostname)))throw journalError('unsupported');
    url.pathname='/journals/ws';url.search='';url.hash='';return url.toString();
  }
  function closeJournalArchive(c){
    const archive=c.journalArchive;if(!archive)return;c.journalArchive=null;
    archive.dead=true;clearTimeout(archive.timer);archive.rejectReady(journalError('disconnected'));
    for(const entry of archive.pending.values()){clearTimeout(entry.timer);entry.reject(journalError('disconnected'));}archive.pending.clear();
    archive.socket.onclose=null;archive.socket.close();
  }
  function preserveJournalArchive(c){
    if(c!==selected()||paused||c.connected||!c.journalArchive)return false;
    const t=journalTarget(),archive=c.journalArchive;
    try{return archive.token===t.token&&archive.room===t.room&&archive.url===journalArchiveURL(t);}catch{return false;}
  }
  async function journalArchiveRequest(t,action,params){
    if(!journalReadActions.has(action)||!journalTargetCurrent(t,true))throw journalError('stale_target');
    const c=t.host,url=journalArchiveURL(t);
    let archive=c.journalArchive;
    if(archive&&(archive.url!==url||archive.token!==t.token||archive.room!==t.room)){closeJournalArchive(c);archive=null;}
    if(!archive){
      const socket=new WebSocket(url);archive={socket,url,token:t.token,room:t.room,generation:t.generation,pending:new Map(),dead:false};c.journalArchive=archive;
      archive.ready=new Promise((resolve,reject)=>{archive.resolveReady=resolve;archive.rejectReady=reject;});
      archive.timer=setTimeout(()=>closeJournalArchive(c),8000);
      const current=()=>c.journalArchive===archive&&!archive.dead&&journalTargetCurrent(t,true);
      socket.onopen=()=>{if(!current()){closeJournalArchive(c);return;}socket.send(JSON.stringify({op:'hello',protocol:'sma-journal-1',role:'viewer',token:t.token,room:t.room}));};
      socket.onmessage=event=>{
        if(!current())return;
        try{
          if(typeof event.data!=='string'||encoder.encode(event.data).length>196608)throw journalError('invalid_response');
          const value=JSON.parse(event.data);
          if(value.op==='hello'){if(value.ok!==true||value.protocol!=='sma-journal-1')throw journalError('request_refused');clearTimeout(archive.timer);archive.resolveReady();}
          else if(value.op==='response'){
            const pending=archive.pending.get(value.request_id);if(!pending)return;
            archive.pending.delete(value.request_id);clearTimeout(pending.timer);
            if(!journalTargetCurrent(t,true))pending.reject(journalError('stale_target'));
            else if(value.ok===true)pending.resolve(value.result);else pending.reject(journalError('request_refused'));
          }
        }catch{closeJournalArchive(c);}
      };
      socket.onclose=()=>{if(c.journalArchive===archive)closeJournalArchive(c);};socket.onerror=()=>{};
    }
    await archive.ready;
    if(!journalTargetCurrent(t,true)||archive.dead||archive.socket.readyState!==1)throw journalError('stale_target');
    return new Promise((resolve,reject)=>{
      const request_id='mobile-archive-'+(++journalRequestSequence),timer=setTimeout(()=>{archive.pending.delete(request_id);reject(journalError('timeout'));},8000);
      archive.pending.set(request_id,{resolve,reject,timer});
      try{archive.socket.send(JSON.stringify({op:'request',request_id,action,params}));}catch{clearTimeout(timer);archive.pending.delete(request_id);reject(journalError('disconnected'));}
    });
  }
  async function journalRequest(t,action,params={}){
    if(!journalTargetCurrent(t,t.archive===true))throw journalError('stale_target');
    if(t.archive===true)return journalArchiveRequest(t,action,params);
    if(t.host.connected&&(t.host.capabilities?.conversation_journal===1||t.host.capabilities?.conversation_journal===true))return journalHostRequest(t,action,params);
    if(!journalReadActions.has(action))throw journalError('unsupported');
    if(action==='status'&&params.session_id){if(!t.journalId)throw journalError('unsupported');params={journal_id:t.journalId};}
    return journalArchiveRequest(t,action,params);
  }
  function journalQuote(t,text,source){
    if(!journalTargetCurrent(t)||!t.session||composing||composerSubmit.pending||typeof managedPending==='function'&&managedPending()||uploadJob||typeof text!=='string'||!text)return false;
    const draft=$('draft').value,quote=C.formatQuote(text,source),combined=draft+(draft?'\n\n':'')+quote;
    if(combined.length>65536){notice('引用超过草稿长度限制；未截断或发送，请减少选择。');return false;}
    syncDraftFields(combined);drafts.set(sessionKey(),combined);updateComposer();return true;
  }
  function journalViewerOptions(t,inline=false){return {root:inline?$('journal-reading'):document.body,inline,targetKey:journalContextKey(t),request:(action,params)=>journalRequest(t,action,params),isTargetCurrent:()=>journalTargetCurrent(t),onQuote:(text,source)=>journalQuote(t,text,source),onOriginalTerminal:()=>{journalViewer?.close();showJournalRaw(true);}};}
  function showJournalRaw(remember=false){
    $('journal-reading').hidden=true;journalInlineViewer?.close();journalInlineSeq=-1;
    $('journal-code').title='代码：显示';$('journal-code').setAttribute('aria-label','代码：显示');$('journal-code').setAttribute('aria-pressed','true');
    if(remember){const t=journalTarget(),manifest=t.host.journalManifests?.get(t.session);if(manifest)journalCodePreferences.set(journalCodeKey(t,manifest),true);}
  }
  function resetJournalContext(){
    journalContextVersion++;journalStatusPending=false;journalFoldRequested=false;clearTimeout(journalRefreshTimer);journalRefreshTimer=null;
    closeJournalArchive(selected());
    journalViewer?.close();journalInlineViewer?.close();$('journal-source-dialog').close();journalSourceTarget=null;showJournalRaw();
    if(journalViewer)journalViewer.setContext({targetKey:'inactive-'+journalContextVersion,isTargetCurrent:()=>false});
    if(journalInlineViewer)journalInlineViewer.setContext({targetKey:'inactive-'+journalContextVersion,isTargetCurrent:()=>false});
  }
  function journalCanFold(manifest){return !!manifest?.source?.bound&&manifest.source.supported===true&&manifest.source.healthy===true&&manifest.capture_state==='capturing'&&!manifest.local_error&&manifest.storage?.available===true;}
  function journalUnavailable(manifest){
    if(!manifest?.source?.bound)return '尚未绑定准确对话源；请在设置中选择会话 ID。';
    if(manifest.source.supported!==true)return '此提供方版本尚未证明最终答复可用；已保留原始终端。';
    return '对话采集未就绪或保存异常；已保留原始终端。';
  }
  async function refreshJournalStatus(fold=false){
    if(fold)journalFoldRequested=true;
    if(journalStatusPending||!active||paused)return;
    const t=journalTarget(),version=journalContextVersion;journalStatusPending=true;
    try{
      const manifest=await journalRequest(t,'status',{session_id:t.session});
      if(version!==journalContextVersion||!journalTargetCurrent(t)||!manifest?.journal_id)return;
      (t.host.journalManifests||(t.host.journalManifests=new Map())).set(t.session,manifest);t.journalId=manifest.journal_id;
      fold=journalFoldRequested;journalFoldRequested=false;
      if(fold)journalCodePreferences.set(journalCodeKey(t,manifest),false);
      const reading=journalCodePreferences.get(journalCodeKey(t,manifest))===false;
      if(!reading||!journalCanFold(manifest)){showJournalRaw();if(fold)notice(journalUnavailable(manifest));return;}
      const options=journalViewerOptions(t,true),request=options.request;let readFailed=false;
      options.request=async(action,params)=>{try{return await request(action,params);}catch(error){readFailed=true;throw error;}};
      if(!journalInlineViewer)journalInlineViewer=C.createConversationViewer(options);
      if($('journal-reading').hidden){
        journalInlineViewer.setContext(options);await journalInlineViewer.open({projection:typeof isManagedSession==='function'&&isManagedSession()?'conversation':'history',journalId:manifest.journal_id});
        if(!journalTargetCurrent(t)||version!==journalContextVersion||journalCodePreferences.get(journalCodeKey(t,manifest))!==false)return;
        if(readFailed){showJournalRaw();return;}journalInlineViewer.setCodeVisible(false);journalInlineSeq=manifest.last_seq;
      }else if(journalInlineSeq!==manifest.last_seq){
        const viewer=journalInlineViewer,refreshed=await viewer.refresh();
        if(!journalTargetCurrent(t)||version!==journalContextVersion||viewer!==journalInlineViewer||journalCodePreferences.get(journalCodeKey(t,manifest))!==false||!refreshed)return;
        journalInlineSeq=manifest.last_seq;
      }
      $('journal-reading').hidden=false;$('journal-code').title='代码：折叠';$('journal-code').setAttribute('aria-label','代码：折叠');$('journal-code').setAttribute('aria-pressed','false');
    }catch{if(version===journalContextVersion){showJournalRaw();if(fold)notice('此连接尚不支持可用的对话采集；原始终端仍可使用。');}}
    finally{if(version===journalContextVersion){journalStatusPending=false;clearTimeout(journalRefreshTimer);journalRefreshTimer=setTimeout(()=>refreshJournalStatus(),2500);}}
  }
  async function openJournal(projection,archives=false){
    const t=journalTarget();if(!archives&&!t.session){notice('请先选择终端窗口。');return;}
    if(!t.token){notice('请先配置此电脑的访问令牌。');return;}
    t.archive=!t.host.connected||!(t.host.capabilities?.conversation_journal===1||t.host.capabilities?.conversation_journal===true);
    const options=journalViewerOptions(t);
    if(!journalViewer)journalViewer=C.createConversationViewer(options);else journalViewer.setContext(options);
    await journalViewer.open(archives?{projection}:{projection,journalId:t.journalId,sessionId:t.session,windowTitle:t.windowTitle});
  }
  async function loadJournalSources(){
    const t=journalSourceTarget;if(!journalTargetCurrent(t)||!t.session)return;
    const status=$('journal-source-status'),provider=$('journal-provider').value;
    if(!['codex','claude'].includes(provider))return;
    $('journal-sources').replaceChildren();status.textContent='正在查找准确会话 ID…';
    try{
      const manifest=await journalHostRequest(t,'status',{session_id:t.session});
      if(!journalTargetCurrent(t)||journalSourceTarget!==t)return;
      const result=await journalHostRequest(t,'list_sources',{session_id:t.session,provider});
      if(!journalTargetCurrent(t)||journalSourceTarget!==t||$('journal-provider').value!==provider)return;
      status.textContent=result.sources?.length?'核对会话 ID 后选择绑定；不会启动或恢复 CLI。':'没有可绑定来源；请确认提供方版本支持。';
      for(const source of (result.sources||[]).slice(0,100)){
        if(typeof source.source_ref!=='string')continue;
        const button=document.createElement('button');button.type='button';button.textContent=[source.provider,source.provider_session_id,source.provider_version,source.supported===true?'支持':'覆盖受限'].filter(Boolean).join(' · ');
        button.onclick=async()=>{
          if(!journalTargetCurrent(t)||journalSourceTarget!==t||$('journal-provider').value!==provider)return;
          try{
            await journalHostRequest(t,'bind_source',{session_id:t.session,source_ref:source.source_ref,terminal_generation:manifest.process_generation});
            if(!journalTargetCurrent(t)||journalSourceTarget!==t)return;
            status.textContent='来源已绑定；仅经验证的最终答复进入对话记录。';await refreshJournalStatus();
          }catch{if(journalSourceTarget===t)status.textContent='绑定未完成；目标、权限或终端代际可能已变化。';}
        };$('journal-sources').append(button);
      }
    }catch{if(journalSourceTarget===t)status.textContent='此主机不支持来源选择，或连接/权限不可用。';}
  }
  $('journal-history').onclick=()=>openJournal('history');$('journal-conversation').onclick=()=>openJournal('conversation');
  $('journal-code').onclick=()=>{$('journal-reading').hidden?refreshJournalStatus(true):showJournalRaw(true);};
  const journalSourceButton=document.createElement('button');journalSourceButton.id='journal-source';journalSourceButton.type='button';journalSourceButton.textContent='绑定对话源';
  journalSourceButton.onclick=()=>{$('settings-dialog').close();journalSourceTarget=journalTarget();$('journal-sources').replaceChildren();$('journal-source-status').textContent='请选择提供方，再查找准确会话 ID。';$('journal-source-dialog').showModal();};$('settings-dialog').append(journalSourceButton);
  const journalArchivesButton=document.createElement('button');journalArchivesButton.id='journal-archives';journalArchivesButton.type='button';journalArchivesButton.textContent='所有终端记录（含归档）';journalArchivesButton.onclick=()=>{$('settings-dialog').close();openJournal('history',true);};$('settings-dialog').append(journalArchivesButton);
  $('journal-source-close').onclick=()=>{$('journal-source-dialog').close();journalSourceTarget=null;};
  $('journal-source-dialog').addEventListener('cancel',()=>{journalSourceTarget=null;});$('journal-source-load').onclick=loadJournalSources;
  $('journal-provider').onchange=()=>{$('journal-sources').replaceChildren();$('journal-source-status').textContent='提供方已变化，请重新查找。';};
  // JOURNAL_INTEGRATION_END
  // MANAGED_INTEGRATION_START: exact targets and durable receipts, never terminal stdin.
  const managedPendingRecords=new Map(),managedCreations=new Map();
  let managedController=null,managedContext=null,managedSnapshot=null,managedSetupVersion=0,managedPollTimer=null,managedOpeningJournal='';
  const managedTargetFields=['window_id','session_id','process_generation','journal_id'];
  const managedStates=new Set(['starting','ready','idle','running','waiting_permission','interrupting','closing','ended','error','unknown']);
  const managedActions=new Set(['create_managed','submit_managed','managed_status','interrupt_managed','respond_managed_permission','close_managed']);
  try{for(const row of JSON.parse(sessionStorage.getItem('sma.mobile.managed-pending')||'[]'))if(Array.isArray(row)&&typeof row[0]==='string'&&row[1]?.schema===1&&managedPendingRecords.size<32)managedPendingRecords.set(row[0],row[1]);}catch{}
  try{for(const row of JSON.parse(sessionStorage.getItem('sma.mobile.managed-creations')||'[]'))if(Array.isArray(row)&&typeof row[0]==='string'&&row[1]?.nonce&&managedCreations.size<32)managedCreations.set(row[0],{...row[1],phase:'unknown'});}catch{}
  function managedSession(){return selected().inventory.find(s=>s.id===active);}
  function isManagedSession(){return managedSession()?.interaction_mode==='managed';}
  function managedPending(){return !!managedSnapshot?.pending||!!managedSnapshot?.operation;}
  function submissionPending(){return composerSubmit.pending||isManagedSession()&&managedPending();}
  function managedID(value){return typeof value==='string'&&value.length>0&&value.length<=256&&!/[\x00-\x1f\x7f]/.test(value);}
  function managedNonce(){if(crypto.randomUUID)return crypto.randomUUID();const bytes=crypto.getRandomValues(new Uint8Array(16));return Array.from(bytes,x=>x.toString(16).padStart(2,'0')).join('');}
  function managedTarget(){
    const t=journalTarget(),s=managedSession();
    return {...t,window_id:s?.window,session_id:s?.id,process_generation:s?.process_generation,journal_id:s?.journal_id,interaction_mode:s?.interaction_mode||'native'};
  }
  function managedCurrent(t,requireManaged=true){
    if(!journalTargetCurrent(t)||!t.host.connected||t.host.capabilities?.managed_conversation!==1)return false;
    const s=t.host.inventory.find(s=>s.id===t.session_id&&s.window===t.window_id);
    return !!s&&s.process_generation===t.process_generation&&(!requireManaged||s.interaction_mode==='managed'&&s.journal_id===t.journal_id);
  }
  async function managedAuthKey(t){
    const bytes=encoder.encode(JSON.stringify([t.host.host.id,t.endpoint,t.room,t.token]));
    const digest=await crypto.subtle.digest('SHA-256',bytes);
    return Array.from(new Uint8Array(digest),x=>x.toString(16).padStart(2,'0')).join('');
  }
  function managedRecordKey(auth,t){return JSON.stringify([auth,...managedTargetFields.map(key=>t[key])]);}
  function saveManagedRecords(){
    try{sessionStorage.setItem('sma.mobile.managed-pending',JSON.stringify([...managedPendingRecords]));sessionStorage.setItem('sma.mobile.managed-creations',JSON.stringify([...managedCreations]));}
    catch{notice('独立对话待核对状态无法保存到本机；草稿已保留，请先核对后再刷新。');}
  }
  function rememberManagedPending(){
    if(!managedController||!managedContext)return;
    const record=managedController.exportPending();
    if(record)managedPendingRecords.set(managedContext.key,record);else managedPendingRecords.delete(managedContext.key);
    saveManagedRecords();
  }
  function releaseManagedContext(){
    managedSetupVersion++;clearTimeout(managedPollTimer);managedPollTimer=null;
    rememberManagedPending();managedController?.dispose();managedController=null;managedContext=null;managedSnapshot=null;managedOpeningJournal='';
    $('managed-panel').hidden=true;$('managed-permissions').replaceChildren();
  }
  function managedRPC(t,action,params){
    const creating=action==='create_managed';
    const feedback=action==='submit_cockpit_feedback'||action==='cockpit_feedback_status';
    if((feedback?typeof mobileFeedbackQa==='undefined'||!mobileFeedbackQa?.allowsTarget(t):!managedActions.has(action))||
      !managedCurrent(t,!creating)||creating&&t.interaction_mode==='managed')return Promise.reject(journalError('stale_target'));
    if(!creating&&!managedTargetFields.every(key=>params[key]===t[key]))return Promise.reject(journalError('stale_target'));
    return new Promise((resolve,reject)=>{
      const c=t.host,request_id='mobile-managed-'+(++journalRequestSequence),pending=c.journalPending||(c.journalPending=new Map());
      const finish=(error,result)=>{const entry=pending.get(request_id);if(!entry)return;pending.delete(request_id);clearTimeout(entry.timer);if(error)reject(error);else if(!managedCurrent(t,!creating))reject(journalError('stale_target'));else resolve(result);};
      const timer=setTimeout(()=>finish(journalError('timeout')),8000);pending.set(request_id,{timer,finish});
      try{if(!sendHost(c,24,{request_id,action,params}))finish(journalError('disconnected'));}catch{finish(journalError('disconnected'));}
    });
  }
  function renderManagedState(snapshot=managedSnapshot){
    if(!isManagedSession()){$('managed-panel').hidden=true;return;}
    $('managed-panel').hidden=false;
    if(term)term.options.disableStdin=true;
    const s=managedSession(),state=!selected().connected?'unknown':snapshot?.state||s.managed_state||'unknown';
    const labels={starting:'正在启动',ready:'可发送',idle:'可发送',running:'本轮进行中',waiting_permission:'等待工具权限',interrupting:'正在请求中断',closing:'正在结束',ended:'已结束',error:'记录或运行异常',unknown:'状态待核对',disposed:'状态待核对'};
    $('managed-state').textContent='独立对话记录 · '+(labels[state]||'状态待核对');
    const root=snapshot?.provider_root_id;
    $('managed-identity').textContent=[snapshot?.provider||s.provider||'',snapshot?.provider_version||'版本待核对',root?'已确认根会话 '+root:'根会话待确认','启动目录：'+String(s.cwd||s.launch_cwd||'待核对')+'（不是实时工作目录）'].filter(Boolean).join(' · ');
    const policy=snapshot?.policy;
    $('managed-policy').textContent='CLI 权限策略：'+(policy===null||policy===undefined?'待核对':typeof policy==='string'?policy:JSON.stringify(policy));
    const pending=snapshot?.pending;
    $('managed-message').textContent=!selected().connected?'连接已断开；原请求与草稿保留，重连后仅核对状态，不会自动重发。':pending?(pending.phase==='absent'?'已核对原请求未被接收；可明确重试同一请求。':pending.phase==='sending'?'正在等待持久接收凭据；草稿尚未清空。':'接收结果未知；保留原文和标识，请核对状态，不会自动重发。'):snapshot?.error?'状态或操作未确认；请核对状态。':snapshot?.receipt?'消息已持久接收（#'+snapshot.receipt.seq+'）；观察终端始终只读。':'消息只经结构化接口提交；观察终端始终只读。';
    const prior=snapshot?.last_outcome,note=$('managed-outcome');
    const outcomeNotes={failed:'上轮未完成，原输入已保留；请检查所选工具的接入配置与连接。',blocked:'上轮被阻止；输入记录已保留。',ambiguous:'上轮结果待核对；输入记录已保留。',interrupted:'上轮已中断；输入记录已保留。',not_dispatched:'上轮未派发；输入记录已保留。',cancelled:'上轮已取消；输入记录已保留。'};
    const outcomeText=prior?.outcome==='completed'?(prior.has_final===false?'上轮已结束，但未发现可确认结论；输入记录已保留。':''):outcomeNotes[prior?.outcome]||'';
    if(note.textContent!==outcomeText)note.textContent=outcomeText;note.hidden=!outcomeText;
    $('managed-retry').hidden=pending?.phase!=='absent';$('managed-retry').disabled=!managedController||!['ready','idle'].includes(state)||!!snapshot?.operation;
    const retryControl=$('managed-retry-control'),operation=snapshot?.operation;
    retryControl.hidden=operation?.phase!=='unknown';retryControl.disabled=!managedController||!connected;retryControl.managedOperation=operation?.phase==='unknown'?{...operation}:null;
    $('managed-reconcile').disabled=!managedController||!connected;
    $('managed-interrupt').disabled=!managedController||!snapshot?.active_submission||['interrupting','closing','ended','error'].includes(state)||!!snapshot?.operation;
    $('managed-close').disabled=!managedController||['closing','ended'].includes(state)||!!snapshot?.operation;
    const area=$('managed-permissions'),existing=new Map(Array.from(area.children).map(node=>[node.dataset.permissionId,node]));
    const live=new Set();
    for(const permission of snapshot?.permissions||[]){
      const id=permission.permission_request_id,signature=JSON.stringify(permission);live.add(id);
      if(existing.get(id)?.permissionSignature===signature)continue;
      if(existing.has(id)){existing.get(id).remove();existing.delete(id);}
      const card=document.createElement('section');card.className='managed-permission';card.dataset.permissionId=id;
      card.permissionSignature=signature;
      const title=document.createElement('strong');title.textContent=(permission.requires_user_interaction?'交互类型尚不支持，仅可拒绝':'实际 CLI 工具权限请求')+' · '+permission.tool_name;
      const detail=document.createElement('pre');detail.textContent=permission.details;
      const identity=document.createElement('p');identity.className='hint';identity.textContent='会话 '+s.id+' · 本轮 '+permission.submission_id+(permission.provider_turn_id?' · '+permission.provider_turn_id:'');
      card.append(title,identity,detail);
      const deny=document.createElement('button');deny.type='button';deny.dataset.decision='deny';deny.textContent='拒绝';
      const respond=decision=>{const controller=managedController,context=managedContext;if(!controller||!context||!managedCurrent(context.target))return;for(const button of card.querySelectorAll('button'))button.disabled=true;controller.respondPermission(id,decision).then(()=>{if(managedController===controller)controller.reconcile();});};
      deny.onclick=()=>respond('deny');card.append(deny);
      if(permission.choices.includes('allow_once')&&!permission.requires_user_interaction){const allow=document.createElement('button');allow.type='button';allow.dataset.decision='allow_once';allow.textContent='仅允许本次';allow.onclick=()=>respond('allow_once');card.append(allow);}
      area.append(card);
      // Default focus is deny; merely displaying or dismissing never grants permission.
      if(!document.querySelector('dialog[open]')&&!['TEXTAREA','INPUT'].includes(document.activeElement?.tagName))deny.focus({preventScroll:true});
    }
    for(const [id,node] of existing)if(!live.has(id))node.remove();
    $('model').disabled=true;$('ctrl').disabled=true;for(const button of document.querySelectorAll('[data-key]'))button.disabled=true;
    $('keys').hidden=$('terminal-actions').hidden=true;$('tools-toggle').textContent='原始画面';$('tools-toggle').title='查看只读观察画面';$('tools-toggle').setAttribute('aria-expanded','false');
  }
  function managedAccepted(t,key,receipt,record){
    if(!managedCurrent(t)||!managedTargetFields.every(field=>receipt[field]===t[field]))return;
    const draftKey=sessionKey(t.session_id,t.host.host.id),saved=drafts.get(draftKey);
    if(saved===record.text)drafts.delete(draftKey);
    if($('draft').value===record.text)syncDraftFields('');
    managedPendingRecords.delete(key);notice('');saveManagedRecords();
  }
  async function pollManaged(){
    const controller=managedController,context=managedContext;if(!controller||!context||!managedCurrent(context.target))return;
    try{await controller.reconcile();}finally{if(managedController===controller&&managedContext===context&&managedCurrent(context.target)){clearTimeout(managedPollTimer);managedPollTimer=setTimeout(pollManaged,2000);}}
  }
  async function syncManagedContext(){
    const s=managedSession();
    if(s?.interaction_mode!=='managed'){
      if(managedController||managedContext)releaseManagedContext();
      $('managed-panel').hidden=true;$('ctrl').disabled=false;for(const button of document.querySelectorAll('[data-key]'))button.disabled=false;
      if(typeof setToolsCollapsed==='function')setToolsCollapsed(localStorage.getItem('sma.mobile.tools-collapsed')!=='0',false);
      updateManagedCreation();restoreManagedCreation();return;
    }
    if(term)term.options.disableStdin=true;
    const t=managedTarget();renderManagedState();updateManagedCreation();
    if(managedContext&&managedTargetFields.every(key=>managedContext.target[key]===t[key])&&managedCurrent(managedContext.target)){
      const saved=managedPendingRecords.get(managedContext.key);if(saved&&!managedController.exportPending())managedController.restorePending(saved);
      renderManagedState();return;
    }
    if(!managedCurrent(t)||!managedTargetFields.every(key=>managedID(t[key]))){$('managed-message').textContent='此连接不支持独立对话控制，或目标代际尚未核对；观察终端保持只读。';return;}
    const version=++managedSetupVersion;
    try{
      const auth=await managedAuthKey(t);if(version!==managedSetupVersion||!managedCurrent(t))return;
      if(managedController)releaseManagedContext();
      const key=managedRecordKey(auth,t),target=Object.fromEntries(managedTargetFields.map(field=>[field,t[field]]));
      let controller;
      controller=C.createManagedController({target,targetKey:key,request:(action,params)=>managedRPC(t,action,params),isCurrent:()=>managedCurrent(t),
        onState:value=>{if(managedController!==controller)return;managedSnapshot=value;rememberManagedPending();renderManagedState(value);updateComposer();},
        onAccepted:(receipt,record)=>managedAccepted(t,key,receipt,record)});
      managedController=controller;managedContext={target:t,key};managedSnapshot=controller.snapshot();
      const saved=managedPendingRecords.get(key);if(saved)controller.restorePending(saved);
      renderManagedState();updateComposer();pollManaged();
      if(managedOpeningJournal!==t.journal_id){managedOpeningJournal=t.journal_id;refreshJournalStatus(true);}
    }catch{if(version===managedSetupVersion)$('managed-message').textContent='当前环境无法安全初始化独立对话控制；草稿已保留。';}
  }
  function submitManaged(){
    if(composing||uploadJob||attachmentsPending()||!managedController||!managedContext||!managedCurrent(managedContext.target)){notice('独立对话尚未就绪，或存在未处理附件；草稿已保留。');return false;}
    const snapshot=managedController.snapshot();if(!snapshot.canSubmit){renderManagedState(snapshot);return false;}
    if(!managedPendingRecords.has(managedContext.key)&&managedPendingRecords.size>=32){notice('待核对独立请求过多；请先核对已有请求，未发送新文本。');return false;}
    const text=$('draft').value;if(!text.trim())return false;
    if(encoder.encode(text).length>65536){notice('独立对话限 64 KiB UTF-8 原文；未截断或发送。');return false;}
    // The shared controller sets pending synchronously before its first await.
    managedController.submit(text);return true;
  }
  function mergeManagedCatalog(c,received){
    for(const [id,entry] of c.managedCreatedTargets||[]){
      if(entry.token!==hostTokens.get(c.host.id)){c.managedCreatedTargets.delete(id);continue;}
      const actual=received.find(s=>s.id===id);
      if(actual){c.managedCreatedTargets.delete(id);continue;}
      received.push({...entry.session});
    }
    return received;
  }
  function updateManagedCreation(){
    const managed=$('new-interaction-mode').value==='managed',supported=selected().capabilities?.managed_conversation===1,s=managedSession();
    $('shell').hidden=managed;$('managed-provider').hidden=!managed;$('managed-create-hint').hidden=!managed&&s?.interaction_mode!=='managed';
    const record=selected().managedCreation;
    if(managed){$('new').textContent=record?.phase==='unknown'?'核对创建结果':record?.phase==='sending'?'正在创建…':'＋ 新建独立对话';$('new').disabled=!connected||createPending||!supported||!s||s.interaction_mode==='managed'||!managedID(s.process_generation)||record?.phase==='sending';$('managed-provider').disabled=!!record;
      $('managed-create-hint').textContent=record?.phase==='unknown'?'创建结果未知；将使用原标识核对，不会另起一份会话。':!supported?'此主机尚不支持独立对话记录模式。':'以当前原始 CLI 的启动目录新建独立窗口；不会恢复或修改原会话。';
    }else{$('new').textContent='＋ 新建终端';$('new').disabled=!connected||createPending||s?.interaction_mode==='managed';$('managed-provider').disabled=false;if(s?.interaction_mode==='managed')$('managed-create-hint').textContent='请先选择原始 CLI 窗口；新终端不会加入独立对话窗口。';}
  }
  async function restoreManagedCreation(){
    const t=managedTarget();if(!managedCurrent(t,false)||t.interaction_mode==='managed')return;
    try{const auth=await managedAuthKey(t);if(!managedCurrent(t,false))return;const c=t.host,key=JSON.stringify([auth,t.session_id,t.process_generation]);
      if(c.managedCreation?.authKey&&c.managedCreation.authKey!==auth)c.managedCreation=null;
      if(!c.managedCreation&&managedCreations.has(key))c.managedCreation={...managedCreations.get(key),phase:'unknown'};
      updateManagedCreation();
    }catch{}
  }
  async function createManaged(){
    const t=managedTarget(),provider=$('managed-provider').value;
    if(!managedCurrent(t,false)||t.interaction_mode==='managed'||!managedID(t.process_generation)||!['claude','codex'].includes(provider)){notice('请先选择支持独立模式主机上的原始 CLI 会话。');return;}
    const c=t.host;if(createPending||c.managedCreation?.phase==='sending')return;
    let record=c.managedCreation;
    if(!record){record={nonce:managedNonce(),session_id:t.session_id,process_generation:t.process_generation,provider,phase:'sending'};c.managedCreation=record;}
    if(record.session_id!==t.session_id||record.process_generation!==t.process_generation){notice('另一个启动来源的创建结果尚未核对；请返回原来源核对。');return;}
    record.phase='sending';updateManagedCreation();
    try{
      const auth=await managedAuthKey(t);if(!managedCurrent(t,false))throw journalError('stale_target');
      if(record.authKey&&record.authKey!==auth)throw journalError('stale_target');
      const key=JSON.stringify([auth,record.session_id,record.process_generation]);
      const restored=managedCreations.get(key);if(restored&&restored.nonce!==record.nonce){record={...restored,phase:'sending'};c.managedCreation=record;}
      if(!managedCreations.has(key)&&managedCreations.size>=32)throw journalError('busy');record.authKey=auth;
      managedCreations.set(key,record);saveManagedRecords();
      const result=await managedRPC(t,'create_managed',{session_id:record.session_id,process_generation:record.process_generation,provider:record.provider,nonce:record.nonce});
      if(!managedCurrent(t,false)||!managedTargetFields.every(field=>managedID(result?.[field]))||result.interaction_mode!=='managed'||result.provider!==record.provider||!managedStates.has(result.state))throw journalError('invalid_response');
      const existing=c.inventory.find(s=>s.id===result.session_id);
      if(existing&&(existing.window!==result.window_id||existing.process_generation!==result.process_generation||existing.journal_id!==result.journal_id||existing.interaction_mode!=='managed'))throw journalError('invalid_response');
      const session=existing||{id:result.session_id,window:result.window_id,title:(result.provider==='claude'?'Claude':'Codex')+' · 独立对话记录',provider:result.provider,agent:result.provider,interaction_mode:'managed',managed_state:result.state,process_generation:result.process_generation,journal_id:result.journal_id,cwd:typeof result.cwd==='string'?result.cwd:''};
      if(!existing){c.inventory.push(session);(c.managedCreatedTargets||(c.managedCreatedTargets=new Map())).set(session.id,{session,token:t.token});}
      c.managedCreation=null;managedCreations.delete(key);saveManagedRecords();loadSelected();$('session-dialog').close();activate(result.session_id);renderList();
    }catch{if(c.managedCreation){c.managedCreation.phase='unknown';saveManagedRecords();}notice('独立会话创建结果未确认；保留原标识，请明确核对后再操作。');}
    finally{updateManagedCreation();}
  }
  $('new-interaction-mode').onchange=updateManagedCreation;
  $('managed-edit').onclick=()=>{modes.set(sessionKey(),'buffered');setMode(true);};
  $('managed-reconcile').onclick=()=>pollManaged();$('managed-retry').onclick=()=>managedController?.retry();
  $('managed-retry-control').onclick=()=>{
    const controller=managedController,captured=$('managed-retry-control').managedOperation,current=controller?.snapshot().operation;
    if(!controller||!managedContext||!managedCurrent(managedContext.target)||!captured||current?.phase!=='unknown'||current.nonce!==captured.nonce||current.action!==captured.action||current.permission_request_id!==captured.permission_request_id||current.decision!==captured.decision)return;
    if(captured.action==='respond_managed_permission')controller.respondPermission(captured.permission_request_id,captured.decision);
    else if(captured.action==='interrupt_managed')controller.interrupt();else if(captured.action==='close_managed')controller.closeSession();
  };
  $('managed-interrupt').onclick=()=>managedController?.interrupt();$('managed-close').onclick=()=>openTerminalAction('close');
  // MANAGED_INTEGRATION_END
  function stopSocket(c=selected(),preserveArchive=preserveJournalArchive(c)){
    if(terminalAction?.target.host===c)stopTerminalAction('连接已变化，请关闭后重新核对目标。');
    if(c===selected())mobileFeedbackQa?.invalidate();
    c.cockpitInstallationId='';
    if(pendingRefresh?.host===c)cancelRefresh();
    if(c===selected())releaseManagedContext();
    for(const entry of c.journalPending?.values()||[])entry.finish(journalError('disconnected'));if(!preserveArchive){closeJournalArchive(c);if(c===selected())resetJournalContext();}
    c.generation++;c.historyContexts.clear();clearTimeout(c.retryTimer);clearInterval(c.pingTimer);c.retryTimer=c.pingTimer=null;
    if(c.socket){c.socket.onclose=null;c.socket.close();c.socket=null;}c.connected=false;c.subscription='';
    if(c===selected()){composerSubmit.cancel('disconnected');connected=false;ready=false;pendingKeys=[];composing=false;setCtrl(false);clearTimeout(controlAction?.timer);controlAction=null;updateControlButton();if(term)term.options.disableStdin=true;if($('composer').open)updateAttachments();}
    if(c===selected()&&isManagedSession()){renderManagedState();updateComposer();}
  }
  function hostState(c,text){c.status=text;if(c===selected())state(text);renderHosts();renderList();}
  function subscribeSessions(c,refresh=''){
    // TD-006D/I: unopened terminals never subscribe to output. Activity dots use
    // received visible-terminal activity only; directory state is independent.
    const session=c===selected()?active:'';
    if(session&&restoringTerms.has(term)&&(terminalWrites.get(term)||0)>0){const current=term,generation=c.generation;settledSubscriptions.set(current,()=>{if(term===current&&selected()===c&&active===session&&c.generation===generation)subscribeSessions(c,refresh);});return;}
    const body={sessions:session?[session]:[]},cursor=session&&initializedTerms.has(term)&&streamPositions.get(term);
    if(cursor&&c.capabilities?.history_resume===1){if(restoringTerms.has(term))queuedPositions.set(term,cursor);body.history_after={[session]:{position:cursor.position,epoch:cursor.epoch,stream_id:cursor.stream_id}};}
    if((refresh||c.subscription!==session)&&sendHost(c,16,body))c.subscription=session;
  }
  let updateHint='',updateCheckPending=false;
  function receiveUpdateHint(message){
    const revision=C.updateHintRevision(message);
    if(!revision||revision===updateHint||updateCheckPending)return;
    updateHint=revision;
    if(!window.NativeUpdates?.check)return;
    updateCheckPending=true;Promise.resolve(window.NativeUpdates.check()).catch(()=>{}).finally(()=>{updateCheckPending=false;});
  }
  function connect(c=selected()){
    if(paused)return;const preserveArchive=preserveJournalArchive(c);stopSocket(c,preserveArchive);const gen=c.generation;c.lastPing=0;hostState(c,'正在连接');
    if(!roomAllowed(c.host)){hostState(c,'此房间未勾选，请返回连接设置重新选择');return;}
    if(c===selected()&&active&&!preserveArchive){const sid=active;drafts.set(sessionKey(),$('draft').value);disposeTerminal();active='';activate(sid);}
    let url;try{url=endpoint(c.host);}catch(e){hostState(c,e.message);return;}
    const ws=c.socket=new WebSocket(url);ws.binaryType='arraybuffer';metrics.connections++;
    const current=()=>gen===c.generation;
    const hello=()=>ws.send(C.pack(1,encoder.encode(JSON.stringify({token:hostTokens.get(c.host.id)||'',multiplex:true,reliable:true,history_context:1,history_resume:1,history_chunks:1,conversation_journal:1,managed_conversation:1}))));
    ws.onopen=()=>{if(!current())return;if(c.host.code)ws.send(JSON.stringify({op:'phone',code:c.host.code,token:hostTokens.get(c.host.id)||'',reliable:true}));else hello();};
    ws.onmessage=event=>{
      if(!current())return;
      try{
        if(typeof event.data==='string'){const m=JSON.parse(event.data);if(m.op==='ready')hello();else if(m.op==='ui_update')receiveUpdateHint(m);else if(m.error)hostState(c,String(m.error));return;}
        const {op,payload}=C.unpack(event.data);metrics.frames++;metrics.bytes+=payload.length;
        if(op===2||op===7)sync(JSON.parse(decoder.decode(payload)),c);
        else if(op===24){try{if(payload.length>196608)throw journalError('invalid_response');journalHostResponse(c,JSON.parse(decoder.decode(payload)));}catch{for(const entry of c.journalPending?.values()||[])entry.finish(journalError('invalid_response'));}}
        else if(op===3||op===8||op===23){
          if(!payload.length)return;const feed=op===23?M.historyPayload(payload):C.sessionPayload(payload);
          if(op===8&&(c.historyContexts.has(feed.session)||restoringTerms.has(term)&&c.capabilities?.history_resume===1&&c===selected()&&feed.session===active))return;
          if(op===23){if(c.capabilities?.history_context!==1)throw Error('Unnegotiated history context');const previous=c.historyContexts.get(feed.session);if(previous&&feed.context.epoch<previous.epoch)return;c.historyContexts.set(feed.session,feed.context);const lease=c.controls[feed.session];if(lease&&Number.isSafeInteger(lease.history_epoch)&&lease.history_epoch<feed.context.epoch){c.controls[feed.session]={...lease,owner:false,busy:true};if(c===selected()&&feed.session===active){control=false;updateControlButton();}}if(feed.context.kind==='snapshot'&&c===selected()){waitingFeeds=waitingFeeds.filter(item=>item.sid!==feed.session);waitingBytes=waitingFeeds.reduce((sum,item)=>sum+item.bytes.length,0);}}
          const context=feed.context||(c.capabilities?.history_context===1?c.historyContexts.get(feed.session):null),replay=op===8||op===23&&['snapshot','resume'].includes(context.kind);
          if(!replay&&feed.bytes.length&&!M.debugRecoveryFeed(feed.bytes))c.activity.set(feed.session,performance.now());
          if(c!==selected()||feed.session!==active||!writer)return;
          if(!context&&!controls[active]){if(waitingBytes+feed.bytes.length>524288){M.closeForRecovery(ws,'等待终端尺寸的输出积压');return;}waitingFeeds.push({sid:active,bytes:feed.bytes,replay});waitingBytes+=feed.bytes.length;}
          else consumeFeed(active,feed.bytes,replay,context,op===23);
        }else if(op===11){c.lastPing=0;hostState(c,'已连接');}
      }catch(e){stopSocket(c);hostState(c,'终端数据异常: '+e.message);}
    };
    ws.onerror=()=>{if(current())hostState(c,'网络不可用');};
    ws.onclose=event=>{if(!current())return;stopSocket(c);hostState(c,event.code===4001?'验证失败，请更新令牌':event.code===4004?'电脑离线 · 等待重连':'已断线 · 等待重连');if(!paused&&event.code!==4001)c.retryTimer=setTimeout(()=>connect(c),C.retryDelay(c.retry++));};
  }
  // Only explicit profiles grant directory access. Discovered rooms never become
  // credential roots, nor persist tokens or room lists across a UI navigation.
  function removeDiscovered(h){
    if(h.id===hostId)chooseHost('home');
    const c=connections.get(h.id);if(c)stopSocket(c);
    connections.delete(h.id);hostTokens.delete(h.id);hosts=hosts.filter(x=>x!==h);
  }
  function applyDirectory(w,message){
    if(!Array.isArray(message.hosts)||message.hosts.length>64||!Number.isSafeInteger(message.revision)||message.revision<0||message.revision<w.revision)return;
    w.revision=message.revision;
    const discovery=message.capabilities?.token_discovery===true,wanted=new Set();
    for(const entry of message.hosts.slice(0,64)){
      if(!entry||typeof entry.code!=='string'||!entry.code||entry.code.length>128)continue;
      if(!roomCodeAllowed(w.url,entry.code))continue;
      const known=w.known.filter(h=>h.code===entry.code);
      for(const h of known){const c=connection(h);c.directoryOnline=entry.online===true;if(entry.online===true){c.machine=String(entry.machine||'');c.project=String(entry.project||'');}}
      if(known.length||!discovery||entry.online!==true||!Number.isInteger(entry.token_index)||entry.token_index<0||entry.token_index>=w.tokens.length)continue;
      const id='discovered:'+JSON.stringify([w.url,entry.code]);
      // Match canonical transport identity, including migrated legacy profiles.
      if([home,...hosts].some(h=>!h.autoDirectory&&h.code===entry.code&&safeEndpoint(h)===w.url))continue;
      wanted.add(id);let h=hosts.find(h=>h.id===id);
      if(!h){if(hosts.length>=64)continue;const u=new URL(w.url);u.protocol=u.protocol==='wss:'?'https:':'http:';u.pathname=u.pathname.replace(/\/ws$/,'');h={id,name:entry.machine||entry.code,endpoint:u.toString().replace(/\/+$/,''),code:entry.code,autoDirectory:w.url,discoveryScope:w.signature};hosts.push(h);}
      const c=connection(h),token=w.tokens[entry.token_index],changed=hostTokens.get(id)!==token;
      setHostToken(id,token);h.name=String(entry.machine||entry.code);c.machine=String(entry.machine||'');c.project=String(entry.project||'');c.directoryOnline=true;
      if(!c.socket||changed)connect(c);
    }
    for(const h of [...hosts])if(h.autoDirectory===w.url&&!wanted.has(h.id))removeDiscovered(h);
    if(restoringUI){const h=hosts.find(h=>h.id===restoringUI.hostId);if(h){chooseHost(h.id,true);restoreSelected();}}
    loadSelected();renderHosts();renderList();
  }
  function safeEndpoint(h){try{return endpoint(h);}catch{return '';}}
  function requestDirectory(w){
    if(paused||watchers.get(w.url)!==w||w.socket?.readyState!==1)return;
    const delay=1000-(Date.now()-w.lastRequest);
    if(delay>0){if(!w.refreshTimer)w.refreshTimer=setTimeout(()=>{w.refreshTimer=null;requestDirectory(w);},delay);return;}
    w.lastRequest=Date.now();w.socket.send(JSON.stringify({op:'watch',rooms:w.known.map(h=>({code:h.code,token:hostTokens.get(h.id)})),tokens:w.tokens}));
  }
  function closeWatcher(w){clearTimeout(w.timer);clearTimeout(w.refreshTimer);w.socket.onclose=null;w.socket.close();}
  function refreshWatchers(){
    const groups=new Map();if(!paused)for(const h of [home,...hosts]){
      if(h.autoDirectory||!h.code||!hostTokens.get(h.id)||!roomAllowed(h))continue;
      const url=safeEndpoint(h);if(!url)continue;
      if(!groups.has(url))groups.set(url,[]);groups.get(url).push(h);
    }
    for(const [url,w] of watchers)if(!groups.has(url)){closeWatcher(w);watchers.delete(url);}
    for(const h of [...hosts])if(!paused&&h.autoDirectory&&!groups.has(h.autoDirectory))removeDiscovered(h);
    for(const [url,known] of groups){
      const tokens=[...new Set(known.map(h=>hostTokens.get(h.id)))].slice(0,32),signature=JSON.stringify(known.map(h=>[h.id,h.code,hostTokens.get(h.id)]));
      const existing=watchers.get(url);
      if(existing&&existing.signature===signature&&existing.socket.readyState<2){requestDirectory(existing);continue;}
      if(existing)closeWatcher(existing);
      // Remove grants from a previous credential scope before accepting a new snapshot.
      for(const h of [...hosts])if(h.autoDirectory===url&&h.discoveryScope!==signature)removeDiscovered(h);
      const socket=new WebSocket(url),w={url,known,tokens,signature,socket,timer:null,refreshTimer:null,lastRequest:0,revision:-1};watchers.set(url,w);
      socket.onopen=()=>requestDirectory(w);
      socket.onmessage=e=>{if(paused||watchers.get(url)!==w||typeof e.data!=='string'||e.data.length>1048576)return;try{const message=JSON.parse(e.data);if(message.op==='directory')applyDirectory(w,message);}catch{}};
      socket.onclose=e=>{if(watchers.get(url)!==w)return;for(const h of [...hosts])if(h.autoDirectory===url)removeDiscovered(h);renderHosts();renderList();if(!paused&&e.code!==4000&&e.code!==4001)w.timer=setTimeout(refreshWatchers,10000);};socket.onerror=()=>{};
    }
  }
  function consumeFeed(sid,bytes,replay,context=null,producerFrame=false){
    if(sid!==active||!writer||!context&&!controls[sid])return;
    const current=term,c=selected(),generation=c.generation;
    if(producerFrame&&context?.v!==2||!producerFrame&&c.capabilities?.history_resume!==1){streamPositions.delete(current);queuedPositions.delete(current);}
    const grid=context?{cols:context.cols,rows:context.rows,epoch:context.epoch}:{...controls[sid]},gridKey=grid.cols+'x'+grid.rows+':'+(context?.epoch??'legacy')+':'+(context?.stream_id||'');
    const resume=replay&&context?.kind==='resume',snapshot=replay&&!resume,tracked=producerFrame&&context?.v===2;
    const known=queuedPositions.get(current)||streamPositions.get(current),sameStream=!context?.stream_id||!known||context.stream_id===known.stream_id;
    if(tracked&&!snapshot){
      const start=context.end_position-bytes.length;
      if(start<0||resume&&(!initializedTerms.has(current)||!known||!sameStream||known.epoch!==context.epoch)||known&&sameStream&&known.epoch===context.epoch&&start>known.position||known&&!sameStream){ready=false;current.options.disableStdin=true;refreshScreen();return;}
      if(known&&sameStream&&known.epoch===context.epoch)bytes=bytes.subarray(Math.max(0,Math.min(bytes.length,known.position-start)));
    }
    if(tracked){grid.producer=true;grid.stream_id=context.stream_id;grid.end_position=context.end_position;if(!snapshot)grid.position_start=context.end_position-bytes.length;queuedPositions.set(current,{position:Math.max(context.end_position,sameStream&&known?.epoch===context.epoch?known?.position||0:0),epoch:context.epoch,stream_id:context.stream_id});}
    grid.boundaryKind=snapshot?'snapshot':resume?'resume':'';
    const candidate=pendingRefresh||followReplay;
    const refresh=replay&&candidate?.host===c&&candidate.sid===sid&&candidate.generation===generation&&candidate.terminal===current?candidate:null;
    const previous=replayFeeds.get(current);
    const identical=snapshot&&initializedTerms.has(current)&&replayGrids.get(current)===gridKey&&previous?.length===bytes.length&&bytes.every((value,index)=>value===previous[index]);
    if(snapshot){replayFeeds.set(current,bytes.length<=262144?bytes.slice():null);replayGrids.set(current,gridKey);}
    else if(previous&&previous.length+bytes.length<=262144){const combined=new Uint8Array(previous.length+bytes.length);combined.set(previous);combined.set(bytes,previous.length);replayFeeds.set(current,combined);}
    else replayFeeds.set(current,null);
    if(replay){if(ready&&!replaying&&(current.cols!==grid.cols||current.rows!==grid.rows||Number.isSafeInteger(grid.epoch)&&drawEpochs.has(current)&&grid.epoch!==drawEpochs.get(current)))captureGridHistory(current);
      if(c.capabilities?.history_chunks===1&&context?.kind==='snapshot'&&Number.isSafeInteger(controls[sid]?.history_end))chunkedReplay=context.end_position<controls[sid].history_end;
      replaying++;ready=false;}
    let reset=snapshot&&!identical;
    if(tracked){const frame=C.historyFrameBytes(context,bytes,known);if(!frame){ready=false;current.options.disableStdin=true;refreshScreen();return;}bytes=frame.bytes;reset=reset&&frame.reset;}
    let history=reset?M.safeHistoryReplay(C.snapshotBytes(bytes,sid,c.capabilities?.snapshot_sgr===true),grid,context?.partial===true):null;
    if(history&&initializedTerms.has(current)&&sameStream&&current.cols===grid.cols&&current.rows===grid.rows){const baseline=M.safeHistoryReplay(C.snapshotBytes(bytes,sid,c.capabilities?.snapshot_sgr===true),grid,context?.partial===true,true);if(baseline.baseline){history=baseline;reset=false;}else captureGridHistory(current);}
    grid.boundaryKind=reset?'snapshot':replay?'resume':'';
    if(history){let quality=history.partial?'较早历史或屏幕状态不完整；已保留本机已收到的记录。':'';if(history.recovered&&reset)quality='窗口尺寸曾变化，较早历史不完整；已从完整重绘恢复。';if(restoringTerms.has(current))quality=history.partial?'本机历史已保留；离开期间的较早回放可能不完整。':'本机历史已保留；已重新同步当前画面。';if(quality)c.historyQuality.set(sid,quality);else c.historyQuality.delete(sid);updateHistoryQuality();}
    // A byte-identical authorized replay is already rendered. Avoid resetting
    // its scrollback while still acknowledging the server snapshot in order.
    writer.push(identical?new Uint8Array():history?history.bytes:bytes,reset,()=>{
      if(sid!==active||term!==current||selected()!==c||c.generation!==generation)return;
      if(replay){
        replaying=Math.max(0,replaying-1);
        if(chunkedReplay&&context?.kind==='resume'&&!bytes.length&&context.end_position>=controls[sid]?.history_end)chunkedReplay=false;
        ready=connected&&replaying===0&&!chunkedReplay;term.options.disableStdin=!ready||isManagedSession();
        if(refresh&&(pendingRefresh===refresh||followReplay===refresh)){
          const buffer=term.buffer.active;if(buffer.type==='normal')term.scrollToLine(Math.max(0,buffer.baseY-refresh.distance));
          localView().bottom=refresh.bottom;const screen=$('screen');screen.scrollLeft=refresh.left;screen.scrollTop=refresh.bottom?screen.scrollHeight:refresh.top;
          if(followReplay===refresh){clearTimeout(refresh.timer);followReplay=null;}
          else{cancelRefresh();$('refresh-screen').textContent='\u5df2\u5237\u65b0';refreshFeedbackTimer=setTimeout(()=>{if(term===current&&!pendingRefresh)$('refresh-screen').textContent='\u5237\u65b0';},3000);}
        }else if(ready){if(localView().following!==false){term.scrollToBottom();localBottom();}if(!isManagedSession())replayNotice(c.historyQuality.get(sid)||(bytes.length?'\u6709\u9650\u5386\u53f2\u5df2\u6062\u590d\uff1b\u5168\u5c4f\u7a0b\u5e8f\u53ef\u80fd\u9700\u8981\u81ea\u884c\u5237\u65b0':''));}
      }
      if(replay){restoreAttachmentEditor();if($('composer').open||attachmentDrafts.has(sessionKey()))updateComposer();}
      captureScreenHistory();updateControlButton();updateTerminalStatus();historyStatus();scheduleFit();
    },grid);
  }
  function updateHistoryQuality(){const text=selected().historyQuality.get(active)||'';$('history-quality').textContent=text;$('history-quality').hidden=!text;}
  function sync(info,c=selected()){
    if(!info.capabilities?.multiplex){stopSocket(c);hostState(c,'后台版本不兼容');return;}
    if(!c.connected){c.connected=true;c.retry=0;c.status='已连接';c.pingTimer=setInterval(()=>{if(c.lastPing&&performance.now()-c.lastPing>30000){M.closeForRecovery(c.socket,'连接探测超时');return;}if(!c.lastPing)c.lastPing=performance.now();sendHost(c,10,encoder.encode('mobile'));},15000);}
    if(info.error)c.status=String(info.error);c.capabilities=info.capabilities||c.capabilities;
    if(info.windows||Object.hasOwn(info,'host_installation_id'))c.cockpitInstallationId=typeof info.host_installation_id==='string'?info.host_installation_id:'';
    if(info.controls)c.controls=info.controls;
    if(info.machine!==undefined)c.machine=String(info.machine||'');if(info.project!==undefined)c.project=String(info.project||'');
    if(info.groups)c.groups=info.groups;
    if(typeof info.selected==='string')c.serverWindow=info.selected;if(typeof info.session==='string')c.serverSession=info.session;
    if(info.windows){const receivedAt=performance.now();c.inventory=mergeManagedCatalog(c,info.windows.flatMap(w=>(w.sessions||[]).map(s=>({...s,reportedOutputAt:Number.isSafeInteger(s.output_age_ms)&&s.output_age_ms>=0?receivedAt-s.output_age_ms:undefined,learnedAgent:c.agents.get(s.id)||'',title:w.title,window:w.id,windowFocused:w.focused,group:w.group}))));}
    observeTerminalAction(c,info);
    if(c===selected())mobileFeedbackQa?.checkBinding();
    if(info.windows){const ids=new Set(c.inventory.map(s=>s.id));for(const cache of [c.activity,c.agents])for(const sid of cache.keys())if(!ids.has(sid))cache.delete(sid);for(const [key,draft] of attachmentDrafts)if(draft.target.host===c&&!ids.has(draft.target.session)){if(uploadJob?.target.key===key)cancelAttachmentUpload();attachmentDrafts.delete(key);}}
    if(c!==selected()){renderHosts();renderList();return;}
    loadSelected();renderHosts();renderList();
    if(c.createPending){const created=inventory.find(s=>!c.beforeCreate.has(s.id));if(created){c.createPending=false;createPending=false;activate(created.id);}}
    restoreSelected();
    if(!inventory.some(s=>s.id===active)){if(inventory.length)activate(inventory[0].id);else{disposeTerminal();active='';}}
    if(controls[active]){const size=controls[active],context=c.historyContexts.get(active),stale=context&&Number.isSafeInteger(size.history_epoch)&&size.history_epoch<context.epoch;control=!stale&&!!size.owner;if(stale)controls[active]={...size,owner:false,busy:true};if(controlAction&&control===(controlAction.mode==='take')){clearTimeout(controlAction.timer);controlAction=null;}updateControlButton();if(!stale&&term&&(term.cols!==size.cols||term.rows!==size.rows))writer?.push(new Uint8Array(),false,scheduleFit,size);scheduleFit();}
    if(controls[active]&&waitingFeeds.length){const feeds=waitingFeeds;waitingFeeds=[];waitingBytes=0;for(const feed of feeds)consumeFeed(feed.sid,feed.bytes,feed.replay);}
    subscribeSessions(c);refreshAgentMode();refreshJournalStatus();syncManagedContext();
  }
  function updateSessionDots(){
    const labels={refreshing:'正在刷新',waiting:'等待输入（暂未收到新输出）',disconnected:'已断开'};
    for(const row of document.querySelectorAll('#session-list .session')){
      const c=connections.get(row.dataset.host);if(!c)continue;
      const status=M.sessionState(c.connected,c.activity.get(row.dataset.session),performance.now());
      row.dataset.state=status;row.title=labels[status];
      row.setAttribute('aria-label',row.dataset.name+' · '+row.dataset.agent+' · '+labels[status]);
    }
  }
  let activityTimer=null,lastListSignature='';
  function startActivityTimer(){clearInterval(activityTimer);activityTimer=null;if(!paused&&$('session-dialog').open)activityTimer=setInterval(updateSessionDots,500);}
  function renderList(){
    updateTerminalActionButtons();
    const signature=JSON.stringify([hostId,active,connected,createPending,$('session-search').value,[...connections.values()].map(c=>[c.host.id,c.host.name,c.connected,c.status,c.machine,c.project,c.groups,c.inventory.map(s=>[s.id,s.title,s.shell,s.cwd,s.group,s.window,s.agent,s.learnedAgent,s.command,s.argv])])]);
    if(signature===lastListSignature){metrics.listSkips++;return;}lastListSignature=signature;metrics.listBuilds++;
    $('session-list').replaceChildren();const query=$('session-search').value.trim().toLocaleLowerCase();let count=0;
    const projects=new Map();for(const c of connections.values()){if(!c.connected&&!c.inventory.length)continue;const project=c.project||'未分配项目';if(!projects.has(project))projects.set(project,[]);projects.get(project).push(c);}
    for(const [project,computers] of projects){const ph=document.createElement('h3');ph.textContent=project;$('session-list').append(ph);
      for(const c of computers){const heading=document.createElement('h4');heading.textContent=(c.machine||c.host.name)+' · '+c.status;$('session-list').append(heading);const groups=new Map();
        for(const s of c.inventory){count++;if(![s.title,s.shell,s.cwd,c.machine,c.project].join(' ').toLocaleLowerCase().includes(query))continue;const key=s.group||s.window||s.title||'窗口';if(!groups.has(key))groups.set(key,[]);groups.get(key).push(s);}
        for(const sessions of groups.values()){
          const group=document.createElement('p');group.className='window-heading';group.textContent=c.groups.find(g=>g.id===sessions[0].group)?.name||sessions[0].title||'窗口';$('session-list').append(group);
          for(const s of sessions){const b=document.createElement('button');b.className='session';b.dataset.host=c.host.id;b.dataset.session=s.id;b.dataset.name=s.title||'未命名窗口';b.dataset.agent=M.agentType(s);b.setAttribute('aria-label',b.dataset.name+'（'+b.dataset.agent+'）');b.setAttribute('aria-current',String(c.host.id===hostId&&s.id===active));const dot=document.createElement('i');dot.className='session-dot';dot.setAttribute('aria-hidden','true');const name=document.createElement('span');name.className='session-name';name.textContent=b.dataset.name;const type=document.createElement('small');type.className='session-agent';type.textContent='（'+b.dataset.agent+'）';b.append(dot,name,type);b.onclick=()=>{chooseHost(c.host.id);activate(s.id);$('session-dialog').close();};$('session-list').append(b);}
        }
      }
    }
    $('session-count').textContent=count+' 个终端';$('new').disabled=!connected||createPending;const current=inventory.find(s=>s.id===active);if(current)$('title').textContent=current.title+'（'+M.agentType(current,current.title)+'）';updateManagedCreation();
    updateSessionDots();scheduleDebugGrid();
  }
  function debugEntries(){
    const entries=new Map();
    for(const c of connections.values()){
      const relay=safeEndpoint(c.host);if(!c.host.code||!relay||!roomAllowed(c.host))continue;
      for(const session of c.inventory){
        const key=JSON.stringify([relay,c.host.code,session.id]),previous=entries.get(key);
        if(!debugFirstSeen.has(key))debugFirstSeen.set(key,debugNextOrder++);
        const token=hostTokens.get(c.host.id),local=c.activity.get(session.id);let observed=debugActivity.get(key);
        if(c.connected&&session.reportedOutputAt!==undefined&&(!observed||observed.token!==token||session.reportedOutputAt>observed.lastOutput)){observed={token,lastOutput:session.reportedOutputAt,updatedAt:performance.timeOrigin+session.reportedOutputAt};debugActivity.set(key,observed);}
        if(c.connected&&local!==undefined&&(!observed||observed.token!==token||local>observed.lastOutput)){observed={token,lastOutput:local,updatedAt:performance.timeOrigin+local};debugActivity.set(key,observed);}
        const valid=!!observed&&observed.token===token,lastOutput=valid?observed.lastOutput:undefined,updatedAt=valid?observed.updatedAt:0;
        if(!previous||!previous.host.connected)entries.set(key,{key,host:c,session,active:c.connected&&lastOutput!==undefined&&performance.now()-lastOutput<1800,updatedAt,order:debugFirstSeen.get(key),observed:lastOutput!==undefined});
      }
    }
    for(const key of debugActivity.keys())if(!entries.has(key))debugActivity.delete(key);
    for(const key of debugFirstSeen.keys())if(!entries.has(key))debugFirstSeen.delete(key);
    return [...entries.values()];
  }
  function noteDebugActivity(host,sid,bytes){
    if(!bytes.length||M.debugRecoveryFeed(bytes))return;
    const key=JSON.stringify([safeEndpoint(host.host),host.host.code,sid]),now=performance.now();
    debugActivity.set(key,{token:hostTokens.get(host.host.id),lastOutput:now,updatedAt:performance.timeOrigin+now});host.activity.set(sid,now);
  }
  function stopDebugActivityMonitor(monitor){
    monitor.dead=true;clearTimeout(monitor.retryTimer);clearInterval(monitor.pingTimer);
    if(monitor.socket){monitor.socket.onclose=null;monitor.socket.close();}
  }
  function syncDebugActivityMonitors(entries){
    const groups=new Map();
    for(const item of entries){const id=item.host.host.id;if(!groups.has(id))groups.set(id,{host:item.host,ids:[]});groups.get(id).ids.push(item.session.id);}
    const wanted=new Map();
    for(const [hostId,group] of groups)for(let at=0;at<group.ids.length;at+=256){const ids=group.ids.slice(at,at+256).sort(),key=hostId+':'+at;wanted.set(key,{...group,ids,signature:JSON.stringify([safeEndpoint(group.host.host),group.host.host.code,hostTokens.get(hostId),ids])});}
    for(const [key,monitor] of debugActivityMonitors)if(wanted.get(key)?.signature!==monitor.signature){stopDebugActivityMonitor(monitor);debugActivityMonitors.delete(key);}
    for(const [key,group] of wanted){
      if(debugActivityMonitors.has(key))continue;
      const monitor={signature:group.signature,endpoint:group.host.host.endpoint,code:group.host.host.code,token:hostTokens.get(group.host.host.id),dead:false,socket:null,retry:0,retryTimer:null,pingTimer:null};debugActivityMonitors.set(key,monitor);
      const valid=()=>!monitor.dead&&!paused&&$('debug-dialog').open&&debugActivityMonitors.get(key)===monitor&&monitor.endpoint===group.host.host.endpoint&&monitor.code===group.host.host.code&&monitor.token===hostTokens.get(group.host.host.id);
      function connectActivity(){
        if(!valid())return;let socket;try{socket=new WebSocket(endpoint(group.host.host));}catch{return;}
        monitor.socket=socket;socket.binaryType='arraybuffer';let subscribed=false,lastPing=0;
        const current=()=>valid()&&monitor.socket===socket;
        const send=(op,value)=>{if(current()&&socket.readyState===1)socket.send(C.pack(op,value instanceof Uint8Array?value:encoder.encode(JSON.stringify(value))));};
        const hello=()=>send(1,{token:hostTokens.get(group.host.host.id)||'',multiplex:true,reliable:true,history_context:1});
        socket.onopen=()=>{if(current())socket.send(JSON.stringify({op:'phone',code:group.host.host.code,token:hostTokens.get(group.host.host.id)||'',reliable:true}));};
        socket.onmessage=event=>{
          if(!current())return;
          try{
            if(typeof event.data==='string'){const value=JSON.parse(event.data);if(value.op==='ready')hello();else if(value.error)socket.close(4000,'活动监测不可用');return;}
            const {op,payload}=C.unpack(event.data);
            if(op===2||op===7){const info=JSON.parse(decoder.decode(payload));if(!info.capabilities?.multiplex){socket.close(4000,'活动监测协议不兼容');return;}monitor.retry=0;if(!subscribed){subscribed=true;send(16,{sessions:group.ids,replay:false});}}
            else if(op===3||op===23){const feed=op===23?M.historyPayload(payload):C.sessionPayload(payload);if(op===23&&feed.context.kind!=='output')return;if(group.ids.includes(feed.session)){noteDebugActivity(group.host,feed.session,feed.bytes);scheduleDebugGrid();}}
            else if(op===11)lastPing=0;
          }catch{socket.close(4000,'活动监测数据异常');}
        };
        socket.onerror=()=>{};
        socket.onclose=event=>{clearInterval(monitor.pingTimer);if(current()&&event.code!==4001)monitor.retryTimer=setTimeout(connectActivity,C.retryDelay(monitor.retry++));};
        clearInterval(monitor.pingTimer);monitor.pingTimer=setInterval(()=>{if(!current()||!subscribed)return;if(lastPing&&performance.now()-lastPing>30000){socket.close(4000,'活动监测超时');return;}if(!lastPing)lastPing=performance.now();send(10,encoder.encode('mobile-debug-activity'));},15000);
      }
      connectActivity();
    }
  }
  function stopDebugMonitor(monitor){
    monitor.dead=true;clearTimeout(monitor.retryTimer);clearInterval(monitor.pingTimer);
    if(monitor.socket){monitor.socket.onclose=null;monitor.socket.close();}
    for(const preview of monitor.previews.values())preview.dispose();
  }
  function stopDebugMonitors(){
    for(const monitor of debugMonitors.values())stopDebugMonitor(monitor);
    debugMonitors.clear();
  }
  function startDebugMonitor(host,items){
    // A separate read-only client keeps the visible terminal's subscriptions,
    // resize lease, drafts and history position entirely untouched.
    const monitor={host,items,endpoint:host.host.endpoint,code:host.host.code,token:hostTokens.get(host.host.id),socket:null,previews:new Map(),prompts:new Map(),activity:new Map(),dead:false,ready:false,failed:false,retry:0,retryTimer:null,pingTimer:null};
    debugMonitors.set(host.host.id,monitor);
    const valid=()=>!monitor.dead&&!paused&&$('debug-dialog').open&&monitor.endpoint===host.host.endpoint&&monitor.code===host.host.code&&monitor.token===hostTokens.get(host.host.id);
    function connectPreview(){
      if(!valid())return;
      monitor.ready=false;monitor.failed=false;monitor.lastPing=0;
      monitor.contexts=new Map();
      for(const preview of monitor.previews.values())preview.dispose();monitor.previews.clear();
      for(const item of items)monitor.previews.set(item.session.id,M.createDebugPreview({
        createTerminal:options=>{const t=new Terminal(options);t.loadAddon(new Unicode11Addon.Unicode11Addon());t.unicode.activeVersion='11';return t;},
        onText:text=>{if(valid()){monitor.prompts.set(item.session.id,text);scheduleDebugGrid();}},
        onError:()=>{if(valid())monitor.socket?.close(4000,'调试输出积压');}
      }));
      let socket;try{socket=new WebSocket(endpoint(host.host));}catch{monitor.failed=true;scheduleDebugGrid();return;}
      monitor.socket=socket;socket.binaryType='arraybuffer';let subscribed=false;
      const current=()=>valid()&&monitor.socket===socket;
      const sendPreview=(op,value)=>{if(current()&&socket.readyState===1)socket.send(C.pack(op,value instanceof Uint8Array?value:encoder.encode(JSON.stringify(value))));};
      const hello=()=>sendPreview(1,{token:hostTokens.get(host.host.id)||'',multiplex:true,reliable:true,history_context:1});
      socket.onopen=()=>{if(!current())return;socket.send(JSON.stringify({op:'phone',code:host.host.code,token:hostTokens.get(host.host.id)||'',reliable:true}));};
      socket.onmessage=event=>{
        if(!current())return;
        try{
          if(typeof event.data==='string'){const message=JSON.parse(event.data);if(message.op==='ready')hello();else if(message.error)socket.close(4000,'调试连接不可用');return;}
          const {op,payload}=C.unpack(event.data);
          if(op===2||op===7){
            const info=JSON.parse(decoder.decode(payload));if(!info.capabilities?.multiplex){socket.close(4000,'调试协议不兼容');return;}
            monitor.preservesStyle=info.capabilities.snapshot_sgr===true;monitor.historyContext=info.capabilities.history_context===1;monitor.ready=true;monitor.retry=0;
            for(const item of items){const size=info.controls?.[item.session.id],context=monitor.contexts.get(item.session.id);if(size&&!(context&&Number.isSafeInteger(size.history_epoch)&&size.history_epoch<context.epoch))monitor.previews.get(item.session.id)?.setGrid(size);}
            if(!subscribed){subscribed=true;sendPreview(16,{sessions:items.map(item=>item.session.id)});}
          }else if(op===3||op===8||op===23){
            const feed=op===23?M.historyPayload(payload):C.sessionPayload(payload),preview=monitor.previews.get(feed.session);if(!preview)return;
            if(op===8&&monitor.contexts.has(feed.session))return;
            if(op===23){if(!monitor.historyContext)throw Error('Unnegotiated history context');const previous=monitor.contexts.get(feed.session);if(previous&&previous.epoch>feed.context.epoch)return;monitor.contexts.set(feed.session,feed.context);preview.setGrid(feed.context);}
            const context=feed.context||monitor.contexts.get(feed.session),replay=op===8||op===23&&context.kind==='snapshot';
            if(!replay&&feed.bytes.length){if(!M.debugRecoveryFeed(feed.bytes))monitor.activity.set(feed.session,performance.now());noteDebugActivity(host,feed.session,feed.bytes);}
            const grid=context||monitor.host.controls[feed.session],bytes=replay?C.snapshotBytes(feed.bytes,feed.session,monitor.preservesStyle):feed.bytes;
            preview.push(replay&&grid?M.safeHistoryReplay(bytes,grid,context?.partial===true).bytes:bytes,replay);
          }else if(op===11)monitor.lastPing=0;
          scheduleDebugGrid();
        }catch{socket.close(4000,'调试数据异常');}
      };
      socket.onerror=()=>{if(current()){monitor.failed=true;scheduleDebugGrid();}};
      socket.onclose=()=>{
        if(!current())return;monitor.ready=false;monitor.failed=true;clearInterval(monitor.pingTimer);
        for(const preview of monitor.previews.values())preview.dispose();monitor.previews.clear();scheduleDebugGrid();
        monitor.retryTimer=setTimeout(connectPreview,C.retryDelay(monitor.retry++));
      };
      clearInterval(monitor.pingTimer);monitor.pingTimer=setInterval(()=>{
        if(!current()||!monitor.ready)return;
        if(monitor.lastPing&&performance.now()-monitor.lastPing>30000){socket.close(4000,'调试连接超时');return;}
        if(!monitor.lastPing)monitor.lastPing=performance.now();sendPreview(10,encoder.encode('mobile-debug'));
      },15000);
    }
    connectPreview();
  }
  function createDebugCard(item){
    const tile=document.createElement('button');tile.className='debug-tile';tile.dataset.key=item.key;
    const name=document.createElement('strong');name.className='debug-tile-name';
    const host=document.createElement('span');host.className='debug-tile-host';
    const state=document.createElement('span');state.className='debug-tile-state';
    const dot=document.createElement('i');dot.className='session-dot';dot.setAttribute('aria-hidden','true');
    const status=document.createElement('span');state.append(dot,status);
    const prompt=document.createElement('div');prompt.className='debug-tile-prompt';
    const label=document.createElement('small');label.textContent='最新终端提示';
    const marquee=document.createElement('div');marquee.className='debug-marquee';
    const track=document.createElement('div');track.className='debug-marquee-track';
    const text=document.createElement('span'),repeat=document.createElement('span');repeat.setAttribute('aria-hidden','true');track.append(text,repeat);marquee.append(track);prompt.append(label,marquee);tile.append(name,host,state,prompt);
    tile.onclick=()=>{const current=debugCards.get(item.key)?.item;if(!current||!connections.get(current.host.host.id)?.inventory.some(s=>s.id===current.session.id))return;$('debug-dialog').close();chooseHost(current.host.host.id);activate(current.session.id);};
    $('debug-grid').append(tile);debugCards.set(item.key,{item,tile,name,host,status,marquee,text,repeat});
  }
  function scheduleDebugGrid(){
    if(paused||!$('debug-dialog').open||debugFrame!==null)return;
    debugFrame=requestAnimationFrame(()=>{debugFrame=null;renderDebugGrid();});
  }
  function renderDebugGrid(){
    if(paused||!$('debug-dialog').open)return;
    const entries=debugEntries(),byKey=new Map(entries.map(item=>[item.key,item])),now=performance.now();syncDebugActivityMonitors(entries);
    if(!debugPointer&&now>=debugInteractionUntil&&(now-debugOrderAt>=1000||!debugOrder.length)){debugOrder=M.debugSortEntries(entries).map(item=>item.key);debugOrderAt=now;}
    else{debugOrder=debugOrder.filter(key=>byKey.has(key));for(const item of entries)if(!debugOrder.includes(item.key))debugOrder.push(item.key);}
    const page=M.debugGridPage(debugOrder.map(key=>byKey.get(key)).filter(Boolean),debugPage,debugCapacity);debugPage=page.page;debugPages=page.pages;
    const signature=JSON.stringify([debugPage,debugCapacity,page.items.map(item=>[item.key,item.host.host.id])]);
    if(signature!==debugSignature){
      if(debugPointer)return;
      debugSignature=signature;const keys=new Set(page.items.map(item=>item.key));
      for(const [key,card] of debugCards)if(!keys.has(key)){card.tile.remove();debugCards.delete(key);}
      const groups=new Map();for(const item of page.items){if(!debugCards.has(item.key))createDebugCard(item);debugCards.get(item.key).item=item;$('debug-grid').append(debugCards.get(item.key).tile);const id=item.host.host.id;if(!groups.has(id))groups.set(id,[]);groups.get(id).push(item);}
      for(const [id,monitor] of debugMonitors){const items=groups.get(id);if(!items||monitor.host!==items[0].host||monitor.endpoint!==items[0].host.host.endpoint||monitor.code!==items[0].host.host.code||monitor.token!==hostTokens.get(id)||items.map(item=>item.session.id).sort().join('\0')!==monitor.items.map(item=>item.session.id).sort().join('\0')){stopDebugMonitor(monitor);debugMonitors.delete(id);}}
      for(const [id,items] of groups)if(!debugMonitors.has(id))startDebugMonitor(items[0].host,items);
    }
    $('debug-grid').style.setProperty('--debug-columns',String(Math.sqrt(debugCapacity)));$('debug-grid').dataset.density=String(debugCapacity);
    $('debug-count').textContent='共 '+page.total+' 个终端';$('debug-page').textContent=(page.page+1)+' / '+page.pages;
    $('debug-prev').disabled=page.page===0;$('debug-next').disabled=page.page===page.pages-1;
    $('debug-empty').hidden=page.total>0;$('debug-grid').hidden=!page.total;
    for(const item of page.items){
      const card=debugCards.get(item.key),monitor=debugMonitors.get(item.host.host.id),sid=item.session.id;
      if(!card)continue;card.item=item;
      const state=monitor?.failed?'disconnected':!monitor?.ready?'connecting':item.active?'refreshing':'waiting';
      const label={refreshing:'正在刷新',waiting:item.observed?'暂无新输出':'等待观测',disconnected:'已断开',connecting:'连接中'}[state];
      const name=item.session.title||'未命名窗口',host=item.host.machine||item.host.host.name;
      card.name.textContent=name;card.name.title=name;card.host.textContent=host+' · '+M.agentType(item.session);card.host.title=card.host.textContent;
      card.tile.dataset.state=state;card.status.textContent=label;card.tile.setAttribute('aria-current',String(item.host===selected()&&sid===active));
      const prompt=monitor?.prompts.get(sid)||(state==='disconnected'?'连接断开，提示暂不可用':monitor?.prompts.has(sid)?'暂无提示文本':'正在读取终端提示…');
      if(card.text.textContent!==prompt){card.text.textContent=prompt;card.repeat.textContent=prompt;card.marquee.title=prompt;}
      card.marquee.classList.toggle('is-scrolling',card.text.getBoundingClientRect().width-(parseFloat(getComputedStyle(card.text).paddingRight)||0)>card.marquee.clientWidth+1);
      card.tile.setAttribute('aria-label',host+' · '+name+' · '+label+' · '+prompt);
    }
  }
  function pauseDebugGrid(){
    clearInterval(debugTimer);debugTimer=null;if(debugFrame!==null)cancelAnimationFrame(debugFrame);debugFrame=null;
    stopDebugMonitors();debugSignature='';
    for(const monitor of debugActivityMonitors.values())stopDebugActivityMonitor(monitor);debugActivityMonitors.clear();debugPointer=null;debugInteractionUntil=0;debugSuppressClickUntil=0;
  }
  function resumeDebugGrid(){
    if(paused||!$('debug-dialog').open)return;
    clearInterval(debugTimer);debugTimer=setInterval(scheduleDebugGrid,400);scheduleDebugGrid();
  }
  $('debug').onclick=()=>{debugPage=0;debugOrder=[];debugOrderAt=0;$('debug-dialog').showModal();resumeDebugGrid();};
  $('debug-close').onclick=()=>$('debug-dialog').close();$('debug-dialog').addEventListener('close',pauseDebugGrid);
  $('debug-density').onchange=()=>{debugCapacity=['9','16','25'].includes($('debug-density').value)?Number($('debug-density').value):16;debugPage=0;scheduleDebugGrid();};
  $('debug-prev').onclick=()=>{debugPage--;scheduleDebugGrid();};$('debug-next').onclick=()=>{debugPage++;scheduleDebugGrid();};
  $('debug-board').addEventListener('pointerdown',event=>{if(event.isPrimary===false)return;debugPointer={id:event.pointerId,x:event.clientX,y:event.clientY,dragged:false};debugInteractionUntil=Infinity;});
  $('debug-board').addEventListener('pointermove',event=>{if(!debugPointer||event.pointerId!==debugPointer.id)return;const dx=event.clientX-debugPointer.x,dy=event.clientY-debugPointer.y;if(Math.hypot(dx,dy)>10)debugPointer.dragged=true;if(Math.abs(dx)>12&&Math.abs(dx)>Math.abs(dy)*1.3){event.preventDefault();$('debug-board').setPointerCapture?.(event.pointerId);}});
  $('debug-board').addEventListener('pointerup',event=>{if(!debugPointer||event.pointerId!==debugPointer.id)return;const dragged=debugPointer.dragged,swipe=M.debugSwipePage(debugPointer,{x:event.clientX,y:event.clientY},debugPage,debugPages);debugPointer=null;debugInteractionUntil=performance.now()+500;if(swipe.swiped||dragged){if(swipe.swiped)debugPage=swipe.page;debugSuppressClickUntil=performance.now()+500;event.preventDefault();}scheduleDebugGrid();});
  $('debug-board').addEventListener('pointercancel',()=>{debugPointer=null;debugInteractionUntil=performance.now()+500;scheduleDebugGrid();});
  for(const type of ['pointerleave','lostpointercapture'])$('debug-board').addEventListener(type,event=>{if(debugPointer&&!$('debug-board').hasPointerCapture?.(debugPointer.id)){debugPointer=null;debugInteractionUntil=performance.now()+500;scheduleDebugGrid();}});
  $('debug-board').addEventListener('click',event=>{if(performance.now()<debugSuppressClickUntil){event.preventDefault();event.stopImmediatePropagation();}},{capture:true});
  new ResizeObserver(()=>{const board=$('debug-board');$('debug-grid').style.setProperty('--debug-side',Math.max(0,Math.min(board.clientWidth,board.clientHeight))+'px');scheduleDebugGrid();}).observe($('debug-board'));
  function validTerminalBinding(binding){const c=binding&&connections.get(binding.host);return !!c&&safeEndpoint(c.host)===binding.endpoint&&c.host.code===binding.code&&hostTokens.get(c.host.id)===binding.token&&c.inventory.some(s=>s.id===binding.sid);}
  function evictTerminal(key){const entry=terminalCache.get(key);if(!entry)return;terminalCache.delete(key);if(validTerminalBinding(entry.binding)&&initializedTerms.has(entry.terminal))captureGridHistory(entry.terminal,entry.binding);entry.terminal.dispose();const history=screenHistories.get(key);if(history?.archive.length)history.archivedGrid=true;}
  function cacheTerminal(current){
    const binding=terminalBindings.get(current);if(!validTerminalBinding(binding)){current.dispose();if(binding)screenHistories.delete(binding.key);return;}
    const history=screenHistories.get(binding.key),wasArchived=history?.archivedGrid||false;captureGridHistory(current);const saved=screenHistories.get(binding.key);if(saved)saved.archivedGrid=wasArchived;
    current.options.disableStdin=true;current.blur();current.element?.remove();queuedPositions.delete(current);
    terminalCache.set(binding.key,{terminal:current,fit,binding});
    const bytes=()=>[...terminalCache.values()].reduce((total,entry)=>total+entry.terminal.cols*(entry.terminal.buffer.normal.length+entry.terminal.buffer.alternate.length)*16,0);
    while(terminalCache.size>6||bytes()>48*1024*1024)evictTerminal(terminalCache.keys().next().value);
  }
  function takeCachedTerminal(key){const entry=terminalCache.get(key);if(!entry)return null;if(!validTerminalBinding(entry.binding)){evictTerminal(key);screenHistories.delete(key);return null;}terminalCache.delete(key);return entry;}
  function parsedTerminalProgress(current,progress){
    for(const part of progress){const grid=part.grid;if(!grid)continue;
      if(grid.producer){const position=Number.isSafeInteger(grid.position_start)?grid.position_start+part.offset:part.complete?grid.end_position:null;if(Number.isSafeInteger(position)){const previous=streamPositions.get(current);if(!previous||previous.stream_id!==grid.stream_id||previous.epoch!==grid.epoch||position>=previous.position)streamPositions.set(current,{position,epoch:grid.epoch,stream_id:grid.stream_id});}}
      if(part.complete&&grid.boundaryKind){initializedTerms.add(current);restoringTerms.delete(current);}
    }
  }
  function activeWriter(current){return M.createWriter({write:(bytes,reset,done,grid,progress)=>{if(term!==current)return;
    const epoch=grid?.epoch??grid?.history_epoch,readingReset=!!reset||!!grid&&(current.cols!==grid.cols||current.rows!==grid.rows||Number.isSafeInteger(epoch)&&drawEpochs.has(current)&&epoch!==drawEpochs.get(current));
    const readingAnchor=captureReadingAnchor(current,readingReset);
    if(grid){const size=C.terminalSize(grid.cols,grid.rows),epoch=grid.epoch??grid.history_epoch,changed=current.cols!==size.cols||current.rows!==size.rows||Number.isSafeInteger(epoch)&&drawEpochs.has(current)&&epoch!==drawEpochs.get(current);if(changed){if(initializedTerms.has(current)){captureGridHistory(current);selected().historyQuality.set(active,'尺寸变化前的本机历史已归档，可用顶部或上翻查看。');updateHistoryQuality();}if(!reset)replayGrids.delete(current);current.resize(size.cols,size.rows);}if(Number.isSafeInteger(epoch))drawEpochs.set(current,epoch);}
    if(grid?.boundaryKind==='snapshot')streamPositions.delete(current);
    if(reset){if(initializedTerms.has(current))captureGridHistory(current);initializedTerms.delete(current);current.reset();}
    displayGrid();metrics.writes++;terminalWrites.set(current,(terminalWrites.get(current)||0)+1);
    current.write(bytes,()=>{if(bytes.length||reset)writeStamps.set(current,(writeStamps.get(current)||0)+1);parsedTerminalProgress(current,progress);terminalWrites.set(current,Math.max(0,(terminalWrites.get(current)||1)-1));restoreReadingAnchor(current,readingAnchor);done();agentDetector?.request();if(term===current){current.options.cursorStyle='block';updateTerminalStatus();const replayBatch=!!reset||!!grid?.boundaryKind||!!progress?.some(part=>part.grid?.boundaryKind);if(bytes.length||replayBatch)composerSubmit.output({replaying:replayBatch});recordLocalOutput(current,bytes.length>0&&(!replayBatch||progress?.some(part=>!part.grid?.boundaryKind)));}if(!terminalWrites.get(current)){const subscribe=settledSubscriptions.get(current);settledSubscriptions.delete(current);subscribe?.();}});
  },schedule:cb=>requestAnimationFrame(cb),cancel:id=>cancelAnimationFrame(id),onOverflow:()=>{notice('输出积压超过上限，已暂停并请求重新连接');M.closeForRecovery(selected().socket,'手机输出积压');}});}
  function disposeTerminal(){
    setRawInput(false);
    releaseManagedContext();
    resetJournalContext();
    cancelTerminalPress();cancelTouchMomentum();
    leaveScreenHistory();
    cancelAttachmentUpload();
    cancelRefresh();const strip=$('terminal-status');if(strip){strip.textContent='';strip.hidden=true;}
    agentModeGeneration++;agentModeRequest=null;agentDetector?.dispose();agentDetector=null;$('agent-mode').disabled=true;
    delete $('screen').dataset.agentMode;delete $('screen').dataset.agentPreference;delete $('screen').dataset.actualAgent;delete $('screen').dataset.presentationStyle;
    composerSubmit.cancel('target_changed');
    waitingFeeds=[];waitingBytes=0;
    ready=false;pendingKeys=[];composing=false;replaying=0;lastSize='';control=false;chunkedReplay=false;
    clearTimeout(controlAction?.timer);controlAction=null;followIntent=false;clearTimeout(followReplay?.timer);followReplay=null;updateControlButton();
    setCtrl(false);
    if(fitFrame!==null)cancelAnimationFrame(fitFrame);fitFrame=null;rendererGridGeometry=null;
    if(writer)writer.dispose();writer=null;if(highlighter)highlighter.dispose();highlighter=null;if(webgl)webgl.dispose();webgl=null;
    if(term)cacheTerminal(term);term=null;fit=null;$('screen').replaceChildren();
  }
  function openExternalUrl(url){
    if(typeof url!=='string'||!url)return;
    if(/^www\./i.test(url))url='https://'+url;
    try{if(window.smaShell&&typeof window.smaShell.openExternal==='function'){window.smaShell.openExternal(url);return;}}catch{}
    try{window.open(url,'_blank');}catch{}
  }
  function activate(sid){
    if(attachmentReturnKey&&attachmentReturnKey!==sessionKey(sid))attachmentReturnKey=null;
    cancelTouchMomentum();if(scrollFrame!==null)cancelAnimationFrame(scrollFrame);scrollFrame=null;scrollRows=0;gesture=M.createGesture();
    if(sid===active&&term)return;
    mobileFeedbackQa?.invalidate();
    if(active)drafts.set(sessionKey(),$('draft').value);disposeTerminal();active=sid;selected().subscription='';
    const s=inventory.find(x=>x.id===sid);if(!s)return;updateHistoryQuality();$('title').textContent=s.title+'（'+M.agentType(s,s.title)+'）';
    if(s.interaction_mode==='managed')notice('');
    syncDraftFields(drafts.get(sessionKey(sid))||'');for(const [key,value] of drafts)if(!value&&key!==sessionKey(sid))drafts.delete(key);
    const font=Number(localStorage.getItem('sma.mobile.font'))||14;$('font').value=String(Math.max(11,Math.min(22,font)));
    const cached=takeCachedTerminal(sessionKey(sid));
    if(cached){term=cached.terminal;fit=cached.fit;term.options.fontSize=Number($('font').value);$('screen').append(term.element);restoringTerms.add(term);const position=streamPositions.get(term);if(position)queuedPositions.set(term,position);}
    else term=new Terminal({allowProposedApi:true,disableStdin:true,convertEol:false,scrollback:20000,fontSize:Number($('font').value),fontFamily:'Consolas, monospace',cursorBlink:false,minimumContrastRatio:4.5,windowsPty:{backend:'conpty'},theme:{background:'#141414',foreground:'#ededed',cursor:'#ddd'}});
    const current=term,agentHost=selected();
    if(!cached){terminalBindings.set(current,Object.freeze({key:sessionKey(),host:hostId,sid,endpoint:safeEndpoint(agentHost.host),code:agentHost.host.code,token:hostTokens.get(hostId)}));
    // Full-screen tools may erase scrollback during repaint; preserve mobile reading history.
    term.parser.registerCsiHandler({final:'J'},params=>params[0]===3);
    term.parser.registerOscHandler(7501,data=>{
      if(term!==current||active!==sid||!validTerminalBinding(terminalBindings.get(current)))return false;
      const currentHost=selected(),session=currentHost.inventory.find(item=>item.id===sid);if(!session||session.agent)return false;
      const app=/(?:^|[;:])app=([a-zA-Z0-9_-]{1,40})(?:[;:]|$)/.exec(data)?.[1];
      const clear=/(?:^|[;:])state=clear(?:[;:]|$)/.test(data);
      if(!clear&&!/^(codex|claude(?:[-_]code)?|grok|kimi|pi[-_]dev|cursor[-_]cli|gemini|opencode|copilot|aider)$/i.test(app||''))return false;
      const next=clear?'':app.toLowerCase().replace('_','-');if((currentHost.agents.get(sid)||'')===next)return false;
      if(next)currentHost.agents.set(sid,next);else currentHost.agents.delete(sid);
      session.learnedAgent=next;renderList();refreshAgentMode();return false;
    });
    fit=new FitAddon.FitAddon();term.loadAddon(fit);term.loadAddon(new Unicode11Addon.Unicode11Addon());term.unicode.activeVersion='11';if(window.WebLinksAddon?.WebLinksAddon){try{term.loadAddon(new WebLinksAddon.WebLinksAddon(openExternalUrl));}catch{}}term.open($('screen'));term.textarea.readOnly=true;term.textarea.inputMode='none';term.textarea.setAttribute('aria-readonly','true');}
    setRenderer();
    highlighter=C.createHighlighter(current,{enabled:$('highlight').checked,profile:C.commonAgentProfile,presentationStyle:'codex'});
    refreshAgentMode(true);
    agentDetector=C.createAgentDetector(current,{isUnknown:()=>{if(term!==current||active!==sid)return false;const c=selected(),session=c.inventory.find(item=>item.id===sid);return (agentModePreferences.get(agentModeKey())||'auto')==='auto'&&!c.agents.get(sid)&&C.agentModeFromSession(session||{},session?.title||'')==='auto';},onDetected:mode=>{if(term!==current||active!==sid)return;const c=selected(),session=c.inventory.find(item=>item.id===sid);if(!session||session.agent)return;session.learnedAgent=mode;c.agents.set(sid,mode);renderList();refreshAgentMode(true);}});
    agentDetector.request();
    writer=activeWriter(current);
    if(!cached){term.onData(text=>{if(term!==current||active!==sid||!validTerminalBinding(terminalBindings.get(current)))return;text=M.terminalInput(text,replaying>0);if(ctrl&&text.length===1&&nativeInputDepth===0){text=M.controlKey(text);setCtrl(false);}input(text);});
    term.textarea.addEventListener('compositionstart',()=>{if(term===current)composing=true;});
    term.textarea.addEventListener('compositionend',()=>{if(term===current){composing=false;setTimeout(flushKeys,0);}});
    term.onScroll(scheduleHistoryStatus);}setMode(false);scheduleFit();renderList();
    if(isManagedSession())term.options.disableStdin=true;
    if(connected)subscribeSessions(selected(),active);refreshJournalStatus();syncManagedContext();
    // Deliberately do not focus: switching sessions must not pop the keyboard.
  }
  function input(text,source='external'){if(isManagedSession())return false;if(!text)return false;if(source!=='composer'&&nativeInputDepth===0&&composerSubmit.pending)composerSubmit.cancel('auxiliary_input');if(!ready||paused||!connected||selected().socket?.readyState!==1)return false;if(historyRenderer){leaveScreenHistory();term.scrollToBottom();localBottom();}selected().socket.send(C.targetFrame(17,active,text));return true;}
  function key(text){if(isManagedSession())return;if(composerSubmit.pending&&nativeInputDepth===0)composerSubmit.cancel('auxiliary_key');if(composing){pendingKeys.push({sid:sessionKey(),text});return;}input(text);}
  function flushKeys(){const keys=pendingKeys;pendingKeys=[];for(const k of keys)if(k.sid===sessionKey())input(k.text);}
  const localViews=new WeakMap();
  function localView(){let view=localViews.get(term);if(!view){view={bottom:true,following:true,newOutput:0};localViews.set(term,view);$('screen').scrollLeft=0;}return view;}
  function localHistoryContext(source='button'){
    const buffer=term?.buffer.active,session=inventory.find(s=>s.id===active)||{},view=term?localView():{};
    return {source,following:view.following,atBottom:!historyRenderer&&!!buffer&&(buffer.type!=='normal'||buffer.viewportY>=buffer.baseY)&&($('screen').dataset.viewportMode!=='readable'||$('screen').scrollTop>=$('screen').scrollHeight-$('screen').clientHeight-2),bufferType:buffer?.type,baseY:buffer?.baseY||0,archiveAvailable:!!term&&archivedHistoryAvailable(),historyActive:!!historyRenderer,actualAgent:C.agentModeFromSession(session,session.title||''),nativePaging:codexPagingAvailable(),ready:ready&&connected&&!paused,replaying:replaying>0,managed:isManagedSession()};
  }
  function observeLocalHistory(source){if(!term)return;const view=localView(),policy=C.localHistoryPolicy('observe',{...localHistoryContext(source),userInitiated:true});view.following=policy.follow;view.bottom=policy.follow;if(policy.follow)view.newOutput=0;historyStatus();}
  function localBottom(){if(!term)return;const view=localView();view.following=C.localHistoryPolicy('end',localHistoryContext('button')).follow;view.bottom=true;view.newOutput=0;$('screen').scrollTop=$('screen').scrollHeight;}
  function captureReadingAnchor(current,reset){
    if(term!==current||!current.buffer||historyRenderer||localView().following!==false)return null;
    const buffer=current.buffer.active;
    if(reset||buffer.type==='alternate'){
      const archive=screenHistory();archive.record({cols:current.cols,rows:current.rows,lines:Array.from({length:current.rows},(_,i)=>M.visibleBufferLine(buffer.getLine(buffer.viewportY+i),current.cols))});archive.previous();renderScreenHistory();return null;
    }
    return {buffer,marker:current.registerMarker?.(buffer.viewportY-buffer.baseY-buffer.cursorY),line:buffer.viewportY,top:$('screen').scrollTop,left:$('screen').scrollLeft};
  }
  function restoreReadingAnchor(current,anchor){
    if(!anchor)return;
    if(term===current&&current.buffer.active===anchor.buffer&&!historyRenderer&&localView().following===false){current.scrollToLine(anchor.marker&&!anchor.marker.isDisposed?anchor.marker.line:Math.min(anchor.line,current.buffer.active.baseY));$('screen').scrollTop=anchor.top;$('screen').scrollLeft=anchor.left;}
    anchor.marker?.dispose();
  }
  function recordLocalOutput(current,fresh){if(term!==current||!fresh)return;const view=localView(),policy=C.localHistoryPolicy('output',localHistoryContext('output'));view.following=policy.follow;if(!policy.follow)view.newOutput=Math.min(999999,(view.newOutput||0)+1);historyStatus();}
  let naturalCell=null;
  function baseCell(){
    if(!term)return null;
    const key=JSON.stringify([term.options.fontSize,term.options.fontFamily,term.options.fontWeight,term.options.fontWeightBold,devicePixelRatio]);
    if(naturalCell?.term!==term||naturalCell.key!==key||naturalCell.renderer!==webgl){
      if(term.options.lineHeight!==1)term.options.lineHeight=1;
      if(term.options.letterSpacing!==0)term.options.letterSpacing=0;
      const cell=term._core._renderService.dimensions.css.cell;
      if(!cell.width||!cell.height)return null;
      naturalCell={term,key,renderer:webgl,width:cell.width,height:cell.height};
    }
    return naturalCell;
  }
  function desiredSize(){
    if(!term||document.hidden||$('screen').clientWidth<48||$('screen').clientHeight<24)return null;
    const cell=baseCell();if(!cell)return null;
    return C.terminalSize(Math.floor(($('screen').clientWidth-12-term._core.viewport.scrollBarWidth)/cell.width),Math.floor(($('screen').clientHeight-5)/cell.height));
  }
  function displayGrid(){
    if(!term?.element)return;const cell=baseCell();if(!cell)return;
    const screen=$('screen'),rect=screen.getBoundingClientRect(),view=localView();
    const readable=sizing==='phone'&&!control;
    // Keep glyph size and columns unchanged; short desktop grids may use the
    // available vertical space through local row spacing alone.
    let lineHeight=readable?Math.max(1,(screen.clientHeight-5)/(term.rows*cell.height)):1;
    if(term.options.lineHeight!==lineHeight)term.options.lineHeight=lineHeight;
    let actual=term._core._renderService.dimensions.css.cell;
    if(lineHeight>1&&actual.height*term.rows<screen.clientHeight-5){
      lineHeight+=(screen.clientHeight-5-actual.height*term.rows+1)/(term.rows*cell.height);
      term.options.lineHeight=lineHeight;actual=term._core._renderService.dimensions.css.cell;
    }
    const width=term.cols*actual.width+12+term._core.viewport.scrollBarWidth,height=term.rows*actual.height+5;
    const scale=readable?1:Math.min(rect.width/width,rect.height/height);
    if(scale<=0)return;screen.dataset.viewportMode=readable?'readable':'overview';
    Object.assign(term.element.style,{width:width+'px',height:(readable?Math.max(height,screen.clientHeight):height)+'px',transformOrigin:'top left',transform:'scale('+scale+')'});
    screen.dataset.grid=term.cols+'×'+term.rows;screen.dataset.scale=String(scale);
    // Only explicit local gestures change follow mode. Renderer/resize scroll
    // callbacks are not evidence that the user paused reading.
    if(view.following!==false&&!historyRenderer){if(term.buffer.active.type==='normal')term.scrollToBottom();view.bottom=true;}
    if(readable&&view.bottom)screen.scrollTop=screen.scrollHeight;
    if(!readable){screen.scrollLeft=0;screen.scrollTop=0;}
    historyStatus();
  }
  function requestSize(takeControl=false){
    // Viewing, switching and rotating are local actions. Only an existing owner
    // or an explicit Take Control click may alter the shared PTY dimensions.
    if(!takeControl&&(!control||followIntent))return false;
    const desired=desiredSize(),lease=controls[active];if(!desired||!ready||!lease)return;
    const key=desired.cols+'x'+desired.rows;
    if((takeControl||key!==lastSize)&&send(5,{session:active,...desired,take_control:takeControl})){lastSize=key;return true;}return false;
  }
  function updateControlButton(){
    const button=$('control');button.textContent=controlAction?'切换中…':control?'跟随':'接管';
    button.disabled=!!controlAction||!!followReplay||!!pendingRefresh||!ready||!connected;
    $('refresh-screen').disabled=!!controlAction||!!followReplay;
    button.setAttribute('aria-pressed',String(control));
    button.title=control?'当前手机接管；点击释放尺寸控制并跟随远端':'当前跟随远端；点击按手机屏幕接管尺寸';
    $('screen').dataset.controlMode=control?'phone':'follow';
    const waiting='已跟随：等待远端接回尺寸控制，暂时保持当前网格。';
    if(followIntent&&!control&&ready&&!controlAction&&controls[active]?.busy===false){if($('notice').hidden||replayNoticeTimer!==null)notice(waiting);}
    else if($('notice').textContent===waiting)notice('');
    if(typeof setRawInput==='function')setRawInput(rawInputPreferences.get(sessionKey())!==false);
  }
  function toggleControl(){
    if(controlAction||followReplay||pendingRefresh||!term||!active||!ready||!connected||paused)return;
    if(historyRenderer){leaveScreenHistory();term.scrollToBottom();localBottom();}
    const c=selected(),sid=active,current=term,generation=c.generation;
    const request={mode:control?'follow':'take'};controlAction=request;
    if(control){
      // Legacy daemons release a lease when its subscriber removes this session.
      // Re-add it on the same ordered connection: never close the shell or guess
      // a previous desktop size. An online desktop reclaims its current fit.
      followIntent=true;lastSize='';
      const screen=$('screen'),buffer=term.buffer.active;
      followReplay={host:c,generation,sid,terminal:current,distance:buffer.baseY-buffer.viewportY,left:screen.scrollLeft,top:screen.scrollTop,bottom:localView().bottom};
      const replay=followReplay;replay.timer=setTimeout(()=>{if(followReplay===replay){followReplay=null;updateControlButton();}},6000);
      sizing='phone';$('sizing').value=sizing;localStorage.setItem('sma.mobile.sizing',sizing);
      if(!sendHost(c,16,{sessions:[]})||!sendHost(c,16,{sessions:[sid]})){clearTimeout(replay.timer);followReplay=null;controlAction=null;notice('连接不可用，未确认释放尺寸控制。');}
      else c.subscription=sid;
    }else{
      followIntent=false;lastSize='';
      if(!requestSize(true)){controlAction=null;notice('终端尚未就绪，未接管尺寸。');}
    }
    if(controlAction===request)request.timer=setTimeout(()=>{
      if(controlAction!==request||selected()!==c||c.generation!==generation||active!==sid||term!==current)return;
      controlAction=null;updateControlButton();notice('模式切换尚未收到确认，请检查连接后重试。');
    },4000);
    updateControlButton();scheduleFit();
  }
  let rendererGridGeometry=null;
  const screenGeometry=()=>[$('screen').clientWidth,$('screen').clientHeight,devicePixelRatio].join(':');
  function scheduleFit(){if(fitFrame!==null||!term)return;fitFrame=requestAnimationFrame(()=>{fitFrame=null;if(!term||paused||writer?.idle===false)return;
    displayGrid();if(rendererGridGeometry){if(rendererGridGeometry===screenGeometry()&&(!control||followIntent))return;if(desiredSize()===null)return;rendererGridGeometry=null;}requestSize();});}
  function setRenderer(){
    rendererGridGeometry=screenGeometry();
    if(webgl)webgl.dispose();webgl=null;let effective='dom';
    if(renderer==='webgl')try{const gl=new WebglAddon.WebglAddon();term.loadAddon(gl);webgl=gl;effective='webgl';gl.onContextLoss(()=>{if(webgl!==gl)return;webgl=null;gl.dispose();term.refresh(0,term.rows-1);metrics.fallbacks++;$('render-status').textContent='GPU 上下文丢失，已回退 DOM';});}catch{metrics.fallbacks++;notice('GPU 渲染不可用，已回退 DOM');}
    term.refresh(0,term.rows-1);$('render-status').textContent='实际渲染：'+effective.toUpperCase();$('renderer').value=C.rendererName(renderer);scheduleFit();
  }
  function setCtrl(value){ctrl=value;$('ctrl').setAttribute('aria-pressed',String(ctrl));}
  function setRawInput(value,focus=false,remember=false){
    if(remember&&!isManagedSession())rawInputPreferences.set(sessionKey(),!!value);
    nativeRawInput=!!value&&!isManagedSession()&&!!term&&ready&&connected&&!submissionPending();
    const button=$('raw-input');button.hidden=isManagedSession();button.disabled=isManagedSession()||!term||!ready||!connected||submissionPending();button.textContent=nativeRawInput?'原始':'草稿';button.title=nativeRawInput?'当前原始输入；点击切换到本地草稿':'当前本地草稿；点击切换到原始输入';button.setAttribute('aria-label',button.title);button.setAttribute('aria-pressed',String(nativeRawInput));
    if(term?.textarea){term.textarea.readOnly=!nativeRawInput;term.textarea.inputMode=nativeRawInput?'text':'none';term.textarea.setAttribute('aria-readonly',String(!nativeRawInput));}
    if(focus){if(nativeRawInput){showJournalRaw(true);term.focus();}else $('quick-draft').focus();}
  }
  function syncDraftFields(text,source=''){
    if(source!=='draft')$('draft').value=text;if(source!=='quick')$('quick-draft').value=text;drafts.set(sessionKey(),text);
  }
  function attachmentsPending(key=sessionKey()){
    const draft=attachmentDrafts.get(key);return !!draft&&draft.items.some(item=>!item.receipt);
  }
  function updateComposer(pending=submissionPending()){
    if(typeof pruneNativeUncertainty==='function')pruneNativeUncertainty();
    const uncertain=!!$('draft').value&&uncertainDrafts.get(sessionKey())===$('draft').value,waiting=!!uploadJob||attachmentsPending(),disabled=pending||uncertain||waiting;
    $('draft').readOnly=pending;$('composer-close').disabled=composerSubmit.pending;
    $('send').disabled=disabled;$('send').textContent=pending?'验证输入中…':uncertain?'请先检查终端':uploadJob?'附件上传中':attachmentsPending()?'请先处理附件':'发送';$('send').setAttribute('aria-busy',String(pending||waiting));
    $('quick-send').disabled=disabled;$('quick-send').textContent=pending?'验证中…':uncertain?'请检查终端':waiting?'附件未就绪':'发送';$('quick-send').setAttribute('aria-busy',String(pending||waiting));$('quick-draft').readOnly=pending;
    $('model').disabled=isManagedSession()||pending||waiting||uncertainModelCommands.has(sessionKey());$('model').setAttribute('aria-busy',String(pending));$('mode').disabled=composerSubmit.pending||!!uploadJob||isManagedSession()&&managedSnapshot?.pending?.phase==='sending';$('github').disabled=pending||waiting;updateAttachments();
    if(isManagedSession()){
      const available=!!managedSnapshot?.canSubmit&&!waiting&&!composing;
      $('send').disabled=$('quick-send').disabled=!available;$('draft').readOnly=$('quick-draft').readOnly=pending;
      $('send').textContent=$('quick-send').textContent=managedSnapshot?.pending?'请核对接收状态':managedSnapshot?.operation?'操作待核对':managedSnapshot?.canSubmit?'发送':'本轮不可发送';
      $('composer-close').disabled=managedSnapshot?.pending?.phase==='sending';
      $('composer-status').textContent=$('managed-message').textContent||'消息提交后仅凭持久接收记录清空草稿；不会发送终端回车。';
    }
    $('quick-send').title=$('quick-send').textContent;$('quick-send').setAttribute('aria-label',$('quick-send').textContent);
    if(typeof setRawInput==='function')setRawInput(rawInputPreferences.get(sessionKey())!==false);
  }
  function setMode(focus,open=false){
    const editor=$('composer');
    if((focus||open)&&modes.get(sessionKey())==='buffered'){
      setRawInput(false,false,true);
      $('composer-target').textContent=[machine||selected().host.name,$('title').textContent].filter(Boolean).join(' · ');
      $('composer-status').textContent=isManagedSession()?$('managed-message').textContent:uncertainDrafts.get(sessionKey())===$('draft').value&&$('draft').value?'未收到输入回显，尚未按回车。请检查终端，避免重复粘贴。':'收到终端回显后自动按一次回车。';
      if(!editor.open)editor.showModal();if(focus)$('draft').focus();
    }else if(editor.open)editor.close();
    $('mode').setAttribute('aria-expanded',String(editor.open));updateComposer();
  }
  function closeComposer(){if(composerSubmit.pending||isManagedSession()&&managedSnapshot?.pending?.phase==='sending')return;syncDraftFields($('draft').value,'draft');attachmentReturnKey=null;cancelAttachmentUpload();modes.set(sessionKey(),'direct');$('draft').blur();setMode(false);if(isManagedSession())$('managed-permissions').querySelector('button[data-decision="deny"]:not(:disabled)')?.focus({preventScroll:true});}
  function submit(){
    if(composing||submissionPending()||uploadJob||attachmentsPending()){if(attachmentsPending()){$('composer-status').textContent='附件尚未确认保存，请等待完成或移除失败附件后再发送。';notice($('composer-status').textContent);}return;}
    syncDraftFields($('draft').value,'draft');
    if(isManagedSession()){submitManaged();return;}
    if(!ready||!connected){$('composer-status').textContent='终端未连接，指令尚未发送；文本已保留。';notice($('composer-status').textContent);return;}
    if(!$('draft').value.trim()){$('composer-status').textContent='请先输入指令。';notice($('composer-status').textContent);return;}
    pruneNativeUncertainty();
    if(uncertainDrafts.get(sessionKey())===$('draft').value)return;
    const sid=sessionKey(),text=$('draft').value;
    $('composer-status').textContent='正在验证终端输入回显…';drafts.set(sid,text);
    if(!sendNativeComposer(text,'draft',()=>{syncDraftFields('');uncertainDrafts.delete(sid);closeComposer();drafts.delete(sid);notice('输入回显已确认，已发送回车。');}))return;
    modes.set(sid,'direct');$('draft').blur();setMode(false);notice('输入已交给终端，等待回显确认…');
  }
  function openModelMenu(){
    if(isManagedSession()){notice('独立模式不接受原始 /model 按键；请使用受支持的结构化控制。');return;}
    if(composing||composerSubmit.pending||uploadJob)return;
    if(!ready||!connected||paused||!term){notice('终端尚未就绪，未发送 /model。');return;}
    const sid=sessionKey();pruneNativeUncertainty();
    if(uncertainModelCommands.has(sid)){notice('上次 /model 未确认，已停止自动回车；请在原始终端检查后手动操作。');return;}
    notice('正在打开模型选择…');
    sendNativeComposer('/model','model',()=>notice('已发送 /model，请在终端选择模型。'));
  }
  function attachmentTarget(){
    const c=selected(),session=c.inventory.find(s=>s.id===active);
    if(!active||!session)throw Error('请先选择一个终端。');
    return {key:sessionKey(),host:c,session:active,window:session.window,endpoint:safeEndpoint(c.host),code:c.host.code,token:hostTokens.get(c.host.id)};
  }
  function attachmentTargetMatches(t){
    return !!t&&connections.get(t.host.host.id)===t.host&&safeEndpoint(t.host.host)===t.endpoint&&t.host.host.code===t.code&&hostTokens.get(t.host.host.id)===t.token&&t.host.inventory.some(s=>s.id===t.session&&s.window===t.window);
  }
  function attachmentTransportCurrent(t){return attachmentTargetMatches(t)&&t.host.connected&&t.host.socket===t.socket&&t.host.generation===t.generation&&t.socket?.readyState===1;}
  function attachmentCurrent(t){return attachmentTransportCurrent(t)&&sessionKey()===t.key&&ready&&!paused;}
  function updateAttachments(){
    const draft=attachmentDrafts.get(sessionKey()),items=draft?.items||[],busy=!!uploadJob;
    $('attach-file').disabled=$('attach-image').disabled=isManagedSession()||busy||submissionPending();
    $('upload-files').disabled=isManagedSession()||busy||submissionPending()||!ready||!connected||!draft||!attachmentTargetMatches(draft.target)||!items.some(item=>!item.receipt&&!item.uncertain);
    $('cancel-upload').hidden=!busy;$('attachment-list').replaceChildren();
    for(const item of items){const row=document.createElement('li');row.className='attachment-item';const text=document.createElement('span'),remove=document.createElement('button');text.textContent=item.name+' · '+(item.receipt?'已保存':item.uncertain?'保存结果待核对':item.error?'失败，可重试':'待上传');remove.textContent='移除';remove.type='button';remove.disabled=busy;remove.onclick=()=>{draft.items=draft.items.filter(value=>value!==item);if(!draft.items.length)attachmentDrafts.delete(sessionKey());updateComposer();};row.append(text,remove);$('attachment-list').append(row);}
    if(!busy)$('attachment-status').textContent=isManagedSession()?'独立模式暂不支持结构化附件；已有附件与草稿保留，可返回原始会话处理。':items.some(item=>item.uncertain)?'有附件的保存结果未确认，请先检查目标电脑，避免重复上传。':items.some(item=>!item.receipt)?'附件未全部保存，文本发送已锁定；失败项可重试或移除。':items.length&&selected().capabilities?.file_transfer!==true?'附件将保存到目标电脑 D:\\aicoding\\supermonoagent\\tmp（无 D 盘时为 C 盘对应目录）。':'最多 8 个附件，单个限 32 MiB；上传完成后将路径加入草稿，不自动发送。';
  }
  function pickAttachments(input){
    if(isManagedSession()){notice('独立模式暂不支持附件；现有草稿和附件未改动。');return;}
    if(uploadJob||submissionPending())return;
    try{attachmentPickerTarget=attachmentTarget();attachmentReturnKey=attachmentPickerTarget.key;$(input).value='';$(input).click();}catch(error){$('attachment-status').textContent=error.message;}
  }
  function restoreAttachmentEditor(){
    if(!attachmentReturnKey||paused||!ready)return;
    if(attachmentReturnKey!==sessionKey()){attachmentReturnKey=null;return;}
    attachmentReturnKey=null;modes.set(sessionKey(),'buffered');setMode(false,true);
  }
  function collectAttachments(input){
    if(isManagedSession()){notice('独立模式暂不支持附件；请回到原始会话处理。');return;}
    const files=[...input.files],target=attachmentPickerTarget;attachmentPickerTarget=null;input.value='';if(!files.length){restoreAttachmentEditor();return;}
    try{
      if(!attachmentTargetMatches(target)||target.key!==sessionKey())throw Error('选择期间目标终端已变化，请重新选择附件。');
      let draft=attachmentDrafts.get(target.key);
      if(draft&&!attachmentTargetMatches(draft.target))throw Error('原附件的目标已变化，请先移除原附件。');
      if((draft?.items.length||0)+files.length>8||files.some(file=>file.size>32*1024*1024)||files.reduce((sum,file)=>sum+file.size,0)+(draft?.items.reduce((sum,item)=>sum+item.size,0)||0)>128*1024*1024)throw Error('最多 8 个附件，单个限 32 MiB，总计限 128 MiB。');
      if(!draft){if(attachmentDrafts.size>=32)throw Error('附件窗口过多，请先清理其他窗口的待上传附件。');draft={target,items:[]};attachmentDrafts.set(target.key,draft);}
      draft.items.push(...files.map(file=>({file,name:file.name,size:file.size})));restoreAttachmentEditor();updateAttachments();uploadAttachments();
    }catch(error){$('attachment-status').textContent=error.message;}
  }
  function attachmentRequest(target,body,signal,cleanup=false){return new Promise((resolve,reject)=>{
    if(!(cleanup?attachmentTransportCurrent(target):attachmentCurrent(target))||!cleanup&&signal.aborted){reject(Error('目标连接已变化或上传已取消。'));return;}
    const socket=target.socket,request='android-file-'+Date.now().toString(36)+'-'+(++fileRequestSequence);let timer,settled=false;
    const done=(error,value)=>{if(settled)return;settled=true;clearTimeout(timer);socket.removeEventListener('message',receive);socket.removeEventListener('close',closed);signal.removeEventListener('abort',aborted);error?reject(error):resolve(value);};
    const closed=()=>done(Error('文件上传连接已断开。')),aborted=()=>done(Error('上传已取消。'));
    const receive=event=>{try{if(!(event.data instanceof ArrayBuffer))return;const frame=C.unpack(event.data);if(frame.op!==21)return;const value=JSON.parse(decoder.decode(frame.payload));if(value.request!==request)return;if(!(cleanup?attachmentTransportCurrent(target):attachmentCurrent(target)))done(Error('目标终端已变化，未插入附件路径。'));else if(value.ok!==true)done(Error('目标电脑拒绝接收文件，请检查接收权限和可用空间。'));else done(null,value);}catch{}};
    socket.addEventListener('message',receive);socket.addEventListener('close',closed);if(!cleanup)signal.addEventListener('abort',aborted,{once:true});timer=setTimeout(()=>done(Error('等待文件接收确认超时。')),cleanup?3000:15000);
    try{socket.send(C.pack(21,encoder.encode(JSON.stringify({request,session:target.session,...body}))));}catch{done(Error('无法发送文件数据。'));}
  });}
  function cancelAttachmentUpload(){uploadJob?.controller.abort();}
  function attachmentError(error){return ({host_unavailable:'目标电脑未安装或连接文件助手。',connection_failed:'无法连接文件服务，请检查接收端。',authentication_failed:'文件服务凭据验证失败。',project_required:'目标电脑暂存目录不可用，请检查文件助手。',upload_dir_unavailable:'目标电脑暂存目录不可用，请检查文件助手。',unsupported:'目标连接不支持文件服务。'})[error.code]||error.message;}
  async function uploadAttachments(){
    if(isManagedSession()){notice('独立模式暂不支持附件上传；现有文件和草稿已保留。');return;}
    if(uploadJob||composerSubmit.pending)return;
    const draft=attachmentDrafts.get(sessionKey());if(!draft||!attachmentTargetMatches(draft.target))return;
    if(!ready||!connected){$('attachment-status').textContent='终端尚未连接，附件尚未上传。';return;}
    const target={...attachmentTarget(),socket:selected().socket,generation:selected().generation},job={target,controller:new AbortController()};uploadJob=job;updateComposer();
    try{
      for(const item of draft.items.filter(value=>!value.receipt&&!value.uncertain)){
        $('attachment-status').textContent='准备上传 '+item.name+'…';
        try{
          const direct=target.host.capabilities?.file_transfer===true,onProgress=(loaded,total,details)=>{
            $('attachment-status').textContent=details?.phase==='receiving'?item.name+' · 已暂存，等待目标保存确认。':details?.phase==='submitting'?item.name+' · 正在提交，请勿重复上传。':item.name+' · '+Math.floor(loaded/Math.max(1,total)*100)+'%';
          };
          item.receipt=direct?await M.uploadAttachment(item.file,{signal:job.controller.signal,isCurrent:()=>attachmentCurrent(target),request:(body,cleanup)=>attachmentRequest(target,body,job.controller.signal,cleanup),onProgress}):await M.uploadExchangeAttachment(item.file,{target,signal:job.controller.signal,isCurrent:()=>attachmentCurrent(target),onProgress});
          item.file=null;item.error=false;
          const addition='附件：'+JSON.stringify(item.receipt.path);syncDraftFields($('draft').value+($('draft').value?'\n':'')+addition,'attachments');updateAttachments();
        }catch(error){item.error=true;item.uncertain=error.uncertain===true||error.unknownCommit===true||!!error.pendingFileId;item.pendingFileId=error.pendingFileId;item.pendingUploadId=error.pendingUploadId;throw error;}
      }
      $('attachment-status').textContent='附件已保存到目标电脑，路径已加入草稿。';
    }catch(error){$('attachment-status').textContent=attachmentError(error);}
    finally{if(uploadJob===job){uploadJob=null;const message=$('attachment-status').textContent;updateComposer();if(sessionKey()===target.key)$('attachment-status').textContent=message;}}
  }
  $('attach-file').onclick=()=>pickAttachments('attachment-files');$('attach-image').onclick=()=>pickAttachments('attachment-images');
  $('attachment-files').onchange=e=>collectAttachments(e.target);$('attachment-images').onchange=e=>collectAttachments(e.target);
  for(const id of ['attachment-files','attachment-images'])$(id).addEventListener('cancel',()=>{attachmentPickerTarget=null;restoreAttachmentEditor();});
  $('upload-files').onclick=uploadAttachments;$('cancel-upload').onclick=cancelAttachmentUpload;
  function historyStatus(){
    if(!term)return;
    const view=localView(),fresh=view.newOutput?' · 有新输出':'';
    if(historyRenderer){const archive=screenHistory(),frame=archive.current;if(!frame){leaveScreenHistory();return;}$('history-hint').textContent='本次连接历史 · '+archive.length+' 帧 · '+new Date(frame.timestamp).toLocaleTimeString()+fresh;$('resume').hidden=false;return;}
    $('history-hint').textContent='正在查看本机历史'+fresh;const b=term.buffer.active,screen=$('screen');$('resume').hidden=view.following!==false&&(b.type!=='normal'||b.viewportY>=b.baseY)&&(screen.dataset.viewportMode!=='readable'||screen.scrollTop>=screen.scrollHeight-screen.clientHeight-2);
  }
  function screenHistory(key=sessionKey(),c=selected()){
    const token=hostTokens.get(c.host.id),identity=JSON.stringify([safeEndpoint(c.host),c.host.code]);
    let entry=screenHistories.get(key);
    if(!entry||entry.token!==token||entry.identity!==identity){entry={token,identity,archive:M.createScreenHistory({now:()=>Date.now(),minInterval:0,maxFrames:128,maxBytes:4*1024*1024}),lastSample:0,archivedGrid:false,altSeen:false,lastGridArchive:0};screenHistories.set(key,entry);}
    // Sample only the active tool. Bound total phone memory too.
    screenHistories.delete(key);screenHistories.set(key,entry);
    while(screenHistories.size>24||screenHistories.size>1&&[...screenHistories.values()].reduce((total,item)=>total+item.archive.bytes,0)>32*1024*1024)screenHistories.delete(screenHistories.keys().next().value);
    return entry.archive;
  }
  function captureScreenHistory(force=false){
    if(!term||!active||!ready||paused||replaying||writer&&!writer.idle||term.modes?.synchronizedOutputMode)return;
    const key=sessionKey(),existing=screenHistories.get(key),now=Date.now();
    if(!force&&existing&&now-existing.lastSample<750){metrics.historySkips++;return;}
    const started=performance.now(),archive=screenHistory(key),entry=screenHistories.get(key),b=term.buffer.active;
    const lines=Array.from({length:term.rows},(_,i)=>M.visibleBufferLine(b.getLine(b.baseY+i),term.cols));
    metrics.historySamples++;metrics.historyCells+=term.cols*term.rows;metrics.historyMilliseconds+=performance.now()-started;entry.lastSample=now;
    if(!force&&!lines.some(line=>line.trim()))return;
    if(b.type==='alternate')entry.altSeen=true;archive.record({cols:term.cols,rows:term.rows,lines});
    if(historyRenderer)historyStatus();
  }
  function captureGridHistory(current,binding=terminalBindings.get(current)){
    if(!current||!binding||!validTerminalBinding(binding))return;
    const key=binding.key,c=connections.get(binding.host),buffer=current.buffer.active,archive=screenHistory(key,c),entry=screenHistories.get(key),now=Date.now();
    const signature=[current.cols,current.rows,buffer.length,buffer.baseY,drawEpochs.get(current),writeStamps.get(current)].join(':');
    if(entry.backupSignature===signature){if(archive.length)entry.archivedGrid=true;return;}
    const full=buffer.type==='normal'&&now-entry.lastGridArchive>=1000;
    if(full&&!entry.archivedGrid&&!entry.altSeen&&!archive.current)archive.clear();
    let recorded=false;
    for(let row=full?Math.max(0,buffer.length-200*128):buffer.baseY;row<buffer.length;row+=full?200:current.rows){
      const rows=full?Math.max(current.rows,Math.min(200,buffer.length-row)):current.rows,lines=Array.from({length:rows},(_,i)=>M.visibleBufferLine(buffer.getLine(row+i),current.cols));
      if(lines.some(line=>line.trim()))recorded=archive.record({cols:current.cols,rows,lines})||recorded;
    }
    if(recorded){entry.archivedGrid=true;entry.lastGridArchive=now;entry.backupSignature=signature;}
  }
  function archivedHistoryAvailable(){const entry=screenHistories.get(sessionKey()),buffer=term?.buffer.active;return !!entry&&(buffer?.type==='alternate'||buffer?.type==='normal'&&buffer.length<=term.rows&&entry.archive.length>0);}
  function leaveScreenHistory(){
    if(active)screenHistories.get(sessionKey())?.archive.bottom();
    if(!historyRenderer)return;
    historyRenderer.dispose();historyRenderer=null;historyFrameID=null;$('history-terminal').replaceChildren();$('screen-history').hidden=true;$('screen').hidden=false;displayGrid();historyStatus();updateTerminalStatus();
  }
  function renderScreenHistory(returnAtTop=false){
    const frame=screenHistory().current;if(!frame){leaveScreenHistory();if(term?.buffer.active.type==='normal'){returnAtTop?term.scrollToTop():term.scrollToBottom();}if(returnAtTop){localView().bottom=false;$('screen').scrollTop=0;}else localBottom();historyStatus();return;}
    localView().following=false;localView().bottom=false;
    if(!historyRenderer){
      $('screen').hidden=true;$('screen-history').hidden=false;
      historyRenderer=new Terminal({cols:frame.cols,rows:frame.rows,allowProposedApi:true,disableStdin:true,scrollback:0,fontSize:term.options.fontSize,fontFamily:term.options.fontFamily,cursorBlink:false,minimumContrastRatio:4.5,theme:{...term.options.theme}});
      historyRenderer.loadAddon(new Unicode11Addon.Unicode11Addon());historyRenderer.unicode.activeVersion='11';historyRenderer.open($('history-terminal'));
    }
    if(historyFrameID!==frame.id){
      historyFrameID=frame.id;historyRenderer.resize(frame.cols,frame.rows);historyRenderer.reset();
      const bytes='\x1b[?25l\x1b[?7l'+frame.lines.map((line,i)=>'\x1b['+(i+1)+';1H'+line).join('');
      historyRenderer.write(bytes);sizeHistoryRenderer();$('screen-history').scrollTop=0;$('screen-history').scrollLeft=0;
    }
    historyStatus();updateTerminalStatus();
  }
  function sizeHistoryRenderer(){
    if(!historyRenderer)return;const frame=screenHistory().current;if(!frame)return;
    historyRenderer.options.lineHeight=1;let cell=historyRenderer._core._renderService.dimensions.css.cell;
    if(cell.height>0)historyRenderer.options.lineHeight=Math.max(1,($('screen-history').clientHeight-5)/(frame.rows*cell.height));
    cell=historyRenderer._core._renderService.dimensions.css.cell;
    Object.assign(historyRenderer.element.style,{width:(frame.cols*cell.width+12+historyRenderer._core.viewport.scrollBarWidth)+'px',height:(frame.rows*cell.height+5)+'px'});
  }
  function visibleRows(){const cell=term?._core?._renderService?.dimensions.css.cell,scale=Number($('screen').dataset.scale)||1;return Math.max(1,Math.min(term?.rows||1,Math.floor($('screen').clientHeight/((cell?.height||17)*scale))));}
  function localScroll(pixels,source='wheel'){
    if(!term||!pixels)return;const screen=$('screen'),buffer=term.buffer.active,line=term._core._renderService.dimensions.css.cell.height*(Number(screen.dataset.scale)||1);
    const view=localView(),policy=C.localHistoryPolicy(pixels<0?'up':'down',localHistoryContext(source));view.following=policy.follow;if(!policy.follow)view.bottom=false;
    if(historyRenderer){const pane=$('screen-history'),before=pane.scrollTop;pane.scrollTop+=pixels;if(Math.abs(pane.scrollTop-before)<=1){const archive=screenHistory();pixels<0?archive.previous():archive.next();renderScreenHistory(pixels>0&&buffer.type==='normal'&&buffer.baseY>0);}historyStatus();return;}
    if(pixels<0&&screen.scrollTop<=1&&(buffer.type==='alternate'||buffer.viewportY===0&&archivedHistoryAvailable())){captureScreenHistory(true);const archive=screenHistory();if(archive.length){archive.previous();renderScreenHistory();historyStatus();return;}}
    if(screen.dataset.viewportMode!=='readable'){const rows=Math.trunc(pixels/line);if(rows&&buffer.type==='normal')term.scrollLines(rows);if(pixels>0)observeLocalHistory(source);historyStatus();return;}
    const historyPixels=value=>{const total=(view.historyPixels||0)+value,before=buffer.viewportY,rows=Math.trunc(total/line);if(rows)term.scrollLines(rows);const rest=total-(buffer.viewportY-before)*line,edge=total<0?buffer.viewportY===0:buffer.viewportY>=buffer.baseY;view.historyPixels=edge?0:rest;return edge?rest:0;};
    let remaining=pixels;
    // Returning towards the prompt consumes history before the current grid.
    if(remaining>0&&buffer.type==='normal'&&buffer.viewportY<buffer.baseY)remaining=historyPixels(remaining);
    const before=screen.scrollTop;screen.scrollTop+=remaining;remaining-=screen.scrollTop-before;
    if(remaining<0&&buffer.type==='normal')historyPixels(remaining);
    view.bottom=screen.scrollTop>=screen.scrollHeight-screen.clientHeight-2&&(buffer.type!=='normal'||buffer.viewportY>=buffer.baseY);if(pixels>0)observeLocalHistory(source);historyStatus();
  }
  let gesture=M.createGesture(),scrollRows=0,scrollFrame=null,panTouch=null,terminalPress=null,terminalPressTimer=null,touchMomentumFrame=null;
  function cancelTouchMomentum(){if(touchMomentumFrame!==null)cancelAnimationFrame(touchMomentumFrame);touchMomentumFrame=null;}
  function cancelTerminalPress(){clearTimeout(terminalPressTimer);terminalPressTimer=null;terminalPress=null;}
  function beginTerminalPress(event){
    cancelTerminalPress();if(event.touches.length!==1||!term)return;
    const touch=event.touches[0];terminalPress={x:touch.clientX,y:touch.clientY,key:sessionKey(),terminal:term};
    const press=terminalPress;terminalPressTimer=setTimeout(()=>{terminalPressTimer=null;if(terminalPress===press&&press.key===sessionKey()&&press.terminal===term&&!paused)openTextSelection(false);},600);
  }
  function moveTerminalPress(event){if(event.touches.length!==1||terminalPress&&Math.hypot(event.touches[0].clientX-terminalPress.x,event.touches[0].clientY-terminalPress.y)>10)cancelTerminalPress();}
  function scroll(rows){if(!rows||!term)return;scrollRows+=rows;if(scrollFrame!==null)return;scrollFrame=requestAnimationFrame(()=>{scrollFrame=null;const n=scrollRows;scrollRows=0;if(!term)return;
    localScroll(n*term._core._renderService.dimensions.css.cell.height*(Number($('screen').dataset.scale)||1));});}
  function beginTouchMomentum(touch){
    if(!touch?.dragged||touch.remote||performance.now()-touch.at>120||Math.abs(touch.velocity)<.15)return;
    const current=term,key=sessionKey(),started=performance.now();let previous=started,velocity=Math.max(-3,Math.min(3,touch.velocity));
    cancelTouchMomentum();
    const frame=now=>{
      touchMomentumFrame=null;if(paused||term!==current||sessionKey()!==key)return;
      const elapsed=now-previous;previous=now;if(elapsed<=0){touchMomentumFrame=requestAnimationFrame(frame);return;}
      const dt=Math.min(40,elapsed),pixels=velocity*dt,line=(current._core._renderService.dimensions.css.cell.height||17)*(Number($('screen').dataset.scale)||1);
      if($('screen').dataset.viewportMode==='readable')localScroll(pixels,'touch');else scroll(pixels/line);
      velocity*=Math.pow(.94,dt/16.67);if(now-started<900&&Math.abs(velocity)>.03)touchMomentumFrame=requestAnimationFrame(frame);
    };
    touchMomentumFrame=requestAnimationFrame(frame);
  }
  $('screen').addEventListener('touchstart',e=>{cancelTouchMomentum();beginTerminalPress(e);if(e.touches.length!==1)return;const touch=e.touches[0],now=performance.now();gesture.start(touch.clientX,touch.clientY);panTouch={x:touch.clientX,y:touch.clientY,at:now,velocity:0,dragged:false,remote:false,travel:0,horizontal:0};e.preventDefault();},{passive:false});
  $('screen').addEventListener('touchmove',e=>{moveTerminalPress(e);if(e.touches.length!==1||!panTouch)return;const touch=e.touches[0],cell=term?._core?._renderService?.dimensions?.css?.cell,line=(cell?.height||17)*(Number($('screen').dataset.scale)||1),move=gesture.move(touch.clientX,touch.clientY,line),now=performance.now(),dt=now-panTouch.at,dx=panTouch.x-touch.clientX,dy=panTouch.y-touch.clientY;
    if(move.drag){if(dt>0)panTouch.velocity=panTouch.velocity*.65+dy/dt*.35;panTouch.dragged=true;panTouch.travel+=dy;panTouch.horizontal+=dx;
      if($('screen').dataset.viewportMode==='readable'){$('screen').scrollLeft+=dx;if(!panTouch.remote)localScroll(dy,'touch');}else if(!panTouch.remote)scroll(move.rows);
    }panTouch.x=touch.clientX;panTouch.y=touch.clientY;panTouch.at=now;e.preventDefault();},{passive:false});
  $('screen').addEventListener('touchend',e=>{cancelTerminalPress();const touch=panTouch;gesture.end();panTouch=null;beginTouchMomentum(touch);e.preventDefault();},{passive:false});
  $('screen').addEventListener('touchcancel',()=>{cancelTerminalPress();panTouch=null;});
  $('screen-history').addEventListener('touchstart',beginTerminalPress,{passive:true});$('screen-history').addEventListener('touchmove',moveTerminalPress,{passive:true});
  for(const type of ['touchend','touchcancel'])$('screen-history').addEventListener(type,cancelTerminalPress,{passive:true});
  $('screen').addEventListener('wheel',e=>{cancelTouchMomentum();e.preventDefault();if($('screen').dataset.viewportMode==='readable'){$('screen').scrollLeft+=e.deltaX||(e.shiftKey?e.deltaY:0);if(!e.shiftKey)localScroll(e.deltaY);}else scroll(Math.sign(e.deltaY)*3);},{passive:false});
  function updateTerminalActionButtons(){
    const c=selected(),s=c.inventory.find(item=>item.id===active),label=$('current-terminal-target');
    const name=s?[c.machine||c.host.name,s.title||s.window,s.id].join(' · '):'请先选择终端';
    if(label.textContent!==name)label.textContent=name;
    const available=!!s&&!!s.window&&c.connected&&c.socket?.readyState===1&&!paused;
    $('rename-current-window').disabled=!available;
    $('close-current-process').disabled=!available||(s.interaction_mode==='managed'&&(!managedCurrent(managedTarget())||$('managed-close').disabled))||
      (s.interaction_mode!=='managed'&&c.capabilities?.close_ack===1&&!s.process_generation);
  }
  function closeTerminalAction(){
    const dialog=$('terminal-action-dialog');
    if(!dialog.open)return false;
    dialog.close();
    return true;
  }
  function stopTerminalAction(message){
    if(!terminalAction)return;
    clearTimeout(terminalAction.timer);
    terminalAction.phase='finished';
    $('terminal-action-confirm').disabled=true;
    $('terminal-action-name').readOnly=true;
    $('terminal-action-cancel').textContent='关闭';
    $('terminal-action-status').textContent=message;
  }
  function sameActionConnection(action){
    const t=action.target,c=t.host;
    return !paused&&!document.hidden&&selected()===c&&connections.get(c.host.id)===c&&c.connected&&
      c.socket===action.socket&&c.socket?.readyState===1&&c.generation===t.generation&&
      safeEndpoint(c.host)===t.endpoint&&(c.host.code||'')===t.room&&(hostTokens.get(c.host.id)||'')===t.token;
  }
  function matchingTerminal(action){
    const t=action.target;
    const s=t.host.inventory.find(item=>item.id===t.session&&item.window===t.window);
    return s&&(s.process_generation||'')===t.process_generation&&
      (s.interaction_mode==='managed')===action.managed&&(!action.managed||s.journal_id===t.journalId)?s:null;
  }
  function canConfirmAction(action){
    if(!sameActionConnection(action)||active!==action.target.session)return false;
    const s=matchingTerminal(action);
    if(!s||(s.title||s.shell||'')!==action.target.windowTitle)return false;
    if(action.kind==='rename')return true;
    if(action.managed)return ! $('managed-close').disabled&&!!managedController&&!!managedContext&&managedCurrent(managedContext.target)&&
      managedContext.target.session_id===s.id;
    return action.ack===(action.target.host.capabilities?.close_ack===1)&&(!action.ack||!!s.process_generation);
  }
  function openTerminalAction(kind){
    if(!['rename','close'].includes(kind)||$('terminal-action-dialog').open)return;
    const target=journalTarget(),session=managedSession();
    if(!session||!target.session||!target.window||!target.host.connected||target.host.socket?.readyState!==1){
      notice('请先连接并选择终端。');return;
    }
    const action={kind,target,socket:target.host.socket,managed:session.interaction_mode==='managed',
      ack:target.host.capabilities?.close_ack===1,phase:'confirm',timer:0,request:null,title:''};
    terminalAction=action;
    const rename=kind==='rename',input=$('terminal-action-name'),submit=$('terminal-action-confirm');
    $('terminal-action-heading').textContent=rename?'修改窗口名称':'确认结束终端进程';
    $('terminal-action-target').textContent=[target.host.machine||target.host.host.name,target.windowTitle||target.window,
      (session.provider||M.agentType(session))+' · 终端 '+target.session].join(' / ');
    $('terminal-action-name-label').hidden=!rename;input.disabled=!rename;input.required=rename;input.readOnly=false;
    input.value=rename?target.windowTitle:'';
    submit.dataset.action=kind;submit.textContent=rename?'保存名称':'确认结束进程';submit.disabled=false;
    $('terminal-action-cancel').textContent='取消';
    $('terminal-action-status').textContent=rename?'名称用于此窗口下的终端。':'此终端的运行进程将被终止，其他查看者也会失去此会话。';
    $('terminal-action-dialog').showModal();
    if(!canConfirmAction(action))stopTerminalAction('当前主机尚未提供此操作所需的终端身份，请重新检查连接。');
    else if(rename){input.focus();input.select();}else $('terminal-action-cancel').focus();
  }
  function observeTerminalAction(c,info){
    const action=terminalAction;
    if(!action||action.target.host!==c||!$('terminal-action-dialog').open||action.phase==='finished')return;
    if(!sameActionConnection(action)){stopTerminalAction('连接或授权已变化，请重新核对。');return;}
    if(action.phase==='confirm'){
      if(!canConfirmAction(action))stopTerminalAction('窗口或终端身份已变化，请重新选择。');
      return;
    }
    const t=action.target,ack=info.close_ack;
    if(action.kind==='close'&&!action.managed&&action.ack&&ack?.request===action.request&&
      ack.session===t.session&&ack.window_id===t.window&&ack.process_generation===t.process_generation){
      stopTerminalAction(ack.closed===true?'主机已确认结束选定终端进程。':
        ack.error==='process_still_alive'?'主机尚未确认进程退出，终端仍在目录中。':'主机拒绝了本次请求，请重新核对目标状态。');
      return;
    }
    if(!Array.isArray(info.windows))return;
    const s=matchingTerminal(action);
    if(action.kind==='rename'&&s?.title===action.title)stopTerminalAction('主机已确认窗口名称：'+action.title);
    else if(action.kind==='close'&&(action.managed||!action.ack)){
      if(!c.inventory.some(item=>item.id===t.session)||action.managed&&s?.managed_state==='ended')
        stopTerminalAction(action.managed?'主机已关闭所选独立会话。':'主机目录已移除选定终端。');
      else if(!s)stopTerminalAction('终端身份已变化，原进程退出状态待核对。');
    }
  }
  async function confirmTerminalAction(event){
    event.preventDefault();
    const action=terminalAction;
    if(!action||action.phase!=='confirm')return;
    if(!canConfirmAction(action)){stopTerminalAction('窗口、连接或授权已变化，请重新选择。');return;}
    const t=action.target;
    if(action.kind==='rename'){
      const name=$('terminal-action-name').value.trim();
      if(!name||Array.from(name).length>40||/[\x00-\x1f\x7f]/.test(name)){
        $('terminal-action-status').textContent='请输入 1–40 个字符的窗口名称。';return;
      }
      if(name===t.windowTitle){stopTerminalAction('窗口名称已是该值。');return;}
      action.title=name;
    }
    action.phase='pending';$('terminal-action-confirm').disabled=true;$('terminal-action-name').readOnly=true;
    $('terminal-action-cancel').textContent='关闭';$('terminal-action-status').textContent='请求已提交，等待主机确认…';
    action.timer=setTimeout(()=>{
      if(terminalAction===action&&action.phase==='pending')$('terminal-action-status').textContent='尚未收到主机确认，请查看终端状态。';
    },8000);
    try{
      if(action.kind==='rename'){
        if(!sendHost(t.host,15,{action:'title',window:t.window,title:action.title}))throw Error();
      }else if(action.managed){
        const result=await managedController.closeSession();
        if(terminalAction!==action||action.phase!=='pending'||!sameActionConnection(action))return;
        if(result?.state==='ended')stopTerminalAction('主机已关闭所选独立会话。');
        else if(result?.operation?.phase==='unknown'||result?.state==='unknown')
          $('terminal-action-status').textContent='关闭结果待核对，请查看独立会话状态。';
      }else{
        const params={session:t.session};
        if(action.ack){
          action.request=managedNonce();
          Object.assign(params,{window_id:t.window,process_generation:t.process_generation,request:action.request});
        }
        if(!sendHost(t.host,9,params))throw Error();
      }
    }catch{if(terminalAction===action)stopTerminalAction('请求结果尚未确认，请检查当前主机连接和终端状态。');}
  }
  $('rename-current-window').onclick=()=>openTerminalAction('rename');
  $('close-current-process').onclick=()=>openTerminalAction('close');
  $('terminal-action-form').onsubmit=confirmTerminalAction;
  $('terminal-action-cancel').onclick=closeTerminalAction;
  $('terminal-action-dialog').addEventListener('close',()=>{
    if($('terminal-action-dialog').open)return;
    clearTimeout(terminalAction?.timer);terminalAction=null;$('terminal-action-name').value='';
  });
  $('sessions').onclick=()=>{refreshWatchers();renderHosts();renderList();$('session-dialog').showModal();startActivityTimer();};$('sessions-close').onclick=()=>$('session-dialog').close();
  $('session-dialog').addEventListener('close',()=>{clearInterval(activityTimer);activityTimer=null;});
  $('session-search').oninput=renderList;
  let editingHost=null;
  let hostEditorTarget='';
  function mobileServerAddress(address){
    let value=String(address||'').trim();
    if(!value.includes('://')){const slash=value.indexOf('/'),authority=slash<0?value:value.slice(0,slash),path=slash<0?'':value.slice(slash);value='https://'+(!authority.startsWith('[')&&authority.indexOf(':')!==authority.lastIndexOf(':')?'['+authority+']':authority)+path;}
    const url=new URL(value);if(!['http:','https:'].includes(url.protocol)||url.username||url.password||url.search||url.hash)throw new Error('请填写 IP、域名或不含凭据的完整 URL');
    const authority=value.split('/')[2],explicit=/(?:\]|[^:]):([0-9]+)$/.exec(authority||''),embedded=explicit?Number(explicit[1]):null;
    if(embedded!==null&&(embedded<1||embedded>65535))throw new Error('端口应为 1–65535');
    return {url,embedded};
  }
  function mobileServerEndpoint(address,port=''){
    const {url,embedded}=mobileServerAddress(address);
    if(embedded===null&&String(port).trim()){if(!/^[0-9]{1,5}$/.test(String(port).trim())||Number(port)<1||Number(port)>65535)throw new Error('端口应为 1–65535');url.port=String(Number(port));}
    return url.toString().replace(/\/+$/,'');
  }
  function hostFormTarget(){try{return JSON.stringify([mobileServerEndpoint($('host-endpoint').value,$('host-port').value),$('host-code').value.trim()]);}catch{return '';}}
  function editHost(h=null){
    editingHost=h;$('host-form').reset();$('host-name').value=h?.name||'';
    const endpoint=new URL(h?.endpoint||selected().host.endpoint),port=endpoint.port||(endpoint.protocol==='https:'?'443':'80');endpoint.port='';
    $('host-endpoint').value=(endpoint.protocol==='https:'?endpoint.host:endpoint.origin)+endpoint.pathname.replace(/\/+$/,'');$('host-port').value=port;$('host-port').disabled=false;
    $('host-code').value=h?.code||'';hostEditorTarget=hostFormTarget();$('host-token').value=h?hostTokens.get(h.id)||'':'';$('host-error').hidden=true;$('session-dialog').close();$('host-dialog').showModal();
  }
  $('host-add').onclick=()=>editHost();
  $('host-discover').onclick=()=>{$('host-token').value='';location.assign('/settings');};
  // A credential belongs to an exact normalized endpoint and room. Editing either requires re-entry.
  for(const id of ['host-endpoint','host-port','host-code'])$(id).oninput=()=>{
    if(id==='host-endpoint'){try{const {embedded}=mobileServerAddress($('host-endpoint').value);$('host-port').disabled=embedded!==null;if(embedded!==null)$('host-port').value=String(embedded);}catch{$('host-port').disabled=false;}}
    const target=hostFormTarget();if(target!==hostEditorTarget)$('host-token').value='';hostEditorTarget=target;
  };
  const closeHost=()=>{$('host-token').value='';editingHost=null;$('host-dialog').close();$('session-dialog').showModal();};
  $('host-cancel').onclick=closeHost;$('host-dialog').addEventListener('cancel',e=>{e.preventDefault();closeHost();});
  $('host-form').onsubmit=e=>{e.preventDefault();try{
    const u=new URL(mobileServerEndpoint($('host-endpoint').value,$('host-port').value));
    const name=$('host-name').value.trim();if(!name)throw new Error('请填写电脑名称');if(!editingHost&&hosts.filter(h=>!h.autoDirectory).length>=30)throw new Error('最多保存30台电脑');
    const h=editingHost||{id:'host-'+Array.from(crypto.getRandomValues(new Uint32Array(4)),n=>n.toString(16)).join('-')};const old=editingHost&&targetKey(h);
    Object.assign(h,{name,endpoint:u.toString().replace(/\/+$/,''),code:$('host-code').value.trim()});if(old&&old!==targetKey(h)){
      const c=connection(h);stopSocket(c);hostTokens.delete(h.id);clearAgentModes(h.id);
      c.inventory=[];c.groups=[];c.controls={};c.machine='';c.project='';c.activity.clear();c.agents.clear();c.createPending=false;c.beforeCreate.clear();c.capabilities=null;c.directoryOnline=undefined;c.retry=0;
      for(const key of [...drafts.keys()])if(JSON.parse(key)[0]===h.id)drafts.delete(key);
      for(const key of [...modes.keys()])if(JSON.parse(key)[0]===h.id)modes.delete(key);
      if(h.id===hostId){disposeTerminal();active='';$('draft').value='';$('quick-draft').value='';loadSelected();}
    }
    setHostToken(h.id,$('host-token').value);if(!editingHost)hosts.push(h);saveSessionCredentials();saveHosts();$('host-token').value='';editingHost=null;$('host-dialog').close();connect(connection(h));chooseHost(h.id);refreshWatchers();renderHosts();renderList();
  }catch(error){$('host-error').textContent=error.message;$('host-error').hidden=false;}};
  $('new').onclick=()=>{if($('new-interaction-mode').value==='managed'){createManaged();return;}if(isManagedSession()){notice('请先选择原始 CLI 窗口；新终端不会加入独立对话窗口。');return;}if(!connected)return;const c=selected();c.beforeCreate=new Set(inventory.map(s=>s.id));c.createPending=createPending=send(6,{shell:$('shell').value});$('session-dialog').close();};
  $('control').onclick=toggleControl;
  $('sizing').onchange=()=>{sizing=$('sizing').value;localStorage.setItem('sma.mobile.sizing',sizing);lastSize='';scheduleFit();};
  $('options').onclick=()=>$('settings-dialog').showModal();$('settings-close').onclick=()=>$('settings-dialog').close();
  for(const link of document.querySelectorAll('#settings-dialog a'))link.onclick=e=>{e.preventDefault();location.assign(link.getAttribute('href'));};
  $('font').oninput=()=>{localStorage.setItem('sma.mobile.font',$('font').value);rendererGridGeometry=null;if(term)term.options.fontSize=Number($('font').value);if(historyRenderer){historyRenderer.options.fontSize=Number($('font').value);sizeHistoryRenderer();}scheduleFit();};
  $('renderer').onchange=()=>{renderer=C.rendererName($('renderer').value);if(term)setRenderer();};
  function setToolsCollapsed(value,persist=true){
    const collapsed=!!value;for(const id of ['terminal-actions','keys'])$(id).hidden=collapsed;
    $('tools-toggle').textContent=collapsed?'工具':'收起';$('tools-toggle').setAttribute('aria-expanded',String(!collapsed));$('tools-toggle').title=collapsed?'展开辅助工具栏':'收起辅助工具栏';
    if(persist)localStorage.setItem('sma.mobile.tools-collapsed',collapsed?'1':'0');scheduleFit();
  }
  $('tools-toggle').onclick=()=>{if(isManagedSession()){showJournalRaw(true);return;}setToolsCollapsed($('tools-toggle').getAttribute('aria-expanded')==='true');};
  setToolsCollapsed(localStorage.getItem('sma.mobile.tools-collapsed')!=='0',false);
  $('ctrl').onpointerdown=e=>e.preventDefault();$('ctrl').onclick=()=>{if(composerSubmit.pending)composerSubmit.cancel('auxiliary_control');setCtrl(!ctrl);};
  const keys={escape:'\x1b',tab:'\t',f2:'\x1bOQ',enter:'\r',up:'\x1b[A',down:'\x1b[B',left:'\x1b[D',right:'\x1b[C'};
  for(const b of document.querySelectorAll('[data-key]')){b.onpointerdown=e=>e.preventDefault();b.onclick=()=>key(C.cursorKey?.(term,b.dataset.key)||keys[b.dataset.key]);}
  $('mode').onclick=()=>{modes.set(sessionKey(),'buffered');setMode(true);};
  $('model').onpointerdown=e=>e.preventDefault();$('model').onclick=openModelMenu;
  $('github').onclick=()=>{modes.set(sessionKey(),'buffered');setMode(true);if(!$('draft').value){syncDraftFields('提交全部文件到 GitHub');updateComposer();}else $('composer-status').textContent='已保留现有草稿；清空后可再次选择“提交 GitHub”。';};
  $('composer-close').onclick=closeComposer;
  $('composer').addEventListener('cancel',e=>{e.preventDefault();closeComposer();});
  function bindComposerField(id,source){
    const field=$(id);let newline=false;
    field.addEventListener('input',()=>{syncDraftFields(field.value,source);updateComposer();});
    field.addEventListener('focus',()=>setRawInput(false,false,true));
    field.addEventListener('compositionstart',()=>composing=true);field.addEventListener('compositionend',()=>{composing=false;setTimeout(flushKeys,0);});
    field.onkeydown=e=>{const action=M.composerAction(e,composing);newline=action==='newline';if(action==='send'){e.preventDefault();submit();}};
    field.onbeforeinput=e=>{if(newline){newline=false;return;}if(M.composerAction(e,composing)==='send'){e.preventDefault();submit();}};
  }
  bindComposerField('draft','draft');bindComposerField('quick-draft','quick');
  $('raw-input').onclick=()=>setRawInput(!nativeRawInput,true,true);
  $('send').onclick=submit;$('quick-composer').onsubmit=e=>{e.preventDefault();submit();};
  function viewHistory(action){
    if(!term)return;cancelTouchMomentum();
    if(scrollFrame!==null)cancelAnimationFrame(scrollFrame);scrollFrame=null;scrollRows=0;
    action(term);historyStatus();
  }
  function codexPagingAvailable(){
    if(isManagedSession()||!term||!ready||!connected||paused||replaying)return false;
    const buffer=term.buffer.active,session=inventory.find(item=>item.id===active)||{};
    return (buffer.type==='alternate'||buffer.baseY===0)&&M.codexOwnedPaging(session);
  }
  function remoteHistoryPage(action){
    const intent={'page-up':'up','page-down':'down','history-top':'start','history-bottom':'end'}[action];
    if(C.localHistoryPolicy(intent,localHistoryContext('button')).route!=='native')return false;
    const keys={'page-up':'\x1b[5~','page-down':'\x1b[6~','history-top':'\x1b[1;5H','history-bottom':'\x1b[1;5F'};
    if(!input(keys[action]))return false;notice('正在翻阅 Codex 历史。');return true;
  }
  function historyPage(direction){
    if(!term)return;
    if(remoteHistoryPage(direction<0?'page-up':'page-down'))return;
    localView().following=C.localHistoryPolicy(direction<0?'up':'down',localHistoryContext('button')).follow;if(direction<0)localView().bottom=false;
    if(historyRenderer){const screen=$('screen-history'),before=screen.scrollTop;screen.scrollTop+=direction*Math.max(1,screen.clientHeight-24);if(Math.abs(screen.scrollTop-before)>1)return;const archive=screenHistory();direction<0?archive.previous():archive.next();renderScreenHistory(direction>0&&term.buffer.active.type==='normal'&&term.buffer.active.baseY>0);return;}
    const line=term._core._renderService.dimensions.css.cell.height*(Number($('screen').dataset.scale)||1),screen=$('screen');
    if(direction<0&&screen.scrollTop<=1&&(term.buffer.active.type==='alternate'||term.buffer.active.viewportY===0&&archivedHistoryAvailable())){const archive=screenHistory();if(term.buffer.active.type==='normal'&&term.buffer.active.baseY>0){archive.top();for(let i=1;i<archive.length;i++)archive.next();}else{captureScreenHistory(true);archive.previous();}renderScreenHistory();return;}
    localScroll(direction*Math.max(1,visibleRows()-1)*line,'button');
  }
  const historyActions={'page-up':()=>historyPage(-1),'page-down':()=>historyPage(1),'history-top':t=>{if(remoteHistoryPage('history-top'))return;if(historyRenderer||t.buffer.active.type==='alternate'||archivedHistoryAvailable()){if(t.buffer.active.type==='alternate'||t.buffer.active.baseY===0)captureScreenHistory(true);screenHistory().top();renderScreenHistory();return;}t.scrollToTop();localView().bottom=false;$('screen').scrollTop=0;},'history-bottom':t=>{if(remoteHistoryPage('history-bottom'))return;leaveScreenHistory();if(t.buffer.active.type==='normal')t.scrollToBottom();localBottom();}};
  for(const [id,action] of Object.entries(historyActions)){$(id).onpointerdown=e=>e.preventDefault();$(id).onclick=()=>viewHistory(action);}
  $('bottom').onclick=()=>viewHistory(historyActions['history-bottom']);
  $('refresh-screen').onpointerdown=e=>e.preventDefault();
  $('refresh-screen').onclick=refreshScreen;
  function openTextSelection(selectAll=true){
    if(!term)return;cancelTerminalPress();copyTargetKey=sessionKey();const b=term.buffer.active;
    const text=historyRenderer?screenHistory().current.lines.join('\n'):Array.from({length:term.rows},(_,i)=>M.visibleBufferLine(b.getLine(b.viewportY+i),term.cols)).join('\n');
    $('copy-text').value=text;$('settings-dialog').close();if(!$('copy-dialog').open)$('copy-dialog').showModal();$('copy-text').inputMode='none';$('copy-text').focus();if(selectAll)$('copy-text').select();else $('copy-text').setSelectionRange(0,0);
  }
  function clipboardTarget(){return active&&copyTargetKey===sessionKey()&&$('copy-dialog').open&&!composing&&!submissionPending()&&!uploadJob?sessionKey():null;}
  function pasteClipboardText(expectedKey,text){
    if(expectedKey!==clipboardTarget()||typeof text!=='string'||!text||text.length>65536)return false;
    const field=$('draft'),start=field.selectionStart??field.value.length,end=field.selectionEnd??start;
    field.setRangeText(text,start,end,'end');syncDraftFields(field.value,'draft');$('copy-dialog').close();modes.set(sessionKey(),'buffered');setMode(false,true);return true;
  }
  $('copy').onclick=()=>openTextSelection(true);$('copy-close').onclick=()=>$('copy-dialog').close();
  new ResizeObserver(()=>{scheduleFit();updateTerminalStatus();}).observe($('screen'));window.visualViewport?.addEventListener('resize',scheduleFit);
  new ResizeObserver(sizeHistoryRenderer).observe($('screen-history'));
  function pause(){if(paused)return;mobileFeedbackQa?.disable();paused=true;cancelTerminalPress();cancelTouchMomentum();cancelAttachmentUpload();pauseDebugGrid();clearInterval(activityTimer);activityTimer=null;if(active)drafts.set(sessionKey(),$('draft').value);for(const c of connections.values())stopSocket(c);refreshWatchers();writer?.dispose();if(fitFrame!==null)cancelAnimationFrame(fitFrame);fitFrame=null;if(scrollFrame!==null)cancelAnimationFrame(scrollFrame);scrollFrame=null;state('后台已暂停连接；服务器终端继续运行');}
  function resume(){if(!paused)return;paused=false;startActivityTimer();for(const c of connections.values())connect(c);refreshWatchers();resumeDebugGrid();if(webgl&&term)term.refresh(0,term.rows-1);}
  document.addEventListener('visibilitychange',()=>document.hidden?pause():resume());
  // WebGL incremental redraw can leave stale glyph overlays over time; a
  // periodic full row refresh clears them while the UI is visible.
  setInterval(()=>{if(!paused&&webgl&&term)term.refresh(0,term.rows-1);},15000);
  window.addEventListener('pagehide',pause);
  function captureUIState(){
    if(composing||composerSubmit.pending||uploadJob||[...attachmentDrafts.values()].some(draft=>draft.items.some(item=>!item.receipt)))return null;
    pruneNativeUncertainty();if([...nativeReceiptScopes.values()].some(scope=>scope.proofPending||!scope.authKey))return null;
    if(active)drafts.set(sessionKey(),$('draft').value);saveSessionCredentials();rememberManagedPending();
    return {version:1,hostId,activeSid:active,drafts:[...drafts],modes:[...modes],uncertainDrafts:[...uncertainDrafts],uncertainModelCommands:[...uncertainModelCommands],nativeUncertaintyTargets:nativeUncertaintyTargets(),journalCodePreferences:[...journalCodePreferences],managedPending:[...managedPendingRecords],managedCreations:[...managedCreations]};
  }
  function prepareUIReload(){const state=captureUIState();if(!state)return null;pause();$('draft').disabled=true;return state;}
  function cancelUIReload(){$('draft').disabled=false;resume();}
  function openNotification(value){
    if(!value||typeof value.session!=='string')return false;
    let wanted;try{wanted=targetKey({endpoint:value.endpoint,code:value.code||''});}catch{return false;}
    const host=[home,...hosts].find(h=>targetKey(h)===wanted),c=host&&connection(host);
    if(!c||!c.inventory.some(s=>s.id===value.session))return false;
    document.querySelectorAll('dialog[open]').forEach(d=>d.close());chooseHost(host.id);activate(value.session);return true;
  }
  function restoreSelected(){
    if(!restoringUI||restoringUI.hostId!==hostId||!inventory.some(s=>s.id===restoringUI.activeSid))return;
    const sid=restoringUI.activeSid;restoringUI=null;activate(sid);syncDraftFields(drafts.get(sessionKey())||'');setMode(false);
  }
  function restoreUIState(value){
    if(!value||value.version!==1)return;
    const h=[home,...hosts].find(h=>h.id===value.hostId);
    if(!h&&!(typeof value.hostId==='string'&&value.hostId.startsWith('discovered:')))return;
    // Leave the startup host before restoring drafts: chooseHost saves the
    // visible editor, which may still be empty after a fresh navigation.
    restoringUI={hostId:value.hostId,activeSid:value.activeSid};if(h)chooseHost(h.id,true);
    for(const [key,text] of value.drafts||[])drafts.set(key,text);
    for(const [key,mode] of value.modes||[])modes.set(key,mode);
    for(const [key,text] of value.uncertainDrafts||[])uncertainDrafts.set(key,text);
    for(const key of value.uncertainModelCommands||[])if(typeof key==='string')uncertainModelCommands.add(key);
    restoreNativeUncertaintyTargets(value.nativeUncertaintyTargets);
    for(const row of value.journalCodePreferences||[]){try{const parts=JSON.parse(row[0]);if(row.length===2&&typeof row[1]==='boolean'&&Array.isArray(parts)&&parts.length===4&&parts.every(part=>typeof part==='string'))journalCodePreferences.set(row[0],row[1]);}catch{}}
    for(const row of (value.managedPending||[]).slice(0,32))if(Array.isArray(row)&&typeof row[0]==='string'&&row[1]?.schema===1)managedPendingRecords.set(row[0],row[1]);
    for(const row of (value.managedCreations||[]).slice(0,32))if(Array.isArray(row)&&typeof row[0]==='string'&&row[1]?.nonce)managedCreations.set(row[0],{...row[1],phase:'unknown'});
    saveManagedRecords();
    if(active)syncDraftFields(drafts.get(sessionKey())||'');
    restoreSelected();if(isManagedSession())syncManagedContext();
  }
  window.MobileTerminal={roomSelectionVersion:1,pause,resume,captureUIState,restoreUIState,prepareUIReload,cancelUIReload,openNotification,clipboardTarget,pasteClipboardText,reconnect:()=>{if(paused)resume();else{connect();refreshWatchers();}},chooseHost,get hosts(){return [...connections.values()].map(c=>({id:c.host.id,connected:c.connected,status:c.status,sessions:c.inventory.map(s=>s.id),subscription:c.subscription}));},get terminal(){return term;},get active(){return active;},get activeKey(){return sessionKey();},get connected(){return connected;},get ready(){return ready;},get controls(){return controls[active]||null;},revision:config.uiRevision||'bundled',metrics};
  // MOBILE_FEEDBACK_QA_START
  function createMobileFeedbackQa(profile){
    if(!profile||typeof profile!=='object'||Array.isArray(profile)||!window.SuperMonoCockpitFeedback?.open)return null;
    const fields=Object.keys(profile).sort().join(',');
    if(fields!=='endpoint,host_installation_id,project,room'||!managedID(profile.project)||profile.project==='*'||
      !managedID(profile.host_installation_id)||typeof profile.room!=='string'||profile.room.length>128)return null;
    let endpoint;
    try{endpoint=C.cockpitEndpoint(profile.endpoint);}catch{return null;}
    const checkbox=document.createElement('input');
    checkbox.type='checkbox';checkbox.id='cockpit-enable-qa';checkbox.checked=false;
    const label=document.createElement('label');
    label.htmlFor=checkbox.id;
    label.append(checkbox,document.createTextNode(' 本次开启驾驶舱反馈测试 · '+profile.project));
    $('settings-dialog').append(label);
    let epoch={},capture=null;
    const issued=new WeakSet();
    function invalidate(){
      capture=null;epoch={};
      window.dispatchEvent(new Event('sma-cockpit-context-invalidated'));
    }
    function disable(){checkbox.checked=false;invalidate();}
    checkbox.onchange=()=>{if(!checkbox.checked)invalidate();};
    function eligible(){return checkbox.checked&&!paused&&!document.hidden;}
    function allowsTarget(t){
      return eligible()&&t?.host===selected()&&t.host.connected&&t.host.socket?.readyState===1&&
        t.host.cockpitInstallationId===profile.host_installation_id&&t.room===profile.room&&
        t.host.capabilities?.cockpit_feedback===1&&managedCurrent(t);
    }
    function describe(attempt){
      if(!eligible()||attempt?.host_installation_id!==profile.host_installation_id||!term||
        !['window_id','terminal_session_id','generation','attempt_id','tool'].every(key=>managedID(attempt[key])))return null;
      const t=managedTarget(),source=managedSession();
      if(!source||!journalTargetCurrent(t)||!t.host.connected||t.host.socket?.readyState!==1||!t.token||
        t.room!==profile.room||t.host.cockpitInstallationId!==profile.host_installation_id||
        t.window_id!==attempt.window_id||t.session_id!==attempt.terminal_session_id||t.process_generation!==attempt.generation)return null;
      const managed=t.interaction_mode==='managed';
      if(managed&&(source.provider!==attempt.tool||!attempt.journal_id||t.journal_id!==attempt.journal_id))return null;
      const tuple=Object.fromEntries(managedTargetFields.map(k=>[k,t[k]]));
      const ref=Object.freeze({host:t.host,ws:t.host.socket,hostGeneration:t.generation,pane:epoch,
        sessionId:t.session_id,title:source.title||t.window_id,managed,canSend:managed&&allowsTarget(t),
        token:t.token,room:t.room,relay:t.endpoint,terminal:term,source:t,attempt:{...attempt},
        capture:managed?{target:Object.freeze(tuple),targetKey:journalContextKey(t)}:null});
      capture=ref;issued.add(ref);
      return ref;
    }
    function current(ref){
      if(!ref||!issued.has(ref)||!eligible()||ref.pane!==epoch||ref.terminal!==term||!journalTargetCurrent(ref.source)||
        ref.ws!==ref.host.socket||ref.ws.readyState!==1||!ref.host.connected||
        ref.host.cockpitInstallationId!==profile.host_installation_id)return false;
      const s=managedSession();
      if(!s||s.window!==ref.attempt.window_id||s.process_generation!==ref.attempt.generation||
        (s.title||s.window)!==ref.title||(s.interaction_mode==='managed')!==ref.managed)return false;
      return !ref.managed||(s.provider===ref.attempt.tool&&s.journal_id===ref.attempt.journal_id&&
        ref.canSend===allowsTarget(ref.source));
    }
    function request(ref,action,fields){
      const allowed=action==='submit_cockpit_feedback'||action==='cockpit_feedback_status';
      const f=fields?.feedback;
      if(!allowed||!current(ref)||!ref.canSend||f?.project!==profile.project||
        f.host_installation_id!==profile.host_installation_id||f.attempt_id!==ref.attempt.attempt_id||
        f.tool!==ref.attempt.tool||f.role!==ref.attempt.role||f.account!==ref.attempt.account)
        return Promise.reject(journalError('stale_target'));
      return managedRPC(ref.source,action,{...fields,...ref.capture.target});
    }
    function prepareDraft(ref,text){
      if(!current(ref)||ref.managed||typeof text!=='string'||!text.trim()||composing||submissionPending()||
        uploadJob||attachmentsPending()||uncertainDrafts.has(sessionKey())||uncertainModelCommands.has(sessionKey()))return false;
      const previous=$('draft').value;
      const next=previous+(previous&&!previous.endsWith('\n')?'\n':'')+text;
      if(encoder.encode(next).length>65536)return false;
      syncDraftFields(next);modes.set(sessionKey(),'buffered');setMode(true,true);
      return true;
    }
    const hostInterface=Object.freeze({describe,request,prepareDraft});
    function select(project,viewerEndpoint){
      return eligible()&&project===profile.project&&viewerEndpoint===endpoint?hostInterface:null;
    }
    const checkBinding=()=>{if(capture&&!current(capture))invalidate();};
    window.addEventListener('orientationchange',invalidate);
    window.screen?.orientation?.addEventListener?.('change',invalidate);
    return {select,allowsTarget,disable,invalidate,checkBinding};
  }
  // MOBILE_FEEDBACK_QA_END
  mobileFeedbackQa=createMobileFeedbackQa(config.cockpitFeedbackQA);
  window.MobileTerminal.closeWindowAction=closeTerminalAction;
  const cockpit=C.mountCockpit?.({
    menu:$('settings-dialog'),headerBefore:$('options'),mobile:true,
    mobileFeedbackHost:(project,endpoint)=>mobileFeedbackQa?.select(project,endpoint),
    beforeOpen:()=>{if($('settings-dialog').open)$('settings-dialog').close();}
  });
  if(cockpit)window.SuperMonoCockpit=cockpit;
  for(const h of [home,...hosts])connection(h);
  if(document.hidden){paused=true;state('Background UI paused');renderHosts();renderList();}
  else{startActivityTimer();for(const c of connections.values())connect(c);refreshWatchers();}
})();
