"""Assistant UI uses fake native model APIs and fixture-owned terminals only."""
from pathlib import Path

import pytest

from test_workspace_profile_rooms import fixture, run


ROOT = Path('src/supermonoagent/static')


async def prepare(browser, url, sessions, *, gated=False, auto=False):
    page = await browser.new_page(viewport={'width': 1250, 'height': 850})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    await page.goto(url)
    await page.wait_for_function('smaWorkspace?.connected&&smaWorkspace.panes.size===2')
    await page.evaluate('sid=>smaWorkspace.openHostSession("local",sid,false)', sessions[0][0].id)
    await page.evaluate('''options=>{
      window.chatCalls=[];window.chatRequests=[];window.chatCancelCount=0;window.chatCancelIds=[];window.chatAutoContext=options.auto;
      window.pywebview={api:{
        recommend_skills:async()=>({available:true,indexed:0,skills:[]}),
        get_assistant_preferences:async()=>({preferences:{auto_context:chatAutoContext},status:'saved'}),
        assistant_chat:payload=>{chatCalls.push(payload);return options.gated?new Promise(resolve=>chatRequests.push({payload,resolve})):Promise.resolve({text:'<img src=x onerror=window.chatInjected=true>',provider:'fixture',model:'fake-model',context_used_chars:payload.context.length,context_truncated:false});},
        cancel_assistant_chat:async requestId=>{chatCancelCount++;chatCancelIds.push(requestId);}
      }};
    }''', {'gated': gated, 'auto': auto})
    await page.evaluate((ROOT / 'mylib-assistant.js').read_text(encoding='utf-8'))
    await page.evaluate((ROOT / 'assistant-chat.js').read_text(encoding='utf-8'))
    await page.locator('#mylib-toggle').click()
    await page.locator('#assistant-chat-question').wait_for(state='visible')
    return page, errors


def assert_no_input(sessions):
    assert not any(session.pty.written for owned in sessions for session in owned)


def test_chat_defaults_to_no_context_escapes_response_and_limits_history(tmp_path):
    run(lambda playwright: check_success(playwright, tmp_path))


async def check_success(playwright, tmp_path):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons, _ops, _windows, sessions):
        page, errors = await prepare(browser, url, sessions)
        sessions[0][0].pty.inbox.put(b'\r\nOWN_FIXTURE_CONTEXT\r\n')
        await page.wait_for_function('()=>{const b=smaWorkspace.terminal.buffer.active;return Array.from({length:b.length},(_,i)=>b.getLine(i)?.translateToString()).some(line=>line?.includes("OWN_FIXTURE_CONTEXT"));}')
        assert not await page.locator('#assistant-chat-context').is_checked()
        await page.locator('#assistant-chat-question').fill('Review this task')
        await page.locator('#assistant-chat-send').click()
        await page.wait_for_function('chatCalls.length===1&&!document.querySelector("#assistant-chat-send").disabled')
        first = await page.evaluate('chatCalls[0]')
        assert isinstance(first['request_id'], str) and len(first['request_id']) <= 64
        assert first['question'] == 'Review this task'
        assert first['context'] == '' and first['attach_context'] is False
        assert first['history'] == [] and first['redactions'] == ['profile-owner-0']
        assert await page.locator('#assistant-chat-history img').count() == 0
        assert '<img src=x' in await page.locator('#assistant-chat-history').inner_text()
        assert not await page.evaluate('window.chatInjected===true')
        await page.locator('#assistant-chat-context').check()
        for i in range(5):
            await page.locator('#assistant-chat-question').fill('Question ' + str(i))
            await page.locator('#assistant-chat-send').click()
            await page.wait_for_function('expected=>chatCalls.length===expected&&!document.querySelector("#assistant-chat-send").disabled', arg=i + 2)
        last = await page.evaluate('chatCalls[chatCalls.length-1]')
        assert last['attach_context'] and 'OWN_FIXTURE_CONTEXT' in last['context']
        assert len(last['history']) == 6
        assert all(message['role'] in {'user', 'assistant'} and len(message['content']) <= 4000 for message in last['history'])
        assert await page.locator('#assistant-chat-history article').count() == 8
        # Analysis is an explicit request to include output; preserve the draft.
        await page.locator('#assistant-chat-context').uncheck()
        await page.locator('#assistant-chat-question').fill('Keep this unsent draft')
        await page.locator('#assistant-chat-analyze').click()
        await page.wait_for_function('chatCalls.length===7&&!document.querySelector("#assistant-chat-send").disabled')
        assert await page.locator('#assistant-chat-question').input_value() == 'Keep this unsent draft'
        assert await page.evaluate('chatCalls[6].attach_context')
        await page.set_viewport_size({'width': 430, 'height': 850})
        assert await page.locator('#assistant-chat-question').evaluate('(el)=>{const r=el.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth;}')
        await page.locator('#assistant-chat-section').screenshot(path=str(tmp_path / 'assistant-chat-mobile.png'))
        assert_no_input(sessions)
        assert not errors


