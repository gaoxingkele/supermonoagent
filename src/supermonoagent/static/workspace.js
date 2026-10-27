/* Concurrent host directories and byte-preserving, owner-bound xterm views. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const C = window.TerminalClient;
  const encoder = new TextEncoder(), decoder = new TextDecoder();
  const params = new URLSearchParams(location.search);
  const agentAssetBase=new URL('agent-modes/',document.currentScript.src),agentAssetQuery=new URL(document.currentScript.src).search;
  const launch = new URLSearchParams(location.hash.slice(1));
  const explicitToken=launch.has('token')||params.has('token');
  const read = (storage, key) => { try { return storage.getItem(key) || ''; } catch { return ''; } };
  const save = (storage, key, value) => { try { value ? storage.setItem(key, value) : storage.removeItem(key); } catch {} };
  let highlightEnabled=read(localStorage,'sma.workspace.highlight')!=='off';
  let config = {relay: params.get('direct')==='1'?'':params.get('relay') || read(localStorage, 'sma.workspace.relay'),
    code: params.get('direct')==='1'?'':params.get('code') || read(localStorage, 'sma.workspace.code'),
    token: launch.get('token') || params.get('token') || read(sessionStorage, 'sma.workspace.token') || read(localStorage, 'sma.workspace.token'),
    authMode:explicitToken||params.get('direct')==='1'?'manual':read(localStorage,'sma.workspace.auth-mode')||'manual'};
  let renderer = C.rendererName(params.get('renderer') || read(localStorage, 'sma.workspace.renderer'));
  let catalog = [], panes = new Map(), openTabs = [], active = '', layout = null;
  let userConnectedOnce = false, maximized = false, ctrl = false;
  let renameWindow = null, terminateSession = null, connectionOwner = null;
  let homeConfig={...config}, savedHosts=loadHosts(), selectedHost='local', editingHost='', homeRelays=[];
  let relayProfiles=loadRelayProfiles(),profileLegacy='connection',roomMenu=null;
  let runtimeDefaultToken='',prefilledTokenTarget='',defaultTokenEdited=false;
  let relayProbe=null;
  const daemonMonitor={hostID:'',timer:null,pending:null,sequence:0,denied:null};
  const hosts=new Map(), directories=new Map(), relayAccounts=new Map();
  let restoredUI=null;
  try{const raw=read(sessionStorage,'sma.workspace.ui-state');if(raw.length<=65536){const value=JSON.parse(raw||'null');if(value&&Array.isArray(value.tabs))restoredUI=value;}}catch{}
  const restoringKeys=new Set(restoredUI?.tabs.filter(value=>{try{const pair=JSON.parse(value);return pair.length===2&&pair.every(item=>typeof item==='string');}catch{return false;}})||[]);
  function restorePaneViews(host){
    if(!restoredUI)return;
    const ids=catalog.filter(win=>win.host===host).flatMap(win=>win.sessions.map(session=>session.id));
    for(const id of ids)if(restoringKeys.has(id)){openView(id,false);restoringKeys.delete(id);}
    if(restoredUI.active&&panes.has(restoredUI.active))openView(restoredUI.active,false);
    if(!restoringKeys.size){
      const tree=(value,depth=0)=>{if(depth>16)return null;if(typeof value==='string')return panes.has(value)?value:null;
        if(!value||!['horizontal','vertical'].includes(value.direction))return null;const a=tree(value.a,depth+1),b=tree(value.b,depth+1);
        return a&&b?{direction:value.direction,ratio:Math.max(15,Math.min(85,Number(value.ratio)||50)),a,b}:a||b;};
      layout=tree(restoredUI.layout)||layout;restoredUI=null;save(sessionStorage,'sma.workspace.ui-state','');renderLayout();subscribe();
    }
  }
  let accountLogin=null,accountLoginGeneration=0,workspaceAuthorization=null,connectionAuthorization=null;
  const mobile = () => matchMedia('(max-width:600px)').matches;
  const metrics = {socketsOpened:0, receivedBytes:0, transmittedBytes:0, outputFrames:0, snapshots:0, historyResumes:0, historyRecoveries:0,
    renderMilliseconds:0, parsedBytes:0, reconnects:0, rendererFallbacks:0, lastPongMs:null};
  const key=(host,id)=>JSON.stringify([host.id,id]);
  const currentHost=()=>hosts.get(selectedHost);
  const owner=host=>({host,generation:host.generation});
  const validOwner=context=>!!context&&hosts.get(context.host.id)===context.host&&context.generation===context.host.generation;
  const livePane=pane=>!!pane&&panes.get(pane.sid)===pane&&validOwner(pane);
  function scheduleFit(pane){if(!pane)return;const context=owner(pane.host);requestAnimationFrame(()=>{if(validOwner(context)&&livePane(pane))fitPane(pane);});}
  function scheduleVisibleFit(){for(const id of visibleIDs())scheduleFit(panes.get(id));}
  class HostConnection {
    constructor(id,name,settings){
      Object.assign(this,{id,name,config:{...settings},ws:null,generation:0,retry:0,retryTimer:0,pingTimer:0,
        connected:false,supported:false,helloSent:false,catalog:[],groups:[],controls:{},capabilities:{},identity:{},activity:new Map(),agents:new Map(),
        selectedGroup:'',collapsed:read(sessionStorage,'sma.workspace.collapsed.'+id)==='true',initialCatalog:true,nextSplit:'',sessionsBeforeOpen:new Set(),subscriptionSignature:'',
        phase:'offline',error:'',pingSentAt:null,daemonInfo:null,meter:new C.ConnectionMeter(performance.now()),rates:{tx:0,rx:0}});
    }
  }
  function aggregateCatalog(){
    catalog=[...hosts.values()].flatMap(host=>host.catalog.map(win=>({...win,host,raw:win.id,id:key(host,win.id),
      group:key(host,win.group||'default'),sessions:(win.sessions||[]).map(session=>({...session,raw:session.id,id:key(host,session.id)}))})));
  }
  const presets = [ ['powershell','PowerShell'], ['pwsh','PowerShell 7'], ['cmd','CMD'], ['claude','Claude'], ['codex','Codex'] ];
  const node = (tag, className, text) => { const el = document.createElement(tag); if(className)el.className=className;if(text!==undefined)el.textContent=text;return el; };
  const button = (text, title, action) => { const b=node('button','',text);b.type='button';b.title=title;b.setAttribute('aria-label',title);b.onclick=action;return b; };
  const sessionInfo = sid => { for(const win of catalog){const session=(win.sessions||[]).find(s=>s.id===sid);if(session)return {win,session};}return null; };
  const visibleIDs = () => { if(!layout)return [];if(mobile()||maximized)return active?[active]:[];const ids=[];const walk=n=>{if(typeof n==='string')ids.push(n);else if(n){walk(n.a);walk(n.b);}};walk(layout);return [...new Set(ids)]; };
  const notice = message => { $('notice').textContent=message;$('notice').hidden=!message; };
  function status(text) { $('status').textContent=text; }
  function socketSend(host,message){
    if(!host?.ws||host.ws.readyState!==WebSocket.OPEN)return false;
    host.ws.send(message);metrics.transmittedBytes+=host.meter.sent(message);return true;
  }
  function send(context,op,value){
    if(!validOwner(context))return false;
    const host=context.host;if(!host.connected||!host.supported)return false;
    return socketSend(host,C.pack(op,value instanceof Uint8Array?value:encoder.encode(JSON.stringify(value))));
  }
  function connectionName(host){
    if(host.config.code)return '中继';
    const target=new URL(host.config.relay||location.origin,location.origin);
    return ['localhost','127.0.0.1','[::1]','::1'].includes(target.hostname)?'本机':'远端直连';
  }
  function updateConnection(){
    const host=currentHost();if(!host)return;
    const quality=host.meter.quality(performance.now(),host.phase);
    $('connection-label').textContent=host.phase==='connected'?connectionName(host)+'已连接':{connecting:'正在连接',reconnecting:'离线 · 重连中','auth-failed':'验证失败',unsupported:'协议不兼容',invalid:'连接配置无效',offline:'离线'}[host.phase]||'离线';
    $('connection-dot').classList.toggle('online',host.connected);
    $('connection-tx').textContent='↑ '+C.formatRate(host.rates.tx);$('connection-rx').textContent='↓ '+C.formatRate(host.rates.rx);
    const bars=$('connection-quality');bars.dataset.bars=String(quality.bars);bars.setAttribute('aria-label','连接质量：'+quality.label);
    $('connect-button').title=host.name+' · '+quality.label+(host.meter.rtt===null?'':' · '+Math.round(host.meter.rtt)+' ms')+'；当前电脑应用消息字节数';
    metrics.lastPongMs=host.meter.rtt===null?null:Math.round(host.meter.rtt);
    notice(host.error);
  }
  function ping(host){host.pingSentAt=performance.now();send(owner(host),10,encoder.encode(String(host.pingSentAt)));}
  function tokenlessLocal(settings){
    if(settings.code)return false;
    try{return new URL(C.socketURL(settings.relay,location.origin)).hostname==='127.0.0.1';}catch{return false;}
  }
  function handshake(host){
    if(host.helloSent||host.ws?.readyState!==WebSocket.OPEN)return;
    host.helloSent=true;socketSend(host,C.pack(1,encoder.encode(JSON.stringify({...(tokenlessLocal(host.config)||host.config.authMode==='account'?{}:{token:host.config.token}),multiplex:true,reliable:true,history_context:1,history_resume:1,
      channel:host.id==='local'?params.get('channel')||undefined:undefined}))));
  }
  function stopHost(host){
    host.daemonInfo=null;
    invalidateDaemonMonitorHost(host);
    if(host.id==='local'){runtimeDefaultToken='';if(prefilledTokenTarget){$('relay-profile-token').value='';prefilledTokenTarget='';}}
    host.generation++;clearTimeout(host.retryTimer);clearInterval(host.pingTimer);
    if(host.ws){host.ws.onclose=null;host.ws.close();host.ws=null;}host.connected=false;host.supported=false;host.activity.clear();updateSessionDots();
    for(const pane of panes.values())if(pane.host===host){pane.ready=false;pane.term.options.disableStdin=true;clearTimeout(pane.redrawTimer);clearTimeout(pane.resizeRequest?.timer);pane.resizeRequest=null;cancelAnimationFrame(pane.followFrame);pane.followFrame=0;cancelAnimationFrame(pane.scrollIntentFrame);pane.scrollIntentFrame=0;}
  }
  let updateHint='',updateCheckPending=false;
  function receiveUpdateHint(message){
    const revision=C.updateHintRevision(message);
    if(!revision||revision===updateHint||updateCheckPending)return;
    updateHint=revision;
    if(!window.NativeUpdates?.check)return;
    updateCheckPending=true;Promise.resolve(window.NativeUpdates.check()).catch(()=>{}).finally(()=>{updateCheckPending=false;});
  }
  function connect(host){
    stopHost(host);const context=owner(host);
    host.helloSent=false;host.subscriptionSignature='';host.phase='connecting';host.error='';host.meter.reset(performance.now(),host.retry);host.rates={tx:0,rx:0};host.pingSentAt=null;
    for(const pane of panes.values())if(pane.host===host){
      pane.resumeControl=pane.resumeControl||host.controls[pane.raw]?.owner===true;
      pane.generation=host.generation;pane.bindHeader();pane.bindInput();pane.bindObserver();pane.writing=false;pane.lastSize='';pane.lastAttempt='';pane.limitedSize=null;pane.resizePaused=false;pane.frames=[];pane.replayPending=0;
    }
    let url;try{url=C.socketURL(host.config.relay,location.origin);const safe=new URL(url);safe.searchParams.delete('token');url=safe.toString();}
    catch(error){host.error=error.message;host.phase='invalid';updateConnection();renderHosts();return;}
    const local=tokenlessLocal(host.config),account=!local&&host.config.authMode==='account'?accountFor(url):null;
    if(!local&&host.config.authMode==='account'&&(!host.config.code||!account)){host.phase='auth-failed';host.error='请登录此中继账号并重新选择已授权电脑。';updateConnection();renderHosts();return;}
    host.ws=new WebSocket(url);host.ws.binaryType='arraybuffer';metrics.socketsOpened++;renderHosts();updateConnection();
    host.ws.onopen=()=>{if(!validOwner(context))return;if(host.config.code){
      if(host.config.authMode==='account'){if(!accountFor(url)||accountFor(url)!==account){stopAccountHost(host,'账号会话已过期，请重新登录。');return;}socketSend(host,JSON.stringify({op:'phone',code:host.config.code,session:account.session,reliable:true}));}
      else socketSend(host,JSON.stringify({op:'phone',code:host.config.code,token:host.config.token,reliable:true}));
    }else handshake(host);};
    host.ws.onmessage=event=>{
      if(!validOwner(context))return;
      try{
        metrics.receivedBytes+=host.meter.received(event.data);
        if(typeof event.data==='string'){
          const message=JSON.parse(event.data);if(message.op==='ready')handshake(host);else if(message.op==='ui_update')receiveUpdateHint(message);else if(message.error){host.error=String(message.error);updateConnection();}return;
        }
        const {op,payload}=C.unpack(new Uint8Array(event.data));
        if(op===2||op===7)sync(host,JSON.parse(decoder.decode(payload)),op===2);
        else if(op===20)receiveDaemonLogs(host,JSON.parse(decoder.decode(payload)));
        else if(op===3||op===8||op===23){
          if((op===8||op===23)&&payload.length===0)return;
          if(op===23&&host.capabilities.history_context!==1)throw new Error('Unnegotiated history context');
          const data=op===23?C.historyPayload(payload):C.sessionPayload(payload),pane=panes.get(key(host,data.session));
          const replay=op===8||op===23&&['snapshot','resume'].includes(data.context.kind);
          if(!replay&&data.bytes.length)host.activity.set(data.session,performance.now());
          if(livePane(pane)){
            const size=data.context||host.controls[data.session];
            pane.frames.push({op,bytes:data.bytes,context:data.context||null,grid:size?{cols:size.cols,rows:size.rows}:null});
            if(replay){pane.replayPending++;if(data.context?.kind==='resume')metrics.historyResumes++;else metrics.snapshots++;}metrics.outputFrames++;drainPane(pane);
          }
        }else if(op===11){
          const reply=decoder.decode(payload),barrier=/^resize:(\d+):(.+)$/.exec(reply);
          if(barrier){const pane=panes.get(key(host,barrier[2]));if(livePane(pane)&&pane.resizeRequest?.id===Number(barrier[1])){pane.resizeRequest.barrier=true;acceptResize(pane,{},false);drainPane(pane);scheduleFit(pane);}}
          else if(Number(reply)===host.pingSentAt){host.meter.pong(performance.now()-host.pingSentAt,performance.now());updateConnection();updateFooter();}
        }
      }catch(error){host.error='终端协议错误：'+error.message;updateConnection();}
    };
    host.ws.onerror=()=>{if(validOwner(context)){host.error='连接失败，正在等待重试';updateConnection();}};
    host.ws.onclose=event=>{
      if(!validOwner(context))return;
      host.daemonInfo=null;invalidateDaemonMonitorHost(host);
      host.connected=false;host.supported=false;clearInterval(host.pingTimer);
      for(const pane of panes.values())if(pane.host===host){pane.ready=false;pane.term.options.disableStdin=true;}
      host.phase=event.code===4001?'auth-failed':'reconnecting';host.meter.reset(performance.now());host.rates={tx:0,rx:0};
      host.error=event.code===4001?(host.config.authMode==='account'?'账号未获此电脑授权，或主机尚未绑定此账号；请联系管理员。':'访问验证失败，请编辑此电脑的房间码和 Token。'):event.code===4004?'对应电脑尚未上线，正在等待。':event.reason||'连接已断开；此电脑输入已暂停';
      updateConnection();renderHosts();updateFooter();updateSessionDots();
      if(host.config.authMode==='account'&&!accountFor(url)){stopAccountHost(host,'账号会话已过期，请重新登录。');return;}
      if(event.code!==4001){metrics.reconnects++;host.retryTimer=setTimeout(()=>{if(validOwner(context))connect(host);},C.retryDelay(host.retry++));}
    };
  }
  function sync(host,info,greeting){
    if(greeting&&!host.connected){
      if(!info.capabilities?.multiplex){host.error='此电脑后台尚不支持多终端工作区，请更新后台。';host.phase='unsupported';host.ws.onclose=null;host.ws.close();updateConnection();renderHosts();return;}
      host.supported=true;host.connected=true;host.retry=0;host.initialCatalog=true;host.phase='connected';host.error='';userConnectedOnce=true;
      clearInterval(host.pingTimer);ping(host);host.pingTimer=setInterval(()=>ping(host),15000);
    }
    if(!host.supported)return;
    if(host.id==='local'){
      if(Array.isArray(info.service?.relays))homeRelays=info.service.relays;
      if(homeRelayAccess()&&info.service?.default_relay_token_source==='token.txt'){
        runtimeDefaultToken=typeof info.service.default_relay_token==='string'?info.service.default_relay_token:'';
        prefillRuntimeToken();
      }
    }
    if(info.error)host.error=String(info.error);
    if(info.capabilities)host.capabilities=info.capabilities;
    if(info.service&&typeof info.service==='object')updateDaemonInfo(host,info.service);
    for(const field of ['machine','project'])if(typeof info[field]==='string')host.identity[field]=info[field].slice(0,40);
    if(info.controls){host.controls=info.controls;for(const pane of panes.values())if(pane.host===host){acceptResize(pane,info,greeting);if(pane.resumeControl&&host.controls[pane.raw]){pane.resumeControl=false;pane.redrawAfterReplay=true;pane.wantControl=host.controls[pane.raw].owner===true||host.controls[pane.raw].busy===false;}}}
    if(info.windows){host.catalog=info.windows;const sessions=new Set(host.catalog.flatMap(win=>(win.sessions||[]).map(session=>{session.learnedAgent=host.agents.get(session.id)||'';return session.id;})));for(const cache of [host.activity,host.agents])for(const id of cache.keys())if(!sessions.has(id))cache.delete(id);}if(info.groups)host.groups=info.groups;
    if(host.autoDirectory&&isLocalRelayCopy(host)){
      const directory=directories.get(host.autoDirectory);if(directory){directory.selfRooms??=new Set();directory.selfRooms.add(host.config.code);}
      detachHost(host);renderTree();renderTabs();renderLayout();return;
    }
    aggregateCatalog();
    restorePaneViews(host);
    const valid=new Set(host.catalog.flatMap(win=>(win.sessions||[]).map(session=>key(host,session.id))));
    let layoutChanged=false;
    for(const [id,pane] of panes)if(pane.host===host&&!valid.has(id)){removeView(id,false);layoutChanged=true;}
    if(host.initialCatalog){host.initialCatalog=false;const existing=host.catalog.flatMap(win=>win.sessions||[]);
      if(existing.length&&!openTabs.some(id=>panes.get(id)?.host===host)){
        // Restore all initial tabs for the selected computer; other catalogs remain directory-only.
        if(host.id===selectedHost){for(const session of existing)openView(key(host,session.id),false);openView(key(host,(existing.find(session=>session.id===info.session)||existing[0]).id),false);}
      }
    }
    if(host.nextSplit==='new'){const created=host.catalog.flatMap(win=>win.sessions||[]).find(session=>!host.sessionsBeforeOpen.has(session.id));if(created){host.nextSplit='';openView(key(host,created.id));}}
    for(const pane of panes.values())if(pane.host===host){pane.ready=true;pane.term.options.disableStdin=false;const data=sessionInfo(pane.sid);if(data){pane.title.textContent=data.win.title+'（'+agentType(data.session,data.win.title)+'）';TabThemes.apply(pane.title.parentElement,data.win.tab_theme);updateAgentMode(pane);}drainPane(pane);}
    renderTree();renderTabs();renderHosts();renderDaemonMonitor();if(layoutChanged)renderLayout();subscribe();updateConnection();updateFooter();if(greeting||host.id==='local')refreshDirectories();
    scheduleVisibleFit();
  }
  function recoverPane(pane){
    if(!livePane(pane))return;
    pane.historyCursor=null;pane.historyInitialized=false;
    if(pane.recoveryGeneration===pane.host.generation)return;
    pane.recoveryGeneration=pane.host.generation;metrics.historyRecoveries++;
    const sessions=openTabs.map(id=>panes.get(id)).filter(item=>item?.host===pane.host&&sessionInfo(item.sid)).map(item=>item.raw);
    send(pane,16,{sessions,refresh:true});
  }
  function drainPane(pane){
    if(!livePane(pane)||pane.writing||pane.resizeRequest||!pane.host.controls[pane.raw]||!pane.frames.length)return;
    // Catalog updates can arrive while an earlier write is parsing. Keep each
    // frame on the grid confirmed when it arrived, including queued erase/cursor sequences.
    const frame=pane.frames.shift(),replay=frame.op===8||['snapshot','resume'].includes(frame.context?.kind),size=frame.grid||pane.host.controls[pane.raw];
    const tracked=frame.context?C.historyFrameBytes(frame.context,frame.bytes,pane.historyCursor):{bytes:frame.bytes,position:null,reset:replay};
    if(!tracked){
      if(replay)pane.replayPending=Math.max(0,pane.replayPending-1);
      recoverPane(pane);drainPane(pane);return;
    }
    frame.bytes=tracked.bytes;
    applyGrid(pane,size);
    pane.writing=true;
    pane.questions?.beforeOutput({replay});
    if(tracked.reset){
      resizeGrid(pane,size);pane.term.reset();pane.history.hidden=false;pane.historyCursor=null;pane.historyInitialized=false;
      if(frame.op===8)frame.bytes=C.snapshotBytes(frame.bytes,pane.raw,pane.host.capabilities.snapshot_sgr===true);
    }
    const before=performance.now();
    const writeGeneration=pane.generation;
    pane.term.write(frame.bytes,()=>{
      if(!livePane(pane)||pane.generation!==writeGeneration)return;
      pane.writing=false;if(replay){pane.replayPending=Math.max(0,pane.replayPending-1);pane.historyInitialized=true;}
      if(frame.context?.v===2&&tracked.position!==null)pane.historyCursor={position:tracked.position,epoch:frame.context.epoch,stream_id:frame.context.stream_id};
      metrics.renderMilliseconds+=performance.now()-before;metrics.parsedBytes+=frame.bytes.length;
      if(pane.followFrame)keepFollowing(pane);
      drainPane(pane);
      if(!pane.frames.length){fitPane(pane);maybeRedrawPane(pane);}
    });
  }
  function createPane(sid) {
    const data=sessionInfo(sid);if(!data)return null;
    const el=node('article','pane');el.dataset.session=sid;
    const header=node('div','pane-header'),title=node('span','pane-title',data.win.title+'（'+agentType(data.session,data.win.title)+'）');
    const own=node('span','pane-control','');header.append(title,own);
    TabThemes.apply(header,data.win.tab_theme);
    const screen=node('div','terminal-screen');const history=node('div','history-note','已恢复有限输出历史 · 全屏程序画面可能需要刷新');history.hidden=true;
    history.title=history.textContent;el.append(header,screen);$('stage').append(el);
    const term=new Terminal({allowProposedApi:true,convertEol:false,disableStdin:!data.win.host.connected,cursorBlink:true,
      fontFamily:'"Cascadia Mono", "Cascadia Code", Consolas, monospace',fontSize:mobile()?13:14,scrollback:5000,minimumContrastRatio:4.5,
      theme:{background:'#121212',foreground:'#dedede',cursor:'#ddd',selectionBackground:'#55555588'},rendererType:'dom'});
    const fit=new FitAddon.FitAddon();term.loadAddon(fit);
    if(window.Unicode11Addon?.Unicode11Addon){try{term.loadAddon(new Unicode11Addon.Unicode11Addon());term.unicode.activeVersion='11';}catch{}}
    term.open(screen);
    // Native clipboard accelerators must not also send SYN/interrupt bytes.
    // Without a selection, Ctrl+C remains the CLI's ordinary interrupt key.
    term.attachCustomKeyEventHandler(event=>{
      if(event.type!=='keydown'||!/Win/i.test(navigator.platform)||!event.ctrlKey||event.altKey||event.metaKey||event.shiftKey)return true;
      const key=event.key.toLowerCase();
      return key!=='v'&&!(key==='c'&&term.hasSelection());
    });
    // Preserve readable history when a full-screen tool clears saved lines.
    term.parser.registerCsiHandler({final:'J'},params=>params[0]===3);
    const pane={sid,host:data.win.host,raw:data.session.raw,generation:data.win.host.generation,el,screen,term,fit,history,title,own,ready:data.win.host.connected,replayPending:0,lastSize:'',renderer:'dom',webgl:null,observer:null,frames:[],writing:false,wantControl:false,redrawAfterReplay:false,redrawTimer:0,resizeRequest:null,lastAttempt:'',lastInputAt:0,repaintAt:0,composing:false,historyCursor:null,historyInitialized:false,recoveryGeneration:-1};
    term.parser.registerOscHandler(7501,data=>{
      if(!livePane(pane))return false;
      const session=pane.host.catalog.flatMap(win=>win.sessions||[]).find(item=>item.id===pane.raw);if(!session||session.agent)return false;
      const app=/(?:^|[;:])app=([a-zA-Z0-9_-]{1,40})(?:[;:]|$)/.exec(data)?.[1];
      const clear=/(?:^|[;:])state=clear(?:[;:]|$)/.test(data);
      if(!clear&&!/^(codex|claude(?:[-_]code)?|kimi|gemini|opencode|copilot|aider)$/i.test(app||''))return false;
      const next=clear?'':app.toLowerCase().replace('_','-');if((pane.host.agents.get(pane.raw)||'')===next)return false;
      if(next)pane.host.agents.set(pane.raw,next);else pane.host.agents.delete(pane.raw);
      session.learnedAgent=next;aggregateCatalog();const info=sessionInfo(pane.sid);if(info)pane.title.textContent=info.win.title+'（'+agentType(info.session,info.win.title)+'）';renderTree();return false;
    });
    pane.highlighter=C.createHighlighter(term,{enabled:highlightEnabled});
    pane.questions=window.TerminalQuestions?.create(pane,{enabled:highlightEnabled,isCurrent:livePane});
    panes.set(sid,pane);setRenderer(pane);updateAgentMode(pane);
    pane.followOutput=true;pane.followFrame=0;pane.followGeometry=viewportGeometry(pane);pane.userScrollUntil=0;
    const scrollIntent=event=>{
      const buffer=term.buffer.active;if(event.type==='wheel'&&event.deltaY>=0&&buffer.viewportY===buffer.baseY)return;
      pane.userScrollGeometry=viewportGeometry(pane);pane.userScrollUntil=performance.now()+500;cancelAnimationFrame(pane.followFrame);pane.followFrame=0;
      cancelAnimationFrame(pane.scrollIntentFrame);const context=owner(pane.host);
      pane.scrollIntentFrame=requestAnimationFrame(()=>{
        pane.scrollIntentFrame=0;if(!validOwner(context)||!livePane(pane))return;
        const buffer=term.buffer.active;pane.followOutput=buffer.viewportY===buffer.baseY;
        // A tap or downward wheel at the bottom is not a history view.
        if(pane.followOutput)keepFollowing(pane);
      });
    };
    screen.addEventListener('wheel',scrollIntent,{capture:true,passive:true});
    screen.addEventListener('touchmove',scrollIntent,{capture:true,passive:true});
    screen.addEventListener('pointerdown',event=>{if(event.target.classList?.contains('xterm-viewport'))scrollIntent(event);},true);
    screen.addEventListener('keydown',event=>{if(['PageUp','PageDown','Home','End'].includes(event.key)&&event.shiftKey)scrollIntent(event);},true);
    term.onScroll(()=>{
      if(!livePane(pane)||pane.followingProgrammatically)return;
      const explicit=performance.now()<pane.userScrollUntil&&pane.userScrollGeometry===viewportGeometry(pane);
      // Zoom can generate a native DOM scroll before fit/resize sees the new geometry.
      // A stable, idle programmatic history scroll is also an intentional view change.
      if(explicit||(!pane.writing&&!pane.resizeRequest&&!pane.followFrame&&pane.followGeometry===viewportGeometry(pane)))
        pane.followOutput=term.buffer.active.viewportY===term.buffer.active.baseY;
    });
    const scrollBar=node('div','pane-scroll');
    const scrollAction=fn=>{
      return()=>{
        // The pane survives reconnects and receives the current generation.
        if(!livePane(pane))return;
        pane.userScrollGeometry=viewportGeometry(pane);pane.userScrollUntil=performance.now()+500;
        cancelAnimationFrame(pane.followFrame);pane.followFrame=0;
        fn();
      };
    };
    scrollBar.append(
      history,
      button('▲ 上翻屏','向上翻动一页历史',scrollAction(()=>pageHistory(pane,'up'))),
      button('▼ 下翻屏','向下翻动一页历史',scrollAction(()=>pageHistory(pane,'down'))),
      button('⤒ 到开头','翻到最早的历史输出',scrollAction(()=>pageHistory(pane,'start'))),
      button('⤓ 到末尾','翻到最新输出并恢复自动跟随',scrollAction(()=>pageHistory(pane,'end')))
    );
    el.append(scrollBar);
    pane.bindHeader=()=>{
      const context=owner(pane.host),guard=action=>(...args)=>{if(validOwner(context)&&livePane(pane))action(...args);};
      header.replaceChildren(title,own);
      header.oncontextmenu=guard(event=>tabMenu(event,data.win,context));
      header.append(button('接管','接管此终端尺寸',guard(()=>takeControl(sid))),button('刷新','强制后台终端全量重绘',guard(()=>redrawPane(pane))),button('改名','重命名窗口',guard(()=>rename(data.win.id,context))),button('结束','结束后台会话',guard(()=>terminate(sid))),button('×','关闭视图（保留后台进程）',guard(()=>removeView(sid))));
    };
    pane.bindHeader();
    pane.bindInput=()=>{
      pane.inputSubscription?.dispose();const context=owner(pane.host);
      pane.questions?.reset();
      pane.inputSubscription=term.onData(text=>{if(!validOwner(context)||!livePane(pane))return;text=C.terminalInput(text,pane.replayPending>0);if(!text)return;if(ctrl&&text.length===1){text=String.fromCharCode(text.toUpperCase().charCodeAt(0)&31);setCtrl(false);}input(sid,text);});
    };
    pane.bindInput();
    term.textarea.addEventListener('compositionstart',()=>{pane.composing=true;});term.textarea.addEventListener('compositionend',()=>{pane.composing=false;pane.lastInputAt=performance.now();});
    el.addEventListener('pointerdown',()=>{if(livePane(pane))activate(sid,false);});
    screen.addEventListener('pointerdown',event=>{if(event.target===screen&&livePane(pane)){event.preventDefault();activate(sid,false);term.focus();}});
    pane.bindObserver=()=>{
      pane.observer?.disconnect();const context=owner(pane.host);
      pane.observer=new ResizeObserver(()=>{if(validOwner(context)&&livePane(pane)){keepFollowing(pane);scheduleFit(pane);}});pane.observer.observe(screen);
    };
    pane.bindObserver();
    return pane;
  }
  function setRenderer(pane) {
    if(pane.webgl){pane.webgl.dispose();pane.webgl=null;}
    pane.renderer='dom';
    if(renderer!=='dom'&&window.WebglAddon?.WebglAddon){
      try{
        const gl=new WebglAddon.WebglAddon();pane.term.loadAddon(gl);pane.webgl=gl;pane.renderer='webgl';
        gl.onContextLoss(()=>{if(pane.webgl!==gl)return;pane.webgl=null;gl.dispose();pane.renderer='dom';metrics.rendererFallbacks++;notice('WebGL 上下文已丢失，已切换为 DOM 渲染。');updateFooter();});
      }catch(error){pane.renderer='dom';metrics.rendererFallbacks++;if(renderer==='webgl')notice('WebGL 不可用，已使用 DOM 渲染：'+error.message);}
    }else if(renderer==='webgl'){metrics.rendererFallbacks++;notice('此浏览器不支持 WebGL 附加组件，已使用 DOM 渲染。');}
  }
  const agentModeKey=pane=>'sma.workspace.agent-mode.'+JSON.stringify([endpoint(pane.host.config),pane.raw]);
  function updateAgentMode(pane){
    const info=sessionInfo(pane.sid);if(!info)return;
    const saved=read(localStorage,agentModeKey(pane)),mode=C.agentProfiles.some(profile=>profile.id===saved)?saved:'auto';
    const detected=mode==='auto'?C.agentModeFromSession(info.session,info.win.title):mode;
    pane.agentMode=mode;if(pane.agentRequested===detected)return;
    pane.agentRequested=detected;const generation=pane.agentGeneration=(pane.agentGeneration||0)+1;
    const apply=profile=>{
      if(!livePane(pane)||pane.agentGeneration!==generation)return;
      pane.term.options.theme={background:'#121212',foreground:'#dedede',cursor:'#ddd',selectionBackground:'#55555588',...profile.theme};
      pane.highlighter.setProfile(profile);pane.agentProfile=profile.id;
      if(active===pane.sid)updateFooter();
    };
    if(detected==='auto'){apply(C.commonAgentProfile);return;}
    const url=new URL(detected+'.json',agentAssetBase);url.search=agentAssetQuery;
    C.loadAgentProfile(url.href,detected).then(apply).catch(()=>{
      if(!livePane(pane)||pane.agentGeneration!==generation)return;apply(C.commonAgentProfile);
      if(active===pane.sid)notice('该模式暂时无法加载，已使用默认配色。');
    });
  }
  function viewportGeometry(pane){
    const rect=pane.screen.getBoundingClientRect();return [rect.width,rect.height,devicePixelRatio].join(':');
  }
  function pageHistory(pane,direction){
    const buffer=pane.term.buffer.active;
    if(buffer.type==='alternate'||buffer.baseY===0){
      const session=sessionInfo(pane.sid)?.session||{},declared=session.agent||session.learnedAgent;
      const executable=value=>String(Array.isArray(value)?value[0]||'':value||'').trim().replace(/^"|"$/g,'').split(/[\\/]/).pop();
      const codex=declared?/^codex$/i.test(declared):[session.shell,session.command,session.argv].some(value=>/^codex(?:\.(?:exe|cmd|js))?$/i.test(executable(value)));
      const canPage=codex&&pane.ready&&pane.host.connected&&!pane.replayPending;
      const message=codex?(canPage?'正在翻阅 Codex 历史。':'请等待连接和历史恢复后再翻屏。'):buffer.type==='alternate'?'此全屏程序没有本地滚动历史，请使用程序自身的历史查看功能。':'当前会话尚无可翻阅的本地历史。';
      pane.history.textContent=message;pane.history.title=message;pane.history.hidden=false;
      if(canPage){
        if(direction==='up'||direction==='start')pane.followOutput=false;
        else if(direction==='end')pane.followOutput=true;
        const keys={up:'\x1b[5~',down:'\x1b[6~',start:'\x1b[1;5H',end:'\x1b[1;5F'};
        input(pane.sid,keys[direction]);
      }
      return;
    }
    if(direction==='up'){pane.followOutput=false;pane.term.scrollPages(-1);}
    else if(direction==='start'){pane.followOutput=false;pane.term.scrollToTop();}
    else if(direction==='end'){pane.followOutput=true;keepFollowing(pane);}
    else{pane.term.scrollPages(1);pane.followOutput=buffer.viewportY===buffer.baseY;if(pane.followOutput)keepFollowing(pane);}
  }
  function keepFollowing(pane){
    if(!livePane(pane)||!pane.followOutput)return;
    if(pane.followFrame)return;
    const context=owner(pane.host);let settled=0;
    const follow=()=>{
      pane.followFrame=0;if(!validOwner(context)||!livePane(pane)||!pane.followOutput)return;
      pane.followingProgrammatically=true;pane.term.scrollToBottom();pane.followingProgrammatically=false;
      // Native viewport/scrollbar synchronization may finish after resize and write callbacks.
      settled=pane.writing||pane.resizeRequest||pane.frames.length||pane.replayPending?0:settled+1;
      pane.followGeometry=viewportGeometry(pane);
      if(settled<2)pane.followFrame=requestAnimationFrame(follow);
    };
    pane.followFrame=requestAnimationFrame(follow);
  }
  function resizeGrid(pane,size){
    const term=pane.term;
    if(term.cols!==size.cols||term.rows!==size.rows){
      pane.followingProgrammatically=true;term.resize(size.cols,size.rows);pane.followingProgrammatically=false;
      keepFollowing(pane);
    }else if(pane.followGeometry!==viewportGeometry(pane))keepFollowing(pane);
  }
  function applyGrid(pane,grid=null){
    const size=grid||pane.host.controls[pane.raw];if(!size||pane.writing||(!grid&&pane.frames.length))return;
    resizeGrid(pane,size);
    pane.lastSize=size.cols+'x'+size.rows;
    if(!visibleIDs().includes(pane.sid))return;
    const cell=pane.term._core?._renderService?.dimensions?.css?.cell;if(!cell?.width||!cell?.height)return;
    const style=getComputedStyle(pane.term.element),bar=pane.term._core.viewport?.scrollBarWidth||0;
    const width=size.cols*cell.width+parseFloat(style.paddingLeft)+parseFloat(style.paddingRight)+bar;
    const height=size.rows*cell.height+parseFloat(style.paddingTop)+parseFloat(style.paddingBottom);
    const scale=Math.min(1,pane.screen.clientWidth/width,pane.screen.clientHeight/height);
    Object.assign(pane.term.element.style,{width:width+'px',height:height+'px',transformOrigin:'top left',transform:scale<.999?'scale('+scale+')':''});
  }
  let resizeSequence=0;
  function requestResize(pane,size,takeover=false,stage='fit',attempt=0){
    if(!livePane(pane)||pane.resizeRequest||!pane.ready)return false;
    keepFollowing(pane);
    const request={...size,id:++resizeSequence,stage,attempt};pane.resizeRequest=request;
    if(!send(pane,5,{session:pane.raw,cols:size.cols,rows:size.rows,request:request.id,...(takeover?{take_control:true}:{})})){pane.resizeRequest=null;return false;}
    if(!pane.host.capabilities.resize_ack)send(pane,10,encoder.encode('resize:'+request.id+':'+pane.raw));
    request.timer=setTimeout(()=>{
      if(!livePane(pane)||pane.resizeRequest!==request)return;
      pane.resizeRequest=null;pane.lastAttempt='';
      const restore=pane.redrawRestore;pane.redrawRestore=null;
      if(pane.host.controls[pane.raw]?.owner&&((request.stage==='redraw-out'&&restore)||(request.stage==='redraw-back'&&request.attempt<1))){
        requestResize(pane,restore||{cols:request.cols,rows:request.rows},false,'redraw-back',1);
      }else{
        pane.resizePaused=true;pane.timedOutRequest=request.id;pane.redrawAfterReplay=false;applyGrid(pane);drainPane(pane);
        notice('尺寸确认超时；已暂停此终端的自动调整，可点击刷新重试。');
      }
    },3000);
    return true;
  }
  function acceptResize(pane,info,greeting){
    const request=pane.resizeRequest,ack=info.resize_ack;if(!request){if(ack?.session===pane.raw&&ack?.request===pane.timedOutRequest){pane.resizePaused=false;pane.timedOutRequest=null;}return;}
    if(pane.host.capabilities.resize_ack?ack?.session!==pane.raw||ack?.request!==request.id:!request.barrier)return;
    clearTimeout(request.timer);pane.resizeRequest=null;pane.lastAttempt='';
    const size=pane.host.controls[pane.raw];if(!size)return;
    pane.lastSize=size.cols+'x'+size.rows;
    if(request.stage==='fit')pane.limitedSize=size.owner&&(size.cols!==request.cols||size.rows!==request.rows)?{wanted:{cols:request.cols,rows:request.rows},actual:{cols:size.cols,rows:size.rows}}:null;
    if(request.stage==='redraw-out'){
      const restore=pane.redrawRestore;pane.redrawRestore=null;
      if(restore&&size.owner)requestResize(pane,restore,false,'redraw-back');
    }else if(request.stage==='redraw-back')pane.history.textContent='终端画面已同步';
  }
  function fitPane(pane, takeover=false) {
    if(!pane||!livePane(pane)||pane.writing||pane.replayPending||!pane.host.controls[pane.raw]||!visibleIDs().includes(pane.sid)||pane.screen.clientWidth<20||pane.screen.clientHeight<20)return;
    if(pane.frames.length){drainPane(pane);return;}
    applyGrid(pane);if(pane.resizeRequest||pane.resizePaused)return;
    let proposed;try{proposed=pane.fit.proposeDimensions();}catch{return;}if(!proposed)return;
    const desired=C.terminalSize(proposed.cols,proposed.rows),lease=pane.host.controls[pane.raw];
    takeover=takeover||pane.wantControl;
    const unchanged=desired.cols===lease.cols&&desired.rows===lease.rows;
    const limited=pane.limitedSize;
    if(limited&&limited.wanted.cols===desired.cols&&limited.wanted.rows===desired.rows&&limited.actual.cols===lease.cols&&limited.actual.rows===lease.rows&&(lease.owner||!takeover)){pane.wantControl=false;maybeRedrawPane(pane);return;}
    const key=desired.cols+'x'+desired.rows+'@'+lease.cols+'x'+lease.rows+':'+lease.owner+':'+takeover;
    if((lease.owner||lease.busy===false||takeover)&&(!unchanged||(takeover&&!lease.owner))&&pane.lastAttempt!==key){
      if(requestResize(pane,desired,takeover)){pane.lastAttempt=key;pane.wantControl=false;}
    }else if(unchanged)pane.wantControl=false;
    updateFooter();maybeRedrawPane(pane);
  }
  function redrawPane(pane) {
    if(!livePane(pane)||!pane.ready||!visibleIDs().includes(pane.sid))return;
    pane.redrawAfterReplay=true;pane.wantControl=true;pane.lastAttempt='';pane.resizePaused=false;fitPane(pane,true);
  }
  function maybeRedrawPane(pane) {
    if(!livePane(pane)||!pane.ready||!pane.redrawAfterReplay||pane.resizeRequest||pane.writing||pane.replayPending||pane.frames.length||pane.sid!==active||!pane.host.controls[pane.raw]?.owner)return;
    const now=performance.now(),delay=Math.max(0,700-(now-(pane.lastInputAt||-10000)),2000-(now-(pane.repaintAt||-10000)));
    if(pane.composing||delay>0){clearTimeout(pane.redrawTimer);pane.redrawTimer=setTimeout((context=>()=>{if(!validOwner(context)||!livePane(pane))return;pane.redrawTimer=0;maybeRedrawPane(pane);})(owner(pane.host)),Math.max(100,delay));return;}
    pane.redrawAfterReplay=false;pane.repaintAt=now;
    const size=pane.host.controls[pane.raw],[temporary,restore]=C.redrawSizes(size.cols,size.rows,pane.host.capabilities.resize_ack?2:20);
    pane.redrawRestore=restore;requestResize(pane,temporary,false,'redraw-out');
  }
  function input(sid,text) {
    const pane=panes.get(sid);
    if(!livePane(pane)||!pane.ready||!pane.host.connected||!pane.host.supported||!openTabs.includes(sid))return;
    pane.lastInputAt=performance.now();const frame=C.targetFrame(17,pane.raw,text);
    if(socketSend(pane.host,frame))pane.questions?.observeAcceptedInput(text);
  }
  function takeControl(sid=active){const pane=panes.get(sid);if(pane){pane.wantControl=true;pane.lastAttempt='';pane.redrawAfterReplay=true;pane.resizePaused=false;fitPane(pane,true);}}
  function openView(sid, takeover=false) {
    if(!sessionInfo(sid))return;
    const changed=active!==sid||!panes.has(sid);
    if(!panes.has(sid))createPane(sid);
    if(!openTabs.includes(sid))openTabs.push(sid);
    if(!layout)layout=sid;
    else if(!visibleIDs().includes(sid)){layout=replaceLeaf(layout,active,sid);}
    active=sid;selectedHost=panes.get(sid).host.id;panes.get(sid).host.selectedGroup=sessionInfo(sid).win.group;updateConnection();renderHosts();
    if(takeover&&(changed||!panes.get(sid).host.controls[panes.get(sid).raw]?.owner)){const pane=panes.get(sid);pane.wantControl=true;pane.lastAttempt='';pane.redrawAfterReplay=true;}
    renderTree();renderTabs();renderLayout();subscribe();panes.get(sid).term.focus();
    $('app').classList.remove('mobile-sidebar');
  }
  function replaceLeaf(tree,find,replacement){if(typeof tree==='string')return tree===find?replacement:tree;if(!tree)return replacement;return {...tree,a:replaceLeaf(tree.a,find,replacement),b:replaceLeaf(tree.b,find,replacement)};}
  function prune(tree,sid){if(typeof tree==='string')return tree===sid?null:tree;if(!tree)return null;const a=prune(tree.a,sid),b=prune(tree.b,sid);return a&&b?{...tree,a,b}:a||b;}
  function removeView(sid, redraw=true){
    openTabs=openTabs.filter(id=>id!==sid);layout=prune(layout,sid);
    if(active===sid){const first=tree=>typeof tree==='string'?tree:tree?first(tree.a):'';active=first(layout)||openTabs[openTabs.length-1]||'';}
    if(!layout&&active)layout=active;
    // Closed views unsubscribe and release GPU/parser resources. Reopen requests bounded history.
    cancelAnimationFrame(panes.get(sid)?.followFrame);cancelAnimationFrame(panes.get(sid)?.scrollIntentFrame);C.disposePane(panes,sid);
    if(active&&panes.has(active)){selectedHost=panes.get(active).host.id;updateConnection();renderHosts();}
    if(redraw){renderTree();renderTabs();renderLayout();subscribe();}
  }
  function activate(sid,focus=true){if(active===sid)return;active=sid;selectedHost=panes.get(sid).host.id;updateConnection();renderHosts();renderTree();renderTabs();if(mobile()||maximized)renderLayout();else for(const p of panes.values())p.el.classList.toggle('active',p.sid===active);updateFooter();if(focus)panes.get(sid)?.term.focus();}
  function split(direction){
    const candidate=openTabs.find(id=>id!==active&&!visibleIDs().includes(id))||catalog.flatMap(w=>w.sessions||[]).find(s=>s.id!==active&&!visibleIDs().includes(s.id))?.id;
    if(!active||!candidate){notice('先新建另一个终端，再使用分屏。');return;}
    if(!panes.has(candidate))createPane(candidate);if(!openTabs.includes(candidate))openTabs.push(candidate);
    layout=replaceLeaf(layout,active,{direction,ratio:50,a:active,b:candidate});maximized=false;active=candidate;selectedHost=panes.get(candidate).host.id;updateConnection();renderHosts();
    renderTabs();renderTree();renderLayout();subscribe();
  }
  function renderLayout(){
    for(const p of panes.values()){p.el.hidden=true;p.el.classList.toggle('active',p.sid===active);}
    for(const el of [...$('stage').children])if(el.classList.contains('split'))el.remove();
    $('empty').hidden=!!layout;
    if(!layout){updateFooter();return;}
    const draw=(tree,parent)=>{
      if(typeof tree==='string'){const p=panes.get(tree);if(p){parent.append(p.el);p.el.hidden=false;}return;}
      const box=node('div','split '+(tree.direction==='vertical'?'vertical':''));const a=node('div','split-child'),b=node('div','split-child'),divider=node('div','divider');
      a.style.flex='0 0 calc('+tree.ratio+'% - 2.5px)';b.style.flex='1';box.append(a,divider,b);parent.append(box);draw(tree.a,a);draw(tree.b,b);
      divider.setAttribute('role','separator');divider.setAttribute('aria-orientation',tree.direction==='vertical'?'horizontal':'vertical');divider.tabIndex=0;
      const apply=ratio=>{tree.ratio=Math.max(15,Math.min(85,ratio));a.style.flexBasis='calc('+tree.ratio+'% - 2.5px)';};
      divider.onkeydown=e=>{if(['ArrowLeft','ArrowUp','ArrowRight','ArrowDown'].includes(e.key)){e.preventDefault();apply(tree.ratio+(['ArrowLeft','ArrowUp'].includes(e.key)?-3:3));}};
      divider.onpointerdown=e=>{e.preventDefault();divider.setPointerCapture(e.pointerId);divider.classList.add('dragging');divider.onpointermove=event=>{const rect=box.getBoundingClientRect();apply(tree.direction==='vertical'?(event.clientY-rect.top)/rect.height*100:(event.clientX-rect.left)/rect.width*100);};divider.onpointerup=()=>{divider.onpointermove=null;divider.classList.remove('dragging');};};
    };
    draw(mobile()||maximized?active:layout,$('stage'));
    scheduleVisibleFit();updateFooter();
  }
  function subscribe(){
    for(const host of hosts.values()){
      const panesForHost=openTabs.map(id=>panes.get(id)).filter(pane=>pane?.host===host&&sessionInfo(pane.sid)),sessions=panesForHost.map(pane=>pane.raw);
      const signature=JSON.stringify(sessions),body={sessions};
      if(host.capabilities.history_resume===1){
        const historyAfter={};
        for(const pane of panesForHost)if(pane.historyInitialized&&pane.historyCursor)historyAfter[pane.raw]={...pane.historyCursor};
        if(Object.keys(historyAfter).length)body.history_after=historyAfter;
      }
      if(signature!==host.subscriptionSignature&&send(owner(host),16,body))host.subscriptionSignature=signature;
    }
  }
  function renderTabs(){
    $('tabs').replaceChildren();
    for(const id of openTabs){const info=sessionInfo(id);if(!info)continue;const context=owner(info.win.host),guard=action=>()=>{if(validOwner(context))action();};const tab=node('div','tab');tab.dataset.current=String(id===active);tab.setAttribute('role','presentation');
      const pick=button(info.win.title||info.session.shell,'切换到 '+(info.win.title||info.session.shell),guard(()=>openView(id)));pick.setAttribute('role','tab');pick.setAttribute('aria-selected',String(id===active));
      TabThemes.apply(tab,info.win.tab_theme);
      tab.oncontextmenu=event=>tabMenu(event,info.win,context);
      tab.append(pick,button('×','关闭视图（保留后台进程）',guard(()=>removeView(id))));$('tabs').append(tab);
    }
    renderTabPicker();
    const current=$('tabs').querySelector('[aria-selected="true"]')?.parentElement;
    if(current){const tabs=$('tabs'),a=tabs.getBoundingClientRect(),b=current.getBoundingClientRect();if(b.left<a.left)tabs.scrollLeft+=b.left-a.left;else if(b.right>a.right)tabs.scrollLeft+=b.right-a.right;}
    updateTabNavigation();
  }
  function updateTabNavigation(){const tabs=$('tabs');if(!$('tabs-scroll-left'))return;$('tabs-scroll-left').disabled=tabs.scrollLeft<=1;$('tabs-scroll-right').disabled=tabs.scrollLeft+tabs.clientWidth>=tabs.scrollWidth-1;positionTabPicker();}
  function positionTabPicker(){const menu=$('tabs-overflow-menu');if(!menu||menu.hidden)return;const anchor=$('tabs-overflow-toggle').getBoundingClientRect(),size=menu.getBoundingClientRect();menu.style.position='fixed';menu.style.right='auto';menu.style.left=Math.max(8,Math.min(anchor.right-size.width,innerWidth-size.width-8))+'px';menu.style.top=Math.max(8,Math.min(anchor.bottom+6,innerHeight-size.height-8))+'px';}
  function closeTabPicker(focus=false){const menu=$('tabs-overflow-menu');if(!menu)return;menu.hidden=true;$('tabs-overflow-toggle').setAttribute('aria-expanded','false');if(focus)$('tabs-overflow-toggle').focus();}
  function renderTabPicker(){
    const menu=$('tabs-overflow-menu');if(!menu)return;menu.replaceChildren();
    const groups=new Map();for(const id of openTabs){const info=sessionInfo(id);if(!info)continue;const host=info.win.host;if(!groups.has(host))groups.set(host,[]);groups.get(host).push({id,info});}
    $('tabs-overflow-toggle').textContent='全部窗口 ('+[...groups.values()].reduce((n,items)=>n+items.length,0)+') ▾';
    for(const [host,items] of groups){
      menu.append(node('div','tab-picker-heading',(host.identity.machine||host.name)+(host.config.code?' · '+host.config.code:'')));
      for(const {id,info} of items){
        const context=owner(host),row=button('','切换到 '+(info.win.title||info.session.shell),()=>{closeTabPicker();if(validOwner(context)&&sessionInfo(id))openView(id);});
        row.className='tab-picker-row';row.setAttribute('role','menuitemradio');row.setAttribute('aria-checked',String(id===active));row.dataset.host=host.id;row.dataset.session=info.session.raw;
        row.append(node('span','session-dot'),node('span','tab-picker-title',info.win.title||info.session.shell),node('span','',id===active?'✓':''));menu.append(row);
      }
    }
    if(!groups.size)menu.append(node('p','muted','尚未打开窗口'));
    updateSessionDots();positionTabPicker();
  }
  function tabMenu(event,win,context){
    if(context&&!validOwner(context))return;
    win=catalog.find(item=>item.id===win.id);if(!win)return;
    context=context||owner(win.host);
    TabThemes.open(event,{theme:win.tab_theme,rename:()=>rename(win.id,context),
      select:theme=>send(context,15,{action:'tab-theme',window:win.raw,theme})});
  }
  function updateSessionDots(){
    const now=performance.now(),labels={disconnected:'已断开',waiting:'等待输入（暂未收到新输出）',refreshing:'正在刷新'};
    for(const row of document.querySelectorAll('#tree .session-row,#tabs-overflow-menu .tab-picker-row')){
      const host=hosts.get(row.dataset.host),last=host?.activity.get(row.dataset.session);
      const state=!host?.connected?'disconnected':last!==undefined&&now-last<1800?'refreshing':'waiting';
      if(row.dataset.state===state)continue;
      row.dataset.state=state;const dot=row.querySelector('.session-dot');
      dot.title=labels[state];dot.setAttribute('aria-label','状态：'+labels[state]);
    }
  }
  function agentType(session,windowTitle=''){
    for(const [pattern,label] of [[/\bgemini\b/i,'Gemini'],[/\bopen[ -]?code\b/i,'OpenCode'],[/\bcopilot\b/i,'Copilot'],[/\baider\b/i,'Aider']])if(pattern.test(String(session.agent||'')))return label;
    const profile=C.agentProfiles.find(profile=>profile.id===C.agentModeFromSession(session,windowTitle));if(profile)return profile.label;
    const values=[session.agent,session.learnedAgent,session.command,session.argv,session.title,windowTitle,session.shell];
    for(const value of values){
      const text=Array.isArray(value)?value.join(' '):String(value||'');
      for(const [pattern,label] of [[/\bcodex\b/i,'Codex'],[/\bclaude(?:[ _-]code)?\b/i,'Claude Code'],[/\bkimi\b/i,'Kimi'],[/\bgemini\b/i,'Gemini'],[/\bopen[ -]?code\b/i,'OpenCode'],[/\bcopilot\b/i,'Copilot'],[/\baider\b/i,'Aider']])if(pattern.test(text))return label;
    }
    return '其他';
  }
  function renderTree(){
    const tree=$('tree');tree.replaceChildren();let count=0;
    for(const host of hosts.values()){
      const computer=node('section','computer-tree');computer.dataset.host=host.id;
      computer.append(hostRow(host));
      const windows=node('div','computer-windows');windows.id='host-windows-'+encodeURIComponent(host.id);windows.hidden=host.collapsed;computer.append(windows);
      const entries=catalog.filter(win=>win.host===host).flatMap(win=>win.sessions.map(session=>({win,session})));
      const context=owner(host);
      for(const {win,session} of entries){count++;const row=node('div','session-row');row.dataset.current=String(session.id===active);row.dataset.host=host.id;row.dataset.session=session.raw;
        const type=agentType(session,win.title),open=button('','打开 '+win.title+'（'+type+'）',()=>{if(validOwner(context))openView(session.id);});open.className='session-open';const text=node('span','session-label',win.title),agent=node('span','session-shell','（'+type+'）');agent.title=type;const dot=node('i','session-dot');dot.setAttribute('role','img');open.append(dot,text,agent);
        row.append(open,button('···','重命名窗口',()=>rename(win.id,context)));TabThemes.apply(row,win.tab_theme);row.oncontextmenu=event=>tabMenu(event,win,context);windows.append(row);
      }
      tree.append(computer);
    }
    if(!catalog.length)tree.append(node('p','muted small','暂无会话。新建终端开始工作。'));
    $('session-count').textContent=count+' 个终端';
    updateSessionDots();
  }
  function updateFooter(){
    const pane=panes.get(active),info=sessionInfo(active),host=currentHost();
    $('dimensions').textContent=pane?pane.term.cols+' × '+pane.term.rows:'—';$('effective-renderer').textContent=pane?pane.renderer.toUpperCase():'—';
    if(host?.connected)status((info?.session.cwd||'工作区已连接')+(host.meter.rtt===null?'':' · '+Math.round(host.meter.rtt)+' ms'));
    for(const pane of panes.values()){const lease=pane.host.controls[pane.raw];pane.own.textContent=lease?(lease.owner?'尺寸主控':'共享观看'):'';}
    $('take-control').disabled=!pane||!pane.host.connected;
    $('agent-mode').value=pane?.agentMode||'auto';$('agent-mode').disabled=!pane;
    for(const id of ['new-session','empty-new'])$(id).disabled=!host?.connected;
  }
  function newSession(){newSessionFor(currentHost());}
  function newSessionFor(host){
    if(!host?.connected)return;
    host.sessionsBeforeOpen=new Set(host.catalog.flatMap(win=>win.sessions||[]).map(session=>session.id));host.nextSplit='new';
    const group=host.groups.find(group=>key(host,group.id)===host.selectedGroup)?.id||host.groups[0]?.id||'default';
    send(owner(host),6,{shell:$('preset').value,group});
  }
  function rename(id,context){
    const win=catalog.find(win=>win.id===id);if(!win)return;context=context||owner(win.host);if(!validOwner(context))return;
    renameWindow={...context,id:win.raw};$('rename-value').value=win.title;$('rename-dialog').showModal();$('rename-value').focus();$('rename-value').select();
  }
  function terminate(sid){const pane=panes.get(sid);if(!livePane(pane))return;terminateSession={...owner(pane.host),id:pane.raw};$('terminate-dialog').querySelector('p').textContent='这将终止当前终端中的进程。其他客户端也会断开此会话。';$('terminate-dialog').showModal();}
  function setCtrl(value){ctrl=value;$('ctrl-key').setAttribute('aria-pressed',String(value));}
  function endpoint(settings){
    const url=new URL(C.socketURL(settings.relay,location.origin));url.searchParams.delete('token');url.hash='';
    return JSON.stringify([url.toString(),settings.code.trim()]);
  }
  function showConnection(){
    cancelAccountLogin();
    const host=currentHost();if(!host)return;connectionOwner={...owner(host),target:endpoint(host.config),token:host.config.token};
    connectionAuthorization=host.config.authMode==='account'?{...owner(host),target:endpoint(host.config),url:relayURL(host.config.relay),account:accountFor(host.config.relay)}:null;
    $('connection-auth').value=host.config.authMode||'manual';
    $('relay-url').value=host.config.relay;$('relay-code').value=host.config.code;$('access-token').value=host.config.token;
    $('remember-token').checked=!!read(localStorage,host.id==='local'?'sma.workspace.token':hostTokenKey(host.id));
    $('connection-error').textContent='';$('account-relay-url').value=host.config.relay||$('default-relay').value;renderAccountPanel();renderAuthField('connection');$('connection-dialog').showModal();$('relay-url').focus();
  }
  function loadHosts(){
    try{const seen=new Set(['local']);return JSON.parse(read(localStorage,'sma.workspace.hosts')||'[]').filter(host=>{
      if(!host||typeof host.id!=='string'||!host.id||seen.has(host.id)||typeof host.name!=='string'||typeof host.code!=='string'||!host.code.trim()||host.code.length>128||typeof host.relay!=='string'||!host.relay.trim())return false;
      try{C.socketURL(host.relay,location.origin);seen.add(host.id);return true;}catch{return false;}
    }).slice(0,31).map(({id,name,code,relay,authMode})=>({id,name:name.slice(0,40),code,relay,authMode:authMode==='account'?'account':'manual'}));}catch{return [];}
  }
  function profileToken(profile){
    const key='sma.workspace.profile-token.'+profile.id;
    const target=profileBinding(profile);
    for(const storage of [sessionStorage,localStorage]){const binding=read(storage,key+'.target');if(binding===target||(!profile.code&&!profile.excludedRooms?.length&&binding===profile.endpoint)){const value=read(storage,key);if(value)return value;}}
    return '';
  }
  function profileBinding(profile){return JSON.stringify([profile.endpoint,profile.code||'',...(profile.excludedRooms?.length?[profile.excludedRooms]:[])]);}
  function roomAllowed(scope,code){return (!scope.onlyRoom||scope.code===code)&&!scope.excludedRooms?.includes(code);}
  function loadRelayProfiles(){
    try{return JSON.parse(read(localStorage,'sma.workspace.relay-profiles')||'[]').filter((profile,index,all)=>{
      if(!profile||typeof profile.id!=='string'||!profile.id||profile.id.length>128||typeof profile.endpoint!=='string'||all.findIndex(item=>item?.id===profile.id)!==index)return false;
      if(profile.code!==undefined&&(typeof profile.code!=='string'||profile.code.length>128||/[\x00-\x1f\x7f]/.test(profile.code)))return false;
      if(profile.excludedRooms!==undefined&&(!Array.isArray(profile.excludedRooms)||profile.excludedRooms.length>64||profile.excludedRooms.some(code=>typeof code!=='string'||!code||code.length>128||/[\x00-\x1f\x7f]/.test(code))))return false;
      try{return C.socketURL(profile.endpoint,location.origin)===profile.endpoint;}catch{return false;}
    }).slice(0,32).map(({id,endpoint,code,excludedRooms})=>({id,endpoint,code:code||'',excludedRooms:[...new Set(excludedRooms||[])]}));}catch{return [];}
  }
  function profileEndpoint(address,port){
    address=address.trim();if(!address)throw new Error('请填写 IP 或服务器地址。');
    if(port&&(!/^\d+$/.test(port)||Number(port)<1||Number(port)>65535))throw new Error('端口应为 1～65535。');
    const explicit=/^[a-z]+:\/\//i.test(address);
    if(!explicit&&!address.startsWith('[')&&address.split('/')[0].split(':').length>2){const slash=address.indexOf('/');address='['+(slash<0?address:address.slice(0,slash))+']'+(slash<0?'':address.slice(slash));}
    const embedded=address.match(/^(?:[a-z]+:\/\/)?(?:\[[^\]]+\]|[^/?#:]+):(\d+)(?:[/?#]|$)/i),url=new URL(explicit?address:'https://'+address);
    if(!explicit&&['localhost','127.0.0.1','[::1]'].includes(url.hostname))url.protocol='http:';
    if(embedded)url.port=embedded[1];else if(port)url.port=port;
    if(url.hostname==='117.50.48.22'&&url.port==='8817'&&!explicit)url.protocol='http:';
    return C.socketURL(url.toString(),location.origin);
  }
  function renderRelayProfiles(){
    const list=$('relay-profile-list');if(!list)return;list.replaceChildren();
    relayProfiles.forEach((profile,index)=>{
      const directory=directories.get(profile.endpoint),token=profileToken(profile);
      const count=(directory?.hosts||[]).filter(host=>host.online&&directory.tokens[host.token_index]===token&&(!profile.code||host.code===profile.code)&&!profile.excludedRooms?.includes(host.code)).length;
      const row=node('div','relay-profile-row');row.append(node('span','',profile.endpoint),node('small','muted','凭据 '+(index+1)+' · '+(!token?'需要填写 Token':directory?.phase==='connected'?count+' 个房间':directory?.phase==='unsupported'?'中继暂不支持此凭据的自动发现':'正在连接…')));
      row.append(button('移除','移除连接 '+(index+1),()=>{
        relayProfiles=relayProfiles.filter(item=>item.id!==profile.id);save(localStorage,'sma.workspace.relay-profiles',JSON.stringify(relayProfiles));
        for(const storage of [localStorage,sessionStorage])for(const suffix of ['', '.target'])save(storage,'sma.workspace.profile-token.'+profile.id+suffix,'');
        refreshDirectories();renderRelayProfiles();
      }));
      if(token)for(const room of (directory?.availableHosts||directory?.hosts||[]).filter(room=>room.online&&directory.tokens[room.token_index]===token&&(!profile.code||profile.code===room.code))){
        const label=node('label','check relay-room-choice'),check=node('input');check.type='checkbox';check.checked=!profile.excludedRooms?.includes(room.code);
        check.onchange=()=>{
          if(!check.checked&&(profile.excludedRooms||[]).length>=64&&!profile.excludedRooms.includes(room.code)){check.checked=true;return;}
          const excluded=new Set(profile.excludedRooms||[]);check.checked?excluded.delete(room.code):excluded.add(room.code);profile.excludedRooms=[...excluded];
          const key='sma.workspace.profile-token.'+profile.id;
          for(const storage of [sessionStorage,localStorage])if(read(storage,key))save(storage,key+'.target',profileBinding(profile));
          save(localStorage,'sma.workspace.relay-profiles',JSON.stringify(relayProfiles));refreshDirectories();renderRelayProfiles();
        };
        label.append(check,node('span','',(room.machine||room.code)+' · '+room.code));row.append(label);
      }
      list.append(row);
    });
    $('relay-profile-status').textContent=relayProfiles.length?'房间会自动显示在左侧，无需填写房间号。':'添加连接后，将自动显示此 Token 有权访问的房间。';
  }
  function showRelayProfiles(legacy='connection'){
    profileLegacy=legacy;closeAppMenu();cancelRelayProbe();$('relay-profile-error').textContent='';
    $('relay-profiles-dialog').querySelector('details').open=false;
    let url;try{url=new URL(C.socketURL(currentHost()?.config.code?currentHost().config.relay:relayProfiles.at(-1)?.endpoint||homeRelays[0]?.url||$('default-relay').value,location.origin));}catch{url=new URL(C.socketURL($('default-relay').value,location.origin));}
    $('relay-profile-address').value=['/','/ws','/ws/'].includes(url.pathname)?url.hostname:url.toString();$('relay-profile-port').value=url.port||(url.protocol==='wss:'?'443':'80');$('relay-profile-room').value='';$('relay-profile-token').value='';$('relay-profile-remember').checked=read(localStorage,'sma.workspace.profile-remember')!=='off';prefilledTokenTarget='';defaultTokenEdited=false;
    const stored=[...relayProfiles].reverse().find(profile=>profile.endpoint===url.toString()&&!profile.code&&profileToken(profile));
    if(stored){$('relay-profile-token').value=profileToken(stored);prefilledTokenTarget=stored.endpoint;defaultTokenEdited=true;}
    renderRelayProfiles();$('relay-profiles-dialog').showModal();prefillRuntimeToken();$('relay-profile-address').focus();
    const local=hosts.get('local');if(local?.connected&&homeRelayAccess())send(owner(local),16,{sessions:openTabs.map(id=>panes.get(id)).filter(pane=>pane?.host===local).map(pane=>pane.raw)});
  }
  function prefillRuntimeToken(){
    if(defaultTokenEdited||!$('relay-profiles-dialog').open)return;
    let target;try{target=profileEndpoint($('relay-profile-address').value,$('relay-profile-port').value);}catch{return;}
    const local=hosts.get('local'),isDefault=target===C.socketURL($('default-relay').value,location.origin);
    const configured=homeRelayAccess()&&local?.connected&&homeRelays.some(relay=>{try{return C.socketURL(relay.url,location.origin)===target;}catch{return false;}});
    const token=(isDefault?runtimeDefaultToken:'')||(configured?local.config.token:'');
    if(!isDefault&&!configured)return;
    $('relay-profile-token').value=token;prefilledTokenTarget=token?target:'';
    $('relay-profile-token').title=token?(isDefault&&runtimeDefaultToken?'默认来自本机 token.txt，可手动替换':'来自本机已配置的中继连接，可手动替换'):'';
  }
  function closeAppMenu(){const menu=$('app-menu');if(menu)menu.hidden=true;$('app-menu-toggle')?.setAttribute('aria-expanded','false');}
  function updateDaemonInfo(host,info){
    const pid=Number.isInteger(info.pid)&&info.pid>0&&info.pid<=2147483647?info.pid:null;
    const uptime=typeof info.uptime_seconds==='number'&&Number.isFinite(info.uptime_seconds)&&info.uptime_seconds>=0&&info.uptime_seconds<=Number.MAX_SAFE_INTEGER?info.uptime_seconds:null;
    host.daemonInfo={pid,uptime,receivedAt:performance.now()};
  }
  function invalidateDaemonMonitorHost(host){
    if(daemonMonitor.pending?.context.host===host)daemonMonitor.pending=null;
    if(daemonMonitor.denied?.host===host)daemonMonitor.denied=null;
    if(daemonMonitor.hostID===host.id){$('daemon-monitor-log').textContent='';$('daemon-monitor-status').textContent='连接已变更，等待重新读取运行日志。';}
  }
  function daemonUptime(host){
    const info=host.daemonInfo;if(info?.uptime===null||info?.uptime===undefined)return '运行时长未知';
    const seconds=Math.floor(info.uptime+Math.max(0,performance.now()-info.receivedAt)/1000);
    return '已运行 '+(seconds>=86400?Math.floor(seconds/86400)+'天 ':'')+Math.floor(seconds%86400/3600)+'时 '+Math.floor(seconds%3600/60)+'分 '+seconds%60+'秒';
  }
  function renderDaemonMonitor(){
    if(!$('daemon-monitor-dialog').open)return;
    if(!hosts.has(daemonMonitor.hostID)){
      daemonMonitor.hostID=hosts.has(selectedHost)?selectedHost:hosts.keys().next().value||'';
      daemonMonitor.pending=null;daemonMonitor.denied=null;$('daemon-monitor-log').textContent='';
    }
    const list=$('daemon-monitor-list'),select=$('daemon-monitor-select'),options=[];list.replaceChildren();
    for(const host of hosts.values()){
      const label=host.identity.machine||host.name,room=host.config.code||'本机直连';
      const phase=host.connected?'已连接':({'auth-failed':'验证失败',connecting:'连接中',reconnecting:'重连中',offline:'已断开',invalid:'连接无效',unsupported:'后台版本不兼容'}[host.phase]||'未连接');
      const row=node('div','daemon-monitor-row');row.setAttribute('role','listitem');row.dataset.host=host.id;row.dataset.current=String(host.id===daemonMonitor.hostID);
      row.append(node('strong','',label+' · '+room),node('span','',phase+' · '+host.catalog.length+' 个窗口 / '+host.catalog.reduce((count,win)=>count+(win.sessions||[]).length,0)+' 个终端'),node('span','',(host.daemonInfo?.pid?'PID '+host.daemonInfo.pid:'PID 未知')+' · '+daemonUptime(host)));
      list.append(row);const option=node('option','',label+' · '+room);option.value=host.id;options.push(option);
    }
    if(select.options.length!==options.length||options.some((option,index)=>select.options[index].value!==option.value||select.options[index].textContent!==option.textContent))select.replaceChildren(...options);
    select.value=daemonMonitor.hostID;select.disabled=!hosts.size;
  }
  function requestDaemonLogs(){
    if(!$('daemon-monitor-dialog').open)return;
    renderDaemonMonitor();const host=hosts.get(daemonMonitor.hostID);
    if(!host?.connected){daemonMonitor.pending=null;daemonMonitor.denied=null;$('daemon-monitor-log').textContent='';$('daemon-monitor-status').textContent='此电脑未连接，连接恢复后自动读取日志。';return;}
    if(host.capabilities.daemon_logs!==true){daemonMonitor.pending=null;daemonMonitor.denied=null;$('daemon-monitor-log').textContent='';$('daemon-monitor-status').textContent='此电脑的后台版本不支持运行日志，请升级对应电脑的后台；窗口和终端状态仍可查看。';return;}
    if(daemonMonitor.denied&&validOwner(daemonMonitor.denied)&&daemonMonitor.denied.host===host)return;
    const pending=daemonMonitor.pending;
    if(pending&&validOwner(pending.context)&&pending.context.host===host&&performance.now()<pending.deadline)return;
    daemonMonitor.pending=null;
    const context=owner(host),request='daemon-log-'+(++daemonMonitor.sequence)+'-'+Date.now().toString(36);
    if(send(context,20,{request,lines:80})){
      daemonMonitor.pending={context,request,deadline:performance.now()+6000};
      $('daemon-monitor-status').textContent='正在读取 '+(host.identity.machine||host.name)+' 的运行日志…';
    }else $('daemon-monitor-status').textContent='日志连接暂不可用，稍后自动重试。';
  }
  function receiveDaemonLogs(host,info){
    const pending=daemonMonitor.pending;
    if(!$('daemon-monitor-dialog').open||!pending||!validOwner(pending.context)||pending.context.host!==host||host.id!==daemonMonitor.hostID||performance.now()>pending.deadline||!info||info.request!==pending.request)return;
    daemonMonitor.pending=null;
    if(info.error){daemonMonitor.denied=info.error_code==='owner_required'?pending.context:null;$('daemon-monitor-status').textContent=String(info.error).slice(0,300);$('daemon-monitor-log').textContent='';return;}
    if(typeof info.text!=='string'){$('daemon-monitor-status').textContent='后台返回的日志内容无效，稍后自动重试。';return;}
    if(info.pid!==undefined||info.uptime_seconds!==undefined)updateDaemonInfo(host,info);
    const bytes=encoder.encode(info.text);let start=Math.max(0,bytes.length-32768);while(start<bytes.length&&(bytes[start]&0xc0)===0x80)start++;
    const text=decoder.decode(bytes.subarray(start)).split('\n').slice(-80).join('\n'),log=$('daemon-monitor-log');
    const follow=log.scrollHeight-log.clientHeight-log.scrollTop<=4,position=log.scrollTop;
    log.textContent=text;if(follow)log.scrollTop=log.scrollHeight;else log.scrollTop=position;
    $('daemon-monitor-status').textContent='每 2 秒更新所选电脑 · 最多 80 行';renderDaemonMonitor();
  }
  function closeDaemonMonitor(){clearInterval(daemonMonitor.timer);daemonMonitor.timer=null;daemonMonitor.pending=null;daemonMonitor.denied=null;}
  function showDaemonMonitor(){
    closeAppMenu();closeDaemonMonitor();daemonMonitor.hostID=hosts.has(selectedHost)?selectedHost:hosts.keys().next().value||'';
    $('daemon-monitor-log').textContent='';$('daemon-monitor-status').textContent='';$('daemon-monitor-dialog').showModal();requestDaemonLogs();daemonMonitor.timer=setInterval(requestDaemonLogs,2000);
  }
  function cancelRelayProbe(clear=true){
    const probe=relayProbe;relayProbe=null;if(probe){clearTimeout(probe.timer);probe.socket.onclose=null;probe.socket.close();}
    $('relay-profile-connect').disabled=true;$('relay-profile-connect').textContent='连接所选房间';$('relay-profile-form').querySelector('button[type=submit]').disabled=false;
    if(clear){$('relay-probe-rooms').replaceChildren();$('relay-probe-status').textContent='';}
  }
  function renderRelayProbe(probe){
    const list=$('relay-probe-rooms');list.replaceChildren();
    for(const [code,selected] of probe.selected)if(selected&&!probe.rooms.some(room=>room.code===code))probe.selected.delete(code);
    for(const room of probe.rooms){
      if(!probe.selected.has(room.code))probe.selected.set(room.code,true);
      const label=node('label','check relay-room-choice'),check=node('input');check.type='checkbox';check.checked=probe.selected.get(room.code);
      check.onchange=()=>{if(!check.checked&&[...probe.selected.values()].filter(value=>!value).length>=64){check.checked=true;return;}probe.selected.set(room.code,check.checked);$('relay-profile-connect').disabled=!probe.rooms.some(room=>probe.selected.get(room.code));};
      label.append(check,node('span','',(room.machine||room.code)+' · '+room.code));list.append(label);
    }
    $('relay-profile-connect').disabled=probe.rooms.length>0&&!probe.rooms.some(room=>probe.selected.get(room.code));
    $('relay-profile-connect').textContent=probe.rooms.length?'连接所选房间':'保存中继';
    $('relay-probe-status').textContent=probe.rooms.length?'测试成功，发现 '+probe.rooms.length+' 个房间；'+(probe.restoredSelection?'已保留上次勾选。':'默认全选。'):'服务器已连接，当前没有授权的在线房间；可保存中继，等待房间上线。';
  }
  function closeRoomMenu(){roomMenu?.remove();roomMenu=null;}
  async function closeRoomWindows(context,windows){
    const host=context.host;
    for(const id of windows){
      if(!validOwner(context)||!host.connected)return;
      if(!host.catalog.some(win=>win.id===id))continue;
      if(!send(context,9,{window:id}))return;
      const deadline=performance.now()+10000;
      while(validOwner(context)&&host.connected&&host.catalog.some(win=>win.id===id)&&performance.now()<deadline)await new Promise(resolve=>setTimeout(resolve,40));
      if(host.catalog.some(win=>win.id===id)){notice('部分窗口暂未确认关闭，请检查连接后重试。');return;}
    }
  }
  function showRoomMenu(event,host){
    closeRoomMenu();closeAppMenu();const context=owner(host),windows=host.catalog.map(win=>win.id);
    const menu=node('div','room-menu');menu.setAttribute('role','menu');roomMenu=menu;
    const add=(label,action,disabled=false,requiresConnection=true)=>{const item=button(label,label,()=>{closeRoomMenu();if(validOwner(context)&&(!requiresConnection||host.connected))action();});item.setAttribute('role','menuitem');item.disabled=disabled;menu.append(item);};
    add('新增窗口',()=>newSessionFor(host),!host.connected);
    add('刷新所有窗口',()=>{
      const sessions=openTabs.map(id=>panes.get(id)).filter(pane=>pane?.host===host).map(pane=>pane.raw);
      send(context,16,{sessions,refresh:true});for(const pane of panes.values())if(pane.host===host)pane.term.refresh(0,pane.term.rows-1);
    },!host.connected);
    add('关闭所有窗口',()=>{
      if(!confirm('关闭 '+(host.identity.machine||host.name)+' 的这 '+windows.length+' 个窗口？其中运行的终端进程也会结束。'))return;
      if(!validOwner(context)||!host.connected)return;
      closeRoomWindows(context,windows);
    },!host.connected||!windows.length);
    if(host.id!=='local'&&!host.autoDirectory)add('编辑工作区 '+host.name,()=>showWorkspace(host.id),false,false);
    document.body.append(menu);const rect=event.currentTarget.getBoundingClientRect();Object.assign(menu.style,{position:'fixed',left:Math.max(4,Math.min(rect.left,innerWidth-menu.offsetWidth-4))+'px',top:Math.max(4,Math.min(rect.bottom,innerHeight-menu.offsetHeight-4))+'px'});menu.querySelector('button:not(:disabled)')?.focus();
  }
  const hostTokenKey=id=>'sma.workspace.host-token.'+id;
  function boundToken(id,settings){
    const tokenKey=id==='local'?'sma.workspace.token':hostTokenKey(id),target=endpoint(settings);
    for(const storage of [sessionStorage,localStorage]){
      const token=read(storage,tokenKey),binding=read(storage,tokenKey+'.target');
      if(token&&binding===target)return token;
      if(token&&!binding){save(storage,tokenKey+'.target',target);return token;} // Migrate established per-host credentials once.
      if(token){save(storage,tokenKey,'');save(storage,tokenKey+'.target','');}
    }
    return '';
  }
  function saveHostToken(id,token,remember){const settings=savedHosts.find(host=>host.id===id);save(sessionStorage,hostTokenKey(id),token);save(localStorage,hostTokenKey(id),remember?token:'');for(const storage of [sessionStorage,localStorage])save(storage,hostTokenKey(id)+'.target',token&&settings?endpoint(settings):'');}
  function hostRow(host){
      const row=node('div','workspace-host');row.dataset.current=String(host.id===selectedHost);row.dataset.host=host.id;row.dataset.phase=host.phase;
      const toggle=button('', '',()=>{
        host.collapsed=!host.collapsed;save(sessionStorage,'sma.workspace.collapsed.'+host.id,String(host.collapsed));
        const windows=$('host-windows-'+encodeURIComponent(host.id));if(windows)windows.hidden=host.collapsed;
        updateToggle();
      });toggle.className='workspace-host-toggle';toggle.setAttribute('aria-controls','host-windows-'+encodeURIComponent(host.id));
      function updateToggle(){toggle.textContent=host.collapsed?'▸':'▾';toggle.setAttribute('aria-expanded',String(!host.collapsed));toggle.title=(host.collapsed?'展开':'收起')+'窗口列表';toggle.setAttribute('aria-label',toggle.title+' '+(host.identity.machine||host.name));}
      updateToggle();row.append(toggle);
      const pick=button('','切换到'+(host.id==='local'?host.name:' '+host.name),()=>switchHost(host.id));pick.className='workspace-host-pick';pick.setAttribute('aria-current',String(host.id===selectedHost));
      const rooms=host.config.code?[host.config.code]:host.id==='local'?[...new Set(homeRelays.map(relay=>relay.code).filter(code=>typeof code==='string'&&code))]:[];
      pick.append(node('span','workspace-host-name',host.identity.machine||host.name),node('small','muted',rooms.length?'房间号：'+rooms.join('、'):'未配置房间号'));
      pick.append(node('span','workspace-host-status',(host.connected?'已连接':host.phase)+' · '+(host.meter.rtt===null?'—':Math.round(host.meter.rtt)+' ms')),
        node('small','host-rates','↑ '+C.formatRate(host.rates.tx)+' ↓ '+C.formatRate(host.rates.rx)));row.append(pick);
      const actions=button('···','房间菜单 '+(host.identity.machine||host.name),event=>showRoomMenu(event,host));actions.className='workspace-host-edit';row.append(actions);return row;
  }
  function renderHosts(){
    for(const computer of $('tree').querySelectorAll('.computer-tree')){
      const host=hosts.get(computer.dataset.host);if(host)computer.querySelector('.workspace-host')?.replaceWith(hostRow(host));
    }
  }
  function switchHost(id){
    const host=hosts.get(id);if(!host)return;selectedHost=id;save(sessionStorage,'sma.workspace.activeHost',id);TabThemes.close();
    const existing=openTabs.find(key=>panes.get(key)?.host===host);
    if(existing)openView(existing,false);
    else{const sessions=host.catalog.flatMap(win=>win.sessions||[]);for(const session of sessions)openView(key(host,session.id),false);if(sessions.length)openView(key(host,sessions[0].id),false);}
    renderTree();renderHosts();updateConnection();updateFooter();
  }
  function showWorkspace(id=''){
    const host=savedHosts.find(item=>item.id===id);editingHost=host?.id||'';
    $('workspace-dialog-title').textContent=host?'编辑工作区':'增加工作区';$('workspace-name').value=host?.name||'';$('workspace-code').value=host?.code||'';
    $('workspace-relay').value=host?.relay||currentHost()?.config.relay||homeRelays.find(item=>item.up)?.url||homeRelays[0]?.url||savedHosts.find(item=>item.relay)?.relay||$('default-relay').value;
    workspaceAuthorization=host?.authMode==='account'?{target:endpoint(host),url:relayURL(host.relay),code:host.code,account:accountFor(host.relay)}:null;
    $('workspace-auth').value=host?.authMode||'manual';
    $('workspace-token').value=host?(read(sessionStorage,hostTokenKey(host.id))||read(localStorage,hostTokenKey(host.id))):'';
    $('workspace-remember').checked=!!(host&&read(localStorage,hostTokenKey(host.id)));$('workspace-remove').hidden=!host;$('workspace-error').textContent='';
    $('workspace-dialog').showModal();renderDiscovery();renderAuthField('workspace');$('workspace-code').focus();
  }
  function clearChangedCredential(prefix,original){
    try{if(original&&endpoint({relay:$(prefix==='workspace'?'workspace-relay':'relay-url').value,code:$(prefix==='workspace'?'workspace-code':'relay-code').value})!==endpoint(original))$(prefix==='workspace'?'workspace-token':'access-token').value='';}catch{}
  }
  for(const id of ['workspace-relay','workspace-code'])$(id).addEventListener('input',()=>{
    const original=savedHosts.find(host=>host.id===editingHost);clearChangedCredential('workspace',original);clearAccountSelection('workspace');$('workspace-verification').textContent='';renderDiscovery();
  });
  for(const id of ['relay-url','relay-code'])$(id).addEventListener('input',()=>{clearChangedCredential('connection',connectionOwner?.host.config);clearAccountSelection('connection');});
  function detachHost(host){
    stopHost(host);for(const [id,pane] of [...panes])if(pane.host===host)removeView(id,false);hosts.delete(host.id);aggregateCatalog();
  }
  $('workspace-form').onsubmit=event=>{
    event.preventDefault();const code=$('workspace-code').value.trim(),relay=$('workspace-relay').value.trim();
    if(!code||!relay||code.length>128){$('workspace-error').textContent='请填写有效房间码和中继地址。';return;}
    if(!editingHost&&hosts.size>=32){$('workspace-error').textContent='最多保存 32 台电脑。';return;}
    try{endpoint({relay,code});}catch(error){$('workspace-error').textContent=error.message;return;}
    const id=editingHost||(typeof crypto.randomUUID==='function'?crypto.randomUUID():'host-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2));
    const previous=savedHosts.find(host=>host.id===id),old=hosts.get(id),settings={id,name:($('workspace-name').value.trim()||'电脑 '+code).slice(0,40),relay,code};
    const authMode=$('workspace-auth').value;
    if(authMode==='account'&&!authorizedSelection('workspace',{relay,code})){$('workspace-error').textContent='请登录并从授权电脑列表重新选择目标。';return;}
    settings.authMode=authMode;let token=authMode==='account'?'':$('workspace-token').value.trim();if(previous&&endpoint(previous)!==endpoint(settings)&&token===old?.config.token)token='';
    const index=savedHosts.findIndex(host=>host.id===id);if(index<0)savedHosts.push(settings);else savedHosts[index]=settings;
    save(localStorage,'sma.workspace.hosts',JSON.stringify(savedHosts));saveHostToken(id,token,$('workspace-remember').checked);
    if(old)detachHost(old);const host=new HostConnection(id,settings.name,{relay,code,token,authMode});hosts.set(id,host);
    $('workspace-dialog').close();selectedHost=id;save(sessionStorage,'sma.workspace.activeHost',id);connect(host);refreshDirectories();renderTree();renderTabs();renderLayout();
  };
  $('workspace-close').onclick=$('workspace-cancel').onclick=()=>$('workspace-dialog').close();
  $('workspace-remove').onclick=()=>{
    const id=editingHost,host=hosts.get(id);if(!host)return;detachHost(host);
    savedHosts=savedHosts.filter(host=>host.id!==id);save(localStorage,'sma.workspace.hosts',JSON.stringify(savedHosts));saveHostToken(id,'',false);
    $('workspace-dialog').close();if(selectedHost===id)switchHost('local');refreshDirectories();renderHosts();renderTree();renderTabs();renderLayout();subscribe();
  };
  $('connection-form').onsubmit=event=>{
    event.preventDefault();if(!validOwner(connectionOwner)){$('connection-error').textContent='此连接已改变，请重新打开设置。';return;}
    const host=connectionOwner.host,relay=$('relay-url').value.trim(),code=$('relay-code').value.trim(),authMode=$('connection-auth').value;let token=authMode==='account'?'':$('access-token').value.trim();
    if(authMode==='account'&&!authorizedSelection('connection',{relay,code})){$('connection-error').textContent='请登录并重新选择此中继授权电脑。';return;}
    if(host.id!=='local'&&(!relay||!code)){$('connection-error').textContent='此工作区需要中继地址和房间码。';return;}
    try{if(endpoint({relay,code})!==connectionOwner.target&&token===connectionOwner.token)token='';}catch(error){$('connection-error').textContent=error.message;return;}
    if(host.id==='local'){
      homeConfig={relay,code,token,authMode};config={...homeConfig};save(localStorage,'sma.workspace.auth-mode',authMode);save(localStorage,'sma.workspace.relay',relay);save(localStorage,'sma.workspace.code',code);
      save(sessionStorage,'sma.workspace.token',token);save(localStorage,'sma.workspace.token',$('remember-token').checked?token:'');for(const storage of [sessionStorage,localStorage])save(storage,'sma.workspace.token.target',token?endpoint({relay,code}):'');
    }else{const saved=savedHosts.find(item=>item.id===host.id);saved.relay=relay;saved.code=code;saved.authMode=authMode;save(localStorage,'sma.workspace.hosts',JSON.stringify(savedHosts));saveHostToken(host.id,token,$('remember-token').checked);}
    // A different endpoint is a different authority: discard old directory and views.
    if(endpoint(host.config)!==endpoint({relay,code})){
      for(const [id,pane] of [...panes])if(pane.host===host)removeView(id,false);host.catalog=[];host.groups=[];host.controls={};host.identity={};aggregateCatalog();
    }
    host.config={relay,code,token,authMode};$('connection-dialog').close();connect(host);refreshDirectories();renderTree();renderTabs();renderLayout();
  };
  $('connection-close').onclick=()=>{cancelAccountLogin();returnToWorkspace=false;$('connection-dialog').close();};$('connect-button').onclick=()=>showRelayProfiles();
  $('rename-cancel').onclick=()=>$('rename-dialog').close();$('rename-form').onsubmit=e=>{e.preventDefault();send(renameWindow,15,{action:'title',window:renameWindow?.id,title:$('rename-value').value.trim()});$('rename-dialog').close();};
  $('terminate-cancel').onclick=()=>$('terminate-dialog').close();$('terminate-form').onsubmit=async e=>{
    e.preventDefault();const context=terminateSession,host=context?.host,dialog=$('terminate-dialog'),message=dialog.querySelector('p'),submit=dialog.querySelector('.danger');
    if(!validOwner(context)||!host.connected||!send(context,9,{session:context.id})){message.textContent='当前电脑未连接，结束指令未发送。请恢复连接后重试。';return;}
    submit.disabled=true;message.textContent='正在等待后台确认结束会话…';
    try{
      const exists=()=>host.catalog.some(win=>(win.sessions||[]).some(session=>session.id===context.id)),deadline=performance.now()+8000;
      while(validOwner(context)&&host.connected&&exists()&&performance.now()<deadline)await new Promise(resolve=>setTimeout(resolve,40));
      if(validOwner(context)&&host.connected&&!exists())dialog.close();
      else message.textContent='尚未收到后台结束确认，请检查连接后重试。';
    }finally{submit.disabled=false;}
  };
  $('new-project').onclick=()=>showRelayProfiles('workspace');
  const tabPicker=node('div','tab-picker'),tabPickerToggle=button('全部窗口 ▾','查看全部已打开窗口',()=>{const menu=$('tabs-overflow-menu'),show=menu.hidden;closeRoomMenu();closeAppMenu();renderTabPicker();menu.hidden=!show;positionTabPicker();tabPickerToggle.setAttribute('aria-expanded',String(show));if(show)(menu.querySelector('[aria-checked="true"]')||menu.querySelector('button'))?.focus();});
  tabPickerToggle.id='tabs-overflow-toggle';tabPickerToggle.setAttribute('aria-haspopup','menu');tabPickerToggle.setAttribute('aria-expanded','false');tabPickerToggle.setAttribute('aria-controls','tabs-overflow-menu');
  const tabPickerMenu=node('div');tabPickerMenu.id='tabs-overflow-menu';tabPickerMenu.hidden=true;tabPickerMenu.setAttribute('role','menu');tabPickerMenu.setAttribute('aria-label','全部已打开窗口');tabPicker.append(tabPickerToggle,tabPickerMenu);
  const titlebar=document.querySelector('.titlebar');titlebar.querySelector('.header-spacer').before(tabPicker);
  for(const [direction,label,arrow] of [[-1,'向左滚动页签','‹'],[1,'向右滚动页签','›']]){const control=button(arrow,label,()=>$('tabs').scrollBy({left:direction*Math.max(120,$('tabs').clientWidth*.7),behavior:'smooth'}));control.className='tab-scroll';control.id=direction<0?'tabs-scroll-left':'tabs-scroll-right';if(direction<0)$('tabs').before(control);else $('tabs').after(control);}
  $('tabs').addEventListener('scroll',updateTabNavigation,{passive:true});addEventListener('resize',updateTabNavigation);
  const titlebarObserver=new ResizeObserver(()=>{document.documentElement.style.setProperty('--workspace-titlebar-height',titlebar.getBoundingClientRect().height+'px');updateTabNavigation();});titlebarObserver.observe(titlebar);addEventListener('beforeunload',()=>titlebarObserver.disconnect(),{once:true});
  $('app-menu-toggle').onclick=()=>{closeTabPicker();closeRoomMenu();const menu=$('app-menu');menu.hidden=!menu.hidden;$('app-menu-toggle').setAttribute('aria-expanded',String(!menu.hidden));if(!menu.hidden)menu.querySelector('button')?.focus();};
  $('menu-connect').onclick=()=>showRelayProfiles();
  $('menu-daemon-monitor').onclick=showDaemonMonitor;
  $('daemon-monitor-close').onclick=()=>$('daemon-monitor-dialog').close();
  $('daemon-monitor-dialog').addEventListener('close',closeDaemonMonitor);
  $('daemon-monitor-select').onchange=()=>{daemonMonitor.hostID=$('daemon-monitor-select').value;daemonMonitor.pending=null;daemonMonitor.denied=null;$('daemon-monitor-log').textContent='';requestDaemonLogs();};
  $('refresh-relays').onclick=()=>refreshRelayCatalogs();
  $('relay-profiles-close').onclick=()=>$('relay-profiles-dialog').close();
  $('relay-profiles-dialog').addEventListener('close',()=>cancelRelayProbe());
  $('relay-profiles-advanced').onclick=()=>{$('relay-profiles-dialog').close();profileLegacy==='workspace'?showWorkspace():showConnection();};
  $('relay-profile-form').onsubmit=event=>{
    event.preventDefault();cancelRelayProbe();$('relay-profile-error').textContent='';try{
      const target=profileEndpoint($('relay-profile-address').value,$('relay-profile-port').value.trim()),token=$('relay-profile-token').value.trim(),code=$('relay-profile-room').value.trim();
      if(code.length>128||/[\x00-\x1f\x7f]/.test(code))throw new Error('房间号格式无效。');
      if(!token||token.length>4096)throw new Error('请填写有效的 Token。');
      const previous=relayProfiles.find(profile=>profile.endpoint===target&&(profile.code||'')===code&&profileToken(profile)===token);
      const excluded=previous?.excludedRooms||[];
      const socket=new WebSocket(target),probe={target,token,code,socket,timer:0,rooms:[],selected:new Map(excluded.map(code=>[code,false])),restoredSelection:excluded.length>0};relayProbe=probe;
      const live=()=>relayProbe===probe&&$('relay-profiles-dialog').open;
      const failed=message=>{if(!live())return;cancelRelayProbe();$('relay-profile-error').textContent=message;};
      $('relay-profile-form').querySelector('button[type=submit]').disabled=true;$('relay-probe-status').textContent='正在测试连接并获取全部授权房间…';
      probe.timer=setTimeout(()=>failed('测试连接超时，请检查服务器地址、端口和 Token。'),5000);
      socket.onopen=()=>{if(live())socket.send(JSON.stringify({op:'watch',rooms:code?[{code,token}]:[],tokens:[token]}));};
      socket.onmessage=event=>{
        if(!live()||typeof event.data!=='string'||event.data.length>1048576)return;
        try{
          const data=JSON.parse(event.data);if(data.op!=='directory'||!Array.isArray(data.hosts)||data.hosts.length>64)return;
          clearTimeout(probe.timer);$('relay-profile-form').querySelector('button[type=submit]').disabled=false;
          const seen=new Set();probe.rooms=data.hosts.filter(room=>room&&room.online===true&&typeof room.code==='string'&&room.code.length>0&&room.code.length<=128&&!/[\x00-\x1f\x7f]/.test(room.code)&&(!code||room.code===code)&&(data.capabilities?.token_discovery===true?room.token_index===0:!!code&&room.code===code)).filter(room=>{if(seen.has(room.code))return false;seen.add(room.code);return true;}).map(room=>({code:room.code,machine:String(room.machine||'').slice(0,40)}));
          renderRelayProbe(probe);
        }catch{failed('服务器返回了无效的房间列表。');}
      };
      socket.onerror=()=>failed('测试连接失败，请检查加密入口、地址和端口。');socket.onclose=()=>failed('目录连接已关闭，请重新测试连接。');
    }catch(error){$('relay-profile-error').textContent=error.message;}
  };
  $('relay-profile-connect').onclick=()=>{
    const probe=relayProbe;if(!probe)return;
    try{
      const {target,token,code}=probe;
      if(target!==profileEndpoint($('relay-profile-address').value,$('relay-profile-port').value.trim())||token!==$('relay-profile-token').value.trim()||code!==$('relay-profile-room').value.trim())throw new Error('连接信息已修改，请重新测试。');
      if(probe.rooms.length&&!probe.rooms.some(room=>probe.selected.get(room.code)))throw new Error('请至少勾选一个房间。');
      let profile=relayProfiles.find(item=>item.endpoint===target&&(item.code||'')===code&&(profileToken(item)===token||!profileToken(item)));
      if(!profile){if(relayProfiles.length>=32)throw new Error('最多添加 32 组连接。');profile={id:crypto.randomUUID(),endpoint:target,code};relayProfiles.push(profile);}
      profile.excludedRooms=[...probe.selected].filter(([,selected])=>!selected).map(([code])=>code);
      const binding=profileBinding(profile),key='sma.workspace.profile-token.'+profile.id;save(sessionStorage,key,token);save(sessionStorage,key+'.target',binding);
      save(localStorage,key,$('relay-profile-remember').checked?token:'');save(localStorage,key+'.target',$('relay-profile-remember').checked?binding:'');
      save(localStorage,'sma.workspace.profile-remember',$('relay-profile-remember').checked?'on':'off');
      save(localStorage,'sma.workspace.relay-profiles',JSON.stringify(relayProfiles));cancelRelayProbe();prefilledTokenTarget=target;defaultTokenEdited=true;$('relay-profile-error').textContent='';refreshDirectories();renderRelayProfiles();
    }catch(error){$('relay-profile-error').textContent=error.message;}
  };
  $('relay-profile-token').addEventListener('input',()=>{prefilledTokenTarget='';defaultTokenEdited=true;});
  for(const id of ['relay-profile-address','relay-profile-port','relay-profile-room','relay-profile-token'])$(id).addEventListener('input',()=>cancelRelayProbe());
  for(const id of ['relay-profile-address','relay-profile-port'])$(id).addEventListener('input',()=>{
    if(!prefilledTokenTarget)return;let same=false;try{same=profileEndpoint($('relay-profile-address').value,$('relay-profile-port').value)===prefilledTokenTarget;}catch{}
    if(!same){$('relay-profile-token').value='';prefilledTokenTarget='';defaultTokenEdited=true;$('relay-profile-token').title='';}
  });
  document.addEventListener('click',event=>{if(!event.target.closest('.tab-picker'))closeTabPicker();if(!event.target.closest('#app-menu,#app-menu-toggle')||event.target.closest('#app-menu button'))closeAppMenu();if(roomMenu&&!event.target.closest('.room-menu,.workspace-host-edit'))closeRoomMenu();});
  document.addEventListener('keydown',event=>{
    const pickerOpen=!$('tabs-overflow-menu').hidden;
    const menu=roomMenu||(pickerOpen?$('tabs-overflow-menu'):!$('app-menu').hidden?$('app-menu'):null);if(!menu)return;
    if(event.key==='Escape'){event.preventDefault();closeRoomMenu();closeAppMenu();if(pickerOpen)closeTabPicker(true);else $('app-menu-toggle').focus();}
    if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){event.preventDefault();const items=[...menu.querySelectorAll('button:not(:disabled)')];if(!items.length)return;const index=items.indexOf(document.activeElement);items[event.key==='Home'?0:event.key==='End'?items.length-1:(index+(event.key==='ArrowUp'?-1:1)+items.length)%items.length].focus();}
  });
  for(const [command,label] of presets){const option=node('option','',label);option.value=command;$('preset').append(option);}
  $('new-session').onclick=newSession;$('empty-new').onclick=newSession;
  $('split-horizontal').onclick=()=>split('horizontal');$('split-vertical').onclick=()=>split('vertical');
  $('maximize').onclick=()=>{if(!active)return;maximized=!maximized;$('maximize').setAttribute('aria-pressed',String(maximized));renderLayout();};
  $('take-control').onclick=()=>takeControl();$('renderer').value=renderer;
  $('agent-mode').onchange=()=>{const pane=panes.get(active);if(!pane)return;save(localStorage,agentModeKey(pane),$('agent-mode').value);pane.agentRequested=null;updateAgentMode(pane);};
  $('highlight-toggle').setAttribute('aria-pressed',String(highlightEnabled));
  if(!window.NativeUpdates)$('check-updates').onclick=()=>alert('此窗口未接入本机更新服务。请从 Windows 桌面版的启动入口打开，再检查更新。');
  $('highlight-toggle').onclick=()=>{
    highlightEnabled=!highlightEnabled;save(localStorage,'sma.workspace.highlight',highlightEnabled?'on':'off');
    $('highlight-toggle').setAttribute('aria-pressed',String(highlightEnabled));
    for(const pane of panes.values()){pane.highlighter?.setEnabled(highlightEnabled);pane.questions?.setEnabled(highlightEnabled);}
  };
  $('renderer').onchange=()=>{renderer=C.rendererName($('renderer').value);save(localStorage,'sma.workspace.renderer',renderer);for(const pane of panes.values())setRenderer(pane);updateFooter();};
  $('sidebar-toggle').onclick=()=>{if(mobile())$('app').classList.toggle('mobile-sidebar');else {$('app').classList.toggle('sidebar-hidden');$('app').style.gridTemplateColumns=$('app').classList.contains('sidebar-hidden')?'0 minmax(0,1fr)':($('sidebar').style.width||'240px')+' minmax(0,1fr)';}scheduleVisibleFit();};
  const sidebarObserver=new ResizeObserver(()=>{if(!$('app').classList.contains('sidebar-hidden')&&!mobile())$('app').style.gridTemplateColumns=$('sidebar').getBoundingClientRect().width+'px minmax(0,1fr)';});sidebarObserver.observe($('sidebar'));
  $('ctrl-key').onclick=()=>{setCtrl(!ctrl);panes.get(active)?.term.focus();};
  const keys={escape:'\x1b',tab:'\t',up:'\x1b[A',down:'\x1b[B',left:'\x1b[D',right:'\x1b[C'};
  for(const b of document.querySelectorAll('[data-key]')){b.onpointerdown=e=>e.preventDefault();b.onclick=()=>{const term=panes.get(active)?.term;input(active,C.cursorKey?.(term,b.dataset.key)||keys[b.dataset.key]);term?.focus();};}
  addEventListener('resize',()=>{if(mobile())$('app').style.gridTemplateColumns='';renderLayout();});
  addEventListener('keydown',e=>{if(e.ctrlKey&&e.altKey&&e.key.toLowerCase()==='n'){e.preventDefault();newSession();}if(e.key==='Escape')$('app').classList.remove('mobile-sidebar');});
  function relayURL(value){return C.socketURL(value,location.origin);}
  function accountTransport(value){
    const url=new URL(relayURL(value)),local=['localhost','[::1]','::1'].includes(url.hostname)||/^127(?:\.\d{1,3}){3}$/.test(url.hostname);
    if(url.protocol!=='wss:'&&!local)throw new Error('请先为中继配置 HTTPS/WSS；公网 ws 不可发送密码或账号会话。');
    return url.toString();
  }
  function accountFor(value){
    try{const account=relayAccounts.get(accountTransport(value));return account?.session&&account.expires*1000>Date.now()?account:null;}catch{return null;}
  }
  function saveRelayAccounts(){
    save(sessionStorage,'sma.workspace.relay-accounts',JSON.stringify([...relayAccounts.values()].filter(account=>accountFor(account.url)===account).map(({url,session,expires,account})=>({url,session,expires,account}))));
  }
  function loadRelayAccounts(){
    try{const stored=JSON.parse(read(sessionStorage,'sma.workspace.relay-accounts')||'[]');if(!Array.isArray(stored))return;
      for(const item of stored.slice(0,32)){
        if(!item||typeof item.url!=='string'||typeof item.session!=='string'||!item.session||item.session.length>4096||!Number.isFinite(item.expires)||item.expires*1000<=Date.now()||typeof item.account?.id!=='string')continue;
        let url;try{url=accountTransport(item.url);}catch{continue;}if(url!==item.url)continue;
        const account={url,session:item.session,expires:item.expires,account:{id:item.account.id,email:String(item.account.email||'').slice(0,254)},generation:0,ws:null,retry:0,retryTimer:0,expiryTimer:0,hosts:[],phase:'connecting',revision:0};
        relayAccounts.set(url,account);scheduleAccountExpiry(account);
      }
    }catch{}saveRelayAccounts();
  }
  function scheduleAccountExpiry(account){
    clearTimeout(account.expiryTimer);account.expiryTimer=setTimeout(()=>{
      if(relayAccounts.get(account.url)!==account)return;
      if(account.expires*1000<=Date.now())forgetAccount(account,'账号会话已过期，请重新登录。');else scheduleAccountExpiry(account);
    },Math.min(2147483647,Math.max(1,account.expires*1000-Date.now())));
  }
  function stopAccountDirectory(account){
    account.generation++;clearTimeout(account.retryTimer);if(account.ws){account.ws.onclose=null;account.ws.close();account.ws=null;}
  }
  function stopAccountHost(host,message){
    stopHost(host);host.phase='auth-failed';host.error=message;host.catalog=[];host.groups=[];host.controls={};host.identity={};
    for(const [id,pane] of [...panes])if(pane.host===host)removeView(id,false);
    aggregateCatalog();renderTree();renderTabs();renderLayout();renderHosts();updateConnection();updateFooter();
  }
  function forgetAccount(account,message){
    if(relayAccounts.get(account.url)!==account)return;
    stopAccountDirectory(account);clearTimeout(account.expiryTimer);relayAccounts.delete(account.url);account.hosts=[];account.session='';saveRelayAccounts();
    for(const host of hosts.values())if(host.config.authMode==='account'&&relayURL(host.config.relay)===account.url)stopAccountHost(host,message);
    if(workspaceAuthorization?.url===account.url)clearAccountSelection('workspace');
    if(connectionAuthorization?.url===account.url)clearAccountSelection('connection');
    renderDiscovery();renderAccountPanel();
    $('relay-account-status').textContent=message;
  }
  function connectAccountDirectory(account){
    if(accountFor(account.url)!==account)return;
    stopAccountDirectory(account);const generation=account.generation,session=account.session;
    const live=()=>relayAccounts.get(account.url)===account&&account.generation===generation&&accountFor(account.url)===account;
    account.phase='connecting';account.hosts=[];account.revision=0;
    const socket=new WebSocket(accountTransport(account.url));account.ws=socket;
    socket.onopen=()=>{if(live())socket.send(JSON.stringify({op:'watch',session}));};
    socket.onmessage=event=>{
      if(!live()||typeof event.data!=='string'||event.data.length>1048576)return;
      try{
        const data=JSON.parse(event.data);
        if(data.op!=='directory'||!data.capabilities?.relay_accounts||!Array.isArray(data.hosts)||data.hosts.length>64||!Number.isSafeInteger(data.revision)||data.revision<account.revision)return;
        account.hosts=data.hosts.filter(host=>host&&typeof host.code==='string'&&host.code.length<=128&&typeof host.endpoint==='string'&&accountTransport(host.endpoint)===account.url).map(host=>({code:host.code,endpoint:account.url,online:host.online===true,
          ...(host.online===true?{machine:String(host.machine||'').slice(0,40),project:String(host.project||'').slice(0,40)}:{})}));
        account.revision=data.revision;account.phase='connected';account.retry=0;
        const granted=new Set(account.hosts.map(host=>host.code));
        if(workspaceAuthorization?.account===account&&!granted.has(workspaceAuthorization.code))clearAccountSelection('workspace');
        if(connectionAuthorization?.account===account){const code=JSON.parse(connectionAuthorization.target)[1];if(!granted.has(code))clearAccountSelection('connection');}
        for(const host of hosts.values())if(host.config.authMode==='account'&&relayURL(host.config.relay)===account.url&&!granted.has(host.config.code))stopAccountHost(host,'此电脑的账号授权已撤销。');
        renderDiscovery();renderAccountPanel();
      }catch{account.hosts=[];account.phase='unsupported';renderDiscovery();renderAccountPanel();}
    };
    socket.onerror=()=>{};
    socket.onclose=event=>{
      if(relayAccounts.get(account.url)!==account||account.generation!==generation)return;
      if(event.code===4001||!accountFor(account.url)){forgetAccount(account,'账号会话已失效，请重新登录。');return;}
      account.hosts=[];account.phase=event.code===4000?'unsupported':'offline';renderDiscovery();renderAccountPanel();
      if(account.phase!=='unsupported')account.retryTimer=setTimeout(()=>{if(live())connectAccountDirectory(account);},C.retryDelay(account.retry++));
    };
  }
  function clearAccountSelection(prefix){
    const auth=$(prefix==='workspace'?'workspace-auth':'connection-auth');
    const wasAccount=auth.value==='account';
    if(prefix==='workspace')workspaceAuthorization=null;else connectionAuthorization=null;
    if(wasAccount){auth.value='manual';$(prefix==='workspace'?'workspace-token':'access-token').value='';}
    renderAuthField(prefix);
  }
  function authorizedSelection(prefix,settings){
    try{
      const selection=prefix==='workspace'?workspaceAuthorization:connectionAuthorization,account=accountFor(settings.relay);
      return !!account&&!!selection&&selection.account===account&&selection.target===endpoint(settings)&&account.hosts.some(host=>host.code===settings.code&&host.online);
    }catch{return false;}
  }
  function renderAuthField(prefix){
    const workspace=prefix==='workspace',auth=$(workspace?'workspace-auth':'connection-auth'),input=$(workspace?'workspace-token':'access-token'),remember=$(workspace?'workspace-remember':'remember-token');
    const settings={relay:$(workspace?'workspace-relay':'relay-url').value,code:$(workspace?'workspace-code':'relay-code').value},account=accountFor(settings.relay),using=auth.value==='account';
    const local=tokenlessLocal(settings);
    input.closest('label').hidden=local;remember.closest('label').hidden=local;auth.closest('label').hidden=local;
    if(!workspace)$('relay-code').closest('label').hidden=local;
    if(local){$(workspace?'workspace-account-status':'connection-account-status').textContent='本机 127.0.0.1 直连无需 Token 和房间号。';return;}
    input.readOnly=using;remember.disabled=using;if(using){input.value=account?'••••••••':'';remember.checked=false;}else if(input.value==='••••••••')input.value='';
    $(workspace?'workspace-account-status':'connection-account-status').textContent=using?(account?'中继账号：'+account.account.email+'；凭据仅用于此中继，不作为电脑 Token。':'请先登录此中继，并选择账号授权的电脑。'):'手动 Token 只用于所填电脑；账号登录单独管理。';
  }
  $('workspace-auth').onchange=()=>{if($('workspace-auth').value==='manual')workspaceAuthorization=null;renderAuthField('workspace');};
  $('connection-auth').onchange=()=>{
    if($('connection-auth').value==='account'){
      const settings={relay:$('relay-url').value,code:$('relay-code').value},account=accountFor(settings.relay);
      connectionAuthorization=account&&account.hosts.some(host=>host.code===settings.code&&host.online)?{target:endpoint(settings),url:account.url,account}:null;
    }else connectionAuthorization=null;
    renderAuthField('connection');
  };
  function renderAccountPanel(){
    let account=null,message='';try{const url=accountTransport($('account-relay-url').value||$('default-relay').value);account=accountFor(url);message=account?'已登录 '+account.account.email+' · '+(account.phase==='connected'?'授权电脑目录已连接':account.phase):'登录后仅显示管理员授予此账号的电脑。';}catch(error){message=error.message;}
    $('relay-account-status').textContent=message;$('relay-account-logout').disabled=!account;$('relay-account-computers').disabled=!account?.hosts.some(host=>host.online);
    if(account&&!$('relay-account-email').value)$('relay-account-email').value=account.account.email;
  }
  function cancelAccountLogin(){
    accountLoginGeneration++;if(accountLogin){clearTimeout(accountLogin.timer);accountLogin.socket.onclose=null;accountLogin.socket.close();accountLogin=null;}
    $('relay-account-password').value='';$('relay-account-login').disabled=false;
  }
  $('account-relay-url').addEventListener('input',()=>{cancelAccountLogin();renderAccountPanel();});
  $('connection-dialog').addEventListener('cancel',()=>{cancelAccountLogin();returnToWorkspace=false;});
  for(const id of ['relay-account-email','relay-account-password'])$(id).addEventListener('keydown',event=>{
    if(event.key==='Enter'){event.preventDefault();$('relay-account-login').click();}
  });
  let returnToWorkspace=false;
  $('workspace-account-login').onclick=()=>{
    const relay=$('workspace-relay').value;returnToWorkspace=true;$('workspace-dialog').close();showConnection();$('account-relay-url').value=relay||$('default-relay').value;renderAccountPanel();$('relay-account-email').focus();
  };
  $('relay-account-computers').onclick=()=>{returnToWorkspace=false;$('connection-dialog').close();showWorkspace();};
  $('relay-account-login').onclick=()=>{
    const email=$('relay-account-email').value.trim(),password=$('relay-account-password').value;let url;
    try{url=accountTransport($('account-relay-url').value||$('default-relay').value);}catch(error){$('relay-account-status').textContent=error.message;$('relay-account-password').value='';return;}
    if(!email||!password){$('relay-account-status').textContent='请填写中继账号邮箱和密码。';return;}
    if(!relayAccounts.has(url)&&relayAccounts.size>=32){$('relay-account-status').textContent='最多登录 32 个中继。';return;}
    cancelAccountLogin();const generation=accountLoginGeneration,socket=new WebSocket(url);
    const request={socket,url,generation,timer:0};accountLogin=request;$('relay-account-login').disabled=true;$('relay-account-status').textContent='正在登录…';
    const live=()=>accountLogin===request&&accountLoginGeneration===generation&&relayURL($('account-relay-url').value||$('default-relay').value)===url;
    const finish=message=>{const current=live();clearTimeout(request.timer);if(accountLogin===request){accountLogin=null;$('relay-account-login').disabled=false;}socket.onclose=null;socket.close();if(current)$('relay-account-status').textContent=message;};
    request.timer=setTimeout(()=>finish('账号登录超时，请检查中继地址与加密配置。'),5000);
    socket.onopen=()=>{if(live())socket.send(JSON.stringify({op:'relay_login',email,password}));else finish('');};
    socket.onmessage=event=>{
      if(!live()){finish('');return;}if(typeof event.data!=='string'||event.data.length>1048576)return;
      try{
        const data=JSON.parse(event.data);if(data.op!=='relay_auth')return;
        if(data.error){finish('登录失败：'+String(data.error));return;}
        if(typeof data.session!=='string'||!data.session||data.session.length>4096||!Number.isFinite(data.expires)||data.expires*1000<=Date.now()||typeof data.account?.id!=='string'){finish('中继返回了无效账号会话。');return;}
        finish('登录成功，正在获取授权电脑。');const previous=relayAccounts.get(url);
        if(previous){
          if(previous.account.id!==data.account.id)forgetAccount(previous,'已切换中继账号；旧账号视图已清除。');
          else{stopAccountDirectory(previous);clearTimeout(previous.expiryTimer);}
        }
        const account={url,session:data.session,expires:data.expires,account:{id:data.account.id,email:String(data.account.email||email).slice(0,254)},generation:0,ws:null,retry:0,retryTimer:0,expiryTimer:0,hosts:[],phase:'connecting',revision:0};
        relayAccounts.set(url,account);saveRelayAccounts();scheduleAccountExpiry(account);connectAccountDirectory(account);renderAccountPanel();
        for(const host of hosts.values())if(host.config.authMode==='account'&&relayURL(host.config.relay)===url)connect(host);
        if(returnToWorkspace){returnToWorkspace=false;$('connection-dialog').close();showWorkspace(editingHost);}
      }catch{finish('无法解析中继账号响应。');}
    };
    socket.onerror=()=>finish('账号连接失败，请检查 HTTPS/WSS 和证书。');socket.onclose=()=>finish('账号连接已关闭，登录未完成。');
  };
  $('relay-account-logout').onclick=()=>{
    const account=accountFor($('account-relay-url').value);if(!account)return;
    const url=accountTransport(account.url),session=account.session;forgetAccount(account,'已注销；相关账号连接已停止。');
    const socket=new WebSocket(url),timer=setTimeout(()=>socket.close(),2000);
    socket.onopen=()=>{socket.send(JSON.stringify({op:'relay_logout',session}));};socket.onmessage=()=>socket.close();socket.onclose=()=>clearTimeout(timer);socket.onerror=()=>socket.close();
  };
  function stopDirectory(directory){
    directory.generation++;clearTimeout(directory.retryTimer);
    if(directory.ws){directory.ws.onclose=null;directory.ws.close();directory.ws=null;}
  }
  let lastRelayRefresh=-Infinity;
  function refreshRelayCatalogs(){
    const now=performance.now();if(now-lastRelayRefresh<1000)return;lastRelayRefresh=now;
    refreshDirectories();
    for(const directory of directories.values()){
      if(directory.ws?.readyState===WebSocket.OPEN){
        directory.ws.send(JSON.stringify({op:'watch',rooms:directory.rooms,tokens:directory.tokens}));
      }else if(directory.ws?.readyState!==WebSocket.CONNECTING){
        directory.revision=0;connectDirectory(directory);
      }
    }
    for(const account of relayAccounts.values()){
      if(account.ws?.readyState===WebSocket.OPEN){
        account.ws.send(JSON.stringify({op:'watch',session:account.session}));
      }else if(account.ws?.readyState!==WebSocket.CONNECTING)connectAccountDirectory(account);
    }
    for(const host of hosts.values()){
      if(host.connected){
        const sessions=openTabs.map(id=>panes.get(id)).filter(pane=>pane?.host===host&&sessionInfo(pane.sid)).map(pane=>pane.raw);
        send(owner(host),16,{sessions});
      }else if((host.config.token||accountFor(host.config.relay))&&(!host.ws||host.ws.readyState>=WebSocket.CLOSING))connect(host);
    }
    const count=directories.size+relayAccounts.size;
    status(count?'已请求刷新 '+count+' 个中继的授权房间及窗口列表。':'尚未配置可用中继，请先连接工作区。');
  }
  function refreshDirectories(){
    const wanted=new Map();
    // Invalid saved scope bindings must not reopen a broader implicit home scope.
    const explicitScope=(url,token)=>relayProfiles.some(profile=>profile.endpoint===url&&(profileToken(profile)===token||[sessionStorage,localStorage].some(storage=>read(storage,'sma.workspace.profile-token.'+profile.id)===token)));
    for(const profile of relayProfiles){const token=profileToken(profile);if(!token)continue;if(!wanted.has(profile.endpoint))wanted.set(profile.endpoint,[]);wanted.get(profile.endpoint).push({code:profile.code||'',token,onlyRoom:!!profile.code,excludedRooms:profile.excludedRooms||[]});}
    for(const host of [...hosts.values()].sort((a,b)=>Number(b.connected)-Number(a.connected)))if(!host.autoDirectory&&host.config.authMode!=='account'&&host.config.code&&host.config.token){
      const url=JSON.parse(endpoint(host.config))[0];if(explicitScope(url,host.config.token))continue;if(!wanted.has(url))wanted.set(url,[]);
      const rooms=wanted.get(url);if(!rooms.some(room=>room.code===host.config.code)&&rooms.length<64)rooms.push({code:host.config.code,token:host.config.token});
    }
    const local=hosts.get('local');
    if(homeRelayAccess()&&local?.config.token)for(const relay of homeRelays){
      if(typeof relay.url!=='string')continue;
      const code=typeof relay.code==='string'?relay.code:'';
      let url;try{url=JSON.parse(endpoint({relay:relay.url,code}))[0];}catch{continue;}
      if(explicitScope(url,local.config.token))continue;
      if(!wanted.has(url))wanted.set(url,[]);const rooms=wanted.get(url);
      if(!rooms.some(room=>room.code===code&&room.token===local.config.token)&&rooms.length<64)rooms.push({code,token:local.config.token});
    }
    for(const [url,directory] of directories)if(!wanted.has(url)){stopDirectory(directory);syncDiscoveredHosts(directory,[]);directories.delete(url);}
    for(const [url,rooms] of wanted){
      let directory=directories.get(url);
      if(!directory){directory={url,rooms:[],generation:0,ws:null,retry:0,retryTimer:0,signature:'',hosts:[],phase:'connecting',revision:0};directories.set(url,directory);}
      const signature=JSON.stringify(rooms);if(directory.signature===signature)continue;
      const oldTokens=directory.tokens||[],tokens=[...new Set(rooms.map(room=>room.token))].slice(0,32);
      const retained=(directory.hosts||[]).filter(host=>Number.isInteger(host.token_index)&&tokens.includes(oldTokens[host.token_index])&&rooms.some(scope=>scope.token===oldTokens[host.token_index]&&roomAllowed(scope,host.code))).map(host=>({...host,token_index:tokens.indexOf(oldTokens[host.token_index])}));
      directory.availableHosts=(directory.availableHosts||directory.hosts||[]).filter(host=>tokens.includes(oldTokens[host.token_index])&&rooms.some(scope=>scope.token===oldTokens[host.token_index]&&(!scope.onlyRoom||scope.code===host.code))).map(host=>({...host,token_index:tokens.indexOf(oldTokens[host.token_index])}));
      directory.rooms=rooms.filter((room,index)=>room.code&&rooms.findIndex(item=>item.code===room.code)===index).map(({code,token})=>({code,token}));directory.scopes=rooms;directory.tokens=tokens;directory.signature=signature;directory.hosts=retained;directory.selfRooms??=new Set();directory.revision=0;
      syncDiscoveredHosts(directory,retained);
      // Close before replacing credentials, so stale authorized metadata cannot be displayed.
      connectDirectory(directory);
    }
    renderDiscovery();
  }
  function connectDirectory(directory){
    stopDirectory(directory);const generation=directory.generation;directory.phase='connecting';
    const live=()=>directories.get(directory.url)===directory&&directory.generation===generation;
    const socket=new WebSocket(directory.url);directory.ws=socket;
    socket.onopen=()=>{if(live())socket.send(JSON.stringify({op:'watch',rooms:directory.rooms,tokens:directory.tokens}));};
    socket.onmessage=event=>{
      if(!live()||typeof event.data!=='string'||event.data.length>1048576)return;
      try{
        const data=JSON.parse(event.data);
        if(data.op!=='directory'||!Array.isArray(data.hosts)||data.hosts.length>64||!Number.isSafeInteger(data.revision)||data.revision<directory.revision)return;
        const known=new Set(directory.rooms.map(room=>room.code));
        const discovery=data.capabilities?.token_discovery===true;
        directory.hosts=data.hosts.filter(host=>typeof host.code==='string'&&host.code.length>0&&host.code.length<=128&&(known.has(host.code)||(discovery&&Number.isInteger(host.token_index)&&host.token_index>=0&&host.token_index<directory.tokens.length))).map(host=>({code:host.code,online:host.online===true,
          ...(discovery&&Number.isInteger(host.token_index)&&host.token_index>=0&&host.token_index<directory.tokens.length?{token_index:host.token_index}:{}),
          ...(host.online===true?{machine:String(host.machine||'').slice(0,40),project:String(host.project||'').slice(0,40)}:{})}));
        directory.availableHosts=directory.hosts.map(host=>Number.isInteger(host.token_index)?host:{...host,token_index:directory.tokens.indexOf(directory.rooms.find(room=>room.code===host.code)?.token)}).filter(host=>directory.scopes.some(scope=>scope.token===directory.tokens[host.token_index]&&(!scope.onlyRoom||scope.code===host.code)));
        directory.hosts=directory.availableHosts.filter(host=>directory.scopes.some(scope=>scope.token===directory.tokens[host.token_index]&&roomAllowed(scope,host.code)));
        directory.revision=data.revision;directory.phase='connected';directory.retry=0;syncDiscoveredHosts(directory,directory.hosts);renderDiscovery();
      }catch{directory.phase='unsupported';directory.hosts=[];directory.availableHosts=[];syncDiscoveredHosts(directory,[]);renderDiscovery();}
    };
    socket.onerror=()=>{};
    socket.onclose=event=>{
      if(!live())return;directory.hosts=[];directory.availableHosts=[];directory.phase=[4000,4001,4003].includes(event.code)?'unsupported':'offline';syncDiscoveredHosts(directory,[]);renderDiscovery();
      if(directory.phase!=='unsupported')directory.retryTimer=setTimeout(()=>{if(live())connectDirectory(directory);},C.retryDelay(directory.retry++));
    };
  }
  function homeRelayAccess(){
    const local=hosts.get('local');if(!local||local.config.code||local.config.authMode==='account')return false;
    try{return ['localhost','127.0.0.1','[::1]','::1'].includes(new URL(C.socketURL(local.config.relay,location.origin)).hostname);}catch{return false;}
  }
  function isLocalRelayCopy(host){
    // Frozen hosts omitted their room code. Recognize the same authenticated
    // terminal inventory rather than treating a matching computer name as proof.
    const local=hosts.get('local');if(!homeRelayAccess()||!local?.connected||!host.catalog.length)return false;
    if(!homeRelays.some(relay=>{try{return JSON.parse(endpoint({relay:relay.url,code:''}))[0]===host.autoDirectory;}catch{return false;}}))return false;
    if(!local.identity.machine||host.identity.machine!==local.identity.machine||host.identity.project!==local.identity.project)return false;
    const inventory=value=>JSON.stringify(value.map(win=>[win.id,(win.sessions||[]).map(session=>session.id).sort()]).sort((a,b)=>a[0].localeCompare(b[0])));
    return inventory(host.catalog)===inventory(local.catalog);
  }
  function syncDiscoveredHosts(directory,items){
    const wanted=new Set();let changed=false;
    for(const item of items){
      if(!item.online||directory.selfRooms?.has(item.code)||!Number.isInteger(item.token_index)||!directory.tokens?.[item.token_index])continue;
      const token=directory.tokens[item.token_index],target=JSON.stringify([directory.url,item.code]);
      if([...hosts.values()].some(host=>!host.autoDirectory&&endpoint(host.config)===target))continue;
      if(homeRelayAccess()&&homeRelays.some(relay=>{try{return endpoint({relay:relay.url,code:relay.code})===target;}catch{return false;}}))continue;
      const id='discovered:'+target;wanted.add(id);let host=hosts.get(id);
      if(!host){if(hosts.size>=64)continue;host=new HostConnection(id,item.machine||item.code,{relay:directory.url,code:item.code,token,authMode:'manual'});host.autoDirectory=directory.url;hosts.set(id,host);changed=true;connect(host);}
      else if(host.config.token!==token||host.phase==='offline'){host.config.token=token;connect(host);}
      host.name=item.machine||item.code;host.identity={machine:item.machine,project:item.project};
    }
    for(const host of [...hosts.values()])if(host.autoDirectory===directory.url&&!wanted.has(host.id)){
      detachHost(host);changed=true;if(selectedHost===host.id){selectedHost='local';save(sessionStorage,'sma.workspace.activeHost','local');}
    }
    if(changed){aggregateCatalog();renderTree();renderTabs();renderLayout();subscribe();updateConnection();updateFooter();}
  }
  function renderDiscovery(){
    renderRelayProfiles();
    const list=$('workspace-known');if(!list)return;const selected=list.value;list.replaceChildren();
    const placeholder=node('option','','选择已授权电脑');placeholder.value='';list.append(placeholder);
    for(const account of relayAccounts.values())if(accountFor(account.url)===account)for(const host of account.hosts)if(host.online){
      const option=node('option','',[account.account.email,host.project,host.machine,host.code].filter(Boolean).join(' / '));option.value=JSON.stringify(['account',account.url,host.code]);list.append(option);
    }
    for(const directory of directories.values())for(const host of directory.hosts)if(host.online){
      const option=node('option','',[host.project,host.machine,host.code].filter(Boolean).join(' / '));option.value=JSON.stringify([directory.url,host.code]);list.append(option);
    }
    if([...list.options].some(option=>option.value===selected))list.value=selected;
    $('workspace-directory-status').textContent=relayAccounts.size?'已登录账号的授权在线电脑会自动列出；手动 Token 目录独立保留。':[...directories.values()].some(directory=>directory.phase==='connected')?'仅显示此设备已授权的已知电脑；不会公开其他房间。':directories.size?'目录暂不可用；各电脑的终端连接独立工作。':'登录中继账号自动发现授权电脑，或手填房间码和 Token。';
  }
  $('workspace-known').onchange=()=>{
    const value=$('workspace-known').value;if(!value)return;
    const choice=JSON.parse(value);
    if(choice[0]==='account'){
      const [,url,code]=choice,account=accountFor(url),host=account?.hosts.find(host=>host.code===code&&host.online);if(!host)return;
      $('workspace-name').value=host.machine||host.project||'电脑 '+code;$('workspace-relay').value=host.endpoint;$('workspace-code').value=host.code;
      $('workspace-auth').value='account';workspaceAuthorization={target:endpoint({relay:host.endpoint,code}),url,code,account};renderAuthField('workspace');
      $('workspace-verification').textContent='账号授权在线电脑：'+[host.machine,host.project].filter(Boolean).join(' / ');return;
    }
    const [url,code]=choice,directory=directories.get(url),host=directory?.hosts.find(host=>host.code===code&&host.online);
    const enrolled=[...hosts.values()].find(host=>host.config.authMode!=='account'&&host.config.code===code&&relayURL(host.config.relay)===url);
    if(!host||!enrolled)return;
    if(enrolled.autoDirectory){$('workspace-dialog').close();switchHost(enrolled.id);return;}
    showWorkspace(enrolled.id);$('workspace-verification').textContent='已授权在线电脑：'+[host.machine,host.project].filter(Boolean).join(' / ');
  };
  $('workspace-verify').onclick=()=>{
    const code=$('workspace-code').value.trim(),token=$('workspace-token').value.trim(),relay=$('workspace-relay').value.trim();let url;
    if($('workspace-auth').value==='account'){$('workspace-verification').textContent=authorizedSelection('workspace',{relay,code})?'账号已授权此在线电脑。':'请登录并重新选择授权电脑。';return;}
    try{url=JSON.parse(endpoint({relay,code}))[0];}catch(error){$('workspace-verification').textContent=error.message;return;}
    const directory=directories.get(url),known=directory?.rooms.find(room=>room.code===code&&room.token===token),identity=known&&directory.hosts.find(host=>host.code===code&&host.online);
    if(identity){$('workspace-verification').textContent='已验证在线电脑：'+[identity.machine,identity.project].filter(Boolean).join(' / ');return;}
    // An explicit verification can authenticate one candidate; it never asks for a roster.
    if(!code||!token){$('workspace-verification').textContent='请填写此电脑的房间码和 Token。';return;}
    const request=JSON.stringify([url,code,token]),socket=new WebSocket(url);let complete=false;
    $('workspace-verification').textContent='正在验证已知电脑…';
    const finish=message=>{if(complete)return;complete=true;clearTimeout(timer);socket.close();
      let current='';try{current=JSON.stringify([JSON.parse(endpoint({relay:$('workspace-relay').value.trim(),code:$('workspace-code').value.trim()}))[0],$('workspace-code').value.trim(),$('workspace-token').value.trim()]);}catch{}
      if($('workspace-dialog').open&&request===current)$('workspace-verification').textContent=message;
    };
    const timer=setTimeout(()=>finish('未能验证；电脑未上线、凭据无效或中继不支持目录。'),2500);
    socket.onopen=()=>socket.send(JSON.stringify({op:'watch',rooms:[{code,token}]}));
    socket.onmessage=event=>{try{const data=JSON.parse(event.data),host=data.op==='directory'&&data.hosts?.find(host=>host.code===code&&host.online);finish(host?'已验证在线电脑：'+[String(host.machine||'').slice(0,40),String(host.project||'').slice(0,40)].filter(Boolean).join(' / '):'无法验证此电脑的在线身份。');}catch{finish('中继不支持已知电脑目录。');}};
    socket.onerror=()=>finish('无法连接电脑目录。');socket.onclose=()=>finish('无法验证此电脑的在线身份。');
  };
  const meterTimer=setInterval(()=>{for(const host of hosts.values())host.rates=host.meter.sample(performance.now());updateConnection();renderHosts();},1000);
  const activityTimer=setInterval(updateSessionDots,300);
  addEventListener('beforeunload',()=>{closeDaemonMonitor();cancelRelayProbe();clearInterval(meterTimer);clearInterval(activityTimer);for(const host of hosts.values())stopHost(host);for(const directory of directories.values())stopDirectory(directory);cancelAccountLogin();for(const account of relayAccounts.values()){stopAccountDirectory(account);clearTimeout(account.expiryTimer);}});
  // Launch secrets are consumed once. Fragments never reach HTTP access logs.
  if(params.has('token')||launch.has('token')){
    save(sessionStorage,'sma.workspace.token',config.token);const clean=new URL(location.href);clean.searchParams.delete('token');launch.delete('token');clean.hash=launch.toString();history.replaceState(null,'',clean);
  }
  window.smaWorkspace={get activeSession(){return active;},get terminal(){return panes.get(active)?.term;},get panes(){return panes;},metrics,
    get activePane(){return panes.get(active);},get activeHost(){return currentHost();},get connections(){return hosts;},get directories(){return directories;},get relayAccounts(){return relayAccounts;},
    get isComposing(){return [...panes.values()].some(pane=>pane.composing);},
    captureUIState:()=>({selectedHost,active,tabs:[...openTabs],layout,renderer}),
    pauseForUIUpdate:()=>{
      const states=[...panes.values()].map(pane=>({pane,disabled:pane.term.options.disableStdin,readonly:pane.term.textarea.readOnly}));
      for(const {pane} of states){pane.term.options.disableStdin=true;pane.term.textarea.readOnly=true;}
      return ()=>{for(const {pane,disabled,readonly} of states)if(livePane(pane)){pane.term.options.disableStdin=disabled;pane.term.textarea.readOnly=readonly;}};
    },
    get effectiveRenderer(){return panes.get(active)?.renderer;},get connected(){return !!currentHost()?.connected;},get layout(){return layout;},
    openSession:(id,takeover=true)=>openView(sessionInfo(id)?id:key(currentHost(),id),takeover),openHostSession:(hostID,id,takeover=true)=>{const host=hosts.get(hostID);if(host)openView(key(host,id),takeover);},switchHost,split,
    closeView:id=>removeView(panes.has(id)?id:key(currentHost(),id)),takeControl,redraw:()=>redrawPane(panes.get(active)),setRenderer:value=>{$('renderer').value=C.rendererName(value);$('renderer').onchange();},
    get controls(){return Object.fromEntries([...hosts.values()].flatMap(host=>Object.entries(host.controls).map(([id,value])=>[key(host,id),value])));}};
  function bootstrap(){
    loadRelayAccounts();
    if(!explicitToken){
      const established={relay:read(localStorage,'sma.workspace.relay'),code:read(localStorage,'sma.workspace.code')};
      const target=endpoint(homeConfig),bound=[sessionStorage,localStorage].some(storage=>read(storage,'sma.workspace.token.target')===target);
      homeConfig.token=bound||endpoint(established)===target?boundToken('local',homeConfig):'';config.token=homeConfig.token;
    }
    else save(sessionStorage,'sma.workspace.token.target',endpoint(homeConfig));
    const home=new HostConnection('local',homeConfig.code?'当前连接':'本机',homeConfig);hosts.set(home.id,home);
    for(const saved of savedHosts)hosts.set(saved.id,new HostConnection(saved.id,saved.name,{relay:saved.relay,code:saved.code,token:saved.authMode==='account'?'':boundToken(saved.id,saved),authMode:saved.authMode}));
    const selected=restoredUI?.selectedHost||read(sessionStorage,'sma.workspace.activeHost');selectedHost=hosts.has(selected)?selected:'local';
    for(const host of hosts.values())connect(host);for(const account of relayAccounts.values())connectAccountDirectory(account);refreshDirectories();renderTree();updateFooter();
    if(!tokenlessLocal(config)&&!config.token&&!config.code)setTimeout(()=>{if(!userConnectedOnce)showRelayProfiles();},1500);
  }
  bootstrap();
  window.NativeUpdates?.uiReady();
})();
