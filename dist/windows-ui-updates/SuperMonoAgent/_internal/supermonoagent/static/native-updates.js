/* Bundled native boundary. This file is never part of a downloadable UI snapshot. */
(() => {
  'use strict';
  const script=document.currentScript;
  if(!script?.dataset.nonce)return;
  const NativeSocket=window.WebSocket;
  const boot=Object.freeze({nonce:script.dataset.nonce,revision:script.dataset.revision,
    ticket:script.dataset.ticket,generation:Number(script.dataset.generation),sequence:Number(script.dataset.sequence)});
  const interval=6*60*60*1000,pending=new Map();
  let socket=null,sequence=0,connecting=null,lastCheck=0,latest=null,failed=false,acknowledged=false,readyStarted=false;
  let panel=null,message=null,download=null,restore=null,toast=null,trigger=null,composing=false,installing=false;
  const waitFrame=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
  const isComposing=()=>composing||window.smaWorkspace?.isComposing===true;
  const show=text=>{if(message)message.textContent=text;};
  function render(status){
    latest=status;show(status.message||'界面更新');
    if(download)download.disabled=!status.available||installing;
    if(restore)restore.disabled=!status.previous_revision||installing;
    if(toast){toast.hidden=!status.available||!panel.hidden;toast.textContent=status.available?'发现界面更新 '+status.available.revision+'；可从顶部检查更新菜单下载。':'';}
    if(trigger){trigger.title=status.available?'发现可用更新：'+status.available.revision:'检查更新';trigger.style.color=status.available?'#a7dcb5':'';}
  }
  function connect(){
    if(socket?.readyState===NativeSocket.OPEN)return Promise.resolve(socket);
    if(connecting)return connecting;
    connecting=new Promise((resolve,reject)=>{
      const url=new URL('/__updates/ws',location.origin);url.protocol=location.protocol==='https:'?'wss:':'ws:';
      const current=new NativeSocket(url);socket=current;
      current.onopen=()=>{connecting=null;resolve(current);};
      current.onerror=()=>{connecting=null;reject(new Error('本机更新服务暂不可用。'));};
      current.onclose=()=>{
        connecting=null;for(const operation of pending.values()){clearTimeout(operation.timer);operation.reject(new Error('本机更新连接已关闭。'));}pending.clear();
        if(boot.ticket&&!acknowledged)setTimeout(()=>request('status').then(checkRollback).catch(()=>{}),1000);
      };
      current.onmessage=event=>{
        let data;try{data=JSON.parse(event.data);}catch{return;}
        if(data.event==='rollback'){if(data.failed_revision===boot.revision)location.reload();return;}
        const operation=pending.get(data.id);if(!operation)return;
        pending.delete(data.id);clearTimeout(operation.timer);render(data);checkRollback(data);
        if(data.ok===false)operation.reject(new Error(data.error||'更新操作失败。'));else operation.resolve(data);
      };
    });
    return connecting;
  }
  async function request(op,body={}){
    const current=await connect(),id=++sequence;
    return new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{pending.delete(id);reject(new Error('更新服务响应超时。'));},60000);
      pending.set(id,{resolve,reject,timer});current.send(JSON.stringify({id,op,nonce:boot.nonce,...body}));
    });
  }
  function checkRollback(status){
    if(boot.ticket&&status.active_revision!==boot.revision&&status.failed_sequences?.includes(boot.sequence))location.reload();
  }
  function capture(){
    const state=window.smaWorkspace?.captureUIState?.();if(!state)return;
    // Only workspace navigation state crosses reload. Never copy account objects or input credentials.
    const tree=(value,depth=0)=>{
      if(depth>16)return null;if(typeof value==='string')return value;
      if(!value||!['horizontal','vertical'].includes(value.direction))return null;
      return {direction:value.direction,ratio:Math.max(15,Math.min(85,Number(value.ratio)||50)),a:tree(value.a,depth+1),b:tree(value.b,depth+1)};
    };
    const safe={selectedHost:state.selectedHost,active:state.active,tabs:Array.isArray(state.tabs)?state.tabs.slice(0,128):[],layout:tree(state.layout),renderer:state.renderer};
    try{const raw=JSON.stringify(safe);if(raw.length<=65536)sessionStorage.setItem('sma.workspace.ui-state',raw);}catch{}
  }
  async function waitForInput(){
    while(isComposing()){
      show('更新已验证，正在等待输入法组合完成…');
      await new Promise(resolve=>setTimeout(resolve,100));
    }
    await waitFrame();
    if(isComposing())return waitForInput();
  }
  async function downloadAndInstall(){
    if(installing)return;installing=true;if(download)download.disabled=true;
    let resume=null;
    try{
      show('正在下载并验证完整界面包；终端输入可继续…');
      const prepared=await request('download');await waitForInput();capture();
      resume=window.smaWorkspace?.pauseForUIUpdate?.();
      const result=await request('activate',{prepared:prepared.prepared});if(result.reload)location.reload();
    }catch(error){resume?.();show(error.message);installing=false;if(latest)render({...latest,message:error.message});}
  }
  async function rollback(){
    if(installing)return;await waitForInput();capture();const resume=window.smaWorkspace?.pauseForUIUpdate?.();
    try{const result=await request('rollback');if(result.reload)location.reload();}catch(error){resume?.();show(error.message);}
  }
  async function check(automatic=false){
    if(automatic&&(document.visibilityState!=='visible'||Date.now()-lastCheck<interval))return latest;
    lastCheck=Date.now();if(!automatic&&panel)panel.hidden=false;
    try{return await request('check',{automatic});}catch(error){show(error.message);return null;}
  }
  async function uiReady(){
    if(readyStarted||failed)return;readyStarted=true;
    await waitFrame();
    const app=document.querySelector('#app'),rect=app?.getBoundingClientRect();
    if(failed||!window.smaWorkspace||!rect?.width||!rect?.height)return;
    if(!boot.ticket){acknowledged=true;return;}
    try{await request('ready',{ticket:boot.ticket,revision:boot.revision,generation:boot.generation});acknowledged=true;check(true);}
    catch(error){
      try{
        const status=await request('status');
        if(!status.pending&&status.active_revision===boot.revision&&status.generation===boot.generation&&!status.failed_sequences?.includes(boot.sequence)){
          acknowledged=true;check(true);return;
        }
      }catch{}
      show(error.message);
    }
  }
  function fatal(event){
    if(!boot.ticket||acknowledged||failed)return;
    if(event.type==='unhandledrejection'&&/failed to fetch|networkerror|load failed/i.test(String(event.reason?.message||event.reason||'')))return;
    failed=true;request('fail',{ticket:boot.ticket,revision:boot.revision,generation:boot.generation}).then(()=>location.reload()).catch(()=>{});
  }
  addEventListener('error',fatal,true);addEventListener('unhandledrejection',fatal);
  addEventListener('compositionstart',()=>{composing=true;},true);addEventListener('compositionend',()=>{composing=false;},true);
  if(boot.ticket)setInterval(()=>{if(!acknowledged)request('status').then(checkRollback).catch(()=>{});},1000);
  Object.defineProperty(window,'NativeUpdates',{value:Object.freeze({uiReady,check:()=>check(false),downloadAndInstall,rollback,revision:boot.revision}),writable:false,configurable:false});
  function mount(){
    const host=document.createElement('div');host.id='native-updates-host';
    Object.assign(host.style,{position:'fixed',right:'12px',top:'48px',zIndex:'2147483647'});document.documentElement.append(host);
    const root=host.attachShadow({mode:'open'});
    root.innerHTML='<style>:host{font:13px "Segoe UI",sans-serif;color:#e5e5e5}section,aside{width:min(360px,calc(100vw - 24px));box-sizing:border-box;padding:16px;background:#202020;border:1px solid #555;border-radius:8px;box-shadow:0 10px 36px #0008}aside{line-height:1.6}h2{font-size:16px;margin:0 0 10px}p{line-height:1.6;overflow-wrap:anywhere}button{font:inherit;color:inherit;border:1px solid #555;background:#303030;border-radius:5px;padding:7px 10px;cursor:pointer;margin:3px}button:disabled{opacity:.45}button.primary{background:#ddd;color:#111}header{display:flex;justify-content:space-between;align-items:start}[hidden]{display:none!important}</style><aside id="toast" role="status" hidden></aside><section hidden><header><h2>界面更新</h2><button id="close" aria-label="关闭更新菜单">×</button></header><p role="status">正在读取本机更新状态…</p><button id="check">检查更新</button><button id="download" class="primary" disabled>下载并更新</button><button id="restore" disabled>恢复上一版本</button></section>';
    panel=root.querySelector('section');message=root.querySelector('p');download=root.querySelector('#download');restore=root.querySelector('#restore');toast=root.querySelector('#toast');
    root.querySelector('#close').onclick=()=>{panel.hidden=true;};root.querySelector('#check').onclick=()=>check(false);download.onclick=downloadAndInstall;restore.onclick=rollback;
    trigger=document.querySelector('#check-updates');
    if(!trigger){trigger=document.createElement('button');trigger.textContent='↥';trigger.title='检查更新';trigger.setAttribute('aria-label','检查更新');root.prepend(trigger);}
    trigger.hidden=false;trigger.onclick=()=>{panel.hidden=!panel.hidden;if(!panel.hidden){toast.hidden=true;request('status').catch(error=>show(error.message));}};
    request('status').then(checkRollback).catch(error=>show(error.message));if(!boot.ticket)check(true);
    setInterval(()=>check(true),interval);
    document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')check(true);});
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',mount,{once:true});else mount();
})();
