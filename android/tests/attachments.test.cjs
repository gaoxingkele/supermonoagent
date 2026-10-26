const {test}=require('node:test');
const assert=require('node:assert/strict');
const {createHash}=require('node:crypto');
const {uploadAttachment}=require('../assets/mobile/mobile-core.js');

test('attachment chunks and SHA256 match the original and only a save receipt returns a path',async()=>{
  const bytes=Buffer.alloc(100123,137),chunks=[],id='a'.repeat(48),progress=[];
  const file=new File([bytes],'图片.png',{type:'image/png'});
  let hash,received=0;
  const result=await uploadAttachment(file,{isCurrent:()=>true,onProgress:n=>progress.push(n),request:async body=>{
    if(body.action==='begin'){hash=body.sha256;return {id,received:0};}
    if(body.action==='chunk'){assert.equal(body.offset,received);const part=Buffer.from(body.data,'base64');assert.ok(part.length<=96*1024);chunks.push(part);received+=part.length;return {received};}
    if(body.action==='finish')return {path:'C:\\exchange\\图片.png'};
    assert.fail('Successful upload must not cancel');
  }});
  assert.deepEqual(Buffer.concat(chunks),bytes);
  assert.equal(hash,createHash('sha256').update(bytes).digest('hex'));
  assert.equal(result.path,'C:\\exchange\\图片.png');
  assert.deepEqual(progress,[96*1024,bytes.length]);
});

test('bad progress cancels the partial file without returning a receipt',async()=>{
  const calls=[];
  await assert.rejects(uploadAttachment(new File(['hello'],'notes.txt'),{isCurrent:()=>true,request:async body=>{
    calls.push(body.action);return body.action==='begin'?{id:'b'.repeat(48),received:0}:{received:0};
  }}),/进度/);
  assert.deepEqual(calls,['begin','chunk','cancel']);
});

test('a lost finish acknowledgement is uncertain and cannot be called saved',async()=>{
  const calls=[];
  await assert.rejects(uploadAttachment(new File([],'empty.txt'),{isCurrent:()=>true,request:async body=>{
    calls.push(body.action);if(body.action==='begin')return {id:'c'.repeat(48),received:0};if(body.action==='finish')throw Error('lost reply');return {};
  }}),error=>error.uncertain===true&&error.message==='lost reply');
  assert.deepEqual(calls,['begin','finish','cancel']);
});

test('target changes and oversized attachments cannot send bytes',async()=>{
  let sent=0;
  const request=async()=>{sent++;};
  await assert.rejects(uploadAttachment(new File(['x'],'notes.txt'),{isCurrent:()=>false,request}),/目标/);
  await assert.rejects(uploadAttachment({name:'big.bin',size:33*1024*1024},{isCurrent:()=>true,request}),/32 MiB/);
  assert.equal(sent,0);
});
