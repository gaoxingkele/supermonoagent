"""Owned workspace browsers/PTYs only; never read or modify the OS clipboard."""
from pathlib import Path
import pytest

from test_workspace_profile_rooms import fixture, run, server_barrier, eventually, add_profile, wait_room


SCRIPT = Path(__file__).resolve().parents[1] / 'src/supermonoagent/static/terminal-composer.js'


def test_editor_keeps_draft_and_inserts_exact_text_once_without_enter():
    run(check_editor)


async def check_editor(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons,
                                      _ops, _windows, sessions):
        page = await browser.new_page(viewport={'width': 1280, 'height': 850})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.goto(url)
        await page.wait_for_function('smaWorkspace?.connected&&smaWorkspace.activePane?.ready')
        await page.evaluate(SCRIPT.read_text(encoding='utf-8'))
        assert await page.locator('main>footer>#terminal-composer-open').count() == 1
        assert await page.locator('.titlebar #terminal-composer-open').count() == 0
        await page.locator('#terminal-composer-open').click()
        await page.locator('#terminal-composer-text').fill('owned exact draft')
        await page.locator('#terminal-composer-close').click()
        await page.locator('#terminal-composer-open').click()
        assert await page.locator('#terminal-composer-text').input_value() == 'owned exact draft'
        selected = await page.evaluate('smaWorkspace.activePane.raw')
        receiver = next(session for session in sessions[0] if session.id == selected)
        await page.locator('#terminal-composer-insert').click()
        await server_barrier(page, 'local')
        await eventually(lambda: bytes(receiver.pty.written) == b'owned exact draft')
        assert await page.locator('#terminal-composer-insert').is_disabled()
        assert await page.locator('#terminal-composer-text').input_value() == 'owned exact draft'
        await page.locator('#terminal-composer-insert').evaluate('button=>button.click()')
        await server_barrier(page, 'local')
        assert bytes(receiver.pty.written) == b'owned exact draft'
        assert b'\r' not in receiver.pty.written and b'\n' not in receiver.pty.written
        assert not errors


async def prepared(browser, url, endpoint=None, sessions=None):
    page = await browser.new_page(viewport={'width': 1280, 'height': 850})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    await page.goto(url)
    await page.wait_for_function('smaWorkspace?.connected&&smaWorkspace.activePane?.ready')
    if endpoint:
        await add_profile(page, endpoint, 'profile-owner-1')
        await wait_room(page, 1)
        await page.locator('#relay-profiles-close').click()
        await page.evaluate('sid=>smaWorkspace.openHostSession("local",sid,false)', sessions[0][0].id)
    await page.evaluate('''()=>{
      window.composerCalls=[];window.composerReadCount=0;window.composerMode='success';
      const nativeRead=File.prototype.arrayBuffer;
      File.prototype.arrayBuffer=function(){composerReadCount++;return nativeRead.call(this);};
      const capture=()=>{const p=smaWorkspace.activePane;return {pane:p,key:p.sid,host:p.host,ws:p.host.ws,
        generation:p.host.generation,session:p.raw,token:p.host.config.token,room:p.host.config.code};};
      const valid=t=>smaWorkspace.panes.get(t.key)===t.pane&&t.host.ws===t.ws&&t.host.generation===t.generation
        &&t.host.connected&&t.pane.raw===t.session&&t.host.config.token===t.token&&t.host.config.code===t.room;
      window.SuperMonoFilePaste={captureTarget:capture,isCurrent:valid,upload:async(files,options)=>{
        composerCalls.push({host:options.target.host.id,session:options.target.session,names:files.map(f=>f.name)});
        window.composerUploadSignal=options.signal;window.composerEntered=true;
        if(composerMode==='gated'||composerMode==='late')await new Promise((yes,no)=>{
          window.composerRelease=yes;
          if(composerMode!=='late')options.signal.addEventListener('abort',()=>no(Error('fixture cancelled')),{once:true});
        });
        if(composerMode==='failure')throw Error('Target helper unavailable');
        if(composerMode==='unknown-commit')throw Object.assign(Error('request_timeout'),{unknownCommit:true,pendingUploadId:'d'.repeat(48)});
        if(composerMode==='missing-profile')throw Error('文件交换助手尚未安装，请更新完整客户端。');
        if(composerMode==='queued'||composerMode==='queued-cancel'){
          const file=files[0];options.onProgress?.({phase:'receiving',name:file.name,index:0,total:files.length,loaded:file.size,size:file.size,file_id:'c'.repeat(48)});
          if(composerMode==='queued'){const error=Error('No destination receipt');error.pendingFileId='c'.repeat(48);throw error;}
          window.composerCommitted=true;await new Promise(yes=>{window.composerConfirm=yes;});
          const saved={name:file.name,path:'C:/owned-'+options.target.host.id+'/tmp/'+file.name,size:file.size,sha256:'a'.repeat(64)};
          options.onProgress?.({phase:'saved',name:file.name,index:0,total:files.length,loaded:file.size,size:file.size,receipt:saved});
          return [saved];
        }
        const receipts=[];
        for(const [index,file] of files.entries()){
          if(options.signal.aborted&&composerMode!=='late'){const e=Error('cancelled');e.receipts=receipts;throw e;}
          options.onProgress?.({phase:'preparing',name:file.name,index,total:files.length,loaded:0,size:file.size});
          options.onProgress?.({phase:'uploading',name:file.name,index,total:files.length,loaded:file.size,size:file.size});
          if(composerMode==='partial'&&index===1){const e=Error('second file failed');e.receipts=receipts;throw e;}
          const receipt={name:file.name,path:'C:/owned-'+options.target.host.id+'/tmp/'+file.name,size:file.size,sha256:'a'.repeat(64)};
          receipts.push(receipt);
          if(composerMode!=='late')options.onProgress?.({phase:'saved',name:file.name,index,total:files.length,loaded:file.size,size:file.size,receipt});
        }return receipts;
      }};
    }''')
    await page.evaluate(SCRIPT.read_text(encoding='utf-8'))
    return page, errors


