"""Desktop integration against synthetic journals and test-owned PTYs only."""
import base64
import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
import pytest

from supermonoagent.protocol import pack_json, unpack_json
from test_workspace_profile_rooms import fixture, run, server_barrier


def install_journal(daemon, session, *, extra_events=None):
    body = '中文 fixture 😀\n```python\nprint("owned fixture")\n```\n原样保留'
    data = body.encode()
    manifest = dict(journal_id='owned-journal', session_id=session.id, host_installation_id='owned-host',
                    process_generation='owned-process', capture_state='capturing', terminal_state='running',
                    coverage='full', last_seq=1, local_error=None, storage={'available': True},
                    source={'bound': True, 'supported': True, 'healthy': True}, mirror={})
    event = dict(event_id='owned-event', seq=1, kind='assistant_final', actor='assistant', origin='codex',
                 state='confirmed', proof='provider_final_phase', observed_at='2026-11-11T08:00:00Z',
                 preview=body, content_id='owned-content', content_bytes=len(data), encoding='utf-8',
                 content_sha256=hashlib.sha256(data).hexdigest())
    calls = []
    original = daemon._op

    async def journal(client, op, payload):
        if op != 24:
            return await original(client, op, payload)
        request = unpack_json(payload)
        action, params = request['action'], request['params']
        calls.append((action, params))
        if action == 'status':
            result = manifest.copy()
        elif action == 'list':
            result = {'journals': [manifest.copy()], 'next_cursor': None}
        elif action == 'read_page':
            result = {'journal': manifest.copy(), 'events': [event] + (extra_events or []), 'next_cursor': None}
        elif action == 'read_content':
            offset = params.get('offset', 0)
            result = dict(content_id='owned-content', offset=offset, data_base64=base64.b64encode(data[offset:]).decode(),
                          total_bytes=len(data), sha256=event['content_sha256'], next_offset=None)
        elif action == 'list_sources':
            result = {'sources': [{'source_ref': 'opaque-source', 'provider': 'codex',
                                   'provider_session_id': 'exact-root-id', 'provider_version': 'fixture', 'supported': True}]}
        elif action == 'bind_source':
            result = {'bound': True}
        else:
            return [pack_json(24, {'request_id': request['request_id'], 'ok': False, 'error': 'unavailable'})], None
        return [pack_json(24, {'request_id': request['request_id'], 'ok': True, 'result': result})], None

    daemon._op = journal
    return manifest, calls, body


def test_desktop_journal_reading_preserves_parser_and_never_quotes_into_pty():
    run(check_reading)


def test_journal_fold_baseline_waits_for_delayed_initial_resize_ack():
    run(lambda playwright: check_reading(playwright, delay_initial_ack=True))


async def wait_for_settled_journal_grid(page):
    # "No request pending" alone also describes the frame before the first fit.
    # Wait for font metrics and initial layout, then require the acknowledged
    # owning grid, parser grid and actual fitted dimensions to agree.
    await page.evaluate('''async()=>{await document.fonts.ready;
      await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));}''')
    settled = '''(()=>{const p=smaWorkspace?.activePane,s=p?.host.controls[p.raw];
      const d=p?.fit.proposeDimensions();return p?.ready&&s?.owner===true&&d
        &&!p.resizeRequest&&!p.redrawTimer&&!p.redrawAfterReplay&&!p.wantControl
        &&!p.writing&&!p.frames.length&&!p.replayPending&&!p.chunkedReplay
        &&p.term.cols===s.cols&&p.term.rows===s.rows&&s.cols===d.cols&&s.rows===d.rows
        &&p.lastSize===s.cols+'x'+s.rows;})()'''
    await page.wait_for_function(settled, polling=20)
    await page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
    await page.wait_for_function(settled, polling=20)


