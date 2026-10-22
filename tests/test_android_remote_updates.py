"""Real JVM crypto/ZIP/store and HTTPS fixtures for the Android native updater."""
import base64
import copy
import hashlib
import http.server
import io
import json
from pathlib import Path
import shutil
import ssl
import subprocess
import threading
import zipfile

import pytest

ROOT=Path(__file__).resolve().parents[1]
PACKAGE=ROOT/'android/src/dev/supermonoagent/mobile'
FEED='https://updates.example.test/updates/stable/manifest.json'
VECTORS=ROOT/'.omx/artifacts/ui-updates/shared-vectors.json'
OPENSSL=shutil.which('openssl') or next((str(p) for p in [Path('D:/Espressif/tools/idf-git/2.44.0/mingw64/bin/openssl.exe'),Path('D:/KimiData/daimon-bundle/runtime/git/mingw64/bin/openssl.exe')] if p.exists()),None)
HARNESS=r'''
import dev.supermonoagent.mobile.*;
import java.nio.file.*;
import java.net.*;
import java.util.*;
public class RemoteCheck {
 static byte[] bytes(String p)throws Exception{return Files.readAllBytes(Paths.get(p));}
 static void check(boolean ok,String why){if(!ok)throw new AssertionError(why);}
 static void reject(Throwing action)throws Exception{try{action.run();}catch(Exception expected){return;}throw new AssertionError("Accepted invalid input");}
 interface Throwing{void run()throws Exception;}
 static Map<String,byte[]> baseline(){Map<String,byte[]> files=new LinkedHashMap<>();for(String name:RemoteUpdates.FILES)files.put(name,("effective USB baseline "+name).getBytes(java.nio.charset.StandardCharsets.UTF_8));return files;}
 public static void main(String[] a)throws Exception{
  if(a[0].equals("download")){
   java.net.CookieManager cookies=new java.net.CookieManager();cookies.getCookieStore().add(URI.create(a[1]),new java.net.HttpCookie("account","MUST-NOT-SEND"));java.net.CookieHandler.setDefault(cookies);
   java.net.Authenticator.setDefault(new java.net.Authenticator(){protected java.net.PasswordAuthentication getPasswordAuthentication(){return new java.net.PasswordAuthentication("account","MUST-NOT-SEND".toCharArray());}});
   byte[] result=RemoteUpdates.download(URI.create(a[1]),Integer.parseInt(a[2]));System.out.println(RemoteUpdates.hash(result));return;}
  if(a[0].equals("html")){System.out.print(UpdateDocument.html(bytes(a[1]),new String(bytes(a[2]),"UTF-8"),"native-fixture-ticket","fixture-iframe",1,"http://native-fixture.test"));return;}
  if(a[0].equals("document")){
   String origin="https://terminal.example.test",rev="test-2",ticket="native-secret",doc=origin+"/mobile/index.html?ui_revision="+rev;
   String good="sma-update://ready?ticket="+ticket+"&revision="+rev+"&generation=7";
   check("ready".equals(UpdateDocument.action(origin,doc,good,true,ticket,rev,7)),"Valid main-frame bridge denied");
   check(UpdateDocument.action(origin,doc,good,false,ticket,rev,7)==null,"Subframe bridge accepted");
   check(UpdateDocument.action(origin,doc,good,true,ticket,rev,8)==null,"Old generation accepted");
   check(UpdateDocument.action(origin,doc,good,true,"different",rev,7)==null,"Wrong ticket accepted");
   check(UpdateDocument.action(origin,"https://hostile.test/mobile/index.html?ui_revision="+rev,good,true,ticket,rev,7)==null,"Foreign document accepted");
   check(UpdateDocument.action(origin,doc,good+"&ticket=x",true,ticket,rev,7)==null,"Duplicate capability accepted");
   check(UpdateDocument.action(origin,doc,good.replace("ready","delete"),true,ticket,rev,7)==null,"Unexpected command accepted");
   String html=UpdateDocument.html("<html><head><script src=\"app.js\"></script></head><body></body></html>".getBytes("UTF-8"),"function(c){}",ticket,rev,7,"https://terminal.example.test");
   check(html.contains("frame-src 'none'")&&html.contains("object-src 'none'")&&html.contains("X-Frame") == false,"Frame policy missing");
   check(html.indexOf("function(c)")<html.indexOf("src=\"/mobile/app.js?ui_revision="),"Bridge not immutable/pre-app");
   check(UpdateDocument.asset(origin,origin+"/mobile/app.js?ui_revision=old",rev)==null,"Mixed revision accepted");
   byte[] safe=UpdateDocument.state("{\"version\":1,\"hostId\":\"host:other\",\"activeSid\":\"sameid\",\"drafts\":[[\"[\\\"host:other\\\",\\\"sameid\\\"]\",\"unsent\"]],\"modes\":[],\"token\":\"MUST-NOT-EXPORT\",\"accounts\":{\"secret\":true}}".getBytes("UTF-8"));
   check(!new String(safe,"UTF-8").contains("MUST-NOT")&&!new String(safe,"UTF-8").contains("accounts"),"Credential exported");System.out.println("trusted-document-and-state-ok");return;
  }
  byte[] trust=bytes(a[1]);URI feed=URI.create(a[2]);long now=Long.parseLong(a[3]);
  if(a[0].equals("scenario")){
   Path root=Paths.get(a[4]);byte[] one=bytes(a[5]),zip1=bytes(a[6]),two=bytes(a[7]),zip2=bytes(a[8]),three=bytes(a[9]),zip3=bytes(a[10]),conflict=bytes(a[11]);
   java.util.concurrent.atomic.AtomicLong clock=new java.util.concurrent.atomic.AtomicLong(1000000L);RemoteUpdates.Store store=new RemoteUpdates.Store(root,trust,feed,clock::get);Map<String,byte[]> baseline=baseline();
   store.observe(one,now);store.install(one,zip1,baseline,now);
   Map<String,Object> pointer=RemoteUpdates.object(RemoteUpdates.Json.parse(Files.readAllBytes(root.resolve("state.json"))));
   Map<String,byte[]> backup=store.snapshot((String)pointer.get("previous"));for(String name:RemoteUpdates.FILES)check(Arrays.equals(baseline.get(name),backup.get(name)),"First effective backup differs");
   RemoteUpdates.Boot boot1=store.beginBoot();check(!store.acknowledge("old-ticket",boot1.revision,boot1.generation),"Wrong boot ticket accepted");check(store.acknowledge(boot1.ticket,boot1.revision,boot1.generation),"Healthy ready not committed");
   store.install(one,zip1,baseline,now);check(store.active().equals(boot1.revision),"Same payload not idempotent");reject(()->store.observe(conflict,now));
   store.observe(two,now);check(store.highest()==2,"Highest OBSERVED not persisted");reject(()->store.observe(one,now));
   java.util.concurrent.ExecutorService pool=java.util.concurrent.Executors.newFixedThreadPool(2);try{
    java.util.concurrent.Future<?> first=pool.submit(()->{try{store.install(two,zip2,baseline,now);}catch(Exception e){throw new RuntimeException(e);}});
    java.util.concurrent.Future<?> second=pool.submit(()->{try{store.install(two,zip2,baseline,now);}catch(Exception e){throw new RuntimeException(e);}});first.get();second.get();
   }finally{pool.shutdown();}
   RemoteUpdates.Boot boot2=store.beginBoot();check(!store.acknowledge(boot1.ticket,boot1.revision,boot1.generation),"Late previous ready accepted");check(!store.acknowledge(boot2.ticket,boot2.revision,"old-generation"),"Wrong install generation accepted");clock.addAndGet(20001L);check(!store.acknowledge(boot2.ticket,boot2.revision,boot2.generation),"Late ready committed after persistent deadline");
   check(store.active().equals(boot1.revision)&&store.highest()==2,"Rollback downgraded highest or lost previous");reject(()->store.install(two,zip2,baseline,now));
   reject(()->RemoteUpdates.verify(one,trust,feed,now+10000000L,true));check(store.snapshot(boot1.revision).size()==10,"Known good naturally expired snapshot unavailable");
   store.install(three,zip3,baseline,now);store.beginBoot();RemoteUpdates.Store restarted=new RemoteUpdates.Store(root,trust,feed);
   check(restarted.active().equals(boot1.revision)&&restarted.highest()==3,"Unfinished startup was not recovered");
   Files.createDirectories(root.resolve("snapshots/stage-interrupted"));new RemoteUpdates.Store(root,trust,feed).snapshot(restarted.active());
   Files.write(root.resolve("snapshots").resolve(restarted.active()).resolve("mobile/app.js"),"corrupt cached file".getBytes("UTF-8"));reject(()->restarted.snapshot(restarted.active()));restarted.rollback();
   check(restarted.active()==null&&restarted.highest()==3,"Corrupt baseline did not fall back while retaining high water");reject(()->restarted.install(three,zip3,baseline,now));
   Path usb=root.resolve("../usb/active.txt").normalize();Files.createDirectories(usb.getParent());Files.write(usb,"original-usb-revision".getBytes("UTF-8"));
   restarted.useBundled();check(restarted.active()==null&&restarted.highest()==3,"Explicit bundled recovery lowered sequence");check(new String(Files.readAllBytes(usb),"UTF-8").equals("original-usb-revision"),"USB pointer changed during bundled recovery");
   System.out.println("signature-backup-idempotence-concurrency-watchdog-restart-quarantine-ok");return;
  }
  try{RemoteUpdates.Candidate candidate=RemoteUpdates.verify(bytes(a[4]),trust,feed,now,true);
   if(a[0].equals("unpack"))RemoteUpdates.unpack(candidate,bytes(a[5]));
   else if(candidate.nativeApi!=RemoteUpdates.NATIVE_API)throw new java.io.IOException("Native shell upgrade required");
   if(a[a.length-1].equals("reject"))throw new AssertionError("Accepted invalid vector");System.out.println(candidate.revision+":"+candidate.sequence);
  }catch(Exception expected){if(!a[a.length-1].equals("reject"))throw expected;System.out.println("rejected");}
 }
}
'''

