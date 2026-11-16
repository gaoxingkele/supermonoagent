"""Unified Android interaction against delayed synthetic terminal transport only."""
import asyncio
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import quote, urlsplit

import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from android_journal_fixture import Fixture, TOKEN, ROOM, packed

ROOT = Path(__file__).resolve().parents[1]
EDGE = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
AGENTS = [('codex', 'Codex'), ('cursor-cli', 'Cursor CLI'), ('claude-code', 'Claude Code'), ('grok', 'Grok')]


class UnifiedFixture(Fixture):
    def __init__(self):
        super().__init__()
        self.inputs = []
        self.resizes = []
        self.subscribed = {}
        self.jobs = set()
        self.echo_enabled = True
        self.echo_times = []
        self.output = {agent: bytearray() for agent, _ in AGENTS}

    def catalog(self):
        return dict(capabilities=dict(multiplex=True, targeted_input=True, snapshot_sgr=True, snapshot_refresh=True),
                    machine='Synthetic500ms', project='No CLI / no model', selected='window-codex', session='codex',
                    controls={agent: dict(cols=100, rows=28, owner=False, busy=False) for agent, _ in AGENTS},
                    windows=[dict(id='window-' + agent, title=label, focused=agent,
                                  sessions=[dict(id=agent, shell=agent, agent=agent, interaction_mode='native', process_generation='generation-' + agent)]) for agent, label in AGENTS])

    def snapshot(self, agent):
        lines = ''.join(f'{agent} history {n:03} const value = 42; src/demo.py "shared"\r\n' for n in range(90))
        return ('\x1b[H\x1b[2J\x1b[?2004h' + lines + '\x1b[38;2;12;34;56mNATIVE_RGB_WARNING\x1b[0m\r\n'
                '\x1b[38;2;180;180;180mconst gray_code = 42;\x1b[0m\r\n\x1b[2mghost suggestion\x1b[0m\r\n> ').encode()

    async def emit(self, agent, data):
        self.output[agent].extend(data)
        for ws, sessions in list(self.subscribed.items()):
            if agent in sessions:
                try:
                    await ws.send(packed(3, agent.encode() + b'\0' + data))
                except ConnectionClosed:
                    pass

    async def echo(self, ws, agent, data):
        await asyncio.sleep(.5)
        if not self.echo_enabled or data == b'\r':
            return
        body = data.replace(b'\x1b[200~', b'').replace(b'\x1b[201~', b'')
        try:
            self.echo_times.append(time.monotonic())
            await self.emit(agent, b'\r\n> ' + body)
        except ConnectionClosed:
            pass

    async def socket(self, ws):
        try:
            async for frame in ws:
                if isinstance(frame, str):
                    value = json.loads(frame)
                    if value.get('op') == 'watch':
                        await ws.send(json.dumps(dict(op='directory', revision=0, hosts=[], capabilities={})))
                    elif value.get('op') == 'phone' and value.get('token') == TOKEN and value.get('code') == ROOM:
                        await ws.send(json.dumps(dict(op='ready')))
                    else:
                        await ws.close(4001)
                    continue
                op, payload = frame[4], frame[5:]
                if op == 1:
                    assert json.loads(payload)['token'] == TOKEN
                    await ws.send(packed(2, self.catalog()))
                elif op == 16:
                    sessions = set(json.loads(payload).get('sessions', [])); self.subscribed[ws] = sessions
                    for agent in sessions:
                        raw = self.snapshot(agent)
                        await ws.send(packed(8, agent.encode() + b'\0' + raw))
                elif op == 17:
                    agent, body = payload.split(b'\0', 1)
                    agent = agent.decode(); self.inputs.append((time.monotonic(), agent, body))
                    job = asyncio.create_task(self.echo(ws, agent, body)); self.jobs.add(job); job.add_done_callback(self.jobs.discard)
                elif op == 5:
                    self.resizes.append(json.loads(payload))
                elif op == 10:
                    await ws.send(packed(11, payload))
        finally:
            self.subscribed.pop(ws, None)

    async def http(self, connection, request):
        route = urlsplit(request.path).path
        if route.startswith('/mobile/agent-modes/'):
            name = route.removeprefix('/mobile/agent-modes/')
            base = ROOT / 'src/supermonoagent/static/agent-modes'
            asset = base / name
            if asset.resolve().is_relative_to(base.resolve()) and asset.is_file():
                return self.http_response(200, asset.read_bytes(), 'application/json')
        return await super().http(connection, request)


