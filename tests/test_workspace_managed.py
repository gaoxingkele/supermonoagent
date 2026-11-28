"""Managed desktop contract fixtures; no provider binary or model is invoked."""
import asyncio
import json
from pathlib import Path
from supermonoagent.protocol import pack_json, unpack_json, unpack
from test_workspace_profile_rooms import fixture, run


def install_managed_wire(daemon, *, runtime_events=False):
    original = daemon._op
    original_state = daemon._state
    summary = daemon.hub.summary
    state = dict(target=None, phase='ready', active=None, permissions=[], receipts={}, expose=True, lose=False, lose_create=False, lose_permission=False, calls=[])
    if runtime_events:
        installation = getattr(getattr(daemon.journals, 'store', None), 'host_installation_id', 'fixture-host')
        state.update(runtime_cursor=dict(host_installation_id=installation, runtime_epoch='fixture-runtime', revision=0),
                     subscriptions=[])
        queued_frame_current = daemon._queued_frame_current

        def fixture_frame_current(client, frame):
            if frame[4] == 26:
                target = state['target']
                return target is not None and target['session_id'] in client.subscriptions
            return queued_frame_current(client, frame)

        daemon._queued_frame_current = fixture_frame_current
    else:
        add_identity = daemon._add_cockpit_identity

        def legacy_catalog(client, value):
            add_identity(client, value)
            value['capabilities'].pop('managed_events', None)

        daemon._add_cockpit_identity = legacy_catalog
        daemon.hub.decorate_catalog = legacy_catalog

    def managed_summary(*args, **kwargs):
        result = summary(*args, **kwargs)
        result['capabilities']['managed_conversation'] = 1
        if runtime_events:
            result['capabilities']['managed_events'] = 1
            result['host_installation_id'] = installation
        return result

    daemon.hub.summary = managed_summary

    def managed_state(client, *args, **kwargs):
        opcode, payload = unpack(original_state(client, *args, **kwargs))
        value = unpack_json(payload)
        value['capabilities']['managed_conversation'] = 1
        if runtime_events:
            value['capabilities']['managed_events'] = 1
            value['host_installation_id'] = installation
        return pack_json(opcode, value)

    daemon._state = managed_state

    async def op(client, opcode, payload):
        if opcode != 24:
            if runtime_events and opcode == 16:
                state['subscriptions'].append(unpack_json(payload))
            if opcode in {6, 9}:
                state['calls'].append(('legacy_open' if opcode == 6 else 'legacy_close', unpack_json(payload)))
            if opcode == 17:
                state['calls'].append(('raw_input', bytes(payload)))
            result = await original(client, opcode, payload)
            if opcode == 1:
                client.managed_conversation = unpack_json(payload).get('managed_conversation') == 1
            return result
        request = unpack_json(payload)
        action, params = request['action'], request['params']
        state['calls'].append((action, params))
        if action == 'create_managed':
            if state['target'] is None:
                anchor = daemon.hub.sessions[params['session_id']]
                state['anchor'] = (anchor.id, anchor.process_generation, anchor.cwd, anchor.window_id)
                catalog = daemon.hub._catalog
                daemon.hub._catalog = lambda: None
                try:
                    other = daemon.hub.open_window('Concurrent unrelated window', owner=daemon.token)
                    daemon.hub.open_cli(other.id, 'cmd')
                    window = daemon.hub.open_window('Owned managed fixture', owner=daemon.token)
                    session = daemon.hub.open_cli(window.id, 'cmd')
                    session.interaction_mode = 'managed'
                    session.provider = params['provider']
                    session.managed_snapshot = lambda: {'state': state['phase']}
                    state['target'] = dict(window_id=window.id, session_id=session.id,
                                           process_generation=session.process_generation, journal_id=session.journal_id)
                finally:
                    daemon.hub._catalog = catalog
                asyncio.get_running_loop().call_later(.2, catalog)
            result = dict(**state['target'], interaction_mode='managed', provider=params['provider'], state=state['phase'], cwd=state['anchor'][2])
            if state['lose_create']:
                state['lose_create'] = False
                return [], None
        elif action == 'managed_status':
            result = dict(**state['target'], state=state['phase'], provider='codex', provider_version='fixture',
                          provider_root_id='owned-provider-root', policy={'fixture': 'explicit permissions'},
                          active_submission=state['active'], permissions=state['permissions'])
            if state.get('last_outcome'):
                result['last_outcome'] = state['last_outcome']
            if runtime_events:
                result.update(state['runtime_cursor'])
            if 'nonce' in params:
                result['receipt'] = state['receipts'].get(params['nonce']) if state['expose'] else None
        elif action == 'submit_managed':
            result = dict(**state['target'], nonce=params['nonce'], submission_id='owned-submit', event_id='owned-event',
                          seq=1, state='accepted', dispatch_state='pending')
            state['receipts'][params['nonce']] = result
            state['phase'] = 'running'
            state['active'] = dict(submission_id='owned-submit', provider_turn_id='owned-turn', cancel_version=0)
            if state['lose']:
                return [], None
        elif action == 'respond_managed_permission':
            state['permissions'] = []
            result = dict(permission_request_id=params['permission_request_id'], state='decided', decision=params['decision'])
            if state['lose_permission']:
                state['lose_permission'] = False
                return [], None
        elif action == 'interrupt_managed':
            state['phase'] = 'interrupting'
            result = dict(submission_id=params['submission_id'], state='interrupt_requested', cancel_version=1)
        elif action == 'close_managed':
            state['phase'] = 'ended'
            result = dict(**state['target'], state='ended')
            if state.get('remove_on_close'):
                asyncio.get_running_loop().call_later(.01, daemon.hub.close_window, state['target']['window_id'])
        elif action == 'status':
            result = dict(journal_id=state['target']['journal_id'], host_installation_id='fixture-host',
                          last_seq=0, capture_state='capturing', terminal_state='running', coverage='full',
                          storage={'available': True}, source={'bound': True, 'supported': True, 'healthy': True})
        elif action == 'read_page':
            result = dict(journal={'journal_id': state['target']['journal_id'], 'host_installation_id': 'fixture-host'}, events=[], next_cursor=None)
        elif action == 'list':
            result = {'journals': [], 'next_cursor': None}
        else:
            return [pack_json(24, {'request_id': request['request_id'], 'ok': False, 'error': 'unsupported'})], None
        return [pack_json(24, {'request_id': request['request_id'], 'ok': True, 'result': result})], None

    daemon._op = op
    return state


