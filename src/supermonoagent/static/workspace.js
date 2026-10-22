/* Concurrent host directories and byte-preserving, owner-bound xterm views. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const C = window.TerminalClient;
  const encoder = new TextEncoder(), decoder = new TextDecoder();
  const params = new URLSearchParams(location.search);
  const launch = new URLSearchParams(location.hash.slice(1));
  const explicitToken=launch.has('token')||params.has('token');
  const read = (storage, key) => { try { return storage.getItem(key) || ''; } catch { return ''; } };
  const save = (storage, key, value) => { try { value ? storage.setItem(key, value) : storage.removeItem(key); } catch {} };
  let config = {relay: params.get('direct')==='1'?'':params.get('relay') || read(localStorage, 'sma.workspace.relay'),
    code: params.get('direct')==='1'?'':params.get('code') || read(localStorage, 'sma.workspace.code'),
    token: launch.get('token') || params.get('token') || read(sessionStorage, 'sma.workspace.token') || read(localStorage, 'sma.workspace.token'),
    authMode:explicitToken||params.get('direct')==='1'?'manual':read(localStorage,'sma.workspace.auth-mode')||'manual'};
  let renderer = C.rendererName(params.get('renderer') || read(localStorage, 'sma.workspace.renderer'));
  let catalog = [], panes = new Map(), openTabs = [], active = '', layout = null;
  let userConnectedOnce = false, maximized = false, ctrl = false;
  let renameWindow = null, terminateSession = null, connectionOwner = null;
  let homeConfig={...config}, savedHosts=loadHosts(), selectedHost='local', editingHost='', homeRelays=[];
  const hosts=new Map(), directories=new Map(), relayAccounts=new Map();
  let accountLogin=null,accountLoginGeneration=0,workspaceAuthorization=null,connectionAuthorization=null;
  const mobile = () => matchMedia('(max-width:600px)').matches;
  const metrics = {socketsOpened:0, receivedBytes:0, transmittedBytes:0, outputFrames:0, snapshots:0,
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
        connected:false,supported:false,helloSent:false,catalog:[],groups:[],controls:{},capabilities:{},identity:{},activity:new Map(),
        selectedGroup:'',initialCatalog:true,nextSplit:'',sessionsBeforeOpen:new Set(),subscriptionSignature:'',
        phase:'offline',error:'',pingSentAt:null,meter:new C.ConnectionMeter(performance.now()),rates:{tx:0,rx:0}});
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
    $('host-name').textContent=[host.identity.machine,host.identity.project].filter(Boolean).join(' / ')||host.name;
    notice(host.error);
  }
  function ping(host){host.pingSentAt=performance.now();send(owner(host),10,encoder.encode(String(host.pingSentAt)));}
  function handshake(host){
    if(host.helloSent||host.ws?.readyState!==WebSocket.OPEN)return;
    host.helloSent=true;socketSend(host,C.pack(1,encoder.encode(JSON.stringify({...(host.config.authMode==='account'?{}:{token:host.config.token}),multiplex:true,reliable:true,
      channel:host.id==='local'?params.get('channel')||undefined:undefined}))));
  }
  function stopHost(host){
    host.generation++;clearTimeout(host.retryTimer);clearInterval(host.pingTimer);
    if(host.ws){host.ws.onclose=null;host.ws.close();host.ws=null;}host.connected=false;host.supported=false;host.activity.clear();updateSessionDots();
    for(const pane of panes.values())if(pane.host===host){pane.ready=false;pane.term.options.disableStdin=true;clearTimeout(pane.redrawTimer);clearTimeout(pane.resizeRequest?.timer);pane.resizeRequest=null;cancelAnimationFrame(pane.followFrame);pane.followFrame=0;cancelAnimationFrame(pane.scrollIntentFrame);pane.scrollIntentFrame=0;}
  }
  function connect(host){
    stopHost(host);const context=owner(host);
    host.helloSent=false;host.subscriptionSignature='';host.phase='connecting';host.error='';host.meter.reset(performance.now(),host.retry);host.pingSentAt=null;
    for(const pane of panes.values())if(pane.host===host){
      pane.resumeControl=pane.resumeControl||host.controls[pane.raw]?.owner===true;
      pane.generation=host.generation;pane.bindHeader();pane.bindInput();pane.bindObserver();pane.writing=false;pane.lastSize='';pane.lastAttempt='';pane.limitedSize=null;pane.resizePaused=false;pane.frames=[];pane.replayPending=0;
    }
    let url;try{url=C.socketURL(host.config.relay,location.origin);const safe=new URL(url);safe.searchParams.delete('token');url=safe.toString();}
    catch(error){host.error=error.message;host.phase='invalid';updateConnection();renderHosts();return;}
    const account=host.config.authMode==='account'?accountFor(url):null;
    if(host.config.authMode==='account'&&(!host.config.code||!account)){host.phase='auth-failed';host.error='请登录此中继账号并重新选择已授权电脑。';updateConnection();renderHosts();return;}
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
          const message=JSON.parse(event.data);if(message.op==='ready')handshake(host);else if(message.error){host.error=String(message.error);updateConnection();}return;
        }
        const {op,payload}=C.unpack(new Uint8Array(event.data));
        if(op===2||op===7)sync(host,JSON.parse(decoder.decode(payload)),op===2);
        else if(op===3||op===8){
          if(op===8&&payload.length===0)return;
          const data=C.sessionPayload(payload),pane=panes.get(key(host,data.session));
          if(op===3&&data.bytes.length)host.activity.set(data.session,performance.now());
          if(livePane(pane)){
            const size=host.controls[data.session];
            pane.frames.push({op,bytes:data.bytes,grid:size?{cols:size.cols,rows:size.rows}:null});
            if(op===8){pane.replayPending++;metrics.snapshots++;}metrics.outputFrames++;drainPane(pane);
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
      host.connected=false;host.supported=false;clearInterval(host.pingTimer);
      for(const pane of panes.values())if(pane.host===host){pane.ready=false;pane.term.options.disableStdin=true;}
      host.phase=event.code===4001?'auth-failed':'reconnecting';host.meter.reset(performance.now());
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
    if(host.id==='local'&&Array.isArray(info.service?.relays))homeRelays=info.service.relays;
    if(info.error)host.error=String(info.error);
    if(info.capabilities)host.capabilities=info.capabilities;
    for(const field of ['machine','project'])if(typeof info[field]==='string')host.identity[field]=info[field].slice(0,40);
    if(info.controls){host.controls=info.controls;for(const pane of panes.values())if(pane.host===host){acceptResize(pane,info,greeting);if(pane.resumeControl&&host.controls[pane.raw]){pane.resumeControl=false;pane.redrawAfterReplay=true;pane.wantControl=host.controls[pane.raw].owner===true||host.controls[pane.raw].busy===false;}}}
    if(info.windows){host.catalog=info.windows;const sessions=new Set(host.catalog.flatMap(win=>(win.sessions||[]).map(session=>session.id)));for(const id of host.activity.keys())if(!sessions.has(id))host.activity.delete(id);}if(info.groups)host.groups=info.groups;aggregateCatalog();
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
    for(const pane of panes.values())if(pane.host===host){pane.ready=true;pane.term.options.disableStdin=false;const data=sessionInfo(pane.sid);if(data){pane.title.textContent=data.win.title+' · '+data.session.shell;TabThemes.apply(pane.title.parentElement,data.win.tab_theme);}drainPane(pane);}
    renderTree();renderTabs();renderHosts();if(layoutChanged)renderLayout();subscribe();updateConnection();updateFooter();if(greeting)refreshDirectories();
    scheduleVisibleFit();
  }
  function drainPane(pane){
    if(!livePane(pane)||pane.writing||pane.resizeRequest||!pane.host.controls[pane.raw]||!pane.frames.length)return;
    // Catalog updates can arrive while an earlier write is parsing. Keep each
    // frame on the grid confirmed when it arrived, including queued erase/cursor sequences.
    const frame=pane.frames.shift(),replay=frame.op===8,size=frame.grid||pane.host.controls[pane.raw];
    applyGrid(pane,size);
    pane.writing=true;
    if(replay){resizeGrid(pane,size);pane.term.reset();pane.history.hidden=false;frame.bytes=C.snapshotBytes(frame.bytes,pane.raw);}
    const before=performance.now();
    const writeGeneration=pane.generation;
    pane.term.write(frame.bytes,()=>{
      if(!livePane(pane)||pane.generation!==writeGeneration)return;
      pane.writing=false;if(replay)pane.replayPending=Math.max(0,pane.replayPending-1);
      metrics.renderMilliseconds+=performance.now()-before;metrics.parsedBytes+=frame.bytes.length;
      if(pane.followFrame)keepFollowing(pane);
      drainPane(pane);
      if(!pane.frames.length){fitPane(pane);maybeRedrawPane(pane);}
    });
  }
  function createPane(sid) {
    const data=sessionInfo(sid);if(!data)return null;
    const el=node('article','pane');el.dataset.session=sid;
    const header=node('div','pane-header'),title=node('span','pane-title',data.win.title+' · '+data.session.shell);
    const own=node('span','pane-control','');header.append(title,own);
    TabThemes.apply(header,data.win.tab_theme);
    const screen=node('div','terminal-screen');const history=node('div','history-note','已恢复有限输出历史 · 全屏程序画面可能需要刷新');history.hidden=true;
    el.append(header,screen,history);$('stage').append(el);
    const term=new Terminal({allowProposedApi:true,convertEol:false,disableStdin:!data.win.host.connected,cursorBlink:true,
      fontFamily:'"Cascadia Mono", "Cascadia Code", Consolas, monospace',fontSize:mobile()?13:14,scrollback:5000,
      theme:{background:'#121212',foreground:'#dedede',cursor:'#ddd',selectionBackground:'#55555588'},rendererType:'dom'});
    const fit=new FitAddon.FitAddon();term.loadAddon(fit);
    if(window.Unicode11Addon?.Unicode11Addon){try{term.loadAddon(new Unicode11Addon.Unicode11Addon());term.unicode.activeVersion='11';}catch{}}
    term.open(screen);
    const pane={sid,host:data.win.host,raw:data.session.raw,generation:data.win.host.generation,el,screen,term,fit,history,title,own,ready:data.win.host.connected,replayPending:0,lastSize:'',renderer:'dom',webgl:null,observer:null,frames:[],writing:false,wantControl:false,redrawAfterReplay:false,redrawTimer:0,resizeRequest:null,lastAttempt:'',lastInputAt:0,repaintAt:0,composing:false};
    panes.set(sid,pane);setRenderer(pane);
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
    pane.bindHeader=()=>{
      const context=owner(pane.host),guard=action=>(...args)=>{if(validOwner(context)&&livePane(pane))action(...args);};
      header.replaceChildren(title,own);
      header.oncontextmenu=guard(event=>tabMenu(event,data.win,context));
      header.append(button('接管','接管此终端尺寸',guard(()=>takeControl(sid))),button('刷新','强制后台终端全量重绘',guard(()=>redrawPane(pane))),button('改名','重命名窗口',guard(()=>rename(data.win.id,context))),button('结束','结束后台会话',guard(()=>terminate(sid))),button('×','关闭视图（保留后台进程）',guard(()=>removeView(sid))));
    };
    pane.bindHeader();
    pane.bindInput=()=>{
      pane.inputSubscription?.dispose();const context=owner(pane.host);
      pane.inputSubscription=term.onData(text=>{if(!validOwner(context)||!livePane(pane))return;text=C.terminalInput(text,pane.replayPending>0);if(!text)return;if(ctrl&&text.length===1){text=String.fromCharCode(text.toUpperCase().charCodeAt(0)&31);setCtrl(false);}input(sid,text);});
    };
    pane.bindInput();
    term.textarea.addEventListener('compositionstart',()=>{pane.composing=true;});term.textarea.addEventListener('compositionend',()=>{pane.composing=false;pane.lastInputAt=performance.now();});
    el.addEventListener('pointerdown',()=>{if(livePane(pane))activate(sid,false);});
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
  function viewportGeometry(pane){
    const rect=pane.screen.getBoundingClientRect();return [rect.width,rect.height,devicePixelRatio].join(':');
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
    clearTimeout(request.timer);pane.resizeRequest=null;
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
    pane.lastInputAt=performance.now();const frame=C.targetFrame(17,pane.raw,text);socketSend(pane.host,frame);
  }
  function takeControl(sid=active){const pane=panes.get(sid);if(pane){pane.wantControl=true;pane.lastAttempt='';pane.redrawAfterReplay=true;pane.resizePaused=false;fitPane(pane,true);}}
  function openView(sid, takeover=true) {
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
    scheduleVisibleFit();
  }
  function subscribe(){
    for(const host of hosts.values()){
      const sessions=openTabs.map(id=>panes.get(id)).filter(pane=>pane?.host===host&&sessionInfo(pane.sid)).map(pane=>pane.raw);
      const signature=JSON.stringify(sessions);
      if(signature!==host.subscriptionSignature&&send(owner(host),16,{sessions}))host.subscriptionSignature=signature;
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
    for(const row of $('tree').querySelectorAll('.session-row')){
      const host=hosts.get(row.dataset.host),last=host?.activity.get(row.dataset.session);
      const state=!host?.connected?'disconnected':last!==undefined&&now-last<1800?'refreshing':'waiting';
      if(row.dataset.state===state)continue;
      row.dataset.state=state;const dot=row.querySelector('.session-dot');
      dot.title=labels[state];dot.setAttribute('aria-label','状态：'+labels[state]);
    }
  }
  function renderTree(){
    const tree=$('tree');tree.replaceChildren();const query=$('search').value.trim().toLowerCase();let count=0;
    const projects=new Map();for(const host of hosts.values()){const project=host.identity.project||'工作区';if(!projects.has(project))projects.set(project,[]);projects.get(project).push(host);}
    for(const [project,computers] of projects){
      tree.append(node('div','project-label',project));
      for(const host of computers){
        const computer=node('section','computer-tree');computer.dataset.host=host.id;
        computer.append(button(host.identity.machine||host.name,'选择电脑 '+host.name,()=>switchHost(host.id)));
        const allGroups=[...host.groups];for(const win of host.catalog)if(!allGroups.some(group=>group.id===(win.group||'default')))allGroups.push({id:win.group||'default',name:'默认分组'});
        for(const group of allGroups){
          const entries=catalog.filter(win=>win.host===host&&win.group===key(host,group.id)).flatMap(win=>win.sessions.map(session=>({win,session}))).filter(({win,session})=>(win.title+' '+session.shell+' '+(session.cwd||'')).toLowerCase().includes(query));
          if(query&&!entries.length)continue;
          const context=owner(host),heading=node('div','group-label');const pick=button('⌑ '+group.name,'选择分组 '+group.name,()=>{if(!validOwner(context))return;switchHost(host.id);host.selectedGroup=key(host,group.id);renderTree();});
          pick.setAttribute('aria-pressed',String(host.id===selectedHost&&host.selectedGroup===key(host,group.id)));heading.append(pick,node('span','muted',String(entries.length)));computer.append(heading);
          for(const {win,session} of entries){count++;const row=node('div','session-row');row.dataset.current=String(session.id===active);row.dataset.host=host.id;row.dataset.session=session.raw;
            const open=button('','打开 '+win.title,()=>{if(validOwner(context))openView(session.id);});open.className='session-open';const text=node('span','session-label',win.title);text.append(node('span','session-shell',session.shell));const dot=node('i','session-dot');dot.setAttribute('role','img');open.append(dot,text);
            row.append(open,button('···','重命名窗口',()=>rename(win.id,context)));TabThemes.apply(row,win.tab_theme);row.oncontextmenu=event=>tabMenu(event,win,context);computer.append(row);
          }
        }
        tree.append(computer);
      }
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
    for(const id of ['new-session','empty-new','new-group'])$(id).disabled=!host?.connected;
  }
  function newSession(){
    const host=currentHost();if(!host?.connected)return;
    host.sessionsBeforeOpen=new Set(host.catalog.flatMap(win=>win.sessions||[]).map(session=>session.id));host.nextSplit='new';
    const group=host.groups.find(group=>key(host,group.id)===host.selectedGroup)?.id||host.groups[0]?.id||'default';
    send(owner(host),6,{shell:$('preset').value,group});
  }
  function rename(id,context){
    const win=catalog.find(win=>win.id===id);if(!win)return;context=context||owner(win.host);if(!validOwner(context))return;
    renameWindow={...context,id:win.raw};$('rename-value').value=win.title;$('rename-dialog').showModal();$('rename-value').focus();$('rename-value').select();
  }
  function terminate(sid){const pane=panes.get(sid);if(!livePane(pane))return;terminateSession={...owner(pane.host),id:pane.raw};$('terminate-dialog').showModal();}
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
  function renderHosts(){
    const list=$('workspace-hosts');list.replaceChildren();
    for(const host of hosts.values()){
      const row=node('div','workspace-host');row.dataset.current=String(host.id===selectedHost);row.dataset.host=host.id;row.dataset.phase=host.phase;
      const pick=button('','切换到'+(host.id==='local'?host.name:' '+host.name),()=>switchHost(host.id));pick.className='workspace-host-pick';pick.setAttribute('aria-current',String(host.id===selectedHost));
      pick.append(node('span','workspace-host-name',host.name),node('small','muted',host.config.code||'本机终端'));
      const protocol=(Number.isInteger(host.capabilities.protocol_version)?'v'+host.capabilities.protocol_version:'旧版/未知')+(host.capabilities.multiplex?' · multiplex':'')+(host.capabilities.resize_ack?' / ACK':'');
      pick.append(node('span','workspace-host-status',(host.connected?'已连接':host.phase)+' · '+(host.meter.rtt===null?'—':Math.round(host.meter.rtt)+' ms')),
        node('small','host-rates','↑ '+C.formatRate(host.rates.tx)+' ↓ '+C.formatRate(host.rates.rx)+' · '+protocol));row.append(pick);
      if(host.id!=='local'){const edit=button('···','编辑工作区 '+host.name,()=>showWorkspace(host.id));edit.className='workspace-host-edit';row.append(edit);}list.append(row);
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
  $('connection-close').onclick=()=>{cancelAccountLogin();returnToWorkspace=false;$('connection-dialog').close();};$('connect-button').onclick=showConnection;
  $('rename-cancel').onclick=()=>$('rename-dialog').close();$('rename-form').onsubmit=e=>{e.preventDefault();send(renameWindow,15,{action:'title',window:renameWindow?.id,title:$('rename-value').value.trim()});$('rename-dialog').close();};
  $('terminate-cancel').onclick=()=>$('terminate-dialog').close();$('terminate-form').onsubmit=e=>{e.preventDefault();send(terminateSession,9,{session:terminateSession?.id});$('terminate-dialog').close();};
  $('new-project').onclick=()=>showWorkspace();
  $('new-group').onclick=()=>{const context=owner(currentHost()),name=prompt('新分组名称');if(name?.trim())send(context,15,{action:'create',name:name.trim(),channel:name.trim()});};
  for(const [command,label] of presets){const option=node('option','',label);option.value=command;$('preset').append(option);}
  $('new-session').onclick=newSession;$('empty-new').onclick=newSession;$('search').oninput=renderTree;
  $('split-horizontal').onclick=()=>split('horizontal');$('split-vertical').onclick=()=>split('vertical');
  $('maximize').onclick=()=>{if(!active)return;maximized=!maximized;$('maximize').setAttribute('aria-pressed',String(maximized));renderLayout();};
  $('take-control').onclick=()=>takeControl();$('renderer').value=renderer;
  $('renderer').onchange=()=>{renderer=C.rendererName($('renderer').value);save(localStorage,'sma.workspace.renderer',renderer);for(const pane of panes.values())setRenderer(pane);updateFooter();};
  $('sidebar-toggle').onclick=()=>{if(mobile())$('app').classList.toggle('mobile-sidebar');else {$('app').classList.toggle('sidebar-hidden');$('app').style.gridTemplateColumns=$('app').classList.contains('sidebar-hidden')?'0 minmax(0,1fr)':($('sidebar').style.width||'240px')+' minmax(0,1fr)';}scheduleVisibleFit();};
  const sidebarObserver=new ResizeObserver(()=>{if(!$('app').classList.contains('sidebar-hidden')&&!mobile())$('app').style.gridTemplateColumns=$('sidebar').getBoundingClientRect().width+'px minmax(0,1fr)';});sidebarObserver.observe($('sidebar'));
  $('ctrl-key').onclick=()=>{setCtrl(!ctrl);panes.get(active)?.term.focus();};
  const keys={escape:'\x1b',tab:'\t',up:'\x1b[A',down:'\x1b[B',left:'\x1b[D',right:'\x1b[C'};
  for(const b of document.querySelectorAll('[data-key]')){b.onpointerdown=e=>e.preventDefault();b.onclick=()=>{input(active,keys[b.dataset.key]);panes.get(active)?.term.focus();};}
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
  function refreshDirectories(){
    const wanted=new Map();
    for(const host of [...hosts.values()].sort((a,b)=>Number(b.connected)-Number(a.connected)))if(host.config.authMode!=='account'&&host.config.code&&host.config.token){
      const url=JSON.parse(endpoint(host.config))[0];if(!wanted.has(url))wanted.set(url,[]);
      const rooms=wanted.get(url);if(!rooms.some(room=>room.code===host.config.code)&&rooms.length<64)rooms.push({code:host.config.code,token:host.config.token});
    }
    for(const [url,directory] of directories)if(!wanted.has(url)){stopDirectory(directory);directories.delete(url);}
    for(const [url,rooms] of wanted){
      let directory=directories.get(url);
      if(!directory){directory={url,rooms:[],generation:0,ws:null,retry:0,retryTimer:0,signature:'',hosts:[],phase:'connecting',revision:0};directories.set(url,directory);}
      const signature=JSON.stringify(rooms);if(directory.signature===signature)continue;
      directory.rooms=rooms;directory.signature=signature;directory.hosts=[];directory.revision=0;
      // Close before replacing credentials, so stale authorized metadata cannot be displayed.
      connectDirectory(directory);
    }
    renderDiscovery();
  }
  function connectDirectory(directory){
    stopDirectory(directory);const generation=directory.generation;directory.phase='connecting';
    const live=()=>directories.get(directory.url)===directory&&directory.generation===generation;
    const socket=new WebSocket(directory.url);directory.ws=socket;
    socket.onopen=()=>{if(live())socket.send(JSON.stringify({op:'watch',rooms:directory.rooms}));};
    socket.onmessage=event=>{
      if(!live()||typeof event.data!=='string'||event.data.length>1048576)return;
      try{
        const data=JSON.parse(event.data);
        if(data.op!=='directory'||!Array.isArray(data.hosts)||data.hosts.length>64||!Number.isSafeInteger(data.revision)||data.revision<directory.revision)return;
        const known=new Set(directory.rooms.map(room=>room.code));
        directory.hosts=data.hosts.filter(host=>known.has(host.code)).map(host=>({code:host.code,online:host.online===true,
          ...(host.online===true?{machine:String(host.machine||'').slice(0,40),project:String(host.project||'').slice(0,40)}:{})}));
        directory.revision=data.revision;directory.phase='connected';directory.retry=0;renderDiscovery();
      }catch{directory.phase='unsupported';directory.hosts=[];renderDiscovery();}
    };
    socket.onerror=()=>{};
    socket.onclose=event=>{
      if(!live())return;directory.hosts=[];directory.phase=[4000,4001,4003].includes(event.code)?'unsupported':'offline';renderDiscovery();
      if(directory.phase!=='unsupported')directory.retryTimer=setTimeout(()=>{if(live())connectDirectory(directory);},C.retryDelay(directory.retry++));
    };
  }
  function renderDiscovery(){
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
  addEventListener('beforeunload',()=>{clearInterval(meterTimer);clearInterval(activityTimer);for(const host of hosts.values())stopHost(host);for(const directory of directories.values())stopDirectory(directory);cancelAccountLogin();for(const account of relayAccounts.values()){stopAccountDirectory(account);clearTimeout(account.expiryTimer);}});
  // Launch secrets are consumed once. Fragments never reach HTTP access logs.
  if(params.has('token')||launch.has('token')){
    save(sessionStorage,'sma.workspace.token',config.token);const clean=new URL(location.href);clean.searchParams.delete('token');launch.delete('token');clean.hash=launch.toString();history.replaceState(null,'',clean);
  }
  window.smaWorkspace={get activeSession(){return active;},get terminal(){return panes.get(active)?.term;},get panes(){return panes;},metrics,
    get activePane(){return panes.get(active);},get activeHost(){return currentHost();},get connections(){return hosts;},get directories(){return directories;},get relayAccounts(){return relayAccounts;},
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
    const selected=read(sessionStorage,'sma.workspace.activeHost');selectedHost=hosts.has(selected)?selected:'local';
    for(const host of hosts.values())connect(host);for(const account of relayAccounts.values())connectAccountDirectory(account);refreshDirectories();renderTree();updateFooter();
    if(!config.token&&!config.code)setTimeout(()=>{if(!userConnectedOnce)showConnection();},1500);
  }
  bootstrap();
})();
