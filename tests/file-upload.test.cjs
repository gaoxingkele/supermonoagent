const test=require('node:test');
const assert=require('node:assert/strict');
const {upload}=require('../src/supermonoagent/static/file-upload.js');
const workspace={window_id:'window1',session_id:'session1',daemon_epoch:'a'.repeat(64),revision:0,project_status:'ready'};
const source=data=>({name:'owned.bin',size:data.length,sha256:require('node:crypto').createHash('sha256').update(data).digest('hex'),
  read:async offset=>({data:Buffer.from(data.subarray(offset,offset+96*1024)).toString('base64'),next:Math.min(data.length,offset+96*1024)})});

test('shared exchange uploads chunks and validates a saved receipt',async()=>{
  const data=Buffer.alloc(150000,7),actions=[];
  const saved=await upload({session:'session1',getWorkspace:async()=>workspace,sources:[source(data)],isCurrent:()=>true,
    request:async(action,body)=>{
      actions.push({action,body});
      if(action==='upload.begin')return {upload_id:'b'.repeat(48),name:'owned.bin'};
      if(action==='upload.chunk')return {received:body.offset+Buffer.from(body.data,'base64').length};
      if(action==='upload.finish')return {file_id:'c'.repeat(48)};
      if(action==='file.status')return {state:'saved',receipt:{name:'owned.bin',path:'D:/owned/tmp/owned.bin',size:data.length,sha256:source(data).sha256}};
      throw Error(action);
    }});
  assert.equal(saved[0].path,'D:/owned/tmp/owned.bin');
  assert.deepEqual(actions.map(item=>item.action),['upload.begin','upload.chunk','upload.chunk','upload.finish','file.status']);
  assert.equal(actions[0].body.workspace_revision,0);
  assert.equal(Buffer.concat(actions.filter(item=>item.action==='upload.chunk').map(item=>Buffer.from(item.body.data,'base64'))).compare(data),0);
});

test('only workspace discovery failure is safe for a fallback',async()=>{
  let begins=0;
  await assert.rejects(upload({session:'session1',getWorkspace:async()=>{throw Object.assign(Error('missing'),{code:'host_unavailable'});},
    sources:[source(Buffer.from('x'))],isCurrent:()=>true,request:async()=>{begins++;}},),error=>error.beforeUpload===true&&begins===0);
  await assert.rejects(upload({session:'session1',getWorkspace:async()=>({...workspace,project_status:'selection_required'}),
    sources:[source(Buffer.from('x'))],isCurrent:()=>true,request:async()=>{begins++;}},),error=>error.code==='project_required'&&begins===0);
  await assert.rejects(upload({session:'session1',getWorkspace:async()=>({...workspace,project_status:'selection_required',upload_ready:false}),
    sources:[source(Buffer.from('x'))],isCurrent:()=>true,request:async()=>{begins++;}},),error=>error.code==='project_required'&&begins===0);
});

test('selection_required with a ready central drop starts uploading',async()=>{
  const data=Buffer.alloc(9,3),actions=[];
  const saved=await upload({session:'session1',getWorkspace:async()=>({...workspace,project_status:'selection_required',upload_ready:true,upload_dir:'D:/aicoding/supermonoagent/tmp'}),
    sources:[source(data)],isCurrent:()=>true,
    request:async(action,body)=>{
      actions.push(action);
      if(action==='upload.begin')return {upload_id:'b'.repeat(48),name:'owned.bin'};
      if(action==='upload.chunk')return {received:body.offset+Buffer.from(body.data,'base64').length};
      if(action==='upload.finish')return {file_id:'c'.repeat(48)};
      if(action==='file.status')return {state:'saved',receipt:{name:'owned.bin',path:'D:/aicoding/supermonoagent/tmp/owned.bin',size:data.length,sha256:source(data).sha256}};
      throw Error(action);
    }});
  assert.equal(saved[0].path,'D:/aicoding/supermonoagent/tmp/owned.bin');
  assert.deepEqual(actions,['upload.begin','upload.chunk','upload.finish','file.status']);
});

test('unknown finish and staged receipt states prevent blind retries',async()=>{
  const id='b'.repeat(48);
  await assert.rejects(upload({session:'session1',getWorkspace:async()=>workspace,sources:[source(Buffer.from('x'))],isCurrent:()=>true,
    request:async action=>{if(action==='upload.begin')return {upload_id:id,name:'owned.bin'};if(action==='upload.chunk')return {received:1};if(action==='upload.finish')throw Error('timeout');throw Error(action);}}),
    error=>error.unknownCommit===true&&error.pendingUploadId===id);
  await assert.rejects(upload({session:'session1',getWorkspace:async()=>workspace,sources:[source(Buffer.from('x'))],isCurrent:()=>true,
    request:async action=>{if(action==='upload.begin')return {upload_id:id,name:'owned.bin'};if(action==='upload.chunk')return {received:1};if(action==='upload.finish')return {file_id:'c'.repeat(48)};if(action==='file.status')throw Error('disconnected');throw Error(action);}}),
    error=>error.pendingFileId==='c'.repeat(48));
});