@pytest.mark.parametrize('renderer', ['dom', 'webgl'])
def test_android_unified_local_draft_history_and_identity(renderer):
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Owned headless Edge unavailable')
    asyncio.run(check_unified(playwright, renderer))


async def choose(page, agent):
    await page.locator('#sessions').click()
    await page.locator('#session-list .session[data-session="' + agent + '"]').click()
    await page.wait_for_function('agent=>MobileTerminal.ready&&MobileTerminal.active===agent&&document.getElementById("screen").dataset.actualAgent===agent', arg=agent)


async def check_unified(playwright, renderer):
    fixture = UnifiedFixture()
    async with serve(fixture.socket, '127.0.0.1', 0, process_request=fixture.http) as server:
        fixture.origin = 'http://127.0.0.1:' + str(server.sockets[0].getsockname()[1])
        config = dict(endpoint=fixture.origin, code=ROOM, token=TOKEN, renderer=renderer, uiRevision='unified-synthetic')
        document = fixture.origin + '/mobile/index.html#' + quote(json.dumps(config))
        async with playwright.async_playwright() as runtime:
            browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
            try:
                page = await browser.new_page(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.goto(document)
                await page.wait_for_function('window.MobileTerminal?.ready&&document.getElementById("screen").dataset.actualAgent==="codex"')
                assert await page.evaluate('document.activeElement?.tagName') not in ['INPUT', 'TEXTAREA']
                for agent, _ in AGENTS:
                    await choose(page, agent)
                    options = await page.evaluate('({agent:document.getElementById("screen").dataset.actualAgent,cursor:MobileTerminal.terminal.options.cursorStyle,theme:MobileTerminal.terminal.options.theme.background,font:MobileTerminal.terminal.options.fontSize})')
                    assert options == {'agent': agent, 'cursor': 'block', 'theme': '#11151b', 'font': 14}
                    before = len(fixture.inputs)
                    text = agent + ' 本地草稿 中文😀'
                    started = time.monotonic(); await page.locator('#quick-draft').fill(text); edit_ms = (time.monotonic() - started) * 1000
                    assert await page.locator('#quick-draft').input_value() == text
                    assert len(fixture.inputs) == before and edit_ms < 500
                    await page.locator('#quick-draft').dispatch_event('compositionstart')
                    await page.locator('#quick-draft').dispatch_event('keydown', {'key': 'Enter', 'keyCode': 229, 'isComposing': True})
                    assert len(fixture.inputs) == before
                    await page.locator('#quick-draft').dispatch_event('compositionend')
                    await page.locator('#quick-draft').dispatch_event('keydown', {'key': 'Enter', 'keyCode': 229, 'isComposing': False})
                    await page.wait_for_function('document.getElementById("quick-draft").readOnly')
                    async with asyncio.timeout(3):
                        while len(fixture.inputs) == before:
                            await asyncio.sleep(.01)
                    assert len(fixture.inputs) == before + 1 and fixture.inputs[-1][2] != b'\r'
                    await page.wait_for_function('document.getElementById("quick-draft").value === ""')
                    sent = fixture.inputs[before:]
                    assert len(sent) == 2 and sent[-1][2] == b'\r'
                    assert sent[0][2].replace(b'\x1b[200~', b'').replace(b'\x1b[201~', b'') == text.encode()
                    assert sent[-1][0] - fixture.echo_times[-1] >= .17
                await choose(page, 'cursor-cli')
                await page.locator('#quick-draft').fill('preserved local draft')
                baseline_inputs = len(fixture.inputs)
                await page.locator('#screen').hover(); await page.mouse.wheel(0, -500)
                await page.wait_for_function('MobileTerminal.terminal.buffer.active.viewportY < MobileTerminal.terminal.buffer.active.baseY')
                before = await page.evaluate('(()=>{const b=MobileTerminal.terminal.buffer.active;return {y:b.viewportY,line:b.getLine(b.viewportY).translateToString(true)};})()')
                await fixture.emit('cursor-cli', b'\r\nNEW_FRESH_OUTPUT\r\n')
                await page.wait_for_function('document.getElementById("history-hint").textContent.includes("有新输出")')
                after = await page.evaluate('(()=>{const b=MobileTerminal.terminal.buffer.active;return {y:b.viewportY,line:b.getLine(b.viewportY).translateToString(true)};})()')
                assert after == before
                assert len(fixture.inputs) == baseline_inputs and not fixture.resizes
                await page.set_viewport_size({'width': 844, 'height': 390})
                await page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
                rotated = await page.evaluate('(()=>{const b=MobileTerminal.terminal.buffer.active;return {y:b.viewportY,line:b.getLine(b.viewportY).translateToString(true)};})()')
                assert rotated == before and not await page.locator('#resume').is_hidden()
                await page.set_viewport_size({'width': 390, 'height': 844})
                await page.locator('#bottom').click()
                await page.wait_for_function('document.getElementById("resume").hidden')
                assert await page.locator('#quick-draft').input_value() == 'preserved local draft'
                await page.locator('#raw-input').click()
                assert await page.evaluate('document.activeElement===MobileTerminal.terminal.textarea&&!MobileTerminal.terminal.textarea.readOnly')
                assert len(fixture.inputs) == baseline_inputs
                await page.locator('#raw-input').click()
                assert await page.evaluate('document.activeElement===document.getElementById("quick-draft")&&MobileTerminal.terminal.textarea.readOnly')
                assert await page.locator('#quick-draft').input_value() == 'preserved local draft'
                await choose(page, 'codex')
                await fixture.emit('codex', b'\x1b[?1049h\x1b[H\x1b[2JREAD_ONLY_ALT_HISTORY\r\n')
                await page.wait_for_function('MobileTerminal.terminal.buffer.active.type === "alternate"')
                baseline_inputs = len(fixture.inputs)
                touch = await page.context.new_cdp_session(page); rect = await page.locator('#screen').bounding_box()
                x, y = rect['x'] + rect['width'] / 2, rect['y'] + rect['height'] / 2
                await touch.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [{'x': x, 'y': y}]})
                await touch.send('Input.dispatchTouchEvent', {'type': 'touchMove', 'touchPoints': [{'x': x, 'y': y + 90}]})
                await touch.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
                await page.wait_for_function('!document.getElementById("screen-history").hidden')
                assert len(fixture.inputs) == baseline_inputs
                await page.locator('#bottom').click()
                if await page.locator('#page-up').is_hidden():
                    await page.locator('#tools-toggle').click()
                await page.locator('#page-up').click()
                async with asyncio.timeout(3):
                    while len(fixture.inputs) == baseline_inputs:
                        await asyncio.sleep(.01)
                assert fixture.inputs[-1][2] == b'\x1b[5~'
                await choose(page, 'cursor-cli')
                if not await page.locator('#resume').is_hidden():
                    await page.locator('#bottom').click()
                await page.wait_for_function('document.getElementById("resume").hidden')
                await page.wait_for_function('document.getElementById("notice").hidden')
                artifacts = ROOT / '.omx/artifacts/android-unified' / (renderer + '-' + str(time.time_ns()))
                artifacts.mkdir(parents=True, exist_ok=True); verdicts = []
                for width, height in [(1100, 760), (390, 844), (844, 390), (768, 1200), (1200, 768)]:
                    await page.set_viewport_size({'width': width, 'height': height})
                    await page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
                    evidence = await page.evaluate('''() => ({inside:[...document.querySelectorAll('#quick-composer,#journal-actions')].every(node=>{const r=node.getBoundingClientRect();return r.left>=0&&r.top>=0&&r.right<=innerWidth+1&&r.bottom<=innerHeight+1;}),overflow:document.documentElement.scrollWidth>innerWidth,actualAgent:document.getElementById('screen').dataset.actualAgent,cursor:MobileTerminal.terminal.options.cursorStyle,grid:[MobileTerminal.terminal.cols,MobileTerminal.terminal.rows]})''')
                    assert await page.locator('#resume').is_hidden(), 'Rotation must preserve explicit following mode'
                    image = f'{width}x{height}.png'; await page.screenshot(path=str(artifacts / image))
                    passed = evidence['inside'] and not evidence['overflow'] and evidence['actualAgent'] == 'cursor-cli' and evidence['cursor'] == 'block' and evidence['grid'] == [100, 28]
                    verdicts.append(dict(viewport={'width': width, 'height': height}, screenshot=image, verdict='pass' if passed else 'fail', evidence=evidence))
                    (artifacts / 'visual-verdict.json').write_text(json.dumps(dict(scope='android-unified', renderer=renderer, physical_android=False, echo_delay_ms=500, verdicts=verdicts), indent=2), encoding='utf-8')
                    assert passed, evidence
                assert not fixture.resizes and not errors, errors
                assert await page.locator('#quick-draft').input_value() == 'preserved local draft'
                # A paste that loses its receipt must survive view/reload changes
                # as uncertainty, rather than becoming a fresh second paste.
                fixture.echo_enabled = False
                uncertain_text = 'uncertain original 中文😀'
                await page.locator('#quick-draft').fill(uncertain_text)
                baseline_inputs = len(fixture.inputs)
                await page.locator('#quick-send').click()
                async with asyncio.timeout(3):
                    while len(fixture.inputs) == baseline_inputs:
                        await asyncio.sleep(.01)
                await choose(page, 'grok'); await choose(page, 'cursor-cli')
                assert await page.locator('#quick-draft').input_value() == uncertain_text
                assert await page.locator('#quick-send').is_disabled()
                assert len(fixture.inputs) == baseline_inputs + 1
                await page.wait_for_function('MobileTerminal.captureUIState() !== null')
                saved = await page.evaluate('MobileTerminal.captureUIState()')
                assert saved['nativeUncertaintyTargets'] and TOKEN not in json.dumps(saved)
                await page.goto(document.replace('/mobile/index.html#', '/mobile/index.html?reload=native-uncertain#'))
                await page.wait_for_function('window.MobileTerminal?.ready')
                await page.evaluate('state=>MobileTerminal.restoreUIState(state)', saved)
                await page.wait_for_function('MobileTerminal.active==="cursor-cli" && MobileTerminal.ready && document.getElementById("quick-send").disabled')
                assert await page.locator('#quick-draft').input_value() == uncertain_text
                assert len(fixture.inputs) == baseline_inputs + 1
                fixture.echo_enabled = True
                await choose(page, 'grok')
                await page.locator('#quick-draft').fill('pending Grok input')
                baseline_inputs = len(fixture.inputs)
                await page.locator('#quick-send').click()
                await page.locator('[data-key="escape"]').click()
                await asyncio.sleep(.8)
                assert len(fixture.inputs) == baseline_inputs + 2
                assert fixture.inputs[-1][2] == b'\x1b'
                assert all(body != b'\r' for _, _, body in fixture.inputs[baseline_inputs:])
                assert await page.locator('#quick-send').is_disabled()
                await choose(page, 'claude-code')
                baseline_inputs = len(fixture.inputs)
                await page.locator('#model').click()
                await page.locator('[data-key="escape"]').click()
                await asyncio.sleep(.8)
                assert len(fixture.inputs) == baseline_inputs + 2
                assert all(body != b'\r' for _, _, body in fixture.inputs[baseline_inputs:])
                await choose(page, 'codex'); await choose(page, 'claude-code')
                assert await page.locator('#model').is_disabled()
                assert not errors, errors
                await page.evaluate('MobileTerminal.pause()')
            finally:
                await browser.close()
                for job in list(fixture.jobs):
                    job.cancel()
                await asyncio.gather(*fixture.jobs, return_exceptions=True)
