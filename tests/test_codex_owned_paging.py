"""Real, credential-free Codex resume paging; only a disposable local transcript."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
import uuid

import pytest

ROOT=Path(__file__).resolve().parents[1]
EDGE=Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')


def test_real_codex_owned_transcript_page_keys_without_model_request():
    if sys.platform!='win32' or not EDGE.exists() or not shutil.which('codex'):
        pytest.skip('Real Codex and Windows Edge required')
    playwright=pytest.importorskip('playwright.async_api')
    asyncio.run(_exercise(playwright))


def seed_rollout(home,work):
    identity=str(uuid.uuid4());now=datetime.now(timezone.utc)
    timestamp=now.isoformat(timespec='seconds').replace('+00:00','Z')
    folder=home/'sessions'/now.strftime('%Y/%m/%d');folder.mkdir(parents=True)
    rollout=folder/('rollout-'+now.strftime('%Y-%m-%dT%H-%M-%S')+'-'+identity+'.jsonl')
    rows=[{'timestamp':timestamp,'type':'session_meta','payload':{
        'id':identity,'session_id':identity,'timestamp':timestamp,'cwd':str(work),
        'originator':'cli','cli_version':'0.164.0','source':'cli',
        'model_provider':'paging-fixture','history_mode':'legacy'}}]
    for index in range(48):
        user=f'OWNED_PAGING_USER_{index:03d}: deterministic local history only.'
        answer=f'OWNED_PAGING_REPLY_{index:03d}: fixture response, no model was called.'
        for role,text,content in [('user',user,'input_text'),('assistant',answer,'output_text')]:
            rows.append({'timestamp':timestamp,'type':'event_msg','payload':{
                'type':'user_message' if role=='user' else 'agent_message','message':text,
                **({'images':[],'local_images':[],'text_elements':[]} if role=='user' else {'phase':'final_answer'})}})
            rows.append({'timestamp':timestamp,'type':'response_item','payload':{
                'type':'message','role':role,'content':[{'type':content,'text':text}],
                **({'phase':'final_answer'} if role=='assistant' else {})}})
    rollout.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf8')
    return identity


async def _exercise(playwright):
    from supermonoagent.pty import WinPty,PtyClosed
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_GET(self):
            requests.append(('GET',self.path.split('?')[0]))
            data=json.dumps({'object':'list','data':[{'id':'gpt-5.4','object':'model','owned_by':'fixture'}]}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        def do_POST(self):
            requests.append(('POST',self.path.split('?')[0]));self.send_error(503)
        def do_CONNECT(self):
            requests.append(('CONNECT','external-blocked'));self.send_error(503)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server_thread=threading.Thread(target=server.serve_forever,daemon=True);server_thread.start()
    with tempfile.TemporaryDirectory(prefix='codex-owned-paging-') as temporary:
        temporary=Path(temporary);home=temporary/'codex-home';home.mkdir();work=temporary/'work';work.mkdir()
        identity=seed_rollout(home,work);provider=f'http://127.0.0.1:{server.server_port}'
        (home/'config.toml').write_text('\n'.join([
            'model="gpt-5.4"','model_provider="paging-fixture"',
            'check_for_update_on_startup=false','notify=[]',
            '[tui]','alternate_screen="always"','notifications=false',
            '[features]','hooks=false',
            '[model_providers.paging-fixture]','name="Owned paging fixture"',
            'base_url='+json.dumps(provider+'/v1'),'wire_api="responses"','requires_openai_auth=false',
            '[projects.'+json.dumps(str(work))+']','trust_level="trusted"',
        ])+'\n',encoding='utf8')
        allowed={'PATH','PATHEXT','SYSTEMROOT','WINDIR','TEMP','TMP','COMSPEC','SYSTEMDRIVE','PROGRAMFILES','PROGRAMFILES(X86)'}
        env={key:value for key,value in os.environ.items() if key.upper() in allowed}
        for key,subdir in [('USERPROFILE','profile'),('HOME','profile'),('APPDATA','roaming'),('LOCALAPPDATA','local')]:
            (temporary/subdir).mkdir(exist_ok=True);env[key]=str(temporary/subdir)
        env.update(CODEX_HOME=str(home),HTTP_PROXY=provider,HTTPS_PROXY=provider,ALL_PROXY=provider,NO_PROXY='127.0.0.1,localhost')
        chunks=queue.Queue();writes=[];closed=threading.Event()
        command=[shutil.which('codex'),'--no-daemon','--disable','hooks','-C',str(work),'resume',identity]
        pty=None;reader=None
        try:
            with patch.dict(os.environ,env,clear=True):pty=WinPty(command,str(work),100,28)
            def send(data):
                writes.append(bytes(data));pty.write(data)
            def read():
                try:
                    while not closed.is_set():
                        data=pty.read();chunks.put(data)
                except (PtyClosed,OSError,EOFError):pass
            reader=threading.Thread(target=read,daemon=True);reader.start()
            async with playwright.async_playwright() as runtime:
                browser=await runtime.chromium.launch(executable_path=str(EDGE),headless=True)
                try:
                    page=await browser.new_page(viewport={'width':1200,'height':800})
                    await page.set_content('<div id="terminal" style="width:1100px;height:700px"></div>')
                    await page.add_script_tag(path=str(ROOT/'src/supermonoagent/static/xterm.js'))
                    await page.evaluate('''() => {window.term=new Terminal({cols:100,rows:28,allowProposedApi:true});term.open(document.querySelector('#terminal'));term.onData(data=>(window.responses??=[]).push(data));}''')
                    async def pump(seconds=.15):
                        deadline=time.monotonic()+seconds
                        while time.monotonic()<deadline:
                            batch=[]
                            while not chunks.empty():batch.append(chunks.get_nowait())
                            if batch:await page.evaluate('bytes=>new Promise(done=>term.write(Uint8Array.from(bytes),done))',list(b''.join(batch)))
                            replies=await page.evaluate('()=>{const r=window.responses||[];window.responses=[];return r;}')
                            for reply in replies:send(reply.encode())
                            if any(method=='POST' for method,_ in requests):raise AssertionError({'reason':'Codex attempted model request','requests':requests})
                            await asyncio.sleep(.025)
                    async def screen():
                        return await page.evaluate("()=>{const b=term.buffer.active;return {type:b.type,lines:Array.from({length:term.rows},(_,i)=>b.getLine(b.viewportY+i)?.translateToString(true)||'')};}")
                    deadline=time.monotonic()+18
                    while time.monotonic()<deadline:
                        await pump(.1);view=await screen()
                        if any('OWNED_PAGING_REPLY_047' in line for line in view['lines']):break
                        if not pty.alive():break
                    else:raise AssertionError({'reason':'Seeded owned transcript did not load','view':await screen(),'requests':requests})
                    initial=await screen()
                    assert initial['type']=='alternate',initial
                    assert any('OWNED_PAGING_REPLY_047' in line for line in initial['lines']),initial
                    # All four are navigation only; never submit fixture text or press Enter.
                    states={'initial':initial}
                    for name,key in [('page_up',b'\x1b[5~'),('top',b'\x1b[1;5H'),('page_down',b'\x1b[6~'),('bottom',b'\x1b[1;5F')]:
                        send(key);await pump(.6);states[name]=await screen()
                    assert states['page_up']['lines']!=initial['lines'],states
                    assert any('OWNED_PAGING_USER_000' in line for line in states['top']['lines']),states
                    assert states['page_down']['lines']!=states['top']['lines'],states
                    assert any('OWNED_PAGING_REPLY_047' in line for line in states['bottom']['lines']),states
                    assert not any(method=='POST' for method,_ in requests),requests
                    for key in (b'\x1b[5~',b'\x1b[1;5H',b'\x1b[6~',b'\x1b[1;5F'):assert writes.count(key)==1
                    assert not any(b'\r' in data or b'\n' in data for data in writes)
                    artifact=ROOT/'.omx/artifacts/codex-owned-paging';artifact.mkdir(parents=True,exist_ok=True)
                    (artifact/'real-codex.json').write_text(json.dumps({'passed':True,'version':subprocess.check_output([shutil.which('codex'),'--version'],text=True).strip(),'isolated_home':True,'user_auth_copied':False,'requests':requests,'navigation_keys':[key.hex() for key in [b'\x1b[5~',b'\x1b[1;5H',b'\x1b[6~',b'\x1b[1;5F']],'states':states},indent=2),encoding='utf8')
                finally:await browser.close()
        finally:
            closed.set()
            if pty:
                pty.close()
                try:pty._proc.fileobj.shutdown(socket.SHUT_RDWR)
                except OSError:pass
                pty._proc.fileobj.close();pty._proc._server.close()
            if reader:await asyncio.to_thread(reader.join,3)
            server.shutdown();server.server_close();server_thread.join(3)
