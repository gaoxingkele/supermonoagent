/* Orca-inspired shared-session workspace. One socket, byte-preserving xterm streams. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const C = window.TerminalClient;
  const encoder = new TextEncoder(), decoder = new TextDecoder();
  const params = new URLSearchParams(location.search);
  const read = (storage, key) => { try { return storage.getItem(key) || ''; } catch { return ''; } };
  const save = (storage, key, value) => { try { value ? storage.setItem(key, value) : storage.removeItem(key); } catch {} };
  let config = {relay: params.get('relay') || read(localStorage, 'sma.workspace.relay'),
    code: params.get('code') || read(localStorage, 'sma.workspace.code'),
    token: params.get('token') || read(sessionStorage, 'sma.workspace.token') || read(localStorage, 'sma.workspace.token')};
  let renderer = C.rendererName(params.get('renderer') || read(localStorage, 'sma.workspace.renderer'));
  let catalog = [], groups = [], panes = new Map(), openTabs = [], active = '', layout = null;
  let ws = null, generation = 0, retry = 0, retryTimer = 0, connected = false, helloSent = false;
  let supported = false, userConnectedOnce = false, controls = {}, maximized = false, nextSplit = '', ctrl = false;
  let selectedGroup = '', renameWindow = '', terminateSession = '', initialCatalog = true, pingTimer = 0;
  let subscriptionSignature = '', sessionsBeforeOpen = new Set();
  const mobile = () => matchMedia('(max-width:600px)').matches;
  const metrics = {socketsOpened:0, receivedBytes:0, transmittedBytes:0, outputFrames:0, snapshots:0,
    renderMilliseconds:0, parsedBytes:0, reconnects:0, rendererFallbacks:0, lastPongMs:null};
  const presets = [ ['powershell','PowerShell'], ['pwsh','PowerShell 7'], ['cmd','CMD'], ['claude','Claude'], ['codex','Codex'] ];
  const node = (tag, className, text) => { const el = document.createElement(tag); if(className)el.className=className;if(text!==undefined)el.textContent=text;return el; };
  const button = (text, title, action) => { const b=node('button','',text);b.type='button';b.title=title;b.setAttribute('aria-label',title);b.onclick=action;return b; };
  const sessionInfo = sid => { for(const win of catalog){const session=(win.sessions||[]).find(s=>s.id===sid);if(session)return {win,session};}return null; };
  const visibleIDs = () => { if(!layout)return [];if(mobile()||maximized)return active?[active]:[];const ids=[];const walk=n=>{if(typeof n==='string')ids.push(n);else if(n){walk(n.a);walk(n.b);}};walk(layout);return [...new Set(ids)]; };
  const notice = message => { $('notice').textContent=message;$('notice').hidden=!message; };
  function status(text) { $('status').textContent=text; }
  function send(op, value) {
    if(!connected||!supported||!ws||ws.readyState!==WebSocket.OPEN)return false;
    const payload=value instanceof Uint8Array?value:encoder.encode(JSON.stringify(value));
    const frame=C.pack(op,payload);ws.send(frame);metrics.transmittedBytes+=frame.length;return true;
  }
  function handshake() {
    if(helloSent||!ws||ws.readyState!==WebSocket.OPEN)return;
    helloSent=true;
    const payload=encoder.encode(JSON.stringify({token:config.token,multiplex:true,reliable:true,channel:params.get('channel')||undefined}));
    ws.send(C.pack(1,payload));
  }
  function connect() {
    clearTimeout(retryTimer);clearInterval(pingTimer);
    generation++;const current=generation;
    if(ws){ws.onclose=null;ws.close();}
    connected=false;supported=false;helloSent=false;subscriptionSignature='';
    for(const p of panes.values()){p.ready=false;p.lastSize='';p.term.options.disableStdin=true;}
    $('connection-dot').classList.remove('online');$('connection-label').textContent='正在连接';status('正在连接…');
    let url;
    try {url=C.socketURL(config.relay,location.origin);if(!config.code&&config.token){const direct=new URL(url);direct.searchParams.set('token',config.token);url=direct.toString();}}catch(error){notice(error.message);$('connection-label').textContent='连接配置无效';return;}
    ws=new WebSocket(url);ws.binaryType='arraybuffer';metrics.socketsOpened++;
    ws.onopen=()=>{if(current!==generation)return;if(config.code)ws.send(JSON.stringify({op:'phone',code:config.code,token:config.token,reliable:true}));else handshake();};
    ws.onmessage=event=>{
      if(current!==generation)return;
      try {
        if(typeof event.data==='string'){
          const message=JSON.parse(event.data);
          if(message.op==='ready')handshake();
          else if(message.error){notice(String(message.error));status('连接被拒绝');}
          return;
        }
        const frame=new Uint8Array(event.data);metrics.receivedBytes+=frame.length;const {op,payload}=C.unpack(frame);
        if(op===2||op===7){const info=JSON.parse(decoder.decode(payload));sync(info,op===2);}
        else if(op===3||op===8){
          if(op===8&&payload.length===0)return; // Empty-window selection control, not a session feed.
          const data=C.sessionPayload(payload);const pane=panes.get(data.session);
          if(pane){
            const replay=op===8;
            if(replay){pane.replayPending++;pane.term.reset();pane.history.hidden=false;metrics.snapshots++;}
            const before=performance.now();
            pane.term.write(data.bytes,()=>{if(replay)pane.replayPending=Math.max(0,pane.replayPending-1);metrics.renderMilliseconds+=performance.now()-before;metrics.parsedBytes+=data.bytes.length;});metrics.outputFrames++;
          }
        }
        else if(op===11){const stamp=Number(decoder.decode(payload));if(Number.isFinite(stamp)){metrics.lastPongMs=Math.round(performance.now()-stamp);updateFooter();}}
      }catch(error){notice('终端协议错误：'+error.message);}
    };
    ws.onerror=()=>status('连接失败，正在等待重试');
    ws.onclose=event=>{
      if(current!==generation)return;
      connected=false;supported=false;clearInterval(pingTimer);
      for(const p of panes.values()){p.ready=false;p.term.options.disableStdin=true;}
      $('connection-dot').classList.remove('online');$('connection-label').textContent='离线 · 重连中';
      status('连接已断开；输入已暂停');
      if(event.reason)notice('连接关闭：'+event.reason);
      metrics.reconnects++;retryTimer=setTimeout(connect,C.retryDelay(retry++));
    };
  }
  function sync(info, greeting) {
    if(greeting&&!connected){
      if(!info.capabilities?.multiplex){notice('此服务器尚不支持多终端工作区。请升级后台，或使用右上角经典版。');status('服务器协议不兼容');ws.onclose=null;ws.close();return;}
      supported=true;connected=true;retry=0;userConnectedOnce=true;initialCatalog=true;
      notice('');$('connection-dot').classList.add('online');$('connection-label').textContent=config.code?'中继已连接':'本机已连接';
      clearInterval(pingTimer);pingTimer=setInterval(()=>send(10,encoder.encode(String(performance.now()))),15000);
    }
    if(!supported)return;
    if(info.error){notice(String(info.error));}
    const beforeCatalog=JSON.stringify([catalog,groups]);let layoutChanged=false;
    if(info.controls)controls=info.controls;
    if(info.windows)catalog=info.windows;
    if(info.groups)groups=info.groups;
    $('host-name').textContent=[info.machine,info.project].filter(Boolean).join(' / ')||'本机';
    const valid=new Set(catalog.flatMap(w=>(w.sessions||[]).map(s=>s.id)));
    for(const id of panes.keys()){if(!valid.has(id)){removeView(id,false);layoutChanged=true;}}
    if(initialCatalog){initialCatalog=false;const first=catalog.flatMap(w=>w.sessions||[])[0];if(first&&!openTabs.length)openView(first.id);}
    if(nextSplit==='new'){const created=catalog.flatMap(w=>w.sessions||[]).find(s=>!sessionsBeforeOpen.has(s.id));if(created){nextSplit='';openView(created.id);}}
    for(const p of panes.values()){p.ready=true;p.term.options.disableStdin=false;const data=sessionInfo(p.sid);if(data)p.title.textContent=data.win.title+' · '+data.session.shell;}
    if(beforeCatalog!==JSON.stringify([catalog,groups])){renderTree();renderTabs();}
    if(layoutChanged)renderLayout();
    subscribe();updateFooter();
  }
  function createPane(sid) {
    const data=sessionInfo(sid);if(!data)return null;
    const el=node('article','pane');el.dataset.session=sid;
    const header=node('div','pane-header'),title=node('span','pane-title',data.win.title+' · '+data.session.shell);
    const own=node('span','pane-control','');header.append(title,own);
    header.append(button('接管','接管此终端尺寸',()=>takeControl(sid)),button('改名','重命名窗口',()=>rename(data.win.id)),button('结束','结束后台会话',()=>terminate(sid)),button('×','关闭视图（保留后台进程）',()=>removeView(sid)));
    const screen=node('div','terminal-screen');const history=node('div','history-note','已恢复有限输出历史 · 全屏程序画面可能需要刷新');history.hidden=true;
    el.append(header,screen,history);$('stage').append(el);
    const term=new Terminal({allowProposedApi:true,convertEol:false,disableStdin:!connected,cursorBlink:true,
      fontFamily:'"Cascadia Mono", "Cascadia Code", Consolas, monospace',fontSize:mobile()?13:14,scrollback:5000,
      theme:{background:'#121212',foreground:'#dedede',cursor:'#ddd',selectionBackground:'#55555588'},rendererType:'dom'});
    const fit=new FitAddon.FitAddon();term.loadAddon(fit);
    if(window.Unicode11Addon?.Unicode11Addon){try{term.loadAddon(new Unicode11Addon.Unicode11Addon());term.unicode.activeVersion='11';}catch{}}
    term.open(screen);
    const pane={sid,el,screen,term,fit,history,title,own,ready:connected,replayPending:0,lastSize:'',renderer:'dom',webgl:null,observer:null};
    panes.set(sid,pane);setRenderer(pane);
    term.onData(text=>{text=C.terminalInput(text,pane.replayPending>0);if(!text)return;if(ctrl&&text.length===1){text=String.fromCharCode(text.toUpperCase().charCodeAt(0)&31);setCtrl(false);}input(sid,text);});
    el.addEventListener('pointerdown',()=>activate(sid,false));
    pane.observer=new ResizeObserver(()=>{requestAnimationFrame(()=>fitPane(pane));});pane.observer.observe(screen);
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
  function fitPane(pane, takeover=false) {
    if(panes.get(pane.sid)!==pane||!visibleIDs().includes(pane.sid)||pane.screen.clientWidth<20||pane.screen.clientHeight<20)return;
    try{pane.fit.fit();}catch{return;}
    const size=pane.term.cols+'x'+pane.term.rows;
    if(pane.ready&&(takeover||pane.lastSize!==size)){
      if(send(5,{session:pane.sid,cols:pane.term.cols,rows:pane.term.rows,...(takeover?{take_control:true}:{})}))pane.lastSize=size;
    }
    updateFooter();
  }
  function input(sid,text) {
    const pane=panes.get(sid);
    if(!pane?.ready||!connected||!supported||!openTabs.includes(sid))return;
    const frame=C.targetFrame(17,sid,text);ws.send(frame);metrics.transmittedBytes+=frame.length;
  }
  function takeControl(sid=active){const pane=panes.get(sid);if(pane)fitPane(pane,true);}
  function openView(sid) {
    if(!sessionInfo(sid))return;
    if(!panes.has(sid))createPane(sid);
    if(!openTabs.includes(sid))openTabs.push(sid);
    if(!layout)layout=sid;
    else if(!visibleIDs().includes(sid)){layout=replaceLeaf(layout,active,sid);}
    active=sid;selectedGroup=sessionInfo(sid).win.group||'default';
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
    C.disposePane(panes,sid);
    if(redraw){renderTree();renderTabs();renderLayout();subscribe();}
  }
  function activate(sid,focus=true){if(active===sid)return;active=sid;renderTree();renderTabs();if(mobile()||maximized)renderLayout();else for(const p of panes.values())p.el.classList.toggle('active',p.sid===active);updateFooter();if(focus)panes.get(sid)?.term.focus();}
  function split(direction){
    const candidate=openTabs.find(id=>id!==active&&!visibleIDs().includes(id))||catalog.flatMap(w=>w.sessions||[]).find(s=>s.id!==active&&!visibleIDs().includes(s.id))?.id;
    if(!active||!candidate){notice('先新建另一个终端，再使用分屏。');return;}
    if(!panes.has(candidate))createPane(candidate);if(!openTabs.includes(candidate))openTabs.push(candidate);
    layout=replaceLeaf(layout,active,{direction,ratio:50,a:active,b:candidate});maximized=false;active=candidate;
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
    requestAnimationFrame(()=>{for(const id of visibleIDs())fitPane(panes.get(id));});
  }
  function subscribe(){
    // All cached open tabs remain subscribed: hidden parsers retain ANSI/UTF8 state without repainting.
    const sessions=openTabs.filter(id=>sessionInfo(id));const signature=JSON.stringify(sessions);
    if(signature!==subscriptionSignature&&send(16,{sessions}))subscriptionSignature=signature;
  }
  function renderTabs(){
    $('tabs').replaceChildren();
    for(const id of openTabs){const info=sessionInfo(id);if(!info)continue;const tab=node('div','tab');tab.dataset.current=String(id===active);tab.setAttribute('role','presentation');
      const pick=button(info.win.title||info.session.shell,'切换到 '+(info.win.title||info.session.shell),()=>openView(id));pick.setAttribute('role','tab');pick.setAttribute('aria-selected',String(id===active));
      tab.append(pick,button('×','关闭视图（保留后台进程）',()=>removeView(id)));$('tabs').append(tab);
    }
  }
  function renderTree(){
    const tree=$('tree');tree.replaceChildren();const query=$('search').value.trim().toLowerCase();let count=0;
    const allGroups=[...groups];for(const w of catalog)if(!allGroups.some(g=>g.id===(w.group||'default')))allGroups.push({id:w.group||'default',name:'默认项目'});
    for(const group of allGroups){const wins=catalog.filter(w=>(w.group||'default')===group.id);const entries=wins.flatMap(win=>(win.sessions||[]).map(session=>({win,session}))).filter(({win,session})=>(win.title+' '+session.shell+' '+(session.cwd||'')).toLowerCase().includes(query));
      if(query&&!entries.length)continue;
      const heading=node('div','group-label');const groupPick=button('⌄  '+group.name,'选择项目 '+group.name,()=>{selectedGroup=group.id;renderTree();});groupPick.style.fontSize='11px';groupPick.style.fontWeight='600';groupPick.style.padding='0';groupPick.style.minHeight='23px';groupPick.setAttribute('aria-pressed',String(selectedGroup===group.id));heading.append(groupPick,node('span','muted',String(entries.length)));tree.append(heading);
      for(const {win,session} of entries){count++;const row=node('div','session-row');row.dataset.current=String(session.id===active);const pick=button('','打开 '+win.title,()=>openView(session.id));pick.className='session-open';const text=node('span','session-label',win.title);text.append(node('span','session-shell',session.shell));pick.append(node('span','session-icon',session.shell.includes('cmd')?'>_':'❯_'),text);row.append(pick);const ren=button('···','重命名窗口',()=>rename(win.id));ren.className='rename';row.append(ren);tree.append(row);}
    }
    if(!catalog.length)tree.append(node('p','muted small','暂无会话。新建终端开始工作。'));
    $('session-count').textContent=count+' 个终端';
  }
  function updateFooter(){
    const p=panes.get(active),info=sessionInfo(active);$('dimensions').textContent=p?p.term.cols+' × '+p.term.rows:'—';$('effective-renderer').textContent=p?p.renderer.toUpperCase():'—';
    if(connected)status((info?.session.cwd||'工作区已连接')+(metrics.lastPongMs===null?'':' · '+metrics.lastPongMs+' ms'));
    for(const pane of panes.values()){const lease=controls[pane.sid];pane.own.textContent=lease?(lease.owner?'尺寸主控':'共享观看'):'';}
    $('take-control').disabled=!p||!connected;
    for(const id of ['new-session','empty-new','new-project'])$(id).disabled=!connected;
  }
  function newSession(){if(!connected)return;sessionsBeforeOpen=new Set(catalog.flatMap(w=>w.sessions||[]).map(s=>s.id));nextSplit='new';send(6,{shell:$('preset').value,group:selectedGroup||groups[0]?.id||'default'});}
  function rename(id){const win=catalog.find(w=>w.id===id);if(!win)return;renameWindow=id;$('rename-value').value=win.title;$('rename-dialog').showModal();$('rename-value').focus();$('rename-value').select();}
  function terminate(sid){terminateSession=sid;$('terminate-dialog').showModal();}
  function setCtrl(value){ctrl=value;$('ctrl-key').setAttribute('aria-pressed',String(value));}
  function showConnection(){
    $('relay-url').value=config.relay;$('relay-code').value=config.code;$('access-token').value=config.token;$('remember-token').checked=!!read(localStorage,'sma.workspace.token');$('connection-error').textContent='';$('connection-dialog').showModal();$('relay-url').focus();
  }
  $('connection-form').onsubmit=e=>{
    e.preventDefault();const relay=$('relay-url').value.trim(),code=$('relay-code').value.trim(),token=$('access-token').value.trim();
    try{C.socketURL(relay,location.origin);}catch(error){$('connection-error').textContent=error.message;return;}
    config={relay,code,token};save(localStorage,'sma.workspace.relay',relay);save(localStorage,'sma.workspace.code',code);save(sessionStorage,'sma.workspace.token',token);save(localStorage,'sma.workspace.token',$('remember-token').checked?token:'');
    $('connection-dialog').close();retry=0;connect();
  };
  $('connection-close').onclick=()=>$('connection-dialog').close();$('connect-button').onclick=showConnection;
  $('rename-cancel').onclick=()=>$('rename-dialog').close();$('rename-form').onsubmit=e=>{e.preventDefault();send(15,{action:'title',window:renameWindow,title:$('rename-value').value.trim()});$('rename-dialog').close();};
  $('terminate-cancel').onclick=()=>$('terminate-dialog').close();$('terminate-form').onsubmit=e=>{e.preventDefault();send(9,{session:terminateSession});$('terminate-dialog').close();};
  $('new-project').onclick=()=>{const name=prompt('新项目名称');if(name?.trim())send(15,{action:'create',name:name.trim(),channel:name.trim()});};
  for(const [command,label] of presets){const option=node('option','',label);option.value=command;$('preset').append(option);}
  $('new-session').onclick=newSession;$('empty-new').onclick=newSession;$('search').oninput=renderTree;
  $('split-horizontal').onclick=()=>split('horizontal');$('split-vertical').onclick=()=>split('vertical');
  $('maximize').onclick=()=>{if(!active)return;maximized=!maximized;$('maximize').setAttribute('aria-pressed',String(maximized));renderLayout();};
  $('take-control').onclick=()=>takeControl();$('renderer').value=renderer;
  $('renderer').onchange=()=>{renderer=C.rendererName($('renderer').value);save(localStorage,'sma.workspace.renderer',renderer);for(const pane of panes.values())setRenderer(pane);updateFooter();};
  $('sidebar-toggle').onclick=()=>{if(mobile())$('app').classList.toggle('mobile-sidebar');else {$('app').classList.toggle('sidebar-hidden');$('app').style.gridTemplateColumns=$('app').classList.contains('sidebar-hidden')?'0 minmax(0,1fr)':($('sidebar').style.width||'240px')+' minmax(0,1fr)';}requestAnimationFrame(()=>{for(const id of visibleIDs())fitPane(panes.get(id));});};
  const sidebarObserver=new ResizeObserver(()=>{if(!$('app').classList.contains('sidebar-hidden')&&!mobile())$('app').style.gridTemplateColumns=$('sidebar').getBoundingClientRect().width+'px minmax(0,1fr)';});sidebarObserver.observe($('sidebar'));
  $('ctrl-key').onclick=()=>{setCtrl(!ctrl);panes.get(active)?.term.focus();};
  const keys={escape:'\x1b',tab:'\t',up:'\x1b[A',down:'\x1b[B',left:'\x1b[D',right:'\x1b[C'};
  for(const b of document.querySelectorAll('[data-key]')){b.onpointerdown=e=>e.preventDefault();b.onclick=()=>{input(active,keys[b.dataset.key]);panes.get(active)?.term.focus();};}
  addEventListener('resize',()=>{if(mobile())$('app').style.gridTemplateColumns='';renderLayout();});
  addEventListener('keydown',e=>{if(e.ctrlKey&&e.altKey&&e.key.toLowerCase()==='n'){e.preventDefault();newSession();}if(e.key==='Escape')$('app').classList.remove('mobile-sidebar');});
  addEventListener('beforeunload',()=>{generation++;clearTimeout(retryTimer);clearInterval(pingTimer);if(ws){ws.onclose=null;ws.close();}});
  // Query secrets are consumed once, then removed from the visible history URL.
  if(params.has('token')){save(sessionStorage,'sma.workspace.token',config.token);const clean=new URL(location.href);clean.searchParams.delete('token');history.replaceState(null,'',clean);}
  window.smaWorkspace={get activeSession(){return active;},get terminal(){return panes.get(active)?.term;},get panes(){return panes;},metrics,
    get effectiveRenderer(){return panes.get(active)?.renderer;},get connected(){return connected;},get layout(){return layout;},openSession:openView,split,
    closeView:removeView,takeControl,setRenderer:value=>{$('renderer').value=C.rendererName(value);$('renderer').onchange();},get controls(){return controls;}};
  async function bootstrap(){
    if(!config.token&&!config.code&&!config.relay&&['localhost','127.0.0.1','[::1]','::1'].includes(location.hostname)){
      try{const response=await fetch('/health',{signal:AbortSignal.timeout(2000)});if(response.ok){const health=await response.json();if(typeof health.default_token==='string')config.token=health.default_token;}}catch{}
    }
    connect();
    if(!config.token&&!config.code)setTimeout(()=>{if(!userConnectedOnce)showConnection();},1500);
  }
  updateFooter();bootstrap();
})();