def test_multiline_standard_paste_event_clipboard_button_failure_and_dialog_geometry():
    run(check_multiline)


async def check_multiline(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons,
                                      _ops, _windows, sessions):
        page, errors = await prepared(browser, url)
        await page.locator('#terminal-composer-open').click()
        await page.locator('#terminal-composer-text').fill('first line\nsecond line')
        result = await page.evaluate(r'''()=>{
          const field=document.querySelector('#terminal-composer-text');field.focus();field.setSelectionRange(field.value.length,field.value.length);
          const key=new KeyboardEvent('keydown',{key:'v',code:'KeyV',ctrlKey:true,bubbles:true,cancelable:true});field.dispatchEvent(key);
          const data=new DataTransfer();data.setData('text/plain','\nfixture pasted text');
          const event=new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true});field.dispatchEvent(event);
          // Synthetic events don't run the browser's privileged default paste.
          // Apply that default with fixture-only data, never the OS clipboard.
          if(!event.defaultPrevented){field.setRangeText(data.getData('text/plain'),field.selectionStart,field.selectionEnd,'end');field.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertFromPaste'}));}
          return {key:key.defaultPrevented,paste:event.defaultPrevented,value:field.value};
        }''')
        assert result == {'key': False, 'paste': False, 'value': 'first line\nsecond line\nfixture pasted text'}
        await page.evaluate('''()=>Object.defineProperty(navigator,'clipboard',{configurable:true,value:{readText:async()=>{throw Error('fixture permission denied');}}})''')
        await page.locator('#terminal-composer-paste').click()
        await page.wait_for_function('document.querySelector("#terminal-composer-state").textContent.includes("Ctrl+V")')
        assert await page.locator('#terminal-composer-text').input_value() == result['value']
        await page.evaluate('''()=>{
          navigator.clipboard.readText=async()=> ' clipboard fixture';
          const target=smaWorkspace.activePane,original=target.term.paste.bind(target.term);window.composerPastes=[];
          target.term.paste=text=>{composerPastes.push(text);return original(text);};
        }''')
        await page.locator('#terminal-composer-paste').click()
        await page.wait_for_function('document.querySelector("#terminal-composer-text").value.endsWith(" clipboard fixture")')
        await page.locator('#terminal-composer-text').press('Enter')
        expected = result['value'] + ' clipboard fixture\n'
        assert await page.locator('#terminal-composer-text').input_value() == expected
        assert all(not session.pty.written for owned in sessions for session in owned)
        for width in (390, 760, 1280):
            await page.set_viewport_size({'width': width, 'height': 700})
            assert await page.locator('#terminal-composer').evaluate('''el=>{
              const r=el.getBoundingClientRect();return r.left>=0&&r.top>=0&&r.right<=innerWidth+1&&r.bottom<=innerHeight+1&&el.scrollWidth<=el.clientWidth+1;
            }''')
            assert await page.locator('#terminal-composer-open').evaluate('el=>{const r=el.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth+1;}')
        await page.locator('#terminal-composer-insert').click()
        assert await page.evaluate('composerPastes') == [expected]
        assert not errors


