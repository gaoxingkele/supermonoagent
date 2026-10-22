const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const {webcrypto}=require('node:crypto');
const MobileCore=require('../android/assets/mobile/mobile-core.js');

function client(){
  class Element{
    constructor(){this.value='';this.children=[];this.hidden=false;this.attributes={};this.dataset={};}
    replaceChildren(...children){this.children=children;}
    append(...children){this.children.push(...children);}
    setAttribute(name,value){this.attributes[name]=value;}
    addEventListener(){} showModal(){} close(){} reset(){} focus(){}
  }
  const nodes=new Map(),storage=new Map(),sockets=[];
  const get=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
  class Socket{
    constructor(url){this.url=url;this.sent=[];this.readyState=1;sockets.push(this);}
    send(value){this.sent.push(value);} close(){this.closed=true;}
  }
  const context={document:{getElementById:get,createElement:()=>new Element(),querySelectorAll:()=>[],addEventListener(){}},
    location:{hash:'#'+encodeURIComponent(JSON.stringify({endpoint:'http://home:1234',token:'home-secret'})),pathname:'/mobile/index.html'},
    addEventListener(){},history:{replaceState(){}},localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},
    TerminalClient:{pack:(op,value)=>({op,value}),retryDelay:()=>100},MobileCore,
    TextEncoder,TextDecoder,URL,Uint8Array,Uint32Array,crypto:webcrypto,WebSocket:Socket,performance:{now:()=>0},
    ResizeObserver:class{observe(){}},setTimeout:()=>1,clearTimeout(){},setInterval:()=>2,clearInterval(){},cancelAnimationFrame(){}};
  context.window=context;
  vm.runInNewContext(fs.readFileSync(require('node:path').join(__dirname,'../android/assets/mobile/app.js'),'utf8'),context);
  const add=(name,token)=>{
    get('host-name').value=name;get('host-endpoint').value='http://other:5678';get('host-code').value='room';get('host-token').value=token;
    get('host-form').onsubmit({preventDefault(){}});
  };
  return {get,storage,sockets,add};
}
test('saved computers retain independent transports and scope authentication',()=>{
  const c=client(),home=c.sockets[0];home.onopen();
  assert.equal(new URL(home.url).search,'');
  assert.equal(JSON.parse(new TextDecoder().decode(home.sent[0].value)).token,'home-secret');
  c.add('Office','office-secret');assert.ok(!home.closed);
  const first=c.sockets.filter(s=>s.binaryType==='arraybuffer').at(-1);first.onopen();assert.equal(JSON.parse(first.sent[0]).token,'office-secret');
  const saved=JSON.parse(c.storage.get('sma.mobile.hosts'));assert.equal(saved[0].name,'Office');assert.equal('token' in saved[0],false);assert.ok(!c.storage.get('sma.mobile.hosts').includes('secret'));
  c.add('Laptop','laptop-secret');const second=c.sockets.filter(s=>s.binaryType==='arraybuffer').at(-1);second.onopen();assert.equal(JSON.parse(second.sent[0]).token,'laptop-secret');assert.ok(!first.closed);assert.ok(!home.closed);
  const count=c.sockets.length;
  c.get('host-list').children[1].children[0].onclick();
  c.get('host-list').children[0].children[0].onclick();
  assert.equal(c.sockets.length,count);assert.ok(!second.closed);assert.ok(!first.closed);assert.ok(!home.closed);
  first.onclose({code:4001});assert.ok(!home.closed);assert.ok(!second.closed);
  assert.equal(c.get('host-token').value,'');
});
test('invalid endpoint cannot create a saved computer or transport',()=>{
  const c=client();c.get('host-name').value='Bad';c.get('host-endpoint').value='https://user:secret@example.test/?token=secret';
  c.get('host-form').onsubmit({preventDefault(){}});
  assert.equal(c.sockets.length,1);assert.equal(c.storage.size,0);assert.equal(c.get('host-error').hidden,false);
});
