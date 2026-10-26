"""Context menus against owned browser sessions; native clipboard is mocked."""
from pathlib import Path
import pytest

from test_workspace_profile_rooms import fixture, run, server_barrier, eventually


SCRIPT = Path(__file__).resolve().parents[1] / 'src/supermonoagent/static/clipboard-context-menu.js'


def test_terminal_helper_textarea_context_paste_reaches_pty_once_without_enter():
    run(check_terminal)


async def check_terminal(playwright):
    async with fixture(playwright) as (browser, url, _, _trap, _messages, _daemons,
                                      _ops, _windows, sessions):
        page = await browser.new_page()
        await page.goto(url)
        await page.wait_for_function('smaWorkspace.activePane?.ready')
        await page.evaluate(SCRIPT.read_text(encoding='utf-8'))
        await page.evaluate('''()=>{
          window.clipboardReads=0;window.pywebview={api:{
            clipboard_read_text:async()=>{clipboardReads++;return 'owned context paste';},
            clipboard_has_files:async()=>false
          }};
          smaWorkspace.activePane.term.textarea.dispatchEvent(new MouseEvent('contextmenu',
            {bubbles:true,cancelable:true,clientX:300,clientY:300}));
        }''')
        await page.locator('#clipboard-context-paste').click()
        sid = await page.evaluate('smaWorkspace.activePane.raw')
        receiver = next(item for item in sessions[0] if item.id == sid)
        await eventually(lambda: bytes(receiver.pty.written) == b'owned context paste')
        await server_barrier(page, 'local')
        assert bytes(receiver.pty.written) == b'owned context paste'
        assert await page.evaluate('clipboardReads') == 1


@pytest.mark.parametrize('change', ['caret', 'readonly'])
def test_delayed_field_paste_revalidates_caret_and_writability(change):
    run(lambda playwright: check_caret(playwright, change))


async def check_caret(playwright, change):
    async with fixture(playwright) as (browser, url, *_):
        page = await browser.new_page()
        await page.goto(url)
        await page.wait_for_function('smaWorkspace.activePane?.ready')
        await page.evaluate(SCRIPT.read_text(encoding='utf-8'))
        await page.evaluate('''()=>{
          window.pywebview={api:{clipboard_read_text:()=>new Promise(resolve=>window.releasePaste=resolve)}};
          const field=document.createElement('textarea');field.id='owned-field';field.value='abcdef';
          document.body.append(field);field.focus();field.setSelectionRange(2,2);
          field.dispatchEvent(new KeyboardEvent('keydown',{key:'v',ctrlKey:true,bubbles:true,cancelable:true}));
        }''')
        await page.wait_for_function('typeof releasePaste==="function"')
        await page.evaluate('''change=>{const field=document.querySelector('#owned-field');
          if(change==='caret')field.setSelectionRange(5,5);else field.readOnly=true;releasePaste('WRONG');}''', change)
        await page.wait_for_function('!document.querySelector("#clipboard-context-status").hidden')
        assert await page.locator('#owned-field').input_value() == 'abcdef'


def test_modal_menu_can_copy_and_replace_selection_and_escape_restores_focus():
    run(check_modal)


async def check_modal(playwright):
    async with fixture(playwright) as (browser, url, *_):
        page = await browser.new_page()
        await page.goto(url)
        await page.wait_for_function('smaWorkspace.activePane?.ready')
        await page.evaluate(SCRIPT.read_text(encoding='utf-8'))
        await page.evaluate('''()=>{
          window.copied=[];window.pywebview={api:{clipboard_write_text:async text=>copied.push(text),clipboard_read_text:async()=> 'XY'}};
          const dialog=document.createElement('dialog');dialog.id='owned-dialog';
          const field=document.createElement('textarea');field.id='owned-field';field.value='abcdef';
          dialog.append(field);document.body.append(dialog);dialog.showModal();field.focus();field.setSelectionRange(2,4);
        }''')
        async def menu():
            await page.locator('#owned-field').evaluate('field=>field.dispatchEvent(new MouseEvent("contextmenu",{bubbles:true,cancelable:true,clientX:300,clientY:300}))')
        await menu()
        assert await page.locator('#owned-dialog>#clipboard-context-menu').count() == 1
        await page.locator('#clipboard-context-copy').click()
        await page.wait_for_function('copied.length===1')
        assert await page.evaluate('copied') == ['cd']
        await menu()
        await page.locator('#clipboard-context-paste').click()
        await page.wait_for_function('document.querySelector("#owned-field").value==="abXYef"')
        await menu()
        await page.keyboard.press('Escape')
        assert await page.locator('#owned-dialog').evaluate('dialog=>dialog.open')
        assert await page.locator('#owned-field').evaluate('field=>document.activeElement===field')


