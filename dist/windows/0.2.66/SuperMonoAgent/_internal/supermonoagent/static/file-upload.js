(function(root,factory){
  'use strict';
  const api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.SuperMonoFileUpload=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  const object=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
  const hex=(value,length)=>typeof value==='string'&&new RegExp('^[a-f0-9]{'+length+'}$').test(value);
  function workspace(value,session){
    if(!object(value)||typeof value.window_id!=='string'||!value.window_id||value.window_id.length>64
      ||typeof value.daemon_epoch!=='string'||!value.daemon_epoch||value.daemon_epoch.length>128
      ||!Number.isSafeInteger(value.revision)||value.revision<0
      ||value.session_id!==undefined&&value.session_id!==session)throw Error('文件目录回执无效。');
    if(value.project_status==='selection_required'&&value.upload_ready!==true)throw Object.assign(Error('请先在底部选择当前项目目录，附件将保存到该项目的 tmp。'),{code:'project_required'});
    return value;
  }
  function validateSource(source){
    if(!object(source)||typeof source.name!=='string'||!source.name||source.name.length>200
      ||!Number.isSafeInteger(source.size)||source.size<0||source.size>512*1024*1024)throw Error('附件名称或大小无效。');
  }
  async function uploadExchange({session,getWorkspace,request,sources,isCurrent,signal,onProgress=()=>{}}){
    if(typeof session!=='string'||!session||session.length>256||typeof getWorkspace!=='function'
      ||typeof request!=='function'||typeof isCurrent!=='function'||!Array.isArray(sources)||sources.length>32)throw Error('上传请求无效。');
    const receipts=[],check=()=>{if(signal?.aborted)throw Error('文件传输已取消。');if(!isCurrent())throw Error('目标窗口或连接已变化。');};
    const progress=value=>{try{onProgress(value);}catch{}};
    try{for(const [index,source] of sources.entries()){
      validateSource(source);let upload_id=null,file_id=null,finishSent=false;
      try{
        check();progress({phase:'preparing',name:source.name,index,total:sources.length,loaded:0,size:source.size});
        let currentWorkspace;
        try{currentWorkspace=workspace(await getWorkspace(),session);}catch(error){error.beforeUpload=true;throw error;}
        const sha256=source.prepare?await source.prepare():source.sha256;
        if(!hex(sha256,64))throw Error('附件 SHA-256 无效。');check();
        const begin=await request('upload.begin',{original_name:source.name,size:source.size,sha256,
          window_id:currentWorkspace.window_id,daemon_epoch:currentWorkspace.daemon_epoch,
          workspace_revision:currentWorkspace.revision,timezone_offset_minutes:-new Date().getTimezoneOffset()});
        upload_id=begin?.upload_id;
        if(!hex(upload_id,48))throw Error('上传标识回执无效。');
        let offset=0;
        while(offset<source.size){
          check();const chunk=await source.read(offset);check();
          if(!chunk||typeof chunk.data!=='string'||!chunk.data||!Number.isSafeInteger(chunk.next)||chunk.next<=offset||chunk.next>source.size)throw Error('源文件分块无效。');
          const response=await request('upload.chunk',{upload_id,offset,data:chunk.data});
          if(response.received!==chunk.next)throw Error('接收进度不一致。');offset=chunk.next;
          progress({phase:'uploading',name:source.name,index,total:sources.length,loaded:offset,size:source.size});
        }
        check();finishSent=true;
        progress({phase:'submitting',name:source.name,index,total:sources.length,loaded:source.size,size:source.size,upload_id});
        const queued=await request('upload.finish',{upload_id});
        if(!hex(queued?.file_id,48))throw Error('提交结果无效。');upload_id=null;file_id=queued.file_id;
        progress({phase:'receiving',name:source.name,index,total:sources.length,loaded:source.size,size:source.size,file_id});
        const deadline=performance.now()+180000;let result;
        while(performance.now()<deadline){
          if(!isCurrent())throw Object.assign(Error('文件已存入中继，但目标连接已变化；请核对接收状态。'),{pendingFileId:file_id});
          result=await request('file.status',{file_id});
          if(result.state==='saved')break;
          if(['failed','cancelled'].includes(result.state))throw Error('目标电脑未能保存文件，请检查文件助手。');
          await new Promise(resolve=>setTimeout(resolve,250));
        }
        if(result?.state!=='saved')throw Object.assign(Error('文件已存入中继，尚未收到目标保存确认。'),{pendingFileId:file_id});
        const saved=result.receipt;
        if(!saved||typeof saved.path!=='string'||!saved.path||saved.path.length>4096||/[\u0000-\u001f\u007f]/.test(saved.path)
          ||saved.name!==begin.name||saved.size!==source.size||saved.sha256!==sha256)throw Error('目标保存回执无效。');
        const receipt=Object.freeze({name:source.name,savedName:saved.name,path:saved.path,size:saved.size,sha256});
        receipts.push(receipt);source.saved=true;file_id=null;
        progress({phase:'saved',name:source.name,index,total:sources.length,loaded:source.size,size:source.size,receipt});
      }catch(error){
        if(file_id&&!error.pendingFileId)error.pendingFileId=file_id;
        if(finishSent&&!file_id){error.unknownCommit=true;error.pendingUploadId=upload_id;error.message='提交结果待确认，勿重复上传；请先核对目标项目 tmp 的接收情况。';}
        throw error;
      }finally{
        if(upload_id&&!finishSent)try{await request('upload.cancel',{upload_id});}catch{}
        try{await source.dropPrepared?.();}catch{}
      }
    }}catch(error){error.receipts=[...receipts];throw error;}
    return receipts;
  }
  return {upload:uploadExchange};
});