@pytest.fixture(scope='module')
def jvm(tmp_path_factory):
    javac=shutil.which('javac') or next((str(p) for p in (ROOT/'.omx/tools/jdk').glob('*/bin/javac.exe')),None)
    if not javac:pytest.skip('Portable JDK missing')
    directory=tmp_path_factory.mktemp('android-remote-jvm');source=directory/'RemoteCheck.java';source.write_text(HARNESS,'utf-8')
    subprocess.run([javac,'-encoding','UTF-8','-d',str(directory),*[str(PACKAGE/name) for name in ['RemoteUpdates.java','UpdateDocument.java','LocalAssets.java','ConnectionEndpoint.java']],str(source)],check=True,capture_output=True)
    java=str(Path(javac).with_name('java.exe' if javac.endswith('.exe') else 'java'))
    def run(*args,extra=(),success=True):
        result=subprocess.run([java,*extra,'-cp',str(directory),'RemoteCheck',*map(str,args)],capture_output=True,text=True,timeout=45)
        if success:assert result.returncode==0,result.stderr
        else:assert result.returncode!=0,'Unexpected successful operation'
        return result
    return run,java,directory

@pytest.fixture(scope='module')
def vectors():
    if not VECTORS.exists():pytest.skip('Signing team shared vectors not generated')
    return json.loads(VECTORS.read_text('utf-8'))


