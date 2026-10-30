"""Android attachments reach the owned project through the file broker, without OP21."""
from pathlib import Path
from urllib.parse import quote
import json

import pytest
from websockets.asyncio.server import serve

from supermonoagent import web
from test_file_companion import environment


ROOT = Path(__file__).resolve().parents[1]
EDGE = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')


def test_android_file_and_image_upload_without_op21(tmp_path):
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Owned Edge browser unavailable')
    import asyncio
    asyncio.run(check_upload(playwright, tmp_path))


async def check_upload(playwright, tmp_path):
    async with environment(tmp_path) as (broker, _url, parents, _companions):
        parent = parents[0]
        async def http(connection, request):
            path = request.path.split('?', 1)[0]
            if path.startswith('/mobile/'):
                name = path[len('/mobile/'):]
                if name.startswith('vendor/'):
                    resource = ROOT / 'src/supermonoagent/static' / name[len('vendor/'):]
                elif name.startswith('agent-modes/'):
                    resource = ROOT / 'src/supermonoagent/static/agent-modes' / name[len('agent-modes/'):]
                else:
                    resource = ROOT / 'android/assets/mobile' / name
                if resource.is_file():
                    mime = {'.html': 'text/html', '.css': 'text/css', '.json': 'application/json'}.get(resource.suffix, 'text/javascript')
                    return web.text_response(connection, 200, resource.read_text('utf-8'), mime + '; charset=utf-8')
            return await parent.http(connection, request)
        async def route(ws):
            if ws.request.path == '/file/ws':
                await broker.handle_socket(ws)
                return
            greeting = json.loads(await ws.recv())
            if greeting.get('op') == 'watch':
                await ws.send(json.dumps({'op': 'directory', 'revision': 0, 'hosts': [], 'capabilities': {}}))
                greeting = json.loads(await ws.recv())
            assert greeting.get('op') == 'phone' and greeting.get('code') == 'A'
            await ws.send(json.dumps({'op': 'ready'}))
            await parent.handle(ws)
        async with serve(route, '127.0.0.1', 0, process_request=http) as server:
            origin = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
            config = {'endpoint': origin, 'code': 'A', 'token': 'owned-viewer-token', 'renderer': 'dom'}
            document = origin + '/mobile/index.html?ui_revision=android-file-upload#' + quote(json.dumps(config))
            async with playwright.async_playwright() as runtime:
                browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
                try:
                    page = await browser.new_page(viewport={'width': 440, 'height': 900}, is_mobile=True, has_touch=True)
                    errors, sockets = [], []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('websocket', lambda socket: sockets.append(socket.url))
                    await page.goto(document)
                    await page.wait_for_function('window.MobileTerminal?.ready && MobileTerminal.connected')
                    assert await page.evaluate('MobileTerminal.active') == 'session1'
                    await page.locator('#mode').click()
                    image = b'\x89PNG\r\nandroid owned image'
                    document_bytes = b'%PDF-1.4 android owned document'
                    async with page.expect_file_chooser() as chooser_event:
                        await page.locator('#attach-file').click()
                    chooser = await chooser_event.value
                    await chooser.set_files([
                        {'name': 'android-image.png', 'mimeType': 'image/png', 'buffer': image},
                        {'name': 'android-document.pdf', 'mimeType': 'application/pdf', 'buffer': document_bytes},
                    ])
                    await page.wait_for_function("document.getElementById('upload-files').disabled===false")
                    await page.locator('#upload-files').click()
                    await page.wait_for_function("document.getElementById('attachment-status').textContent.includes('路径已加入草稿')", timeout=20000)
                    saved = [path for path in (parents[0].project / 'tmp').glob('*') if path.is_file()]
                    assert len(saved) == 2
                    for name, data in [('android-image.png', image), ('android-document.pdf', document_bytes)]:
                        match = next(path for path in saved if path.name.endswith('-' + name))
                        assert match.read_bytes() == data
                        assert match.name in await page.locator('#draft').input_value()
                    assert not (parents[1].project / 'tmp').exists()
                    assert all(not parent.pty_written for parent in parents)
                    assert any(url.endswith('/file/ws') for url in sockets)
                    assert not errors, errors
                    artifacts = ROOT / '.omx/artifacts/android-file-upload'
                    artifacts.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(artifacts / 'saved.png'))
                finally:
                    await browser.close()
