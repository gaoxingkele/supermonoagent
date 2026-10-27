"""Folder editor selection with owned files, broker/helper and headless browser."""
from pathlib import Path

from test_file_workspace_ui import browser_page, run, STATIC


def test_folder_picker_reaches_original_project_tmp_with_hierarchy(tmp_path):
    async def exercise(playwright, folder):
        from test_file_companion import environment
        from supermonoagent.windows_clipboard import ClipboardFiles

        source = folder / 'owned-source'
        (source / 'a').mkdir(parents=True)
        (source / 'b').mkdir()
        (source / 'empty').mkdir()
        (source / 'a/same.txt').write_bytes(b'first leaf')
        (source / 'b/same.txt').write_bytes(b'second leaf')
        pool = ClipboardFiles()
        try:
            async with environment(folder) as (_broker, url, parents, _companions):
                async with browser_page(playwright, url, document_url=url.replace('ws:', 'http:').replace('/file/ws', '/')) as (page, errors, _sockets):
                    await page.expose_function('ownedChooseFolder', lambda: pool.snapshot_folder(source))
                    await page.expose_function('ownedFolderChunk', pool.read)
                    await page.expose_function('ownedFolderRelease', pool.release)
                    await page.evaluate("""()=>{
                        window.pywebview={api:{choose_folder:()=>ownedChooseFolder(),
                            clipboard_chunk:(id,offset)=>ownedFolderChunk(id,offset),
                            clipboard_release:id=>ownedFolderRelease(id)}};
                    }""")
                    for name in ['folder-client.js', 'clipboard-files.js', 'terminal-composer.js', 'folder-composer.js']:
                        await page.evaluate((STATIC / name).read_text(encoding='utf-8'))
                    await page.locator('#terminal-composer-open').click()
                    await page.locator('#terminal-composer-text').fill('keep this draft')
                    await page.locator('#terminal-folder-choose').click()
                    await page.wait_for_function("document.querySelectorAll('#terminal-folder-list li').length===1")
                    await page.locator('#terminal-folder-upload').click()
                    await page.wait_for_function("!SuperMonoFolderComposer.busy", timeout=30000)
                    assert '已保存' in await page.locator('#terminal-folder-state').inner_text(), await page.locator('#terminal-folder-state').inner_text()
                    roots = [p for p in (parents[0].project / 'tmp').iterdir() if p.is_dir() and p.name.endswith('-owned-source')]
                    assert len(roots) == 1
                    assert (roots[0] / 'a/same.txt').read_bytes() == b'first leaf'
                    assert (roots[0] / 'b/same.txt').read_bytes() == b'second leaf'
                    assert (roots[0] / 'empty').is_dir()
                    assert not (parents[1].project / 'tmp').exists()
                    assert await page.locator('#terminal-composer-text').input_value() == 'keep this draft'
                    assert not pool._items
                    assert all(set(p.operations) <= {1, 10} for p in parents)
                    assert not errors
        finally:
            pool.close()

    run(exercise, tmp_path)


def test_folder_editor_destroy_releases_selected_native_sources(tmp_path):
    async def exercise(playwright, folder):
        from test_file_companion import environment
        from supermonoagent.windows_clipboard import ClipboardFiles

        source = folder / 'owned-destroy-source'
        source.mkdir()
        (source / 'one.txt').write_bytes(b'owned queued source')
        pool = ClipboardFiles()
        try:
            async with environment(folder) as (_broker, url, parents, _companions):
                async with browser_page(playwright, url, document_url=url.replace('ws:', 'http:').replace('/file/ws', '/')) as (page, errors, _sockets):
                    await page.expose_function('ownedChooseFolder', lambda: pool.snapshot_folder(source))
                    await page.expose_function('ownedFolderRelease', pool.release)
                    await page.evaluate("()=>{window.pywebview={api:{choose_folder:()=>ownedChooseFolder(),clipboard_release:id=>ownedFolderRelease(id)}};}")
                    for name in ['folder-client.js', 'clipboard-files.js', 'terminal-composer.js', 'folder-composer.js']:
                        await page.evaluate((STATIC / name).read_text(encoding='utf-8'))
                    await page.locator('#terminal-composer-open').click()
                    await page.locator('#terminal-folder-choose').click()
                    await page.wait_for_function("document.querySelectorAll('#terminal-folder-list li').length===1")
                    assert pool._items
                    await page.evaluate('()=>SuperMonoTerminalComposer.destroy()')
                    await page.wait_for_function('!window.SuperMonoFolderComposer', timeout=2000)
                    assert not pool._items
                    assert all(set(p.operations) <= {1, 10} for p in parents)
                    assert not errors
        finally:
            pool.close()

    run(exercise, tmp_path)