def test_terminal_selection_copy_uses_native_bridge_without_interrupt():
    run(check_copy)


async def check_copy(playwright):
    async with fixture(playwright) as (browser, url, _, _trap, _messages, _daemons,
                                      _ops, _windows, sessions):
        page = await browser.new_page()
        await page.goto(url)
        await page.wait_for_function('smaWorkspace.activePane?.ready')
        await page.evaluate(SCRIPT.read_text(encoding='utf-8'))
        await page.evaluate(r'''async()=>{
          window.copied=[];window.pywebview={api:{clipboard_write_text:async value=>copied.push(value)}};
          const t=smaWorkspace.activePane.term;
          await new Promise(done=>t.write('\x1b[2J\x1b[HOWNED_SELECTION',done));
          t.select(0,0,15);t.focus();
        }''')
        await page.keyboard.press('Control+c')
        await page.wait_for_function('copied.length===1')
        assert await page.evaluate('copied') == ['OWNED_SELECTION']
        await server_barrier(page, 'local')
        assert all(not item.pty.written for owned in sessions for item in owned)


def test_delayed_terminal_paste_rejects_new_connection_generation():
    run(check_generation)


async def check_generation(playwright):
    async with fixture(playwright) as (browser, url, _, _trap, _messages, _daemons,
                                      _ops, _windows, sessions):
        page = await browser.new_page()
        await page.goto(url)
        await page.wait_for_function('smaWorkspace.activePane?.ready')
        await page.evaluate(SCRIPT.read_text(encoding='utf-8'))
        await page.evaluate('''()=>{
          window.pywebview={api:{clipboard_read_text:()=>new Promise(resolve=>window.releasePaste=resolve)}};
          smaWorkspace.activePane.term.textarea.dispatchEvent(new MouseEvent('contextmenu',
            {bubbles:true,cancelable:true,clientX:300,clientY:300}));
        }''')
        await page.locator('#clipboard-context-paste').click()
        await page.wait_for_function('typeof releasePaste==="function"')
        await page.evaluate('()=>{smaWorkspace.activePane.generation++;releasePaste("WRONG_TARGET");}')
        await page.wait_for_function('!document.querySelector("#clipboard-context-status").hidden')
        await server_barrier(page, 'local')
        assert all(not item.pty.written for owned in sessions for item in owned)


def test_field_paste_respects_maxlength_and_readonly():
    run(check_limits)


async def check_limits(playwright):
    async with fixture(playwright) as (browser, url, *_):
        page = await browser.new_page()
        await page.goto(url)
        await page.wait_for_function('smaWorkspace.activePane?.ready')
        await page.evaluate(SCRIPT.read_text(encoding='utf-8'))
        await page.evaluate('''()=>{
          window.pywebview={api:{clipboard_read_text:async()=> '12345'}};
          const field=document.createElement('input');field.id='owned-field';field.value='abcd';field.maxLength=6;
          document.body.append(field);field.focus();field.setSelectionRange(1,3);
          field.dispatchEvent(new KeyboardEvent('keydown',{key:'v',ctrlKey:true,bubbles:true,cancelable:true}));
        }''')
        await page.wait_for_function('document.querySelector("#owned-field").value==="a1234d"')
        await page.locator('#owned-field').evaluate('''field=>{
          field.readOnly=true;
          field.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:300,clientY:300}));
        }''')
        assert await page.locator('#clipboard-context-paste').is_disabled()


def test_editor_browser_clipboard_files_queue_without_typing_file_text_or_uploading():
    from test_workspace_composer import prepared
    async def exercise(playwright):
        async with fixture(playwright) as (browser, url, _, _trap, _messages, _daemons,
                                          _ops, _windows, sessions):
            page, errors = await prepared(browser, url)
            await page.evaluate(SCRIPT.read_text(encoding='utf-8'))
            await page.locator('#terminal-composer-open').click()
            await page.locator('#terminal-composer-text').fill('keep draft')
            for name in ['first.png', 'second.pdf']:
                prevented = await page.evaluate('''name=>{
                  const data=new DataTransfer();data.items.add(new File(['owned attachment'],name));
                  data.setData('text/plain','DO_NOT_TYPE_FILE_TEXT');
                  const event=new ClipboardEvent('paste',{bubbles:true,cancelable:true,clipboardData:data});
                  document.querySelector('#terminal-composer-text').dispatchEvent(event);return event.defaultPrevented;
                }''', name)
                assert prevented
            assert await page.locator('#terminal-composer-files li').count() == 2
            assert await page.locator('#terminal-composer-text').input_value() == 'keep draft'
            assert await page.evaluate('composerCalls.length') == 0
            assert all(not item.pty.written for owned in sessions for item in owned)
            assert not errors
    run(exercise)
