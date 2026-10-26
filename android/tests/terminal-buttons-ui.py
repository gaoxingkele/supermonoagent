"""Six-agent Android button matrix against isolated VT fixtures, never live hosts.

Control ownership, native Update, file pickers and debug grids retain their own
dedicated suites. This suite exercises terminal navigation and input buttons.
"""
import asyncio
import importlib.util
import json
import mimetypes
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import async_playwright
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / '.omx/artifacts/android-button-compat'
AGENTS = ['codex', 'claude-code', 'grok', 'kimi', 'pi-dev', 'cursor-cli']
AGENT_LABELS = dict(zip(AGENTS, ['Codex', 'Claude Code', 'Grok', 'Kimi', 'pi.dev', 'Cursor CLI']))
spec = importlib.util.spec_from_file_location('grid_fixture', Path(__file__).with_name('debug-grid-ui.py'))
grid = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grid)


def screen(stage):
    return '\x1b[2J\x1b[H' + ''.join(
        f'\x1b[{row + 1};1Hstage {stage} · 历史画面 {row:02d} · 数据 12345'
        for row in range(43))


async def eventually(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.02)


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    clients, written, operations, refreshes, errors, results = [], [], [], [], [], []
    touch_selection = {}

    def state(client):
        return {'capabilities': {'multiplex': True, 'snapshot_sgr': True, 'snapshot_refresh': True},
                'machine': 'isolated-agent-buttons',
                'windows': [{'id': agent, 'title': agent,
                             'sessions': [{'id': agent + '-' + kind, 'agent': agent} for kind in ['normal', 'alternate']]}
                            for agent in AGENTS],
                'controls': {sid: {'cols': 113, 'rows': 43, 'owner': False} for sid in client['sub']}}

    def snapshot(sid):
        if sid.endswith('-alternate'):
            return '\x1b[?1049h' + screen('A')
        return '\x1b[?1049l\x1b[2J\x1b[H' + ''.join(f'{row:03d} · 中文历史记录 · {sid}\r\n' for row in range(160))

    async def socket(ws):
        client = {'ws': ws, 'sub': set()}
        clients.append(client)
        try:
            async for message in ws:
                if isinstance(message, str):
                    value = json.loads(message)
                    if value['op'] == 'phone':
                        await ws.send(json.dumps({'op': 'ready'}))
                    elif value['op'] == 'watch':
                        await ws.send(json.dumps({'op': 'directory', 'revision': 1, 'hosts': []}))
                    continue
                op, payload = message[4], message[5:]
                operations.append(op)
                if op == 1:
                    await ws.send(grid.frame(2, state(client)))
                elif op == 16:
                    value = json.loads(payload)
                    wanted = set(value['sessions'])
                    if value.get('refresh'):
                        refreshes.append(next(iter(wanted)))
                    for sid in wanted - client['sub'] if not value.get('refresh') else wanted:
                        await ws.send(grid.feed(8, sid, snapshot(sid)))
                    client['sub'] = wanted
                    await ws.send(grid.frame(7, state(client)))
                elif op == 17:
                    sid, text = payload.split(b'\0', 1)
                    text = text.decode()
                    written.append((sid.decode(), text))
                    clean = text.replace('\x1b[200~', '').replace('\x1b[201~', '')
                    if clean == '\r':
                        await ws.send(grid.feed(3, sid.decode(), '\r\n'))
                    elif clean and not any(ord(char) < 32 for char in clean):
                        await ws.send(grid.feed(3, sid.decode(), clean))
                elif op == 10:
                    await ws.send(grid.frame(11, payload))
        finally:
            clients.remove(client)

    async def assets(connection, request):
        path = request.path.split('?')[0]
        if not path.startswith('/mobile/'):
            return None
        name = path[len('/mobile/'):]
        base = ROOT / ('src/supermonoagent/static' if name.startswith('vendor/') else 'android/assets/mobile')
        file = (base / name.removeprefix('vendor/')).resolve()
        if not file.is_relative_to(base) or not file.is_file():
            return Response(404, 'Not Found', Headers(), b'')
        return Response(200, 'OK', Headers({'Content-Type': mimetypes.guess_type(name)[0] or 'application/octet-stream'}), file.read_bytes())

    async def emit(sid, value):
        owners = [client for client in clients if sid in client['sub']]
        assert len(owners) == 1, (sid, owners)
        await owners[0]['ws'].send(grid.feed(3, sid, value))

    async with serve(socket, '127.0.0.1', 0, process_request=assets) as server:
        port = server.sockets[0].getsockname()[1]
        async with async_playwright() as runtime:
            browser = await runtime.chromium.launch(channel='msedge')
            try:
                page = await browser.new_page(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
                page.on('pageerror', lambda error: errors.append(str(error)))
                config = {'endpoint': f'http://127.0.0.1:{port}', 'code': 'fixture', 'token': 'fixture-only', 'renderer': 'dom'}
                await page.goto(f'http://127.0.0.1:{port}/mobile/index.html#' + quote(json.dumps(config)))
                await grid.wait(page, 'MobileTerminal.ready')

                async def choose(sid):
                    if await page.locator('#composer').evaluate('e=>e.open'):
                        await page.locator('#composer-close').click()
                    await page.locator('#sessions').click()
                    agent = sid.rsplit('-', 1)[0]
                    assert await page.locator(f'#session-list .session[data-session="{sid}"]').get_attribute('data-agent') == AGENT_LABELS[agent]
                    await page.locator(f'#session-list .session[data-session="{sid}"]').click()
                    await grid.wait(page, f'MobileTerminal.ready&&MobileTerminal.active==={json.dumps(sid)}')

                async def click(button):
                    await page.locator('#' + button).click()
                    await asyncio.sleep(.05)

                async def position():
                    return await page.evaluate("()=>{const b=MobileTerminal.terminal.buffer.active,s=document.getElementById('screen');return {y:b.viewportY,base:b.baseY,top:s.scrollTop,max:Math.max(0,s.scrollHeight-s.clientHeight)}}")

                async def check_touch_selection():
                    before_input = len(written)
                    touch = await page.context.new_cdp_session(page)

                    async def begin():
                        box = await page.locator('#screen').bounding_box()
                        point = {'x': box['x'] + min(120, box['width'] / 2), 'y': box['y'] + 100}
                        await touch.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [point]})
                        return point

                    async def end():
                        await touch.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})

                    async def long_press():
                        await begin()
                        await asyncio.sleep(.7)
                        await end()
                        await grid.wait(page, 'document.getElementById("copy-dialog").open')

                    await page.evaluate('document.activeElement?.blur()')
                    await begin()
                    await end()
                    await asyncio.sleep(.1)
                    assert await page.evaluate('!document.getElementById("composer").open&&document.activeElement!==MobileTerminal.terminal.textarea&&document.activeElement!==document.getElementById("draft")')
                    assert await page.evaluate('MobileTerminal.terminal.textarea.readOnly&&MobileTerminal.terminal.textarea.inputMode==="none"')
                    await long_press()
                    assert await page.locator('#copy-text').evaluate('e=>e.readOnly&&e.inputMode==="none"')
                    frozen = await page.locator('#copy-text').input_value()
                    await emit('codex-normal', 'new output while selecting text')
                    await asyncio.sleep(.1)
                    assert await page.locator('#copy-text').input_value() == frozen
                    old_key = await page.evaluate('MobileTerminal.clipboardTarget()')
                    assert old_key
                    original = '原草稿 '
                    clipboard = "粘贴中文 '\"\\\n<script>alert('x')</script>"
                    await page.locator('#draft').evaluate('(e,value)=>{e.value=value;e.setSelectionRange(value.length,value.length);e.dispatchEvent(new Event("input"));}', original)
                    assert await page.evaluate('([key,text])=>MobileTerminal.pasteClipboardText(key,text)', [old_key, clipboard]) is True
                    assert await page.locator('#draft').input_value() == original + clipboard
                    assert await page.locator('#composer').evaluate('e=>e.open')
                    assert not await page.locator('#copy-dialog').evaluate('e=>e.open')
                    assert await page.evaluate('document.activeElement!==document.getElementById("draft")&&document.activeElement!==MobileTerminal.terminal.textarea')
                    assert await page.locator('#composer script').count() == 0
                    assert len(written) == before_input, 'Paste must only edit the draft'
                    await click('composer-close')
                    await choose('codex-alternate')
                    await long_press()
                    assert await page.evaluate('MobileTerminal.clipboardTarget()') != old_key
                    assert await page.evaluate('([key,text])=>MobileTerminal.pasteClipboardText(key,text)', [old_key, clipboard]) is False
                    assert await page.locator('#draft').input_value() == ''
                    await click('copy-close')
                    await choose('codex-normal')
                    assert await page.locator('#draft').input_value() == original + clipboard
                    await page.evaluate('document.activeElement?.blur()')
                    point = await begin()
                    await asyncio.sleep(.1)
                    await touch.send('Input.dispatchTouchEvent', {'type': 'touchMove', 'touchPoints': [{**point, 'y': point['y'] - 40}]})
                    await asyncio.sleep(.7)
                    await end()
                    assert not await page.locator('#copy-dialog').evaluate('e=>e.open'), 'Dragging must cancel long-press selection'
                    assert await page.evaluate('!document.getElementById("composer").open&&document.activeElement!==MobileTerminal.terminal.textarea&&document.activeElement!==document.getElementById("draft")')
                    assert len(written) == before_input
                    return {'tap_keeps_keyboard_closed': True, 'long_press_readonly_selection': True,
                            'selection_text_frozen': True, 'paste_preserves_draft_and_unicode': True,
                            'paste_sends_no_terminal_input': True, 'paste_does_not_focus_editor': True,
                            'stale_session_target_refused': True, 'drag_cancels_long_press': True}

                for agent in AGENTS:
                    sid = agent + '-normal'
                    await choose(sid)
                    readonly_start = len(written)
                    readonly_operations = len(operations)
                    await click('history-bottom')
                    bottom = await position()
                    assert bottom['base'] >= 100 and bottom['y'] == bottom['base']
                    await click('page-up')
                    up = await position()
                    assert up['y'] < bottom['y'] or up['top'] < bottom['top']
                    await click('page-down')
                    down = await position()
                    assert down['y'] > up['y'] or down['top'] > up['top']
                    await click('history-top')
                    assert (await position())['y'] == 0
                    await click('history-bottom')
                    assert (await position())['y'] == (await position())['base']
                    count = len(refreshes)
                    await click('refresh-screen')
                    await eventually(lambda: len(refreshes) == count + 1)
                    await grid.wait(page, 'MobileTerminal.ready&&!document.getElementById("refresh-screen").hasAttribute("aria-busy")')
                    assert len(written) == readonly_start
                    assert all(op in [10, 16] for op in operations[readonly_operations:]), operations[readonly_operations:]
                    if agent == 'codex':
                        touch_selection = await check_touch_selection()

                    for key, expected in [('escape', '\x1b'), ('tab', '\t'), ('f2', '\x1bOQ'),
                                          ('up', '\x1b[A'), ('down', '\x1b[B'),
                                          ('left', '\x1b[D'), ('right', '\x1b[C'), ('enter', '\r')]:
                        start = len(written)
                        await page.locator(f'[data-key={key}]').click()
                        await eventually(lambda: len(written) == start + 1)
                        assert written[-1] == (sid, expected), (agent, key, written[-1])
                    await emit(sid, '\x1b[?1h')
                    await grid.wait(page, 'MobileTerminal.terminal.modes.applicationCursorKeysMode')
                    await page.locator('[data-key=up]').click()
                    await eventually(lambda: written[-1] == (sid, '\x1bOA'))
                    await emit(sid, '\x1b[?1l')
                    await grid.wait(page, '!MobileTerminal.terminal.modes.applicationCursorKeysMode')
                    await page.locator('#screen .xterm-helper-textarea').focus()
                    await page.locator('#ctrl').click()
                    await page.keyboard.press('c')
                    await eventually(lambda: written[-1] == (sid, '\x03'))
                    assert await page.locator('#ctrl').get_attribute('aria-pressed') == 'false'

                    await click('mode')
                    await page.locator('#draft').fill('保留草稿，不覆盖')
                    await click('composer-close')
                    await click('github')
                    assert await page.locator('#draft').input_value() == '保留草稿，不覆盖'
                    await page.locator('#draft').fill('中文指令 ' + agent)
                    start = len(written)
                    await click('send')
                    await eventually(lambda: len(written) >= start + 2)
                    await grid.wait(page, '!document.getElementById("composer").open')
                    assert len(written) == start + 2 and written[-1] == (sid, '\r')
                    assert '中文指令 ' + agent in written[-2][1]
                    start = len(written)
                    await click('model')
                    await eventually(lambda: len(written) >= start + 2)
                    assert len(written) == start + 2 and '/model' in written[-2][1] and written[-1] == (sid, '\r')
                    assert await page.locator('#github').inner_text() == '提交'
                    start = len(written)
                    await click('github')
                    assert await page.locator('#draft').input_value() == '提交全部文件到 GitHub'
                    assert len(written) == start, 'GitHub preset must wait for explicit Send'
                    await click('composer-close')
                    normal = {'passed': True, 'history_four_buttons': True, 'refresh_snapshot': True,
                              'auxiliary_keys': True, 'application_cursor_keys': True, 'ctrl_c': True,
                              'editor_unicode_single_enter': True, 'github_preserves_draft': True, 'model_single_enter': True}

                    sid = agent + '-alternate'
                    await choose(sid)
                    readonly_start = len(written)
                    readonly_operations = len(operations)
                    assert await page.evaluate('MobileTerminal.terminal.buffer.active.type') == 'alternate'
                    await asyncio.sleep(.35)
                    for stage in ['B', 'C', 'D']:
                        await emit(sid, screen(stage))
                        await asyncio.sleep(.85)
                    await click('history-top')
                    await grid.wait(page, '!document.getElementById("screen-history").hidden')
                    await grid.wait(page, 'document.getElementById("history-terminal").textContent.includes("stage A")')
                    assert await page.locator('#screen').evaluate('e=>e.hidden')
                    assert await page.evaluate('MobileTerminal.terminal.buffer.active.baseY') == 0
                    await emit(sid, screen('E'))
                    await asyncio.sleep(.85)
                    assert await page.locator('#history-terminal').inner_text() and 'stage A' in await page.locator('#history-terminal').inner_text()
                    assert await page.evaluate('MobileTerminal.terminal.buffer.active.getLine(0).translateToString(true).includes("stage E")')
                    for _ in range(4):
                        await click('page-down')
                        if 'stage A' not in await page.locator('#history-terminal').inner_text():
                            break
                    assert 'stage A' not in await page.locator('#history-terminal').inner_text(), agent
                    await click('history-bottom')
                    assert await page.locator('#screen-history').evaluate('e=>e.hidden')
                    for _ in range(4):
                        await click('page-up')
                        if not await page.locator('#screen-history').evaluate('e=>e.hidden'):
                            break
                    assert not await page.locator('#screen-history').evaluate('e=>e.hidden'), agent
                    assert len(written) == readonly_start
                    assert all(op in [10, 16] for op in operations[readonly_operations:]), operations[readonly_operations:]
                    await page.screenshot(path=OUT / (agent + '-alternate-history.png'))
                    await click('history-bottom')
                    alternate = {'passed': True, 'native_scrollback_absent': True, 'local_frames_readable': True,
                                 'history_four_buttons': True, 'new_output_does_not_move_reader': True,
                                 'navigation_sends_no_input': True}
                    results.append({'agent': agent, 'normal': normal, 'alternate': alternate})
                    print(json.dumps({'agent': agent, 'normal': True, 'alternate': True}), flush=True)
                    (OUT / 'verification.json').write_text(json.dumps({'passed': False, 'results': results}, indent=2), 'utf-8')
                assert not errors, errors
                assert not any(op in operations for op in [4, 5]), operations
                report = {'passed': True, 'physical_android': False, 'agents': AGENTS, 'results': results,
                          'touch_selection': touch_selection,
                          'no_remote_resize': True, 'legacy_input_opcode_unused': True,
                          'specialized_suites': ['model-button-ui.py', 'attachments-ui.py', 'debug-grid-ui.py', 'test_android_control_follow.py']}
                (OUT / 'verification.json').write_text(json.dumps(report, indent=2), 'utf-8')
                print(json.dumps({'passed': True, 'agents': len(results), 'normal_and_alternate_cases': len(results) * 2}))
            except BaseException:
                await page.screenshot(path=OUT / 'failure.png')
                raise
            finally:
                await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