def test_real_java_shared_signature_and_archive_vectors(jvm,vectors,tmp_path):
    run,_,_=jvm;trust=tmp_path/'trust.json';trust.write_text(json.dumps(vectors['trust']),'utf-8')
    for item in [vectors['valid'],*vectors['invalid']]:
        envelope=tmp_path/(item['name']+'.json');envelope.write_bytes(base64.b64decode(item['envelope']))
        args=['verify',trust,FEED,vectors['now'],envelope]
        if item['stage']=='archive' or item is vectors['valid']:
            archive=tmp_path/(item['name']+'.zip');archive.write_bytes(base64.b64decode(item['archives']['android']));args=['unpack',trust,FEED,vectors['now'],envelope,archive]
        run(*args,'accept' if item is vectors['valid'] else 'reject')


def test_bridge_mainframe_generation_ticket_and_credential_free_state(jvm):
    jvm[0]('document')


def signed(payload,private):
    raw=json.dumps(payload,separators=(',',':'),sort_keys=True).encode()
    signature=subprocess.run([OPENSSL,'dgst','-sha256','-sign',str(private)],input=raw,capture_output=True,check=True).stdout
    return json.dumps({'schema':1,'key_id':'fixture','payload':base64.b64encode(raw).decode(),'signature':base64.b64encode(signature).decode()}).encode()

