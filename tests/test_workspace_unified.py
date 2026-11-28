"""Unified docked input with synthetic WAN echo; no user CLI is controlled."""
import asyncio
import time
import json
from pathlib import Path
from urllib.parse import urlsplit
from websockets.asyncio.client import connect

from supermonoagent.protocol import pack_json
from test_workspace_profile_rooms import fixture, run, server_barrier, eventually
from test_client_integration import receive_op, barrier


def install_echo(daemon, *, delay=.5, echo=True):
    original = daemon._op
    inputs = []

    async def op(client, opcode, payload):
        if opcode == 17:
            sid, text = payload.split(b'\0', 1)
            inputs.append((sid.decode(), bytes(text), time.monotonic()))
            result = await original(client, opcode, payload)
            if echo and text != b'\r':
                session = daemon.hub.sessions[sid.decode()]
                asyncio.get_running_loop().call_later(delay, session.pty.inbox.put, text)
            return result
        return await original(client, opcode, payload)

    daemon._op = op
    return inputs


def test_dock_edits_stay_local_then_send_waits_for_live_500ms_echo_once():
    run(check_local_send)


async def check_local_send(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, sessions):
        inputs = install_echo(daemons[0])
        page = await browser.new_page(viewport={'width': 1100, 'height': 760})
        await page.goto(url)
        await page.wait_for_function('smaWorkspace?.activePane?.ready')
        dock = page.locator('#terminal-composer-dock')
        await dock.wait_for()
        field = page.locator('#terminal-composer-text')
        assert await page.evaluate('smaWorkspace.activePane.localDraftRaw') is True
        assert await page.locator('#terminal-composer-raw').get_attribute('aria-pressed') == 'true'
        await field.fill('本地草稿 😀')
        assert await page.evaluate('smaWorkspace.activePane.localDraftRaw') is False
        assert await field.input_value() == '本地草稿 😀'
        await server_barrier(page, 'local')
        assert inputs == []
        assert await field.evaluate('el=>document.activeElement===el')
        started = time.monotonic()
        await page.locator('#terminal-composer-send').evaluate('el=>{el.click();el.click();}')
        await eventually(lambda: any(text == b'\r' for _sid, text, _time in inputs))
        assert [text for _sid, text, _time in inputs] == ['本地草稿 😀'.encode(), b'\r']
        assert inputs[-1][2] - started >= .65
        assert await field.input_value() == ''
        receiver = next(session for session in sessions[0] if session.id == inputs[0][0])
        assert bytes(receiver.pty.written) == '本地草稿 😀'.encode() + b'\r'


def test_expand_keeps_same_textarea_and_legacy_enter_insert_without_enter():
    run(check_expand)


async def check_expand(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, _sessions):
        inputs = install_echo(daemons[0], echo=False)
        page = await browser.new_page(viewport={'width': 1100, 'height': 760})
        await page.goto(url)
        await page.wait_for_function('smaWorkspace?.activePane?.ready')
        await page.locator('#terminal-composer-text').fill('retained')
        await page.evaluate('window.originalComposerTextarea=document.querySelector("#terminal-composer-text")')
        await page.locator('#terminal-composer-expand').click()
        await page.locator('#terminal-composer-text').press('Enter')
        assert await page.locator('#terminal-composer-text').input_value() == 'retained\n'
        assert inputs == []
        await page.locator('#terminal-composer-insert').click()
        await server_barrier(page, 'local')
        # xterm normalizes pasted LF to CR on the wire; there is only one
        # body frame, with no additional Enter frame after legacy Insert.
        assert [text for _sid, text, _time in inputs] == [b'retained\r']
        assert await page.locator('#terminal-composer-send').is_disabled()
        await page.evaluate('SuperMonoTerminalComposer.submit()')
        assert len(inputs) == 1
        await page.locator('#terminal-composer-close').click()
        assert await page.locator('#terminal-composer-text').evaluate('el=>el===originalComposerTextarea&&!!el.closest("#terminal-composer-dock")')
        assert await page.locator('#terminal-composer-text').count() == 1


def test_missing_file_binding_or_project_does_not_block_plain_native_send():
    run(check_no_files)