def test_old_receiver_is_rejected_before_opening_folder_picker(tmp_path):
    async def exercise(playwright, folder):
        from test_file_companion import environment

        async with environment(folder) as (_broker, url, _parents, _companions):
            async with browser_page(playwright, url, document_url=url.replace('ws:', 'http:').replace('/file/ws', '/')) as (page, errors, _sockets):
                await page.evaluate("""()=>{
                    window.ownedPickerCalls=0;window.pywebview={api:{choose_folder:()=>{ownedPickerCalls++;throw Error('must not open');}}};
                    const original=SuperMonoFileWorkspaces.request;
                    SuperMonoFileWorkspaces.request=(target,action,data)=>action==='folder.capabilities'
                        ?Promise.reject(Object.assign(Error('action_forbidden'),{code:'action_forbidden'})):original(target,action,data);
                }""")
                for name in ['folder-client.js', 'clipboard-files.js', 'terminal-composer.js', 'folder-composer.js']:
                    await page.evaluate((STATIC / name).read_text(encoding='utf-8'))
                await page.locator('#terminal-composer-open').click()
                await page.locator('#terminal-composer-text').fill('keep my draft')
                await page.locator('#terminal-folder-choose').click()
                await page.wait_for_function('!SuperMonoFolderComposer.busy')
                assert await page.evaluate('ownedPickerCalls') == 0
                assert '升级' in await page.locator('#terminal-folder-state').inner_text()
                assert await page.locator('#terminal-composer-text').input_value() == 'keep my draft'
                assert not errors

    run(exercise, tmp_path)


def test_selected_folder_never_retargets_after_connection_replaced(tmp_path):
    async def exercise(playwright, folder):
        from test_file_companion import environment
        from supermonoagent.windows_clipboard import ClipboardFiles

        source = folder / 'owned-stale-source'
        source.mkdir()
        (source / 'one.txt').write_bytes(b'never retarget this selection')
        pool = ClipboardFiles()
        try:
            async with environment(folder) as (_broker, url, parents, _companions):
                async with browser_page(playwright, url, document_url=url.replace('ws:', 'http:').replace('/file/ws', '/')) as (page, errors, _sockets):
                    await page.expose_function('ownedChooseFolder', lambda: pool.snapshot_folder(source))
                    await page.expose_function('ownedFolderChunk', pool.read)
                    await page.expose_function('ownedFolderRelease', pool.release)
                    await page.evaluate("""()=>{window.pywebview={api:{choose_folder:()=>ownedChooseFolder(),
                        clipboard_chunk:(id,offset)=>ownedFolderChunk(id,offset),clipboard_release:id=>ownedFolderRelease(id)}};}""")
                    for name in ['folder-client.js', 'clipboard-files.js', 'terminal-composer.js', 'folder-composer.js']:
                        await page.evaluate((STATIC / name).read_text(encoding='utf-8'))
                    await page.locator('#terminal-composer-open').click()
                    await page.locator('#terminal-folder-choose').click()
                    await page.wait_for_function("document.querySelectorAll('#terminal-folder-list li').length===1")
                    await page.evaluate("()=>{smaWorkspace.activePane.host.ws={readyState:1};}")
                    await page.evaluate("()=>{SuperMonoTerminalComposer.close();SuperMonoTerminalComposer.open();}")
                    await page.locator('#terminal-folder-upload').click()
                    await page.wait_for_function('!SuperMonoFolderComposer.busy')
                    assert not list((parents[0].project / 'tmp').glob('**/one.txt'))
                    assert not pool._items
                    assert all(set(p.operations) <= {1, 10} for p in parents)
                    assert not errors
        finally:
            pool.close()

    run(exercise, tmp_path)


