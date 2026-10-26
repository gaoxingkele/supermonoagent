/* Independent file broker JSON transport; no terminal or native API dependency. */
(function(root,factory){
  'use strict';
  const api=factory(root);
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.SuperMonoFileClient=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(root){
  'use strict';
  const MAX_FRAME=262144,TIMEOUT=15000,MAX_PENDING=64,FILE_PROTOCOL=2;
  const encoder=new TextEncoder();
  const bindingErrors={project_binding_required:'项目绑定不可用，请更新接收电脑的文件助手和客户端后重试。',project_binding_changed:'接收项目或目录已变化，请重新确认当前项目后重试。',file_protocol_required:'文件服务协议不匹配，请升级接收电脑的文件助手和中继文件服务。'};
  class FileClientError extends Error{constructor(code){super(Object.hasOwn(bindingErrors,code)?bindingErrors[code]:code);this.name='FileClientError';this.code=code;}}
  const fail=code=>{throw new FileClientError(code);};
  const object=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
  function parse(value){
    if(typeof value!=='string'||!value||value.length>4096||/[\s\u0000-\u001f\u007f\\?#]/u.test(value))fail('invalid_endpoint');
    let url;try{url=new URL(value);}catch{fail('invalid_endpoint');}
    if(url.username||url.password||!url.hostname||url.port==='0')fail('invalid_endpoint');
    if(['ws:','http:'].includes(url.protocol)){
      const authority=value.slice(value.indexOf('://')+3).split('/')[0];
      if(!/^(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]+)?$/i.test(authority))fail('insecure_endpoint');
    }
    return url;
  }
  function brokerURL(value){
    const url=parse(value);
    if(!['https:','http:','wss:','ws:'].includes(url.protocol))fail('invalid_endpoint');
    const protocol=['https:','wss:'].includes(url.protocol)?'wss:':'ws:';
    return protocol+'//'+url.host+'/file/ws';
  }
  function endpoint(value){
    const url=parse(value);
    if(!['wss:','ws:'].includes(url.protocol)||url.pathname!=='/file/ws')fail('invalid_endpoint');
    return url.href;
  }
  function nonce(){
    if(!root.crypto?.getRandomValues)fail('unsupported');
    return [...root.crypto.getRandomValues(new Uint8Array(12))].map(value=>value.toString(16).padStart(2,'0')).join('');
  }
  function createClient(options){
    if(!object(options))fail('invalid_configuration');
    const url=endpoint(options.url),room=options.room,token=options.token;
    if(typeof room!=='string'||!room||room.length>128||room!==room.trim()||/[\u0000-\u001f\u007f]/u.test(room))fail('invalid_room');
    if(typeof token!=='string'||!token||token.length>8192||/[\u0000-\u001f\u007f]/u.test(token))fail('missing_token');
    let socket=null,epoch=0,closed=false,ready=null,opening=null,sequence=0,prefix='';
    const pending=new Map();
    function disconnect(code,ws,generation){
      if(ws!==socket||generation!==epoch)return;
      socket=null;epoch++;ready=null;
      if(opening){clearTimeout(opening.timer);opening.reject(new FileClientError(code));opening=null;}
      for(const entry of pending.values()){clearTimeout(entry.timer);entry.reject(new FileClientError(code));}
      pending.clear();
      try{ws?.close();}catch{}
    }
    function connect(){
      if(closed)return Promise.reject(new FileClientError('closed'));
      if(ready&&socket?.readyState===1)return Promise.resolve(ready);
      if(opening)return opening.promise;
      if(typeof root.WebSocket!=='function')return Promise.reject(new FileClientError('unsupported'));
      prefix=nonce();sequence=0;
      const generation=++epoch,hello=prefix+'_hello';
      let ws;try{ws=new root.WebSocket(url+'?file_protocol=2');}catch{return Promise.reject(new FileClientError('connection_failed'));}
      socket=ws;
      let resolve,reject;
      const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});
      const timer=setTimeout(()=>disconnect('ready_timeout',ws,generation),TIMEOUT);
      opening={promise,resolve,reject,timer};
      const current=()=>socket===ws&&epoch===generation;
      ws.onopen=()=>{
        if(!current())return;
        try{ws.send(JSON.stringify({protocol:'sma-files-1',file_protocol:FILE_PROTOCOL,id:hello,role:'client',room,token}));}
        catch{disconnect('connection_failed',ws,generation);}
      };
      ws.onmessage=event=>{
        if(!current())return;
        let message;
        try{
          if(typeof event.data!=='string'||encoder.encode(event.data).length>MAX_FRAME)fail('invalid_response');
          message=JSON.parse(event.data);if(!object(message))fail('invalid_response');
        }catch{disconnect('invalid_response',ws,generation);return;}
        if(!ready){
          if(message.file_protocol!==FILE_PROTOCOL){disconnect('file_protocol_required',ws,generation);return;}
          if(message.type!=='ready'||message.id!==hello||message.role!=='client'||message.room!==room||typeof message.agent_online!=='boolean'){
            disconnect('invalid_response',ws,generation);return;
          }
          ready={room,agent_online:message.agent_online};
          const entry=opening;opening=null;clearTimeout(entry.timer);entry.resolve(ready);return;
        }
        if(message.type!=='response'||typeof message.id!=='string')return;
        const entry=pending.get(message.id);if(!entry)return;
        pending.delete(message.id);clearTimeout(entry.timer);
        if(message.ok===true&&object(message.result))entry.resolve(message.result);
        else if(message.ok===false){const code=typeof message.error==='string'&&/^[a-z_]{1,64}$/.test(message.error)?message.error:'service_unavailable';entry.reject(new FileClientError(code));}
        else entry.reject(new FileClientError('invalid_response'));
      };
      ws.onerror=()=>disconnect(opening?'connection_failed':'disconnected',ws,generation);
      ws.onclose=()=>disconnect(opening?'authentication_failed':'disconnected',ws,generation);
      return promise;
    }
    async function request(action,session,data={}){
      if(typeof action!=='string'||action.length>64||!/^[a-z]+(?:\.[a-z]+)+$/.test(action)
        ||typeof session!=='string'||!session||session.length>256||/[\u0000-\u001f\u007f]/u.test(session)||!object(data))fail('invalid_request');
      // Validate and copy before connecting; invalid requests must not send credentials.
      let copied;try{copied=JSON.parse(JSON.stringify(data));}catch{fail('invalid_request');}
      if(!object(copied))fail('invalid_request');
      if(action==='upload.begin'&&(typeof copied.project_binding!=='string'||!/^[a-f0-9]{64}$/.test(copied.project_binding)))fail('project_binding_required');
      if(encoder.encode(JSON.stringify({type:'request',id:'x'.repeat(64),action,session,data:copied})).length>MAX_FRAME)fail('request_too_large');
      await connect();
      if(closed||!socket||socket.readyState!==1)fail('disconnected');
      if(pending.size>=MAX_PENDING)fail('service_busy');
      if(socket.bufferedAmount>MAX_FRAME*4)fail('service_busy');
      const ws=socket,generation=epoch,id=prefix+'_'+(++sequence).toString(36);
      const raw=JSON.stringify({type:'request',id,action,session,data:copied});
      return new Promise((resolve,reject)=>{
        const timer=setTimeout(()=>{if(pending.delete(id))reject(new FileClientError('request_timeout'));},TIMEOUT);
        pending.set(id,{resolve,reject,timer});
        try{if(ws!==socket||generation!==epoch)fail('disconnected');ws.send(raw);}
        catch{disconnect('disconnected',ws,generation);}
      });
    }
    function close(){closed=true;disconnect('closed',socket,epoch);}
    return {connect,request,close,get connected(){return !!ready&&socket?.readyState===1;},get pendingCount(){return pending.size;}};
  }
  return {createClient,brokerURL,FileClientError,MAX_FRAME};
});
