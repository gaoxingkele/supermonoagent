'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),crypto=require('node:crypto');
if(!globalThis.crypto)Object.defineProperty(globalThis,'crypto',{value:crypto.webcrypto});
const api=require('../src/supermonoagent/static/folder-client.js');
const H='a'.repeat(64),E='b'.repeat(64),B='c'.repeat(48),F='d'.repeat(48),U='e'.repeat(48);
const workspace={host_id:'f'.repeat(32),window_id:'window',session_id:'session',daemon_epoch:E,revision:0,project_binding:H,exchange_dir:'C:\\project\\tmp'};
const digest=data=>crypto.createHash('sha256').update(data).digest('hex');
function canonical(value){return JSON.stringify({directories:value.directories,files:value.files.map(x=>({path:x.path,sha256:x.sha256,size:x.size})),root:value.root,version:1});}
function fixture(extra={}){
 const bytes=extra.bytes||Buffer.from('owned file'),manifest={version:1,root:'folder',directories:[['empty'],['nested']],files:extra.empty?[]:[{path:['nested','owned.txt'],size:bytes.length,sha256:digest(bytes)}]};
 let calls=[],released=0,stored=false,complete=false,offset=0,uploaded=Buffer.alloc(0);
 const md=digest(canonical(manifest)),root='C:\\project\\tmp\\20261027-010203-folder';
 const receipt={file_id:F,name:'owned.txt',path:root+'\\nested\\owned.txt',size:bytes.length,sha256:digest(bytes),daemon_epoch:E,workspace_revision:0,project_binding:H,folder:{batch_id:B,manifest_digest:md,entry_index:0}};
 const status=()=>({batch_id:B,manifest_digest:md,manifest:structuredClone(manifest),state:complete?'complete':'ready',entries:extra.empty?[]:[stored?{entry_index:0,state:'saved',file_id:F,receipt:structuredClone(receipt)}:{entry_index:0,state:'pending'}],receipt:{batch_id:B,manifest_digest:md,root_path:root,name:'20261027-010203-folder'}});
 const request=async(action,data)=>{
  calls.push({action,data:structuredClone(data)});
  if(extra.request){const reply=await extra.request(action,data,{status,receipt,calls});if(reply!==undefined)return reply;}
  if(action==='folder.capabilities')return {version:1,max_files:32,max_directories:256,max_total_bytes:1073741824};
  if(['folder.begin','folder.lookup','folder.status'].includes(action))return status();
  if(action==='folder.file.begin')return stored?{file_id:F,state:'saved'}:{upload_id:U,name:'opaque-relay-name'};
  if(action==='upload.chunk'){assert.equal(data.offset,offset);const chunk=Buffer.from(data.data,'base64');uploaded=Buffer.concat([uploaded,chunk]);offset+=chunk.length;return {received:offset};}
  if(action==='upload.finish'){assert.deepEqual(uploaded,bytes);stored=true;return {file_id:F,name:'opaque-relay-name',state:'queued'};}
  if(action==='folder.finish'){complete=true;return status();}
  if(action==='upload.cancel')return {cancelled:true};
  if(action==='folder.cancel'){const value=status();value.state='cancelled';return value;}
  throw Error('unexpected action '+action);
 };
 const sources=extra.empty?[]:[{index:0,size:bytes.length,sha256:digest(bytes),read:async pos=>{const next=Math.min(pos+98304,bytes.length);return {data:bytes.subarray(pos,next).toString('base64'),next};},release:()=>released++}];
 return {manifest,workspace:structuredClone(workspace),sources,request,current:()=>true,sleep:async()=>{},now:()=>0,get calls(){return calls;},get released(){return released;},status,receipt,bytes};
}
test('uploads a folder through immutable batch and strict receipt without terminal input',async()=>{
 const f=fixture(),result=await api.uploadFolder(f);
 assert.equal(result.state,'complete');assert.equal(result.receipts.length,1);assert.equal(f.released,1);
 assert.deepEqual(f.calls.map(x=>x.action),['folder.capabilities','folder.begin','folder.file.begin','upload.chunk','upload.finish','folder.status','folder.finish']);
 const begin=f.calls.find(x=>x.action==='folder.begin').data;
 assert.match(begin.nonce,/^[a-f0-9]{32}$/);assert.equal(begin.project_binding,H);assert.equal(begin.manifest.root,'folder');
});
test('lost begin ACK retains nonce, releases source and reconciliation does only lookup',async()=>{
 const f=fixture({request:async action=>{if(action==='folder.begin')throw Error('network unavailable');}});
 let failure;await assert.rejects(api.uploadFolder(f),error=>{failure=error;return error.unknownCommit===true;});
 assert.match(failure.transferState.nonce,/^[a-f0-9]{32}$/);assert.equal(f.released,1);
 const reconciliation=fixture();const result=await api.reconcileFolder({...reconciliation,state:failure.transferState});
 assert.equal(result.state,'ready');assert.deepEqual(reconciliation.calls.map(x=>x.action),['folder.lookup']);
});
test('forged saved project binding is rejected',async()=>{
 const f=fixture({request:async(action,_data,ctx)=>{if(action==='folder.status'){const value=ctx.status();value.entries=[{entry_index:0,state:'saved',file_id:F,receipt:{...ctx.receipt,project_binding:'0'.repeat(64)}}];return value;}}});
 await assert.rejects(api.uploadFolder(f),error=>error.code==='invalid_response');assert.equal(f.released,1);
});
test('empty directories finish as a real batch without file uploads',async()=>{
 const f=fixture({empty:true}),result=await api.uploadFolder(f);
 assert.equal(result.state,'complete');assert.equal(result.receipts.length,0);
 assert.deepEqual(f.calls.map(x=>x.action),['folder.capabilities','folder.begin','folder.finish']);
});
test('large sources use serial bounded chunks',async()=>{
 const f=fixture({bytes:Buffer.alloc(512*1024,91)});await api.uploadFolder(f);
 const chunks=f.calls.filter(x=>x.action==='upload.chunk');assert.equal(chunks.length,6);
 let offset=0;for(const {data} of chunks){assert.equal(data.offset,offset);const raw=Buffer.from(data.data,'base64');assert.ok(raw.length<=98304);assert.ok(data.data.length<=131072);offset+=raw.length;}
 assert.equal(offset,512*1024);assert.equal(f.released,1);
});
test('lost finish ACK never repeats commit or cancels its possibly committed upload',async()=>{
 const f=fixture({request:async action=>{if(action==='upload.finish')throw Error('lost ACK');}});
 let failure;await assert.rejects(api.uploadFolder(f),error=>{failure=error;return error.unknownCommit&&error.transferState.phase==='finish_unknown';});
 assert.equal(f.calls.filter(x=>x.action==='upload.finish').length,1);assert.ok(!f.calls.some(x=>x.action==='upload.cancel'));assert.equal(f.released,1);
 const next=fixture();await assert.rejects(api.uploadFolder({...next,state:failure.transferState}),error=>error.code==='reconciliation_required');assert.equal(next.calls.length,0);assert.equal(next.released,1);
 const reconcile=fixture({request:async(action,_data,ctx)=>{if(action==='folder.status'){const value=ctx.status();value.entries=[{entry_index:0,state:'saved',file_id:F,receipt:structuredClone(ctx.receipt)}];return value;}}});
 const result=await api.reconcileFolder({...reconcile,state:failure.transferState});assert.equal(result.receipts.length,1);
 assert.deepEqual(reconcile.calls.map(x=>x.action),['folder.status']);assert.equal(reconcile.released,0);
});
test('lost complete ACK remains queryable without repeating folder finish',async()=>{
 const f=fixture({request:async action=>{if(action==='folder.finish')throw Error('lost final ACK');}});
 await assert.rejects(api.uploadFolder(f),error=>error.unknownCommit&&error.transferState.phase==='complete_unknown'&&error.receipts.length===1);
 assert.equal(f.calls.filter(x=>x.action==='folder.finish').length,1);assert.equal(f.released,1);
});
test('cancellation before begin sends nothing and releases native handles',async()=>{
 const f=fixture(),controller=new AbortController();controller.abort();
 await assert.rejects(api.uploadFolder({...f,signal:controller.signal}),error=>error.code==='cancelled');assert.equal(f.calls.length,0);assert.equal(f.released,1);
});
test('cancellation of uncommitted file cleans only its upload and stops the batch',async()=>{
 const f=fixture(),controller=new AbortController();
 await assert.rejects(api.uploadFolder({...f,signal:controller.signal,onProgress:value=>{if(value.phase==='uploading')controller.abort();}}),error=>error.code==='cancelled');
 assert.ok(f.calls.some(x=>x.action==='upload.cancel'));assert.ok(f.calls.some(x=>x.action==='folder.cancel'));assert.ok(!f.calls.some(x=>x.action==='upload.finish'));assert.equal(f.released,1);
});
test('cancellation after commit waits for its saved receipt then stops new work',async()=>{
 const f=fixture(),controller=new AbortController();
 await assert.rejects(api.uploadFolder({...f,signal:controller.signal,onProgress:value=>{if(value.phase==='receiving')controller.abort();}}),error=>error.code==='cancelled'&&error.receipts.length===1);
 assert.ok(!f.calls.some(x=>x.action==='upload.cancel'));assert.ok(!f.calls.some(x=>x.action==='folder.finish'));assert.equal(f.released,1);
});
test('target change while reading never sends a chunk to the changed context',async()=>{
 const f=fixture();let active=true;f.current=()=>active;
 f.sources[0].read=async()=>{active=false;return {data:f.bytes.toString('base64'),next:f.bytes.length};};
 await assert.rejects(api.uploadFolder(f),error=>error.code==='target_changed');
 assert.ok(!f.calls.some(x=>x.action==='upload.chunk'||x.action==='upload.finish'));assert.equal(f.released,1);
});
test('throwing current callback cannot bypass source cleanup',async()=>{
 const f=fixture();f.current=()=>{throw Error('context gone');};
 await assert.rejects(api.uploadFolder(f),error=>error.code==='target_changed');assert.equal(f.released,1);
});
for(const malformed of [()=>({data:'',next:1}),()=>({data:'YQ',next:1}),()=>({data:'YQ==',next:2}),()=>({data:'!!!!',next:1})]){
 test('invalid source chunks are rejected before upload',async()=>{
  const f=fixture();f.sources[0].read=async()=>malformed();
  await assert.rejects(api.uploadFolder(f),error=>error.code==='invalid_source');assert.ok(!f.calls.some(x=>x.action==='upload.chunk'));assert.equal(f.released,1);
 });
}
test('oversized source chunk is rejected even when file size permits it',async()=>{
 const f=fixture({bytes:Buffer.alloc(98305)});f.sources[0].read=async()=>({data:f.bytes.toString('base64'),next:f.bytes.length});
 await assert.rejects(api.uploadFolder(f),error=>error.code==='invalid_source');assert.equal(f.released,1);
});
test('malformed duplicate source references release each underlying source only once',async()=>{
 const f=fixture();f.sources.push(f.sources[0]);
 await assert.rejects(api.uploadFolder(f),error=>error.code==='invalid_source');assert.equal(f.released,1);assert.equal(f.calls.length,0);
});
test('source release exceptions cannot change a confirmed complete result',async()=>{
 const f=fixture();let released=0;f.sources[0].release=()=>{released++;throw Error('already closed');};
 assert.equal((await api.uploadFolder(f)).state,'complete');assert.equal(released,1);
});
test('input metadata and callbacks are captured before the first network await',async()=>{
 let f;f=fixture({request:async(action,data)=>{
  if(action==='folder.capabilities'){f.workspace.project_binding='0'.repeat(64);f.manifest.root='changed';f.sources[0].size=999;f.request=()=>{throw Error('must not retarget');};f.current=()=>false;}
  if(action==='folder.begin'){assert.equal(data.project_binding,H);assert.equal(data.manifest.root,'folder');assert.equal(data.manifest.files[0].size,10);throw Error('stop after inspecting immutable data');}
 }});
 await assert.rejects(api.uploadFolder(f),error=>error.unknownCommit);assert.equal(f.released,1);
});
test('receipt timeout reports durable pending work without repeating uploads',async()=>{
 const f=fixture({request:async(action,_data,ctx)=>{if(action==='folder.status'){const value=ctx.status();value.entries=[{entry_index:0,state:'queued',file_id:F}];return value;}}});
 let time=0;await assert.rejects(api.uploadFolder({...f,timeoutMs:1,now:()=>time,sleep:async()=>{time+=250;}}),error=>error.code==='receipt_pending'&&error.transferState.batch_id===B);
 assert.equal(f.calls.filter(x=>x.action==='upload.finish').length,1);assert.ok(!f.calls.some(x=>x.action==='upload.cancel'));assert.equal(f.released,1);
});
for(const mutate of [value=>value.receipt.root_path='C:\\other\\'+value.receipt.name,value=>value.manifest_digest='0'.repeat(64),value=>value.entries[0].entry_index=4]){
 test('forged batch metadata cannot authorize any source read',async()=>{
  const f=fixture({request:async(action,_data,ctx)=>{if(action==='folder.begin'){const value=ctx.status();mutate(value);return value;}}});let reads=0;f.sources[0].read=async()=>{reads++;throw Error();};
  await assert.rejects(api.uploadFolder(f),error=>error.unknownCommit);assert.equal(reads,0);assert.equal(f.released,1);
 });
}
test('canonical Unicode ordering and digest agree with the Python server',async()=>{
 const {spawnSync}=require('node:child_process'),path=require('node:path');
 const manifest={version:1,root:'目录😀\u2028',directories:[],files:[{path:['😀.txt'],size:0,sha256:digest('')},{path:['\ue000.txt'],size:0,sha256:digest('')}]};
 let sent,state;const sources=[0,1].map(index=>({index,size:0,sha256:digest(''),read:()=>{throw Error('no reads');}}));
 await assert.rejects(api.uploadFolder({manifest,workspace,sources,current:()=>true,request:async(action,data)=>{if(action==='folder.capabilities')return {version:1,max_files:32,max_directories:256,max_total_bytes:1073741824};sent=data.manifest;throw Error('inspect only');},onProgress:value=>{state=value.transferState;}}),error=>error.unknownCommit);
 assert.deepEqual(sent.files.map(x=>x.path[0]),['\ue000.txt','😀.txt']);
 const python=spawnSync(process.env.SMA_TEST_PYTHON||'python',['-X','utf8','-c','import json,sys;sys.path.insert(0,"src");from supermonoagent.file_folders import manifest_digest;print(manifest_digest(json.loads(sys.stdin.read())))'],{cwd:path.resolve(__dirname,'..'),input:JSON.stringify(manifest),encoding:'utf8'});
 assert.equal(python.status,0,python.stderr);assert.equal(state.manifest_digest,python.stdout.trim());
});
test('actual native chunk fields are accepted and consistency checked',async()=>{
 const f=fixture();f.sources[0].read=async offset=>({offset,data:f.bytes.toString('base64'),next:f.bytes.length,eof:true});
 assert.equal((await api.uploadFolder(f)).state,'complete');assert.equal(f.released,1);
});
for(const fields of [{offset:1,eof:true},{offset:0,eof:false},{offset:0,eof:'true'},{offset:0,eof:true,foreign:true}]){
 test('inconsistent native chunk metadata cannot be uploaded',async()=>{
  const f=fixture();f.sources[0].read=async()=>({data:f.bytes.toString('base64'),next:f.bytes.length,...fields});
  await assert.rejects(api.uploadFolder(f),error=>error.code==='invalid_source');assert.ok(!f.calls.some(x=>x.action==='upload.chunk'));assert.equal(f.released,1);
 });
}
test('explicit reconciliation permits skip of an already saved entry without rereading source',async()=>{
 const original=fixture({request:async action=>{if(action==='upload.finish')throw Error('lost ACK');}});let failed;
 await assert.rejects(api.uploadFolder(original),error=>{failed=error;return error.unknownCommit;});
 const saved=(ctx,complete=false)=>{const value=ctx.status();value.state=complete?'complete':'ready';value.entries=[{entry_index:0,state:'saved',file_id:F,receipt:structuredClone(ctx.receipt)}];return value;};
 const reconciliation=fixture({request:async(action,_data,ctx)=>action==='folder.status'?saved(ctx):undefined});
 const known=await api.reconcileFolder({...reconciliation,state:failed.transferState});
 const resumed=fixture({request:async(action,_data,ctx)=>action==='folder.status'?saved(ctx):action==='folder.finish'?saved(ctx,true):undefined});
 resumed.sources[0].read=async()=>{throw Error('confirmed entry must not be read again');};
 assert.equal((await api.uploadFolder({...resumed,state:known.transferState})).state,'complete');
 assert.deepEqual(resumed.calls.map(x=>x.action),['folder.capabilities','folder.status','folder.finish']);assert.equal(resumed.released,1);
});
test('an authoritative uploading entry is not resubmitted after reconciliation',async()=>{
 const initial=fixture({request:async action=>{if(action==='upload.finish')throw Error('lost ACK');}});let failed;
 await assert.rejects(api.uploadFolder(initial),error=>{failed=error;return error.unknownCommit;});
 const waiting=fixture({request:async(action,_data,ctx)=>{if(action==='folder.status'){const value=ctx.status();value.entries=[{entry_index:0,state:'uploading'}];return value;}}});
 const state=(await api.reconcileFolder({...waiting,state:failed.transferState})).transferState;
 await assert.rejects(api.uploadFolder({...waiting,state}),error=>error.code==='folder_entry_busy');
 assert.ok(!waiting.calls.some(x=>x.action==='folder.file.begin'||x.action==='upload.finish'));assert.equal(waiting.released,1);
});
test('resume state cannot switch its project or exchange directory',async()=>{
 const first=fixture({request:async action=>{if(action==='folder.begin')throw Error('lost ACK');}});let failed;
 await assert.rejects(api.uploadFolder(first),error=>{failed=error;return error.unknownCommit;});
 const next=fixture();next.workspace.exchange_dir='C:\\different\\tmp';
 await assert.rejects(api.uploadFolder({...next,state:failed.transferState}),error=>error.code==='target_changed');assert.equal(next.calls.length,0);assert.equal(next.released,1);
});
test('known nonce retains the exact original timezone offset',async()=>{
 const original=fixture({request:async action=>{if(action==='folder.begin')throw Error('lost ACK');}});let failed;
 await assert.rejects(api.uploadFolder(original),error=>{failed=error;return error.unknownCommit;});
 failed.transferState.phase='reconciled';failed.transferState.timezone_offset_minutes=123;
 const next=fixture();await api.uploadFolder({...next,state:failed.transferState});
 const sent=next.calls.find(x=>x.action==='folder.begin').data;
 assert.equal(sent.nonce,failed.transferState.nonce);assert.equal(sent.timezone_offset_minutes,123);
});
test('directory ancestors may use existing valid 255-unit components',async()=>{
 const f=fixture({request:async action=>{if(action==='folder.begin')throw Error('inspect only');}});
 f.workspace.exchange_dir='C:\\'+ 'p'.repeat(255)+'\\tmp';
 await assert.rejects(api.uploadFolder(f),error=>error.unknownCommit);assert.ok(f.calls.some(x=>x.action==='folder.begin'));assert.equal(f.released,1);
});
for(const mutate of [r=>r.name='other.txt',r=>r.folder.entry_index=1,r=>r.folder.manifest_digest='0'.repeat(64),r=>r.sha256='0'.repeat(64),r=>r.size++,r=>r.daemon_epoch='0'.repeat(64),r=>r.workspace_revision=1,r=>r.path='C:\\project\\tmp\\elsewhere\\owned.txt',r=>r.path=r.path.replace('nested','nested\\..')]){
 test('saved receipts require original basename, binding, digest and exact manifest path',async()=>{
  const f=fixture({request:async(action,_data,ctx)=>{if(action==='folder.status'){const value=ctx.status(),receipt=structuredClone(ctx.receipt);mutate(receipt);value.entries=[{entry_index:0,state:'saved',file_id:F,receipt}];return value;}}});
  await assert.rejects(api.uploadFolder(f),error=>error.code==='invalid_response');assert.ok(!f.calls.some(x=>x.action==='folder.finish'));assert.equal(f.released,1);
 });
}
test('insufficient receiver capabilities reject before reserving a folder',async()=>{
 const f=fixture({request:async action=>action==='folder.capabilities'?{version:1,max_files:0,max_directories:256,max_total_bytes:1073741824}:undefined});
 await assert.rejects(api.uploadFolder(f),error=>error.code==='folder_unsupported');assert.equal(f.calls.length,1);assert.equal(f.released,1);
});
async function pendingState(){
 const f=fixture({request:async action=>{if(action==='folder.begin')throw Error('lost begin ACK');}});let state;
 await assert.rejects(api.uploadFolder(f),error=>{state=error.transferState;return error.unknownCommit;});
 return state;
}
test('explicit cancellation resolves a nonce and preserves confirmed saved receipts',async()=>{
 const state=await pendingState();
 const saved=ctx=>{const value=ctx.status();value.entries=[{entry_index:0,state:'saved',file_id:F,receipt:structuredClone(ctx.receipt)}];return value;};
 const f=fixture({request:async(action,_data,ctx)=>{if(action==='folder.lookup')return saved(ctx);if(action==='folder.cancel'){const value=saved(ctx);value.state='cancelled';return value;}}});
 const result=await api.cancelFolder({...f,state});
 assert.equal(result.state,'cancelled');assert.equal(result.receipts.length,1);assert.equal(result.transferState.phase,'cancelled');
 assert.deepEqual(f.calls.map(x=>x.action),['folder.lookup','folder.cancel']);assert.equal(f.released,0);
});
test('explicit cancellation with a known batch never uploads or begins another folder',async()=>{
 const state=await pendingState();state.batch_id=B;
 const f=fixture();assert.equal((await api.cancelFolder({...f,state})).state,'cancelled');
 assert.deepEqual(f.calls.map(x=>x.action),['folder.cancel']);assert.equal(f.released,0);
});
test('unknown nonce lookup cannot be reported as cancelled',async()=>{
 const state=await pendingState(),f=fixture({request:async action=>{if(action==='folder.lookup')throw Object.assign(Error('not found'),{code:'unknown_folder'});}});
 await assert.rejects(api.cancelFolder({...f,state}),error=>error.code==='unknown_folder'&&error.transferState.phase!=='cancelled');
 assert.deepEqual(f.calls.map(x=>x.action),['folder.lookup']);
});
test('explicit cancellation respects captured target and refuses changed workspace',async()=>{
 const state=await pendingState(),f=fixture();f.workspace.exchange_dir='C:\\different\\tmp';
 await assert.rejects(api.cancelFolder({...f,state}),error=>error.code==='target_changed');assert.equal(f.calls.length,0);
 const g=fixture();g.current=()=>false;
 await assert.rejects(api.cancelFolder({...g,state}),error=>error.code==='target_changed');assert.equal(g.calls.length,0);
});
test('forged cancellation response remains unknown rather than claiming a stopped batch',async()=>{
 const state=await pendingState();state.batch_id=B;
 const f=fixture({request:async(action,_data,ctx)=>{if(action==='folder.cancel'){const value=ctx.status();value.batch_id='0'.repeat(48);value.state='cancelled';return value;}}});
 await assert.rejects(api.cancelFolder({...f,state}),error=>error.unknownCommit&&error.transferState.phase==='cancel_unknown');
 assert.deepEqual(f.calls.map(x=>x.action),['folder.cancel']);
});
test('lost cancellation ACK retains bound metadata for later explicit status checks',async()=>{
 const state=await pendingState();state.batch_id=B;
 const f=fixture({request:async action=>{if(action==='folder.cancel')throw Error('lost cancel ACK');}});let failed;
 await assert.rejects(api.cancelFolder({...f,state}),error=>{failed=error;return error.unknownCommit&&error.transferState.phase==='cancel_unknown';});
 const g=fixture({request:async(action,_data,ctx)=>{if(action==='folder.status'){const value=ctx.status();value.state='cancelled';return value;}}});
 assert.equal((await api.reconcileFolder({...g,state:failed.transferState})).state,'cancelled');
 assert.deepEqual(g.calls.map(x=>x.action),['folder.status']);
});
test('cancelling an already complete batch retains the complete outcome',async()=>{
 const state=await pendingState();state.batch_id=B;
 const f=fixture({request:async(action,_data,ctx)=>{if(action==='folder.cancel'){const value=ctx.status();value.state='complete';value.entries=[{entry_index:0,state:'saved',file_id:F,receipt:structuredClone(ctx.receipt)}];return value;}}});
 const result=await api.cancelFolder({...f,state});assert.equal(result.state,'complete');assert.equal(result.transferState.phase,'complete');assert.equal(result.receipts.length,1);
});