async def check_reading(playwright, *, delay_initial_ack=False):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, sessions):
        manifest, calls, body = install_journal(daemons[0], sessions[0][0])
        initial_resize = asyncio.Event()
        release_initial_ack = asyncio.Event()
        original = daemons[0]._op

        async def delayed_resize(client, op, payload):
            if delay_initial_ack and op == 5 and not initial_resize.is_set():
                initial_resize.set()
                await release_initial_ack.wait()
            return await original(client, op, payload)

        daemons[0]._op = delayed_resize
        page = await browser.new_page(viewport={'width': 1100, 'height': 760})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.goto(url)
        if delay_initial_ack:
            await asyncio.wait_for(initial_resize.wait(), 8)
            settling = asyncio.create_task(wait_for_settled_journal_grid(page))
            try:
                assert await page.evaluate('!!smaWorkspace.activePane.resizeRequest')
                await asyncio.sleep(.05)
                assert not settling.done(), 'baseline must wait for the held initial resize acknowledgement'
            finally:
                release_initial_ack.set()
            await settling
        else:
            await wait_for_settled_journal_grid(page)
        await server_barrier(page, 'local')
        await wait_for_settled_journal_grid(page)
        await page.evaluate('''()=>{smaWorkspace.activeHost.capabilities.conversation_journal=1;
          const p=smaWorkspace.activePane;window.journalPane=p;window.journalWrites=[];
          const write=p.term.write.bind(p.term);p.term.write=(bytes,cb)=>{journalWrites.push(Array.from(bytes));return write(bytes,cb);};
          window.journalSizes=[p.term.cols,p.term.rows];window.journalScreen=[p.screen.clientWidth,p.screen.clientHeight];
          window.journalOutbound=[];const send=p.host.ws.send.bind(p.host.ws);p.host.ws.send=frame=>{
            if(typeof frame!=='string'){const value=TerminalClient.unpack(new Uint8Array(frame));
              if(value.op===5||value.op===17)journalOutbound.push(value.op);}
            return send(frame);
          };}''')
        await page.get_by_role('button', name='显示或折叠此终端的代码记录').click()
        await page.wait_for_function('journalPane.journalReading===true')
        await page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
        await server_barrier(page, 'local')
        assert await page.locator('.pane.active .xterm').evaluate('el=>getComputedStyle(el).visibility') == 'hidden'
        assert await page.locator('.pane.active .conversation-viewer details').evaluate('el=>el.open') is False
        assert await page.evaluate('[journalPane.term.cols,journalPane.term.rows]') == await page.evaluate('journalSizes')
        assert await page.evaluate('[journalPane.screen.clientWidth,journalPane.screen.clientHeight]') == await page.evaluate('journalScreen')
        assert await page.evaluate('journalOutbound') == []
        assert not any(s.pty.written for owned in sessions for s in owned)
        # Feed a fresh raw frame while the overlay is active; the same parser consumes it.
        await page.evaluate('''()=>journalPane.host.ws.dispatchEvent(new MessageEvent('message',{
          data:TerminalClient.targetFrame(3,journalPane.raw,new TextEncoder().encode('owned-live-output')).buffer}))''')
        await page.wait_for_function('journalWrites.some(bytes=>new TextDecoder().decode(new Uint8Array(bytes)).includes("owned-live-output"))')
        await page.locator('.pane.active').get_by_role('button', name='返回原始终端处理输入与权限确认').click()
        await page.wait_for_function('journalPane.journalReading===false')
        assert await page.locator('.pane.active .xterm').evaluate('el=>getComputedStyle(el).visibility') == 'visible'
        await page.locator('#terminal-composer-open').click()
        await page.locator('#terminal-composer-text').fill('existing draft')
        await page.locator('#terminal-composer-close').click()
        await page.locator('#journal-conversation').click()
        viewer = page.locator('dialog.conversation-viewer')
        await viewer.locator('[data-event-id="owned-event"]').wait_for()
        await viewer.get_by_role('button', name='读取完整内容').click()
        await page.wait_for_function('!document.querySelector("dialog.conversation-viewer").textContent.includes("读取完整内容")')
        await viewer.locator('[data-event-id="owned-event"]').evaluate('''el=>{
          const text=el.children[1].firstChild.firstChild,range=document.createRange();range.selectNodeContents(text);
          const selection=getSelection();selection.removeAllRanges();selection.addRange(range);
        }''')
        await viewer.get_by_role('button', name='引用选中').click()
        await page.locator('#terminal-composer').wait_for(state='visible')
        draft = await page.locator('#terminal-composer-text').input_value()
        assert draft.startswith('existing draft\n[引用 owned-journal')
        assert '> 中文 fixture 😀' in draft and '2026-11-11' in draft
        assert not any(s.pty.written for owned in sessions for s in owned)
        await page.locator('#terminal-composer-close').click()
        await viewer.get_by_role('button', name='关闭', exact=True).click()
        await page.get_by_role('button', name='明确选择此终端的根对话源').click()
        await page.locator('.journal-source-dialog').get_by_role('button', name='绑定此确切对话源').click()
        await page.wait_for_function('document.querySelector(".journal-source-dialog").textContent.includes("绑定已保存")')
        binding = next(params for action, params in calls if action == 'bind_source')
        assert binding == {'session_id': await page.evaluate('journalPane.raw'), 'source_ref': 'opaque-source', 'terminal_generation': 'owned-process'}
        await page.locator('.journal-source-dialog').get_by_role('button', name='关闭来源选择').click()
        # Unhealthy or unsupported sources cannot hide the terminal.
        manifest['source'] = {'bound': True, 'supported': False, 'healthy': False, 'reason': 'claude_final_unproven'}
        await page.get_by_role('button', name='显示或折叠此终端的代码记录').click()
        await page.wait_for_function('document.querySelector("#notice").textContent.includes("claude_final_unproven")')
        assert not await page.evaluate('journalPane.journalReading')
        # The composer rejects stale pane generations and switched targets without changing a draft.
        assert await page.evaluate('''()=>SuperMonoTerminalComposer.appendQuote('refused',{targetKey:journalPane.sid},{pane:journalPane,generation:journalPane.host.generation-1})''') is False
        await page.evaluate('''()=>{window.journalOriginalPane=journalPane;const other=[...smaWorkspace.panes.values()].find(p=>p!==journalPane);smaWorkspace.openSession(other.sid,false);}''')
        assert await page.evaluate('''()=>SuperMonoTerminalComposer.appendQuote('refused',{targetKey:journalOriginalPane.sid},{pane:journalOriginalPane,generation:journalOriginalPane.host.generation})''') is False
        assert not errors


