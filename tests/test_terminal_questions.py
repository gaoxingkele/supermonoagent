"""Passive submitted-history marks on owned xterm; never touches user PTYs."""
from pathlib import Path

import pytest

from test_workspace_profile_rooms import fixture, run


SCRIPT = Path(__file__).resolve().parents[1] / 'src/supermonoagent/static/terminal-questions.js'


async def setup(browser, url):
    page = await browser.new_page(viewport={'width': 1200, 'height': 700})
    await page.goto(url)
    await page.wait_for_function('smaWorkspace.activePane?.ready')
    await page.evaluate(SCRIPT.read_text(encoding='utf-8'))
    await page.evaluate(r'''()=>{
        const box=document.createElement('section');box.id='question-owned';box.style.cssText='position:fixed;inset:0;z-index:3000;background:#111';document.body.append(box);
        window.qt=new Terminal({cols:40,rows:12,scrollback:200,allowProposedApi:true,cursorBlink:false});qt.loadAddon(new Unicode11Addon.Unicode11Addon());qt.unicode.activeVersion='11';qt.open(box);
        window.qpane={term:qt,ready:true,replayPending:0,generation:1,raw:'owned-session',sid:'owned-pane',
            host:{generation:1,connected:true,ws:{readyState:1},config:{token:'owned-test-token'}}};
        window.qcurrent=true;window.q=TerminalQuestions.create(qpane,{isCurrent:()=>qcurrent});
        window.qdecos=[];const register=qt.registerDecoration.bind(qt);
        qt.registerDecoration=options=>{const d=register(options);if(d)qdecos.push({d,marker:options.marker,x:options.x,width:options.width,color:options.foregroundColor});return d;};
        window.qaccepted=[];window.qecho=true;
        window.qWrite=async(text,replay=false)=>{q.beforeOutput({replay});await new Promise(done=>qt.write(text,done));await new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(done)));};
        qt.onData(text=>{qaccepted.push(text);q.observeAcceptedInput(text);if(text==='\r'&&qecho)qWrite('\r\n');});
        window.qLive=()=>qdecos.filter(v=>!v.d.isDisposed).map(v=>({line:v.marker.line,x:v.x,width:v.width,color:v.color}));
        qt.focus();
    }''')
    return page


def test_trusted_enter_and_exact_echo_mark_only_submitted_text_excluding_prompt_and_ghost():
    run(check_submit)


async def check_submit(playwright):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages,
                                       _daemons, _ops, _windows, sessions):
        page = await setup(browser, url)
        await page.evaluate(r'''async()=>{
            q.observeAcceptedInput('why app.py "quoted"');
            await qWrite('> why app.py "quoted"\x1b[2m ghost suggestion\x1b[0m\x1b[17D');
        }''')
        assert await page.evaluate('qt.buffer.active.cursorX') == 21
        assert await page.evaluate('qLive()') == []
        await page.keyboard.press('Enter')
        await page.wait_for_function('qLive().length>0')
        marks = await page.evaluate('qLive()')
        assert marks == [{'line': 0, 'x': 2, 'width': 19, 'color': '#ffd75f'}]
        await page.wait_for_function('''()=>[...qt.element.querySelectorAll('.xterm-rows>div:first-child span')]
            .filter(s=>getComputedStyle(s).color==='rgb(255, 215, 95)').map(s=>s.textContent).join('')==='why app.py "quoted"' ''')
        assert await page.evaluate('qaccepted') == ['\r']
        # Later ordinary quotes/code and output repeating a submitted question
        # are not promoted, because there is no new trusted submission witness.
        await page.evaluate('qWrite(\'why app.py "quoted"\\r\\noutput.py "ordinary"\\r\\n\')')
        assert await page.evaluate('qLive()') == marks
        # A later CLI repaint may reuse the old screen coordinates. Marks must
        # never turn new output into an apparent historical user question.
        await page.evaluate('qWrite("\\x1b[H\\x1b[2Kordinary output\\x1b[4;1H")')
        assert await page.evaluate('qLive()') == []
        assert not any(session.pty.written for owned in sessions for session in owned)
        await page.evaluate('q.dispose()')
        assert await page.evaluate('qLive()') == []


def test_copied_unicode_text_is_only_marked_after_separate_physical_enter():
    run(check_pasted_unicode)