@pytest.fixture(scope='module')
def signing(tmp_path_factory,vectors):
    if not OPENSSL:pytest.skip('OpenSSL fixture signer unavailable')
    directory=tmp_path_factory.mktemp('android-update-signing');private=directory/'private.pem';public=directory/'public.der'
    subprocess.run([OPENSSL,'genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:3072','-out',str(private)],check=True,capture_output=True)
    subprocess.run([OPENSSL,'pkey','-in',str(private),'-pubout','-outform','DER','-out',str(public)],check=True,capture_output=True)
    trust=directory/'trust.json';trust.write_text(json.dumps({'schema':1,'keys':{'fixture':base64.b64encode(public.read_bytes()).decode()}}),'utf-8')
    payload=json.loads(base64.b64decode(json.loads(base64.b64decode(vectors['valid']['envelope']))['payload']))
    return directory,private,trust,payload


def release(directory,private,original,sequence,revision=None,zip_mutator=None):
    payload=copy.deepcopy(original);payload.update(sequence=sequence,revision=revision or 'fixture-'+str(sequence))
    files={name:('actual revision '+str(sequence)+' '+name).encode() for name in original['artifacts']['android']['files']}
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,value in files.items():archive.writestr(name,value)
        if zip_mutator:zip_mutator(archive,files)
    data=output.getvalue();artifact=payload['artifacts']['android'];artifact.update(url='releases/'+payload['revision']+'/android.zip',size=len(data),sha256=hashlib.sha256(data).hexdigest(),files={name:{'sha256':hashlib.sha256(value).hexdigest(),'size':len(value)} for name,value in files.items()})
    name=payload['revision'];envelope=directory/(name+'.json');envelope.write_bytes(signed(payload,private));archive=directory/(name+'.zip');archive.write_bytes(data);return envelope,archive


def test_effective_backup_observed_sequence_failed_boot_and_restart(jvm,signing,vectors,tmp_path):
    _,private,trust,payload=signing;one=release(tmp_path,private,payload,1);two=release(tmp_path,private,payload,2);three=release(tmp_path,private,payload,3);conflict=release(tmp_path,private,payload,1,'conflicting-one')
    jvm[0]('scenario',trust,FEED,vectors['now'],tmp_path/'store',*one,*two,*three,conflict[0])


def test_signed_symlink_zip_is_refused(jvm,signing,vectors,tmp_path):
    _,private,trust,payload=signing
    envelope,archive=release(tmp_path,private,payload,4)
    payload=json.loads(base64.b64decode(json.loads(envelope.read_bytes())['payload']))
    original=zipfile.ZipFile(archive);output=io.BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as result:
        for index,name in enumerate(original.namelist()):
            info=zipfile.ZipInfo(name);info.create_system=3;info.external_attr=((0o120777 if index==0 else 0o100644)<<16);result.writestr(info,original.read(name))
    data=output.getvalue();payload['artifacts']['android'].update(size=len(data),sha256=hashlib.sha256(data).hexdigest());envelope.write_bytes(signed(payload,private));archive.write_bytes(data)
    jvm[0]('unpack',trust,FEED,vectors['now'],envelope,archive,'reject')