def test_drafts_remain_per_window_and_submit_detects_unpolled_switch():
    run(check_drafts)


async def check_drafts(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons, _ops, _windows, sessions):
        page, errors = await prepare(browser, url, sessions)
        first = await page.evaluate('smaWorkspace.activeSession')
        second = await page.evaluate('[...smaWorkspace.panes.keys()].find(id=>id!==smaWorkspace.activeSession)')
        await page.locator('#assistant-chat-question').fill('Draft A')
        await page.evaluate('id=>smaWorkspace.openSession(id,false)', second)
        await page.wait_for_function('document.querySelector("#assistant-chat-question").value===""')
        await page.locator('#assistant-chat-question').fill('Draft B')
        await page.evaluate('id=>smaWorkspace.openSession(id,false)', first)
        await page.wait_for_function('document.querySelector("#assistant-chat-question").value==="Draft A"')
        # Change selection and submit in the same JS turn, before the 300ms poll.
        await page.evaluate('''id=>{smaWorkspace.openSession(id,false);document.querySelector('#assistant-chat-form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));}''', second)
        assert await page.evaluate('chatCalls.length') == 0
        assert await page.locator('#assistant-chat-question').input_value() == 'Draft B'
        assert '窗口已切换' in await page.locator('#assistant-chat-status').inner_text()
        await page.locator('#assistant-chat-send').click()
        await page.wait_for_function('chatCalls.length===1&&!document.querySelector("#assistant-chat-send").disabled')
        assert await page.evaluate('chatCalls[0].question') == 'Draft B'
        await page.evaluate('id=>smaWorkspace.openSession(id,false)', first)
        await page.wait_for_function('document.querySelector("#assistant-chat-question").value==="Draft A"')
        assert 'Draft B' not in await page.locator('#assistant-chat-history').inner_text()
        assert_no_input(sessions)
        assert not errors


def test_old_reply_stays_with_original_window_and_cancel_drops_late_answer():
    run(check_late_reply)


async def check_late_reply(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons, _ops, _windows, sessions):
        page, errors = await prepare(browser, url, sessions, gated=True)
        first = await page.evaluate('smaWorkspace.activeSession')
        second = await page.evaluate('[...smaWorkspace.panes.keys()].find(id=>id!==smaWorkspace.activeSession)')
        await page.locator('#assistant-chat-question').fill('Question A')
        await page.locator('#assistant-chat-send').click()
        await page.wait_for_function('chatRequests.length===1')
        await page.evaluate('id=>smaWorkspace.openSession(id,false)', second)
        await page.wait_for_function('document.querySelector("#assistant-chat-question").value===""&&document.querySelector("#assistant-chat-status").textContent.includes("另一窗口")')
        await page.evaluate('chatRequests[0].resolve({text:"ANSWER_FOR_A",provider:"fixture",model:"fake",context_used_chars:0,context_truncated:false})')
        await page.wait_for_function('!document.querySelector("#assistant-chat-send").disabled')
        assert 'ANSWER_FOR_A' not in await page.locator('#assistant-chat-history').inner_text()
        await page.evaluate('id=>smaWorkspace.openSession(id,false)', first)
        await page.wait_for_function('document.querySelector("#assistant-chat-history").textContent.includes("ANSWER_FOR_A")')
        await page.locator('#assistant-chat-question').fill('Cancel this request')
        await page.locator('#assistant-chat-send').click()
        await page.wait_for_function('chatRequests.length===2')
        await page.locator('#assistant-chat-cancel').click()
        assert await page.evaluate('chatCancelCount') == 1
        assert await page.evaluate('chatCancelIds[0]===chatCalls[1].request_id')
        assert await page.locator('#assistant-chat-send').is_disabled()
        await page.evaluate('chatRequests[1].resolve({text:"CANCELLED_ANSWER",provider:"fixture",model:"fake",context_used_chars:0,context_truncated:false})')
        await page.wait_for_function('!document.querySelector("#assistant-chat-send").disabled')
        assert 'CANCELLED_ANSWER' not in await page.locator('#assistant-chat-history').inner_text()
        assert '已取消' in await page.locator('#assistant-chat-status').inner_text()
        assert_no_input(sessions)


