"""Android files and images against a real isolated FileTransferRPC receiver."""
import asyncio
import importlib.util
import json
import mimetypes
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import async_playwright
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from supermonoagent.file_transfer_rpc import FileTransferRPC

spec = importlib.util.spec_from_file_location('grid', Path(__file__).with_name('debug-grid-ui.py'))
grid = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grid)


async def main():
    operations, errors, clients = [], [], []
    capability, slow = True, False
    chunk_seen, release_chunk = asyncio.Event(), asyncio.Event()
    with tempfile.TemporaryDirectory() as tmp:
        receiver = FileTransferRPC(Path(tmp) / 'received', lambda owner, sid: sid in ['first', 'second'])

        def state(sub):
            return {'capabilities': {'multiplex': True, 'snapshot_sgr': True, 'file_transfer': capability},
                    'machine': 'isolated-attachment-test',
                    'windows': [{'id': 'window-' + sid, 'title': 'Codex ' + sid,
                                 'sessions': [{'id': sid, 'agent': 'codex'}]} for sid in ['first', 'second']],
                    'controls': {sid: {'cols': 158, 'rows': 43, 'owner': False} for sid in sub}}

        async def socket(ws):
            sub = set()
            clients.append(ws)
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
                        await ws.send(grid.frame(2, state(sub)))
                    elif op == 16:
                        wanted = set(json.loads(payload)['sessions'])
                        for sid in wanted - sub:
                            await ws.send(grid.feed(8, sid, '\x1b[2J\x1b[H> '))
                        sub = wanted
                        await ws.send(grid.frame(7, state(sub)))
                    elif op == 21:
                        body = json.loads(payload)
                        if slow and body['action'] == 'chunk':
                            chunk_seen.set()
                            await release_chunk.wait()
                        await ws.send(grid.frame(21, receiver.handle('fixture-owner', body)))
                    elif op == 10:
                        await ws.send(grid.frame(11, payload))
            finally:
                clients.remove(ws)
                receiver.cancel_owner('fixture-owner')

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
                    await page.locator('#mode').click()
                    await page.locator('#draft').fill('分析这两个附件，先不要执行命令。')

                    async def choose(button, files):
                        async with page.expect_file_chooser() as event:
                            await page.locator(button).click()
                        await (await event.value).set_files(files)

                    text = ('UTF-8 文本与多分块内容\n' * 10000).encode()
                    image = b'\x89PNG\r\n\x1a\n' + bytes(range(256))
                    async with page.expect_file_chooser() as picker_event:
                        await page.locator('#attach-file').click()
                    picker = await picker_event.value
                    await page.evaluate('MobileTerminal.pause()')
                    await picker.set_files([{'name': '说明.txt', 'mimeType': 'text/plain', 'buffer': text}])
                    await page.evaluate('MobileTerminal.resume()')
                    await grid.wait(page, 'MobileTerminal.ready&&document.getElementById("composer").open&&!document.getElementById("upload-files").disabled')
                    await choose('#attach-image', [{'name': '图片.png', 'mimeType': 'image/png', 'buffer': image}])
                    assert await page.locator('.attachment-item').count() == 2
                    assert await page.evaluate('MobileTerminal.captureUIState()') is None, 'Pending files must prevent an update from silently dropping them'
                    await page.locator('#upload-files').click()
                    await grid.wait(page, "document.getElementById('attachment-status').textContent.includes('路径已加入草稿')")
                    draft = await page.locator('#draft').input_value()
                    assert draft.startswith('分析这两个附件，先不要执行命令。\n')
                    paths = [Path(json.loads(line[len('附件：'):])) for line in draft.splitlines() if line.startswith('附件：')]
                    assert len(paths) == 2 and [path.read_bytes() for path in paths] == [text, image]
                    assert await page.evaluate('MobileTerminal.captureUIState()') is not None
                    slow = True
                    await choose('#attach-file', [{'name': '取消.txt', 'mimeType': 'text/plain', 'buffer': b'cancel me'}])
                    await page.locator('#upload-files').click()
                    await asyncio.wait_for(chunk_seen.wait(), 5)
                    await page.locator('#cancel-upload').click()
                    await page.locator('#composer-close').click()
                    await page.locator('#sessions').click()
                    await page.locator('#session-list .session[data-session=second]').click()
                    release_chunk.set()
                    await page.locator('#mode').click()
                    await grid.wait(page, '!document.getElementById("model").disabled')
                    assert await page.locator('#draft').input_value() == ''
                    assert await page.locator('.attachment-item').count() == 0
                    capability = False
                    for ws in list(clients):
                        await ws.send(grid.frame(7, state({'second'})))
                    await choose('#attach-file', [{'name': 'unsupported.txt', 'mimeType': 'text/plain', 'buffer': b'no capability'}])
                    assert await page.locator('#upload-files').is_disabled()
                    assert '尚未开启文件接收' in await page.locator('#attachment-status').inner_text()
                    assert not errors, errors
                    assert all(op in [1, 10, 16, 21] for op in operations), operations
                    out = ROOT / '.omx/artifacts/android-attachments'
                    out.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=out / 'unsupported-composer.png')
                    report = {'passed': True, 'real_receiver': True, 'text_and_image_bytes_match': True,
                              'draft_preserved': True, 'cancel_and_target_switch': True,
                              'unsupported_host_gated': True, 'native_picker_pause_resume': True,
                              'no_terminal_input_or_resize': True}
                    (out / 'verification.json').write_text(json.dumps(report, indent=2), 'utf-8')
                    print(json.dumps(report))
                finally:
                    await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
