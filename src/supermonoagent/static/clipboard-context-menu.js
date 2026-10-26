/* Explicit clipboard actions for terminals and ordinary text editors. */
(()=>{
  'use strict';
  if(window.SuperMonoClipboardContext||!window.smaWorkspace)return;
  const W=window.smaWorkspace,menu=document.createElement('div'),notice=document.createElement('div'),style=document.createElement('style');
  menu.id='clipboard-context-menu';menu.setAttribute('role','menu');menu.hidden=true;
  notice.id='clipboard-context-status';notice.setAttribute('role','status');notice.hidden=true;
  style.textContent='#clipboard-context-menu{position:fixed;z-index:5000;min-width:170px;padding:4px;background:#242424;border:1px solid #626262;border-radius:5px;box-shadow:0 6px 20px #0008}#clipboard-context-menu[hidden],#clipboard-context-status[hidden]{display:none}#clipboard-context-menu button{display:block;width:100%;padding:7px 10px;text-align:left;color:#eee;background:transparent;border:0}#clipboard-context-menu button:disabled{color:#777}#clipboard-context-status{position:fixed;bottom:42px;right:18px;z-index:5001;max-width:min(440px,90vw);background:#292929;color:#eee;border:1px solid #555;padding:9px;overflow-wrap:anywhere}';
  const copy=document.createElement('button'),paste=document.createElement('button');
  copy.type=paste.type='button';copy.textContent='复制选中文本   Ctrl+C';paste.textContent='粘贴到光标处   Ctrl+V';
  copy.id='clipboard-context-copy';paste.id='clipboard-context-paste';copy.setAttribute('role','menuitem');paste.setAttribute('role','menuitem');menu.append(copy,paste);
  document.head.append(style);document.body.append(menu,notice);
  let current=null,timer=0,dead=false;
  const api=()=>window.pywebview?.api;
  function mount(target){const parent=target?.dialog?.open?target.dialog:document.body;parent.append(menu,notice);}
  function message(text,target){if(dead)return;mount(target);notice.textContent=text;notice.hidden=false;clearTimeout(timer);timer=setTimeout(()=>notice.hidden=true,4500);}
  function close(){menu.hidden=true;current=null;}
  function paneIdentity(pane){return pane?{pane,host:pane.host,ws:pane.host.ws,generation:pane.generation,hostGeneration:pane.host.generation,token:pane.host.config?.token,raw:pane.raw}:null;}
  function samePane(value){return !value||W.panes.get(value.pane.sid)===value.pane&&value.pane.host===value.host&&value.host.ws===value.ws&&value.pane.generation===value.generation&&value.host.generation===value.hostGeneration&&value.host.config?.token===value.token&&value.pane.raw===value.raw;}
  function locate(element){
    if(!(element instanceof Element)||element.closest('#clipboard-context-menu'))return null;
    const field=element.closest('textarea,input');
    if(field&&!field.closest('.terminal-screen')&&!field.disabled&&(field.tagName==='TEXTAREA'||['text','search','url','email','tel','password'].includes(field.type))){
      const start=field.selectionStart,end=field.selectionEnd;
      if(!Number.isInteger(start)||!Number.isInteger(end))return null;
      return {kind:'field',field,start,end,value:field.value,dialog:field.closest('dialog'),identity:paneIdentity(W.activePane),selected:field.type==='password'?'':field.value.slice(start,end),canPaste:!field.readOnly};
    }
    const screen=element.closest('.terminal-screen');
    const pane=screen&&[...W.panes.values()].find(value=>value.screen===screen);
    if(!pane)return null;
    let fileTarget=null;try{fileTarget=window.SuperMonoFilePaste?.captureTarget?.();}catch{}
    return {kind:'terminal',identity:paneIdentity(pane),selected:pane.term.getSelection(),canPaste:pane.ready&&pane.host.connected&&!pane.term.options.disableStdin,fileTarget};
  }
  function valid(target){
    if(dead||!target||!samePane(target.identity))return false;
    if(target.kind==='terminal')return W.activePane===target.identity.pane&&target.identity.pane.ready&&target.identity.host.connected&&target.identity.ws?.readyState===1;
    return target.field.isConnected&&!target.field.disabled&&(!target.dialog||target.dialog.open)&&(!target.identity||W.activePane===target.identity.pane)&&target.field.value===target.value&&target.field.selectionStart===target.start&&target.field.selectionEnd===target.end;
  }
  async function copyText(target){
    if(!target?.selected||target.selected.length>524288)return;
    try{if(typeof api()?.clipboard_write_text==='function')await api().clipboard_write_text(target.selected);else await navigator.clipboard.writeText(target.selected);message('已复制选中文本。',target);if(valid(target)){if(target.kind==='terminal')target.identity.pane.term.focus();else target.field.focus();}}
    catch{message('复制未完成，请重试。',target);}
  }
  async function pasteText(target){
    if(!valid(target)||!target.canPaste)return;
    try{
      if(target.kind==='field'&&target.field.id==='terminal-composer-text'&&window.SuperMonoTerminalComposer?.pasteClipboard){await window.SuperMonoTerminalComposer.pasteClipboard();return;}
      const native=api();
      if(target.kind==='terminal'&&typeof native?.clipboard_has_files==='function'&&await native.clipboard_has_files()){
        if(!valid(target))throw Error('target_changed');
        if(!target.fileTarget||!window.SuperMonoFilePaste?.uploadClipboard)throw Error('files_unavailable');
        target.identity.pane.term.focus();
        await window.SuperMonoFilePaste.uploadClipboard({target:target.fileTarget});return;
      }
      const text=typeof native?.clipboard_read_text==='function'?await native.clipboard_read_text():await navigator.clipboard.readText();
      if(!valid(target)||(target.kind==='field'&&target.field.readOnly)||(target.kind==='terminal'&&target.identity.pane.term.options.disableStdin)||typeof text!=='string'||text.length>524288)throw Error('target_changed');
      if(!text){message('剪贴板中没有可粘贴的文本。',target);return;}
      if(target.kind==='terminal'){target.identity.pane.term.paste(text);target.identity.pane.term.focus();}
      else{const limit=target.field.maxLength,remaining=limit<0?text.length:Math.max(0,limit-(target.value.length-(target.end-target.start)));target.field.focus();target.field.setSelectionRange(target.start,target.end);target.field.setRangeText(text.slice(0,remaining),target.start,target.end,'end');target.field.dispatchEvent(new Event('input',{bubbles:true}));}
    }catch{message('粘贴未完成。请检查目标窗口；附件需要先设置项目目录和文件助手。',target);}
  }
  function context(event){const target=locate(event.target);if(!target)return;event.preventDefault();event.stopPropagation();mount(target);current=target;copy.disabled=!target.selected;paste.disabled=!target.canPaste;menu.hidden=false;
    menu.style.left=Math.max(4,Math.min(event.clientX,innerWidth-menu.offsetWidth-4))+'px';menu.style.top=Math.max(4,Math.min(event.clientY,innerHeight-menu.offsetHeight-4))+'px';(copy.disabled?paste:copy).focus();}
  function down(event){if(!event.target.closest?.('#clipboard-context-menu'))close();}
  function dialogClosed(event){if(event.target===menu.closest('dialog'))close();}
  function filePaste(event){
    if(event.target?.id!=='terminal-composer-text'||event.target.disabled||event.target.readOnly||!event.clipboardData?.files?.length||!window.SuperMonoTerminalComposer?.addFiles)return;
    event.preventDefault();event.stopPropagation();
    window.SuperMonoTerminalComposer.addFiles([...event.clipboardData.files]);
  }
  function key(event){
    if(!menu.hidden&&event.key==='Escape'){event.preventDefault();event.stopPropagation();const target=current;close();if(valid(target)){if(target.kind==='terminal')target.identity.pane.term.focus();else target.field.focus();}return;}
    if(!event.ctrlKey||event.metaKey||event.altKey||event.shiftKey)return;
    const target=locate(event.target);if(!target)return;
    const letter=event.key.toLowerCase();
    if(letter==='c'&&target.selected&&typeof api()?.clipboard_write_text==='function'){event.preventDefault();event.stopPropagation();copyText(target);}
    else if(letter==='v'&&target.kind==='field'&&typeof api()?.clipboard_read_text==='function'){event.preventDefault();event.stopPropagation();pasteText(target);}
  }
  copy.onclick=()=>{const target=current;close();copyText(target);};paste.onclick=()=>{const target=current;close();pasteText(target);};
  document.addEventListener('contextmenu',context,true);document.addEventListener('pointerdown',down,true);document.addEventListener('keydown',key,true);document.addEventListener('close',dialogClosed,true);document.addEventListener('paste',filePaste,true);
  function destroy(){if(dead)return;dead=true;clearTimeout(timer);document.removeEventListener('contextmenu',context,true);document.removeEventListener('pointerdown',down,true);document.removeEventListener('keydown',key,true);document.removeEventListener('close',dialogClosed,true);document.removeEventListener('paste',filePaste,true);window.removeEventListener('beforeunload',destroy);menu.remove();notice.remove();style.remove();delete window.SuperMonoClipboardContext;}
  window.SuperMonoClipboardContext={destroy};window.addEventListener('beforeunload',destroy,{once:true});
})();
