"""Read-only OP22 evidence uses fixture-owned sockets/PTYs, without model calls."""
import asyncio
import json
from pathlib import Path
import struct

import pytest

from supermonoagent.protocol import pack, pack_json, unpack_json
from test_workspace_profile_rooms import fixture, run, eventually


SCRIPT = Path('src/supermonoagent/static/mylib-assistant.js')


def context(project='fixture-project', pages=None, enabled=True):
    return {'project': project, 'project_source': 'session-startup-directory',
            'project_status': 'ready', 'note': 'fixture startup directory',
            'inventory': {'available': True, 'indexed': 1, 'skill_names': ['fixture-skill'], 'status': 'ready'},
            'ledger': {'enabled': enabled, 'status': 'ready' if enabled else 'not_enabled',
                       'pages': pages if pages is not None else {'README.md': {'status': 'ready', 'text': project}}}}


async def prepare(browser, url, sessions, evolution=None):
    page = await browser.new_page(viewport={'width': 1300, 'height': 850})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    await page.goto(url)
    await page.wait_for_function('smaWorkspace?.connected&&smaWorkspace.panes.size===2')
    await page.evaluate('sid=>smaWorkspace.openHostSession("local",sid,false)', sessions[0][0].id)
    await page.wait_for_function('[...smaWorkspace.panes.values()].every(p=>!p.writing&&!p.frames.length&&!p.resizeRequest)')
    await page.evaluate('''ideas=>{
      window.pywebview={api:{recommend_skills:async()=>({available:true,indexed:0,skills:[],evolution:ideas})}};
      window.evidencePane=smaWorkspace.activePane;window.evidenceWS=evidencePane.host.ws;
      window.evidenceListeners={message:new Set(),close:new Set(),error:new Set()};
      const add=evidenceWS.addEventListener.bind(evidenceWS),remove=evidenceWS.removeEventListener.bind(evidenceWS);
      evidenceWS.addEventListener=(name,fn,...rest)=>{evidenceListeners[name]?.add(fn);return add(name,fn,...rest);};
      evidenceWS.removeEventListener=(name,fn,...rest)=>{evidenceListeners[name]?.delete(fn);return remove(name,fn,...rest);};
    }''', evolution or [])
    await page.evaluate(SCRIPT.read_text(encoding='utf-8'))
    assert await page.evaluate('typeof SuperMonoAssistantEvidence.read') == 'function'
    return page, errors


def intercept(daemon, replies, gate=None):
    """A real fixture WebSocket carries the responses; only its OP22 is replaced."""
    requests, original = [], daemon._op

    async def operation(client, op, payload):
        if op != 22:
            return await original(client, op, payload)
        body = unpack_json(payload)
        requests.append((client, body))
        count = len(requests)
        if gate is not None and count == 1:
            await gate.wait()
        reply = replies(count, body)
        if isinstance(reply, bytes):
            return [reply], None
        return [pack_json(22, {'request': body['request'], 'ok': True, **reply})], None

    daemon._op = operation
    return requests


async def assert_readonly(page, sessions, errors):
    assert await page.evaluate('Object.values(evidenceListeners).every(set=>set.size===0)')
    assert not any(session.pty.written for owned in sessions for session in owned)
    assert not errors


def test_reads_coalesce_cache_is_bounded_and_force_fetches_fresh_data():
    run(check_cache)


async def check_cache(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, sessions):
        gate = asyncio.Event()
        requests = intercept(daemons[0], lambda count, _body: context('snapshot-' + str(count)), gate)
        page, errors = await prepare(browser, url, sessions)
        try:
            await page.evaluate('()=>{window.evidenceFirst=SuperMonoAssistantEvidence.read(evidencePane);window.evidenceSame=SuperMonoAssistantEvidence.read(evidencePane,{force:true});}')
            await eventually(lambda: len(requests) == 1)
            assert await page.evaluate('evidenceListeners.message.size') == 1
            gate.set()
            first, same = await page.evaluate('Promise.all([evidenceFirst,evidenceSame])')
            assert first['available'] and first['project'] == same['project'] == 'snapshot-1'
            assert first['captured_at'] == same['captured_at']
            await page.evaluate('async()=>{const value=await SuperMonoAssistantEvidence.read(evidencePane);value.ledger.pages["README.md"].text="MUTATED";}')
            cached = await page.evaluate('SuperMonoAssistantEvidence.read(evidencePane)')
            assert cached['ledger']['pages']['README.md']['text'] == 'snapshot-1'
            assert len(requests) == 1
            forced = await page.evaluate('SuperMonoAssistantEvidence.read(evidencePane,{force:true})')
            assert forced['project'] == 'snapshot-2' and len(requests) == 2
            # Advance only this fixture page's monotonic observation clock.
            await page.evaluate('()=>{const actual=performance.now.bind(performance);Object.defineProperty(performance,"now",{value:()=>actual()+15001,configurable:true});}')
            expired = await page.evaluate('SuperMonoAssistantEvidence.read(evidencePane)')
            assert expired['project'] == 'snapshot-3' and len(requests) == 3
            await assert_readonly(page, sessions, errors)
        finally:
            gate.set()


