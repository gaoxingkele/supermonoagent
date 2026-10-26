(function(root){
  'use strict';
  const {highlightSpans,createHighlighter,terminalInput:filterTerminalInput}=typeof module!=='undefined'&&module.exports
    ?require('../../../src/supermonoagent/static/terminal-client.js'):root.TerminalClient;
  function createWriter({write,schedule,cancel,onOverflow=()=>{},limit=524288,chunk=32768}){
    let queue=[],pending=0,busy=false,frame=null,dead=false;
    function drain(){
      frame=null;if(dead||busy||!queue.length)return;
      const reset=queue[0].reset,grid=queue[0].grid,parts=[],callbacks=[],progress=[];let size=0;
      while(queue.length&&size<chunk){
        const item=queue[0];if(parts.length&&(item.reset||item.grid?.cols!==grid?.cols||item.grid?.rows!==grid?.rows||(item.grid?.epoch??item.grid?.history_epoch)!==(grid?.epoch??grid?.history_epoch)))break;
        const n=Math.min(item.bytes.length-item.offset,chunk-size);
        parts.push(item.bytes.subarray(item.offset,item.offset+n));size+=n;item.offset+=n;item.reset=false;
        progress.push({grid:item.grid,offset:item.offset,complete:item.offset===item.bytes.length});
        if(item.offset===item.bytes.length){queue.shift();callbacks.push(item.done);}else break;
      }
      const bytes=new Uint8Array(size);let at=0;for(const part of parts){bytes.set(part,at);at+=part.length;}
      busy=true;
      write(bytes,reset,()=>{if(dead)return;pending-=size;busy=false;for(const done of callbacks)done();if(queue.length&&frame===null)frame=schedule(drain);},grid,progress);
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
  function terminalInput(text,replaying=false){const filtered=filterTerminalInput(text,replaying);return filtered.startsWith('\x1b[200~')?filtered:filtered.replace(/\x1b\[[IO]/g,'');}
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
      for(const [pattern,label] of [[/\bcodex\b/i,'Codex'],[/\bclaude(?:[ _-]code)?\b/i,'Claude Code'],[/\bgrok\b/i,'Grok'],[/\bkimi\b/i,'Kimi'],[/\bpi[._ -]dev\b/i,'pi.dev'],[/\bcursor[-_ ]cli\b/i,'Cursor CLI'],[/\bgemini\b/i,'Gemini'],[/\bopen[ -]?code\b/i,'OpenCode'],[/\bcopilot\b/i,'Copilot'],[/\baider\b/i,'Aider']])if(pattern.test(text))return label;
    }
    return '其他';
  }
  function debugGridPage(rows,page,capacity){
    capacity=capacity===9?9:capacity===25?25:16;
    const unique=[...new Map(rows.map(row=>[row.key,row])).values()];
    const pages=Math.max(1,Math.ceil(unique.length/capacity));
    page=Math.max(0,Math.min(pages-1,Math.floor(Number(page)||0)));
    return {items:unique.slice(page*capacity,(page+1)*capacity),page,pages,total:unique.length,capacity};
  }
  function debugSortEntries(rows){
    return rows.map((row,index)=>({row,index})).sort((a,b)=>{
      const activity=Number(!!b.row.active)-Number(!!a.row.active);if(activity)return activity;
      const time=row=>Number.isFinite(row.updatedAt)&&row.updatedAt>0?row.updatedAt:0;
      return time(b.row)-time(a.row)||(Number.isFinite(a.row.order)&&Number.isFinite(b.row.order)?a.row.order-b.row.order:0)||a.index-b.index;
    }).map(item=>item.row);
  }
  function debugSwipePage(start,end,page,pages){
    const dx=end.x-start.x,dy=end.y-start.y,swiped=Math.abs(dx)>=48&&Math.abs(dx)>Math.abs(dy)*1.3;
    return {page:swiped?Math.max(0,Math.min(Math.max(1,pages)-1,page+(dx<0?1:-1))):page,swiped};
  }
  function debugRecoveryFeed(bytes){
    if(bytes.length<13)return false;
    const decoder=new TextDecoder(),prefix=decoder.decode(bytes.subarray(0,64));
    return /^(?:\x1b\]7501;app=codex\x07)?\x1b7\x1b\[\?6l/.test(prefix)&&decoder.decode(bytes.subarray(bytes.length-6))==='\x1b[0m\x1b8';
  }
  function visibleBufferLine(line,cols){
    if(!line)return '';
    if(typeof line.getCell!=='function')return line.translateToString(true);
    let text='';
    for(let x=0;x<cols;x++){
      const cell=line.getCell(x);if(!cell){text+=' ';continue;}
      const width=cell.getWidth();if(!width)continue;
      text+=cell.isInvisible()?' '.repeat(width):cell.getChars()||' '.repeat(width);
    }
    return text.replace(/[\x00-\x1f\x7f-\x9f]/g,'').trimEnd();
  }
  function historyPayload(payload){
    if(!(payload instanceof Uint8Array)||payload.length>262143)throw Error('Invalid history frame');
    const end=payload.indexOf(0);if(end<1||end>256||end+3>payload.length)throw Error('Invalid history header');
    const decode=new TextDecoder('utf-8',{fatal:true}),session=decode.decode(payload.subarray(0,end));
    if(/[\x00-\x1f\x7f]/.test(session))throw Error('Invalid history session');
    const length=payload[end+1]|payload[end+2]<<8,start=end+3;
    if(length<2||length>4096||start+length>payload.length)throw Error('Invalid history metadata length');
    const context=JSON.parse(decode.decode(payload.subarray(start,start+length)));
    const fields=context?.v===2?'cols,end_position,epoch,kind,partial,reason,rows,stream_id,v':'cols,epoch,kind,partial,reason,rows,v';
    if(!context||Array.isArray(context)||Object.keys(context).sort().join(',')!==fields||![1,2].includes(context.v)||!(context.v===2?['snapshot','output','resume']:['snapshot','output']).includes(context.kind)||!Number.isInteger(context.cols)||context.cols<2||context.cols>500||!Number.isInteger(context.rows)||context.rows<1||context.rows>200||!Number.isSafeInteger(context.epoch)||context.epoch<0||typeof context.partial!=='boolean'||typeof context.reason!=='string'||!/^[a-z_]{0,64}$/.test(context.reason)||context.partial!==!!context.reason||context.v===2&&(!Number.isSafeInteger(context.end_position)||context.end_position<0||typeof context.stream_id!=='string'||!/^[a-f0-9]{24}$/.test(context.stream_id)))throw Error('Invalid history metadata');
    return {session,context,bytes:payload.subarray(start+length)};
  }
  function safeHistoryReplay(bytes,grid,partial=false,baselineOnly=false){
    const unchanged=reason=>({bytes,partial:partial||!!reason,recovered:false,baseline:false,reason});
    if(!Number.isInteger(grid?.cols)||!Number.isInteger(grid?.rows))return unchanged('invalid_grid');
    let raw='';for(let i=0;i<bytes.length;i+=8192)raw+=String.fromCharCode(...bytes.subarray(i,i+8192));
    const tokens=[];let i=0,foreign=0,lastForeign=-1;
    // Ignore CSI-looking text inside OSC/DCS strings, including unterminated ones.
    while(i<raw.length){
      const at=raw.indexOf('\x1b',i);if(at<0)break;const type=raw[at+1];
      if([']','P','_','^','X'].includes(type)){const st=raw.indexOf('\x1b\\',at+2),bel=type===']'?raw.indexOf('\x07',at+2):-1,end=st<0?bel:bel<0?st:Math.min(st,bel);if(end<0)break;i=end+(raw[end]==='\x07'?1:2);continue;}
      if(type==='['){
        let end=at+2;while(end<raw.length&&raw.charCodeAt(end)>=0x20&&raw.charCodeAt(end)<=0x3f)end++;
        if(end<raw.length&&raw.charCodeAt(end)>=0x40&&raw.charCodeAt(end)<=0x7e){
          const seq=raw.slice(at,end+1);tokens.push({at,end:end+1,seq});const cup=/^\x1b\[([0-9;]*)[Hf]$/.exec(seq);
          if(cup){const p=cup[1].split(';'),row=Number(p[0])||1,col=Number(p[1])||1;if(p.length<=2&&row<=200&&col<=500&&(row>grid.rows||col>grid.cols)){foreign++;lastForeign=at;}}
          i=end+1;continue;
        }
      }else if(type==='c')tokens.push({at,end:at+2,seq:'\x1bc'});
      i=at+2;
    }
    if(!partial&&foreign<3&&!baselineOnly)return unchanged('');
    const home=seq=>{const m=/^\x1b\[([0-9;]*)[Hf]$/.exec(seq);if(!m)return false;const p=m[1].split(';');return p.length<=2&&(Number(p[0])||1)===1&&(Number(p[1])||1)===1;};
    let cut=-1,reset=false;
    for(let n=0;n<tokens.length;n++){
      const t=tokens[n],next=tokens[n+1];if(t.at<=lastForeign)continue;
      if(t.seq==='\x1bc'){cut=t.at;reset=true;if(!baselineOnly)break;}
      if(next&&t.end===next.at&&((t.seq==='\x1b[2J'&&home(next.seq))||(home(t.seq)&&next.seq==='\x1b[2J'))){cut=t.at;reset=false;if(!baselineOnly)break;}
    }
    if(cut<0)return unchanged(foreign?'no_safe_repaint':'');
    const before=tokens.filter(token=>token.at<cut);
    if(!reset&&before.some(token=>/^\x1b\[[0-9;]*r$/.test(token.seq)||/^\x1b\[\?(?:6|47|1047|1048|1049)[hl]$/.test(token.seq)))return unchanged('unsupported_state');
    const state=reset?'':before.filter(token=>/^\x1b\[[0-9;:]*m$/.test(token.seq)||/^\x1b\[\?(?:1|7|12|25|1000|1002|1003|1004|1006|1015|2004|2026)[hl]$/.test(token.seq)).map(token=>token.seq).join('');
    const prefix=new TextEncoder().encode(state),result=new Uint8Array(prefix.length+bytes.length-cut);result.set(prefix);result.set(bytes.subarray(cut),prefix.length);
    return {bytes:result,partial:partial||foreign>=3||!baselineOnly,baseline:!reset,requiresReset:reset,recovered:cut>0,reason:cut>0?'grid_history_rebased':'',discardedBytes:cut};
  }
  function debugPrompt(term){
    const buffer=term.buffer.active,lines=[];
    for(let row=buffer.baseY+term.rows-1;row>=buffer.baseY&&lines.length<3;row--){
      const text=visibleBufferLine(buffer.getLine(row),term.cols).trim();
      if(text)lines.unshift(text.slice(0,160));
    }
    return lines.join(' · ').slice(0,512);
  }
  // Read-only parser: it is never opened/focused and has no onData handler.
  // Snapshots can precede controls, so do not parse at an invented grid size.
  function createDebugPreview({createTerminal,onText,onError=()=>{},limit=524288}){
    let terminal=null,grid=null,waiting=[],waitingBytes=0,dead=false;
    const fail=()=>{if(!dead)onError();};
    const writer=createWriter({limit,schedule:cb=>setTimeout(cb,0),cancel:clearTimeout,onOverflow:fail,
      write:(bytes,reset,done,size)=>{
        if(dead)return;
        if(terminal.cols!==size.cols||terminal.rows!==size.rows)terminal.resize(size.cols,size.rows);
        if(reset)terminal.reset();
        terminal.write(bytes,()=>{if(dead)return;done();onText(debugPrompt(terminal));});
      }});
    return {
      setGrid(size){
        if(dead||!Number.isInteger(size?.cols)||!Number.isInteger(size?.rows)||size.cols<2||size.cols>500||size.rows<1||size.rows>200)return;
        grid={cols:size.cols,rows:size.rows};
        if(!terminal)terminal=createTerminal({...grid,allowProposedApi:true,disableStdin:true,scrollback:0,convertEol:false,windowsPty:{backend:'conpty'}});
        const feeds=waiting;waiting=[];waitingBytes=0;
        for(const feed of feeds)writer.push(feed.bytes,feed.replay,()=>{},grid);
      },
      push(bytes,replay=false){
        if(dead)return false;
        if(grid)return writer.push(bytes,replay,()=>{},grid);
        if(waitingBytes+bytes.length>limit){fail();return false;}
        waiting.push({bytes:bytes.slice(),replay});waitingBytes+=bytes.length;return true;
      },
      dispose(){dead=true;writer.dispose();terminal?.dispose();waiting=[];waitingBytes=0;}
    };
  }
  // Full-screen programs have no native scrollback. Keep bounded, read-only
  // text frames without letting new output move a reader away from a snapshot.
  function createScreenHistory({maxFrames=120,maxBytes=1048576,minInterval=750,now=()=>performance.now()}={}){
    if(!Number.isSafeInteger(maxFrames)||maxFrames<1||!Number.isSafeInteger(maxBytes)||maxBytes<1||!Number.isFinite(minInterval)||minInterval<0||typeof now!=='function')throw new TypeError('Invalid screen history limits');
    const encoder=new TextEncoder();
    let frames=[],bytes=0,selected=null,nextID=0,lastCheckpoint=0;
    const index=()=>selected===null?-1:frames.findIndex(entry=>entry.frame.id===selected);
    const current=()=>frames[index()]?.frame||null;
    function trim(){
      while(frames.length>maxFrames||bytes>maxBytes){
        // Retain the latest output and the selected frame whenever both fit.
        let at=frames.findIndex((entry,i)=>entry.frame.id!==selected&&i<frames.length-1);
        if(at<0)at=0;
        bytes-=frames.splice(at,1)[0].size;
      }
      if(selected!==null&&index()<0)selected=frames[0]?.frame.id??null;
    }
    return {
      record(value){
        const cols=value?.cols,rows=value?.rows,lines=value?.lines;
        if(!Number.isInteger(cols)||cols<2||cols>500||!Number.isInteger(rows)||rows<1||rows>200||!Array.isArray(lines)||lines.length!==rows||Array.from(lines).some(line=>typeof line!=='string'||line.length>cols*2||/[\x00-\x1f\x7f]/.test(line)))return false;
        const latest=frames[frames.length-1]?.frame;
        if(latest&&latest.cols===cols&&latest.rows===rows&&latest.lines.every((line,i)=>line===lines[i]))return false;
        const time=now();if(!Number.isFinite(time)||time<0)return false;
        const timestamp=Math.max(time,latest?.timestamp||0);
        const frame=Object.freeze({id:nextID+1,cols,rows,lines:Object.freeze([...lines]),timestamp});
        const size=encoder.encode(JSON.stringify(frame)).length;
        if(size>maxBytes)return false;
        if(frames.length>1&&latest.id!==selected&&timestamp-lastCheckpoint<minInterval)bytes-=frames.pop().size;
        else lastCheckpoint=timestamp;
        frames.push({frame,size});bytes+=size;nextID++;trim();return true;
      },
      previous(){
        if(!frames.length)return null;
        const at=index();selected=frames[at<0?Math.max(0,frames.length-2):Math.max(0,at-1)].frame.id;return current();
      },
      next(){
        const at=index();if(at<0)return null;
        selected=frames[at+1]?.frame.id??null;return current();
      },
      top(){selected=frames[0]?.frame.id??null;return current();},
      bottom(){selected=null;return null;},
      clear(){frames=[];bytes=0;selected=null;lastCheckpoint=0;},
      get length(){return frames.length;},
      get bytes(){return bytes;},
      get current(){return current();},
      get atTop(){return !frames.length||selected===frames[0].frame.id;},
      get atBottom(){return selected===null;}
    };
  }
  async function uploadAttachment(file,{request,isCurrent,signal,onProgress=()=>{}}){
    if(!file||typeof file.name!=='string'||!file.name||file.name.length>200||/[\\/\x00-\x1f\x7f]/.test(file.name)||!Number.isSafeInteger(file.size)||file.size<0||file.size>32*1024*1024)throw Error('单个附件限 32 MiB，文件名不能包含路径或控制字符。');
    const check=()=>{if(signal?.aborted)throw Error('上传已取消。');if(!isCurrent())throw Error('目标窗口或连接已变化，上传已停止。');};
    let id=null,finishing=false;
    try{
      check();const bytes=new Uint8Array(await file.arrayBuffer());check();
      if(bytes.length!==file.size)throw Error('文件大小发生变化，请重新选择。');
      const hash=new Uint8Array(await root.crypto.subtle.digest('SHA-256',bytes));check();
      const sha256=[...hash].map(value=>value.toString(16).padStart(2,'0')).join('');
      const begin=await request({action:'begin',name:file.name,size:file.size,sha256});
      if(typeof begin.id!=='string'||!/^[a-f0-9]{48}$/.test(begin.id)||begin.received!==0)throw Error('接收端返回了无效的上传标识。');
      id=begin.id;check();
      for(let offset=0;offset<bytes.length;){
        check();const part=bytes.subarray(offset,offset+96*1024);let binary='';
        for(let at=0;at<part.length;at+=8192)binary+=String.fromCharCode(...part.subarray(at,at+8192));
        const next=offset+part.length,result=await request({action:'chunk',id,offset,data:root.btoa(binary)});check();
        if(result.received!==next)throw Error('接收进度不一致，上传已停止。');
        offset=next;onProgress(offset,file.size);
      }
      check();finishing=true;const saved=await request({action:'finish',id});
      if(typeof saved.path!=='string'||!saved.path||saved.path.length>4096||/[\x00-\x1f\x7f]/.test(saved.path))throw Error('未收到目标电脑的有效保存路径。');
      check();id=null;return {name:file.name,size:file.size,sha256,path:saved.path};
    }catch(error){if(finishing)error.uncertain=true;throw error;}
    finally{if(id)try{await request({action:'cancel',id},true);}catch{}}
  }
  const api={highlightSpans,createHighlighter,createWriter,createGesture,controlKey,terminalInput,composerAction,createComposerSubmitter,createComposerEcho,alternateScroll,closeForRecovery,sessionState,agentType,debugGridPage,debugSortEntries,debugSwipePage,debugRecoveryFeed,debugPrompt,createDebugPreview,createScreenHistory,uploadAttachment,visibleBufferLine,historyPayload,safeHistoryReplay};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.MobileCore=api;
})(typeof window==='undefined'?globalThis:window);