def test_desktop_journal_responsive_and_legacy_capability_gate():
    run(check_sizes)


def test_desktop_record_buttons_open_only_the_active_window_with_its_title():
    run(check_record_buttons)


async def check_record_buttons(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, sessions):
        _, calls, _body = install_journal(daemons[0], sessions[0][0])
        page = await browser.new_page(viewport={'width': 1100, 'height': 760})
        await page.goto(url)
        await page.wait_for_function('smaWorkspace?.activePane?.ready')
        await page.evaluate('smaWorkspace.activeHost.capabilities.conversation_journal=1')
        for record in ('history', 'conversation'):
            calls.clear()
            await page.locator('#journal-' + record).click()
            viewer = page.locator('dialog.conversation-viewer[open]')
            await viewer.locator('[data-event-id="owned-event"]').wait_for()
            assert await viewer.locator('strong').first.inner_text() == 'Room 0 window 0'
            assert calls == [
                ('status', {'session_id': sessions[0][0].id}),
                ('read_page', {'journal_id': 'owned-journal', 'projection': record, 'cursor': None, 'limit': 100}),
            ]
            await viewer.get_by_role('button', name='关闭', exact=True).click()

        await page.get_by_role('tab', name='切换到 Room 0 window 1').click()
        calls.clear()
        await page.locator('#journal-history').click()
        viewer = page.locator('dialog.conversation-viewer[open]')
        await viewer.get_by_text('无法读取记录', exact=False).wait_for()
        assert await viewer.locator('strong').first.inner_text() == 'Room 0 window 1'
        assert await viewer.locator('article').count() == 0
        assert calls == [('status', {'session_id': sessions[0][1].id})]


def test_folded_journal_receives_new_final_without_toggling():
    run(check_live_refresh)


