/* Folder batches over an already captured file transport; no DOM or terminal IO. */
(function(root,factory){
  'use strict';
  const api=factory(root);
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.SuperMonoFolderClient=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(root){
  'use strict';
  const CHUNK=98304,MAX_BYTES=1073741824,MAX_FILE=536870912,MAX_MANIFEST=65536;
  const hex=(value,count)=>typeof value==='string'&&new RegExp('^[a-f0-9]{'+count+'}$').test(value);
  const object=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
  const copy=value=>JSON.parse(JSON.stringify(value));
  const encoder=new TextEncoder();
  const messages={unknown_commit:'文件夹传输结果待确认，请查询状态，勿重复提交。',cancelled:'文件夹传输已停止；已保存的文件会保留。',target_changed:'目标窗口或连接已变化，文件夹不会改投其他项目。'};
  class FolderClientError extends Error{
    constructor(code){super(messages[code]||code);this.name='FolderClientError';this.code=code;}
  }
  const fail=code=>{throw new FolderClientError(code);};
  function keys(value,required,optional=[]){
    if(!object(value))return false;
    const actual=Object.keys(value);
    return actual.length<=required.length+optional.length&&required.every(key=>Object.hasOwn(value,key))&&actual.every(key=>required.includes(key)||optional.includes(key));
  }
  function component(value,limit=200,internal=false){
    if(typeof value!=='string'||!value||value.length>limit||/[\p{Cc}\uD800-\uDFFF]/u.test(value)||/[\\/:<>"|?*]/u.test(value)||/[. ]$/u.test(value)||value==='.'||value==='..'||!internal&&value.toLowerCase()==='.supermono-partials')fail('invalid_manifest');
    const base=value.split('.')[0].replace(/ +$/u,'').toUpperCase();
    if(/^(?:CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])$/u.test(base))fail('invalid_manifest');
    return value;
  }
  function path(value){
    if(!Array.isArray(value)||!value.length||value.length>16)fail('invalid_manifest');
    const parts=value.map(item=>component(item));
    if(parts.reduce((n,item)=>n+item.length,parts.length-1)>1024)fail('invalid_manifest');
    return parts;
  }
  function compareString(a,b){
    const left=Array.from(a,letter=>letter.codePointAt(0)),right=Array.from(b,letter=>letter.codePointAt(0));
    for(let index=0;index<Math.min(left.length,right.length);index++)if(left[index]!==right[index])return left[index]-right[index];
    return left.length-right.length;
  }
  function comparePath(a,b){
    for(let index=0;index<Math.min(a.length,b.length);index++){const order=compareString(a[index],b[index]);if(order)return order;}
    return a.length-b.length;
  }
  function manifest(value){
    if(!keys(value,['version','root','directories','files'])||value.version!==1||!Array.isArray(value.directories)||value.directories.length>256||!Array.isArray(value.files)||value.files.length>32)fail('invalid_manifest');
    const rootName=component(value.root),directories=value.directories.map(path),files=[];
    let size=0;const names=new Set(),parents=new Set(directories.map(parts=>JSON.stringify(parts)));
    for(const entry of value.files){
      if(!keys(entry,['path','size','sha256'])||!Number.isSafeInteger(entry.size)||entry.size<0||entry.size>MAX_FILE||!hex(entry.sha256,64))fail('invalid_manifest');
      size+=entry.size;if(size>MAX_BYTES)fail('invalid_manifest');
      files.push({path:path(entry.path),sha256:entry.sha256,size:entry.size});
    }
    for(const parts of [...directories,...files.map(entry=>entry.path)]){
      // The server performs authoritative Unicode casefold collision checks.
      const key=JSON.stringify(parts.map(item=>item.normalize('NFC').toLowerCase()));
      if(names.has(key))fail('invalid_manifest');names.add(key);
      for(let depth=1;depth<parts.length;depth++)if(!parents.has(JSON.stringify(parts.slice(0,depth))))fail('invalid_manifest');
    }
    directories.sort(comparePath);files.sort((a,b)=>comparePath(a.path,b.path));
    // Insertion order deliberately matches Python's sort_keys canonical JSON.
    const result={directories,files,root:rootName,version:1};
    if(encoder.encode(JSON.stringify(result)).length>MAX_MANIFEST)fail('invalid_manifest');
    return result;
  }
  async function digest(value){
    if(!root.crypto?.subtle)fail('unsupported');
    const data=await root.crypto.subtle.digest('SHA-256',encoder.encode(JSON.stringify(value)));
    return [...new Uint8Array(data)].map(byte=>byte.toString(16).padStart(2,'0')).join('');
  }
  function nonce(){
    if(!root.crypto?.getRandomValues)fail('unsupported');
    return [...root.crypto.getRandomValues(new Uint8Array(16))].map(byte=>byte.toString(16).padStart(2,'0')).join('');
  }
  function absolute(value){
    if(typeof value!=='string'||!value||value.length>4096||/[\p{Cc}\uD800-\uDFFF]/u.test(value))fail('invalid_response');
    const text=value.replace(/\\/gu,'/');
    const prefix=/^[A-Za-z]:\//u.test(text)?text.slice(0,3):text.startsWith('/')&&!text.startsWith('//')?'/':null;
    if(!prefix)fail('invalid_response');
    const parts=text.slice(prefix.length).split('/');
    if(parts.length&&parts.at(-1)==='')parts.pop();
    for(const item of parts){try{component(item,255,true);}catch{fail('invalid_response');}}
    return prefix+parts.join('/');
  }
  function workspace(value){
    if(!object(value)||!hex(value.daemon_epoch,64)||!hex(value.project_binding,64)||!Number.isSafeInteger(value.revision)||value.revision<0)fail('invalid_workspace');
    const result={};
    for(const field of ['host_id','window_id','session_id']){
      if(typeof value[field]!=='string'||!value[field]||value[field].length>256||/[\p{Cc}]/u.test(value[field]))fail('invalid_workspace');
      result[field]=value[field];
    }
    try{result.exchange_dir=absolute(value.exchange_dir);}catch{fail('invalid_workspace');}
    return {...result,daemon_epoch:value.daemon_epoch,revision:value.revision,project_binding:value.project_binding};
  }
  function savedReceipt(value,entry,index,context,batch){
    if(!keys(value,['file_id','name','path','size','sha256','daemon_epoch','workspace_revision','project_binding','folder'])||!hex(value.file_id,48)||value.name!==entry.path.at(-1)||value.size!==entry.size||value.sha256!==entry.sha256||value.daemon_epoch!==context.workspace.daemon_epoch||value.workspace_revision!==context.workspace.revision||value.project_binding!==context.workspace.project_binding)fail('invalid_response');
    if(!keys(value.folder,['batch_id','manifest_digest','entry_index'])||value.folder.batch_id!==batch.batch_id||value.folder.manifest_digest!==context.digest||value.folder.entry_index!==index||!batch.receipt)fail('invalid_response');
    const expected=absolute(batch.receipt.root_path)+'/'+entry.path.join('/');
    if(absolute(value.path)!==expected)fail('invalid_response');
    return copy(value);
  }
  function batch(value,context){
    if(!keys(value,['batch_id','manifest_digest','manifest','state','entries'],['receipt'])||!hex(value.batch_id,48)||value.manifest_digest!==context.digest||(context.state.batch_id&&value.batch_id!==context.state.batch_id)||!['preparing','ready','cancelled','complete'].includes(value.state)||!Array.isArray(value.entries)||value.entries.length!==context.manifest.files.length)fail('invalid_response');
    let actual;try{actual=manifest(value.manifest);}catch{fail('invalid_response');}
    if(JSON.stringify(actual)!==JSON.stringify(context.manifest))fail('invalid_response');
    const result={batch_id:value.batch_id,manifest_digest:value.manifest_digest,manifest:actual,state:value.state,entries:[]};
    if(value.receipt!==undefined){
      const receipt=value.receipt;
      if(!keys(receipt,['batch_id','manifest_digest','root_path','name'])||receipt.batch_id!==value.batch_id||receipt.manifest_digest!==context.digest)fail('invalid_response');
      try{component(receipt.name);}catch{fail('invalid_response');}
      if(absolute(receipt.root_path)!==context.workspace.exchange_dir.replace(/\/$/u,'')+'/'+receipt.name)fail('invalid_response');
      result.receipt=copy(receipt);
    }
    if(value.state!=='preparing'&&!result.receipt)fail('invalid_response');
    const fileIds=new Set();
    for(let index=0;index<value.entries.length;index++){
      const item=value.entries[index];
      if(!keys(item,['entry_index','state'],['file_id','receipt'])||item.entry_index!==index||!['pending','uploading','staged','queued','downloading','saved','failed'].includes(item.state))fail('invalid_response');
      if((['staged','queued','downloading','saved','failed'].includes(item.state)&&!hex(item.file_id,48))||(['pending','uploading'].includes(item.state)&&item.file_id!==undefined))fail('invalid_response');
      const entry={entry_index:index,state:item.state};
      if(item.file_id!==undefined){if(fileIds.has(item.file_id))fail('invalid_response');fileIds.add(item.file_id);entry.file_id=item.file_id;}
      if(item.state==='saved'){
        entry.receipt=savedReceipt(item.receipt,context.manifest.files[index],index,context,result);
        if(entry.receipt.file_id!==entry.file_id)fail('invalid_response');
      }else if(item.receipt!==undefined)fail('invalid_response');
      result.entries.push(entry);
    }
    if(value.state==='complete'&&result.entries.some(entry=>entry.state!=='saved'))fail('invalid_response');
    context.state.batch_id=result.batch_id;
    return result;
  }
  function chunk(value,offset,size){
    if(!keys(value,['data','next'],['offset','eof'])||typeof value.data!=='string'||value.data.length>131072||!Number.isSafeInteger(value.next)||value.next<=offset||value.next>size||value.offset!==undefined&&value.offset!==offset||value.eof!==undefined&&(typeof value.eof!=='boolean'||value.eof!==(value.next===size))||typeof root.atob!=='function'||typeof root.btoa!=='function')fail('invalid_source');
    let raw;try{raw=root.atob(value.data);}catch{fail('invalid_source');}
    if(!raw.length||raw.length>CHUNK||root.btoa(raw)!==value.data||value.next!==offset+raw.length)fail('invalid_source');
    return {data:value.data,next:value.next};
  }
  async function context(options,withSources){
    if(!object(options)||typeof options.request!=='function'||typeof options.current!=='function')fail('invalid_options');
    const value=manifest(options.manifest),target=workspace(options.workspace),sources=[];
    if(withSources){
      if(!Array.isArray(options.sources)||options.sources.length!==value.files.length)fail('invalid_source');
      const seen=new Set();
      for(const source of options.sources){
        if(!object(source)||!Number.isInteger(source.index)||source.index<0||source.index>=value.files.length||seen.has(source.index)||typeof source.read!=='function'||source.release!==undefined&&typeof source.release!=='function')fail('invalid_source');
        const entry=value.files[source.index];if(source.size!==entry.size||source.sha256!==entry.sha256)fail('invalid_source');
        seen.add(source.index);sources[source.index]={read:source.read.bind(source),release:source.release?.bind(source)};
      }
    }
    const settings={request:options.request,current:options.current,signal:options.signal,onProgress:options.onProgress},fromState=options.state!==undefined;
    let state;
    if(options.state!==undefined){
      if(!keys(options.state,['version','nonce','manifest_digest','workspace','phase','timezone_offset_minutes'],['batch_id','entry_index','upload_id'])||options.state.version!==1||!hex(options.state.nonce,32)||!hex(options.state.manifest_digest,64)||!Number.isInteger(options.state.timezone_offset_minutes)||Math.abs(options.state.timezone_offset_minutes)>840||typeof options.state.phase!=='string'||options.state.phase.length>32||options.state.batch_id!==undefined&&!hex(options.state.batch_id,48)||options.state.upload_id!==undefined&&!hex(options.state.upload_id,48)||options.state.entry_index!==undefined&&(!Number.isInteger(options.state.entry_index)||options.state.entry_index<0||options.state.entry_index>=value.files.length))fail('invalid_state');
      let previous;try{previous=workspace(options.state.workspace);}catch{fail('invalid_state');}
      if(JSON.stringify(previous)!==JSON.stringify(target))fail('target_changed');
      state={version:1,nonce:options.state.nonce,manifest_digest:options.state.manifest_digest,workspace:previous,phase:options.state.phase,timezone_offset_minutes:options.state.timezone_offset_minutes};
      for(const field of ['batch_id','entry_index','upload_id'])if(options.state[field]!==undefined)state[field]=options.state[field];
    }
    const timeout=options.timeoutMs??180000;
    if(!Number.isSafeInteger(timeout)||timeout<1||timeout>180000)fail('invalid_options');
    const result={options:settings,manifest:value,workspace:target,state,sources,timeout,fromState,
      sleep:options.sleep||((ms)=>new Promise(resolve=>setTimeout(resolve,ms))),now:options.now||(()=>performance.now())};
    if(typeof result.sleep!=='function'||typeof result.now!=='function')fail('invalid_options');
    const md=await digest(value);
    if(state&&state.manifest_digest!==md)fail('invalid_state');
    result.digest=md;
    result.state=state||{version:1,nonce:nonce(),manifest_digest:md,workspace:target,phase:'new',timezone_offset_minutes:-new Date().getTimezoneOffset()};
    return result;
  }
  function current(ctx){try{return ctx.options.current()===true;}catch{return false;}}
  function check(ctx,ignoreAbort=false){
    if(!current(ctx))fail('target_changed');
    if(!ignoreAbort&&ctx.options.signal?.aborted)fail('cancelled');
  }
  function notify(ctx,phase,extra={}){
    ctx.state.phase=phase;
    try{ctx.options.onProgress?.({phase,...extra,transferState:copy(ctx.state)});}catch{}
  }
  async function request(ctx,action,data,ignoreAbort=false){
    check(ctx,ignoreAbort);
    const response=await ctx.options.request(action,copy(data));
    check(ctx,ignoreAbort);
    return response;
  }
  function output(ctx,value){
    return {state:value.state,batch:copy(value),receipts:value.entries.filter(entry=>entry.state==='saved').map(entry=>copy(entry.receipt)),transferState:copy(ctx.state)};
  }
  function errorState(error,ctx,unknown=false){
    const result=error instanceof FolderClientError?error:new FolderClientError(typeof error?.code==='string'?error.code:'service_unavailable');
    if(unknown){result.code='unknown_commit';result.message=messages.unknown_commit;result.unknownCommit=true;}
    result.transferState=copy(ctx.state);result.receipts=(ctx.latest?.entries||[]).filter(entry=>entry.state==='saved').map(entry=>copy(entry.receipt));
    return result;
  }
  async function reconcileFolder(options){
    const ctx=await context(options,false);
    if(!ctx.fromState)fail('invalid_state');
    try{
      const value=batch(await request(ctx,ctx.state.batch_id?'folder.status':'folder.lookup',ctx.state.batch_id?{batch_id:ctx.state.batch_id}:{nonce:ctx.state.nonce}),ctx);
      ctx.latest=value;delete ctx.state.upload_id;delete ctx.state.entry_index;notify(ctx,'reconciled');
      return output(ctx,value);
    }catch(error){throw errorState(error,ctx);}
  }
  async function cancelFolder(options){
    const ctx=await context(options,false);
    if(!ctx.fromState)fail('invalid_state');
    let attempted=false;
    try{
      if(!ctx.state.batch_id)ctx.latest=batch(await request(ctx,'folder.lookup',{nonce:ctx.state.nonce}),ctx);
      notify(ctx,'cancel_pending');check(ctx);attempted=true;
      const value=batch(await request(ctx,'folder.cancel',{batch_id:ctx.state.batch_id}),ctx);
      if(value.state!=='cancelled'&&value.state!=='complete')fail('invalid_response');
      ctx.latest=value;delete ctx.state.upload_id;delete ctx.state.entry_index;
      notify(ctx,value.state==='complete'?'complete':'cancelled');
      return output(ctx,value);
    }catch(error){
      if(attempted)notify(ctx,'cancel_unknown');
      throw errorState(error,ctx,attempted);
    }
  }
  async function uploadFolder(options){
    let ctx;const released=new Set();
    // Capture release callbacks before asynchronous validation; even malformed
    // source metadata must not leave retained native handles behind.
    const releases=Array.isArray(options?.sources)&&options.sources.length<=128?[...new Set(options.sources.filter(object))].map(source=>typeof source.release==='function'?source.release.bind(source):null):[];
    let uploadId=null,commitAttempted=false,beginAttempted=false,completeAttempted=false;
    try{
      ctx=await context(options,true);check(ctx);
      if(ctx.fromState&&ctx.state.phase!=='reconciled'&&ctx.state.phase!=='complete')fail('reconciliation_required');
      notify(ctx,'capabilities');
      const cap=await request(ctx,'folder.capabilities',{});
      if(!keys(cap,['version','max_files','max_directories','max_total_bytes'])||cap.version!==1||!Number.isSafeInteger(cap.max_files)||cap.max_files<ctx.manifest.files.length||!Number.isSafeInteger(cap.max_directories)||cap.max_directories<ctx.manifest.directories.length||!Number.isSafeInteger(cap.max_total_bytes)||cap.max_total_bytes<ctx.manifest.files.reduce((sum,entry)=>sum+entry.size,0))fail('folder_unsupported');
      let value;
      if(ctx.state.batch_id)value=batch(await request(ctx,'folder.status',{batch_id:ctx.state.batch_id}),ctx);
      if(!value||value.state==='preparing'){
        notify(ctx,'begin_pending');check(ctx);beginAttempted=true;
        const raw=await request(ctx,'folder.begin',{nonce:ctx.state.nonce,manifest:ctx.manifest,window_id:ctx.workspace.window_id,daemon_epoch:ctx.workspace.daemon_epoch,workspace_revision:ctx.workspace.revision,project_binding:ctx.workspace.project_binding,timezone_offset_minutes:ctx.state.timezone_offset_minutes});
        value=batch(raw,ctx);beginAttempted=false;
      }
      ctx.latest=value;
      if(value.state==='cancelled')fail('folder_cancelled');
      if(value.state==='preparing')fail('folder_preparing');
      if(value.state==='complete'){notify(ctx,'complete');return output(ctx,value);}
      notify(ctx,'ready');
      for(let index=0;index<ctx.manifest.files.length;index++){
        check(ctx);const entry=ctx.manifest.files[index];ctx.state.entry_index=index;
        if(value.entries[index].state==='saved')continue;
        if(value.entries[index].state==='failed')fail('folder_entry_failed');
        if(value.entries[index].state==='uploading')fail('folder_entry_busy');
        let fileId=value.entries[index].file_id;
        if(!fileId){
          notify(ctx,'entry_begin_pending',{index});check(ctx);beginAttempted=true;
          const opened=await request(ctx,'folder.file.begin',{batch_id:ctx.state.batch_id,entry_index:index});
          if(keys(opened,['file_id','state'])&&hex(opened.file_id,48)&&['staged','queued','downloading','saved'].includes(opened.state)){fileId=opened.file_id;beginAttempted=false;}
          else{
            if(!keys(opened,['upload_id','name'])||!hex(opened.upload_id,48))fail('invalid_response');
            try{component(opened.name);}catch{fail('invalid_response');}
            uploadId=opened.upload_id;ctx.state.upload_id=uploadId;
            beginAttempted=false;
            let offset=0;
            while(offset<entry.size){
              check(ctx);const part=chunk(await ctx.sources[index].read(offset),offset,entry.size);check(ctx);
              const reply=await request(ctx,'upload.chunk',{upload_id:uploadId,offset,data:part.data});
              if(!keys(reply,['received'])||reply.received!==part.next)fail('invalid_response');
              offset=part.next;notify(ctx,'uploading',{index,loaded:offset,size:entry.size});
            }
            check(ctx);notify(ctx,'finish_pending',{index});check(ctx);commitAttempted=true;
            const queued=await request(ctx,'upload.finish',{upload_id:uploadId},true);
            if(!keys(queued,['file_id','name','state'])||!hex(queued.file_id,48)||queued.state!=='queued'||typeof queued.name!=='string')fail('invalid_response');
            fileId=queued.file_id;commitAttempted=false;uploadId=null;delete ctx.state.upload_id;
          }
        }
        notify(ctx,'receiving',{index,file_id:fileId});
        const deadline=ctx.now()+ctx.timeout;let polls=0;
        while(true){
          value=batch(await request(ctx,'folder.status',{batch_id:ctx.state.batch_id},true),ctx);ctx.latest=value;
          const current=value.entries[index];
          if(current.file_id!==fileId)fail('invalid_response');
          if(current.state==='saved')break;
          if(current.state==='failed')fail('folder_entry_failed');
          if(ctx.now()>=deadline||++polls>Math.ceil(ctx.timeout/250)+1)fail('receipt_pending');
          await ctx.sleep(250);check(ctx,true);
        }
        notify(ctx,'saved',{index,receipt:copy(value.entries[index].receipt)});
        check(ctx);
      }
      delete ctx.state.entry_index;
      check(ctx);notify(ctx,'complete_pending');check(ctx);completeAttempted=true;
      value=batch(await request(ctx,'folder.finish',{batch_id:ctx.state.batch_id},true),ctx);
      if(value.state!=='complete')fail('invalid_response');
      completeAttempted=false;ctx.latest=value;notify(ctx,'complete');return output(ctx,value);
    }catch(error){
      if(!ctx)throw error;
      const unknown=beginAttempted||commitAttempted||completeAttempted;
      if(unknown)notify(ctx,commitAttempted?'finish_unknown':completeAttempted?'complete_unknown':ctx.state.batch_id?'entry_begin_unknown':'begin_unknown');
      else if(error?.code==='cancelled'){
        if(uploadId&&current(ctx))try{await ctx.options.request('upload.cancel',{upload_id:uploadId});uploadId=null;delete ctx.state.upload_id;}catch{}
        if(ctx.state.batch_id&&current(ctx))try{ctx.latest=batch(await ctx.options.request('folder.cancel',{batch_id:ctx.state.batch_id}),ctx);}catch{}
        notify(ctx,'cancelled');
      }
      throw errorState(error,ctx,unknown);
    }finally{
      if(ctx&&uploadId&&!commitAttempted&&current(ctx))try{await ctx.options.request('upload.cancel',{upload_id:uploadId});}catch{}
      for(const release of releases)if(release&&!released.has(release)){released.add(release);try{await release();}catch{}}
    }
  }
  return {uploadFolder,reconcileFolder,cancelFolder,FolderClientError};
});