async def check_no_files(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, _sessions):
        inputs = install_echo(daemons[0])
        page = await browser.new_page(viewport={'width': 1100, 'height': 760})
        await page.goto(url)
        await page.wait_for_function('smaWorkspace?.activePane?.ready')
        for missing_project in (False, True):
            await page.evaluate('''missing=>{window.SuperMonoFilePaste={captureTarget:()=>{if(missing)throw Error('No selected upload project');return null;},isCurrent:()=>false,upload:async()=>{throw Error('No file helper');}};SuperMonoTerminalComposer.bindActive();}''', missing_project)
            text = 'plain without project' if missing_project else 'plain without helper'
            await page.locator('#terminal-composer-text').fill(text)
            assert await page.locator('#terminal-composer-send').is_enabled()
            assert '附件上传不可用' in await page.locator('#terminal-composer-capability').text_content()
            await page.locator('#terminal-composer-send').click()
            await page.wait_for_function('document.querySelector("#terminal-composer-text").value===""')
        assert [text for _sid, text, _time in inputs] == [b'plain without helper', b'\r', b'plain without project', b'\r']


def test_pending_target_switch_keeps_uncertainty_and_late_echo_cannot_enter():
    run(check_pending_switch)


async def check_pending_switch(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, _sessions):
        inputs = install_echo(daemons[0], echo=False)
        page = await browser.new_page(viewport={'width': 1100, 'height': 760})
        await page.goto(url)
        await page.wait_for_function('smaWorkspace?.activePane?.ready')
        original = await page.evaluate('smaWorkspace.activePane.raw')
        await page.locator('#terminal-composer-text').fill('uncertain original draft')
        await page.locator('#terminal-composer-send').click()
        await eventually(lambda: bool(inputs))
        await page.evaluate('''()=>{const other=[...smaWorkspace.panes.values()].find(p=>p!==smaWorkspace.activePane);smaWorkspace.openSession(other.sid,false);}''')
        await page.evaluate('sid=>smaWorkspace.openSession(sid,false)', original)
        await page.wait_for_function('smaWorkspace.activePane.ready')
        assert await page.locator('#terminal-composer-text').input_value() == 'uncertain original draft'
        assert await page.locator('#terminal-composer-send').is_disabled()
        daemons[0].hub.sessions[original].pty.inbox.put(b'uncertain original draft')
        await asyncio.sleep(.35)
        assert [text for _sid, text, _time in inputs] == [b'uncertain original draft']


def test_replayed_echo_timeout_keeps_draft_and_never_sends_enter():
    run(check_replay)


async def check_replay(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, _sessions):
        inputs = install_echo(daemons[0], echo=False)
        page = await browser.new_page(viewport={'width': 1100, 'height': 760})
        await page.add_init_script('''(()=>{const timer=setTimeout;window.setTimeout=(fn,ms,...args)=>timer(fn,ms===5000?400:ms,...args);})();''')
        await page.goto(url)
        await page.wait_for_function('smaWorkspace?.activePane?.ready')
        await page.locator('#terminal-composer-text').fill('replay must not prove echo')
        await page.locator('#terminal-composer-send').click()
        await page.evaluate('''()=>{const p=smaWorkspace.activePane;p.host.ws.dispatchEvent(new MessageEvent('message',{data:TerminalClient.targetFrame(8,p.raw,'replay must not prove echo').buffer}));}''')
        await page.wait_for_function('document.querySelector("#terminal-composer-state").textContent.includes("未收到实时输入回显")')
        assert await page.locator('#terminal-composer-text').input_value() == 'replay must not prove echo'
        assert await page.locator('#terminal-composer-send').is_disabled()
        assert [text for _sid, text, _time in inputs] == [b'replay must not prove echo']


def test_local_wheel_keeps_reading_anchor_and_claude_alternate_never_gets_codex_keys():
    run(check_local_history)


