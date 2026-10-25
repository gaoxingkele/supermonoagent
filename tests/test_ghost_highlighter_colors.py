"""Owned ANSI ghost text in real xterm DOM rendering; no CLI/model calls."""
import json

from test_workspace_profile_rooms import fixture, run


def test_faint_and_explicit_gray_cli_output_keep_native_dom_colors(tmp_path):
    run(lambda playwright: check_colors(playwright, tmp_path))


async def check_colors(playwright, tmp_path):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons, _ops, _windows, sessions):
        page = await browser.new_page(viewport={'width': 1200, 'height': 900})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.goto(url)
        await page.wait_for_function('smaWorkspace?.connected&&smaWorkspace.activePane?.ready')
        await page.evaluate(r'''async()=>{
          const profile=await TerminalClient.loadAgentProfile('/agent-modes/claude-code.json','claude-code');
          const wrapper=document.createElement('section');wrapper.id='ghost-color-fixture';wrapper.style.cssText='position:fixed;inset:0;z-index:2000;background:#111;color:#ddd;padding:20px;overflow:auto';document.body.append(wrapper);
          window.ghostCases=[];
          const ghost='src/app.py 42 "next"';
          const sgr=['2','90','38;2;96;96;96','38;2;160;160;160','0'];
          const bytes=new TextEncoder().encode(sgr.map((code,index)=>'> \x1b['+code+'m'+ghost+'\x1b[0m'+(index<sgr.length-1?'\r\n':'\x1b['+ghost.length+'D')).join(''));
          for(const ratio of [1,4.5]){
            const item=document.createElement('article');item.id='ghost-case-'+ratio;const heading=document.createElement('h3');heading.textContent='Claude palette · contrast '+ratio;heading.style.margin='8px 0';const container=document.createElement('div');container.style.cssText='width:850px;height:135px';item.append(heading,container);wrapper.append(item);
            const term=new Terminal({cols:70,rows:5,allowProposedApi:true,disableStdin:true,cursorBlink:false,cursorStyle:'bar',fontSize:17,fontFamily:'Consolas, monospace',minimumContrastRatio:ratio,theme:profile.theme});term.open(container);
            const output=[];term.onData(data=>output.push(data));await new Promise(done=>term.write(bytes,done));
            ghostCases.push({ratio,term,profile,output,bytes:Array.from(bytes),ghost,highlighter:null});
          }
          await new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(done)));
          window.ghostRead=()=>ghostCases.map(item=>({ratio:item.ratio,cursorX:item.term.buffer.active.cursorX,cursorY:item.term.buffer.active.cursorY,input:item.output.slice(),rows:Array.from(item.term.element.querySelector('.xterm-rows').children).map((row,index)=>({text:row.textContent,spans:[...row.querySelectorAll('span')].map(span=>({text:span.textContent,color:getComputedStyle(span).color,opacity:getComputedStyle(span).opacity,classes:span.className})),dim:!!item.term.buffer.active.getLine(index).getCell(2).isDim(),foreground:item.term.buffer.active.getLine(index).getCell(2).getFgColor()}))}));
        }''')
        before = await page.evaluate('ghostRead()')
        native_pixels = {}
        for ratio in [1, 4.5]:
            rows = page.locator('[id="ghost-case-' + str(ratio) + '"] .xterm-rows > div')
            assert await rows.count() == 5
            for row in range(4):
                native_pixels[ratio, row] = await rows.nth(row).screenshot()
        await page.locator('#ghost-color-fixture').screenshot(path=str(tmp_path / 'native-cli-colors.png'))
        await page.evaluate('''async()=>{for(const item of ghostCases)item.highlighter=TerminalClient.createHighlighter(item.term,{profile:item.profile});await new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(done)));}''')
        after = await page.evaluate('ghostRead()')
        highlighted_pixels = {}
        for ratio in [1, 4.5]:
            rows = page.locator('[id="ghost-case-' + str(ratio) + '"] .xterm-rows > div')
            for row in range(4):
                highlighted_pixels[ratio, row] = await rows.nth(row).screenshot()
        await page.locator('#ghost-color-fixture').screenshot(path=str(tmp_path / 'highlighted-cli-colors.png'))
        evidence = {'native': before, 'highlighted': after,
                    'pixel_identical_after_highlighting': {str(ratio): [native_pixels[ratio, row] == highlighted_pixels[ratio, row] for row in range(4)] for ratio in [1, 4.5]}}
        (tmp_path / 'colors.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        try:
            for ratio in [1, 4.5]:
                for row in range(4):
                    assert native_pixels[ratio, row] == highlighted_pixels[ratio, row], f'contrast={ratio}, row={row}: native CLI style changed'
            for old, new in zip(before, after):
                assert new['cursorX'] == old['cursorX'] == 2
                assert new['cursorY'] == old['cursorY'] == 4
                assert new['input'] == old['input'] == []
                assert new['rows'][0]['dim'] is True
                assert [row['foreground'] for row in new['rows']] == [row['foreground'] for row in old['rows']]
            assert not any(session.pty.written for owned in sessions for session in owned)
            assert not errors
        finally:
            await page.evaluate('()=>{for(const item of ghostCases){item.highlighter?.dispose();item.term.dispose();}}')


def test_cursor_key_helper_matches_physical_dom_arrows_in_both_cursor_modes(tmp_path):
    run(lambda playwright: check_cursor_keys(playwright, tmp_path))


async def check_cursor_keys(playwright, tmp_path):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons, _ops, _windows, sessions):
        page = await browser.new_page(viewport={'width': 1200, 'height': 700})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.goto(url)
        await page.wait_for_function('smaWorkspace?.connected&&smaWorkspace.activePane?.ready')
        await page.evaluate(r'''async()=>{
          const profile=await TerminalClient.loadAgentProfile('/agent-modes/claude-code.json','claude-code');
          const box=document.createElement('section');box.id='ghost-key-fixture';box.style.cssText='position:fixed;inset:0;z-index:2000;padding:24px;background:#111;color:#ddd';const title=document.createElement('h3');title.textContent='Owned Chinese ghost · physical / soft arrow parity';const screen=document.createElement('div');screen.style.cssText='width:1000px;height:140px';box.append(title,screen);document.body.append(box);
          window.ghostKeyInput=[];window.ghostKeyTerm=new Terminal({cols:80,rows:3,allowProposedApi:true,cursorBlink:false,cursorStyle:'bar',fontSize:19,fontFamily:'Consolas, monospace',minimumContrastRatio:4.5,theme:profile.theme});ghostKeyTerm.open(screen);ghostKeyTerm.onData(data=>ghostKeyInput.push(data));
          await new Promise(done=>ghostKeyTerm.write('> \x1b[2m继续完善接口 src/app.py 42\x1b[0m',done));const column=ghostKeyTerm.buffer.active.cursorX;await new Promise(done=>ghostKeyTerm.write('\x1b['+(column-2)+'D',done));
          window.ghostKeyHighlighter=TerminalClient.createHighlighter(ghostKeyTerm,{profile});ghostKeyTerm.focus();await new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(done)));
        }''')
        evidence = []
        try:
            for sequence, application, prefix in [('', False, '['), ('\x1b[?1h', True, 'O'), ('\x1b[?1l', False, '[')]:
                if sequence:
                    await page.evaluate('async value=>{await new Promise(done=>ghostKeyTerm.write(value,done));}', sequence)
                assert await page.evaluate('ghostKeyTerm.modes.applicationCursorKeysMode') is application
                for direction, physical, code in [('up', 'ArrowUp', 'A'), ('down', 'ArrowDown', 'B'), ('left', 'ArrowLeft', 'D'), ('right', 'ArrowRight', 'C')]:
                    before = await page.evaluate('ghostKeyInput.length')
                    await page.keyboard.press(physical)
                    await page.wait_for_function('count=>ghostKeyInput.length===count+1', arg=before)
                    result = await page.evaluate('direction=>({physical:ghostKeyInput.at(-1),soft:TerminalClient.cursorKey(ghostKeyTerm,direction),cursorX:ghostKeyTerm.buffer.active.cursorX,cursorY:ghostKeyTerm.buffer.active.cursorY,text:ghostKeyTerm.buffer.active.getLine(0).translateToString(true),dim:!!ghostKeyTerm.buffer.active.getLine(0).getCell(2).isDim()})', direction)
                    assert result['physical'] == result['soft'] == '\x1b' + prefix + code
                    assert result['cursorX'] == 2 and result['cursorY'] == 0
                    assert result['dim'] and result['text'] == '> 继续完善接口 src/app.py 42'
                    result['application_mode'] = application
                    result['direction'] = direction
                    evidence.append(result)
            await page.locator('#ghost-key-fixture').screenshot(path=str(tmp_path / 'chinese-ghost-cursor-before.png'))
            (tmp_path / 'physical-soft-keys.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
            assert not any(session.pty.written for owned in sessions for session in owned)
            assert not errors
        finally:
            await page.evaluate('()=>{ghostKeyHighlighter.dispose();ghostKeyTerm.dispose();}')
