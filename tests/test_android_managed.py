"""Actual mobile assets against a synthetic OP24 source; no provider or PTY runs."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import quote

import pytest
from websockets.asyncio.server import serve

from android_journal_fixture import Fixture, TOKEN, ROOM, STAMP

ROOT = Path(__file__).resolve().parents[1]
EDGE = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')


class ManagedFixture(Fixture):
    def __init__(self):
        super().__init__()
        self.created = None
        self.creation_nonces = []
        self.receipts = {}
        self.submissions = []
        self.decisions = []
        self.permissions = []
        self.active_submission = None
        self.managed_state = 'ready'
        self.drop_submit_ack = False
        self.last_outcome = None

    def catalog(self):
        value = super().catalog()
        value['capabilities']['managed_conversation'] = 1
        for window in value['windows']:
            for session in window['sessions']:
                session.update(interaction_mode='native', process_generation='pty-fixture-' + session['journal_id'])
        if self.created:
            target = self.created
            value['windows'].append(dict(id=target['window_id'], title='Synthetic managed Claude', focused=target['session_id'],
                                         sessions=[dict(id=target['session_id'], interaction_mode='managed', provider='claude',
                                                        process_generation=target['process_generation'], journal_id=target['journal_id'],
                                                        managed_state=self.managed_state, cwd=target['cwd'])]))
            value['controls'][target['session_id']] = dict(cols=80, rows=24, owner=False, busy=False)
        return value

    def manifest(self, jid):
        value = super().manifest(jid)
        if self.created and jid == self.created['journal_id']:
            value.update(window_id=self.created['window_id'], session_id=self.created['session_id'],
                         process_generation=self.created['process_generation'], agent='claude', title='Synthetic managed Claude')
        return value

    def query(self, action, params):
        if action == 'create_managed':
            assert set(params) == {'session_id', 'process_generation', 'provider', 'nonce'}
            assert params['session_id'] == 's1' and params['process_generation'] == 'pty-fixture-j1'
            self.creation_nonces.append(params['nonce'])
            if self.created is None:
                self.created = dict(window_id='managed-window', session_id='managed-session', process_generation='managed-generation',
                                    journal_id='managed-journal', interaction_mode='managed', provider=params['provider'], state='ready', cwd='D:\\synthetic-server-directory')
            return self.created
        if action in {'submit_managed', 'managed_status', 'respond_managed_permission', 'interrupt_managed', 'close_managed'}:
            assert self.created and all(params.get(field) == self.created[field] for field in ['window_id', 'session_id', 'process_generation', 'journal_id'])
            target = {field: self.created[field] for field in ['window_id', 'session_id', 'process_generation', 'journal_id']}
            if action == 'managed_status':
                result = dict(**target, state=self.managed_state, provider='claude', provider_version='synthetic-2.1.310',
                              provider_root_id='confirmed-synthetic-root', policy={'permissionMode': 'default'},
                              active_submission=self.active_submission, permissions=self.permissions, last_outcome=self.last_outcome)
                if 'nonce' in params:
                    result['receipt'] = self.receipts.get(params['nonce'])
                return result
            if action == 'submit_managed':
                nonce = params['nonce']
                if nonce not in self.receipts:
                    assert len(params['text'].encode()) <= 65536
                    self.submissions.append(params.copy())
                    self.receipts[nonce] = dict(**target, nonce=nonce, submission_id='accepted-submission', event_id='accepted-event', seq=5, state='accepted', dispatch_state='pending')
                    self.active_submission = dict(submission_id='accepted-submission', provider_turn_id='synthetic-turn', cancel_version=0)
                    self.managed_state = 'waiting_permission'
                    self.permissions = [dict(permission_request_id='actual-fake-runtime-request', submission_id='accepted-submission',
                                             provider_turn_id='synthetic-turn', state='pending', tool_name='SyntheticTool <script>',
                                             details='<img src="https://must-not-load.invalid/pixel">\n真实夹具权限请求，不会执行工具。', choices=['allow_once', 'deny'])]
                return self.receipts[nonce]
            if action == 'respond_managed_permission':
                assert params['permission_request_id'] == 'actual-fake-runtime-request'
                assert params['decision'] in {'allow_once', 'deny'}
                self.decisions.append(params.copy()); self.permissions = []; self.managed_state = 'running'
                return dict(permission_request_id=params['permission_request_id'], decision=params['decision'], state='decided')
            if action == 'interrupt_managed':
                self.managed_state = 'interrupting'; self.permissions = []
                return dict(submission_id='accepted-submission', state='interrupt_requested', cancel_version=1)
            self.managed_state = 'ended'; self.permissions = []; self.active_submission = None
            return dict(**target, state='ended')
        jid = params.get('journal_id')
        if self.created and (jid == self.created['journal_id'] or params.get('session_id') == self.created['session_id']):
            if action == 'status':
                return self.manifest(self.created['journal_id'])
            if action == 'read_page':
                events = self.events(self.created['journal_id'])
                if params.get('projection') == 'conversation':
                    events = [row for row in events if row['kind'] in {'user_input', 'assistant_final'}]
                return dict(journal=self.manifest(self.created['journal_id']), events=events, next_cursor=None)
            if action == 'read_content':
                body = self.contents[params['content_id']]; offset = params.get('offset', 0); chunk = body[offset:offset + 49152]
                end = offset + len(chunk)
                return dict(content_id=params['content_id'], offset=offset, data_base64=base64.b64encode(chunk).decode(),
                            total_bytes=len(body), sha256=hashlib.sha256(body).hexdigest(), next_offset=end if end < len(body) else None)
        return super().query(action, params)

    def response(self, value):
        result = super().response(value)
        if self.drop_submit_ack and value.get('action') == 'submit_managed':
            result['request_id'] = 'unmatched-lost-ack'
        return result


class FailedManagedFixture(ManagedFixture):
    """A committed human input with a provider failure and no assistant final."""
    def manifest(self, jid):
        value = super().manifest(jid)
        if self.created and jid == self.created['journal_id']:
            value['last_seq'] = 5 if self.submissions else 0
        return value

    def query(self, action, params):
        if action == 'read_page' and self.created and params.get('journal_id') == self.created['journal_id']:
            events = [dict(event_id='accepted-event', seq=5, kind='user_input', actor='human', origin='app',
                           state='accepted', proof={'kind': 'app_submission'}, observed_at=STAMP,
                           preview=entry['text']) for entry in self.submissions]
            return dict(journal=self.manifest(self.created['journal_id']), events=events, next_cursor=None)
        result = super().query(action, params)
        if action == 'submit_managed':
            self.managed_state = 'ready'; self.active_submission = None; self.permissions = []
            self.last_outcome = dict(submission_id=result['submission_id'], outcome='failed', reason='provider_failed', has_final=False)
        return result


def test_actual_android_managed_creation_submit_permissions_recovery_and_native_input():
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Owned headless Edge is unavailable')
    asyncio.run(check_managed(playwright))


async def check_managed(playwright):
    fixture = ManagedFixture()
    async with serve(fixture.socket, '127.0.0.1', 0, process_request=fixture.http) as server:
        fixture.origin = 'http://127.0.0.1:' + str(server.sockets[0].getsockname()[1])
        config = dict(endpoint=fixture.origin, code=ROOM, token=TOKEN, renderer='dom', uiRevision='managed-synthetic')
        document = fixture.origin + '/mobile/index.html#' + quote(json.dumps(config))
        async with playwright.async_playwright() as runtime:
            browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
            try:
                page = await browser.new_page(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
                errors, external = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: external.append(request.url) if 'must-not-load' in request.url else None)
                await page.goto(document)
                await page.wait_for_function('window.MobileTerminal?.ready')
                await page.locator('#sessions').click()
                await page.locator('#new-interaction-mode').select_option('managed')
                await page.locator('#managed-provider').select_option('claude')
                await page.locator('#new').click()
                await page.wait_for_function('MobileTerminal.active === "managed-session"')
                await page.wait_for_function('!document.getElementById("quick-send").disabled')
                assert len(fixture.creation_nonces) == 1
                assert await page.evaluate('MobileTerminal.terminal.options.disableStdin')
                assert await page.locator('#raw-input').is_hidden()
                assert await page.evaluate('MobileTerminal.terminal.textarea.readOnly')
                assert 'confirmed-synthetic-root' in await page.locator('#managed-identity').inner_text()
                assert '不是实时工作目录' in await page.locator('#managed-identity').inner_text()
                assert await page.locator('#model').is_disabled()
                assert await page.locator('#attach-file').is_disabled()
                assert await page.locator('[data-key="enter"]').is_disabled()
                await page.wait_for_function('!document.getElementById("journal-reading").hidden')
                artifact_root = ROOT / '.omx/artifacts/android-managed'
                artifacts = artifact_root / ('run-' + str(time.time_ns()))
                artifacts.mkdir(parents=True, exist_ok=True)
                verdicts = []
                def persist_verdict(permission_verdicts=None):
                    data = dict(scope='android-managed', surface='actual assets / synthetic OP24 source', physical_android=False,
                                verdicts=verdicts, permission_verdicts=permission_verdicts or [])
                    (artifacts / 'visual-verdict.json').write_text(json.dumps(data, indent=2), encoding='utf-8')
                    latest = json.loads(json.dumps(data))
                    for entry in latest['verdicts'] + latest['permission_verdicts']:
                        entry['screenshot'] = artifacts.name + '/' + entry['screenshot']
                    (artifact_root / 'visual-verdict.json').write_text(json.dumps(latest, indent=2), encoding='utf-8')
                for width, height in [(390, 844), (844, 390), (768, 1200), (1200, 768)]:
                    await page.set_viewport_size({'width': width, 'height': height})
                    await page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
                    bounds = await page.evaluate('''() => {
                        const ids=['managed-panel','quick-composer','journal-actions'];
                        const inside=ids.every(id=>{const r=document.getElementById(id).getBoundingClientRect();return r.left>=0&&r.top>=0&&r.right<=innerWidth+1&&r.bottom<=innerHeight+1;});
                        const first=document.querySelector('#journal-reading article');
                        const raw=[...document.querySelectorAll('#journal-reading button')].find(button=>button.textContent==='原始终端');
                        const r=raw?.getBoundingClientRect();
                        return {inside,overflow:document.documentElement.scrollWidth>innerWidth,
                            cardsHeight:first?.parentElement.clientHeight||0,rawEscapeVisible:!!r&&r.top>=0&&r.bottom<=innerHeight,
                            observationalReadonly:MobileTerminal.terminal.options.disableStdin};
                    }''')
                    image = f'managed-{width}x{height}.png'
                    await page.screenshot(path=str(artifacts / image))
                    passed = bounds['inside'] and not bounds['overflow'] and bounds['cardsHeight'] >= 32 and bounds['rawEscapeVisible'] and bounds['observationalReadonly']
                    verdicts.append(dict(viewport=dict(width=width, height=height), screenshot=image, verdict='pass' if passed else 'fail', evidence=bounds))
                    persist_verdict()
                    assert passed, bounds
                await page.set_viewport_size({'width': 390, 'height': 844})
                await page.locator('#managed-edit').click()
                await page.locator('#draft').fill('候选中文')
                await page.locator('#draft').dispatch_event('compositionstart')
                await page.locator('#draft').dispatch_event('keydown', {'key': 'Enter', 'keyCode': 229, 'isComposing': True})
                assert not fixture.submissions
                await page.locator('#draft').dispatch_event('compositionend')
                await page.locator('#draft').fill('第一行')
                await page.locator('#draft').press('Shift+Enter')
                assert await page.locator('#draft').input_value() == '第一行\n'
                await page.locator('#draft').fill('结构化原文 😀\n保留换行')
                await page.locator('#draft').dispatch_event('keydown', {'key': 'Enter', 'keyCode': 229, 'isComposing': False})
                await page.wait_for_function('document.getElementById("draft").value === ""')
                assert len(fixture.submissions) == 1
                assert fixture.submissions[0]['text'] == '结构化原文 😀\n保留换行'
                await page.get_by_role('button', name='仅允许本次', exact=True).wait_for()
                await page.locator('#composer-close').click()
                assert await page.evaluate('document.activeElement?.dataset.decision') == 'deny'
                assert not external
                permission_verdicts = []
                for width, height in [(390, 844), (844, 390)]:
                    await page.set_viewport_size({'width': width, 'height': height})
                    await page.get_by_role('button', name='拒绝', exact=True).scroll_into_view_if_needed()
                    safe = await page.evaluate('''() => [...document.querySelectorAll('.managed-permission button')].every(button=>{
                        const r=button.getBoundingClientRect();return r.left>=0&&r.top>=0&&r.right<=innerWidth+1&&r.bottom<=innerHeight+1;
                    }) && !document.querySelector('.managed-permission img') && document.documentElement.scrollWidth<=innerWidth''')
                    image = f'permission-{width}x{height}.png'
                    await page.screenshot(path=str(artifacts / image))
                    permission_verdicts.append(dict(viewport=dict(width=width, height=height), screenshot=image, verdict='pass' if safe else 'fail', evidence='Only escaped actual request details and explicit deny/allow-once buttons; scroll contained.'))
                    persist_verdict(permission_verdicts)
                    assert safe
                await page.set_viewport_size({'width': 390, 'height': 844})
                await page.get_by_role('button', name='仅允许本次', exact=True).click()
                async with asyncio.timeout(5):
                    while not fixture.decisions:
                        await asyncio.sleep(.01)
                assert len(fixture.decisions) == 1 and fixture.decisions[0]['decision'] == 'allow_once'
                assert fixture.input_frames == 0
                await page.locator('#managed-interrupt').click()
                await page.wait_for_function('document.getElementById("managed-state").textContent.includes("请求中断")')
                fixture.managed_state = 'ready'; fixture.active_submission = None
                await page.locator('#managed-reconcile').click()
                await page.wait_for_function('!document.getElementById("quick-send").disabled')
                fixture.drop_submit_ack = True
                await page.locator('#quick-draft').fill('丢失 ACK 的不可变原文')
                await page.locator('#quick-send').click()
                async with asyncio.timeout(5):
                    while len(fixture.submissions) != 2:
                        await asyncio.sleep(.01)
                await page.evaluate('MobileTerminal.pause()')
                state = await page.evaluate('MobileTerminal.captureUIState()')
                assert '丢失 ACK' in json.dumps(state, ensure_ascii=False)
                assert TOKEN not in json.dumps(state)
                fixture.drop_submit_ack = False
                await page.goto(document.replace('/mobile/index.html#', '/mobile/index.html?reload=2#'))
                await page.wait_for_function('window.MobileTerminal?.ready')
                await page.evaluate('state=>MobileTerminal.restoreUIState(state)', state)
                await page.wait_for_function('MobileTerminal.active === "managed-session" && document.getElementById("draft").value === ""')
                assert len(fixture.submissions) == 2
                assert await page.evaluate('MobileTerminal.terminal.options.disableStdin')
                await page.locator('#sessions').click()
                await page.locator('#session-list .session[data-session="s1"]').click()
                await page.wait_for_function('MobileTerminal.active === "s1" && MobileTerminal.ready')
                assert not await page.locator('#quick-draft').evaluate('node=>node.readOnly')
                if await page.locator('[data-key="enter"]').is_hidden():
                    await page.locator('#tools-toggle').click()
                await page.locator('[data-key="enter"]').click()
                async with asyncio.timeout(5):
                    while fixture.input_frames != 1:
                        await asyncio.sleep(.01)
                assert not errors, errors
                await page.evaluate('MobileTerminal.pause()')
            finally:
                await browser.close()


def test_actual_android_prior_provider_failure_keeps_input_and_ready_without_fake_reply():
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Owned headless Edge is unavailable')
    asyncio.run(check_prior_failure(playwright))


async def check_prior_failure(playwright):
    fixture = FailedManagedFixture()
    async with serve(fixture.socket, '127.0.0.1', 0, process_request=fixture.http) as server:
        fixture.origin = 'http://127.0.0.1:' + str(server.sockets[0].getsockname()[1])
        config = dict(endpoint=fixture.origin, code=ROOM, token=TOKEN, renderer='dom', uiRevision='managed-failure-synthetic')
        document = fixture.origin + '/mobile/index.html#' + quote(json.dumps(config))
        async with playwright.async_playwright() as runtime:
            browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
            try:
                page = await browser.new_page(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.goto(document)
                await page.wait_for_function('window.MobileTerminal?.ready')
                await page.locator('#sessions').click()
                await page.locator('#new-interaction-mode').select_option('managed')
                await page.locator('#new').click()
                await page.wait_for_function('MobileTerminal.active === "managed-session" && !document.getElementById("quick-send").disabled')
                text = '已接收的人类原文 中文😀；提供方失败也必须保留。'
                await page.locator('#quick-draft').fill(text)
                await page.locator('#quick-send').click()
                await page.wait_for_function('document.getElementById("draft").value === ""')
                await page.wait_for_function('document.getElementById("managed-outcome").textContent.includes("请检查所选工具的接入配置与连接")')
                await page.wait_for_function('!document.getElementById("quick-send").disabled && document.querySelectorAll("#journal-reading article").length === 1')
                assert fixture.submissions[0]['text'] == text
                assert len(fixture.receipts) == 1 and next(iter(fixture.receipts.values()))['state'] == 'accepted'
                assert fixture.last_outcome['has_final'] is False
                assert await page.locator('#quick-draft').evaluate('node=>!node.readOnly')
                assert '可发送' in await page.locator('#managed-state').inner_text()
                assert '消息已持久接收' in await page.locator('#managed-message').inner_text()
                assert text in await page.locator('#journal-reading article').inner_text()
                assert '助手' not in await page.locator('#journal-reading article').inner_text()
                assert '请检查所选工具的接入配置与连接' not in await page.locator('#journal-reading article').inner_text()
                assert '原输入已保留' in await page.locator('#managed-outcome').inner_text()
                assert await page.locator('#notice').is_hidden()
                artifacts = ROOT / '.omx/artifacts/android-managed' / ('negative-' + str(time.time_ns()))
                artifacts.mkdir(parents=True, exist_ok=True)
                verdicts = []
                for width, height in [(390, 844), (844, 390)]:
                    await page.set_viewport_size({'width': width, 'height': height})
                    await page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
                    contained = await page.locator('#managed-outcome').evaluate('node=>{const r=node.getBoundingClientRect();return !node.hidden&&r.top>=0&&r.bottom<=innerHeight&&r.right<=innerWidth+1;}')
                    image = f'prior-failure-{width}x{height}.png'
                    await page.screenshot(path=str(artifacts / image))
                    verdicts.append(dict(viewport={'width': width, 'height': height}, screenshot=image, verdict='pass' if contained else 'fail', evidence='Prior failure is status-only; accepted input remains the only conversation card, ready composer enabled, no toast.'))
                    (artifacts / 'visual-verdict.json').write_text(json.dumps(dict(scope='android-managed-prior-outcome', physical_android=False, verdicts=verdicts), indent=2), encoding='utf-8')
                    assert contained
                assert fixture.input_frames == 0 and not errors, errors
                await page.evaluate('MobileTerminal.pause()')
            finally:
                await browser.close()