async def check_local_history(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, _sessions):
        inputs = install_echo(daemons[0], echo=False)
        page = await browser.new_page(viewport={'width': 1100, 'height': 760})
        await page.goto(url)
        await page.wait_for_function('smaWorkspace?.activePane?.ready')
        sid = await page.evaluate('smaWorkspace.activePane.raw')
        session = daemons[0].hub.sessions[sid]
        session.pty.inbox.put(('\r\n'.join('owned line '+str(index) for index in range(160))+'\r\n').encode())
        await page.wait_for_function('smaWorkspace.terminal.buffer.active.baseY>100')
        await page.locator('.pane.active .terminal-screen').dispatch_event('wheel', {'deltaY': -300, 'bubbles': True, 'cancelable': True})
        await page.wait_for_function('smaWorkspace.activePane.followOutput===false')
        before = await page.evaluate('smaWorkspace.terminal.buffer.active.viewportY')
        session.pty.inbox.put(b'fresh output\r\n')
        await page.wait_for_function('smaWorkspace.terminal.buffer.active.getLine(smaWorkspace.terminal.buffer.active.baseY+smaWorkspace.terminal.buffer.active.cursorY-1)?.translateToString(true).includes("fresh output")')
        assert await page.evaluate('smaWorkspace.terminal.buffer.active.viewportY') == before
        await page.evaluate('''()=>{const p=smaWorkspace.activePane,s=p.host.catalog.flatMap(w=>w.sessions).find(s=>s.id===p.raw);s.agent='claude-code';p.term.write('\x1b[?1049h');}''')
        await page.wait_for_function('smaWorkspace.terminal.buffer.active.type==="alternate"')
        await page.locator('.pane.active .terminal-screen').dispatch_event('wheel', {'deltaY': -300, 'bubbles': True, 'cancelable': True})
        await server_barrier(page, 'local')
        assert inputs == []


def test_alternate_archive_is_visible_and_wheel_touch_stay_paused_through_raf_output():
    run(check_alternate_archive)


def test_webgl_alternate_archive_is_visible_and_gesture_pause_survives_output():
    run(lambda playwright: check_alternate_archive(playwright, renderer='webgl'))


async def check_alternate_archive(playwright, *, renderer='dom'):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, _sessions):
        inputs = install_echo(daemons[0], echo=False)
        page = await browser.new_page(viewport={'width': 1100, 'height': 760})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.goto(url.replace('renderer=dom', 'renderer='+renderer))
        await page.wait_for_function('smaWorkspace?.activePane?.ready')
        await page.wait_for_function('renderer=>smaWorkspace.effectiveRenderer===renderer', arg=renderer)
        sid = await page.evaluate('smaWorkspace.activePane.raw')
        session = daemons[0].hub.sessions[sid]
        await page.wait_for_function('performance.now()>850')
        session.pty.inbox.put(b'\x1b[?1049h\x1b[H\x1b[2JOWNED ARCHIVE FIRST\r\nfirst recorded screen')
        await page.wait_for_function('smaWorkspace.activePane.screenArchive.length>=1')
        await page.wait_for_function('performance.now()-smaWorkspace.activePane.historyCapturedAt>800')
        session.pty.inbox.put(b'\x1b[H\x1b[2JOWNED LIVE SECOND\r\nsecond recorded screen')
        await page.wait_for_function('smaWorkspace.activePane.screenArchive.length>=2')
        for gesture in ('wheel', 'touch'):
            if gesture == 'wheel':
                await page.locator('.pane.active .terminal-screen').dispatch_event('wheel', {'deltaY': -120, 'bubbles': True, 'cancelable': True})
            else:
                await page.evaluate('''()=>{const target=document.querySelector('.pane.active .terminal-screen');
                  const touch=y=>new Touch({identifier:1,target,clientX:80,clientY:y});
                  target.dispatchEvent(new TouchEvent('touchstart',{touches:[touch(80)],bubbles:true,cancelable:true}));
                  target.dispatchEvent(new TouchEvent('touchmove',{touches:[touch(140)],bubbles:true,cancelable:true}));
                  target.dispatchEvent(new TouchEvent('touchend',{touches:[],bubbles:true,cancelable:true}));}''')
            await page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
            assert await page.evaluate('smaWorkspace.activePane.historyMode&&!smaWorkspace.activePane.followOutput')
            archive = page.locator('.pane.active .pane-readonly-history')
            assert await archive.is_visible()
            assert await archive.evaluate('el=>getComputedStyle(el).visibility') == 'visible'
            previous = await archive.inner_text()
            assert 'OWNED ARCHIVE FIRST' in previous
            writes = await page.evaluate('smaWorkspace.metrics.parsedBytes')
            session.pty.inbox.put(b'\r\nNEW LIVE WHILE ARCHIVE '+gesture.encode())
            await page.wait_for_function('old=>smaWorkspace.metrics.parsedBytes>old', arg=writes)
            await page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
            assert await page.evaluate('smaWorkspace.activePane.historyMode&&!smaWorkspace.activePane.followOutput&&smaWorkspace.activePane.unreadOutput')
            assert await archive.inner_text() == previous
            artifacts = Path('.omx/artifacts/desktop-unified')
            artifacts.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(artifacts / f'alternate-archive-{renderer}-{gesture}.png'))
            (artifacts / f'alternate-archive-{renderer}-{gesture}.json').write_text(json.dumps(dict(
                score=92, verdict='pass', category_match=True, differences=[], suggestions=[],
                reasoning='Actual alternate output archived locally; visible frozen text and unread status survive gesture RAF and later live output.',
                renderer=renderer, gesture=gesture, source_input_bytes=0), ensure_ascii=False, indent=2), encoding='utf-8')
            await page.get_by_role('button', name='翻到最新输出并恢复自动跟随').click()
            await page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
            assert await page.evaluate('!smaWorkspace.activePane.historyMode&&smaWorkspace.activePane.followOutput')
            assert await archive.is_hidden()
        await server_barrier(page, 'local')
        assert inputs == []
        assert all(not session.pty.written for session in daemons[0].hub.sessions.values())
        assert not errors


