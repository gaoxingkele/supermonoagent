"""The real dependency-free Java discovery client uses token-scoped WebSockets."""
import asyncio
import json
from pathlib import Path
import shutil
import ssl
import subprocess

import pytest
from websockets.asyncio.server import serve

from supermonoagent.relay import Relay

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'android/src/dev/supermonoagent/mobile'


@pytest.fixture(scope='module')
def room_client(tmp_path_factory):
    javac = shutil.which('javac') or next((str(p) for p in (ROOT / '.omx/tools/jdk').glob('*/bin/javac.exe')), None)
    if not javac:
        pytest.skip('JDK required for actual Android networking class')
    java = str(Path(javac).with_name('java.exe' if javac.endswith('.exe') else 'java'))
    directory = tmp_path_factory.mktemp('android-room-probe')
    harness = directory / 'Rooms.java'
    harness.write_text('''import dev.supermonoagent.mobile.ConnectionProbe;
import java.nio.charset.StandardCharsets;
public class Rooms {
 public static void main(String[] args) throws Exception {
  if(args.length>2&&!args[2].equals("discover")){
   if(args[2].equals("fallback")){ConnectionProbe.DiscoveryResult rooms=ConnectionProbe.discover(args[0],args[1]);if(rooms.success){System.out.print("rooms-ok");return;}}
   ConnectionProbe.ProbeResult direct=ConnectionProbe.verifyDirect(args[0],args[1]);
   if(direct.success){
    if(!direct.authorizesDirect(args[0],"",args[1])||direct.authorizesDirect(args[0],"unexpected-room",args[1])||direct.authorizesDirect(args[0],"",args[1]+"changed")||direct.authorizesDirect(args[0]+"/changed","",args[1]))throw new AssertionError("Direct grant scope mismatch");
   }else if(direct.authorizesDirect(args[0],"",args[1]))throw new AssertionError("Unauthenticated direct access granted");
   System.out.print(direct.success?"direct-ok":"failed");return;
  }
  ConnectionProbe.DiscoveryResult result=ConnectionProbe.discover(args[0],args[1]);
  StringBuilder output=new StringBuilder(result.success?"ok\\n":"failed\\n");
  for(ConnectionProbe.Room room:result.rooms) output.append(room.code).append("|").append(room.machine).append("|").append(room.project).append("\\n");
  output.append("message:").append(result.message);
  System.out.write(output.toString().getBytes(StandardCharsets.UTF_8));
 }
}''', encoding='utf-8')
    subprocess.run([javac, '-encoding', 'UTF-8', '-d', str(directory), str(PACKAGE/'ConnectionEndpoint.java'),
                    str(PACKAGE/'ConnectionProbe.java'), str(harness)], check=True, capture_output=True)
    cert = ROOT / 'tests/fixtures/tls-loopback-cert.pem'
    store = directory / 'trust.p12'
    keytool = str(Path(java).with_name('keytool.exe' if java.endswith('.exe') else 'keytool'))
    subprocess.run([keytool, '-importcert', '-noprompt', '-alias', 'fixture', '-file', str(cert),
                    '-keystore', str(store), '-storetype', 'PKCS12', '-storepass', 'fixturepass'], check=True, capture_output=True)
    def run(endpoint, token='room-fixture-token', trusted=False, mode='discover', extra=()):
        extra = [*extra,*(['-Djavax.net.ssl.trustStore='+str(store), '-Djavax.net.ssl.trustStorePassword=fixturepass'] if trusted else [])]
        return subprocess.run([java, *extra, '-cp', str(directory), 'Rooms', endpoint, token, mode],
                              capture_output=True, check=True, timeout=12).stdout.decode('utf-8')
    run.java=java;run.directory=directory
    return run


def test_actual_java_discovers_only_matching_terminal_rooms(room_client):
    asyncio.run(check_real_relay(room_client))