async def create(page, url, state):
    await page.goto(url)
    await page.wait_for_function('smaWorkspace?.activePane?.ready')
    native = await page.evaluate('smaWorkspace.activePane.raw')
    await page.get_by_role('button', name='从此原 CLI 新建独立对话记录模式').click()
    await page.get_by_role('button', name='新建 codex 对话记录模式').click()
    await page.wait_for_function('smaWorkspace.activePane?.managedController?.snapshot().state==="ready"')
    assert await page.evaluate('smaWorkspace.activePane.raw') == state['target']['session_id']
    assert native != state['target']['session_id']
    return native


def test_runtime_events_replace_idle_status_poll_and_reconcile_a_gap():
    run(check_runtime_events)


async def check_runtime_events(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, *_):
        daemon = daemons[0]
        state = install_managed_wire(daemon, runtime_events=True)
        page = await browser.new_page(viewport={'width': 1100, 'height': 760})
        await create(page, url, state)

        def status_calls():
            return sum(action == 'managed_status' for action, _ in state['calls'])

        def emit(revision, phase, active=None):
            value = dict(state['target'], **state['runtime_cursor'])
            value.update(revision=revision, state=phase, provider='codex',
                         active_submission=active, permissions=[])
            subscribers = [client for client in daemon.hub.clients.values()
                           if state['target']['session_id'] in client.subscriptions]
            assert subscribers
            for client in subscribers:
                daemon.hub._send(client, pack_json(26, value))

        assert status_calls() == 1
        await page.wait_for_timeout(3400)
        assert status_calls() == 1

        emit(1, 'running', dict(submission_id='event-turn', cancel_version=0))
        await page.wait_for_function('smaWorkspace.activePane.managedController.snapshot().state === "running"', timeout=4000)
        emit(2, 'ready')
        await page.wait_for_function('smaWorkspace.activePane.managedController.snapshot().state === "ready"')
        assert status_calls() == 1

        state['runtime_cursor']['revision'] = 4
        emit(4, 'running', dict(submission_id='missing-prior-event', cancel_version=0))
        await page.wait_for_function('smaWorkspace.activePane.managedController.runtimeCursor()?.revision === 4')
        assert await page.evaluate('smaWorkspace.activePane.managedController.snapshot().state') == 'ready'
        assert status_calls() == 2
        assert await page.evaluate('smaWorkspace.metrics.managedEventGaps') == 1

        await page.evaluate('smaWorkspace.activePane.host.ws.close(4000)')
        await page.wait_for_function('smaWorkspace.activePane?.managedController?.snapshot().canSubmit')
        cursor = state['subscriptions'][-1].get('managed_after', {}).get(state['target']['session_id'])
        assert cursor and cursor['revision'] == 4 and cursor['runtime_epoch'] == 'fixture-runtime'
        assert cursor['journal_id'] == state['target']['journal_id']