def test_other_windows_never_share_cached_project_evidence():
    run(check_windows)


async def check_windows(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, sessions):
        requests = intercept(daemons[0], lambda _count, body: context('project-for-' + body['session']))
        page, errors = await prepare(browser, url, sessions)
        first = await page.evaluate('SuperMonoAssistantEvidence.read(evidencePane)')
        second = await page.evaluate('()=>{window.evidenceOther=[...smaWorkspace.panes.values()].find(p=>p!==evidencePane);return SuperMonoAssistantEvidence.read(evidenceOther);}')
        assert first['project'] != second['project']
        assert len(requests) == 2
        again = await page.evaluate('SuperMonoAssistantEvidence.read(evidencePane)')
        assert again['project'] == first['project'] and len(requests) == 2
        await page.evaluate('smaWorkspace.closeView(evidencePane.sid)')
        closed = await page.evaluate('SuperMonoAssistantEvidence.read(evidencePane)')
        assert closed['available'] is False and len(requests) == 2
        await assert_readonly(page, sessions, errors)


@pytest.mark.parametrize('boundary', ['token', 'reconnect', 'session', 'close'])
def test_late_and_cached_responses_are_rejected_after_identity_change(boundary):
    run(lambda playwright: check_identity(playwright, boundary))


async def check_identity(playwright, boundary):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, sessions):
        gate = asyncio.Event()
        requests = intercept(daemons[0], lambda count, _body: context('IDENTITY_RESPONSE_' + str(count)), gate)
        page, errors = await prepare(browser, url, sessions)
        try:
            await page.evaluate('()=>{window.evidenceOld=SuperMonoAssistantEvidence.read(evidencePane);window.evidenceOriginalToken=evidencePane.host.config.token;window.evidenceOriginalSession=evidencePane.raw;window.evidenceOldGeneration=evidencePane.host.generation;}')
            await eventually(lambda: len(requests) == 1)
            if boundary == 'token':
                await page.evaluate('evidencePane.host.config.token="changed-fixture-token"')
            elif boundary == 'session':
                await page.evaluate('evidencePane.raw="changed-session"')
            elif boundary == 'close':
                await page.evaluate('smaWorkspace.closeView(evidencePane.sid)')
            else:
                await page.evaluate('evidencePane.host.ws.close()')
                await page.wait_for_function('evidencePane.host.connected&&evidencePane.host.generation>evidenceOldGeneration')
            gate.set()
            old = await page.evaluate('evidenceOld')
            assert old['available'] is False and 'IDENTITY_RESPONSE' not in str(old)
            if boundary == 'close':
                await page.evaluate('sid=>smaWorkspace.openHostSession("local",sid,false)', sessions[0][0].id)
                fresh = await page.evaluate('SuperMonoAssistantEvidence.read(smaWorkspace.activePane)')
            else:
                await page.evaluate('()=>{evidencePane.host.config.token=evidenceOriginalToken;evidencePane.raw=evidenceOriginalSession;}')
                fresh = await page.evaluate('SuperMonoAssistantEvidence.read(evidencePane)')
            assert fresh['available'] and fresh['project'] == 'IDENTITY_RESPONSE_2'
            # A cache hit is also checked at Promise delivery, not only at lookup.
            await page.evaluate('()=>{window.evidenceCacheRace=SuperMonoAssistantEvidence.read(smaWorkspace.activePane);smaWorkspace.activePane.host.config.token="cache-race-token";}')
            raced = await page.evaluate('evidenceCacheRace')
            assert raced['available'] is False and 'IDENTITY_RESPONSE' not in str(raced)
            await page.evaluate('smaWorkspace.activePane.host.config.token=evidenceOriginalToken')
            await assert_readonly(page, sessions, errors)
        finally:
            gate.set()


