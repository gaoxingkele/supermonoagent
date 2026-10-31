"""Continuous attachment events against owned broker/host fixtures; no OS clipboard."""
import asyncio
import hashlib
from pathlib import Path
import pytest
from test_file_workspace_ui import run, browser_page, STATIC


def test_consecutive_file_paste_events_keep_each_captured_room(tmp_path):
    run(check_consecutive, tmp_path)


async def check_consecutive(playwright, folder):
    from test_file_companion import drop_dir, environment
    async with environment(folder) as (_broker, url, parents, _companions):
        async with browser_page(playwright, url) as (page, errors, _sockets):
            await page.expose_function('fixtureDigest', lambda data: list(hashlib.sha256(bytes(data)).digest()))
            await page.evaluate('''()=>{
              Object.defineProperty(crypto,'subtle',{value:{digest:async(_,bytes)=>new Uint8Array(await fixtureDigest([...bytes])).buffer}});
              for(const p of fixturePanes){p.host.capabilities={file_transfer:true};const screen=document.createElement('div');screen.className='terminal-screen';
                p.term={textarea:document.createElement('textarea')};screen.append(p.term.textarea);document.querySelector('#stage').append(screen);}
              const read=File.prototype.arrayBuffer;
              File.prototype.arrayBuffer=async function(){if(this.name==='A.pdf'){window.readStarted=true;await new Promise(r=>window.releaseRead=r);}return read.call(this);};
              window.pasteFile=(name,text)=>{const d=new DataTransfer();d.items.add(new File([text],name));
                const e=new ClipboardEvent('paste',{clipboardData:d,bubbles:true,cancelable:true});smaWorkspace.activePane.term.textarea.dispatchEvent(e);return e.defaultPrevented;};
            }''')
            await page.evaluate((STATIC/'clipboard-files.js').read_text(encoding='utf-8'))
            assert await page.evaluate('pasteFile("A.pdf","owned-A")')
            await page.wait_for_function('window.readStarted||!SuperMonoFilePaste.busy'); assert await page.evaluate('!!window.readStarted'), await page.locator('#file-transfer-progress').inner_text()
            await page.evaluate('fixtureSelect(1);pasteFile("B.docx","owned-B");pasteFile("C.png","owned-C");')
            await page.evaluate('releaseRead()')
            await page.wait_for_function('!SuperMonoFilePaste.busy')
            savedA = [p for p in drop_dir(folder).glob('*') if p.is_file()]
            savedB = [p for p in drop_dir(folder,'B').glob('*') if p.is_file()]
            assert len(savedA) == 1 and savedA[0].read_bytes() == b'owned-A'
            assert sorted(p.read_bytes() for p in savedB) == [b'owned-B', b'owned-C']
            assert not errors
            assert all(not p.pty_written for p in parents)


@pytest.mark.parametrize('boundary', ['success', 'cancel', 'stale'])
def test_native_snapshots_are_immediate_fifo_and_released(tmp_path, boundary):
    run(lambda pw, folder: check_native(pw, folder, boundary), tmp_path)