def test_managed_creation_readonly_structured_submit_and_permissions():
    run(check_managed)


async def check_managed(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, sessions):
        state = install_managed_wire(daemons[0])
        page = await browser.new_page(viewport={'width': 1100, 'height': 760})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        native = await create(page, url, state)
        assert native in daemons[0].hub.sessions
        assert await page.evaluate('smaWorkspace.activePane.term.options.disableStdin&&smaWorkspace.activePane.term.textarea.readOnly')
        await page.evaluate('smaWorkspace.activePane.term.paste("forbidden raw")')
        await page.locator('[data-key="escape"]').evaluate('el=>el.click()')
        await page.locator('#terminal-composer-open').click()
        assert await page.locator('#terminal-composer-choose-files').is_disabled()
        oversized = '中' * 21846
        await page.locator('#terminal-composer-text').fill(oversized)
        await page.locator('#terminal-composer-insert').click()
        assert await page.locator('#terminal-composer-text').input_value() == oversized
        assert not any(action == 'submit_managed' for action, _params in state['calls'])
        await page.locator('#terminal-composer-text').fill('原样消息 😀')
        await page.locator('#terminal-composer-insert').click()
        await page.wait_for_function('document.querySelector("#terminal-composer-text").value===""')
        submits = [params for action, params in state['calls'] if action == 'submit_managed']
        assert len(submits) == 1 and submits[0]['text'] == '原样消息 😀'
        assert all(submits[0][name] == value for name, value in state['target'].items())
        await page.locator('#terminal-composer-close').click()
        state['phase'] = 'waiting_permission'
        state['permissions'] = [dict(permission_request_id='permission-one', submission_id='owned-submit', provider_turn_id='owned-turn',
                                    tool_name='<script>fixture</script>', details='<img src="https://invalid.fixture/no-load">',
                                    choices=['allow_once', 'deny'], requires_user_interaction=True, state='pending')]
        await page.get_by_role('button', name='检查对话记录状态').click()
        await page.locator('.managed-permission').wait_for()
        assert await page.locator('.managed-permission img').count() == 0
        assert await page.get_by_role('button', name='仅允许此 CLI 权限请求一次').count() == 0
        await page.get_by_role('button', name='拒绝此 CLI 权限请求').evaluate('el=>{el.click();el.click();}')
        await page.wait_for_function('!document.querySelector(".managed-permission")')
        decisions = [params for action, params in state['calls'] if action == 'respond_managed_permission']
        assert len(decisions) == 1 and decisions[0]['decision'] == 'deny'
        state['permissions'] = [dict(permission_request_id='permission-two', submission_id='owned-submit', provider_turn_id='owned-turn',
                                    tool_name='owned tool', details='Synthetic bounded permission details',
                                    choices=['allow_once', 'deny'], state='pending')]
        await page.get_by_role('button', name='检查对话记录状态').click()
        await page.get_by_role('button', name='仅允许此 CLI 权限请求一次').evaluate('el=>{el.click();el.click();}')
        await page.wait_for_function('!document.querySelector(".managed-permission")')
        assert [params['decision'] for action, params in state['calls'] if action == 'respond_managed_permission'] == ['deny', 'allow_once']
        assert await page.evaluate('''()=>{const p=smaWorkspace.activePane;return SuperMonoTerminalComposer.appendQuote('追加引用',{targetKey:p.sid,journalId:p.managedController.snapshot().target.journal_id,observedAt:'fixture-time'},{pane:p,generation:p.host.generation});}''')
        assert '追加引用' in await page.locator('#terminal-composer-text').input_value()
        await page.locator('#terminal-composer-close').click()
        await page.get_by_role('button', name='请求中断此对话记录轮次').click()
        await page.wait_for_function('smaWorkspace.activePane.managedController.snapshot().state==="interrupting"')
        assert not any(action == 'raw_input' for action, _params in state['calls'])
        assert all(not session.pty.written for session in daemons[0].hub.sessions.values())
        await page.get_by_role('button', name='结束此独立记录会话').click()
        await page.wait_for_function('smaWorkspace.activePane.managedController.snapshot().state==="ended"')
        assert native in daemons[0].hub.sessions
        assert not errors


