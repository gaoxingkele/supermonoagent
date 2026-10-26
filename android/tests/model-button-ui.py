"""Exercise Android /model submission against isolated terminals, never real agents."""
import asyncio
import json
import mimetypes
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import async_playwright
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

import importlib.util
spec = importlib.util.spec_from_file_location('grid_test', Path(__file__).with_name('debug-grid-ui.py'))
grid = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grid)
ROOT = Path(__file__).resolve().parents[2]


async def main():
    written, operations, errors = [], [], []
    echo = True

    def state(sub):
        return {'capabilities': {'multiplex': True, 'snapshot_sgr': True},
                'machine': 'isolated-model-test',
                'windows': [{'id': 'window-' + sid, 'title': 'Codex ' + sid,
                             'sessions': [{'id': sid, 'agent': 'codex'}]}
                            for sid in ['first', 'second']],
                'controls': {sid: {'cols': 158, 'rows': 43, 'owner': False} for sid in sub}}

    async def socket(ws):
        sub = set()
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
                await ws.send(grid.frame(2, state(sub)))
            elif op == 16:
                wanted = set(json.loads(payload)['sessions'])
                for sid in wanted - sub:
                    await ws.send(grid.feed(8, sid, '\x1b[2J\x1b[H> '))
                sub = wanted
                await ws.send(grid.frame(7, state(sub)))
            elif op == 17:
                sid, text = payload.split(b'\0', 1)
                text = text.decode()
                written.append((sid.decode(), text))
                if echo:
                    await ws.send(grid.feed(3, sid.decode(), text.replace('\x1b[200~', '').replace('\x1b[201~', '')))
            elif op == 10:
                await ws.send(grid.frame(11, payload))

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
                assert await page.locator('#github').inner_text() == '提交'
                await page.locator('#draft').evaluate("e=>{e.value='保留这份草稿 📎';e.dispatchEvent(new Event('input'));}")
                await page.evaluate("()=>{document.getElementById('model').click();document.getElementById('model').click();}")
                await grid.wait(page, "document.getElementById('notice').textContent.includes('已发送 /model')")
                assert len(written) == 2 and '/model' in written[0][1] and written[1][1] == '\r', written
                assert await page.locator('#draft').input_value() == '保留这份草稿 📎'
                assert not await page.locator('#composer').evaluate('e=>e.open')
                echo = False
                await page.locator('#model').click()
                await grid.wait(page, "document.getElementById('notice').textContent.includes('未确认 /model')")
                assert len(written) == 3 and '/model' in written[-1][1]
                await page.locator('#model').click()
                await asyncio.sleep(.1)
                assert len(written) == 3, 'Uncertain /model must not be pasted twice'
                await page.locator('[data-key=escape]').click()
                echo = True
                await page.locator('#model').click()
                await grid.wait(page, "document.getElementById('notice').textContent.includes('已发送 /model')")
                assert written[-1][1] == '\r' and len(written) == 6, written
                echo = False
                await page.locator('#model').click()
                await page.locator('#sessions').click()
                await page.locator('#session-list .session[data-session=second]').click()
                await asyncio.sleep(.4)
                assert len(written) == 7 and all(sid == 'first' for sid, _ in written), written
                assert await page.evaluate('MobileTerminal.active') == 'second'
                assert not errors, errors
                assert 5 not in operations, 'The model shortcut must not resize the terminal'
                out = ROOT / '.omx/artifacts/android-model-button'
                out.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=out / 'model-toolbar.png')
                report = {'passed': True, 'verified_single_enter': True, 'draft_preserved': True,
                          'timeout_no_enter_or_duplicate': True, 'switch_cancels_enter': True,
                          'submit_label_short': True, 'resize_sent': False}
                (out / 'verification.json').write_text(json.dumps(report, indent=2), 'utf-8')
                print(json.dumps(report))
            finally:
                await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
