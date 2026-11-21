'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {cockpitEndpoint,createCockpitReader} = require('../src/supermonoagent/static/terminal-client.js');

test('cockpit addresses require TLS beyond loopback and the dedicated viewer path',()=>{
  assert.equal(cockpitEndpoint('wss://relay.example'),'wss://relay.example/hub/view');
  assert.equal(cockpitEndpoint('ws://127.0.0.1:8821/hub/view'),'ws://127.0.0.1:8821/hub/view');
  for(const value of ['https://relay.example','ws://relay.example/hub/view','wss://u:p@relay.example/hub/view',
    'wss://relay.example/hub/mcp','wss://relay.example/hub/view?token=x','wss://relay.example/hub/view#x','wss://bad\\@other/']){
    assert.throws(()=>cockpitEndpoint(value),value);
  }
});

test('only granted project read operations can be issued; disconnect rejects pending reads', async()=>{
  let socket;
  class Socket {
    constructor(url){socket=this;this.url=url;this.sent=[];}
    send(raw){this.sent.push(JSON.parse(raw));}
    close(){this.closed=true;}
    reply(value){this.onmessage({data:JSON.stringify(value)});}
  }
  const reader=createCockpitReader({endpoint:'ws://localhost:8821',project:'p1',token:'test-read',Socket});
  socket.onopen();
  assert.deepEqual(socket.sent,[{op:'hello',token:'test-read',project:'p1'}]);
  assert.ok(!socket.url.includes('test-read'));
  socket.reply({op:'ready',project:'p1',projects:['p1','p2']});
  await reader.ready;
  await assert.rejects(reader.request('report_step',{}));
  await assert.rejects(reader.request('get_task_status',{task_id:'x'},'p3'));
  await assert.rejects(reader.request('read_skill',{skill_id:'x'},'*'));
  assert.equal(socket.sent.length,1);
  const pending=reader.request('list_tasks',{limit:20},'*');
  const last=socket.sent.at(-1);
  assert.equal(last.tool,'list_tasks');assert.equal(last.project,'*');assert.equal(last.token,undefined);
  socket.reply({op:'response',id:last.id,ok:true,result:{tasks:[]}});
  assert.deepEqual(await pending,{tasks:[]});
  const revoked=reader.request('get_usage_overview',{},'p2');
  socket.onclose();
  await assert.rejects(revoked);
  await assert.rejects(reader.request('list_tasks',{}));
});

test('a ready response for another project never authorizes queries',async()=>{
  let socket;
  class Socket {constructor(){socket=this;} close(){} send(){}}
  const reader=createCockpitReader({endpoint:'ws://localhost:8821',project:'p1',token:'test-read',Socket});
  socket.onmessage({data:JSON.stringify({op:'ready',project:'p2'})});
  await assert.rejects(reader.ready);
  await assert.rejects(reader.request('list_tasks',{}));
});
