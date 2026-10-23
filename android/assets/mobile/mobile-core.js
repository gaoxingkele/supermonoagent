(function(root){
  'use strict';
  const {highlightSpans,createHighlighter}=typeof module!=='undefined'&&module.exports
    ?require('../../../src/supermonoagent/static/terminal-client.js'):root.TerminalClient;
  function createWriter({write,schedule,cancel,onOverflow=()=>{},limit=524288,chunk=32768}){
    let queue=[],pending=0,busy=false,frame=null,dead=false;
    function drain(){
      frame=null;if(dead||busy||!queue.length)return;
      const reset=queue[0].reset,grid=queue[0].grid,parts=[],callbacks=[];let size=0;
      while(queue.length&&size<chunk){
        const item=queue[0];if(parts.length&&(item.reset||item.grid?.cols!==grid?.cols||item.grid?.rows!==grid?.rows))break;
        const n=Math.min(item.bytes.length-item.offset,chunk-size);
        parts.push(item.bytes.subarray(item.offset,item.offset+n));size+=n;item.offset+=n;item.reset=false;
        if(item.offset===item.bytes.length){queue.shift();callbacks.push(item.done);}else break;
      }
      const bytes=new Uint8Array(size);let at=0;for(const part of parts){bytes.set(part,at);at+=part.length;}
      busy=true;
      write(bytes,reset,()=>{if(dead)return;pending-=size;busy=false;for(const done of callbacks)done();if(queue.length&&frame===null)frame=schedule(drain);},grid);
    }
    return {push(bytes,reset=false,done=()=>{},grid=null){
      if(dead)return false;
      if(pending+bytes.length>limit){onOverflow();return false;}
      pending+=bytes.length;queue.push({bytes,reset,done,grid,offset:0});if(!busy&&frame===null)drain();return true;
    },dispose(){dead=true;if(frame!==null)cancel(frame);queue=[];pending=0;},get pending(){return pending;},get idle(){return !busy&&!queue.length;}};
  }
  function createGesture(threshold=10){
    let x=0,y=0,lastY=0,remainder=0,drag=false;
    return {start(px,py){x=px;y=lastY=py;remainder=0;drag=false;},move(px,py,line){
      if(!drag&&Math.hypot(px-x,py-y)<threshold)return {rows:0,drag:false};
      drag=true;remainder+=lastY-py;lastY=py;const rows=Math.trunc(remainder/Math.max(1,line));remainder-=rows*line;
      return {rows,drag:true};},end(){return !drag;}};
  }
  function controlKey(text){return /^[a-z@\[\]\\^_]$/i.test(text)?String.fromCharCode(text.toUpperCase().charCodeAt(0)&31):text;}
  function composerAction(e,composing=false){
    if(composing||e.isComposing)return 'none';
    if(e.type==='beforeinput')return ['insertLineBreak','insertParagraph'].includes(e.inputType)?'send':'none';
    return e.key!=='Enter'?'none':e.shiftKey?'newline':'send';
  }
  function createComposerSubmitter({schedule=setTimeout,cancel=clearTimeout,onPending=()=>{}}={}){
    let pending=null;
    function clear(){if(!pending)return;for(const timer of [pending.timer,pending.timeout])if(timer!==null)cancel(timer);pending=null;onPending(false);}
    function arm(job){
      if(pending!==job||job.timer!==null)return;
      if(!job.isCurrent()){clear();return;}
      if(job.timeout!==null){cancel(job.timeout);job.timeout=null;}
      job.timer=schedule(()=>{if(pending!==job)return;clear();if(job.isCurrent()){if(job.enter()!==false)job.onSent();}},200);
    }
    function output(){
      const job=pending;if(!job||!job.echo||job.timer!==null)return;
      if(!job.isCurrent()){clear();return;}
      if(job.echo())arm(job);
    }
    return {submit(text,{paste,enter,isCurrent,echo=null,onTimeout=()=>{},onSent=()=>{}}){
      if(!text||pending)return false;
      const job={timer:null,timeout:null,enter,isCurrent,echo,onSent};pending=job;onPending(true);
      try{paste(text);}catch(error){clear();throw error;}
      // ConPTY removes paste markers. A stalled TUI can consume spaced writes
      // together, so editor submissions must confirm remote echo before Enter.
      if(echo){job.timeout=schedule(()=>{if(pending!==job)return;clear();onTimeout();},5000);output();}
      else arm(job);
      return true;
    },output,cancel:clear,get pending(){return pending!==null;}};
  }
  function createComposerEcho(term,text){
    const parts=text.replace(/\r\n?/g,'\n').split('\n').map(line=>line.replace(/\s/g,'')).filter(Boolean);
    const needle=(parts[parts.length-1]||'').slice(-128);
    function read(){
      const b=term.buffer.active,end=b.baseY+b.cursorY,lines=[];
      for(let row=Math.max(b.baseY,end-100);row<=end;row++){
        const line=b.getLine(row);if(line)lines.push(line.translateToString(true,0,row===end?b.cursorX:undefined));
      }
      return lines.join('').replace(/\s/g,'');
    }
    const count=(value,part)=>part?value.split(part).length-1:0;
    const placeholders=value=>[...value.matchAll(/\[Pasted(?:Content|text)[^\]]{0,64}\]/gi)].map(match=>match[0]).join('|');
    const before=read(),copies=count(before,needle),previousPlaceholder=placeholders(before);
    return ()=>{
      const after=read();
      return !needle||count(after,needle)>copies||(after!==before&&after.endsWith(needle))
        ||(text.length>1000&&placeholders(after)!==previousPlaceholder&&placeholders(after)!=='');
    };
  }
  function alternateScroll(rows,application){return '\x1b'+(application?'O':'[')+(rows<0?'A':'B');}
  function closeForRecovery(socket,reason){if(socket)socket.close(4001,reason);}
  function sessionState(connected,lastOutput,now){
    return !connected?'disconnected':lastOutput!==undefined&&now-lastOutput<1800?'refreshing':'waiting';
  }
  function agentType(session,windowTitle=''){
    const values=[session.agent,session.learnedAgent,session.command,session.argv,session.title,windowTitle,session.shell];
    for(const value of values){
      const text=Array.isArray(value)?value.join(' '):String(value||'');
      for(const [pattern,label] of [[/\bcodex\b/i,'Codex'],[/\bclaude(?:[ _-]code)?\b/i,'Claude Code'],[/\bkimi\b/i,'Kimi'],[/\bgemini\b/i,'Gemini'],[/\bopen[ -]?code\b/i,'OpenCode'],[/\bcopilot\b/i,'Copilot'],[/\baider\b/i,'Aider']])if(pattern.test(text))return label;
    }
    return '其他';
  }
  const api={highlightSpans,createHighlighter,createWriter,createGesture,controlKey,composerAction,createComposerSubmitter,createComposerEcho,alternateScroll,closeForRecovery,sessionState,agentType};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.MobileCore=api;
})(typeof window==='undefined'?globalThis:window);