def test_multiple_attachment_progress_partial_failure_receipts_and_explicit_path_draft():
    run(check_attachments)


async def check_attachments(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons,
                                      _ops, _windows, sessions):
        page, errors = await prepared(browser, url)
        await page.locator('#terminal-composer-open').click()
        await page.locator('#terminal-composer-text').fill('keep my prompt')
        await page.locator('#terminal-composer-files-input').set_input_files([
            {'name': 'photo.png', 'mimeType': 'image/png', 'buffer': b'owned png'},
            {'name': 'report.docx', 'mimeType': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'buffer': b'owned word'}])
        assert await page.locator('#terminal-composer-files li').count() == 2
        await page.evaluate('composerMode="partial"')
        await page.locator('#terminal-composer-upload').click()
        await page.wait_for_function('!SuperMonoTerminalComposer.busy')
        assert await page.locator('#terminal-composer-receipts li').count() == 1
        assert await page.locator('#terminal-composer-files li').count() == 1
        assert '已保存 1 项' in await page.locator('#terminal-composer-state').inner_text()
        assert await page.locator('#terminal-composer-text').input_value() == 'keep my prompt'
        await page.locator('#terminal-composer-close').click()
        await page.locator('#terminal-composer-open').click()
        assert await page.locator('#terminal-composer-receipts li').count() == 1
        await page.evaluate('composerMode="success"')
        await page.locator('#terminal-composer-upload').click()
        await page.wait_for_function('!SuperMonoTerminalComposer.busy')
        assert await page.locator('#terminal-composer-receipts li').count() == 2
        assert await page.locator('#terminal-composer-files li').count() == 0
        assert await page.evaluate('composerCalls.map(call=>call.names.length)') == [2, 1]
        assert await page.evaluate('composerReadCount') == 0
        assert all(not session.pty.written for owned in sessions for session in owned)
        await page.locator('#terminal-composer-receipts button').first.click()
        draft = await page.locator('#terminal-composer-text').input_value()
        assert draft.startswith('keep my prompt\nC:/owned-') and 'photo.png' in draft
        assert all(not session.pty.written for owned in sessions for session in owned)
        assert '尚未插入' in await page.locator('#terminal-composer-state').inner_text()
        assert not errors


def test_started_a_upload_survives_select_b_but_insert_requires_active_target():
    run(check_target_switch)


async def check_target_switch(playwright):
    async with fixture(playwright) as (browser, url, endpoint, _trap, _messages, _daemons,
                                      _ops, _windows, sessions):
        page, errors = await prepared(browser, url, endpoint, sessions)
        await page.locator('#terminal-composer-open').click()
        await page.locator('#terminal-composer-text').fill('A draft stays A')
        await page.locator('#terminal-composer-files-input').set_input_files({'name': 'A.pdf', 'mimeType': 'application/pdf', 'buffer': b'owned A pdf'})
        await page.evaluate('composerMode="gated"')
        await page.locator('#terminal-composer-upload').click()
        await page.wait_for_function('window.composerEntered===true')
        await page.evaluate('''sid=>{
          const remote=[...smaWorkspace.connections.values()].find(host=>host.config.code==='profile-room-1');
          smaWorkspace.openHostSession(remote.id,sid,false);
        }''', sessions[1][0].id)
        await page.wait_for_function('document.querySelector("#terminal-composer-insert").disabled')
        await page.locator('#terminal-composer-insert').evaluate('button=>{button.disabled=false;button.click();}')
        assert not await page.evaluate('composerUploadSignal.aborted')
        await page.locator('#terminal-composer-close').click()
        await page.locator('#terminal-composer-open').click()
        assert await page.locator('#terminal-composer-text').input_value() == ''
        await page.locator('#terminal-composer-text').fill('B separate draft')
        await page.evaluate('composerRelease()')
        await page.wait_for_function('!SuperMonoTerminalComposer.busy')
        assert await page.locator('#terminal-composer-text').input_value() == 'B separate draft'
        assert await page.locator('#terminal-composer-receipts li').count() == 0
        assert await page.evaluate('composerCalls[0].host') == 'local'
        await page.locator('#terminal-composer-close').click()
        await page.evaluate('sid=>smaWorkspace.openHostSession("local",sid,false)', sessions[0][0].id)
        await page.locator('#terminal-composer-open').click()
        assert await page.locator('#terminal-composer-text').input_value() == 'A draft stays A'
        assert await page.locator('#terminal-composer-receipts li').count() == 1
        assert all(not session.pty.written for owned in sessions for session in owned)
        assert not errors