def test_https_real_trust_same_origin_redirect_size_and_no_credentials(jvm,tmp_path):
    if not OPENSSL:pytest.skip('OpenSSL TLS fixture unavailable')
    run,java,_=jvm;key=tmp_path/'tls-key.pem';cert=tmp_path/'tls-cert.pem';store=tmp_path/'tls-trust.p12'
    subprocess.run([OPENSSL,'req','-x509','-newkey','rsa:3072','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost'],check=True,capture_output=True)
    keytool=str(Path(java).with_name('keytool.exe' if java.endswith('.exe') else 'keytool'))
    subprocess.run([keytool,'-importcert','-noprompt','-alias','fixture','-file',str(cert),'-keystore',str(store),'-storetype','PKCS12','-storepass','fixturepass'],check=True,capture_output=True)
    requests=[]
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            requests.append(dict(self.headers)|{'__path':self.path})
            if self.path=='/chunked':
                self.send_response(200);self.send_header('Transfer-Encoding','chunked');self.end_headers()
                for chunk in [b'fixture',b' signed transport']:self.wfile.write(f'{len(chunk):x}\r\n'.encode()+chunk+b'\r\n')
                self.wfile.write(b'0\r\n\r\n');return
            if self.path=='/badchunk':self.send_response(200);self.send_header('Transfer-Encoding','chunked');self.end_headers();self.wfile.write(b'ffffffff\r\n');return
            if self.path=='/headers':self.send_response(200);self.send_header('X-Large','x'*40000);self.end_headers();return
            if self.path=='/duplicate':self.send_response(200);self.send_header('Content-Length','2');self.send_header('Content-Length','3');self.end_headers();return
            if self.path=='/truncated':self.send_response(200);self.send_header('Content-Length','100');self.end_headers();self.wfile.write(b'x');return
            if self.path=='/auth':self.send_response(401);self.send_header('WWW-Authenticate','Basic realm=fixture');self.end_headers();return
            if self.path=='/redirect':self.send_response(302);self.send_header('Location','/bundle');self.end_headers();return
            if self.path=='/foreign':self.send_response(302);self.send_header('Location','https://elsewhere.invalid/bundle');self.end_headers();return
            if self.path=='/http':self.send_response(302);self.send_header('Location','http://localhost/bundle');self.end_headers();return
            data=b'fixture signed transport';self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,key);server.socket=context.wrap_socket(server.socket,server_side=True);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url='https://localhost:'+str(server.server_port)
    trusted=('-Djavax.net.ssl.trustStore='+str(store),'-Djavax.net.ssl.trustStorePassword=fixturepass')
    try:
        run('download',url+'/bundle',1024,success=False)
        assert run('download',url+'/redirect',1024,extra=trusted).stdout.strip()==hashlib.sha256(b'fixture signed transport').hexdigest()
        assert run('download',url+'/chunked',1024,extra=trusted).stdout.strip()==hashlib.sha256(b'fixture signed transport').hexdigest()
        run('download',url.replace('localhost','127.0.0.1')+'/bundle',1024,extra=trusted,success=False)
        for path in ['/badchunk','/headers','/duplicate','/truncated']:run('download',url+path,1024,extra=trusted,success=False)
        run('download',url+'/auth',1024,extra=trusted,success=False)
        run('download',url+'/foreign',1024,extra=trusted,success=False);run('download',url+'/http',1024,extra=trusted,success=False);run('download',url+'/bundle',2,extra=trusted,success=False)
        assert requests and all(not row.get('Cookie') and not row.get('Authorization') for row in requests)
        assert sum(row['__path']=='/auth' for row in requests)==1
    finally:server.shutdown();server.server_close();thread.join(2)


