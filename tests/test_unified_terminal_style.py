"""Six Windows CLI styles on owned real xterm renderers, no CLI/model input."""
import pytest

from test_workspace_profile_rooms import fixture, run


@pytest.mark.parametrize('renderer', ['dom', 'webgl'])
def test_unified_semantics_protect_cursor_input_and_native_gray(renderer):
    run(lambda playwright: check_style(playwright, renderer))


async def check_style(playwright, renderer):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages,
                                       _daemons, _ops, _windows, sessions):
        page = await browser.new_page(viewport={'width': 1200, 'height': 900})
        await page.goto(url)
        await page.wait_for_function('smaWorkspace.activePane?.ready')
        result = await page.evaluate(r'''async renderer=>{
            const box=document.createElement('section');box.style.cssText='position:fixed;inset:0;overflow:auto;z-index:3000;background:#11151b';document.body.append(box);
            const frames=()=>new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(done)));
            const results=[];
            for(const {id} of TerminalClient.agentProfiles){
                const profile=await TerminalClient.loadAgentProfile('/agent-modes/'+id+'.json',id);
                const el=document.createElement('div');el.style.cssText='width:1000px;height:180px';box.append(el);
                const t=new Terminal({cols:100,rows:8,allowProposedApi:true,cursorBlink:false,
                    minimumContrastRatio:4.5,theme:profile.theme});t.open(el);
                let gl;if(renderer==='webgl'){gl=new WebglAddon.WebglAddon();t.loadAddon(gl);}
                const records=[],register=t.registerDecoration.bind(t);
                t.registerDecoration=options=>{const d=register(options);if(d)records.push({line:options.marker.line,color:options.foregroundColor,d});return d;};
                let writes=0;t.onData(()=>writes++);
                await new Promise(done=>t.write('# Summary\r\napp.py "literal" `code()` 42\r\nPASSED\r\nError\r\n> \x1b[2mTry "edit <filepath>..."\x1b[0m\r\n\x1b[90m? for shortcuts\x1b[0m\r\napp.py "user input"',done));
                const h=TerminalClient.createHighlighter(t,{profile});await frames();
                const live=()=>records.filter(r=>!r.d.isDisposed).map(({line,color})=>({line,color}));
                const before=live(),cursor=[t.buffer.active.cursorX,t.buffer.active.cursorY],
                    dim=!!t.buffer.active.getLine(4).getCell(2).isDim(),
                    gray=t.buffer.active.getLine(5).getCell(0).getFgColor();
                h.setEnabled(false);await frames();const disabled=live();
                h.setEnabled(true);await frames();
                await new Promise(done=>t.write('\r\n',done));await frames();
                results.push({id,before,after:live(),disabled,cursor,dim,gray,writes,
                    input:t.buffer.active.getLine(6).translateToString(true),
                    inputDefault:t.buffer.active.getLine(6).getCell(0).isFgDefault(),
                    grayTheme:t.options.theme.brightBlack,renderer:gl?'webgl':'dom'});
                h.dispose();gl?.dispose();t.dispose();el.remove();
            }
            box.remove();return results;
        }''', renderer)
        for value in result:
            assert value['renderer'] == renderer
            assert value['cursor'] == [19, 6]
            assert value['input'] == 'app.py "user input"'
            assert value['inputDefault'] and value['dim'] and value['gray'] == 8
            assert value['grayTheme'] == '#8b929c'
            assert value['disabled'] == []
            assert not any(d['line'] in {4, 5, 6} for d in value['before'])
            for row, color in [(0, '#82cfff'), (1, '#e5c07b'), (2, '#98c379'), (3, '#e06c75')]:
                matching = [d['color'] for d in value['before'] if d['line'] == row]
                assert matching and set(matching) == {color}, value
            assert any(d['line'] == 6 and d['color'] == '#e5c07b' for d in value['after'])
            assert value['writes'] == 0
        assert not any(session.pty.written for owned in sessions for session in owned)


def test_soft_wrapped_cursor_paragraph_is_not_decorated_until_hard_newline():
    run(check_wrapped)


async def check_wrapped(playwright):
    async with fixture(playwright) as (browser, url, *_rest):
        page = await browser.new_page()
        await page.goto(url)
        await page.wait_for_function('smaWorkspace.activePane?.ready')
        result = await page.evaluate(r'''async()=>{
            const el=document.createElement('div');document.body.append(el);
            const t=new Terminal({cols:16,rows:8,allowProposedApi:true});t.open(el);
            const records=[],register=t.registerDecoration.bind(t);
            t.registerDecoration=options=>{const d=register(options);if(d)records.push({line:options.marker.line,d});return d;};
            await new Promise(done=>t.write('app.py\r\napp.py "input" app.py "continued"',done));
            const h=TerminalClient.createHighlighter(t),frames=()=>new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(done)));
            await frames();const before=records.filter(r=>!r.d.isDisposed).map(r=>r.line),cursorY=t.buffer.active.cursorY;
            await new Promise(done=>t.write('\r\n',done));await frames();
            const after=records.filter(r=>!r.d.isDisposed).map(r=>r.line);
            h.dispose();t.dispose();el.remove();return {before,after,cursorY};
        }''')
        assert result['cursorY'] >= 2
        assert result['before'] and set(result['before']) == {0}
        assert any(row > 0 for row in result['after'])