def test_credential_rotation_clears_history_and_drops_pending_reply():
    run(check_credential_rotation)


async def check_credential_rotation(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons, _ops, _windows, sessions):
        page, errors = await prepare(browser, url, sessions, gated=True)
        token_a, token_b = 'profile-owner-0', 'fixture-ui-token-B'
        await page.locator('#assistant-chat-question').fill('Question with ' + token_a)
        await page.locator('#assistant-chat-send').click()
        await page.wait_for_function('chatRequests.length===1')
        assert token_a not in await page.locator('#assistant-chat-history').inner_text()
        assert token_a not in await page.evaluate('chatCalls[0].question')
        await page.evaluate('token=>chatRequests[0].resolve({text:"echo "+token,provider:"fixture",model:"fake",context_used_chars:0,context_truncated:false})', token_a)
        await page.wait_for_function('!document.querySelector("#assistant-chat-send").disabled')
        assert token_a not in await page.locator('#assistant-chat-history').inner_text()
        await page.locator('#assistant-chat-question').fill('Pending ' + token_a)
        await page.locator('#assistant-chat-send').click()
        await page.wait_for_function('chatRequests.length===2')
        assert all(token_a not in message['content'] for message in await page.evaluate('chatCalls[1].history'))
        await page.locator('#assistant-chat-question').fill('Keep draft ' + token_a)
        await page.evaluate('token=>{smaWorkspace.activeHost.config.token=token;}', token_b)
        await page.wait_for_function('chatCancelCount===1&&document.querySelector("#assistant-chat-history article")==null')
        assert token_a not in await page.locator('#assistant-chat-question').input_value()
        await page.evaluate('token=>chatRequests[1].resolve({text:"OLD_CREDENTIAL_REPLY "+token,provider:"fixture",model:"fake",context_used_chars:0,context_truncated:false})', token_a)
        await page.wait_for_function('!document.querySelector("#assistant-chat-send").disabled')
        assert 'OLD_CREDENTIAL_REPLY' not in await page.locator('#assistant-chat-history').inner_text()
        await page.locator('#assistant-chat-question').fill('Fresh identity question')
        await page.locator('#assistant-chat-send').click()
        await page.wait_for_function('chatRequests.length===3')
        fresh = await page.evaluate('chatCalls[2]')
        assert fresh['history'] == [] and fresh['redactions'] == [token_b]
        await page.evaluate('chatRequests[2].resolve({text:"new identity",provider:"fixture",model:"fake",context_used_chars:0,context_truncated:false})')
        await page.wait_for_function('!document.querySelector("#assistant-chat-send").disabled')
        assert_no_input(sessions)
        assert not errors


def test_real_same_pane_reconnect_with_rotated_owner_clears_old_conversation():
    run(check_rotated_reconnect)


async def check_rotated_reconnect(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, sessions):
        page, errors = await prepare(browser, url, sessions, gated=True)
        await page.locator('#assistant-chat-question').fill('Old credential profile-owner-0')
        await page.locator('#assistant-chat-send').click()
        await page.wait_for_function('chatRequests.length===1')
        await page.evaluate('chatRequests[0].resolve({text:"old conversation",provider:"fixture",model:"fake",context_used_chars:0,context_truncated:false})')
        await page.wait_for_function('!document.querySelector("#assistant-chat-send").disabled')
        await page.locator('#assistant-chat-question').fill('Old pending request')
        await page.locator('#assistant-chat-send').click()
        await page.wait_for_function('chatRequests.length===2')
        # Rotate the fixture daemon's owner and its existing window groups;
        # the real WebSocket then authenticates the new credential on reconnect.
        new_token = 'fixture-real-rotated-owner'
        previous = daemons[0].token
        daemons[0].token = new_token
        daemons[0].hub.default_owner = new_token
        for group in daemons[0].hub.groups.values():
            if group.owner == previous:
                group.owner = new_token
        await page.evaluate('token=>{window.chatOriginalPane=smaWorkspace.activePane;window.chatOriginalGeneration=smaWorkspace.activeHost.generation;smaWorkspace.activeHost.config.token=token;smaWorkspace.activeHost.ws.close();}', new_token)
        await page.wait_for_function('smaWorkspace.activeHost.connected&&smaWorkspace.activeHost.generation>chatOriginalGeneration&&smaWorkspace.activePane===chatOriginalPane')
        await page.wait_for_function('chatCancelCount===1&&document.querySelector("#assistant-chat-history article")==null')
        await page.evaluate('chatRequests[1].resolve({text:"STALE_AFTER_ROTATION",provider:"fixture",model:"fake",context_used_chars:0,context_truncated:false})')
        await page.wait_for_function('!document.querySelector("#assistant-chat-send").disabled')
        assert 'STALE_AFTER_ROTATION' not in await page.locator('#assistant-chat-history').inner_text()
        await page.locator('#assistant-chat-question').fill('New authenticated question')
        await page.locator('#assistant-chat-send').click()
        await page.wait_for_function('chatRequests.length===3')
        assert await page.evaluate('chatCalls[2].history') == []
        assert await page.evaluate('chatCalls[2].redactions') == [new_token]
        await page.evaluate('chatRequests[2].resolve({text:"fresh",provider:"fixture",model:"fake",context_used_chars:0,context_truncated:false})')
        await page.wait_for_function('!document.querySelector("#assistant-chat-send").disabled')
        assert_no_input(sessions)
        assert not errors


