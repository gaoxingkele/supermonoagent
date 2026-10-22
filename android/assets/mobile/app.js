/* Android-only terminal document. The daemon remains the owner of every shell. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id),C=TerminalClient,M=MobileCore;
  const encoder=new TextEncoder(),decoder=new TextDecoder();
  let config;
  try{config=JSON.parse(decodeURIComponent(location.hash.slice(1)));history.replaceState(null,'',location.pathname+location.search);}
  catch{ $('status').textContent='连接配置缺失，请返回连接设置';$('notice').hidden=false;$('notice').textContent='请从安卓应用打开此页面';return; }
  $('ui-revision').textContent='界面版本：'+(config.uiRevision||'bundled');
  const home={id:'home',name:'默认电脑',endpoint:config.endpoint,code:config.code||''};
  const hostTokens=new Map([['home',config.token||'']]);
  let hosts=[],hostId='home',machine='',project='';
  try{const saved=JSON.parse(localStorage.getItem('sma.mobile.hosts')||'[]');if(Array.isArray(saved))hosts=saved.filter(h=>h&&typeof h.id==='string'&&h.id!=='home'&&typeof h.endpoint==='string').slice(0,30).map(h=>({id:h.id,name:String(h.name||'电脑').slice(0,60),endpoint:h.endpoint,code:String(h.code||'')}));}catch{}
  const connections=new Map(),watchers=new Map();
  const sessionKey=(id=active,hid=hostId)=>JSON.stringify([hid,id]);
  const targetKey=h=>JSON.stringify([new URL(h.endpoint).toString().replace(/\/+$/,''),h.code||'']);
  // Page-private session credentials survive an update navigation, never UI-state export.
  function saveSessionCredentials(){try{sessionStorage.setItem('sma.mobile.credentials',JSON.stringify([home,...hosts].filter(h=>hostTokens.has(h.id)).map(h=>[h.id,targetKey(h),hostTokens.get(h.id)])));}catch{}}
  try{for(const row of JSON.parse(sessionStorage.getItem('sma.mobile.credentials')||'[]')){const h=[home,...hosts].find(h=>h.id===row[0]);if(h&&row.length===3&&row[1]===targetKey(h)&&typeof row[2]==='string'&&h.id!=='home')hostTokens.set(h.id,row[2]);}}catch{}
  let restoringUI=null;

  function connection(h){let c=connections.get(h.id);if(!c){c={host:h,socket:null,generation:0,retry:0,retryTimer:null,pingTimer:null,lastPing:0,connected:false,inventory:[],groups:[],activity:new Map(),controls:{},machine:'',project:'',subscription:'',status:'等待连接',createPending:false,beforeCreate:new Set()};connections.set(h.id,c);}return c;}
  let paused=false,connected=false;
  let inventory=[],active='',term=null,fit=null,writer=null,webgl=null,renderer=config.renderer||'auto';
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
  const notice=text=>{$('notice').textContent=text;$('notice').hidden=!text;};
  const state=text=>{$('status').textContent=text;};
  function saveHosts(){try{localStorage.setItem('sma.mobile.hosts',JSON.stringify(hosts));return true;}catch{notice('电脑列表无法保存；请检查设备存储空间');return false;}}
  function selected(){return connection([home,...hosts].find(h=>h.id===hostId)||home);}
  function loadSelected(){const c=selected();connected=c.connected;inventory=c.inventory;controls=c.controls;machine=c.machine;project=c.project;createPending=c.createPending;state(c.status);}
  function renderHosts(){
    $('host-list').replaceChildren();
    for(const h of [home,...hosts]){
      const c=connection(h),row=document.createElement('div');row.className='host-row';
      const b=document.createElement('button');b.className='host-button';b.dataset.hostId=h.id;b.setAttribute('aria-current',String(h.id===hostId));
      b.textContent=[c.project,c.machine||h.name,c.status].filter(Boolean).join(' · ');b.onclick=()=>chooseHost(h.id);row.append(b);
      const edit=document.createElement('button');edit.textContent='设置';edit.onclick=()=>editHost(h);row.append(edit);
      const retryButton=document.createElement('button');retryButton.textContent='重连';retryButton.onclick=()=>connect(c);row.append(retryButton);
      if(h.id!=='home'){const remove=document.createElement('button');remove.className='host-remove';remove.textContent='移除';remove.setAttribute('aria-label','移除电脑 '+h.name);remove.onclick=()=>{stopSocket(c);connections.delete(h.id);hosts=hosts.filter(x=>x.id!==h.id);hostTokens.delete(h.id);saveSessionCredentials();for(const key of [...drafts.keys()])if(JSON.parse(key)[0]===h.id)drafts.delete(key);for(const key of [...modes.keys()])if(JSON.parse(key)[0]===h.id)modes.delete(key);if(h.id===hostId)chooseHost('home');saveHosts();refreshWatchers();renderHosts();renderList();};row.append(remove);}
      $('host-list').append(row);
    }
    $('host-identity').textContent=[machine||selected().host.name,project].filter(Boolean).join(' · ');
  }
  function chooseHost(id){
    const h=[home,...hosts].find(x=>x.id===id);if(!h||id===hostId)return;
    if(active)drafts.set(sessionKey(),$('draft').value);
    const previous=selected();sendHost(previous,16,{sessions:[]});previous.subscription='';
    disposeTerminal();active='';hostId=id;loadSelected();$('draft').value='';notice('');
    if(inventory.length)activate(inventory[0].id);renderHosts();renderList();
  }
  function endpoint(h){
    const u=new URL(h.endpoint);
    if(!['http:','https:'].includes(u.protocol)||u.username||u.password||u.search||u.hash)throw new Error('无效的服务器地址');
    u.pathname=u.pathname.replace(/\/+$/,'')+'/ws';u.protocol=u.protocol==='https:'?'wss:':'ws:';return u.toString();
  }
  function sendHost(c,op,value){if(!c.connected||paused||c.socket?.readyState!==1)return false;c.socket.send(C.pack(op,value instanceof Uint8Array?value:encoder.encode(JSON.stringify(value))));return true;}
  function send(op,value){return sendHost(selected(),op,value);}
  function stopSocket(c=selected()){
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
  function refreshWatchers(){
    for(const w of watchers.values()){clearTimeout(w.timer);w.socket.onclose=null;w.socket.close();}watchers.clear();if(paused)return;
    const groups=new Map();for(const h of [home,...hosts]){if(!h.code||!hostTokens.get(h.id))continue;let url;try{url=endpoint(h);}catch{continue;}if(!groups.has(url))groups.set(url,[]);groups.get(url).push(h);}
    for(const [url,known] of groups){const socket=new WebSocket(url),w={socket,timer:null};watchers.set(url,w);
      socket.onopen=()=>socket.send(JSON.stringify({op:'watch',rooms:known.map(h=>({code:h.code,token:hostTokens.get(h.id)}))}));
      socket.onmessage=e=>{if(paused||watchers.get(url)!==w)return;try{const m=JSON.parse(e.data);if(m.op!=='directory')return;for(const entry of m.hosts||[])for(const h of known.filter(h=>h.code===entry.code)){const c=connection(h);c.directoryOnline=entry.online;if(entry.online){c.machine=String(entry.machine||'');c.project=String(entry.project||'');} }loadSelected();renderHosts();renderList();}catch{}};
      socket.onclose=e=>{if(!paused&&e.code!==4000&&watchers.get(url)===w)w.timer=setTimeout(refreshWatchers,10000);};socket.onerror=()=>{};
    }
  }
  function consumeFeed(sid,bytes,replay){
    if(sid!==active||!writer||!controls[sid])return;
    if(replay){replaying++;ready=false;}
    writer.push(replay?C.snapshotBytes(bytes,sid):bytes,replay,()=>{if(sid!==active)return;
      if(replay){replaying=Math.max(0,replaying-1);ready=connected;term.options.disableStdin=!ready;term.scrollToBottom();notice(bytes.length?'有限历史已恢复；全屏程序可能需要自行刷新':'');}
      historyStatus();scheduleFit();
    },controls[sid]);
  }
  function sync(info,c=selected()){
    if(!info.capabilities?.multiplex){stopSocket(c);hostState(c,'后台版本不兼容');return;}
    if(!c.connected){c.connected=true;c.retry=0;c.status='已连接';c.pingTimer=setInterval(()=>{if(c.lastPing&&performance.now()-c.lastPing>30000){M.closeForRecovery(c.socket,'连接探测超时');return;}if(!c.lastPing)c.lastPing=performance.now();sendHost(c,10,encoder.encode('mobile'));},15000);}
    if(info.error)c.status=String(info.error);c.capabilities=info.capabilities||c.capabilities;
    if(info.controls)c.controls=info.controls;
    if(info.machine!==undefined)c.machine=String(info.machine||'');if(info.project!==undefined)c.project=String(info.project||'');
    if(info.groups)c.groups=info.groups;
    if(info.windows)c.inventory=info.windows.flatMap(w=>(w.sessions||[]).map(s=>({...s,title:w.title,window:w.id,group:w.group})));
    if(info.windows){const ids=new Set(c.inventory.map(s=>s.id));for(const sid of c.activity.keys())if(!ids.has(sid))c.activity.delete(sid);}
    if(c!==selected()){renderHosts();renderList();return;}
    loadSelected();renderHosts();renderList();
    if(c.createPending){const created=inventory.find(s=>!c.beforeCreate.has(s.id));if(created){c.createPending=false;createPending=false;activate(created.id);}}
    if(restoringUI&&restoringUI.hostId===hostId&&inventory.some(s=>s.id===restoringUI.activeSid)){const sid=restoringUI.activeSid;restoringUI=null;activate(sid);}
    if(!inventory.some(s=>s.id===active)){if(inventory.length)activate(inventory[0].id);else{disposeTerminal();active='';}}
    if(controls[active]){const size=controls[active];control=!!size.owner;$('control').textContent=control?'主控':'接管';if(term&&(term.cols!==size.cols||term.rows!==size.rows))writer?.push(new Uint8Array(),false,scheduleFit,size);scheduleFit();}
    if(controls[active]&&waitingFeeds.length){const feeds=waitingFeeds;waitingFeeds=[];waitingBytes=0;for(const feed of feeds)consumeFeed(feed.sid,feed.bytes,feed.replay);}
    subscribeSessions(c);
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
    const projects=new Map();for(const c of connections.values()){const project=c.project||'未分配项目';if(!projects.has(project))projects.set(project,[]);projects.get(project).push(c);}
    for(const [project,computers] of projects){const ph=document.createElement('h3');ph.textContent=project;$('session-list').append(ph);
      for(const c of computers){const heading=document.createElement('h4');heading.textContent=(c.machine||c.host.name)+' · '+c.status;$('session-list').append(heading);const groups=new Map();
        for(const s of c.inventory){count++;if(![s.title,s.shell,s.cwd,c.machine,c.project].join(' ').toLocaleLowerCase().includes(query))continue;const key=s.group||s.window||s.title||'窗口';if(!groups.has(key))groups.set(key,[]);groups.get(key).push(s);}
        for(const sessions of groups.values()){
          const group=document.createElement('p');group.className='window-heading';group.textContent=c.groups.find(g=>g.id===sessions[0].group)?.name||sessions[0].title||'窗口';$('session-list').append(group);
          for(const s of sessions){const b=document.createElement('button');b.className='session';b.dataset.host=c.host.id;b.dataset.session=s.id;b.dataset.name=s.title||'未命名窗口';b.dataset.agent=M.agentType(s);b.setAttribute('aria-current',String(c.host.id===hostId&&s.id===active));const dot=document.createElement('i');dot.className='session-dot';dot.setAttribute('aria-hidden','true');const name=document.createElement('span');name.className='session-name';name.textContent=b.dataset.name;const type=document.createElement('small');type.className='session-agent';type.textContent=b.dataset.agent;b.append(dot,name,type);b.onclick=()=>{chooseHost(c.host.id);activate(s.id);$('session-dialog').close();};$('session-list').append(b);}
        }
      }
    }
    $('session-count').textContent=count+' 个终端';$('new').disabled=!connected||createPending;
    updateSessionDots();
  }
  function disposeTerminal(){
    composerSubmit.cancel();
    waitingFeeds=[];waitingBytes=0;
    ready=false;pendingKeys=[];composing=false;replaying=0;lastSize='';control=false;
    setCtrl(false);
    if(fitFrame!==null)cancelAnimationFrame(fitFrame);fitFrame=null;
    if(writer)writer.dispose();writer=null;if(webgl)webgl.dispose();webgl=null;
    if(term)term.dispose();term=null;fit=null;$('screen').replaceChildren();
  }
  function activate(sid){
    if(scrollFrame!==null)cancelAnimationFrame(scrollFrame);scrollFrame=null;scrollRows=0;gesture=M.createGesture();
    if(sid===active&&term)return;
    if(active)drafts.set(sessionKey(),$('draft').value);disposeTerminal();active=sid;selected().subscription='';
    const s=inventory.find(x=>x.id===sid);if(!s)return;$('title').textContent=s.title+' · '+s.shell;
    $('draft').value=drafts.get(sessionKey(sid))||'';if(drafts.size>32)drafts.delete(drafts.keys().next().value);
    const font=Number(localStorage.getItem('sma.mobile.font'))||14;$('font').value=String(Math.max(11,Math.min(22,font)));
    term=new Terminal({allowProposedApi:true,disableStdin:true,convertEol:false,scrollback:20000,fontSize:Number($('font').value),fontFamily:'Consolas, monospace',cursorBlink:false,windowsPty:{backend:'conpty'},theme:{background:'#141414',foreground:'#ededed',cursor:'#ddd'}});
    // Full-screen tools may erase scrollback during repaint; preserve mobile reading history.
    term.parser.registerCsiHandler({final:'J'},params=>params[0]===3);
    fit=new FitAddon.FitAddon();term.loadAddon(fit);term.loadAddon(new Unicode11Addon.Unicode11Addon());term.unicode.activeVersion='11';term.open($('screen'));setRenderer();
    const current=term;
    writer=M.createWriter({write:(bytes,reset,done,grid)=>{if(term!==current)return;
      if(grid){const size=C.terminalSize(grid.cols,grid.rows);if(current.cols!==size.cols||current.rows!==size.rows)current.resize(size.cols,size.rows);}
      if(reset)current.reset();displayGrid();metrics.writes++;current.write(bytes,()=>{done();if(!reset&&bytes.length&&term===current)composerSubmit.output();});
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
  function desiredSize(){
    if(!term||document.hidden||$('screen').clientWidth<48||$('screen').clientHeight<24)return null;
    const cell=term._core._renderService.dimensions.css.cell;if(!cell.width||!cell.height)return null;
    return C.terminalSize(Math.floor(($('screen').clientWidth-12-term._core.viewport.scrollBarWidth)/cell.width),Math.floor(($('screen').clientHeight-5)/cell.height));
  }
  function displayGrid(){
    if(!term?.element)return;const cell=term._core._renderService.dimensions.css.cell;if(!cell.width||!cell.height)return;
    const width=term.cols*cell.width+12+term._core.viewport.scrollBarWidth,height=term.rows*cell.height+5;
    const scale=Math.min($('screen').getBoundingClientRect().width/width,$('screen').getBoundingClientRect().height/height);
    if(scale<=0)return;Object.assign(term.element.style,{width:width+'px',height:height+'px',transformOrigin:'top left',transform:'scale('+scale+')'});
    $('screen').dataset.grid=term.cols+'×'+term.rows;$('screen').dataset.scale=String(scale);
  }
  function requestSize(takeControl=false){
    const desired=desiredSize(),lease=controls[active];if(!desired||!ready||!lease)return;
    if(sizing==='shared'&&!takeControl)return;
    const key=desired.cols+'x'+desired.rows;
    if(!takeControl&&!control&&lease.busy===true&&sizing==='shared')return;
    if(sizing==='phone'&&key!==lastSize)takeControl=true;
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
  function historyStatus(){if(!term)return;const b=term.buffer.active;$('resume').hidden=b.type!=='normal'||b.viewportY>=b.baseY;}
  let gesture=M.createGesture(),scrollRows=0,scrollFrame=null;
  function scroll(rows){if(!rows||!term)return;scrollRows+=rows;if(scrollFrame!==null)return;scrollFrame=requestAnimationFrame(()=>{scrollFrame=null;const n=scrollRows;scrollRows=0;if(!term)return;
    if(term.buffer.active.type==='alternate'){for(let i=0;i<Math.min(12,Math.abs(n));i++)key(M.alternateScroll(n,!!term.modes?.applicationCursorKeysMode));}else term.scrollLines(n);historyStatus();});}
  $('screen').addEventListener('touchstart',e=>{if(e.touches.length!==1)return;gesture.start(e.touches[0].clientX,e.touches[0].clientY);e.preventDefault();},{passive:false});
  $('screen').addEventListener('touchmove',e=>{if(e.touches.length!==1)return;const cell=term?._core?._renderService?.dimensions?.css?.cell;const line=(cell?.height||17)*(Number($('screen').dataset.scale)||1);const move=gesture.move(e.touches[0].clientX,e.touches[0].clientY,line);if(move.drag)scroll(move.rows);e.preventDefault();},{passive:false});
  $('screen').addEventListener('touchend',e=>{if(gesture.end()){if(modes.get(sessionKey())==='buffered')setMode(true);else term?.focus();}e.preventDefault();},{passive:false});
  $('screen').addEventListener('wheel',e=>{e.preventDefault();scroll(Math.sign(e.deltaY)*3);},{passive:false});
  $('sessions').onclick=()=>{renderHosts();renderList();$('session-dialog').showModal();};$('sessions-close').onclick=()=>$('session-dialog').close();
  $('session-search').oninput=renderList;
  let editingHost=null;
  function editHost(h=null){editingHost=h;$('host-form').reset();$('host-name').value=h?.name||'';$('host-endpoint').value=h?.endpoint||selected().host.endpoint;$('host-code').value=h?.code||'';$('host-token').value=h?hostTokens.get(h.id)||'':'';$('host-error').hidden=true;$('session-dialog').close();$('host-dialog').showModal();}
  $('host-add').onclick=()=>editHost();
  // A credential belongs to an exact normalized endpoint and room. Editing either requires re-entry.
  for(const id of ['host-endpoint','host-code'])$(id).oninput=()=>{if(editingHost)$('host-token').value='';};
  const closeHost=()=>{$('host-token').value='';editingHost=null;$('host-dialog').close();$('session-dialog').showModal();};
  $('host-cancel').onclick=closeHost;$('host-dialog').addEventListener('cancel',e=>{e.preventDefault();closeHost();});
  $('host-form').onsubmit=e=>{e.preventDefault();try{
    const u=new URL($('host-endpoint').value.trim());if(!['http:','https:'].includes(u.protocol)||u.username||u.password||u.search||u.hash)throw new Error('请填写有效 HTTP(S) 地址');
    const name=$('host-name').value.trim();if(!name)throw new Error('请填写电脑名称');if(!editingHost&&hosts.length>=30)throw new Error('最多保存30台电脑');
    const h=editingHost||{id:'host-'+Array.from(crypto.getRandomValues(new Uint32Array(4)),n=>n.toString(16)).join('-')};const old=editingHost&&targetKey(h);
    Object.assign(h,{name,endpoint:u.toString().replace(/\/+$/,''),code:$('host-code').value.trim()});if(old&&old!==targetKey(h)){
      const c=connection(h);stopSocket(c);hostTokens.delete(h.id);
      c.inventory=[];c.groups=[];c.controls={};c.machine='';c.project='';c.activity.clear();c.createPending=false;c.beforeCreate.clear();c.capabilities=null;c.directoryOnline=undefined;c.retry=0;
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
  const keys={escape:'\x1b',tab:'\t',f2:'\x1bOQ',up:'\x1b[A',down:'\x1b[B',left:'\x1b[D',right:'\x1b[C'};
  for(const b of document.querySelectorAll('[data-key]')){b.onpointerdown=e=>e.preventDefault();b.onclick=()=>key(keys[b.dataset.key]);}
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
  $('bottom').onclick=()=>{term?.scrollToBottom();historyStatus();};
  $('copy').onclick=()=>{if(!term)return;let text=term.getSelection();if(!text){const b=term.buffer.active;text=Array.from({length:term.rows},(_,i)=>b.getLine(b.viewportY+i)?.translateToString(true)||'').join('\n');}
    $('copy-text').value=text;$('settings-dialog').close();$('copy-dialog').showModal();$('copy-text').focus();$('copy-text').select();};$('copy-close').onclick=()=>$('copy-dialog').close();
  new ResizeObserver(scheduleFit).observe($('screen'));window.visualViewport?.addEventListener('resize',scheduleFit);
  function pause(){if(paused)return;paused=true;clearInterval(activityTimer);activityTimer=null;if(active)drafts.set(sessionKey(),$('draft').value);for(const c of connections.values())stopSocket(c);refreshWatchers();writer?.dispose();if(fitFrame!==null)cancelAnimationFrame(fitFrame);fitFrame=null;if(scrollFrame!==null)cancelAnimationFrame(scrollFrame);scrollFrame=null;state('后台已暂停连接；服务器终端继续运行');}
  function resume(){if(!paused)return;paused=false;startActivityTimer();for(const c of connections.values())connect(c);refreshWatchers();}
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
  function restoreUIState(value){
    if(!value||value.version!==1)return;
    const h=[home,...hosts].find(h=>h.id===value.hostId);if(!h)return;
    restoringUI={hostId:h.id,activeSid:value.activeSid};chooseHost(h.id);
    for(const [key,text] of value.drafts||[])drafts.set(key,text);
    for(const [key,mode] of value.modes||[])modes.set(key,mode);
    for(const [key,text] of value.uncertainDrafts||[])uncertainDrafts.set(key,text);
    if(inventory.some(s=>s.id===value.activeSid)){restoringUI=null;activate(value.activeSid);$('draft').value=drafts.get(sessionKey())||'';setMode(false);}
  }
  window.MobileTerminal={pause,resume,captureUIState,restoreUIState,prepareUIReload,cancelUIReload,openNotification,reconnect:()=>{if(paused)resume();else{connect();refreshWatchers();}},chooseHost,get hosts(){return [...connections.values()].map(c=>({id:c.host.id,connected:c.connected,status:c.status,sessions:c.inventory.map(s=>s.id),subscription:c.subscription}));},get terminal(){return term;},get active(){return active;},get activeKey(){return sessionKey();},get connected(){return connected;},get ready(){return ready;},get controls(){return controls[active]||null;},revision:config.uiRevision||'bundled',metrics};
  for(const h of [home,...hosts])connection(h);
  if(document.hidden){paused=true;state('Background UI paused');renderHosts();renderList();}
  else{startActivityTimer();for(const c of connections.values())connect(c);refreshWatchers();}
})();