def test_actual_mobile_reload_preserves_tuple_drafts_modes_and_session_credentials():
    """Actual WebSocket directories and actual app.js across a same-WebView navigation."""
    import asyncio
    from unittest.mock import patch
    from urllib.parse import quote
    import os
    from supermonoagent.daemon import Daemon
    from supermonoagent import web
    from websockets.asyncio.server import serve
    from test_client_integration import ControlledPty
    playwright=pytest.importorskip('playwright.async_api')
    edge=Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
    if not edge.exists():pytest.skip('Hidden Chromium needed for actual packaged-page navigation')
    async def check():
        import tempfile
        with tempfile.TemporaryDirectory() as temporary,patch('supermonoagent.daemon.STATE',Path(temporary)),patch.dict(os.environ,{'SUPERMONO_STATE_DIR':temporary}):
            daemon=Daemon('reload-fixture-only');daemon.hub.persist=False;daemon.hub.pty_factory=lambda *_:ControlledPty();daemon.hub.attach_loop(asyncio.get_running_loop())
            window=daemon.hub.open_window(owner=daemon.token);session=daemon.hub.open_cli(window.id,'cmd')
            async def http(connection,request):
                path=request.path.split('?',1)[0]
                if path.startswith('/mobile/'):
                    name=path[len('/mobile/'):];asset=ROOT/'src/supermonoagent/static'/name[7:] if name.startswith('vendor/') else ROOT/'android/assets/mobile'/name
                    if asset.is_file():return web.text_response(connection,200,asset.read_text('utf-8'),('text/html' if asset.suffix=='.html' else 'text/css' if asset.suffix=='.css' else 'text/javascript')+'; charset=utf-8')
                return await daemon.serve_http(connection,request)
            try:
                async with serve(daemon.handle_socket,'127.0.0.1',0,process_request=http) as server:
                    origin='http://127.0.0.1:'+str(server.sockets[0].getsockname()[1]);config={'endpoint':origin,'token':daemon.token,'renderer':'dom'}
                    async with playwright.async_playwright() as runtime:
                        browser=await runtime.chromium.launch(executable_path=str(edge),headless=True)
                        try:
                            page=await browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
                            await page.add_init_script('if(!localStorage.getItem("sma.mobile.hosts"))localStorage.setItem("sma.mobile.hosts",'+json.dumps(json.dumps([{'id':'host:other','name':'Other','endpoint':origin,'code':''}]))+');')
                            document=origin+'/mobile/index.html#'+quote(json.dumps(config));await page.goto(document);await page.wait_for_function('MobileTerminal.ready')
                            await page.locator('#mode').click();await page.locator('#draft').fill('home unsent draft')
                            await page.locator('#composer-close').click()
                            await page.locator('#sessions').click();await page.locator('#host-list .host-row').nth(1).locator('button').nth(1).click();await page.locator('#host-token').fill(daemon.token);await page.locator('#host-form button[type=submit]').click();await page.wait_for_function('MobileTerminal.ready && MobileTerminal.hosts.every(h=>h.connected)')
                            await page.locator('#mode').click();await page.locator('#draft').fill('other unsent draft')
                            state=await page.evaluate('MobileTerminal.captureUIState()');assert len(state['drafts'])==2 and len(state['modes'])==2
                            assert daemon.token not in json.dumps(state) and 'endpoint' not in state
                            await page.locator('#draft').dispatch_event('compositionstart');assert await page.evaluate('MobileTerminal.prepareUIReload()') is None
                            await page.locator('#draft').dispatch_event('compositionend');prepared=await page.evaluate('MobileTerminal.prepareUIReload()');assert prepared==state
                            assert await page.locator('#draft').is_disabled();assert await page.evaluate('MobileTerminal.hosts.every(h=>!h.connected)')
                            await page.evaluate('MobileTerminal.cancelUIReload()');await page.wait_for_function('MobileTerminal.ready');assert not await page.locator('#draft').is_disabled();assert await page.locator('#draft').input_value()=='other unsent draft'
                            prepared=await page.evaluate('MobileTerminal.prepareUIReload()')
                            prepared['uncertainDrafts']=[[json.dumps(['host:other',session.id],separators=(',',':')),'other unsent draft']]
                            assert await page.evaluate('location.hash')==''
                            # Native supplies retained connection configuration, not the cleared old fragment.
                            await page.goto(origin+'/mobile/index.html?ui_revision=fixture-next#'+quote(json.dumps(config)))
                            assert await page.evaluate('location.search')=='?ui_revision=fixture-next'
                            await page.evaluate('state=>MobileTerminal.restoreUIState(state)',prepared)
                            await page.wait_for_function('MobileTerminal.ready && MobileTerminal.hosts.every(h=>h.connected)')
                            assert json.loads(await page.evaluate('MobileTerminal.activeKey'))==['host:other',session.id]
                            restored=await page.evaluate('MobileTerminal.captureUIState()');assert dict(restored['modes'])==dict(prepared['modes']);assert restored['uncertainDrafts']==prepared['uncertainDrafts']
                            assert await page.locator('#draft').input_value()=='other unsent draft';assert await page.locator('#composer').is_hidden()
                            await page.locator('#mode').click();assert not await page.locator('#composer').is_hidden();assert await page.locator('#send').is_disabled();await page.locator('#composer-close').click()
                            await page.evaluate('MobileTerminal.chooseHost("home")');await page.wait_for_function('MobileTerminal.ready');assert await page.locator('#draft').input_value()=='home unsent draft';assert await page.locator('#composer').is_hidden()
                            assert not session.pty.written,'Unsent drafts must never become terminal input during update'
                            await page.evaluate('MobileTerminal.pause()')
                        finally:await browser.close()
            finally:daemon.hub.close_window(window.id)
    asyncio.run(check())


