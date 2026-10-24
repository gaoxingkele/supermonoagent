"""Real paused-goal rendering versus replay, using disposable local Codex state."""
import asyncio
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

from test_codex_owned_paging import seed_rollout

ROOT = Path(__file__).resolve().parents[1]
EDGE = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')


class UnsupportedGoalApi(Exception):
    pass


def environment(folder, provider, mode):
    home, work = folder / 'codex-home', folder / 'work'
    home.mkdir(); work.mkdir()
    (home / 'config.toml').write_text('\n'.join([
        'model="gpt-5.4"', 'model_provider="paging-fixture"',
        'cli_auth_credentials_store="file"', 'check_for_update_on_startup=false', 'notify=[]',
        '[tui]', 'alternate_screen=' + json.dumps(mode), 'notifications=false',
        '[features]', 'hooks=false', 'apps=false',
        '[model_providers.paging-fixture]', 'name="Owned goal popup fixture"',
        'base_url=' + json.dumps(provider + '/v1'), 'wire_api="responses"', 'requires_openai_auth=false',
        '[projects.' + json.dumps(str(work)) + ']', 'trust_level="trusted"']) + '\n', encoding='utf-8')
    allowed = {'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'COMSPEC',
               'SYSTEMDRIVE', 'PROGRAMFILES', 'PROGRAMFILES(X86)'}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    for key, name in [('USERPROFILE', 'profile'), ('HOME', 'profile'),
                      ('APPDATA', 'roaming'), ('LOCALAPPDATA', 'local')]:
        (folder / name).mkdir(exist_ok=True); env[key] = str(folder / name)
    env.update(CODEX_HOME=str(home), HTTP_PROXY=provider, HTTPS_PROXY=provider,
               ALL_PROXY=provider, NO_PROXY='127.0.0.1,localhost')
    return home, work, env


def seed_paused_goal(cli, home, work, env):
    identity = seed_rollout(home, work)
    process = subprocess.Popen([cli, '--no-daemon', '--disable', 'hooks', 'app-server', '--listen', 'stdio://'],
        env=env, cwd=work, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW)
    output = queue.Queue()
    def read():
        for line in process.stdout:
            output.put(json.loads(line))
        output.put(None)
    reader = threading.Thread(target=read, daemon=True); reader.start()
    def rpc(method, params, request_id):
        process.stdin.write((json.dumps({'id': request_id, 'method': method, 'params': params}) + '\n').encode())
        process.stdin.flush()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            reply = output.get(timeout=max(.1, deadline - time.monotonic()))
            assert reply is not None, 'Owned app-server exited before replying'
            if reply.get('id') != request_id:
                continue
            error = reply.get('error')
            if method == 'thread/goal/set' and error and error.get('code') == -32601:
                raise UnsupportedGoalApi('Installed Codex app-server does not implement thread/goal/set (-32601)')
            assert not error, reply
            return reply['result']
        raise TimeoutError('Owned app-server request: ' + method)
    try:
        rpc('initialize', {'clientInfo': {'name': 'goal-popup-fixture', 'version': '1'},
                          'capabilities': {'experimentalApi': True}}, 1)
        process.stdin.write(b'{"method":"initialized"}\n'); process.stdin.flush()
        rpc('thread/resume', {'threadId': identity, 'cwd': str(work)}, 2)
        result = rpc('thread/goal/set', {'threadId': identity, 'objective': 'OWNED_GOAL_POPUP_FIXTURE',
                                       'status': 'paused', 'origin': 'user'}, 3)
        assert result['goal']['status'] == 'paused'
        return identity
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate(); process.wait(timeout=5)
        reader.join(timeout=2)
        assert process.returncode == 0 and not reader.is_alive()
        process.stdout.close(); process.stderr.close()


@pytest.mark.parametrize('mode', ['auto', 'never'])
def test_real_paused_goal_same_grid_replay_matches_native(tmp_path, mode):
    cli = shutil.which('codex')
    if sys.platform != 'win32' or not EDGE.exists() or not cli:
        pytest.skip('Real Windows Codex CLI and local Edge required')
    playwright = pytest.importorskip('playwright.async_api')
    try:
        asyncio.run(exercise(playwright, cli, tmp_path, mode))
    except UnsupportedGoalApi as error:
        pytest.skip(str(error))