@pytest.mark.parametrize('kind', ['oversize', 'invalid-json', 'invalid-utf8', 'remote-error'])
def test_response_limits_and_errors_are_sanitized_and_release_listeners(kind):
    run(lambda playwright: check_bad_response(playwright, kind))


async def check_bad_response(playwright, kind):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, sessions):
        def reply(_count, body):
            if kind == 'oversize':
                payload = json.dumps({'request': body['request'], 'private': 'x' * (256 * 1024)}).encode()
                return struct.pack('<I', len(payload) + 1) + bytes([22]) + payload
            if kind == 'invalid-json':
                return pack(22, b'private-error-and-path invalid JSON')
            if kind == 'invalid-utf8':
                return pack(22, b'\xff private-error-and-path')
            return pack_json(22, {'request': body['request'], 'ok': False, 'error': 'private-error-and-path'})
        intercept(daemons[0], reply)
        page, errors = await prepare(browser, url, sessions)
        result = await page.evaluate('SuperMonoAssistantEvidence.read(evidencePane)')
        assert result['available'] is False and result['note']
        assert 'private-error-and-path' not in str(result)
        await assert_readonly(page, sessions, errors)


@pytest.mark.parametrize('source', ['missing', 'terminal-output', None])
def test_missing_or_other_project_source_is_never_upgraded_to_trusted(source):
    run(lambda playwright: check_untrusted_source(playwright, source))


async def check_untrusted_source(playwright, source):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, sessions):
        def reply(_count, _body):
            value = context('UNTRUSTED_PROJECT_DATA')
            if source == 'missing':
                value.pop('project_source')
            else:
                value['project_source'] = source
            return value

        intercept(daemons[0], reply)
        page, errors = await prepare(browser, url, sessions)
        value = await page.evaluate('SuperMonoAssistantEvidence.read(evidencePane)')
        assert value['available'] is False
        assert value['project_source'] != 'session-startup-directory'
        assert 'UNTRUSTED_PROJECT_DATA' not in str(value)
        await assert_readonly(page, sessions, errors)


def test_timeout_is_absolute_and_late_response_cannot_fill_cache():
    run(check_timeout)


async def check_timeout(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, sessions):
        gate = asyncio.Event()
        requests = intercept(daemons[0], lambda count, _body: context('timeout-snapshot-' + str(count)), gate)
        page, errors = await prepare(browser, url, sessions)
        spam = None
        try:
            await page.evaluate('()=>{window.evidenceStart=performance.now();window.evidenceTimed=SuperMonoAssistantEvidence.read(evidencePane);}')
            await eventually(lambda: len(requests) == 1)

            async def unmatched_responses():
                while not gate.is_set():
                    client = requests[0][0]
                    daemons[0].hub._send(client, pack_json(22, {'request': 'unrelated-request', 'ok': True, **context('unrelated')}))
                    await asyncio.sleep(.2)
            spam = asyncio.create_task(unmatched_responses())
            result = await page.evaluate('evidenceTimed')
            elapsed = await page.evaluate('performance.now()-evidenceStart')
            assert result['available'] is False and '超时' in result['note']
            assert 7500 <= elapsed < 11000
            assert await page.evaluate('Object.values(evidenceListeners).every(set=>set.size===0)')
            gate.set()
            fresh = await page.evaluate('SuperMonoAssistantEvidence.read(evidencePane,{force:true})')
            assert fresh['project'] == 'timeout-snapshot-2' and len(requests) == 2
            await assert_readonly(page, sessions, errors)
        finally:
            gate.set()
            if spam is not None:
                spam.cancel()
                await asyncio.gather(spam, return_exceptions=True)


def test_response_after_monotonic_deadline_is_rejected_before_timer_delivery():
    run(check_deadline_race)


async def check_deadline_race(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, sessions):
        gate = asyncio.Event()
        requests = intercept(daemons[0], lambda _count, _body: context('LATE_DEADLINE_DATA'), gate)
        page, errors = await prepare(browser, url, sessions)
        try:
            await page.evaluate('()=>{window.evidenceLate=SuperMonoAssistantEvidence.read(evidencePane);}')
            await eventually(lambda: len(requests) == 1)
            await page.evaluate('()=>{const actual=performance.now.bind(performance);Object.defineProperty(performance,"now",{value:()=>actual()+9000,configurable:true});}')
            gate.set()
            result = await page.evaluate('evidenceLate')
            assert result['available'] is False and '超时' in result['note']
            assert 'LATE_DEADLINE_DATA' not in str(result)
            await assert_readonly(page, sessions, errors)
        finally:
            gate.set()