@pytest.mark.parametrize('damage',['missing','unexpected','absolute','expanded-limit','file-hash','archive-hash'])
def test_signed_archive_failures_leave_complete_snapshot(jvm,signing,vectors,tmp_path,damage):
    _,private,trust,payload=signing;envelope,archive=release(tmp_path,private,payload,10)
    catalog=json.loads(base64.b64decode(json.loads(envelope.read_bytes())['payload']))
    if damage=='archive-hash':catalog['artifacts']['android']['sha256']='0'*64
    else:
        original=zipfile.ZipFile(archive);output=io.BytesIO();names=original.namelist()
        with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as result:
            for index,name in enumerate(names):
                if damage=='missing' and index==0:continue
                data=original.read(name)
                if index==0:
                    if damage=='absolute':name='/'+name
                    elif damage=='expanded-limit':data=b'x'*(4*1024*1024+1)
                    elif damage=='file-hash':data=b'tampered bytes'
                result.writestr(name,data)
            if damage=='unexpected':result.writestr('account-config.json','must never enter an asset snapshot')
        data=output.getvalue();archive.write_bytes(data);catalog['artifacts']['android'].update(size=len(data),sha256=hashlib.sha256(data).hexdigest())
    envelope.write_bytes(signed(catalog,private))
    jvm[0]('unpack',trust,FEED,vectors['now'],envelope,archive,'reject')


def test_injected_native_csp_blocks_srcdoc_iframe_execution(jvm,tmp_path):
    import asyncio
    playwright=pytest.importorskip('playwright.async_api');edge=Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
    if not edge.exists():pytest.skip('Hidden Chromium needed for actual CSP enforcement')
    raw=tmp_path/'framed.html';raw.write_text('<html><head></head><body><iframe srcdoc="&lt;script&gt;parent.postMessage(\'escaped\',\'*\')&lt;/script&gt;"></iframe></body></html>','utf-8')
    html=jvm[0]('html',raw,ROOT/'android/assets/update-bridge.js').stdout
    assert "frame-src 'none'" in html and 'native-fixture-ticket' in html
    async def check():
        async with playwright.async_playwright() as runtime:
            browser=await runtime.chromium.launch(executable_path=str(edge),headless=True)
            try:
                page=await browser.new_page();await page.add_init_script('window.frameMessages=[];addEventListener("message",e=>frameMessages.push(e.data));')
                await page.route('http://native-fixture.test/**',lambda route:route.fulfill(status=200,body=html,content_type='text/html'))
                await page.goto('http://native-fixture.test/mobile/index.html?ui_revision=fixture-iframe')
                await page.wait_for_timeout(200)
                assert await page.evaluate('window.frameMessages')==[]
                assert await page.evaluate('typeof NativeUpdates.check')=='function'
                assert await page.evaluate('Object.getOwnPropertyDescriptor(window,"NativeUpdates").configurable') is False
            finally:await browser.close()
    asyncio.run(check())