async def exercise(playwright, cli, folder, mode):
    from supermonoagent.pty import WinPty, PtyClosed
    from supermonoagent.pump import OutputPump
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            requests.append(('GET', self.path))
            data = b'{"object":"list","data":[{"id":"gpt-5.4","object":"model","owned_by":"fixture"}]}'
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
        def do_POST(self):
            requests.append(('POST', self.path)); self.send_error(503)
        def do_CONNECT(self):
            requests.append(('CONNECT', 'external-blocked')); self.send_error(503)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True); server_thread.start()
    stop, chunks = threading.Event(), queue.Queue()
    pty = reader = None
    try:
        home, work, env = environment(folder, 'http://127.0.0.1:' + str(server.server_port), mode)
        identity = await asyncio.to_thread(seed_paused_goal, cli, home, work, env)
        with patch.dict(os.environ, env, clear=True):
            pty = WinPty([cli, '--no-daemon', '--disable', 'hooks', '-C', str(work), 'resume', identity], str(work), 100, 40)
        def read():
            try:
                while not stop.is_set():
                    chunks.put(pty.read())
            except (PtyClosed, OSError, EOFError): pass
        reader = threading.Thread(target=read, daemon=True); reader.start()
        pump = OutputPump()
        async with playwright.async_playwright() as runtime:
            browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
            try:
                page = await browser.new_page(viewport={'width': 1200, 'height': 900})
                await page.set_content('<div id="terminal" style="width:1100px;height:800px"></div>')
                await page.add_style_tag(path=str(ROOT / 'src/supermonoagent/static/xterm.css'))
                await page.add_script_tag(path=str(ROOT / 'src/supermonoagent/static/xterm.js'))
                await page.evaluate("()=>{window.term=new Terminal({cols:100,rows:40});term.open(document.querySelector('#terminal'));window.replies=[];term.onData(value=>replies.push(value));}")
                async def consume(duration):
                    deadline = time.monotonic() + duration
                    while time.monotonic() < deadline:
                        parts = []
                        while not chunks.empty(): parts.append(chunks.get_nowait())
                        if parts:
                            raw = b''.join(parts); pump.remember(raw)
                            await page.evaluate('bytes=>new Promise(done=>term.write(Uint8Array.from(bytes),done))', list(raw))
                        for reply in await page.evaluate('()=>{const r=replies;replies=[];return r;}'):
                            pty.write(reply.encode())  # Terminal status replies only; no keys or submitted input.
                        assert not any(method == 'POST' for method, _ in requests), requests
                        await asyncio.sleep(.025)
                async def view():
                    return await page.evaluate("()=>{const b=term.buffer.active;return {type:b.type,baseY:b.baseY,lines:Array.from({length:term.rows},(_,i)=>b.getLine(b.viewportY+i)?.translateToString(true)||'')};}")
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    await consume(.1)
                    if any('Resume paused goal?' in line for line in (await view())['lines']): break
                await consume(.4)
                fresh = await view()
                assert any('Resume paused goal?' in line for line in fresh['lines']), fresh
                assert any('Leave paused' in line for line in fresh['lines']), fresh
                await page.evaluate('bytes=>new Promise(done=>{term.reset();term.write(Uint8Array.from(bytes),done);})', list(pump.snapshot()))
                replay = await view()
                assert replay == fresh
                def position(state):
                    return [(y + 1, line.index('Resume paused goal?') + 1) for y, line in enumerate(state['lines']) if 'Resume paused goal?' in line]
                assert position(fresh) == position(replay)
                output = ROOT / '.omx/artifacts/goal-popup' / ('regression-' + mode + '.json')
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps({'mode': mode, 'fresh': fresh, 'replay': replay,
                    'heading_positions': position(fresh), 'same_grid_equal': True,
                    'model_post_count': 0, 'user_credentials_read': False}, indent=2), encoding='utf-8')
            finally:
                await browser.close()
    finally:
        stop.set()
        if pty is not None: pty.close()
        if reader is not None: reader.join(timeout=2)
        server.shutdown(); server.server_close(); server_thread.join(timeout=2)
        assert pty is None or not pty.alive()
        assert reader is None or not reader.is_alive()
        assert not server_thread.is_alive()
        assert not any(method == 'POST' for method, _ in requests), requests
