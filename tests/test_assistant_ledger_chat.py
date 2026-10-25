"""Separate consent and connection identity guard project data sent to models."""
import pytest

from test_assistant_chat_ui import prepare, assert_no_input
from test_workspace_profile_rooms import fixture, run


async def ledger_fixture(page, *, gated=False, limit=8192):
    await page.locator('#mylib-pause').click()
    await page.evaluate('''options=>{
      window.ledgerReads=[];window.ledgerPending=[];
      window.ownedLedger={available:true,project:'owned-project',project_source:'session-startup-directory',
        ledger:{enabled:true,status:'ready',pages:{
          'negatives.md':{status:'ready',text:'N0007: failed retry was rejected; check a different hypothesis. '+ 'x'.repeat(3000),mtime:100},
          'trials.md':{status:'ready',text:'T0003: no independent validation yet.',mtime:101},
          'nodes.md':{status:'ready',text:'S0002: pending shadow; not admitted.',mtime:102},
          'ideas.md':{status:'ready',text:'I0008: test isolation before promotion.',mtime:103},
          'README.md':{status:'ready',text:'Snapshot, not a newly computed mode.',mtime:104}
        }}};
      window.SuperMonoAssistantEvidence={read:(pane,options)=>{
        ledgerReads.push({session:pane.raw,force:options.force});
        return window.ledgerGated?new Promise(resolve=>ledgerPending.push(resolve)):Promise.resolve(ownedLedger);
      }};
      window.ledgerGated=options.gated;
      pywebview.api.get_assistant_preferences=async()=>({preferences:{auto_context:false,max_context_chars:options.limit},status:'saved'});
      dispatchEvent(new Event('mylib-preferences-changed'));
    }''', {'gated': gated, 'limit': limit})


def test_project_evidence_requires_separate_consent_and_respects_budget():
    run(check_ledger_consent)


async def check_ledger_consent(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons, _ops, _windows, sessions):
        page, errors = await prepare(browser, url, sessions)
        await ledger_fixture(page, limit=512)
        assert not await page.locator('#assistant-chat-ledger').is_checked()
        await page.locator('#assistant-chat-question').fill('No project data')
        await page.locator('#assistant-chat-send').click()
        await page.wait_for_function('chatCalls.length===1&&!document.querySelector("#assistant-chat-send").disabled')
        assert await page.evaluate('ledgerReads.length') == 0
        assert await page.evaluate('chatCalls[0].context') == ''
        await page.locator('#assistant-chat-ledger').check()
        await page.locator('#assistant-chat-question').fill('Use the project records')
        await page.locator('#assistant-chat-send').click()
        await page.wait_for_function('chatCalls.length===2&&!document.querySelector("#assistant-chat-send").disabled')
        payload = await page.evaluate('chatCalls[1]')
        assert payload['attach_context'] is True and len(payload['context']) <= 512
        assert '.metarsi/wiki/negatives.md' in payload['context'] and 'N0007' in payload['context']
        assert '[终端最近输出片段]' not in payload['context']
        assert '未重新核验' in payload['context']
        assert await page.evaluate('ledgerReads[0].force') is True
        await page.locator('#assistant-chat-question').fill('Keep this analysis draft')
        await page.locator('#assistant-chat-analyze').click()
        await page.wait_for_function('chatCalls.length===3&&!document.querySelector("#assistant-chat-send").disabled')
        assert '可证伪条件' in await page.evaluate('chatCalls[2].question')
        assert await page.locator('#assistant-chat-question').input_value() == 'Keep this analysis draft'
        assert_no_input(sessions)
        assert not errors


@pytest.mark.parametrize('boundary', ['cancel', 'credentials', 'preferences', 'session'])
def test_pending_evidence_read_never_starts_model_after_invalidation(boundary):
    run(lambda playwright: check_pending_ledger(playwright, boundary))


async def check_pending_ledger(playwright, boundary):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons, _ops, _windows, sessions):
        page, errors = await prepare(browser, url, sessions)
        await ledger_fixture(page, gated=True)
        await page.locator('#assistant-chat-ledger').check()
        await page.locator('#assistant-chat-question').fill('Waiting for project evidence')
        await page.locator('#assistant-chat-send').click()
        await page.wait_for_function('ledgerPending.length===1')
        assert await page.locator('#assistant-chat-send').is_disabled()
        assert await page.evaluate('chatCalls.length') == 0
        if boundary == 'cancel':
            await page.locator('#assistant-chat-cancel').click()
        elif boundary == 'credentials':
            await page.evaluate('smaWorkspace.activeHost.config.token="rotated-owned-fixture-token"')
        elif boundary == 'session':
            await page.evaluate('smaWorkspace.activePane.raw="new-owned-session-identity"')
        else:
            await page.evaluate('dispatchEvent(new Event("mylib-preferences-changed"))')
        await page.wait_for_function('chatCancelCount===1')
        assert await page.evaluate('chatCancelIds[0].length') > 0
        if boundary != 'cancel':
            assert not await page.locator('#assistant-chat-ledger').is_checked()
        await page.evaluate('ledgerPending[0](ownedLedger)')
        await page.wait_for_function('!document.querySelector("#assistant-chat-send").disabled')
        assert await page.evaluate('chatCalls.length') == 0
        assert_no_input(sessions)
        assert not errors


def test_ledger_consent_stays_with_its_window_and_missing_records_are_explicit():
    run(check_ledger_window)


async def check_ledger_window(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons, _ops, _windows, sessions):
        page, errors = await prepare(browser, url, sessions)
        await ledger_fixture(page)
        first = await page.evaluate('smaWorkspace.activeSession')
        await page.locator('#assistant-chat-ledger').check()
        await page.evaluate('smaWorkspace.openSession([...smaWorkspace.panes.keys()].find(id=>id!==smaWorkspace.activeSession),false)')
        await page.wait_for_function('!document.querySelector("#assistant-chat-ledger").checked')
        await page.evaluate('id=>smaWorkspace.openSession(id,false)', first)
        await page.wait_for_function('document.querySelector("#assistant-chat-ledger").checked')
        await page.evaluate('ownedLedger={available:false,note:"no trusted project"}')
        await page.locator('#assistant-chat-question').fill('Explain only known facts')
        await page.locator('#assistant-chat-send').click()
        await page.wait_for_function('chatCalls.length===1&&!document.querySelector("#assistant-chat-send").disabled')
        assert '没有可读取的账本页面' in await page.evaluate('chatCalls[0].context')
        assert '账本不可读' in await page.locator('#assistant-chat-status').inner_text()
        assert_no_input(sessions)
        assert not errors