def test_five_page_normalization_and_local_record_cards_are_plain_text(tmp_path):
    run(lambda playwright: check_records(playwright, tmp_path))


async def check_records(playwright, tmp_path):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, sessions):
        xss = '<img src=x onerror=window.evidenceInjected=true>'
        pages = {
            'README.md': {'status': 'ready', 'text': 'x' * 8191 + '\U0001f600', 'mtime': 1e300},
            'negatives.md': {'status': 'ready', 'text': '# Rejected N0001\nSocketError connection retry failed; use independent fixture.\n' + xss},
            'trials.md': {'status': 'ready', 'text': '# Trial T0001\nSocketError connection retry measured in old sample.'},
            'nodes.md': {'status': 'ready', 'text': '# Shadow S0001\nSocketError connection retry future validation pending.'},
            'ideas.md': {'status': 'ready', 'text': '# Idea I0001\nIdeaDivergenceError ideaflow falsifier pending.'},
            'private.txt': {'status': 'ready', 'text': 'NEVER_RETURN_THIS_PAGE'},
        }
        intercept(daemons[0], lambda _count, _body: context('p' * 4095 + '\U0001f600', pages))
        ideas = [{'name': '故障建议', 'evidence': ['SocketError: connection retry failed', 'SocketError: connection retry failed'], 'next_step': '先查账', 'falsifier': '独立样本不能改善则否决'},
                 {'name': '想法建议', 'evidence': ['IdeaDivergenceError: ideaflow', 'IdeaDivergenceError: ideaflow'], 'next_step': '先查想法', 'falsifier': '新样本验证'},
                 {'name': '无匹配建议', 'evidence': ['UnknownVideoProblem: pixels', 'UnknownVideoProblem: pixels'], 'next_step': '不推断', 'falsifier': '预登记'}]
        page, errors = await prepare(browser, url, sessions, ideas)
        value = await page.evaluate('SuperMonoAssistantEvidence.read(evidencePane)')
        assert value['available'] and value['project_source'] == 'session-startup-directory'
        assert len(value['project']) == 4095 and value['project_truncated']
        assert set(value['ledger']['pages']) == {'README.md', 'negatives.md', 'trials.md', 'nodes.md', 'ideas.md'}
        assert len(value['ledger']['pages']['README.md']['text']) == 8191
        assert value['ledger']['pages']['README.md']['truncated']
        assert 'mtime' not in value['ledger']['pages']['README.md']
        assert 'NEVER_RETURN_THIS_PAGE' not in str(value)
        await page.locator('#mylib-toggle').click()
        await page.wait_for_function('document.querySelector("#mylib-evolution").textContent.includes("否决记录片段")')
        cards = await page.locator('#mylib-evolution').inner_text()
        for expected in ['SocketError', '否决记录片段', '试验记录片段', '影子规则节点片段', '想法记录片段', '.metarsi/wiki/negatives.md', '输出行数不是独立故障计数', '账本静态快照', '没有可靠匹配的记录', '未验证']:
            assert expected in cards
        assert await page.locator('#mylib-evolution img').count() == 0
        assert await page.locator('#mylib-project img').count() == 0
        assert not await page.evaluate('window.evidenceInjected===true')
        await page.locator('#mylib-evolution').screenshot(path=str(tmp_path / 'evidence-cards.png'))
        await assert_readonly(page, sessions, errors)


@pytest.mark.parametrize('mode', ['old-three-pages', 'no-ledger', 'old-backend'])
def test_old_and_uninitialized_ledgers_degrade_without_inferred_results(mode):
    run(lambda playwright: check_legacy(playwright, mode))


async def check_legacy(playwright, mode):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, sessions):
        pages = {'README.md': 'old entry', 'negatives.md': 'old negative', 'trials.md': 'old trial'}
        requests = intercept(daemons[0], lambda _count, _body: context(pages=pages if mode != 'no-ledger' else {}, enabled=mode != 'no-ledger'))
        page, errors = await prepare(browser, url, sessions)
        if mode == 'old-backend':
            await page.evaluate('evidencePane.host.capabilities.assistant_context=false')
        result = await page.evaluate('SuperMonoAssistantEvidence.read(evidencePane)')
        if mode == 'old-backend':
            assert result['available'] is False and len(requests) == 0
        elif mode == 'no-ledger':
            assert result['available'] and result['ledger']['enabled'] is False
            assert result['ledger']['pages'] == {}
        else:
            assert result['available'] and set(result['ledger']['pages']) == set(pages)
            assert result['ledger']['pages']['README.md']['text'] == 'old entry'
        await assert_readonly(page, sessions, errors)