def test_inactive_thread_rebinds_and_redacts_old_draft_when_reopened():
    run(check_inactive_rotation)


async def check_inactive_rotation(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons, _ops, _windows, sessions):
        page, errors = await prepare(browser, url, sessions)
        first = await page.evaluate('smaWorkspace.activeSession')
        second = await page.evaluate('[...smaWorkspace.panes.keys()].find(id=>id!==smaWorkspace.activeSession)')
        await page.locator('#assistant-chat-question').fill('Previously sent profile-owner-0')
        await page.locator('#assistant-chat-send').click()
        await page.wait_for_function('chatCalls.length===1&&!document.querySelector("#assistant-chat-send").disabled')
        await page.locator('#assistant-chat-question').fill('Unsent profile-owner-0')
        await page.evaluate('id=>smaWorkspace.openSession(id,false)', second)
        await page.wait_for_function('document.querySelector("#assistant-chat-question").value===""')
        await page.evaluate('smaWorkspace.activeHost.config.token="inactive-new-credential"')
        await page.wait_for_function('document.querySelector("#assistant-chat-status").textContent.includes("连接身份已变化")')
        await page.evaluate('id=>smaWorkspace.openSession(id,false)', first)
        await page.wait_for_function('document.querySelector("#assistant-chat-question").value.includes("Unsent")')
        assert 'profile-owner-0' not in await page.locator('#assistant-chat-question').input_value()
        assert await page.locator('#assistant-chat-history article').count() == 0
        await page.locator('#assistant-chat-send').click()
        await page.wait_for_function('chatCalls.length===2&&!document.querySelector("#assistant-chat-send").disabled')
        assert await page.evaluate('chatCalls[1].history') == []
        assert 'profile-owner-0' not in await page.evaluate('chatCalls[1].question')
        assert_no_input(sessions)
        assert not errors


def test_cancel_passes_same_request_id_before_native_request_registration():
    run(check_early_targeted_cancel)


async def check_early_targeted_cancel(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons, _ops, _windows, sessions):
        page, errors = await prepare(browser, url, sessions)
        await page.evaluate('''()=>{
          window.nativeRegistered=0;window.cancelledRequestIds=new Set();
          pywebview.api.assistant_chat=payload=>{chatCalls.push(payload);return new Promise(resolve=>{window.registerNativeRequest=()=>{nativeRegistered++;resolve({text:cancelledRequestIds.has(payload.request_id)?'CANCELLED_BEFORE_REGISTRATION':'wrong request',provider:'fixture',model:'fake',context_used_chars:0,context_truncated:false});};});};
          pywebview.api.cancel_assistant_chat=async id=>{chatCancelCount++;chatCancelIds.push(id);cancelledRequestIds.add(id);};
        }''')
        await page.locator('#assistant-chat-question').fill('Cancel before registration')
        await page.locator('#assistant-chat-send').click()
        await page.wait_for_function('chatCalls.length===1')
        await page.locator('#assistant-chat-cancel').click()
        assert await page.evaluate('nativeRegistered') == 0
        assert await page.evaluate('chatCancelIds[0]===chatCalls[0].request_id')
        request_id = await page.evaluate('chatCalls[0].request_id')
        assert request_id and len(request_id) <= 64
        assert all(char.isascii() and (char.isalnum() or char in '_-') for char in request_id)
        await page.evaluate('registerNativeRequest()')
        await page.wait_for_function('!document.querySelector("#assistant-chat-send").disabled')
        assert 'CANCELLED_BEFORE_REGISTRATION' not in await page.locator('#assistant-chat-history').inner_text()
        assert_no_input(sessions)
        assert not errors
        assert not errors