def test_cancel_and_generation_failure_keep_pending_files_and_text():
    run(check_cancel)


async def check_cancel(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons,
                                      _ops, _windows, sessions):
        page, errors = await prepared(browser, url)
        await page.locator('#terminal-composer-open').click()
        await page.locator('#terminal-composer-text').fill('preserved on cancellation')
        await page.locator('#terminal-composer-files-input').set_input_files({'name': 'cancel.pdf', 'mimeType': 'application/pdf', 'buffer': b'owned cancel'})
        await page.evaluate('composerMode="gated"')
        await page.locator('#terminal-composer-upload').click()
        await page.wait_for_function('window.composerEntered===true')
        await page.locator('#terminal-composer-cancel').click()
        await page.wait_for_function('!SuperMonoTerminalComposer.busy')
        assert await page.locator('#terminal-composer-files li').count() == 1
        assert await page.locator('#terminal-composer-text').input_value() == 'preserved on cancellation'
        await page.evaluate('composerMode="late";composerEntered=false')
        await page.locator('#terminal-composer-upload').click()
        await page.wait_for_function('composerEntered')
        await page.evaluate('smaWorkspace.activePane.host.generation++')
        await page.wait_for_function('composerUploadSignal.aborted')
        await page.evaluate('composerRelease()')
        await page.wait_for_function('!SuperMonoTerminalComposer.busy')
        assert await page.locator('#terminal-composer-receipts li').count() == 0
        assert await page.locator('#terminal-composer-insert').is_disabled()
        assert await page.locator('#terminal-composer-text').input_value() == 'preserved on cancellation'
        assert all(not session.pty.written for owned in sessions for session in owned)
        assert not errors


def test_missing_upload_api_is_explicit_and_attachment_can_be_removed():
    run(check_missing_api)


async def check_missing_api(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons,
                                      _ops, _windows, sessions):
        page, errors = await prepared(browser, url)
        await page.evaluate('SuperMonoFilePaste={paste(){}}')
        await page.locator('#terminal-composer-open').click()
        await page.locator('#terminal-composer-text').fill('fallback text remains editable')
        await page.locator('#terminal-composer-files-input').set_input_files({'name': 'pending.pdf', 'mimeType': 'application/pdf', 'buffer': b'owned'})
        assert await page.locator('#terminal-composer-upload').is_disabled()
        assert '缺少' in await page.locator('#terminal-composer-capability').inner_text()
        assert not await page.locator('#terminal-composer-insert').is_disabled()
        await page.locator('#terminal-composer-files button').click()
        assert await page.locator('#terminal-composer-files li').count() == 0
        assert all(not session.pty.written for owned in sessions for session in owned)
        assert not errors


def test_file_selection_limits_are_metadata_only_and_keep_existing_selection():
    run(check_selection_limits)


async def check_selection_limits(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons,
                                      _ops, _windows, sessions):
        page, errors = await prepared(browser, url)
        await page.locator('#terminal-composer-open').click()
        await page.evaluate('''()=>{
          window.chooseComposerFixture=files=>{const picker=document.querySelector('#terminal-composer-files-input');
            Object.defineProperty(picker,'files',{configurable:true,value:files});picker.dispatchEvent(new Event('change'));};
          chooseComposerFixture(Array.from({length:33},(_,i)=>({name:'fixture-'+i+'.pdf',size:1,webkitRelativePath:''})));
        }''')
        assert await page.locator('#terminal-composer-files li').count() == 0
        assert '32' in await page.locator('#terminal-composer-state').inner_text()
        await page.evaluate('chooseComposerFixture([{name:"large.pdf",size:64*1024*1024+1,webkitRelativePath:""}])')
        assert await page.locator('#terminal-composer-files li').count() == 0
        assert '64 MiB' in await page.locator('#terminal-composer-state').inner_text()
        await page.evaluate('chooseComposerFixture([{name:"boundary.pdf",size:64*1024*1024,webkitRelativePath:""}])')
        assert await page.locator('#terminal-composer-files li').count() == 1
        await page.evaluate('chooseComposerFixture(Array.from({length:31},(_,i)=>({name:"small-"+i+".png",size:1,webkitRelativePath:""})))')
        assert await page.locator('#terminal-composer-files li').count() == 32
        await page.evaluate('chooseComposerFixture([{name:"extra.pdf",size:1,webkitRelativePath:""}])')
        assert await page.locator('#terminal-composer-files li').count() == 32
        assert await page.evaluate('composerCalls.length===0&&composerReadCount===0')
        assert all(not session.pty.written for owned in sessions for session in owned)
        assert not errors