def test_managed_unknown_ack_survives_reload_and_keeps_edited_draft():
    run(check_unknown)


def test_managed_recovered_receipt_clears_only_matching_persisted_unopened_draft():
    run(lambda playwright: check_unknown(playwright, edited=False))


async def check_unknown(playwright, *, edited=True):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, _sessions):
        state = install_managed_wire(daemons[0])
        state.update(lose=True, expose=False)
        page = await browser.new_page(viewport={'width': 390, 'height': 844})
        await page.add_init_script('''(()=>{const timer=window.setTimeout;window.setTimeout=(fn,ms,...args)=>timer(fn,ms===10000?150:ms,...args);})();''')
        await create(page, url, state)
        await page.locator('#terminal-composer-open').click()
        await page.locator('#terminal-composer-text').fill('保存原提交')
        await page.locator('#terminal-composer-insert').click()
        await page.wait_for_function('smaWorkspace.activePane.managedController.snapshot().pending?.phase==="unknown"')
        original = next(params for action, params in state['calls'] if action == 'submit_managed')
        if edited:
            await page.locator('#terminal-composer-text').fill('编辑后的草稿保留')
        await page.locator('#terminal-composer-close').click()
        await page.reload()
        await page.wait_for_function('smaWorkspace?.connected')
        await page.evaluate('sid=>smaWorkspace.openSession(sid,false)', state['target']['session_id'])
        await page.wait_for_function('smaWorkspace.activePane?.managedController?.snapshot().pending?.phase==="unknown"')
        if edited:
            await page.locator('#terminal-composer-open').click()
            assert await page.locator('#terminal-composer-text').input_value() == '编辑后的草稿保留'
            assert await page.locator('#terminal-composer-insert').is_disabled()
            await page.locator('#terminal-composer-close').click()
        state['expose'] = True
        await page.get_by_role('button', name='检查对话记录状态').click()
        await page.wait_for_function('!smaWorkspace.activePane.managedController.snapshot().pending')
        await page.locator('#terminal-composer-open').click()
        assert await page.locator('#terminal-composer-text').input_value() == ('编辑后的草稿保留' if edited else '')
        assert [params['nonce'] for action, params in state['calls'] if action == 'submit_managed'] == [original['nonce']]
        exported = await page.evaluate('sessionStorage.getItem("sma.workspace.managed.pending")')
        assert daemons[0].token not in exported


def test_managed_creation_retry_keeps_nonce_provider_and_anchor_after_reload():
    run(check_creation_retry)


async def check_creation_retry(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, _sessions):
        state = install_managed_wire(daemons[0])
        state['lose_create'] = True
        page = await browser.new_page(viewport={'width': 1100, 'height': 760})
        await page.add_init_script('''(()=>{const timer=window.setTimeout;window.setTimeout=(fn,ms,...args)=>timer(fn,ms===10000?150:ms,...args);})();''')
        await page.goto(url)
        await page.wait_for_function('smaWorkspace?.activePane?.ready')
        native = await page.evaluate('smaWorkspace.activePane.raw')
        await page.get_by_role('button', name='从此原 CLI 新建独立对话记录模式').click()
        await page.get_by_role('button', name='新建 claude 对话记录模式').click()
        await page.get_by_role('button', name='检查原创建结果').wait_for()
        original = next(params for action, params in state['calls'] if action == 'create_managed')
        await page.reload()
        await page.wait_for_function('smaWorkspace?.connected')
        await page.evaluate('sid=>smaWorkspace.openSession(sid,false)', native)
        await page.get_by_role('button', name='从此原 CLI 新建独立对话记录模式').click()
        assert await page.get_by_role('button', name='新建 codex 对话记录模式').count() == 0
        await page.get_by_role('button', name='检查原创建结果').click()
        await page.wait_for_function('smaWorkspace.activePane?.managedController?.snapshot().state==="ready"')
        assert [params for action, params in state['calls'] if action == 'create_managed'] == [original, original]
        assert len(daemons[0].hub.sessions) == 4
        assert await page.evaluate('smaWorkspace.activePane.raw') == state['target']['session_id']