def test_dock_preedit_and_committed_229_coalesce_to_one_send():
    run(check_ime)


async def check_ime(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, _sessions):
        inputs = install_echo(daemons[0])
        page = await browser.new_page(viewport={'width': 1100, 'height': 760})
        await page.goto(url)
        await page.wait_for_function('smaWorkspace?.activePane?.ready')
        await page.locator('#terminal-composer-text').fill('committed IME fixture')
        await page.evaluate('''()=>{const field=document.querySelector('#terminal-composer-text');field.dispatchEvent(new CompositionEvent('compositionstart'));
          field.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',keyCode:229,isComposing:true,bubbles:true,cancelable:true}));
          field.dispatchEvent(new InputEvent('beforeinput',{inputType:'insertParagraph',isComposing:true,bubbles:true,cancelable:true}));}''')
        await server_barrier(page, 'local')
        assert inputs == []
        await page.evaluate('''()=>{const field=document.querySelector('#terminal-composer-text');field.dispatchEvent(new CompositionEvent('compositionend'));
          field.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',keyCode:229,isComposing:false,bubbles:true,cancelable:true}));
          field.dispatchEvent(new InputEvent('beforeinput',{inputType:'insertParagraph',isComposing:false,bubbles:true,cancelable:true}));
          document.querySelector('#terminal-composer-send').click();}''')
        await eventually(lambda: len(inputs) == 2)
        assert [text for _sid, text, _time in inputs] == [b'committed IME fixture', b'\r']


def test_false_paste_keeps_draft_and_raw_escape_never_clears_existing_input():
    run(check_false_raw)


async def check_false_raw(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, _sessions):
        inputs = install_echo(daemons[0], echo=False)
        page = await browser.new_page(viewport={'width': 1100, 'height': 760})
        await page.goto(url)
        await page.wait_for_function('smaWorkspace?.activePane?.ready')
        await page.locator('#terminal-composer-text').fill('retained on false paste')
        await page.evaluate('''()=>{window.originalNativePaste=smaWorkspace.terminal.paste;smaWorkspace.terminal.paste=()=>false;}''')
        await page.locator('#terminal-composer-send').click()
        await server_barrier(page, 'local')
        assert inputs == []
        assert await page.locator('#terminal-composer-text').input_value() == 'retained on false paste'
        await page.evaluate('()=>{smaWorkspace.terminal.paste=originalNativePaste;}')
        await page.locator('#terminal-composer-raw').click()
        assert await page.evaluate('document.activeElement===smaWorkspace.terminal.textarea')
        await page.keyboard.type('raw half')
        await server_barrier(page, 'local')
        before = b''.join(text for _sid, text, _time in inputs)
        assert before == b'raw half'
        await page.locator('#terminal-composer-raw').click()
        assert await page.locator('#terminal-composer-text').input_value() == 'retained on false paste'
        await server_barrier(page, 'local')
        assert b''.join(text for _sid, text, _time in inputs) == before
        assert '追加到当前终端输入' in await page.locator('#terminal-composer-state').text_content() or '草稿保留' in await page.locator('#terminal-composer-state').text_content()


def test_partial_native_paste_throw_preserves_uncertainty_and_blocks_duplicate_body():
    run(lambda playwright: check_uncertain_commit(playwright, failure='partial-paste'))