def test_failed_insert_and_backend_failure_preserve_draft_then_reconnect_restores_it():
    run(check_failures)


async def check_failures(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons,
                                      _ops, _windows, sessions):
        page, errors = await prepared(browser, url)
        await page.locator('#terminal-composer-open').click()
        await page.locator('#terminal-composer-text').fill('retry only when explicitly requested')
        await page.evaluate('''()=>{
          window.composerPane=smaWorkspace.activePane;window.composerOriginalPaste=composerPane.term.paste;
          composerPane.term.paste=()=>{throw Error('fixture failure');};
        }''')
        await page.locator('#terminal-composer-insert').click()
        assert '草稿已保留' in await page.locator('#terminal-composer-state').inner_text()
        assert await page.locator('#terminal-composer-text').input_value() == 'retry only when explicitly requested'
        await page.evaluate('composerPane.term.paste=composerOriginalPaste;composerPane.term.options.disableStdin=true')
        await page.wait_for_function('document.querySelector("#terminal-composer-insert").disabled')
        await page.evaluate('composerPane.term.options.disableStdin=false')
        await page.locator('#terminal-composer-files-input').set_input_files({'name': 'no-helper.pdf', 'mimeType': 'application/pdf', 'buffer': b'fixture'})
        await page.evaluate('composerMode="failure"')
        await page.locator('#terminal-composer-upload').click()
        await page.wait_for_function('!SuperMonoTerminalComposer.busy')
        assert '后台能力' in await page.locator('#terminal-composer-state').inner_text()
        assert await page.locator('#terminal-composer-files li').count() == 1
        await page.evaluate('window.composerGeneration=composerPane.host.generation;composerPane.host.ws.close()')
        await page.wait_for_function('composerPane.host.connected&&composerPane.host.generation>composerGeneration&&composerPane.ready')
        await page.locator('#terminal-composer-close').click()
        await page.locator('#terminal-composer-open').click()
        assert await page.locator('#terminal-composer-text').input_value() == 'retry only when explicitly requested'
        assert await page.locator('#terminal-composer-files li').count() == 1
        assert all(not session.pty.written for owned in sessions for session in owned)
        assert not errors


def test_staged_pending_item_is_not_saved_or_retried_with_next_selection():
    run(check_pending)


async def check_pending(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons,
                                      _ops, _windows, sessions):
        page, errors = await prepared(browser, url)
        await page.locator('#terminal-composer-open').click()
        await page.locator('#terminal-composer-text').fill('keep pending draft')
        await page.locator('#terminal-composer-files-input').set_input_files({'name': 'pending.pdf', 'mimeType': 'application/pdf', 'buffer': b'owned queued'})
        await page.evaluate('composerMode="queued"')
        await page.locator('#terminal-composer-upload').click()
        await page.wait_for_function('!SuperMonoTerminalComposer.busy')
        assert await page.locator('#terminal-composer-receipts li').count() == 0
        assert '待目标确认' in await page.locator('#terminal-composer-files').inner_text()
        assert 'c' * 48 in await page.locator('#terminal-composer-files').inner_text()
        assert await page.locator('#terminal-composer-upload').is_disabled()
        assert await page.locator('#terminal-composer-files button').is_disabled()
        await page.locator('#terminal-composer-upload').evaluate('button=>{button.disabled=false;button.click();}')
        assert await page.evaluate('composerCalls.length') == 1
        await page.locator('#terminal-composer-close').click()
        await page.locator('#terminal-composer-open').click()
        assert '不会再次上传' in await page.locator('#terminal-composer-state').inner_text()
        await page.locator('#terminal-composer-files-input').set_input_files({'name': 'next.docx', 'mimeType': 'application/octet-stream', 'buffer': b'owned next'})
        await page.evaluate('composerMode="success"')
        await page.locator('#terminal-composer-upload').click()
        await page.wait_for_function('!SuperMonoTerminalComposer.busy')
        assert await page.evaluate('composerCalls.map(call=>call.names)') == [['pending.pdf'], ['next.docx']]
        assert await page.locator('#terminal-composer-receipts li').count() == 1
        assert await page.locator('#terminal-composer-files li').count() == 1
        assert await page.locator('#terminal-composer-text').input_value() == 'keep pending draft'
        assert all(not session.pty.written for owned in sessions for session in owned)
        assert not errors


