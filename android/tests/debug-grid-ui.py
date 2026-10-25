"""Isolated Android UI tests. Uses fake relay rooms; never connects to real terminals."""
import asyncio
import argparse
import json
import mimetypes
import struct
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / '.omx/artifacts/android-debug-grid/browser'


def frame(op, value):
    payload = value if isinstance(value, bytes) else json.dumps(value).encode()
    body = bytes([op]) + payload
    return struct.pack('<I', len(body)) + body


def feed(op, sid, text):
    return frame(op, sid.encode() + b'\0' + text.encode())


async def wait(page, predicate, timeout=10):
    async with asyncio.timeout(timeout):
        while not await page.evaluate(predicate):
            await asyncio.sleep(.05)


async def main(android_zip=None):
    OUT.mkdir(parents=True, exist_ok=True)
    clients, operations, errors = [], [], []
    online = {'pc1': True, 'pc2': True}
    packaged = None
    if android_zip:
        with zipfile.ZipFile(android_zip) as archive:
            packaged = {name.removeprefix('mobile/'): archive.read(name)
                        for name in archive.namelist() if name.startswith('mobile/')}
    windows = {pc: [{'id': f'w{i}', 'title': f'{pc} Codex 窗口 {i}', 'focused': f's{i}',
                     'sessions': [{'id': f's{i}', 'agent': 'codex', 'shell': 'powershell'}]}
                    for i in range(count)] for pc, count in [('pc1', 11), ('pc2', 7)]}

    def state(client):
        return {'capabilities': {'multiplex': True, 'snapshot_sgr': True},
                'windows': windows[client['pc']], 'machine': client['pc'],
                'controls': {sid: {'cols': 158, 'rows': 43, 'owner': False}
                             for sid in client['sub']}}

    async def socket(ws):
        client = {'ws': ws, 'pc': None, 'sub': set()}
        clients.append(client)
        try:
            async for message in ws:
                if isinstance(message, str):
                    value = json.loads(message)
                    if value['op'] == 'watch':
                        await ws.send(json.dumps({'op': 'directory', 'revision': 1,
                                            'capabilities': {'token_discovery': False},
                                            'hosts': [{'code': pc, 'online': online[pc], 'machine': pc}
                                                      for pc in online]}))
                    elif value['op'] == 'phone':
                        client['pc'] = value['code']
                        if not online[client['pc']]:
                            await ws.close(code=4004)
                        else:
                            await ws.send(json.dumps({'op': 'ready'}))
                elif isinstance(message, bytes):
                    op, data = message[4], message[5:]
                    operations.append(op)
                    if op == 1:
                        await ws.send(frame(2, state(client)))
                    elif op == 16:
                        wanted = set(json.loads(data)['sessions'])
                        for sid in wanted - client['sub']:
                            await ws.send(feed(8, sid, '\x1b[2J\x1b[H'
                                                     + client['pc'] + ' ' + sid
                                                     + '\x1b[4;1H等待确认：<img src=x onerror=alert(1)> '
                                                     + '这是持续更新的系统提示 ' * 8))
                        client['sub'] = wanted
                        # Regression: snapshots arrive BEFORE canonical controls.
                        await ws.send(frame(7, state(client)))
                    elif op == 10:
                        await ws.send(frame(11, data))
        except ConnectionClosed:
            pass
        finally:
            clients.remove(client)

    class Assets(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            name = urlsplit(self.path).path.removeprefix('/mobile/')
            if packaged is not None:
                data = packaged.get(name)
                if data is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', mimetypes.guess_type(name)[0] or 'application/octet-stream')
                self.end_headers()
                self.wfile.write(data)
                return
            base = ROOT / ('src/supermonoagent/static' if name.startswith('vendor/') else 'android/assets/mobile')
            path = (base / name.removeprefix('vendor/')).resolve()
            if not path.is_relative_to(base) or not path.is_file():
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
            self.end_headers()
            self.wfile.write(path.read_bytes())

    http = ThreadingHTTPServer(('127.0.0.1', 0), Assets)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    server = await serve(socket, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    endpoint = f'http://127.0.0.1:{port}'
    asset_endpoint = f'http://127.0.0.1:{http.server_port}'
    try:
        async with async_playwright() as runtime:
            browser = await runtime.chromium.launch(channel='msedge')
            context = await browser.new_context(viewport={'width': 390, 'height': 844},
                                                is_mobile=True, has_touch=True)
            await context.add_init_script('localStorage.setItem("sma.mobile.hosts",'
                                          + json.dumps(json.dumps([{'id': 'pc2', 'name': 'pc2',
                                                                    'endpoint': endpoint, 'code': 'pc2'}])) + ');')
            page = await context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            config = {'endpoint': endpoint, 'code': 'pc1', 'token': 'fixture-only', 'renderer': 'dom'}
            await page.goto(asset_endpoint + '/mobile/index.html#' + json.dumps(config))
            await wait(page, 'MobileTerminal.ready&&MobileTerminal.hosts.every(h=>h.connected)')
            baseline = await page.evaluate('({active:MobileTerminal.activeKey,cols:MobileTerminal.terminal.cols,rows:MobileTerminal.terminal.rows})')
            await page.locator('#debug').click()
            await wait(page, 'document.querySelectorAll(".debug-tile").length===16&&[...document.querySelectorAll(".debug-marquee")].every(e=>e.textContent.includes("等待确认"))')
            assert await page.locator('#debug-density').input_value() == '16'
            assert await page.locator('#debug-grid').get_attribute('data-density') == '16'
            assert await page.locator('#debug-grid img').count() == 0
            assert await page.locator('.debug-marquee.is-scrolling').count() == 16
            assert await page.locator('#debug-count').inner_text() == '共 18 个终端'
            for width, height in [(390, 844), (844, 390), (280, 740), (1024, 768)]:
                await page.set_viewport_size({'width': width, 'height': height})
                for density in ['9', '16']:
                    await page.select_option('#debug-density', density)
                    await wait(page, f'document.querySelectorAll(".debug-tile").length==={density}')
                    await asyncio.sleep(.15)
                    geometry = await page.evaluate('''()=>{const d=document.getElementById('debug-dialog').getBoundingClientRect();return {fits:d.x>=0&&d.y>=0&&d.right<=innerWidth+1&&d.bottom<=innerHeight+1,tiles:[...document.querySelectorAll('.debug-tile')].map(e=>{const r=e.getBoundingClientRect(),p=e.querySelector('.debug-marquee').getBoundingClientRect();return {square:Math.abs(r.width-r.height)<1,promptVisible:p.top>=r.top&&p.bottom<=r.bottom+1,right:r.right<=d.right};})}}''')
                    await page.screenshot(path=OUT / f'{width}x{height}-{density}.png')
                    assert geometry['fits'] and all(all(t.values()) for t in geometry['tiles']), (width, height, density, geometry)
            await page.select_option('#debug-density', '9')
            await page.locator('#debug-next').click()
            await wait(page, 'document.getElementById("debug-page").textContent==="2 / 2"&&[...document.querySelectorAll(".debug-marquee")].every(e=>e.textContent.includes("等待确认"))')
            assert await page.locator('.debug-tile-host').all_text_contents() == ['pc1 · Codex'] * 2 + ['pc2 · Codex'] * 7
            await page.evaluate('MobileTerminal.pause()')
            async with asyncio.timeout(5):
                while clients:
                    await asyncio.sleep(.05)
            await page.evaluate('MobileTerminal.resume()')
            await wait(page, 'MobileTerminal.ready&&document.querySelectorAll(".debug-tile").length===9&&[...document.querySelectorAll(".debug-marquee")].every(e=>e.textContent.includes("等待确认"))')
            windows['pc1'].pop()
            for client in list(clients):
                if client['pc']:
                    await client['ws'].send(frame(7, state(client)))
            await wait(page, 'document.getElementById("debug-count").textContent==="共 17 个终端"&&document.querySelectorAll(".debug-tile").length===8')
            # A fresh output marks active; old replay does not imply active work.
            for client in list(clients):
                if client['pc']:
                    for sid in client['sub']:
                        await client['ws'].send(feed(3, sid, '\x1b[4;1H\x1b[2K实时提示已更新'))
            await wait(page, '[...document.querySelectorAll(".debug-tile")].every(e=>e.dataset.state==="refreshing")')
            await wait(page, '[...document.querySelectorAll(".debug-tile")].every(e=>e.dataset.state==="waiting")')
            online['pc2'] = False
            for client in list(clients):
                if client['pc'] == 'pc2':
                    await client['ws'].close(code=4004)
            await wait(page, 'document.querySelectorAll(".debug-tile[data-state=disconnected]").length===7')
            await page.locator('#debug-close').click()
            await asyncio.sleep(.2)
            assert await page.evaluate('({active:MobileTerminal.activeKey,cols:MobileTerminal.terminal.cols,rows:MobileTerminal.terminal.rows})') == baseline
            assert all(op in {1, 10, 16} for op in operations), operations
            assert all(len(client['sub']) <= 1 for client in clients), 'Preview connections leaked after close'
            assert not errors, errors
            report = {'passed': True, 'layouts': 8, 'default_density': 16,
                      'packaged_android_zip_tested': bool(android_zip), 'terminal_input': False,
                      'resize_sent': False, 'allowed_opcodes': sorted(set(operations)),
                      'pages': True, 'same_sid_different_hosts': True, 'disconnect': True,
                      'background_resume': True, 'closed_terminal_removed': True,
                      'visible_terminal_unchanged': True, 'preview_connections_closed': True}
            (OUT / 'verification.json').write_text(json.dumps(report, indent=2), 'utf-8')
            print(json.dumps(report))
            await browser.close()
    finally:
        for client in list(clients):
            await client['ws'].close()
        server.close()
        await server.wait_closed()
        http.shutdown()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--android-zip', type=Path)
    asyncio.run(main(parser.parse_args().android_zip))
