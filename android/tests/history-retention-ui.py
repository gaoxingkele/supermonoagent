"""Isolated Android history retention against a real daemon and legacy VT peers.

No phones, production relays, saved daemon state or real PTYs are used.
"""
import asyncio
import json
import mimetypes
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from playwright.async_api import async_playwright
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
from supermonoagent.daemon import Daemon
from supermonoagent.history_context import unpack_history_context
from supermonoagent.protocol import pack_feed, pack_json, unpack
from test_client_integration import ControlledPty

OUT = ROOT / '.omx/artifacts/android-history-retention'
FIRST = 'FIRST_RETAINED_MARKER'
RIGHT = 'WIDE_RIGHT_EDGE_OLD'
DELTA = 'DETACHED_DELTA_ONLY_ONCE'


def logs():
    head = FIRST + ' ' * 45 + RIGHT + '\r\n'
    return (head + ''.join(f'ROW_{i:05d}_' + 'x' * 56 + '\r\n' for i in range(6000))
            + 'TAIL_LIVE_6000\r\n').encode()


async def wait(page, expression, arg=None, timeout=4):
    async with asyncio.timeout(timeout):
        while not await page.evaluate(expression, arg):
            await asyncio.sleep(.01)


async def assets(connection, request):
    path = request.path.split('?')[0]
    if not path.startswith('/mobile/'):
        return None
    name = path[len('/mobile/'):]
    base = ROOT / ('src/supermonoagent/static' if name.startswith('vendor/') else 'android/assets/mobile')
    resource = (base / name.removeprefix('vendor/')).resolve()
    if not resource.is_relative_to(base) or not resource.is_file():
        return Response(404, 'Not Found', Headers(), b'')
    return Response(200, 'OK', Headers({'Content-Type': mimetypes.guess_type(name)[0] or 'application/octet-stream'}), resource.read_bytes())