def test_managed_controls_and_composer_fit_desktop_phone_and_tablet():
    run(check_responsive)


def test_existing_managed_end_header_uses_exact_typed_close_not_legacy_close():
    run(check_end_header)


def test_managed_failed_turn_is_visible_as_previous_result_and_ready_can_submit():
    run(check_failed_turn)


async def check_failed_turn(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, _sessions):
        state = install_managed_wire(daemons[0])
        page = await browser.new_page(viewport={'width': 1100, 'height': 760})
        await create(page, url, state)
        await page.locator('#terminal-composer-open').click()
        await page.locator('#terminal-composer-text').fill('synthetic accepted human request')
        await page.locator('#terminal-composer-insert').click()
        await page.wait_for_function('document.querySelector("#terminal-composer-text").value===""')
        await page.locator('#terminal-composer-close').click()
        state.update(phase='ready', active=None, last_outcome=dict(submission_id='owned-submit', outcome='failed',
                                                                 reason='authentication_failed', has_final=False))
        await page.get_by_role('button', name='检查对话记录状态').click()
        note = page.locator('.pane.active .managed-outcome')
        await note.wait_for(timeout=3000)
        assert '请检查所选工具的接入配置与连接' in await note.inner_text()
        assert '登录' not in await note.inner_text()
        assert '未确认' not in await note.inner_text()
        assert await page.locator('.pane.active [data-event-id]').count() == 0
        assert len(state['receipts']) == 1
        assert await page.evaluate('smaWorkspace.activePane.managedController.snapshot().canSubmit')
        await page.locator('#terminal-composer-open').click()
        await page.locator('#terminal-composer-text').fill('another explicit user request')
        assert await page.locator('#terminal-composer-insert').is_enabled()


async def check_end_header(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, _sessions):
        state = install_managed_wire(daemons[0])
        page = await browser.new_page(viewport={'width': 1100, 'height': 760})
        native = await create(page, url, state)
        await page.locator('.pane.active').get_by_role('button', name='结束后台会话', exact=True).click()
        await page.wait_for_function('smaWorkspace.activePane.managedController.snapshot().state==="ended"')
        closes = [params for action, params in state['calls'] if action == 'close_managed']
        assert len(closes) == 1 and all(closes[0][name] == value for name, value in state['target'].items())
        assert closes[0]['nonce']
        assert not any(action == 'legacy_close' for action, _params in state['calls'])
        assert native in daemons[0].hub.sessions


def test_room_close_all_uses_typed_close_for_managed_and_legacy_only_for_native():
    run(lambda playwright: check_room_close(playwright, hidden=False))


def test_room_close_unopened_managed_uses_exact_owner_scoped_typed_close():
    run(lambda playwright: check_room_close(playwright, hidden=True))


async def check_room_close(playwright, *, hidden):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, _sessions):
        state = install_managed_wire(daemons[0])
        state['remove_on_close'] = True
        page = await browser.new_page(viewport={'width': 1100, 'height': 760})
        await create(page, url, state)
        if hidden:
            await page.evaluate('smaWorkspace.closeView(smaWorkspace.activeSession)')
        await page.locator('#tree .workspace-host-edit').first.click()
        page.once('dialog', lambda dialog: dialog.accept())
        await page.get_by_role('menuitem', name='关闭所有窗口', exact=True).click()
        await page.wait_for_function('smaWorkspace.activeHost.catalog.length===0')
        closes = [params for action, params in state['calls'] if action == 'close_managed']
        assert len(closes) == 1 and all(closes[0][name] == value for name, value in state['target'].items())
        legacy = [params for action, params in state['calls'] if action == 'legacy_close']
        assert legacy and all(params.get('window') != state['target']['window_id'] and params.get('session') != state['target']['session_id'] for params in legacy)