def test_lost_complete_ack_reconciles_original_binding_after_tmp_creation(tmp_path):
    async def exercise(playwright, folder):
        from test_file_companion import environment
        from supermonoagent.windows_clipboard import ClipboardFiles

        source = folder / 'owned-reconcile-source'
        source.mkdir()
        (source / 'one.txt').write_bytes(b'only one committed copy')
        pool = ClipboardFiles()
        try:
            async with environment(folder) as (_broker, url, parents, _companions):
                assert not (parents[0].project / 'tmp').exists()
                async with browser_page(playwright, url, document_url=url.replace('ws:', 'http:').replace('/file/ws', '/')) as (page, errors, _sockets):
                    await page.expose_function('ownedChooseFolder', lambda: pool.snapshot_folder(source))
                    await page.expose_function('ownedFolderChunk', pool.read)
                    await page.expose_function('ownedFolderRelease', pool.release)
                    await page.evaluate("""()=>{
                        window.pywebview={api:{choose_folder:()=>ownedChooseFolder(),
                          clipboard_chunk:(id,offset)=>ownedFolderChunk(id,offset),clipboard_release:id=>ownedFolderRelease(id)}};
                        window.ownedFolderActions=[];window.ownedDropped=false;
                        const original=SuperMonoFileWorkspaces.request;
                        window.ownedOriginalRequest=original;
                        SuperMonoFileWorkspaces.request=async(target,action,data)=>{
                            ownedFolderActions.push(action);const value=await original(target,action,data);
                            if(action==='workspace.get'&&!window.ownedFirstBinding)window.ownedFirstBinding=value.project_binding;
                            if(action==='folder.finish'&&!ownedDropped){ownedDropped=true;throw Object.assign(Error('owned lost final ACK'),{code:'disconnected'});}
                            return value;
                        };
                    }""")
                    for name in ['folder-client.js', 'clipboard-files.js', 'terminal-composer.js', 'folder-composer.js']:
                        await page.evaluate((STATIC / name).read_text(encoding='utf-8'))
                    await page.locator('#terminal-composer-open').click()
                    await page.locator('#terminal-folder-choose').click()
                    await page.wait_for_function("document.querySelectorAll('#terminal-folder-list li').length===1")
                    await page.evaluate("async()=>{window.ownedFirstBinding=(await SuperMonoFileWorkspaces.getWorkspace(SuperMonoFileWorkspaces.captureTarget())).project_binding;}")
                    await page.locator('#terminal-folder-upload').click()
                    await page.wait_for_function('!SuperMonoFolderComposer.busy')
                    assert await page.evaluate('ownedDropped')
                    assert len(list((parents[0].project / 'tmp').glob('**/one.txt'))) == 1
                    changed = await page.evaluate("""async()=>{
                        const t=SuperMonoFileWorkspaces.captureTarget();
                        const now=await ownedOriginalRequest(t,'workspace.get',{});
                        return now.project_binding!==ownedFirstBinding;
                    }""")
                    assert changed, 'Missing tmp creation must exercise a legitimate binding change'
                    before = await page.evaluate('ownedFolderActions.length')
                    await page.locator('#terminal-folder-list button').filter(has_text='查询状态').click()
                    await page.wait_for_function("document.getElementById('terminal-folder-state').textContent==='已保存'")
                    assert await page.evaluate('(i)=>ownedFolderActions.slice(i)', before) == ['folder.status']
                    assert len(list((parents[0].project / 'tmp').glob('**/one.txt'))) == 1
                    assert not pool._items
                    assert all(set(p.operations) <= {1, 10} for p in parents)
                    assert not errors
        finally:
            pool.close()

    run(exercise, tmp_path)


def test_uncommitted_source_failure_can_stop_batch_and_remove_record(tmp_path):
    async def exercise(playwright, folder):
        import os
        from test_file_companion import environment
        from supermonoagent.windows_clipboard import ClipboardFiles

        source = folder / 'owned-changed-source'
        source.mkdir()
        file = source / 'one.txt'
        file.write_bytes(b'guard this source')
        pool = ClipboardFiles()
        try:
            async with environment(folder) as (_broker, url, parents, _companions):
                (parents[0].project / 'tmp').mkdir()
                async with browser_page(playwright, url, document_url=url.replace('ws:', 'http:').replace('/file/ws', '/')) as (page, errors, _sockets):
                    await page.expose_function('ownedChooseFolder', lambda: pool.snapshot_folder(source))
                    await page.expose_function('ownedFolderChunk', pool.read)
                    await page.expose_function('ownedFolderRelease', pool.release)
                    await page.evaluate("""()=>{window.pywebview={api:{choose_folder:()=>ownedChooseFolder(),
                        clipboard_chunk:(id,offset)=>ownedFolderChunk(id,offset),clipboard_release:id=>ownedFolderRelease(id)}};}""")
                    for name in ['folder-client.js', 'clipboard-files.js', 'terminal-composer.js', 'folder-composer.js']:
                        await page.evaluate((STATIC / name).read_text(encoding='utf-8'))
                    await page.locator('#terminal-composer-open').click()
                    await page.locator('#terminal-folder-choose').click()
                    await page.wait_for_function("document.querySelectorAll('#terminal-folder-list li').length===1")
                    info = file.stat()
                    os.utime(file, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
                    await page.locator('#terminal-folder-upload').click()
                    await page.wait_for_function('!SuperMonoFolderComposer.busy')
                    assert not list((parents[0].project / 'tmp').glob('**/one.txt'))
                    assert not pool._items
                    await page.locator('#terminal-folder-list button').filter(has_text='查询状态').click()
                    await page.wait_for_function('!SuperMonoFolderComposer.busy')
                    await page.locator('#terminal-folder-list button').filter(has_text='停止此批次').click()
                    await page.wait_for_function("!SuperMonoFolderComposer.busy && document.getElementById('terminal-folder-state').textContent==='已停止后续文件'")
                    remove = page.locator('#terminal-folder-list button').filter(has_text='移除记录')
                    assert await remove.is_enabled()
                    await remove.click()
                    assert await page.locator('#terminal-folder-list li').count() == 0
                    await page.locator('#terminal-folder-choose').click()
                    await page.wait_for_function("document.querySelectorAll('#terminal-folder-list li').length===1")
                    await page.evaluate('()=>SuperMonoTerminalComposer.destroy()')
                    assert not pool._items
                    assert not list((parents[0].project / 'tmp').glob('**/one.txt'))
                    assert all(set(p.operations) <= {1, 10} for p in parents)
                    assert not errors
        finally:
            pool.close()

    run(exercise, tmp_path)