def test_cancel_after_relay_commit_waits_for_receipt_and_never_calls_saved_file_cancelled():
    run(check_committed_cancel)


async def check_committed_cancel(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons,
                                      _ops, _windows, sessions):
        page, errors = await prepared(browser, url)
        await page.locator('#terminal-composer-open').click()
        await page.locator('#terminal-composer-files-input').set_input_files({'name': 'committed.png', 'mimeType': 'image/png', 'buffer': b'owned png'})
        await page.evaluate('composerMode="queued-cancel"')
        await page.locator('#terminal-composer-upload').click()
        await page.wait_for_function('window.composerCommitted===true')
        assert await page.locator('#terminal-composer-receipts li').count() == 0
        assert '等待目标保存确认' in await page.locator('#terminal-composer-state').inner_text()
        assert await page.locator('#terminal-composer-progress').evaluate('el=>el.value') < 100
        await page.locator('#terminal-composer-cancel').click()
        assert await page.evaluate('SuperMonoTerminalComposer.busy&&composerUploadSignal.aborted')
        assert '不能撤销' in await page.locator('#terminal-composer-state').inner_text()
        await page.evaluate('composerConfirm()')
        await page.wait_for_function('!SuperMonoTerminalComposer.busy')
        assert await page.locator('#terminal-composer-receipts li').count() == 1
        assert await page.locator('#terminal-composer-files li').count() == 0
        assert '已提交项保存完成' in await page.locator('#terminal-composer-state').inner_text()
        assert all(not session.pty.written for owned in sessions for session in owned)
        assert not errors


def test_missing_full_client_profile_clearly_requires_upgrade_without_fallback():
    run(check_profile_missing)


async def check_profile_missing(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons,
                                      _ops, _windows, sessions):
        page, errors = await prepared(browser, url)
        await page.locator('#terminal-composer-open').click()
        await page.locator('#terminal-composer-files-input').set_input_files({'name': 'upgrade.pdf', 'mimeType': 'application/pdf', 'buffer': b'owned pdf'})
        await page.evaluate('composerMode="missing-profile"')
        await page.locator('#terminal-composer-upload').click()
        await page.wait_for_function('!SuperMonoTerminalComposer.busy')
        assert '更新完整客户端' in await page.locator('#terminal-composer-state').inner_text()
        assert await page.locator('#terminal-composer-files li').count() == 1
        assert await page.locator('#terminal-composer-receipts li').count() == 0
        assert all(not session.pty.written for owned in sessions for session in owned)
        assert not errors


def test_lost_finish_ack_holds_attachment_without_claiming_committed_or_allowing_retry():
    async def exercise(playwright):
        async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons,
                                          _ops, _windows, sessions):
            page, errors = await prepared(browser, url)
            await page.locator('#terminal-composer-open').click()
            await page.locator('#terminal-composer-files-input').set_input_files({'name':'unknown.pdf','mimeType':'application/pdf','buffer':b'owned'})
            await page.evaluate('composerMode="unknown-commit"')
            await page.locator('#terminal-composer-upload').click()
            await page.wait_for_function('!SuperMonoTerminalComposer.busy')
            assert '提交结果待确认' in await page.locator('#terminal-composer-state').inner_text()
            assert '中继已暂存' not in await page.locator('#terminal-composer-files').inner_text()
            assert 'd'*48 in await page.locator('#terminal-composer-files').inner_text()
            assert await page.locator('#terminal-composer-upload').is_disabled()
            assert await page.locator('#terminal-composer-receipts li').count() == 0
            assert await page.locator('#terminal-composer-files button').is_disabled()
            assert await page.evaluate('composerCalls.length') == 1
            assert not errors
            assert all(not s.pty.written for owned in sessions for s in owned)
    run(exercise)


