"""Actual editor upload through staging and HTTP to the captured owned host."""
from pathlib import Path
import hashlib

from test_file_workspace_ui import browser_page, run, STATIC


def test_composer_multiple_attachments_reach_central_drop_without_terminal_input(tmp_path):
    async def exercise(playwright, folder):
        from test_file_companion import drop_dir, environment
        async with environment(folder) as (_broker, url, parents, _companions):
            async with browser_page(playwright, url, document_url=url.replace('ws:', 'http:').replace('/file/ws','/')) as (page, errors, _sockets):
                await page.evaluate('''()=>{const h=document.querySelector('header');const actions=document.createElement('div');actions.className='toolbar-actions';h.append(actions);window.smaWorkspace.metrics={transmittedBytes:0};}''')
                await page.evaluate((STATIC/'terminal-client.js').read_text('utf-8'))
                await page.evaluate((STATIC/'file-upload.js').read_text('utf-8'))
                await page.evaluate((STATIC/'clipboard-files.js').read_text('utf-8'))
                await page.evaluate('''()=>{const original=SuperMonoFilePaste.upload;SuperMonoFilePaste.upload=async(...args)=>{try{return await original(...args);}catch(error){window.fixtureUploadError={code:error.code,message:error.message};throw error;}};}''')
                await page.evaluate((STATIC/'terminal-composer.js').read_text('utf-8'))
                await page.locator('#terminal-composer-open').click()
                await page.locator('#terminal-composer-text').fill('owned draft remains local')
                data_a=b'\x89PNG\r\nowned image attachment'
                data_b=b'%PDF-1.4 owned document attachment'
                async with page.expect_file_chooser() as image_chooser:
                    await page.locator('#terminal-composer-choose-images').click()
                await (await image_chooser.value).set_files({'name':'owned.png','mimeType':'image/png','buffer':data_a})
                async with page.expect_file_chooser() as file_chooser:
                    await page.locator('#terminal-composer-choose-files').click()
                await (await file_chooser.value).set_files({'name':'owned.pdf','mimeType':'application/pdf','buffer':data_b})
                await page.wait_for_function("document.querySelectorAll('#terminal-composer-receipts li').length===2", timeout=20000)
                await page.wait_for_function('!SuperMonoTerminalComposer.busy', timeout=20000)
                files=[path for path in drop_dir(folder).glob('*') if path.is_file()]
                assert len(files)==2, await page.evaluate('window.fixtureUploadError||document.getElementById("terminal-composer").innerText')
                for name,data in [('owned.png',data_a),('owned.pdf',data_b)]:
                    saved=next(path for path in files if path.name.endswith('-'+name))
                    assert saved.read_bytes()==data
                    assert len(saved.name.split('-')[0])==8
                assert not (folder/'drop-B').exists()
                assert await page.evaluate('fixtureInputs')==[]
                draft=await page.locator('#terminal-composer-text').input_value()
                assert draft.startswith('owned draft remains local\n附件：')
                assert all(saved.name in draft for saved in files)
                assert not errors,errors
    run(exercise,tmp_path)


def test_composer_ctrl_v_image_upload_and_plain_text(tmp_path):
    async def exercise(playwright, folder):
        from test_file_companion import drop_dir, environment
        async with environment(folder) as (_broker, url, parents, _companions):
            async with browser_page(playwright, url, document_url=url.replace('ws:', 'http:').replace('/file/ws','/')) as (page, errors, _sockets):
                await page.evaluate('''()=>{const h=document.querySelector('header');const actions=document.createElement('div');actions.className='toolbar-actions';h.append(actions);window.smaWorkspace.metrics={transmittedBytes:0};}''')
                await page.evaluate((STATIC/'terminal-client.js').read_text('utf-8'))
                await page.evaluate((STATIC/'file-upload.js').read_text('utf-8'))
                await page.evaluate((STATIC/'clipboard-files.js').read_text('utf-8'))
                await page.evaluate((STATIC/'terminal-composer.js').read_text('utf-8'))
                await page.locator('#terminal-composer-open').click()
                data=b'\x89PNG\r\nowned image pasted with Ctrl V'
                prevented=await page.evaluate('''data=>{
                  const transfer=new DataTransfer();transfer.items.add(new File([new Uint8Array(data)],'pasted.png',{type:'image/png'}));
                  const event=new ClipboardEvent('paste',{clipboardData:transfer,bubbles:true,cancelable:true});
                  document.getElementById('terminal-composer-text').dispatchEvent(event);return event.defaultPrevented;
                }''',data)
                assert prevented
                await page.wait_for_function('!SuperMonoTerminalComposer.busy',timeout=20000)
                saved=[path for path in drop_dir(folder).glob('*pasted.png') if path.is_file()]
                assert len(saved)==1 and saved[0].read_bytes()==data
                assert not (folder/'drop-B').exists()
                plain=await page.evaluate('''()=>{
                  const transfer=new DataTransfer();transfer.setData('text/plain','ordinary pasted text');
                  const event=new ClipboardEvent('paste',{clipboardData:transfer,bubbles:true,cancelable:true});
                  const field=document.getElementById('terminal-composer-text');field.dispatchEvent(event);
                  if(!event.defaultPrevented)field.setRangeText(transfer.getData('text/plain'),field.selectionStart,field.selectionEnd,'end');
                  return event.defaultPrevented;
                }''')
                assert not plain
                assert 'ordinary pasted text' in await page.locator('#terminal-composer-text').input_value()
                assert await page.evaluate('fixtureInputs')==[]
                assert all(not parent.pty_written for parent in parents)
                assert not errors,errors
    run(exercise,tmp_path)