async def check_real_relay(run):
    from websockets.asyncio.client import connect
    relay = Relay(development=True)
    async with serve(relay.handler, '127.0.0.1', 0) as server:
        endpoint = 'http://127.0.0.1:'+str(server.sockets[0].getsockname()[1])
        async with connect(endpoint.replace('http:', 'ws:')+'/ws') as first, connect(endpoint.replace('http:', 'ws:')+'/ws') as second, connect(endpoint.replace('http:', 'ws:')+'/ws') as private, connect(endpoint.replace('http:', 'ws:')+'/ws') as events:
            for ws, code, token, service in [(first, 'office', 'room-fixture-token', 'terminal'),
                    (second, 'laptop', 'room-fixture-token', 'terminal'),
                    (private, 'private', 'other-fixture-token', 'terminal'),
                    (events, 'office-events', 'room-fixture-token', 'notifications')]:
                await ws.send(json.dumps({'op':'host','code':code,'tokens':[token], 'machine':code.upper(), 'project':'Project','service':service}))
                assert json.loads(await ws.recv())['op'] == 'ready'
            result = await asyncio.to_thread(run, endpoint)
            assert result.startswith('ok\n')
            assert 'office|OFFICE|Project' in result and 'laptop|LAPTOP|Project' in result
            assert 'private' not in result and 'office-events' not in result
            assert 'room-fixture-token' not in result
            rejected = await asyncio.to_thread(run, endpoint, 'wrong-fixture-token')
            assert rejected.startswith('failed\n') and 'office|' not in rejected


def directory(hosts=None):
    return {'op':'directory','revision':1,'capabilities':{'token_discovery':True},
            'hosts':hosts if hosts is not None else [{'code':'office','online':True,'machine':'Computer','project':'Project','token_index':0}]}


@pytest.mark.parametrize('response', [
    {'op':'directory','capabilities':{},'hosts':[]},
    directory([{'code':'office','online':True,'token_index':1}]),
    directory([{'code':'office','online':True,'token_index':False}]),
    directory([{'code':'office','online':True,'token_index':'0'}]),
    directory([{'code':'office','online':True,'token_index':0}]*2),
    directory([{'code':'x'*129,'online':True,'token_index':0}]),
    directory([{'code':'line\nbreak','online':True,'token_index':0}]),
    directory([{}]*65),
    directory('not-an-array'),
    {**directory(), 'revision': '1'},
    {**directory(), 'revision': -1},
    {**directory(), 'revision': 9007199254740992},
])
def test_java_rejects_invalid_or_unscoped_directory(room_client, response):
    asyncio.run(check_response(room_client, response, expected=False))


async def check_response(run, response, expected=True, fragmented=False):
    requests = []
    async def handler(ws):
        requests.append((ws.request.path, json.loads(await ws.recv())))
        payload = json.dumps(response, ensure_ascii=False)
        if fragmented:
            async def parts():
                yield payload[:17]
                pong = await ws.ping(b'fixture')
                await pong
                yield payload[17:]
            await ws.send(parts())
        else:
            await ws.send(payload)
        await ws.wait_closed()
    async with serve(handler, '127.0.0.1', 0) as server:
        result = await asyncio.to_thread(run, 'http://127.0.0.1:'+str(server.sockets[0].getsockname()[1]))
    assert result.startswith('ok\n' if expected else 'failed\n'), result
    assert requests == [('/ws', {'op':'watch','rooms':[], 'tokens':['room-fixture-token']})]
    return result


def test_java_handles_directory_fragments_ping_and_unicode_labels(room_client):
    result = asyncio.run(check_response(room_client, directory([{'code':'office','online':True,
        'machine':'电脑😀','project':'项目','token_index':0}]), fragmented=True))
    assert 'office|电脑😀|项目' in result


def test_java_directory_tls_trust_and_hostname_are_verified(room_client):
    asyncio.run(check_tls(room_client))


async def check_tls(run):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ROOT/'tests/fixtures/tls-loopback-cert.pem', ROOT/'tests/fixtures/tls-loopback-key.pem')
    requests = []
    async def handler(ws):
        requests.append(ws.request.path)
        await ws.recv()
        await ws.send(json.dumps(directory()))
        await ws.wait_closed()
    async with serve(handler, '127.0.0.1', 0, ssl=context, ssl_handshake_timeout=2) as server:
        endpoint = 'https://localhost:'+str(server.sockets[0].getsockname()[1])
        assert (await asyncio.to_thread(run, endpoint, trusted=True)).startswith('ok\n')
        assert (await asyncio.to_thread(run, endpoint)).startswith('failed\n')
    # The fixture intentionally covers both localhost and 127.0.0.1. A separate
    # loopback address exercises hostname rejection without disabling trust.
    async with serve(handler, '127.0.0.2', 0, ssl=context, ssl_handshake_timeout=2) as server:
        endpoint = 'https://127.0.0.2:'+str(server.sockets[0].getsockname()[1])
        assert (await asyncio.to_thread(run, endpoint, trusted=True)).startswith('failed\n')
    assert requests == ['/ws']