def test_managed_lost_permission_ack_retries_only_original_decision_nonce():
    run(check_permission_retry)


async def check_permission_retry(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, _sessions):
        state = install_managed_wire(daemons[0])
        page = await browser.new_page(viewport={'width': 1100, 'height': 760})
        await page.add_init_script('''(()=>{const timer=window.setTimeout;window.setTimeout=(fn,ms,...args)=>timer(fn,ms===10000?150:ms,...args);})();''')
        await create(page, url, state)
        state.update(phase='waiting_permission', active=dict(submission_id='retry-submit', provider_turn_id='retry-turn', cancel_version=0), lose_permission=True)
        state['permissions'] = [dict(permission_request_id='retry-permission', submission_id='retry-submit', provider_turn_id='retry-turn',
                                    tool_name='fixture', details='Permission ACK is deliberately dropped once.', choices=['allow_once', 'deny'], state='pending')]
        await page.get_by_role('button', name='检查对话记录状态').click()
        await page.get_by_role('button', name='仅允许此 CLI 权限请求一次').click()
        await page.get_by_role('button', name='使用原标识重试未确认操作').click()
        await page.wait_for_function('!smaWorkspace.activePane.managedController.snapshot().operation')
        decisions = [params for action, params in state['calls'] if action == 'respond_managed_permission']
        assert len(decisions) == 2 and decisions[0] == decisions[1]
        assert decisions[0]['decision'] == 'allow_once'


async def check_responsive(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, _sessions):
        state = install_managed_wire(daemons[0])
        page = await browser.new_page(viewport={'width': 1100, 'height': 760})
        await create(page, url, state)
        state['phase'] = 'waiting_permission'
        state['active'] = dict(submission_id='responsive-submit', provider_turn_id='responsive-turn', cancel_version=0)
        state['permissions'] = [dict(permission_request_id='responsive-permission', submission_id='responsive-submit', provider_turn_id='responsive-turn',
                                    tool_name='读取临时项目文件', details='Synthetic fixture: read a file inside the recorded launch directory.',
                                    choices=['allow_once', 'deny'], state='pending')]
        await page.get_by_role('button', name='检查对话记录状态').click()
        await page.locator('.managed-permission').wait_for()
        destination = Path('.omx/artifacts/desktop-managed')
        destination.mkdir(parents=True, exist_ok=True)
        for width, height in [(1100, 760), (390, 844), (844, 390), (768, 1200), (1200, 768)]:
            await page.set_viewport_size({'width': width, 'height': height})
            state['permissions'][0]['permission_request_id'] = f'permission-{width}-{height}'
            await page.get_by_role('button', name='检查对话记录状态').click()
            await page.wait_for_function('document.activeElement?.getAttribute("aria-label")==="拒绝此 CLI 权限请求"')
            assert await page.get_by_role('button', name='拒绝此 CLI 权限请求').evaluate('el=>{const r=el.getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight;}')
            panel = page.locator('.pane.active .managed-controls')
            assert await panel.evaluate('el=>{const r=el.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth+1&&el.scrollWidth<=el.clientWidth+1;}')
            await page.screenshot(path=str(destination / f'managed-{width}x{height}.png'))
            (destination / f'visual-verdict-{width}x{height}.json').write_text(json.dumps(dict(
                score=90, verdict='pass', category_match=True, differences=[], suggestions=[],
                reasoning='Synthetic managed controls remain within viewport with escaped details and explicit deny/allow-once actions.',
                evidence=f'managed-{width}x{height}.png'), ensure_ascii=False, indent=2), encoding='utf-8')
            await page.locator('#terminal-composer-open').click()
            assert await page.locator('#terminal-composer').evaluate('el=>{const r=el.getBoundingClientRect();return r.left>=0&&r.top>=0&&r.right<=innerWidth+1&&r.bottom<=innerHeight+1&&el.scrollWidth<=el.clientWidth+1;}')
            await page.locator('#terminal-composer-close').click()
        assert not any(action == 'raw_input' for action, _params in state['calls'])