async def check_pasted_unicode(playwright):
    async with fixture(playwright) as (browser, url, *_rest):
        page = await setup(browser, url)
        await page.evaluate(r'''async()=>{
            const d=new DataTransfer();d.setData('text/plain','中文😀 question');
            qt.textarea.dispatchEvent(new ClipboardEvent('paste',{clipboardData:d,bubbles:true,cancelable:true}));
            await qWrite('> 中文😀 question');
        }''')
        assert await page.evaluate('qLive()') == []
        await page.keyboard.press('Enter')
        await page.wait_for_function('qLive().length>0')
        assert await page.evaluate('qLive()') == [{'line': 0, 'x': 2, 'width': 15, 'color': '#ffd75f'}]
        await page.evaluate('q.dispose()')


@pytest.mark.parametrize('boundary', ['untrusted-enter', 'paste-newlines', 'unknown-edit',
    'current-input', 'overwrite', 'generation', 'token', 'socket', 'session',
    'pane-inactive', 'replay', 'disabled', 'dispose'])
def test_uncertain_or_stale_submission_never_marks_output(boundary):
    run(lambda playwright: check_boundary(playwright, boundary))


async def check_boundary(playwright, boundary):
    async with fixture(playwright) as (browser, url, *_rest):
        page = await setup(browser, url)
        await page.evaluate("async()=>{q.observeAcceptedInput('owned question');await qWrite('> owned question');qecho=false;}")
        if boundary == 'untrusted-enter':
            await page.evaluate("()=>{qt.textarea.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));q.observeAcceptedInput('\\r');}")
        elif boundary == 'paste-newlines':
            await page.evaluate("()=>q.observeAcceptedInput('\\x1b[200~pasted\\r\\nlines\\x1b[201~')")
        elif boundary == 'unknown-edit':
            await page.evaluate("()=>q.observeAcceptedInput('\\x1b[D')")
            await page.keyboard.press('Enter')
        else:
            await page.keyboard.press('Enter')
        if boundary == 'current-input':
            await page.evaluate("qWrite('')")
        else:
            if boundary == 'overwrite': await page.evaluate("qWrite('\\r\\x1b[2Kchanged')")
            elif boundary == 'generation': await page.evaluate('qpane.host.generation++')
            elif boundary == 'token': await page.evaluate("qpane.host.config.token='rotated-owned-token'")
            elif boundary == 'socket': await page.evaluate('qpane.host.ws={readyState:1}')
            elif boundary == 'session': await page.evaluate("qpane.raw='other-owned-session'")
            elif boundary == 'pane-inactive': await page.evaluate('qcurrent=false')
            elif boundary == 'disabled': await page.evaluate('q.setEnabled(false)')
            elif boundary == 'dispose': await page.evaluate('q.dispose()')
            await page.evaluate('replay=>qWrite("\\r\\n",replay)', boundary == 'replay')
        assert await page.evaluate('qLive()') == []
        await page.evaluate('q.dispose()')


def test_wrapped_input_marks_multiple_ranges_and_mark_count_is_bounded():
    run(check_wrapped_and_bounded)


async def check_wrapped_and_bounded(playwright):
    async with fixture(playwright) as (browser, url, *_rest):
        page = await setup(browser, url)
        await page.evaluate("async()=>{qt.resize(16,12);const text='owned long question app.py';q.observeAcceptedInput(text);await qWrite('> '+text);}")
        await page.keyboard.press('Enter')
        await page.wait_for_function('qLive().length===2')
        marks = await page.evaluate('qLive()')
        assert marks[0]['x'] == 2 and marks[0]['width'] == 14
        assert marks[1]['x'] == 0 and marks[1]['width'] == 12
        await page.evaluate(r'''async()=>{
            for(let i=0;i<70;i++){
                const text='owned '+i;await qWrite(text);q.recordSubmission({text,source:'explicit-submit'});await qWrite('\r\n');
            }
        }''')
        assert await page.evaluate('qLive().length') <= 64
        await page.evaluate('q.reset()')
        assert await page.evaluate('qLive()') == []
        assert not await page.evaluate("q.recordSubmission({text:'old historical text',source:'explicit-submit'})")
        assert not await page.evaluate("q.recordSubmission({text:'',source:'unknown'})")
        await page.evaluate('q.dispose()')
