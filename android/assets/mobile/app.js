/* Android-only terminal document. The daemon remains the owner of every shell. SMA_ROOM_SELECTION_V1 SMA_ROOM_SELECTION_V2 */
(() => {
  'use strict';
  const $=id=>document.getElementById(id),C=TerminalClient,M=MobileCore;
  const encoder=new TextEncoder(),decoder=new TextDecoder();
  let config;
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

  function connection(h){let c=connections.get(h.id);if(!c){c={host:h,socket:null,generation:0,retry:0,retryTimer:null,pingTimer:null,lastPing:0,connected:false,inventory:[],groups:[],activity:new Map(),agents:new Map(),controls:{},machine:'',project:'',subscription:'',status:'等待连接',createPending:false,beforeCreate:new Set()};connections.set(h.id,c);}return c;}
  let paused=false,connected=false;
  let inventory=[],active='',term=null,fit=null,writer=null,highlighter=null,webgl=null,renderer=config.renderer||'auto';
  const agentModeIds=new Set(['auto','codex','claude-code','grok','kimi','pi-dev','cursor-cli']);
  const agentModePreferences=new Map();
  let agentModeGeneration=0,agentModeRequest=null;
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
    const apply=profile=>{if(!valid())return;current.options.theme={...profile.theme};decorator.setProfile(profile);$('screen').dataset.agentMode=mode;$('screen').dataset.agentPreference=preference;$('agent-mode-status').textContent=(preference==='auto'?'Auto · ':'')+profile.label;};
    if(mode==='auto'){apply(C.commonAgentProfile);return;}
    const url=new URL('agent-modes/'+mode+'.json',location.href);url.search='';url.hash='';url.searchParams.set('ui_revision',revision);
    $('agent-mode-status').textContent='Loading '+mode+'…';
    C.loadAgentProfile(url.toString(),mode).then(apply).catch(()=>{
      if(!valid())return;current.options.theme={...C.commonAgentProfile.theme};decorator.setProfile(C.commonAgentProfile);$('screen').dataset.agentMode='auto';$('agent-mode-status').textContent='Profile unavailable · '+C.commonAgentProfile.label;
    });
  }
  $('agent-mode').disabled=true;
  $('agent-mode').onchange=()=>{if(!term||!active)return;const value=$('agent-mode').value;if(!agentModeIds.has(value))return;agentModePreferences.set(agentModeKey(),value);saveAgentModes();refreshAgentMode(true);};
  $('highlight').checked=localStorage.getItem('sma.mobile.highlight')!=='off';
  $('highlight').onchange=()=>{localStorage.setItem('sma.mobile.highlight',$('highlight').checked?'on':'off');highlighter?.setEnabled($('highlight').checked);};
  let ready=false,replaying=0,fitFrame=null,lastSize='',control=false,ctrl=false,composing=false,pendingKeys=[];
  const uncertainDrafts=new Map();
  const composerSubmit=M.createComposerSubmitter({onPending:pending=>{updateComposer(pending);$('draft').readOnly=pending;$('composer-close').disabled=pending;}});
  let controls={};
  let waitingFeeds=[],waitingBytes=0;
  let sizing=localStorage.getItem('sma.mobile.sizing')==='shared'?'shared':'phone';
  $('sizing').value=sizing;
  let createPending=false;
  const modes=new Map(),drafts=new Map();
  const metrics={connections:0,frames:0,bytes:0,writes:0,fallbacks:0};
  let replayNoticeTimer=null,pendingRefresh=null,refreshFeedbackTimer=null;
  const replayFeeds=new WeakMap();
  let debugPage=0,debugCapacity=16,debugSignature='',debugFrame=null,debugTimer=null;
  const debugMonitors=new Map(),debugCards=new Map();
  const notice=text=>{clearTimeout(replayNoticeTimer);replayNoticeTimer=null;$('notice').textContent=text;$('notice').hidden=!text;};
  function replayNotice(text){notice(text);if(text)replayNoticeTimer=setTimeout(()=>{replayNoticeTimer=null;if($('notice').textContent===text){$('notice').textContent='';$('notice').hidden=true;}},3000);}
  function cancelRefresh(){clearTimeout(refreshFeedbackTimer);refreshFeedbackTimer=null;if(!pendingRefresh){$('refresh-screen').textContent='\u5237\u65b0';return;}clearTimeout(pendingRefresh.timer);pendingRefresh=null;$('refresh-screen').textContent='\u5237\u65b0';$('refresh-screen').removeAttribute('aria-busy');}
  function refreshScreen(){
    if(pendingRefresh||!term||!active||!connected||paused)return;
    const c=selected(),buffer=term.buffer.active,screen=$('screen');
    const request={host:c,generation:c.generation,sid:active,terminal:term,distance:buffer.baseY-buffer.viewportY,left:screen.scrollLeft,top:screen.scrollTop,bottom:localView().bottom};
    pendingRefresh=request;
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
  function renderHosts(){
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
    if(active)drafts.set(sessionKey(),$('draft').value);
    const previous=selected();sendHost(previous,16,{sessions:[]});previous.subscription='';
    disposeTerminal();active='';hostId=id;loadSelected();$('draft').value='';notice('');
    if(inventory.length)activate(inventory[0].id);renderHosts();renderList();
  }
  function endpoint(h){
    const u=new URL(C.secureEndpoint(h.endpoint));
    if(!['http:','https:'].includes(u.protocol)||u.username||u.password||u.search||u.hash)throw new Error('无效的服务器地址');
    u.pathname=u.pathname.replace(/\/+$/,'')+'/ws';u.protocol=u.protocol==='https:'?'wss:':'ws:';return u.toString();
  }
  function sendHost(c,op,value){if(!c.connected||paused||c.socket?.readyState!==1)return false;c.socket.send(C.pack(op,value instanceof Uint8Array?value:encoder.encode(JSON.stringify(value))));return true;}
  function send(op,value){return sendHost(selected(),op,value);}
  function stopSocket(c=selected()){
    if(pendingRefresh?.host===c)cancelRefresh();
    c.generation++;clearTimeout(c.retryTimer);clearInterval(c.pingTimer);c.retryTimer=c.pingTimer=null;
    if(c.socket){c.socket.onclose=null;c.socket.close();c.socket=null;}c.connected=false;c.subscription='';
    if(c===selected()){composerSubmit.cancel();connected=false;ready=false;pendingKeys=[];composing=false;setCtrl(false);if(term)term.options.disableStdin=true;}
  }
  function hostState(c,text){c.status=text;if(c===selected())state(text);renderHosts();renderList();}
  function subscribeSessions(c,refresh=''){
    // TD-006D/I: unopened terminals never subscribe to output. Activity dots use
    // received visible-terminal activity only; directory state is independent.
    const session=c===selected()?active:'';
    if((refresh||c.subscription!==session)&&sendHost(c,16,{sessions:session?[session]:[]}))c.subscription=session;
  }
  function connect(c=selected()){
    if(paused)return;stopSocket(c);const gen=c.generation;c.lastPing=0;hostState(c,'正在连接');
    if(!roomAllowed(c.host)){hostState(c,'此房间未勾选，请返回连接设置重新选择');return;}
    if(c===selected()&&active){const sid=active;drafts.set(sessionKey(),$('draft').value);disposeTerminal();active='';activate(sid);}
    let url;try{url=endpoint(c.host);}catch(e){hostState(c,e.message);return;}
    const ws=c.socket=new WebSocket(url);ws.binaryType='arraybuffer';metrics.connections++;
    const current=()=>gen===c.generation;
    const hello=()=>ws.send(C.pack(1,encoder.encode(JSON.stringify({token:hostTokens.get(c.host.id)||'',multiplex:true,reliable:true}))));
    ws.onopen=()=>{if(!current())return;if(c.host.code)ws.send(JSON.stringify({op:'phone',code:c.host.code,token:hostTokens.get(c.host.id)||'',reliable:true}));else hello();};
    ws.onmessage=event=>{
      if(!current())return;
      try{
        if(typeof event.data==='string'){const m=JSON.parse(event.data);if(m.op==='ready')hello();else if(m.error)hostState(c,String(m.error));return;}
        const {op,payload}=C.unpack(event.data);metrics.frames++;metrics.bytes+=payload.length;
        if(op===2||op===7)sync(JSON.parse(decoder.decode(payload)),c);
        else if(op===3||op===8){
          if(!payload.length)return;const feed=C.sessionPayload(payload);
          if(op===3&&feed.bytes.length)c.activity.set(feed.session,performance.now());
          if(c!==selected()||feed.session!==active||!writer)return;
          const replay=op===8;
          if(!controls[active]){if(waitingBytes+feed.bytes.length>524288){M.closeForRecovery(ws,'等待终端尺寸的输出积压');return;}waitingFeeds.push({sid:active,bytes:feed.bytes,replay});waitingBytes+=feed.bytes.length;}
          else consumeFeed(active,feed.bytes,replay);
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
      hostTokens.set(id,token);h.name=String(entry.machine||entry.code);c.machine=String(entry.machine||'');c.project=String(entry.project||'');c.directoryOnline=true;
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
  function consumeFeed(sid,bytes,replay){
    if(sid!==active||!writer||!controls[sid])return;
    const current=term,c=selected(),generation=c.generation;
    const refresh=replay&&pendingRefresh?.host===c&&pendingRefresh.sid===sid&&pendingRefresh.generation===generation&&pendingRefresh.terminal===current?pendingRefresh:null;
    const previous=replayFeeds.get(current);
    const identical=!!refresh&&previous?.length===bytes.length&&bytes.every((value,index)=>value===previous[index]);
    if(replay)replayFeeds.set(current,bytes.length<=262144?bytes.slice():null);
    else if(previous&&previous.length+bytes.length<=262144){const combined=new Uint8Array(previous.length+bytes.length);combined.set(previous);combined.set(bytes,previous.length);replayFeeds.set(current,combined);}
    else replayFeeds.set(current,null);
    if(replay){replaying++;ready=false;}
    // A byte-identical authorized replay is already rendered. Avoid resetting
    // its scrollback while still acknowledging the server snapshot in order.
    writer.push(identical?new Uint8Array():replay?C.snapshotBytes(bytes,sid,c.capabilities?.snapshot_sgr===true):bytes,replay&&!identical,()=>{
      if(sid!==active||term!==current||selected()!==c||c.generation!==generation)return;
      if(replay){
        replaying=Math.max(0,replaying-1);ready=connected;term.options.disableStdin=!ready;
        if(refresh&&pendingRefresh===refresh){
          const buffer=term.buffer.active;if(buffer.type==='normal')term.scrollToLine(Math.max(0,buffer.baseY-refresh.distance));
          localView().bottom=refresh.bottom;const screen=$('screen');screen.scrollLeft=refresh.left;screen.scrollTop=refresh.bottom?screen.scrollHeight:refresh.top;
          cancelRefresh();$('refresh-screen').textContent='\u5df2\u5237\u65b0';refreshFeedbackTimer=setTimeout(()=>{if(term===current&&!pendingRefresh)$('refresh-screen').textContent='\u5237\u65b0';},3000);
        }else{term.scrollToBottom();localBottom();replayNotice(bytes.length?'\u6709\u9650\u5386\u53f2\u5df2\u6062\u590d\uff1b\u5168\u5c4f\u7a0b\u5e8f\u53ef\u80fd\u9700\u8981\u81ea\u884c\u5237\u65b0':'');}
      }
      updateTerminalStatus();historyStatus();scheduleFit();
    },controls[sid]);
  }
  function sync(info,c=selected()){
    if(!info.capabilities?.multiplex){stopSocket(c);hostState(c,'后台版本不兼容');return;}
    if(!c.connected){c.connected=true;c.retry=0;c.status='已连接';c.pingTimer=setInterval(()=>{if(c.lastPing&&performance.now()-c.lastPing>30000){M.closeForRecovery(c.socket,'连接探测超时');return;}if(!c.lastPing)c.lastPing=performance.now();sendHost(c,10,encoder.encode('mobile'));},15000);}
    if(info.error)c.status=String(info.error);c.capabilities=info.capabilities||c.capabilities;
    if(info.controls)c.controls=info.controls;
    if(info.machine!==undefined)c.machine=String(info.machine||'');if(info.project!==undefined)c.project=String(info.project||'');
    if(info.groups)c.groups=info.groups;
    if(typeof info.selected==='string')c.serverWindow=info.selected;if(typeof info.session==='string')c.serverSession=info.session;
    if(info.windows)c.inventory=info.windows.flatMap(w=>(w.sessions||[]).map(s=>({...s,learnedAgent:c.agents.get(s.id)||'',title:w.title,window:w.id,windowFocused:w.focused,group:w.group})));
    if(info.windows){const ids=new Set(c.inventory.map(s=>s.id));for(const cache of [c.activity,c.agents])for(const sid of cache.keys())if(!ids.has(sid))cache.delete(sid);}
    if(c!==selected()){renderHosts();renderList();return;}
    loadSelected();renderHosts();renderList();
    if(c.createPending){const created=inventory.find(s=>!c.beforeCreate.has(s.id));if(created){c.createPending=false;createPending=false;activate(created.id);}}
    restoreSelected();
    if(!inventory.some(s=>s.id===active)){if(inventory.length)activate(inventory[0].id);else{disposeTerminal();active='';}}
    if(controls[active]){const size=controls[active];control=!!size.owner;$('control').textContent=control?'主控':'接管';if(term&&(term.cols!==size.cols||term.rows!==size.rows))writer?.push(new Uint8Array(),false,scheduleFit,size);scheduleFit();}
    if(controls[active]&&waitingFeeds.length){const feeds=waitingFeeds;waitingFeeds=[];waitingBytes=0;for(const feed of feeds)consumeFeed(feed.sid,feed.bytes,feed.replay);}
    subscribeSessions(c);refreshAgentMode();
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
  let activityTimer=null;
  function startActivityTimer(){clearInterval(activityTimer);activityTimer=setInterval(()=>{if($('session-dialog').open)updateSessionDots();},300);}
  function renderList(){
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
    $('session-count').textContent=count+' 个终端';$('new').disabled=!connected||createPending;const current=inventory.find(s=>s.id===active);if(current)$('title').textContent=current.title+'（'+M.agentType(current,current.title)+'）';
    updateSessionDots();scheduleDebugGrid();
  }
  function debugEntries(){
    const entries=new Map();
    for(const c of connections.values()){
      const relay=safeEndpoint(c.host);if(!c.host.code||!relay||!roomAllowed(c.host))continue;
      for(const session of c.inventory){
        const key=JSON.stringify([relay,c.host.code,session.id]),previous=entries.get(key);
        if(!previous||!previous.host.connected)entries.set(key,{key,host:c,session});
      }
    }
    return [...entries.values()];
  }
  function stopDebugMonitors(){
    for(const monitor of debugMonitors.values()){
      monitor.dead=true;clearTimeout(monitor.retryTimer);clearInterval(monitor.pingTimer);
      if(monitor.socket){monitor.socket.onclose=null;monitor.socket.close();}
      for(const preview of monitor.previews.values())preview.dispose();
    }
    debugMonitors.clear();
  }
  function startDebugMonitor(host,items){
    // A separate read-only client keeps the visible terminal's subscriptions,
    // resize lease, drafts and history position entirely untouched.
    const monitor={host,items,socket:null,previews:new Map(),prompts:new Map(),activity:new Map(),dead:false,ready:false,failed:false,retry:0,retryTimer:null,pingTimer:null};
    debugMonitors.set(host.host.id,monitor);
    const valid=()=>!monitor.dead&&!paused&&$('debug-dialog').open;
    function connectPreview(){
      if(!valid())return;
      monitor.ready=false;monitor.failed=false;monitor.lastPing=0;
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
      const hello=()=>sendPreview(1,{token:hostTokens.get(host.host.id)||'',multiplex:true,reliable:true});
      socket.onopen=()=>{if(!current())return;socket.send(JSON.stringify({op:'phone',code:host.host.code,token:hostTokens.get(host.host.id)||'',reliable:true}));};
      socket.onmessage=event=>{
        if(!current())return;
        try{
          if(typeof event.data==='string'){const message=JSON.parse(event.data);if(message.op==='ready')hello();else if(message.error)socket.close(4000,'调试连接不可用');return;}
          const {op,payload}=C.unpack(event.data);
          if(op===2||op===7){
            const info=JSON.parse(decoder.decode(payload));if(!info.capabilities?.multiplex){socket.close(4000,'调试协议不兼容');return;}
            monitor.preservesStyle=info.capabilities.snapshot_sgr===true;monitor.ready=true;monitor.retry=0;
            for(const item of items){const size=info.controls?.[item.session.id];if(size)monitor.previews.get(item.session.id)?.setGrid(size);}
            if(!subscribed){subscribed=true;sendPreview(16,{sessions:items.map(item=>item.session.id)});}
          }else if(op===3||op===8){
            const feed=C.sessionPayload(payload),preview=monitor.previews.get(feed.session);if(!preview)return;
            if(op===3&&feed.bytes.length)monitor.activity.set(feed.session,performance.now());
            preview.push(op===8?C.snapshotBytes(feed.bytes,feed.session,monitor.preservesStyle):feed.bytes,op===8);
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
    tile.onclick=()=>{if(!connections.get(item.host.host.id)?.inventory.some(s=>s.id===item.session.id))return;$('debug-dialog').close();chooseHost(item.host.host.id);activate(item.session.id);};
    $('debug-grid').append(tile);debugCards.set(item.key,{tile,name,host,status,marquee,text,repeat});
  }
  function scheduleDebugGrid(){
    if(paused||!$('debug-dialog').open||debugFrame!==null)return;
    debugFrame=requestAnimationFrame(()=>{debugFrame=null;renderDebugGrid();});
  }
  function renderDebugGrid(){
    if(paused||!$('debug-dialog').open)return;
    const page=M.debugGridPage(debugEntries(),debugPage,debugCapacity);debugPage=page.page;
    const signature=JSON.stringify([debugPage,debugCapacity,page.items.map(item=>[item.key,item.host.host.id])]);
    if(signature!==debugSignature){
      stopDebugMonitors();debugCards.clear();$('debug-grid').replaceChildren();debugSignature=signature;
      const groups=new Map();for(const item of page.items){createDebugCard(item);const id=item.host.host.id;if(!groups.has(id))groups.set(id,[]);groups.get(id).push(item);}
      for(const items of groups.values())startDebugMonitor(items[0].host,items);
    }
    $('debug-grid').style.setProperty('--debug-columns',debugCapacity===16?'4':'3');$('debug-grid').dataset.density=String(debugCapacity);
    $('debug-count').textContent='共 '+page.total+' 个终端';$('debug-page').textContent=(page.page+1)+' / '+page.pages;
    $('debug-prev').disabled=page.page===0;$('debug-next').disabled=page.page===page.pages-1;
    $('debug-empty').hidden=page.total>0;$('debug-grid').hidden=!page.total;
    for(const item of page.items){
      const card=debugCards.get(item.key),monitor=debugMonitors.get(item.host.host.id),sid=item.session.id;
      const state=monitor?.failed?'disconnected':!monitor?.ready?'connecting':M.sessionState(true,monitor.activity.get(sid),performance.now());
      const label={refreshing:'正在刷新',waiting:'暂无新输出',disconnected:'已断开',connecting:'连接中'}[state];
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
  }
  function resumeDebugGrid(){
    if(paused||!$('debug-dialog').open)return;
    clearInterval(debugTimer);debugTimer=setInterval(scheduleDebugGrid,400);scheduleDebugGrid();
  }
  $('debug').onclick=()=>{debugPage=0;$('debug-dialog').showModal();resumeDebugGrid();};
  $('debug-close').onclick=()=>$('debug-dialog').close();$('debug-dialog').addEventListener('close',pauseDebugGrid);
  $('debug-density').onchange=()=>{debugCapacity=$('debug-density').value==='9'?9:16;debugPage=0;scheduleDebugGrid();};
  $('debug-prev').onclick=()=>{debugPage--;scheduleDebugGrid();};$('debug-next').onclick=()=>{debugPage++;scheduleDebugGrid();};
  new ResizeObserver(()=>{const board=$('debug-board');$('debug-grid').style.setProperty('--debug-side',Math.max(0,Math.min(board.clientWidth,board.clientHeight))+'px');scheduleDebugGrid();}).observe($('debug-board'));
  function disposeTerminal(){
    cancelRefresh();const strip=$('terminal-status');if(strip){strip.textContent='';strip.hidden=true;}
    agentModeGeneration++;agentModeRequest=null;$('agent-mode').disabled=true;
    delete $('screen').dataset.agentMode;delete $('screen').dataset.agentPreference;
    composerSubmit.cancel();
    waitingFeeds=[];waitingBytes=0;
    ready=false;pendingKeys=[];composing=false;replaying=0;lastSize='';control=false;
    setCtrl(false);
    if(fitFrame!==null)cancelAnimationFrame(fitFrame);fitFrame=null;
    if(writer)writer.dispose();writer=null;if(highlighter)highlighter.dispose();highlighter=null;if(webgl)webgl.dispose();webgl=null;
    if(term)term.dispose();term=null;fit=null;$('screen').replaceChildren();
  }
  function activate(sid){
    if(scrollFrame!==null)cancelAnimationFrame(scrollFrame);scrollFrame=null;scrollRows=0;gesture=M.createGesture();
    if(sid===active&&term)return;
    if(active)drafts.set(sessionKey(),$('draft').value);disposeTerminal();active=sid;selected().subscription='';
    const s=inventory.find(x=>x.id===sid);if(!s)return;$('title').textContent=s.title+'（'+M.agentType(s,s.title)+'）';
    $('draft').value=drafts.get(sessionKey(sid))||'';if(drafts.size>32)drafts.delete(drafts.keys().next().value);
    const font=Number(localStorage.getItem('sma.mobile.font'))||14;$('font').value=String(Math.max(11,Math.min(22,font)));
    term=new Terminal({allowProposedApi:true,disableStdin:true,convertEol:false,scrollback:20000,fontSize:Number($('font').value),fontFamily:'Consolas, monospace',cursorBlink:false,minimumContrastRatio:4.5,windowsPty:{backend:'conpty'},theme:{background:'#141414',foreground:'#ededed',cursor:'#ddd'}});
    // Full-screen tools may erase scrollback during repaint; preserve mobile reading history.
    term.parser.registerCsiHandler({final:'J'},params=>params[0]===3);
    const agentHost=selected(),agentGeneration=agentHost.generation;
    term.parser.registerOscHandler(7501,data=>{
      if(selected()!==agentHost||agentHost.generation!==agentGeneration||active!==sid)return false;
      const session=agentHost.inventory.find(item=>item.id===sid);if(!session||session.agent)return false;
      const app=/(?:^|[;:])app=([a-zA-Z0-9_-]{1,40})(?:[;:]|$)/.exec(data)?.[1];
      const clear=/(?:^|[;:])state=clear(?:[;:]|$)/.test(data);
      if(!clear&&!/^(codex|claude(?:[-_]code)?|grok|kimi|pi[-_]dev|cursor[-_]cli|gemini|opencode|copilot|aider)$/i.test(app||''))return false;
      const next=clear?'':app.toLowerCase().replace('_','-');if((agentHost.agents.get(sid)||'')===next)return false;
      if(next)agentHost.agents.set(sid,next);else agentHost.agents.delete(sid);
      session.learnedAgent=next;renderList();refreshAgentMode();return false;
    });
    fit=new FitAddon.FitAddon();term.loadAddon(fit);term.loadAddon(new Unicode11Addon.Unicode11Addon());term.unicode.activeVersion='11';term.open($('screen'));setRenderer();
    const current=term;
    highlighter=C.createHighlighter(current,{enabled:$('highlight').checked,profile:C.commonAgentProfile});
    refreshAgentMode(true);
    writer=M.createWriter({write:(bytes,reset,done,grid)=>{if(term!==current)return;
      if(grid){const size=C.terminalSize(grid.cols,grid.rows);if(current.cols!==size.cols||current.rows!==size.rows)current.resize(size.cols,size.rows);}
      if(reset)current.reset();displayGrid();metrics.writes++;current.write(bytes,()=>{done();if(term===current){updateTerminalStatus();if(!reset&&bytes.length)composerSubmit.output();}});
    },schedule:cb=>requestAnimationFrame(cb),cancel:id=>cancelAnimationFrame(id),onOverflow:()=>{notice('输出积压超过上限，已暂停并请求重新连接');M.closeForRecovery(selected().socket,'手机输出积压');}});
    term.onData(text=>{text=C.terminalInput(text,replaying>0);if(ctrl&&text.length===1){text=M.controlKey(text);setCtrl(false);}input(text);});
    term.textarea.addEventListener('compositionstart',()=>composing=true);
    term.textarea.addEventListener('compositionend',()=>{composing=false;setTimeout(flushKeys,0);});
    term.onScroll(historyStatus);setMode(false);scheduleFit();renderList();
    if(connected)subscribeSessions(selected(),active);
    // Deliberately do not focus: switching sessions must not pop the keyboard.
  }
  function input(text){if(!text||!ready||paused||!connected||selected().socket?.readyState!==1)return false;selected().socket.send(C.targetFrame(17,active,text));return true;}
  function key(text){if(composing){pendingKeys.push({sid:sessionKey(),text});return;}input(text);}
  function flushKeys(){const keys=pendingKeys;pendingKeys=[];for(const k of keys)if(k.sid===sessionKey())input(k.text);}
  const localViews=new WeakMap();
  function localView(){let view=localViews.get(term);if(!view){view={bottom:true};localViews.set(term,view);$('screen').scrollLeft=0;}return view;}
  function localBottom(){if(!term)return;localView().bottom=true;$('screen').scrollTop=$('screen').scrollHeight;}
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
    if(readable&&view.bottom)screen.scrollTop=screen.scrollHeight;
    if(!readable){screen.scrollLeft=0;screen.scrollTop=0;}
    historyStatus();
  }
  function requestSize(takeControl=false){
    // Viewing, switching and rotating are local actions. Only an existing owner
    // or an explicit Take Control click may alter the shared PTY dimensions.
    if(!takeControl&&!control)return;
    if(sizing==='shared'&&!takeControl)return;
    const desired=desiredSize(),lease=controls[active];if(!desired||!ready||!lease)return;
    const key=desired.cols+'x'+desired.rows;
    if((takeControl||key!==lastSize)&&send(5,{session:active,...desired,take_control:takeControl}))lastSize=key;
  }
  function scheduleFit(){if(fitFrame!==null||!term)return;fitFrame=requestAnimationFrame(()=>{fitFrame=null;if(!term||paused||writer?.idle===false)return;
    displayGrid();requestSize();});}
  function setRenderer(){
    if(webgl)webgl.dispose();webgl=null;let effective='dom';
    if(renderer!=='dom')try{const gl=new WebglAddon.WebglAddon();term.loadAddon(gl);webgl=gl;effective='webgl';gl.onContextLoss(()=>{if(webgl!==gl)return;webgl=null;gl.dispose();metrics.fallbacks++;$('render-status').textContent='GPU 上下文丢失，已回退 DOM';});}catch{metrics.fallbacks++;notice('GPU 渲染不可用，已回退 DOM');}
    $('render-status').textContent='实际渲染：'+effective.toUpperCase();$('renderer').value=C.rendererName(renderer);scheduleFit();
  }
  function setCtrl(value){ctrl=value;$('ctrl').setAttribute('aria-pressed',String(ctrl));}
  function updateComposer(pending=composerSubmit.pending){
    const uncertain=!!$('draft').value&&uncertainDrafts.get(sessionKey())===$('draft').value;
    $('send').disabled=pending||uncertain;$('send').textContent=pending?'验证输入中…':uncertain?'请先检查终端':'发送';$('send').setAttribute('aria-busy',String(pending));
  }
  function setMode(focus){
    const editor=$('composer');
    if(focus&&modes.get(sessionKey())==='buffered'){
      $('composer-target').textContent=[machine||selected().host.name,$('title').textContent].filter(Boolean).join(' · ');
      $('composer-status').textContent=uncertainDrafts.get(sessionKey())===$('draft').value&&$('draft').value?'未收到输入回显，尚未按回车。请关闭窗口检查终端，避免重复粘贴。':'收到终端回显后自动按一次回车。';
      if(!editor.open)editor.showModal();$('draft').focus();
    }else if(editor.open)editor.close();
    $('mode').setAttribute('aria-expanded',String(editor.open));updateComposer();
  }
  function closeComposer(){if(composerSubmit.pending)return;modes.set(sessionKey(),'direct');$('draft').blur();setMode(false);}
  function submit(){
    if(composing||composerSubmit.pending)return;
    if(!ready||!connected){$('composer-status').textContent='终端未连接，指令尚未发送；文本已保留。';return;}
    if(!$('draft').value.trim()){$('composer-status').textContent='请先输入指令。';return;}
    if(uncertainDrafts.get(sessionKey())===$('draft').value)return;
    const current=term,c=selected(),socket=c.socket,generation=c.generation,sid=sessionKey(),text=$('draft').value;
    $('composer-status').textContent='正在验证终端输入回显…';drafts.set(sid,text);
    if(!composerSubmit.submit(text,{paste:value=>current.paste(value),enter:()=>input('\r'),
      echo:M.createComposerEcho(current,text),
      onTimeout:()=>{uncertainDrafts.set(sid,text);$('composer-status').textContent='未收到输入回显，尚未按回车。请关闭窗口检查终端，避免重复粘贴。';updateComposer();},
      onSent:()=>{$('draft').value='';drafts.delete(sid);uncertainDrafts.delete(sid);closeComposer();notice('输入回显已确认，已发送回车。');},
      isCurrent:()=>!paused&&ready&&connected&&term===current&&selected()===c&&c.socket===socket&&c.generation===generation&&sessionKey()===sid}))return;
  }
  function historyStatus(){if(!term)return;const b=term.buffer.active,screen=$('screen');$('resume').hidden=(b.type!=='normal'||b.viewportY>=b.baseY)&&(screen.dataset.viewportMode!=='readable'||screen.scrollTop>=screen.scrollHeight-screen.clientHeight-2);}
  function visibleRows(){const cell=term?._core?._renderService?.dimensions.css.cell,scale=Number($('screen').dataset.scale)||1;return Math.max(1,Math.min(term?.rows||1,Math.floor($('screen').clientHeight/((cell?.height||17)*scale))));}
  function localScroll(pixels){
    if(!term||!pixels)return;const screen=$('screen'),buffer=term.buffer.active,line=term._core._renderService.dimensions.css.cell.height*(Number(screen.dataset.scale)||1);
    if(screen.dataset.viewportMode!=='readable'){if(buffer.type==='normal')term.scrollLines(Math.trunc(pixels/line));historyStatus();return;}
    const view=localView(),historyPixels=value=>{const total=(view.historyPixels||0)+value,before=buffer.viewportY;term.scrollLines(Math.trunc(total/line));const rest=total-(buffer.viewportY-before)*line,edge=total<0?buffer.viewportY===0:buffer.viewportY>=buffer.baseY;view.historyPixels=edge?0:rest;return edge?rest:0;};
    let remaining=pixels;
    // Returning towards the prompt consumes history before the current grid.
    if(remaining>0&&buffer.type==='normal'&&buffer.viewportY<buffer.baseY)remaining=historyPixels(remaining);
    const before=screen.scrollTop;screen.scrollTop+=remaining;remaining-=screen.scrollTop-before;
    if(remaining<0&&buffer.type==='normal')historyPixels(remaining);
    view.bottom=screen.scrollTop>=screen.scrollHeight-screen.clientHeight-2&&(buffer.type!=='normal'||buffer.viewportY>=buffer.baseY);historyStatus();
  }
  let gesture=M.createGesture(),scrollRows=0,scrollFrame=null,panTouch=null;
  function scroll(rows){if(!rows||!term)return;scrollRows+=rows;if(scrollFrame!==null)return;scrollFrame=requestAnimationFrame(()=>{scrollFrame=null;const n=scrollRows;scrollRows=0;if(!term)return;
    localScroll(n*term._core._renderService.dimensions.css.cell.height*(Number($('screen').dataset.scale)||1));});}
  $('screen').addEventListener('touchstart',e=>{if(e.touches.length!==1)return;const touch=e.touches[0];gesture.start(touch.clientX,touch.clientY);panTouch={x:touch.clientX,y:touch.clientY};e.preventDefault();},{passive:false});
  $('screen').addEventListener('touchmove',e=>{if(e.touches.length!==1)return;const touch=e.touches[0],cell=term?._core?._renderService?.dimensions?.css?.cell;const line=(cell?.height||17)*(Number($('screen').dataset.scale)||1);const move=gesture.move(touch.clientX,touch.clientY,line);if(move.drag&&panTouch){if($('screen').dataset.viewportMode==='readable'){$('screen').scrollLeft+=panTouch.x-touch.clientX;localScroll(panTouch.y-touch.clientY);}else scroll(move.rows);panTouch={x:touch.clientX,y:touch.clientY};}e.preventDefault();},{passive:false});
  $('screen').addEventListener('touchend',e=>{if(gesture.end()){if(modes.get(sessionKey())==='buffered')setMode(true);else term?.focus();}e.preventDefault();},{passive:false});
  $('screen').addEventListener('wheel',e=>{e.preventDefault();if($('screen').dataset.viewportMode==='readable'){$('screen').scrollLeft+=e.deltaX||(e.shiftKey?e.deltaY:0);if(!e.shiftKey)localScroll(e.deltaY);}else scroll(Math.sign(e.deltaY)*3);},{passive:false});
  $('sessions').onclick=()=>{refreshWatchers();renderHosts();renderList();$('session-dialog').showModal();};$('sessions-close').onclick=()=>$('session-dialog').close();
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
      if(h.id===hostId){disposeTerminal();active='';$('draft').value='';loadSelected();}
    }
    hostTokens.set(h.id,$('host-token').value);if(!editingHost)hosts.push(h);saveSessionCredentials();saveHosts();$('host-token').value='';editingHost=null;$('host-dialog').close();connect(connection(h));chooseHost(h.id);refreshWatchers();renderHosts();renderList();
  }catch(error){$('host-error').textContent=error.message;$('host-error').hidden=false;}};
  $('new').onclick=()=>{if(!connected)return;const c=selected();c.beforeCreate=new Set(inventory.map(s=>s.id));c.createPending=createPending=send(6,{shell:$('shell').value});$('session-dialog').close();};
  $('control').onclick=()=>requestSize(true);
  $('sizing').onchange=()=>{sizing=$('sizing').value;localStorage.setItem('sma.mobile.sizing',sizing);lastSize='';scheduleFit();};
  $('options').onclick=()=>$('settings-dialog').showModal();$('settings-close').onclick=()=>$('settings-dialog').close();
  for(const link of document.querySelectorAll('#settings-dialog a'))link.onclick=e=>{e.preventDefault();location.assign(link.getAttribute('href'));};
  $('font').oninput=()=>{localStorage.setItem('sma.mobile.font',$('font').value);if(term)term.options.fontSize=Number($('font').value);scheduleFit();};
  $('renderer').onchange=()=>{renderer=C.rendererName($('renderer').value);if(term)setRenderer();};
  $('ctrl').onpointerdown=e=>e.preventDefault();$('ctrl').onclick=()=>setCtrl(!ctrl);
  const keys={escape:'\x1b',tab:'\t',f2:'\x1bOQ',enter:'\r',up:'\x1b[A',down:'\x1b[B',left:'\x1b[D',right:'\x1b[C'};
  for(const b of document.querySelectorAll('[data-key]')){b.onpointerdown=e=>e.preventDefault();b.onclick=()=>key(C.cursorKey?.(term,b.dataset.key)||keys[b.dataset.key]);}
  $('mode').onclick=()=>{modes.set(sessionKey(),'buffered');setMode(true);};
  $('github').onclick=()=>{modes.set(sessionKey(),'buffered');setMode(true);if(!$('draft').value){$('draft').value='提交全部文件到 GitHub';drafts.set(sessionKey(),$('draft').value);updateComposer();}else $('composer-status').textContent='已保留现有草稿；清空后可再次选择“提交 GitHub”。';};
  $('composer-close').onclick=closeComposer;
  $('composer').addEventListener('cancel',e=>{e.preventDefault();closeComposer();});
  $('draft').addEventListener('input',()=>{drafts.set(sessionKey(),$('draft').value);updateComposer();});
  $('draft').addEventListener('compositionstart',()=>composing=true);$('draft').addEventListener('compositionend',()=>{composing=false;setTimeout(flushKeys,0);});
  let composerNewline=false;
  $('draft').onkeydown=e=>{const action=M.composerAction(e,composing);composerNewline=action==='newline';if(action==='send'){e.preventDefault();submit();}};
  $('draft').onbeforeinput=e=>{if(composerNewline){composerNewline=false;return;}if(M.composerAction(e,composing)==='send'){e.preventDefault();submit();}};
  $('send').onclick=submit;
  function viewHistory(action){
    if(!term)return;
    if(scrollFrame!==null)cancelAnimationFrame(scrollFrame);scrollFrame=null;scrollRows=0;
    action(term);historyStatus();
  }
  function historyPage(direction){if(!term)return;const line=term._core._renderService.dimensions.css.cell.height*(Number($('screen').dataset.scale)||1);localScroll(direction*Math.max(1,visibleRows()-1)*line);}
  const historyActions={'page-up':()=>historyPage(-1),'page-down':()=>historyPage(1),'history-top':t=>{if(t.buffer.active.type==='normal')t.scrollToTop();localView().bottom=false;$('screen').scrollTop=0;},'history-bottom':t=>{if(t.buffer.active.type==='normal')t.scrollToBottom();localBottom();}};
  for(const [id,action] of Object.entries(historyActions)){$(id).onpointerdown=e=>e.preventDefault();$(id).onclick=()=>viewHistory(action);}
  $('bottom').onclick=()=>viewHistory(historyActions['history-bottom']);
  $('refresh-screen').onpointerdown=e=>e.preventDefault();
  $('refresh-screen').onclick=refreshScreen;
  $('copy').onclick=()=>{if(!term)return;let text=term.getSelection();if(!text){const b=term.buffer.active;text=Array.from({length:term.rows},(_,i)=>b.getLine(b.viewportY+i)?.translateToString(true)||'').join('\n');}
    $('copy-text').value=text;$('settings-dialog').close();$('copy-dialog').showModal();$('copy-text').focus();$('copy-text').select();};$('copy-close').onclick=()=>$('copy-dialog').close();
  new ResizeObserver(()=>{scheduleFit();updateTerminalStatus();}).observe($('screen'));window.visualViewport?.addEventListener('resize',scheduleFit);
  function pause(){if(paused)return;paused=true;pauseDebugGrid();clearInterval(activityTimer);activityTimer=null;if(active)drafts.set(sessionKey(),$('draft').value);for(const c of connections.values())stopSocket(c);refreshWatchers();writer?.dispose();if(fitFrame!==null)cancelAnimationFrame(fitFrame);fitFrame=null;if(scrollFrame!==null)cancelAnimationFrame(scrollFrame);scrollFrame=null;state('后台已暂停连接；服务器终端继续运行');}
  function resume(){if(!paused)return;paused=false;startActivityTimer();for(const c of connections.values())connect(c);refreshWatchers();resumeDebugGrid();}
  document.addEventListener('visibilitychange',()=>document.hidden?pause():resume());
  window.addEventListener('pagehide',pause);
  function captureUIState(){
    if(composing||composerSubmit.pending)return null;
    if(active)drafts.set(sessionKey(),$('draft').value);saveSessionCredentials();
    return {version:1,hostId,activeSid:active,drafts:[...drafts],modes:[...modes],uncertainDrafts:[...uncertainDrafts]};
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
    const sid=restoringUI.activeSid;restoringUI=null;activate(sid);$('draft').value=drafts.get(sessionKey())||'';setMode(false);
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
    if(active)$('draft').value=drafts.get(sessionKey())||'';
    restoreSelected();
  }
  window.MobileTerminal={roomSelectionVersion:1,pause,resume,captureUIState,restoreUIState,prepareUIReload,cancelUIReload,openNotification,reconnect:()=>{if(paused)resume();else{connect();refreshWatchers();}},chooseHost,get hosts(){return [...connections.values()].map(c=>({id:c.host.id,connected:c.connected,status:c.status,sessions:c.inventory.map(s=>s.id),subscription:c.subscription}));},get terminal(){return term;},get active(){return active;},get activeKey(){return sessionKey();},get connected(){return connected;},get ready(){return ready;},get controls(){return controls[active]||null;},revision:config.uiRevision||'bundled',metrics};
  for(const h of [home,...hosts])connection(h);
  if(document.hidden){paused=true;state('Background UI paused');renderHosts();renderList();}
  else{startActivityTimer();for(const c of connections.values())connect(c);refreshWatchers();}
})();
