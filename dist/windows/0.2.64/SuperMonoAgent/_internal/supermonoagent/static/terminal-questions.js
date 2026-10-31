/* Passive submitted-input marks; no history guessing, input changes or storage. */
(()=>{
  'use strict';
  if(window.TerminalQuestions)return;
  const MAX_TEXT=4096,MAX_PENDING=4,MAX_MARKS=64,MAX_MARK_TEXT=65536,LIFETIME=8000;
  function create(pane,{enabled=true,isCurrent=()=>true}={}){
    const term=pane.term,doc=term.textarea?.ownerDocument||document;
    let dead=false,draft='',invalid=false,enter=null,liveOutput=false,identity=null,pending=[],marks=[];
    const capture=()=>({host:pane.host,ws:pane.host?.ws,hostGeneration:pane.host?.generation,
      generation:pane.generation,raw:pane.raw,sid:pane.sid,token:pane.host?.config?.token,
      relay:pane.host?.config?.relay,code:pane.host?.config?.code,authMode:pane.host?.config?.authMode});
    const same=(a,b)=>a&&b&&Object.keys(a).every(key=>a[key]===b[key]);
    function drop(item){clearTimeout(item.timer);for(const d of item.decorations||[])d.dispose();item.marker?.dispose();}
    function clear(){for(const item of [...pending,...marks])drop(item);if(enter){clearTimeout(enter.timer);drop(enter.item||{});}pending=[];marks=[];draft='';invalid=false;enter=null;liveOutput=false;}
    function valid(){
      if(dead)return false;
      const next=capture();if(!same(identity,next)){clear();identity=next;}
      try{return enabled&&isCurrent(pane)!==false&&pane.ready===true&&pane.host?.connected===true
        &&pane.host?.ws?.readyState===1&&!pane.replayPending&&term.options.disableStdin!==true
        &&term.textarea?.readOnly!==true;}catch{return false;}
    }
    function cursorSegment(){
      const b=term.buffer.active,end=b.baseY+b.cursorY;
      let start=end;while(start>0&&b.getLine(start)?.isWrapped)start--;
      if(end-start>=32)return null;
      let text='';const positions=[];
      for(let row=start;row<=end;row++){
        const line=b.getLine(row);if(!line)return null;
        const limit=row===end?b.cursorX:term.cols;
        for(let x=0;x<limit;x++){
          const cell=line.getCell(x);if(!cell?.getWidth())continue;
          const value=cell.getChars()||' ';
          for(let i=0;i<value.length;i++)positions.push({row,x,width:cell.getWidth()});
          text+=value;if(text.length>MAX_TEXT+1024)return null;
        }
      }
      return {text,positions,buffer:b};
    }
    function anchor(text){
      if(typeof text!=='string'||!text||text.length>MAX_TEXT||/[\x00-\x1f\x7f-\x9f]/.test(text))return null;
      const segment=cursorSegment();if(!segment?.text.endsWith(text))return null;
      const cells=segment.positions.slice(segment.text.length-text.length),ranges=[];
      for(const cell of cells){
        const old=ranges.at(-1);
        if(old&&old.row===cell.row&&cell.x<=old.x+old.width)old.width=Math.max(old.width,cell.x+cell.width-old.x);
        else ranges.push({row:cell.row,x:cell.x,width:cell.width});
      }
      if(!ranges.length)return null;
      const first=ranges[0].row,marker=term.registerMarker(first-segment.buffer.baseY-segment.buffer.cursorY);
      if(!marker)return null;
      return {text,marker,buffer:segment.buffer,ranges:ranges.map(r=>({...r,row:r.row-first})),identity:capture()};
    }
    function queue(item){
      if(!item)return false;
      if(pending.length>=MAX_PENDING)drop(pending.shift());
      item.timer=setTimeout(()=>{const at=pending.indexOf(item);if(at>=0){pending.splice(at,1);drop(item);}},LIFETIME);
      pending.push(item);return true;
    }
    function keydown(event){
      if(event.target!==term.textarea||!event.isTrusted||event.key!=='Enter'||event.repeat
        ||event.isComposing||pane.composing||event.shiftKey||event.ctrlKey||event.altKey||event.metaKey||!valid())return;
      const proof={item:!invalid?anchor(draft):null};enter=proof;
      // A browser may run microtasks between event listeners. Expire at the
      // next task so xterm's synchronous onData can confirm this same event.
      proof.timer=setTimeout(()=>{if(enter===proof){drop(proof.item||{});enter=null;}},0);
    }
    function observeAcceptedInput(text){
      if(!valid()||typeof text!=='string'||!text)return;
      if((text==='\r'||text==='\n')&&enter){
        const item=enter.item;clearTimeout(enter.timer);enter=null;draft='';invalid=false;queue(item);return;
      }
      if(text.startsWith('\x1b[200~')&&text.endsWith('\x1b[201~'))text=text.slice(6,-6);
      if(/[\x00-\x1f\x7f-\x9f]/.test(text)||draft.length+text.length>MAX_TEXT){draft='';invalid=true;return;}
      if(!invalid)draft+=text;
    }
    function textAt(item){
      const b=term.buffer.active;let text='';
      for(const range of item.ranges){
        const line=b.getLine(item.marker.line+range.row);if(!line)return null;
        for(let x=range.x;x<range.x+range.width;x++){
          const c=line.getCell(x);if(!c?.getWidth())continue;
          if(c.isInverse()||c.isInvisible()||!c.isBgDefault())return null;
          text+=c.getChars()||' ';
        }
      }
      return text;
    }
    function parsed(){
      const live=liveOutput;liveOutput=false;
      if(!valid()||!live)return;
      const b=term.buffer.active,current=b.baseY+b.cursorY;
      let start=current,end=current;
      while(start>0&&b.getLine(start)?.isWrapped)start--;
      while(end+1<b.length&&b.getLine(end+1)?.isWrapped)end++;
      for(const item of [...pending]){
        if(item.marker.isDisposed||item.buffer!==b||!same(item.identity,capture())){
          pending.splice(pending.indexOf(item),1);drop(item);continue;
        }
        const first=item.marker.line,last=first+item.ranges.at(-1).row;
        if(start<=last&&end>=first)continue;
        pending.splice(pending.indexOf(item),1);clearTimeout(item.timer);
        if(textAt(item)!==item.text){drop(item);continue;}
        item.decorations=[];
        for(const range of item.ranges){
          const marker=term.registerMarker(first+range.row-b.baseY-b.cursorY);
          if(!marker)continue;
          const d=term.registerDecoration({marker,x:range.x,width:range.width,foregroundColor:'#ffd75f',layer:'top'});
          if(d)item.decorations.push({dispose(){d.dispose();marker.dispose();}});else marker.dispose();
        }
        if(item.decorations.length){
          marks.push(item);
          while(marks.length>MAX_MARKS||marks.reduce((sum,item)=>sum+item.text.length,0)>MAX_MARK_TEXT)drop(marks.shift());
        }
        else drop(item);
      }
      marks=marks.filter(item=>{
        const first=item.marker.line,last=first+item.ranges.at(-1).row;
        if(item.marker.isDisposed||start<=last&&end>=first||textAt(item)!==item.text){drop(item);return false;}
        return true;
      });
    }
    doc.addEventListener('keydown',keydown,{capture:true,passive:true});
    const subscriptions=[term.onWriteParsed(parsed),term.buffer.onBufferChange(()=>clear())];
    identity=capture();
    return {
      observeAcceptedInput,
      beforeOutput({replay=false}={}){if(replay){clear();identity=capture();}else if(valid())liveOutput=true;},
      recordSubmission({text,source}={}){return valid()&&source==='explicit-submit'?queue(anchor(text)):false;},
      setEnabled(value){enabled=!!value;if(!enabled)clear();},
      reset(){clear();identity=capture();},
      dispose(){if(dead)return;dead=true;clear();doc.removeEventListener('keydown',keydown,true);for(const s of subscriptions)s.dispose();}
    };
  }
  window.TerminalQuestions=Object.freeze({create});
})();