def test_native_resource_rewriter_accepts_standard_attribute_forms(jvm,tmp_path):
    raw=tmp_path/'layout.html'
    raw.write_text('''<!doctype html><!-- <head> ignored comment --><HTML><HEAD data-note='>'>
<script SRC = 'app.js?v=1&ui_revision=old#startup'></script>
<link HREF = "/mobile/app.css?cache=2#theme" rel="stylesheet">
<script src="http://native-fixture.test/mobile/mobile-core.js?ui_revision=old"></script>
<script src='//native-fixture.test/mobile/vendor/terminal-client.js?v=2'></script>
<script src = '  ./vendor/xterm.js?ui_revision=old&ui_revision=older  '></script>
</HEAD><body><a href="/settings">Settings</a><a href='/reload'>Reload</a></body></HTML>''','utf-8')
    rewritten=jvm[0]('html',raw,ROOT/'android/assets/update-bridge.js').stdout
    for path,fragment in [('app.js','#startup'),('app.css','#theme'),('mobile-core.js',''),('vendor/terminal-client.js',''),('vendor/xterm.js','')]:assert '/mobile/'+path+'?ui_revision=fixture-iframe'+fragment in rewritten
    assert '?ui_revision=old' not in rewritten and '?cache=' not in rewritten
    assert 'href="/settings"' in rewritten and "href='/reload'" in rewritten
    assert rewritten.index('<HEAD')<rewritten.index('Content-Security-Policy')<rewritten.index('<script SRC')


@pytest.mark.parametrize('html',[
    '<!doctype html><body>missing head</body>',
    '<!doctype html><!-- <head> commented out --><header>not a head</header>',
    '<html><head><script src="https://hostile.test/mobile/app.js"></script></head></html>',
    '<html><head><link href="/mobile/unlisted.css"></head></html>',
    '<html><head><script src="/mobile/%61pp.js"></script></head></html>',
])
def test_native_resource_rewriter_rejects_missing_head_foreign_and_unlisted(jvm,tmp_path,html):
    raw=tmp_path/'rejected.html';raw.write_text(html,'utf-8');jvm[0]('html',raw,ROOT/'android/assets/update-bridge.js',success=False)


def test_native_healthy_navigation_reuses_webview_and_separates_crash_recovery():
    source=(PACKAGE/'MainActivity.java').read_text('utf-8')
    load=source[source.index('    private void openTerminal(boolean updating)'):source.index('    private void initializeUpdates()')]
    assert 'if(!updating||terminal==null){destroyTerminal();root.removeAllViews();terminal=new WebView(this);' in load
    assert load.count('new WebView(this)')==1
    assert 'terminal.setWebViewClient(new WebViewClient()' in load and 'terminal.loadUrl(base+"/mobile/index.html?ui_revision="' in load
    assert 'retireFailedRenderer();openTerminal(true)' in source
    assert 'getWebViewRenderProcess()' in source and 'process.terminate()' in source
    assert 'addJavascriptInterface(' not in source
    assert 'ui-bundled-only' in source and 'First save the old developer UI drafts from EVERY window' in source
    assert 'getExternalFilesDir' in source and 'active.txt' not in source,'Native recovery must not edit the USB active pointer'