async def check_native(playwright, folder, boundary):
    from supermonoagent.windows_clipboard import ClipboardFiles
    from test_file_companion import drop_dir, environment
    sources = []
    for name in ['first.pdf', 'second.docx', 'third.png']:
        path = folder/name
        path.write_bytes(name.encode())
        sources.append(path)
    snapshots = []
    def getter():
        path = sources[len(snapshots)]
        snapshots.append(path.name)
        return [path]
    clip = ClipboardFiles(getter=getter)
    try:
        async with environment(folder) as (_broker, url, parents, _companions):
            async with browser_page(playwright, url) as (page, errors, _sockets):
                await page.expose_function('fixtureSnapshot', clip.snapshot)
                await page.expose_function('fixtureRead', clip.read)
                await page.expose_function('fixtureRelease', clip.release)
                await page.evaluate('''()=>{
                  window.pywebview={api:{clipboard_files:fixtureSnapshot,clipboard_release:fixtureRelease,
                    clipboard_chunk:async(id,offset)=>{if(!window.firstRead){window.firstRead=true;await new Promise(r=>window.releaseRead=r);}return fixtureRead(id,offset);},
                    clipboard_has_files:()=>{throw Error('Must not race asynchronous clipboard probe');}}};
                  window.supermonoClipboardHasFiles=true;
                  for(const p of fixturePanes){const screen=document.createElement('div');screen.className='terminal-screen';p.term={textarea:document.createElement('textarea')};screen.append(p.term.textarea);document.querySelector('#stage').append(screen);}
                  window.pasteNative=()=>smaWorkspace.activePane.term.textarea.dispatchEvent(new ClipboardEvent('paste',{clipboardData:new DataTransfer(),bubbles:true,cancelable:true}));
                }''')
                await page.evaluate((STATIC/'clipboard-files.js').read_text(encoding='utf-8'))
                await page.evaluate('pasteNative()')
                await page.wait_for_function('window.firstRead')
                await page.evaluate('fixtureSelect(1);pasteNative();pasteNative();')
                async with asyncio.timeout(3):
                    while len(snapshots) != 3:
                        await asyncio.sleep(.01)
                assert snapshots == [p.name for p in sources]
                if boundary == 'cancel':
                    await page.locator('#file-transfer-progress>button').click()
                elif boundary == 'stale':
                    await page.evaluate('fixtureHosts[1].config.token="changed-fixture-token"')
                await page.evaluate('releaseRead()')
                await page.wait_for_function('!SuperMonoFilePaste.busy')
                filesA = [p for p in drop_dir(folder).glob('*') if p.is_file()]
                filesB = [p for p in drop_dir(folder,'B').glob('*') if p.is_file()]
                if boundary == 'cancel':
                    assert not filesA and not filesB
                else:
                    assert len(filesA) == 1 and filesA[0].read_bytes() == b'first.pdf'
                    if boundary == 'success':
                        assert sorted(p.read_bytes() for p in filesB) == [b'second.docx', b'third.png']
                    else:
                        assert not filesB
                        assert await page.evaluate('SuperMonoFilePaste.queued') == 2
                        await page.locator('#file-transfer-queue button').filter(has_text='移除').first.click()
                        await page.locator('#file-transfer-queue button').filter(has_text='移除').first.click()
                async with asyncio.timeout(3):
                    while clip._items:
                        await asyncio.sleep(.01)
                assert not errors
                assert all(not parent.pty_written for parent in parents)
    finally:
        clip.close()


def test_queue_bound_cancel_and_plain_text_not_intercepted(tmp_path):
    async def exercise(playwright, folder):
        from test_file_companion import drop_dir, environment
        async with environment(folder) as (_broker, url, parents, _companions):
            async with browser_page(playwright, url) as (page, errors, _sockets):
                await page.evaluate('''()=>{
                  const screen=document.createElement('div');screen.className='terminal-screen';
                  const field=document.createElement('textarea');screen.append(field);document.querySelector('#stage').append(screen);fixturePanes[0].term={textarea:field};
                  window.readCount=0;File.prototype.arrayBuffer=async function(){readCount++;await new Promise(r=>window.releaseRead=r);return new ArrayBuffer(1);};
                  window.pasteNext=()=>{const d=new DataTransfer();d.items.add(new File(['x'],'item.pdf'));field.dispatchEvent(new ClipboardEvent('paste',{clipboardData:d,bubbles:true,cancelable:true}));};
                }''')
                await page.evaluate((STATIC/'clipboard-files.js').read_text(encoding='utf-8'))
                await page.evaluate('pasteNext()')
                await page.wait_for_function('readCount===1')
                await page.evaluate('for(let i=0;i<32;i++)pasteNext()')
                assert await page.evaluate('SuperMonoFilePaste.queued') == 32
                assert '队列已满' in await page.locator('#file-transfer-progress').inner_text()
                assert await page.evaluate('readCount') == 1
                assert await page.evaluate('''()=>{const d=new DataTransfer();d.setData('text/plain','ordinary text');
                  const e=new ClipboardEvent('paste',{clipboardData:d,bubbles:true,cancelable:true});smaWorkspace.activePane.term.textarea.dispatchEvent(e);return e.defaultPrevented;}''') is False
                await page.locator('#file-transfer-progress>button').click()
                await page.evaluate('releaseRead()')
                await page.wait_for_function('!SuperMonoFilePaste.busy&&SuperMonoFilePaste.queued===0')
                assert not drop_dir(folder).exists()
                assert not errors
                assert all(not p.pty_written for p in parents)
    run(exercise, tmp_path)


