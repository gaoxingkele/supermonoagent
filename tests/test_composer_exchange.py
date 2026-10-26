"""Actual editor upload through staging and HTTP to the captured owned host."""
from pathlib import Path
import hashlib

from test_file_workspace_ui import browser_page, run, STATIC


def test_composer_multiple_attachments_reach_project_tmp_without_terminal_input(tmp_path):
    async def exercise(playwright, folder):
        from test_file_companion import environment
        async with environment(folder) as (_broker, url, parents, _companions):
            async with browser_page(playwright, url, document_url=url.replace('ws:', 'http:').replace('/file/ws','/')) as (page, errors, _sockets):
                await page.evaluate('''()=>{const h=document.querySelector('header');const actions=document.createElement('div');actions.className='toolbar-actions';h.append(actions);window.smaWorkspace.metrics={transmittedBytes:0};}''')
                await page.evaluate((STATIC/'terminal-client.js').read_text('utf-8'))
                await page.evaluate((STATIC/'clipboard-files.js').read_text('utf-8'))
                await page.evaluate('''()=>{const original=SuperMonoFilePaste.upload;SuperMonoFilePaste.upload=async(...args)=>{try{return await original(...args);}catch(error){window.fixtureUploadError={code:error.code,message:error.message};throw error;}};}''')
                await page.evaluate((STATIC/'terminal-composer.js').read_text('utf-8'))
                await page.locator('#terminal-composer-open').click()
                await page.locator('#terminal-composer-text').fill('owned draft remains local')
                inputs = page.locator('#terminal-composer input[type=file]')
                data_a=b'\x89PNG\r\nowned image attachment'
                data_b=b'%PDF-1.4 owned document attachment'
                await inputs.set_input_files([
                    {'name':'owned.png','mimeType':'image/png','buffer':data_a},
                    {'name':'owned.pdf','mimeType':'application/pdf','buffer':data_b},
                ])
                await page.locator('#terminal-composer-upload').click()
                await page.wait_for_function('!SuperMonoTerminalComposer.busy', timeout=20000)
                files=[path for path in (parents[0].project/'tmp').glob('*') if path.is_file()]
                assert len(files)==2, await page.evaluate('window.fixtureUploadError||document.getElementById("terminal-composer").innerText')
                for name,data in [('owned.png',data_a),('owned.pdf',data_b)]:
                    saved=next(path for path in files if path.name.endswith('-'+name))
                    assert saved.read_bytes()==data
                    assert len(saved.name.split('-')[0])==8
                assert not (parents[1].project/'tmp').exists()
                assert await page.evaluate('fixtureInputs')==[]
                assert await page.locator('#terminal-composer-text').input_value()=='owned draft remains local'
                assert not errors,errors
    run(exercise,tmp_path)