async def open_page(browser, origin, token):
    page = await browser.new_page(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    config = {'endpoint': origin, 'code': '', 'token': token, 'renderer': 'dom'}
    await page.goto(origin + '/mobile/index.html#' + quote(json.dumps(config)))
    await wait(page, 'MobileTerminal.ready', timeout=8)
    return page, errors


async def choose(page, sid):
    await page.locator('#sessions').click()
    await page.locator(f'#session-list .session[data-session="{sid}"]').click()
    await wait(page, 'sid=>MobileTerminal.ready&&MobileTerminal.active===sid', sid)


async def count(page, marker):
    return await page.evaluate('''marker=>{const b=MobileTerminal.terminal.buffer.active;let count=0;for(let i=0;i<b.length;i++){const text=b.getLine(i)?.translateToString(true)||'';count+=text.split(marker).length-1;}return count;}''', marker)


async def contains(page, marker):
    await wait(page, '''marker=>{const b=MobileTerminal.terminal.buffer.active;for(let i=0;i<b.length;i++)if(b.getLine(i)?.translateToString(true).includes(marker))return true;return false;}''', marker)


async def modern(browser):
    subscriptions, contexts = [], []
    with tempfile.TemporaryDirectory(prefix='android-history-modern-') as temporary, patch(
            'supermonoagent.daemon.STATE', Path(temporary)), patch.dict(os.environ, {'SUPERMONO_STATE_DIR': temporary}):
        daemon = Daemon('history-retention-fixture')
        daemon.hub.persist = False
        daemon.hub.pty_factory = lambda *_: ControlledPty()
        daemon.hub.attach_loop(asyncio.get_running_loop())
        window = daemon.hub.open_window('History retention fixture', owner=daemon.token)
        first = daemon.hub.open_cli(window.id, 'cmd')
        second = daemon.hub.open_cli(window.id, 'powershell')
        original_subscribe = daemon.hub.set_subscriptions
        original_snapshot = daemon.hub._session_snapshot

        def subscribe(client, sessions, **kwargs):
            subscriptions.append({'sessions': list(sessions), 'history_after': json.loads(json.dumps(kwargs.get('history_after')))})
            return original_subscribe(client, sessions, **kwargs)

        def snapshot(client, session):
            frame = original_snapshot(client, session)
            op, payload = unpack(frame)
            if op == 23:
                sid, context, data = unpack_history_context(payload)
                contexts.append({'session': sid, **context, 'bytes': len(data)})
            return frame

        daemon.hub.set_subscriptions = subscribe
        daemon.hub._session_snapshot = snapshot

        async def http(connection, request):
            response = await assets(connection, request)
            return response if response is not None else await daemon.serve_http(connection, request)

        try:
            async with serve(daemon.handle_socket, '127.0.0.1', 0, process_request=http) as server:
                origin = 'http://127.0.0.1:' + str(server.sockets[0].getsockname()[1])
                page, errors = await open_page(browser, origin, daemon.token)
                try:
                    assert await page.evaluate('MobileTerminal.active') == first.id
                    await page.evaluate('window.retainedTerminal=MobileTerminal.terminal')
                    data = logs()
                    assert len(data) > 256 * 1024
                    for at in range(0, len(data), 16384):
                        first.pty.inbox.put(data[at:at + 16384])
                        await asyncio.sleep(.005)
                    await contains(page, 'TAIL_LIVE_6000')
                    assert await count(page, FIRST) == 1
                    assert FIRST.encode() not in first.pump.snapshot(), 'Old marker must already be absent from the 256 KiB server tail'
                    assert await page.evaluate('MobileTerminal.terminal.buffer.active.baseY') >= 5900
                    await choose(page, second.id)
                    first.pty.inbox.put((DELTA + '\r\n').encode())
                    async with asyncio.timeout(4):
                        while DELTA.encode() not in first.pump.snapshot():
                            await asyncio.sleep(.01)
                    assert await count(page, DELTA) == 0, 'Detached output leaked into the other session'
                    await choose(page, first.id)
                    assert await page.evaluate('MobileTerminal.terminal===window.retainedTerminal')
                    await contains(page, DELTA)
                    assert await count(page, FIRST) == 1 and await count(page, DELTA) == 1
                    assert any(row['history_after'] and first.id in row['history_after'] for row in subscriptions)
                    assert any(row['session'] == first.id and row['kind'] == 'resume' and row['bytes'] for row in contexts)
                    await choose(page, second.id)
                    await choose(page, first.id)
                    assert await page.evaluate('MobileTerminal.terminal===window.retainedTerminal')
                    assert await count(page, FIRST) == 1 and await count(page, DELTA) == 1
                    assert any(row['session'] == first.id and row['kind'] == 'resume' and row['bytes'] == 0 for row in contexts)
                    await page.evaluate('MobileTerminal.pause()')
                    await page.evaluate('MobileTerminal.resume()')
                    await wait(page, 'MobileTerminal.ready')
                    assert await page.evaluate('MobileTerminal.terminal===window.retainedTerminal')
                    assert await count(page, FIRST) == 1 and await count(page, DELTA) == 1

                    # Delay actual parse completion, never the underlying parser or byte data.
                    await page.evaluate('''()=>{const t=MobileTerminal.terminal;window.historyOriginalWrite=t.write.bind(t);t.write=(bytes,done)=>historyOriginalWrite(bytes,()=>setTimeout(done,150));}''')
                    busy = ''.join(f'BUSY_{i:05d}_' + 'y' * 48 + '\r\n' for i in range(999)).encode()
                    assert len(busy) < 128 * 1024
                    first.pty.inbox.put(busy)
                    await contains(page, 'BUSY_00000_')
                    await choose(page, second.id)
                    assert await count(page, 'BUSY_') == 0, 'An unfinished write was routed into another session'
                    await choose(page, first.id)
                    await contains(page, 'BUSY_00998_')
                    assert await page.evaluate('MobileTerminal.terminal===window.retainedTerminal')
                    for marker in ['BUSY_00000_', 'BUSY_00500_', 'BUSY_00998_']:
                        assert await count(page, marker) == 1, marker
                    busy_counts = await page.evaluate(r'''()=>{const b=MobileTerminal.terminal.buffer.active,counts={};for(let i=0;i<b.length;i++)for(const match of (b.getLine(i)?.translateToString(true)||'').matchAll(/BUSY_(\d{5})_/g))counts[match[1]]=(counts[match[1]]||0)+1;return counts;}''')
                    assert len(busy_counts) == 999 and all(value == 1 for value in busy_counts.values()), busy_counts
                    assert await count(page, FIRST) == 1
                    await page.evaluate('()=>{MobileTerminal.terminal.write=window.historyOriginalWrite;}')

                    # The pre-resize wide line must survive as a readable archive.
                    first.pty.resize = lambda cols, rows: first.pty.inbox.put((f'\x1b[2J\x1b[HNARROW_CANVAS\x1b[{rows};1H> ').encode())
                    old_cols = first.cols
                    old_rows = first.rows
                    await page.locator('#control').click()
                    await wait(page, 'MobileTerminal.controls?.owner&&MobileTerminal.ready')
                    assert first.cols < old_cols
                    await page.locator('#history-top').click()
                    await wait(page, '!document.getElementById("screen-history").hidden')
                    await wait(page, 'document.getElementById("history-terminal").textContent.includes("FIRST_RETAINED_MARKER")')
                    assert RIGHT in await page.locator('#history-terminal').text_content()
                    await page.screenshot(path=OUT / 'modern-wide-history.png')
                    await page.locator('#history-bottom').click()
                    await page.locator('#control').click()
                    await wait(page, 'MobileTerminal.controls?.owner===false&&MobileTerminal.ready')
                    async with connect(origin.replace('http:', 'ws:') + '/ws') as desktop:
                        await desktop.send(pack_json(1, {'token': daemon.token, 'multiplex': True, 'reliable': True}))
                        await desktop.recv()
                        await desktop.send(pack_json(16, {'sessions': [first.id]}))
                        await desktop.send(pack_json(5, {'session': first.id, 'cols': old_cols, 'rows': old_rows, 'take_control': True}))
                        await wait(page, 'size=>MobileTerminal.ready&&MobileTerminal.terminal.cols===size[0]&&MobileTerminal.terminal.rows===size[1]&&MobileTerminal.controls?.owner===false', [old_cols, old_rows])
                        await page.locator('#history-top').click()
                        await wait(page, 'document.getElementById("history-terminal").textContent.includes("FIRST_RETAINED_MARKER")')
                        assert RIGHT in await page.locator('#history-terminal').text_content()
                    assert not errors, errors
                    assert not first.pty.written and not second.pty.written, 'History navigation sent PTY input'
                    return {'passed': True, 'bytes_received_live': len(data), 'unique_log_lines': 6000,
                            'old_marker_absent_from_server_ring': True, 'terminal_object_reused': True,
                            'exact_resume_delta_once': True, 'empty_resume_preserves_history': True,
                            'pause_resume_preserves_history': True, 'busy_queue_switch_exact': True,
                            'busy_unique_lines_exactly_once': len(busy_counts), 'no_cross_session_bytes': True,
                            'wide_history_survives_takeover_and_follow': True,
                            'resume_contexts': [row for row in contexts if row['kind'] == 'resume']}
                finally:
                    await page.close()
        finally:
            for window_id in list(daemon.hub.windows):
                daemon.hub.close_window(window_id)
            await daemon.close_sidecars()


async def legacy(browser, baseline):
    tails = {'first': bytearray(), 'second': bytearray(b'SECOND_MARKER\r\n')}
    clients = []
    sent_inputs = []

    def state(client):
        return {'capabilities': {'multiplex': True, 'snapshot_sgr': True},
                'windows': [{'id': sid, 'title': 'Legacy ' + sid, 'sessions': [{'id': sid}]} for sid in tails],
                'controls': {sid: {'cols': 100, 'rows': 28, 'owner': False} for sid in client['sub']}}

    async def socket(ws):
        client = {'ws': ws, 'sub': set()}
        clients.append(client)
        try:
            async for frame in ws:
                op, payload = unpack(frame)
                if op == 1:
                    await ws.send(pack_json(2, state(client)))
                elif op == 16:
                    wanted = set(json.loads(payload)['sessions'])
                    for sid in wanted - client['sub']:
                        await ws.send(pack_feed(8, sid, bytes(tails[sid])))
                    client['sub'] = wanted
                    await ws.send(pack_json(7, state(client)))
                elif op == 10:
                    await ws.send(pack_json(11, {}))
                elif op in [4, 5, 17]:
                    sent_inputs.append(op)
        finally:
            clients.remove(client)

    async def emit(sid, data):
        tails[sid].extend(data)
        del tails[sid][:-256 * 1024]
        for client in list(clients):
            if sid in client['sub']:
                await client['ws'].send(pack_feed(3, sid, data))

    async with serve(socket, '127.0.0.1', 0, process_request=assets) as server:
        origin = 'http://127.0.0.1:' + str(server.sockets[0].getsockname()[1])
        page, errors = await open_page(browser, origin, 'legacy-fixture')
        try:
            await page.evaluate('window.retainedTerminal=MobileTerminal.terminal')
            data = logs()
            for at in range(0, len(data), 16384):
                await emit('first', data[at:at + 16384])
                await asyncio.sleep(.005)
            await contains(page, 'TAIL_LIVE_6000')
            assert await count(page, FIRST) == 1
            assert FIRST.encode() not in tails['first']
            await choose(page, 'second')
            delta = b'\x1b[2J\x1b[HBASELINE_REDRAW\r\n> ' if baseline else b'LEGACY_DETACHED_DELTA\r\n'
            await emit('first', delta)
            await choose(page, 'first')
            assert await page.evaluate('MobileTerminal.terminal===window.retainedTerminal')
            await contains(page, 'BASELINE_REDRAW' if baseline else 'LEGACY_DETACHED_DELTA')
            if baseline:
                assert await count(page, FIRST) == 1, 'A safe redraw must not reset the local scrollback'
                assert await count(page, 'BASELINE_REDRAW') == 1
            else:
                assert await count(page, FIRST) == 0, 'The old marker must now come from the archive, not the short server tail'
                await page.locator('#history-top').click()
                await wait(page, '!document.getElementById("screen-history").hidden')
                await wait(page, 'document.getElementById("history-terminal").textContent.includes("FIRST_RETAINED_MARKER")')
                assert RIGHT in await page.locator('#history-terminal').text_content()
                await page.screenshot(path=OUT / 'legacy-archived-history.png')
            assert not errors, errors
            assert not sent_inputs, sent_inputs
            return {'passed': True, 'safe_redraw_baseline': baseline,
                    'old_marker_absent_from_server_ring': True, 'terminal_object_reused': True,
                    'local_history_preserved_in': 'native_scrollback' if baseline else 'readonly_archive',
                    'no_input_or_resize': True}
        finally:
            await page.close()


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    report = {'passed': False, 'physical_android': False}
    async with async_playwright() as runtime:
        browser = await runtime.chromium.launch(channel='msedge')
        try:
            report['modern'] = await modern(browser)
            print('modern retention and resize passed', flush=True)
            report['legacy_plain_log'] = await legacy(browser, False)
            print('legacy plain-log archive passed', flush=True)
            report['legacy_redraw'] = await legacy(browser, True)
            print('legacy full redraw preserves scrollback', flush=True)
            report['passed'] = True
        finally:
            (OUT / 'verification.json').write_text(json.dumps(report, indent=2), 'utf-8')
            await browser.close()
    print(json.dumps({'passed': True, 'cases': 3, 'real_daemon_and_legacy_peers': True}))


if __name__ == '__main__':
    asyncio.run(main())