def test_public_paste_clipboard_and_add_files_use_captured_editor_draft():
    async def exercise(playwright):
        async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons,
                                          _ops, _windows, sessions):
            page, errors = await prepared(browser, url)
            await page.locator('#terminal-composer-open').click()
            await page.locator('#terminal-composer-text').fill('one OLD three')
            await page.evaluate('''()=>{
              const field=document.querySelector('#terminal-composer-text');field.setSelectionRange(4,7);
              window.nativeReads=0;window.nativeUploads=0;
              window.pywebview={api:{clipboard_has_files:async()=>false,clipboard_read_text:async()=>{nativeReads++;return 'NEW';}}};
            }''')
            assert await page.evaluate('SuperMonoTerminalComposer.pasteClipboard()') is True
            assert await page.locator('#terminal-composer-text').input_value() == 'one NEW three'
            assert await page.evaluate('nativeReads') == 1
            assert await page.evaluate('SuperMonoTerminalComposer.addFiles([new File(["pdf"],"queued.pdf")])') is True
            assert await page.locator('#terminal-composer-files li').count() == 1
            await page.evaluate('''()=>{
              pywebview.api.clipboard_has_files=async()=>true;
              SuperMonoFilePaste.uploadClipboard=async options=>{nativeUploads++;return SuperMonoFilePaste.upload([new File(['image'],'native.png')],options);};
            }''')
            assert await page.evaluate('SuperMonoTerminalComposer.pasteClipboard()') is True
            assert await page.evaluate('nativeUploads') == 1
            assert await page.locator('#terminal-composer-receipts li').count() == 1
            assert await page.locator('#terminal-composer-files li').count() == 1
            assert 'queued.pdf' in await page.locator('#terminal-composer-files').inner_text()
            assert await page.locator('#terminal-composer-text').input_value() == 'one NEW three'
            assert all(not s.pty.written for owned in sessions for s in owned)
            assert not errors
    run(exercise)


@pytest.mark.parametrize('boundary',['caret','draft','readonly','disabled','generation','token','endpoint','close','reopen','switch'])
def test_late_native_text_never_overwrites_a_changed_editor_target(boundary):
    async def exercise(playwright):
        async with fixture(playwright) as (browser, url, endpoint, _trap, _messages, _daemons,
                                          _ops, _windows, sessions):
            page, errors = await prepared(browser, url, endpoint, sessions)
            await page.locator('#terminal-composer-open').click()
            await page.locator('#terminal-composer-text').fill('original draft')
            await page.evaluate('''()=>{
              const field=document.querySelector('#terminal-composer-text');field.setSelectionRange(3,3);
              window.pywebview={api:{clipboard_has_files:async()=>false,
                clipboard_read_text:()=>new Promise(yes=>{window.finishNativeText=yes;})}};
              window.latePaste=SuperMonoTerminalComposer.pasteClipboard();
            }''')
            await page.wait_for_function('typeof finishNativeText==="function"')
            if boundary=='caret':
                await page.evaluate('document.querySelector("#terminal-composer-text").setSelectionRange(8,8)')
            elif boundary=='draft':
                await page.locator('#terminal-composer-text').fill('newer user draft')
            elif boundary in ('readonly','disabled'):
                await page.evaluate('property=>document.querySelector("#terminal-composer-text")[property]=true', 'readOnly' if boundary=='readonly' else 'disabled')
            elif boundary=='generation':
                await page.evaluate('smaWorkspace.activePane.host.generation++')
            elif boundary=='token':
                await page.evaluate('smaWorkspace.activePane.host.config.token="changed-owned-token"')
            elif boundary=='endpoint':
                await page.evaluate('smaWorkspace.activePane.host.config.relay="ws://127.0.0.1:1/ws"')
            elif boundary=='close':
                await page.evaluate('SuperMonoTerminalComposer.close()')
            elif boundary=='reopen':
                await page.evaluate('SuperMonoTerminalComposer.close();SuperMonoTerminalComposer.open()')
            else:
                await page.evaluate('sid=>{const host=[...smaWorkspace.connections.values()].find(h=>h.config.code==="profile-room-1");smaWorkspace.openHostSession(host.id,sid,false);}', sessions[1][0].id)
            assert await page.evaluate('finishNativeText("SHOULD_NOT_APPEAR");latePaste') is False
            expected='newer user draft' if boundary=='draft' else 'original draft'
            assert await page.locator('#terminal-composer-text').input_value()==expected
            assert all(not session.pty.written for owned in sessions for session in owned)
            assert not errors
    run(exercise)


