/* Clickable http(s) links in terminal output; activation hands the URL to a handler. */
(()=>{
'use strict';
const TRAILING_PUNCT=/[.,;:!?)\]'">}]+$/;
const URL_PATTERN=/\b(?:https?:\/\/|www\.)[\w\-._~:/?#[\]@!$&*+,;=%]+/g;
const MAX_WRAPPED_LINES=8;

function columnForOffset(line,offset){
  let remaining=offset;
  for(let x=0;x<line.length;x++){
    const cell=line.getCell(x);if(!cell)break;
    const chars=cell.getChars(),length=chars?chars.length:0;
    if(length>0){if(remaining<length)return x;remaining-=length;}
  }
  return line.length;
}

function normalize(url){return /^www\./i.test(url)?'https://'+url:url;}

class WebLinksAddon{
  constructor(handler){this._handler=typeof handler==='function'?handler:url=>window.open(url,'_blank');this._provider=null;this._disposed=false;}
  activate(term){
    if(typeof term.registerLinkProvider!=='function')return;
    const handler=this._handler;
    this._provider=term.registerLinkProvider({provideLinks:(bufferLineNumber,callback)=>{
      if(this._disposed){callback(undefined);return;}
      // xterm addresses provider lines and link ranges as 1-based buffer rows.
      const buffer=term.buffer.active,lineCount=buffer.length;
      let startLine=Math.max(0,Math.min(bufferLineNumber-1,lineCount-1));
      const parts=[];let text='',endLine=startLine;
      for(let i=startLine;i<lineCount&&i<startLine+MAX_WRAPPED_LINES;i++){
        const line=buffer.getLine(i);if(!line)break;
        parts.push({index:i,line,text:line.translateToString(true)});
        endLine=i;
        if(!line.isWrapped)break;
      }
      for(const part of parts)text+=part.text;
      const links=[];URL_PATTERN.lastIndex=0;let match;
      while((match=URL_PATTERN.exec(text))){
        const raw=match[0],url=raw.replace(TRAILING_PUNCT,'');
        if(!url){continue;}
        const startIndex=match.index,endIndex=startIndex+url.length;
        let offset=0,partIndex=0;
        // Locate the buffer line and column for a character offset in the joined text.
        const locate=(target,isStart)=>{
          while(partIndex<parts.length){
            const part=parts[partIndex],length=part.text.length;
            if(target<=offset+length||partIndex===parts.length-1){
              // xterm link ranges use 1-based rows; start.x is 1-based while the
              // exclusive end offset already lands one past the last character.
              const x=columnForOffset(part.line,Math.max(0,target-offset));
              return {y:part.index+1,x:isStart?x+1:x};
            }
            offset+=length;partIndex++;
          }
          return {y:endLine+1,x:0};
        };
        const start=locate(startIndex,true),end=locate(endIndex,false);
        if(end.y>start.y||end.x>start.x){
          const target=normalize(url);
          links.push({range:{start,end},activate:()=>{if(!this._disposed)handler(target);},decorations:{pointerCursor:true,underline:true}});
        }
      }
      callback(links.length?links:undefined);
    }});
  }
  dispose(){this._disposed=true;this._provider?.dispose();this._provider=null;}
}

window.WebLinksAddon={WebLinksAddon};
})();