@pytest.mark.parametrize('mode', ['pending', 'project_required', 'finish-ack-lost'])
def test_queued_failures_are_explicit_and_never_auto_retried(tmp_path, mode):
    async def exercise(playwright, folder):
        from test_file_companion import drop_dir, environment
        async with environment(folder) as (_broker, url, parents, _companions):
            async with browser_page(playwright, url) as (page, errors, _sockets):
                await page.expose_function('fixtureDigest', lambda data: list(hashlib.sha256(bytes(data)).digest()))
                await page.evaluate('''mode=>{
                  Object.defineProperty(crypto,'subtle',{value:{digest:async(_,bytes)=>new Uint8Array(await fixtureDigest([...bytes])).buffer}});
                  const screen=document.createElement('div');screen.className='terminal-screen';const field=document.createElement('textarea');screen.append(field);document.querySelector('#stage').append(screen);fixturePanes[0].term={textarea:field};
                  window.beginCount=0;const F=SuperMonoFileWorkspaces,original=F.request;
                  F.request=async(t,action,data)=>{if(action==='upload.begin')beginCount++;
                    if(action==='upload.begin'&&mode==='project_required')throw Object.assign(Error('project_required'),{code:'project_required'});
                    if(action==='file.status'&&mode==='pending')throw Error('request_timeout');
                    const result=await original(t,action,data);
                    if(action==='upload.finish'&&mode==='finish-ack-lost'){window.committedFileId=result.file_id;throw Error('request_timeout');}
                    return result;};
                  window.pasteOwned=()=>{const d=new DataTransfer();d.items.add(new File(['owned'],'owned.pdf'));field.dispatchEvent(new ClipboardEvent('paste',{clipboardData:d,bubbles:true,cancelable:true}));};
                }''', mode)
                await page.evaluate((STATIC/'clipboard-files.js').read_text(encoding='utf-8'))
                await page.evaluate('pasteOwned()')
                await page.wait_for_function('!SuperMonoFilePaste.busy')
                assert await page.evaluate('SuperMonoFilePaste.queued') == 1
                assert await page.evaluate('beginCount') == 1
                if mode in ('pending', 'finish-ack-lost'):
                    expected = '提交结果待确认' if mode == 'finish-ack-lost' else '中继待确认'
                    assert expected in await page.locator('#file-transfer-queue').inner_text()
                    assert await page.locator('#file-transfer-queue button').filter(has_text='重试').count() == 0
                    if mode == 'finish-ack-lost':
                        async with asyncio.timeout(3):
                            while len([p for p in drop_dir(folder).glob('*') if p.is_file()]) != 1:
                                await asyncio.sleep(.02)
                        assert await page.evaluate('beginCount') == 1
                        assert await page.locator('#file-transfer-queue button').count() == 1
                else:
                    assert '项目目录' in await page.locator('#file-transfer-progress').inner_text()
                    assert await page.locator('#file-transfer-queue button').filter(has_text='重试').count() == 1
                    assert not (parents[0].project/'tmp').exists()
                await page.locator('#file-transfer-queue button').filter(has_text='移除').click()
                assert await page.evaluate('SuperMonoFilePaste.queued') == 0
                assert not errors
                assert all(not p.pty_written for p in parents)
    run(exercise, tmp_path)


def test_frontend_declared_byte_budget_releases_rejected_native_snapshot(tmp_path):
    async def exercise(playwright, folder):
        from test_file_companion import environment
        async with environment(folder) as (_broker, url, parents, _companions):
            async with browser_page(playwright, url) as (page, errors, _sockets):
                await page.evaluate('''()=>{
                  window.released=[];window.nativeReads=0;
                  window.pywebview={api:{clipboard_files:async()=>({items:[{id:'owned-big-id',name:'big.pdf',size:512*1024*1024+1,sha256:'a'.repeat(64)}]}),
                    clipboard_chunk:async()=>{nativeReads++;throw Error('Must not read oversized source');},clipboard_release:async id=>released.push(id)}};
                }''')
                await page.evaluate((STATIC/'clipboard-files.js').read_text(encoding='utf-8'))
                await page.evaluate('SuperMonoFilePaste.paste()')
                await page.wait_for_function('!SuperMonoFilePaste.busy&&SuperMonoFilePaste.queued===0')
                assert await page.evaluate('released') == ['owned-big-id']
                assert await page.evaluate('nativeReads') == 0
                assert '512 MiB' in await page.locator('#file-transfer-progress').inner_text()
                assert not (parents[0].project/'tmp').exists()
                assert not errors
                assert all(not p.pty_written for p in parents)
    run(exercise, tmp_path)