@pytest.mark.parametrize('boundary', ['close', 'reconnect'])
def test_closed_or_reconnected_source_drops_old_reply(boundary):
    run(lambda playwright: check_connection_boundary(playwright, boundary))


async def check_connection_boundary(playwright, boundary):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons, _ops, _windows, sessions):
        page, errors = await prepare(browser, url, sessions, gated=True)
        await page.locator('#assistant-chat-question').fill('Old connection question')
        await page.locator('#assistant-chat-send').click()
        await page.wait_for_function('chatRequests.length===1')
        if boundary == 'close':
            await page.evaluate('smaWorkspace.closeView(smaWorkspace.activeSession)')
        else:
            await page.evaluate('()=>{window.chatOldGeneration=smaWorkspace.activeHost.generation;smaWorkspace.activeHost.ws.close();}')
            await page.wait_for_function('smaWorkspace.activeHost.connected&&smaWorkspace.activeHost.generation>chatOldGeneration')
        await page.wait_for_function('chatCancelCount===1')
        await page.evaluate('chatRequests[0].resolve({text:"INVALID_OLD_REPLY",provider:"fixture",model:"fake",context_used_chars:0,context_truncated:false})')
        await page.wait_for_function('!document.querySelector("#assistant-chat-send").disabled')
        assert 'INVALID_OLD_REPLY' not in await page.locator('#assistant-chat-history').inner_text()
        assert_no_input(sessions)
        assert not errors


def test_preferences_change_clears_history_cancels_pending_and_preserves_draft():
    run(check_preferences)


async def check_preferences(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons, _ops, _windows, sessions):
        page, errors = await prepare(browser, url, sessions, gated=True, auto=True)
        await page.wait_for_function('document.querySelector("#assistant-chat-context").checked')
        await page.locator('#assistant-chat-question').fill('Previous question')
        await page.locator('#assistant-chat-send').click()
        await page.wait_for_function('chatRequests.length===1')
        await page.evaluate('chatRequests[0].resolve({text:"PREVIOUS_ANSWER",provider:"fixture",model:"fake",context_used_chars:0,context_truncated:false})')
        await page.wait_for_function('!document.querySelector("#assistant-chat-send").disabled')
        await page.locator('#assistant-chat-question').fill('Pending question')
        await page.locator('#assistant-chat-send').click()
        await page.wait_for_function('chatRequests.length===2')
        await page.locator('#assistant-chat-question').fill('Keep this new draft')
        await page.evaluate('()=>{chatAutoContext=false;dispatchEvent(new Event("mylib-preferences-changed"));}')
        await page.wait_for_function('chatCancelCount===1&&!document.querySelector("#assistant-chat-context").checked')
        assert 'PREVIOUS_ANSWER' not in await page.locator('#assistant-chat-history').inner_text()
        assert await page.locator('#assistant-chat-question').input_value() == 'Keep this new draft'
        await page.evaluate('chatRequests[1].resolve({text:"OLD_SETTINGS_REPLY",provider:"fixture",model:"fake",context_used_chars:0,context_truncated:false})')
        await page.wait_for_function('!document.querySelector("#assistant-chat-send").disabled')
        assert 'OLD_SETTINGS_REPLY' not in await page.locator('#assistant-chat-history').inner_text()
        assert '新对话' in await page.locator('#assistant-chat-status').inner_text()
        assert_no_input(sessions)
        assert not errors


def test_chat_waits_for_native_ready_and_duplicate_script_is_harmless():
    run(check_ready)


async def check_ready(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons, _ops, _windows, sessions):
        page = await browser.new_page(viewport={'width': 1250, 'height': 850})
        await page.goto(url)
        await page.wait_for_function('smaWorkspace?.connected&&smaWorkspace.activePane?.ready')
        await page.evaluate('()=>{const panel=document.createElement("aside");panel.id="mylib-panel";document.body.append(panel);}')
        script = (ROOT / 'assistant-chat.js').read_text(encoding='utf-8')
        await page.evaluate(script)
        assert await page.locator('#assistant-chat-section').count() == 0
        await page.evaluate('()=>{window.pywebview={api:{assistant_chat:async()=>({text:"fixture"})}};dispatchEvent(new Event("pywebviewready"));}')
        assert await page.locator('#assistant-chat-section').count() == 1
        await page.evaluate(script)
        assert await page.locator('#assistant-chat-section').count() == 1
        assert_no_input(sessions)