def test_page_truncation_flags_preserve_backend_and_cover_surrogate_boundaries():
    run(check_truncation_flags)


async def check_truncation_flags(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, sessions):
        pages = {'README.md': {'status': 'ready', 'text': 'backend prefix', 'truncated': True},
                 'negatives.md': {'status': 'ready', 'text': 'x' * 8193, 'truncated': False},
                 'trials.md': {'status': 'ready', 'text': 'short\ud800', 'truncated': False},
                 'nodes.md': 'legacy\ud800',
                 'ideas.md': {'status': 'ready', 'text': 'complete text', 'truncated': False}}

        def reply(_count, body):
            # Escaped JSON can legally contain an unpaired UTF16 unit even
            # though the wire bytes are valid UTF8; do not keep a cut boundary.
            raw = json.dumps({'request': body['request'], 'ok': True, **context(pages=pages)}, ensure_ascii=True).encode()
            return pack(22, raw)

        intercept(daemons[0], reply)
        page, errors = await prepare(browser, url, sessions)
        result = await page.evaluate('SuperMonoAssistantEvidence.read(evidencePane)')
        observed = result['ledger']['pages']
        for name in ['README.md', 'negatives.md', 'trials.md', 'nodes.md']:
            assert observed[name]['truncated'] is True
        assert observed['trials.md']['text'] == 'short'
        assert observed['nodes.md']['text'] == 'legacy'
        assert observed['ideas.md']['truncated'] is False
        await assert_readonly(page, sessions, errors)


def test_local_context_rejoins_soft_wraps_without_splitting_long_token():
    run(check_soft_wrap_capture)


async def check_soft_wrap_capture(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, sessions):
        intercept(daemons[0], lambda _count, _body: context(pages={}, enabled=False))
        page, errors = await prepare(browser, url, sessions)
        await page.evaluate('()=>{window.localCaptured=[];pywebview.api.recommend_skills=async text=>{localCaptured.push(text);return {available:true,indexed:0,skills:[],evolution:[]};};}')
        token = 'fixture-token-"\\-' + 'abcdefghijklmnopqrstuvwxyz0123456789' * 45
        text = 'HARD_BEFORE\r\nTOKEN_BEGIN:' + token + ':TOKEN_END\r\nHARD_AFTER\r\n'
        sessions[0][0].pty.inbox.put(text.encode())
        await page.wait_for_function('()=>{const b=evidencePane.term.buffer.active;return Array.from({length:b.length},(_,i)=>b.getLine(i)?.translateToString(true)).some(line=>line?.includes("HARD_AFTER"));}')
        assert await page.evaluate('()=>{const b=evidencePane.term.buffer.active;return Array.from({length:b.length},(_,i)=>b.getLine(i)?.isWrapped).filter(Boolean).length;}') > 2
        await page.locator('#mylib-toggle').click()
        await page.wait_for_function('localCaptured.length>0&&document.querySelector("#mylib-project").textContent.includes("终端启动目录")')
        captured = await page.evaluate('localCaptured[0]')
        assert token in captured
        assert 'HARD_BEFORE\nTOKEN_BEGIN:' + token + ':TOKEN_END\nHARD_AFTER' in captured
        assert token in await page.locator('#mylib-context').text_content()
        await assert_readonly(page, sessions, errors)


def test_automatic_panel_recovers_after_same_generation_becomes_ready_without_refresh():
    run(check_automatic_reconnect)


