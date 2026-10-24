"""Real browser/socket upload with fixture-owned clipboard files, never OS clipboard."""
from pathlib import Path
import asyncio
import pytest

from supermonoagent.windows_clipboard import ClipboardFiles
from test_workspace_profile_rooms import fixture, run, server_barrier, eventually, add_profile, wait_room


def test_native_file_paste_uses_upload_frames_and_plain_text_remains_text(tmp_path):
    run(lambda playwright: check_paste(playwright, tmp_path))


async def check_paste(playwright, tmp_path):
    source = tmp_path / 'office-report.pdf'
    content = bytes(range(256)) * 700
    source.write_bytes(content)
    clip = ClipboardFiles(getter=lambda: [source])
    try:
        async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, sessions):
            daemons[0].receive_dir = tmp_path / 'target-computer'
            page = await browser.new_page(viewport={'width': 1200, 'height': 800})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto(url)
            await page.wait_for_function('smaWorkspace?.connected&&smaWorkspace.activePane?.ready')
            await page.expose_function('fixtureClipSnapshot', clip.snapshot)
            await page.expose_function('fixtureClipRead', clip.read)
            await page.expose_function('fixtureClipRelease', clip.release)
            await page.evaluate('()=>{window.pywebview={api:{clipboard_files:fixtureClipSnapshot,clipboard_chunk:fixtureClipRead,clipboard_release:fixtureClipRelease}};}')
            await page.evaluate(Path('src/supermonoagent/static/clipboard-files.js').read_text(encoding='utf-8'))
            await page.evaluate('''()=>{window.supermonoClipboardHasFiles=true;smaWorkspace.terminal.textarea.dispatchEvent(new ClipboardEvent('paste',{clipboardData:new DataTransfer(),bubbles:true,cancelable:true}));}''')
            await page.wait_for_function('!SuperMonoFilePaste.busy')
            saved = daemons[0].receive_dir / source.name
            assert saved.exists(), await page.locator('#file-transfer-progress').inner_text()
            assert saved.read_bytes() == content
            assert '已保存到目标电脑' in await page.locator('#file-transfer-progress').inner_text()
            assert not any(session.pty.written for owned in sessions for session in owned)
            # A plain text paste takes the original xterm path unchanged.
            sid = await page.evaluate('smaWorkspace.activePane.raw')
            await page.evaluate('''()=>{window.supermonoClipboardHasFiles=false;const d=new DataTransfer();d.setData('text/plain','ordinary-text');smaWorkspace.terminal.textarea.dispatchEvent(new ClipboardEvent('paste',{clipboardData:d,bubbles:true,cancelable:true}));}''')
            await server_barrier(page, 'local')
            selected = next(session for session in sessions[0] if session.id == sid)
            await eventually(lambda: bytes(selected.pty.written) == b'ordinary-text')
            # A stale file-format beacon must not eat a subsequent text paste,
            # including when the selected old backend lacks file capability.
            clip._getter = lambda: []
            await page.evaluate('''()=>{smaWorkspace.activeHost.capabilities.file_transfer=false;window.supermonoClipboardHasFiles=true;const d=new DataTransfer();d.setData('text/plain','fallback-text');smaWorkspace.terminal.textarea.dispatchEvent(new ClipboardEvent('paste',{clipboardData:d,bubbles:true,cancelable:true}));}''')
            await eventually(lambda: bytes(selected.pty.written) == b'ordinary-textfallback-text')
            assert not errors
    finally:
        clip.close()


@pytest.mark.parametrize('boundary', ['switch', 'cancel', 'reconnect', 'text-while-uploading'])
def test_upload_keeps_original_computer_and_cleans_aborted_file(tmp_path, boundary):
    run(lambda playwright: check_boundary(playwright, tmp_path, boundary))