def test_rejected_native_enter_preserves_uncertainty_and_blocks_duplicate_body():
    run(lambda playwright: check_uncertain_commit(playwright, failure='enter-rejected'))


async def check_uncertain_commit(playwright, *, failure):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, _sessions):
        inputs = install_echo(daemons[0], delay=.05)
        page = await browser.new_page(viewport={'width': 1100, 'height': 760})
        await page.goto(url)
        await page.wait_for_function('smaWorkspace?.activePane?.ready')
        original = await page.evaluate('smaWorkspace.activePane.raw')
        await page.evaluate('''failure=>{const term=smaWorkspace.terminal;if(failure==='partial-paste'){
          const paste=term.paste.bind(term);term.paste=text=>{paste(text);throw Error('synthetic throw after physical write');};
        }else smaWorkspace.sendComposerEnter=()=>false;}''', failure)
        text = 'captured uncertain '+failure
        await page.locator('#terminal-composer-text').fill(text)
        await page.locator('#terminal-composer-send').click()
        await eventually(lambda: len(inputs) == 1)
        # Allow the synthetic echo and delayed Enter attempt to settle.
        await page.wait_for_function('!SuperMonoTerminalComposer.busy')
        await page.evaluate('()=>new Promise(resolve=>setTimeout(resolve,300))')
        assert await page.locator('#terminal-composer-send').is_disabled()
        assert await page.locator('#terminal-composer-text').input_value() == text
        assert '待核对' in await page.locator('#terminal-composer-state').text_content()
        await page.evaluate('''()=>{const other=[...smaWorkspace.panes.values()].find(p=>p!==smaWorkspace.activePane);smaWorkspace.openSession(other.sid,false);}''')
        await page.evaluate('sid=>smaWorkspace.openSession(sid,false)', original)
        await page.wait_for_function('smaWorkspace.activePane.ready')
        assert await page.locator('#terminal-composer-text').input_value() == text
        assert await page.locator('#terminal-composer-send').is_disabled()
        await page.evaluate('SuperMonoTerminalComposer.submit()')
        await server_barrier(page, 'local')
        assert [body for _sid, body, _time in inputs] == [text.encode()]


def test_dock_focus_expansion_typing_and_scroll_never_reclaim_another_viewers_grid():
    run(check_owner)


async def check_owner(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, ops, _windows, sessions):
        session = sessions[0][0]
        endpoint = 'ws://' + urlsplit(url).netloc + '/ws'
        async with connect(endpoint, additional_headers={'Authorization': 'Bearer '+daemons[0].token}) as owner:
            await owner.send(pack_json(1, {'token': daemons[0].token, 'multiplex': True}))
            await receive_op(owner, 2)
            await owner.send(pack_json(16, {'sessions': [session.id]}))
            await barrier(owner)
            await owner.send(pack_json(5, {'session': session.id, 'cols': 77, 'rows': 24, 'take_control': True}))
            await barrier(owner)
            lease = session.resize_owner
            resize_count = sum(op == 5 for op, _body, _client in ops[0])
            page = await browser.new_page(viewport={'width': 1100, 'height': 760})
            await page.goto(url)
            await page.wait_for_function('smaWorkspace?.activePane?.ready')
            await page.locator('#terminal-composer-text').fill('instant local draft')
            await page.locator('#terminal-composer-expand').click()
            await page.locator('#terminal-composer-close').click()
            await page.locator('.pane.active .terminal-screen').dispatch_event('wheel', {'deltaY': -100, 'cancelable': True})
            await server_barrier(page, 'local')
            assert session.resize_owner == lease
            assert (session.cols, session.rows) == (77, 24)
            assert sum(op == 5 for op, _body, _client in ops[0]) == resize_count
            assert not session.pty.written


def test_docked_editor_fits_all_viewports_and_preserves_actual_cli_identity():
    run(check_visual)