def test_java_directory_timeout_is_bounded(room_client):
    asyncio.run(check_timeout(room_client))


async def check_timeout(run):
    async def handler(ws):
        await ws.recv()
        await ws.wait_closed()
    async with serve(handler, '127.0.0.1', 0) as server:
        async with asyncio.timeout(8):
            result = await asyncio.to_thread(run, 'http://127.0.0.1:'+str(server.sockets[0].getsockname()[1]))
    assert result.startswith('failed\n')


@pytest.mark.parametrize('token,expected',[('room-fixture-token','direct-ok'),('wrong-fixture-token','failed')])
def test_authenticated_https_direct_fallback_and_scope_receipt(room_client,tmp_path,token,expected):
    """Non-loopback DNS identity mapped only inside the fixture JVM, with real verified TLS."""
    openssl=shutil.which('openssl') or next((str(p) for p in [Path('D:/Espressif/tools/idf-git/2.44.0/mingw64/bin/openssl.exe'),Path('D:/KimiData/daimon-bundle/runtime/git/mingw64/bin/openssl.exe')] if p.exists()),None)
    if not openssl:pytest.skip('OpenSSL required for a test-owned TLS identity')
    cert=tmp_path/'direct-cert.pem';key=tmp_path/'direct-key.pem';store=tmp_path/'direct-trust.p12';hosts=tmp_path/'hosts'
    subprocess.run([openssl,'req','-x509','-newkey','rsa:3072','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=direct-fixture.test','-addext','subjectAltName=DNS:direct-fixture.test'],check=True,capture_output=True)
    keytool=str(Path(room_client.java).with_name('keytool.exe' if room_client.java.endswith('.exe') else 'keytool'))
    subprocess.run([keytool,'-importcert','-noprompt','-alias','direct','-file',str(cert),'-keystore',str(store),'-storetype','PKCS12','-storepass','fixturepass'],check=True,capture_output=True)
    hosts.write_text('127.0.0.1 direct-fixture.test\n','ascii')
    extra=('-Djdk.net.hosts.file='+str(hosts),'-Djavax.net.ssl.trustStore='+str(store),'-Djavax.net.ssl.trustStorePassword=fixturepass')
    async def check():
        from supermonoagent.protocol import unpack,pack_json,OP_HELLO,OP_HELLO_OK
        seen=[];context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,key)
        async def handler(ws):
            frame=await ws.recv()
            if isinstance(frame,str):
                assert json.loads(frame)['op']=='watch';seen.append('watch');await ws.close(code=4001);return
            op,payload=unpack(frame);body=json.loads(payload);assert op==OP_HELLO;seen.append('hello')
            if body.get('token')!='room-fixture-token':await ws.close(code=4001);return
            await ws.send(pack_json(OP_HELLO_OK,{'capabilities':{'multiplex':True}}));await ws.wait_closed()
        async with serve(handler,'127.0.0.1',0,ssl=context) as server:
            endpoint='https://direct-fixture.test:'+str(server.sockets[0].getsockname()[1])+'/daemon'
            result=await asyncio.to_thread(room_client,endpoint,token,False,'fallback',extra)
        assert result==expected and seen==['watch','hello']
        assert token not in result
    asyncio.run(check())


def test_relay_directory_auth_failure_never_grants_direct_access(room_client):
    async def check():
        relay=Relay(development=True)
        async with serve(relay.handler,'127.0.0.1',0) as server:
            endpoint='http://127.0.0.1:'+str(server.sockets[0].getsockname()[1])
            assert await asyncio.to_thread(room_client,endpoint,'wrong-fixture-token',False,'fallback')=='failed'
    asyncio.run(check())


def test_native_direct_receipt_and_user_edit_invalidation_contract():
    source=(PACKAGE/'MainActivity.java').read_text('utf-8')
    assert 'ConnectionProbe.verifyDirect(endpoint,accessToken)' in source
    assert 'directResult!=null&&directResult.success' in source
    assert 'verifiedDirect[0].authorizesDirect(' in source
    assert 'probeScopeGeneration[0]++;verifiedDirect[0]=null' in source
    assert 'probeGeneration==probeScopeGeneration[0]' in source
    assert 'tokenEdits.beginLoad();try{token.setText(' in source and 'finally{tokenEdits.endLoad();}' in source
    assert 'tokenEdits.action(rememberToken.isChecked(),value)' in source
    assert 'Action.KEEP)return' in source