def test_consecutive_editor_native_pastes_reach_captured_projects_through_real_broker(tmp_path):
    from supermonoagent.desktop_files import DesktopFiles
    from supermonoagent.windows_clipboard import ClipboardFiles
    from test_desktop_files_exchange import Window
    from test_file_companion import environment
    from test_file_workspace_ui import browser_page, STATIC
    import asyncio
    sources=[]
    for name,content in [('first.png',b'owned image'),('second.pdf',b'owned pdf')]:
        path=tmp_path/name;path.write_bytes(content);sources.append(path)
    snapshots=[]
    def getter():
        value=sources[len(snapshots)];snapshots.append(value.name);return [value]
    clip=ClipboardFiles(getter=getter)
    window=Window();bridge=DesktopFiles(window,window.url);bridge._files=clip
    async def exercise(playwright):
        async with environment(tmp_path) as (_broker,url,parents,_companions):
            async with browser_page(playwright,url) as (page,errors,_sockets):
                await page.expose_function('fixtureClipSnapshot',bridge.clipboard_files)
                await page.expose_function('fixtureClipRead',bridge.clipboard_chunk)
                await page.expose_function('fixtureClipRelease',bridge.clipboard_release)
                await page.evaluate('''()=>{
                  window.pywebview={api:{clipboard_files:fixtureClipSnapshot,clipboard_release:fixtureClipRelease,
                    clipboard_has_files:async()=>true,clipboard_chunk:async(id,offset)=>{
                      if(!window.editorReadStarted){window.editorReadStarted=true;await new Promise(yes=>window.releaseEditorRead=yes);}
                      return fixtureClipRead(id,offset);}}};
                }''')
                await page.evaluate((STATIC/'clipboard-files.js').read_text(encoding='utf-8'))
                await page.evaluate(SCRIPT.read_text(encoding='utf-8'))
                await page.locator('#terminal-composer-open').click()
                await page.locator('#terminal-composer-text').fill('A prompt stays A')
                await page.evaluate('()=>{window.pasteA=SuperMonoTerminalComposer.pasteClipboard();}')
                await page.wait_for_function('window.editorReadStarted||!SuperMonoTerminalComposer.busy')
                assert await page.evaluate('!!window.editorReadStarted'), await page.locator('#terminal-composer-state').inner_text()
                await page.evaluate('fixtureSelect(1);SuperMonoTerminalComposer.close();SuperMonoTerminalComposer.open()')
                await page.locator('#terminal-composer-text').fill('B prompt stays B')
                await page.evaluate('()=>{window.pasteB=SuperMonoTerminalComposer.pasteClipboard();}')
                async with asyncio.timeout(3):
                    while len(snapshots)<2:await asyncio.sleep(.01)
                assert snapshots==['first.png','second.pdf']
                await page.evaluate('releaseEditorRead()')
                assert await page.evaluate('Promise.all([pasteA,pasteB])')==[True,True]
                assert await page.locator('#terminal-composer-text').input_value()=='B prompt stays B'
                assert await page.locator('#terminal-composer-receipts li').count()==1
                assert 'second.pdf' in await page.locator('#terminal-composer-receipts').inner_text()
                await page.evaluate('SuperMonoTerminalComposer.close();fixtureSelect(0);SuperMonoTerminalComposer.open()')
                assert await page.locator('#terminal-composer-text').input_value()=='A prompt stays A'
                assert await page.locator('#terminal-composer-receipts li').count()==1
                assert 'first.png' in await page.locator('#terminal-composer-receipts').inner_text()
                savedA=[p for p in (parents[0].project/'tmp').glob('*') if p.is_file()]
                savedB=[p for p in (parents[1].project/'tmp').glob('*') if p.is_file()]
                assert len(savedA)==len(savedB)==1
                assert savedA[0].read_bytes()==b'owned image' and savedB[0].read_bytes()==b'owned pdf'
                assert not errors and all(not parent.pty_written for parent in parents)
                assert clip._items=={}
    try:run(exercise)
    finally:
        bridge._close()
        if bridge._retire_thread:bridge._retire_thread.join(2)