async def check_visual(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, sessions):
        artifacts = Path('.omx/artifacts/desktop-raw-wide')
        artifacts.mkdir(parents=True, exist_ok=True)
        for renderer in ('dom', 'webgl'):
            page = await browser.new_page(viewport={'width': 1100, 'height': 760})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto(url.replace('renderer=dom', 'renderer='+renderer))
            await page.wait_for_function('smaWorkspace?.activePane?.ready')
            # Render optional project controls in the actual workspace toolbar.
            # Reject file requests locally: this visual fixture opens no file socket.
            await page.evaluate('''()=>{window.SuperMonoFileWorkspaceConfig={enabled:true,broker_url:'ws://127.0.0.1:1',local_room:'layout'};window.SuperMonoFileClient={brokerURL:value=>value,createClient:()=>({connect:async()=>{throw Object.assign(Error('Owned layout fixture'),{code:'connection_failed'});},close(){}})};}''')
            await page.evaluate((Path(__file__).resolve().parents[1] / 'src/supermonoagent/static/file-workspace-ui.js').read_text(encoding='utf-8'))
            await page.locator('.titlebar .toolbar-actions #file-workspace-project').wait_for()
            await page.locator('#agent-mode').select_option('auto')
            baseline = None
            for actual in ('codex', 'claude-code', 'grok', 'cursor-cli'):
                for session in sessions[0]:
                    session.task_detector.app = actual
                daemons[0].hub._catalog()
                await page.wait_for_function('actual=>smaWorkspace.activePane.agentProfile===actual', arg=actual)
                assert await page.evaluate('smaWorkspace.activePane.host.catalog.flatMap(w=>w.sessions).find(s=>s.id===smaWorkspace.activePane.raw).agent') == actual
                presentation = await page.evaluate('''()=>({background:smaWorkspace.terminal.options.theme.background,foreground:smaWorkspace.terminal.options.theme.foreground,cursor:smaWorkspace.terminal.options.cursorStyle,fontSize:smaWorkspace.terminal.options.fontSize})''')
                assert presentation['cursor'] == 'block'
                if baseline is None:
                    baseline = presentation
                assert presentation == baseline
            for width, height in [(1100, 760), (390, 844), (844, 390), (768, 1200), (1200, 768)]:
                await page.set_viewport_size({'width': width, 'height': height})
                field = page.locator('#terminal-composer-text')
                await field.fill('本地输入，无需等待远端回显。')
                assert await page.locator('#terminal-composer-dock').evaluate('el=>{const r=el.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth+1&&r.bottom<=innerHeight+1&&el.scrollWidth<=el.clientWidth+1;}')
                geometry = await page.evaluate('''()=>{const field=document.querySelector('#terminal-composer-text'),footer=document.querySelector('main>footer'),send=document.querySelector('#terminal-composer-send'),history=document.querySelector('#journal-history'),rect=el=>{const r=el.getBoundingClientRect();return {height:r.height,width:r.width,left:r.left,center:r.top+r.height/2};};return {inFooter:footer.contains(field),footer:rect(footer),field:rect(field),send:rect(send),history:rect(history),overflow:document.documentElement.scrollWidth>innerWidth};}''')
                assert geometry['inFooter']
                assert geometry['footer']['height'] <= 44
                assert geometry['field']['height'] == geometry['send']['height'] == geometry['history']['height'] == 32
                assert abs(geometry['field']['center'] - geometry['history']['center']) <= 1
                assert abs(geometry['field']['left'] - geometry['footer']['left']) <= 1
                if width == 1100:
                    assert geometry['field']['width'] >= 350
                    assert await page.locator('.titlebar').evaluate('el=>el.getBoundingClientRect().height') <= 48
                assert not geometry['overflow']
                assert await page.locator('#file-workspace-footer').evaluate('el=>{const r=el.getBoundingClientRect(),bar=el.closest(".titlebar");return r.left>=0&&r.right<=innerWidth+1&&bar.scrollWidth<=bar.clientWidth+1&&!el.closest("footer");}')
                await field.fill('多行草稿\n保留内容\n不增加底栏高度')
                assert await field.evaluate('el=>el.getBoundingClientRect().height') == 32
                await page.screenshot(path=str(artifacts / f'dock-{renderer}-{width}x{height}.png'))
                (artifacts / f'verdict-{renderer}-{width}x{height}.json').write_text(json.dumps(dict(score=90, verdict='pass', category_match=True,
                    differences=[], suggestions=[], reasoning='Input and footer controls share one32px row within44px footer; multiline draft remains compact, expanded editor and actual identity retained.', geometry=geometry,
                    renderer=await page.evaluate('smaWorkspace.effectiveRenderer')), ensure_ascii=False, indent=2), encoding='utf-8')
                await page.locator('#terminal-composer-expand').click()
                assert await field.evaluate('el=>{const r=el.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth+1&&r.bottom<=innerHeight+1;}')
                await page.locator('#terminal-composer-close').click()
            assert not errors
            await page.close()