async def check_boundary(playwright, tmp_path, boundary):
    source = tmp_path / 'target-bound.docx'
    data = bytes(range(256)) * 700
    source.write_bytes(data)
    clip = ClipboardFiles(getter=lambda: [source])
    entered, release = asyncio.Event(), asyncio.Event()

    async def read(source_id, offset):
        entered.set()
        await release.wait()
        return clip.read(source_id, offset)

    try:
        async with fixture(playwright) as (browser, url, endpoint, _trap, _messages, daemons, _ops, _windows, sessions):
            for index, daemon in enumerate(daemons):
                daemon.receive_dir = tmp_path / ('computer-' + str(index))
            page = await browser.new_page(viewport={'width': 1200, 'height': 800})
            await page.goto(url)
            await page.wait_for_function('smaWorkspace?.connected&&smaWorkspace.activePane?.ready')
            await add_profile(page, endpoint, daemons[1].token)
            await wait_room(page, 1)
            await page.locator('#relay-profiles-close').click()
            await page.evaluate('sid=>{const h=[...smaWorkspace.connections.values()].find(h=>h.config.code==="profile-room-1");window.uploadOriginalHost=h;window.uploadOriginalGeneration=h.generation;smaWorkspace.openHostSession(h.id,sid,false);}', sessions[1][0].id)
            await page.expose_function('fixtureClipSnapshot', clip.snapshot)
            await page.expose_function('fixtureClipRead', read)
            await page.expose_function('fixtureClipRelease', clip.release)
            await page.evaluate('()=>{window.pywebview={api:{clipboard_files:fixtureClipSnapshot,clipboard_chunk:fixtureClipRead,clipboard_release:fixtureClipRelease}};}')
            await page.evaluate(Path('src/supermonoagent/static/clipboard-files.js').read_text(encoding='utf-8'))
            await page.evaluate('()=>{window.uploadRun=SuperMonoFilePaste.paste();}')
            async with asyncio.timeout(5):
                await entered.wait()
            if boundary == 'switch':
                await page.evaluate('sid=>smaWorkspace.openHostSession("local",sid,false)', sessions[0][0].id)
            elif boundary == 'cancel':
                await page.locator('#file-transfer-progress button').click()
            elif boundary == 'reconnect':
                await page.evaluate('uploadOriginalHost.ws.close()')
                await page.wait_for_function('uploadOriginalHost.connected&&uploadOriginalHost.generation>uploadOriginalGeneration')
            else:
                await page.evaluate('''()=>{window.supermonoClipboardHasFiles=true;pywebview.api.clipboard_has_files=async()=>false;const d=new DataTransfer();d.setData('text/plain','during-upload');smaWorkspace.terminal.textarea.dispatchEvent(new ClipboardEvent('paste',{clipboardData:d,bubbles:true,cancelable:true}));}''')
                await eventually(lambda: bytes(sessions[1][0].pty.written) == b'during-upload')
            release.set()
            await page.evaluate('uploadRun')
            assert not (daemons[0].receive_dir / source.name).exists()
            saved = daemons[1].receive_dir / source.name
            if boundary in ('switch', 'text-while-uploading'):
                assert saved.read_bytes() == data
            else:
                assert not saved.exists()
            assert not list((daemons[1].receive_dir / '.supermono-partials').glob('*.part'))
            if boundary == 'text-while-uploading':
                assert bytes(sessions[1][0].pty.written) == b'during-upload'
                assert not any(session.pty.written for session in sessions[0] + sessions[1][1:])
            else:
                assert not any(session.pty.written for owned in sessions for session in owned)
    finally:
        release.set()
        clip.close()


def test_browser_files_are_prepared_one_at_a_time(tmp_path):
    run(lambda playwright: check_browser_batch(playwright, tmp_path))


async def check_browser_batch(playwright, tmp_path):
    from supermonoagent.protocol import unpack_json
    entered, release = asyncio.Event(), asyncio.Event()
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, sessions):
        daemons[0].receive_dir = tmp_path / 'browser-received'
        original = daemons[0]._op
        async def gated(client, op, payload):
            if op == 21 and unpack_json(payload).get('action') == 'begin' and not entered.is_set():
                entered.set()
                await release.wait()
            return await original(client, op, payload)
        daemons[0]._op = gated
        page = await browser.new_page()
        try:
            await page.goto(url)
            await page.wait_for_function('smaWorkspace?.connected&&smaWorkspace.activePane?.ready')
            await page.evaluate(Path('src/supermonoagent/static/clipboard-files.js').read_text(encoding='utf-8'))
            await page.evaluate('''()=>{window.preparedFiles=[];const original=File.prototype.arrayBuffer;File.prototype.arrayBuffer=function(){preparedFiles.push(this.name);return original.call(this);};const d=new DataTransfer();d.items.add(new File(['first-pdf'],'one.pdf'));d.items.add(new File(['second-docx'],'two.docx'));smaWorkspace.terminal.textarea.dispatchEvent(new ClipboardEvent('paste',{clipboardData:d,bubbles:true,cancelable:true}));}''')
            async with asyncio.timeout(5):
                await entered.wait()
            assert await page.evaluate('preparedFiles') == ['one.pdf']
            release.set()
            await page.wait_for_function('!SuperMonoFilePaste.busy')
            assert await page.evaluate('preparedFiles') == ['one.pdf', 'two.docx']
            assert (daemons[0].receive_dir / 'one.pdf').read_bytes() == b'first-pdf'
            assert (daemons[0].receive_dir / 'two.docx').read_bytes() == b'second-docx'
            assert not any(session.pty.written for owned in sessions for session in owned)
        finally:
            release.set()