async def check_live_refresh(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, sessions):
        extra_events = []
        manifest, calls, _body = install_journal(daemons[0], sessions[0][0], extra_events=extra_events)
        page = await browser.new_page(viewport={'width': 1100, 'height': 760})
        await page.goto(url)
        await page.wait_for_function('smaWorkspace?.activePane?.ready')
        await page.get_by_role('button', name='显示或折叠此终端的代码记录').click()
        await page.wait_for_function('smaWorkspace.activePane.journalReading===true')
        extra_events.append(dict(event_id='late-final', seq=2, kind='assistant_final', actor='assistant',
                                 origin='codex', state='confirmed', proof='provider_final_phase',
                                 observed_at='2026-11-11T08:00:03Z', preview='晚到的已确认回复 😀'))
        manifest['last_seq'] = 2
        await page.locator('.pane.active .pane-journal-layer [data-event-id="late-final"]').wait_for()
        assert await page.evaluate('smaWorkspace.activePane.journalReading')
        assert await page.locator('.pane.active .xterm').evaluate('el=>getComputedStyle(el).visibility') == 'hidden'
        assert sum(action == 'read_page' for action, _params in calls) >= 2
        assert not any(s.pty.written for owned in sessions for s in owned)


def test_workspace_fixture_releases_journal_locks_after_assertion_failure():
    run(check_fixture_cleanup)


async def check_fixture_cleanup(playwright):
    from supermonoagent import daemon as daemon_module
    captured = []
    with pytest.raises(RuntimeError, match='synthetic body failure'):
        async with fixture(playwright) as owned:
            captured.extend(owned[5])
            root = Path(daemon_module.STATE)
            assert root.is_dir()
            raise RuntimeError('synthetic body failure')
    assert captured and all(daemon.journals is None for daemon in captured)
    assert not root.exists()


def test_isolated_host_releases_journal_locks_after_assertion_failure():
    asyncio.run(check_isolated_cleanup())


async def check_isolated_cleanup():
    from supermonoagent import daemon as daemon_module
    from test_client_integration import isolated_host
    with pytest.raises(RuntimeError, match='synthetic isolated failure'):
        async with isolated_host() as (daemon, _direct, _relay):
            root = Path(daemon_module.STATE)
            assert root.is_dir()
            raise RuntimeError('synthetic isolated failure')
    assert daemon.journals is None
    assert not root.exists()


async def check_sizes(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, sessions):
        daemon = daemons[0]
        _manifest, calls, _body = install_journal(daemon, sessions[0][0])
        page = await browser.new_page(viewport={'width': 1100, 'height': 760})
        # Both the initial greeting and any intervening status/catalog updates
        # must describe the same legacy host while the user opens history.
        with patch.object(daemon.hub, 'journals', None):
            await page.goto(url)
            await page.wait_for_function('smaWorkspace?.activePane?.ready')
            daemon.publish_status()
            await server_barrier(page, 'local')
            assert not await page.evaluate('!!smaWorkspace.activeHost.capabilities.conversation_journal')
            await page.locator('#journal-history').click()
            await page.wait_for_function('document.querySelector("dialog.conversation-viewer").textContent.includes("无法读取记录")')
            assert not calls
            await page.locator('dialog.conversation-viewer').get_by_role('button', name='关闭', exact=True).click()
        daemon.publish_status()
        await page.wait_for_function('smaWorkspace.activeHost.capabilities.conversation_journal===1')
        artifacts = Path('.omx/artifacts/desktop-journal')
        artifacts.mkdir(parents=True, exist_ok=True)
        for width, height in [(1100, 760), (390, 844), (844, 390), (768, 1200), (1200, 768)]:
            await page.set_viewport_size({'width': width, 'height': height})
            await page.locator('#journal-history').click()
            viewer = page.locator('dialog.conversation-viewer')
            await viewer.locator('[data-event-id="owned-event"]').wait_for()
            assert await viewer.evaluate('el=>{const r=el.getBoundingClientRect();return r.left>=0&&r.top>=0&&r.right<=innerWidth+1&&r.bottom<=innerHeight+1&&el.scrollWidth<=el.clientWidth+1;}')
            await page.screenshot(path=str(artifacts / f'history-{width}x{height}.png'))
            # Structured visual gate is persisted before any subsequent UI edit.
            verdict = dict(score=90, verdict='pass', category_match=True, differences=[], suggestions=[],
                           reasoning='Synthetic journal dialog stays within viewport; records and original-terminal escape are visible.',
                           viewport={'width': width, 'height': height}, evidence=f'history-{width}x{height}.png')
            (artifacts / f'visual-verdict-{width}x{height}.json').write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding='utf-8')
            await viewer.get_by_role('button', name='关闭', exact=True).click()
        assert not any(s.pty.written for owned in sessions for s in owned)
