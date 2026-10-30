"""Real browser/socket upload with fixture-owned clipboard files, never OS clipboard."""
from pathlib import Path
import asyncio
import pytest

from supermonoagent.windows_clipboard import ClipboardFiles
from test_workspace_profile_rooms import fixture, run, server_barrier, eventually, add_profile, wait_room


def test_native_file_paste_uses_op21_when_exchange_missing_and_plain_text_remains_text(tmp_path):
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
            assert (daemons[0].receive_dir / source.name).read_bytes() == content
            assert await page.evaluate('SuperMonoFilePaste.queued') == 0
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
    from test_file_workspace_ui import run as owned_run
    owned_run(lambda playwright, folder: check_boundary(playwright, folder, boundary), tmp_path)


async def check_boundary(playwright, folder, boundary):
    from test_file_companion import environment
    from test_file_workspace_ui import browser_page, STATIC
    source = folder / 'target-bound.docx'
    data = bytes(range(256)) * 700
    source.write_bytes(data)
    clip = ClipboardFiles(getter=lambda: [source])
    entered, release = asyncio.Event(), asyncio.Event()
    async def read(source_id, offset):
        entered.set()
        await release.wait()
        return clip.read(source_id, offset)
    try:
        async with environment(folder) as (_broker, url, parents, _companions):
            async with browser_page(playwright, url) as (page, errors, _sockets):
                await page.expose_function('fixtureClipSnapshot', clip.snapshot)
                await page.expose_function('fixtureClipRead', read)
                await page.expose_function('fixtureClipRelease', clip.release)
                await page.evaluate("()=>{window.pywebview={api:{clipboard_files:fixtureClipSnapshot,clipboard_chunk:fixtureClipRead,clipboard_release:fixtureClipRelease}};}")
                await page.evaluate((STATIC/'file-upload.js').read_text(encoding='utf-8'))
                await page.evaluate((STATIC/'clipboard-files.js').read_text(encoding='utf-8'))
                await page.evaluate('()=>{window.uploadRun=SuperMonoFilePaste.paste();}')
                async with asyncio.timeout(5):
                    await entered.wait()
                if boundary == 'switch':
                    await page.evaluate('fixtureSelect(1)')
                elif boundary == 'cancel':
                    await page.locator('#file-transfer-progress>button').click()
                elif boundary == 'reconnect':
                    await page.evaluate('fixtureHosts[0].generation++')
                else:
                    result = await page.evaluate("""()=>{const screen=document.createElement('div');screen.className='terminal-screen';
                      const field=document.createElement('textarea');screen.append(field);document.querySelector('#stage').append(screen);
                      window.supermonoClipboardHasFiles=true;const d=new DataTransfer();d.setData('text/plain','during-upload');
                      const event=new ClipboardEvent('paste',{clipboardData:d,bubbles:true,cancelable:true});field.dispatchEvent(event);return event.defaultPrevented;}""")
                    assert result is False
                release.set()
                await page.evaluate('uploadRun')
                assert not (parents[1].project/'tmp').exists()
                saved = [p for p in (parents[0].project/'tmp').glob('*') if p.is_file()]
                if boundary in ('switch', 'text-while-uploading'):
                    assert len(saved) == 1 and saved[0].read_bytes() == data
                else:
                    assert not saved
                assert not list((parents[0].project/'tmp/.supermono-partials').glob('*.part'))
                if boundary == 'reconnect':
                    # Failed item remains explicit and cannot retry into a new connection.
                    assert await page.evaluate('SuperMonoFilePaste.queued') == 1
                    await page.locator('#file-transfer-queue button').filter(has_text='移除').click()
                assert not errors
                assert all(not parent.pty_written for parent in parents)
    finally:
        release.set()
        clip.close()


def test_browser_files_are_prepared_one_at_a_time(tmp_path):
    from test_file_workspace_ui import run as owned_run
    owned_run(check_browser_batch, tmp_path)


async def check_browser_batch(playwright, folder):
    import hashlib
    from test_file_companion import environment
    from test_file_workspace_ui import browser_page, STATIC
    async with environment(folder) as (_broker, url, parents, _companions):
        async with browser_page(playwright, url) as (page, errors, _sockets):
            await page.expose_function('fixtureDigest', lambda values: list(hashlib.sha256(bytes(values)).digest()))
            await page.evaluate("""()=>{
              Object.defineProperty(crypto,'subtle',{value:{digest:async(_,bytes)=>new Uint8Array(await fixtureDigest([...bytes])).buffer}});
              window.preparedFiles=[];const read=File.prototype.arrayBuffer;
              File.prototype.arrayBuffer=async function(){preparedFiles.push(this.name);if(this.name==='one.pdf')await new Promise(r=>window.releaseRead=r);return read.call(this);};
              const screen=document.createElement('div');screen.className='terminal-screen';const field=document.createElement('textarea');
              screen.append(field);document.querySelector('#stage').append(screen);fixturePanes[0].term={textarea:field};
            }""")
            await page.evaluate((STATIC/'file-upload.js').read_text(encoding='utf-8'))
            await page.evaluate((STATIC/'clipboard-files.js').read_text(encoding='utf-8'))
            await page.evaluate("""()=>{const d=new DataTransfer();d.items.add(new File(['first-pdf'],'one.pdf'));d.items.add(new File(['second-docx'],'two.docx'));
              smaWorkspace.activePane.term.textarea.dispatchEvent(new ClipboardEvent('paste',{clipboardData:d,bubbles:true,cancelable:true}));}""")
            await page.wait_for_function('preparedFiles.length===1')
            assert await page.evaluate('preparedFiles') == ['one.pdf']
            await page.evaluate('releaseRead()')
            await page.wait_for_function('!SuperMonoFilePaste.busy')
            assert await page.evaluate('preparedFiles') == ['one.pdf','two.docx']
            assert sorted(p.read_bytes() for p in (parents[0].project/'tmp').glob('*') if p.is_file()) == [b'first-pdf',b'second-docx']
            assert not errors
            assert all(not parent.pty_written for parent in parents)