async def check_automatic_reconnect(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, sessions):
        original = daemons[0]._op
        hold_hello = [False]
        hello_seen, hello_release = asyncio.Event(), asyncio.Event()
        recovery_seen, recovery_release = asyncio.Event(), asyncio.Event()
        reads = []

        async def gated(client, op, payload):
            if op == 1 and hold_hello[0]:
                hello_seen.set()
                await hello_release.wait()
            if op == 22:
                body = unpack_json(payload)
                reads.append(body)
                if len(reads) == 2:
                    recovery_seen.set()
                    await recovery_release.wait()
                label = 'BASELINE_LEDGER' if len(reads) == 1 else 'RECOVERED_LEDGER_A'
                return [pack_json(22, {'request': body['request'], 'ok': True, **context(label)})], None
            return await original(client, op, payload)

        daemons[0]._op = gated
        page, errors = await prepare(browser, url, sessions)
        await page.evaluate('()=>{window.automaticCalls=0;pywebview.api.recommend_skills=async()=>{automaticCalls++;return {available:true,indexed:0,skills:[],evolution:[]};};}')
        try:
            await page.locator('#mylib-toggle').click()
            await page.wait_for_function('document.querySelector("#mylib-project").textContent.includes("BASELINE_LEDGER")')
            assert len(reads) == 1
            hold_hello[0] = True
            await page.evaluate('()=>{window.beforeAutomaticReconnect=evidencePane.host.generation;evidencePane.host.ws.close();}')
            async with asyncio.timeout(5):
                await hello_seen.wait()
            await page.wait_for_function('evidencePane.host.generation>beforeAutomaticReconnect&&!evidencePane.host.connected')
            # Only the ordinary four-second sensing tick is allowed to observe
            # this new-generation, not-yet-authenticated connection.
            await page.wait_for_function('document.querySelector("#mylib-project").textContent.includes("未连接或已经关闭")', timeout=7000)
            offline = await page.evaluate('({generation:evidencePane.host.generation,text:document.querySelector("#mylib-context").textContent,calls:automaticCalls})')
            assert len(reads) == 1  # No RPC was sent before readiness.
            hold_hello[0] = False
            hello_release.set()
            await page.wait_for_function('evidencePane.host.connected&&evidencePane.ready')
            assert await page.evaluate('evidencePane.host.generation') == offline['generation']
            async with asyncio.timeout(7):
                await recovery_seen.wait()
            calls_before_gate = await page.evaluate('automaticCalls')
            assert calls_before_gate == offline['calls'] + 1
            # Leave that live OP22 outstanding over a sensing tick: no duplicate
            # native recommendation or concurrent evidence read may be started.
            await page.evaluate('window.automaticGateStart=Date.now()')
            await page.wait_for_function('Date.now()-automaticGateStart>=4200', timeout=6000)
            assert len(reads) == 2
            assert await page.evaluate('automaticCalls') == calls_before_gate
            recovery_release.set()
            await page.wait_for_function('document.querySelector("#mylib-project").textContent.includes("RECOVERED_LEDGER_A")')
            assert await page.locator('#mylib-context').text_content() == offline['text']
            assert '未连接或已经关闭' not in await page.locator('#mylib-project').text_content()
            await page.evaluate('window.automaticSettled=Date.now()')
            await page.wait_for_function('Date.now()-automaticSettled>=4200', timeout=6000)
            assert len(reads) == 2 and await page.evaluate('automaticCalls') == calls_before_gate
            await assert_readonly(page, sessions, errors)
        finally:
            hello_release.set()
            recovery_release.set()


def test_connected_temporary_failure_is_retried_by_existing_tick_only():
    run(check_automatic_transient_failure)


async def check_automatic_transient_failure(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, sessions):
        def reply(count, body):
            if count == 1:
                return pack_json(22, {'request': body['request'], 'ok': False, 'error': 'fixture temporary failure'})
            return context('AUTOMATIC_RETRY_RECOVERED')

        reads = intercept(daemons[0], reply)
        page, errors = await prepare(browser, url, sessions)
        await page.evaluate('()=>{window.automaticCalls=0;pywebview.api.recommend_skills=async()=>{automaticCalls++;return {available:true,indexed:0,skills:[],evolution:[]};};}')
        await page.locator('#mylib-toggle').click()
        await page.wait_for_function('document.querySelector("#mylib-project").textContent.includes("项目账本暂不可用")')
        assert len(reads) == 1 and await page.evaluate('automaticCalls') == 1
        await page.wait_for_function('document.querySelector("#mylib-project").textContent.includes("AUTOMATIC_RETRY_RECOVERED")', timeout=11000)
        assert len(reads) == 2 and await page.evaluate('automaticCalls') == 2
        await page.evaluate('window.automaticStable=Date.now()')
        await page.wait_for_function('Date.now()-automaticStable>=4200', timeout=6000)
        assert len(reads) == 2 and await page.evaluate('automaticCalls') == 2
        await assert_readonly(page, sessions, errors)
