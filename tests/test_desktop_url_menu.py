"""URL clicks offer a menu: open on the host computer or on the viewing device."""
from pathlib import Path
import json
from unittest.mock import patch

import pytest

from test_workspace_profile_rooms import fixture, run


@pytest.mark.parametrize('renderer', ['dom', 'webgl'])
def test_url_menu_opens_on_host_or_device(renderer):
    run(lambda playwright: check(playwright, renderer))


async def check(playwright, renderer):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, ops, _windows, sessions):
        pty = sessions[0][0].pty
        pty.inbox.put(b'docs at https://example.com/docs?a=1 end\r\n')
        page = await browser.new_page(viewport={'width': 1536, 'height': 960})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.goto(url.replace('renderer=dom', 'renderer=' + renderer))
        await page.wait_for_function('smaWorkspace?.activePane?.ready')
        await page.wait_for_function("""()=>{
          const b=smaWorkspace.activePane.term.buffer.active;
          for(let y=0;y<b.length;y++){if(b.getLine(y)?.translateToString(true).includes('example.com'))return true;}
          return false;
        }""")
        await page.evaluate("()=>{window.__opened=[];window.open=url=>{window.__opened.push(String(url));return null;};window.__frames=[];const raw=WebSocket.prototype.send;WebSocket.prototype.send=function(data){try{const b=data instanceof ArrayBuffer?new Uint8Array(data):ArrayBuffer.isView(data)?new Uint8Array(data.buffer,data.byteOffset,data.byteLength):null;if(b&&b.length>4)window.__frames.push(b[4]);}catch{}return raw.apply(this,arguments);};}")

        position = '''()=>{
          const p=smaWorkspace.activePane,t=p.term,b=t.buffer.active;
          let row=-1;for(let y=0;y<b.length;y++){if(b.getLine(y)?.translateToString(true).includes('https://example.com')){row=y;break;}}
          if(row<0)throw Error('url line missing');
          const col=b.getLine(row).translateToString(true).indexOf('https://');
          const e=t.element.getBoundingClientRect(),cell=t._core._renderService.dimensions.css.cell;
          return {x:e.left+(col+4)*cell.width,y:e.top+(row-b.viewportY)*cell.height+cell.height/2};
        }'''
        pos = await page.evaluate(position)
        await page.mouse.move(pos['x'], pos['y'])
        await page.mouse.click(pos['x'], pos['y'])
        await page.wait_for_selector('.url-menu')
        assert await page.locator('.url-menu button', has_text='在电脑上打开').count() == 1
        assert await page.locator('.url-menu button', has_text='在本设备打开').count() == 1

        # Device path: window.open on the viewing client.
        await page.locator('.url-menu button', has_text='在本设备打开').click()
        await page.wait_for_function("window.__opened.length===1")
        assert await page.evaluate("window.__opened[0]") == 'https://example.com/docs?a=1'
        assert await page.locator('.url-menu').count() == 0

        # Host path: daemon opens the default browser on the host computer.
        # xterm only re-arms links after the mouse crosses another cell, so step
        # through a different row on the way back to the link.
        await page.mouse.move(pos['x'], pos['y'] + 40)
        await page.mouse.click(pos['x'], pos['y'])
        await page.wait_for_selector('.url-menu')
        with patch('supermonoagent.daemon.webbrowser.open', return_value=True) as host_open:
            await page.locator('.url-menu button', has_text='在电脑上打开').click()
            for _ in range(200):
                if host_open.call_count:
                    break
                await page.wait_for_timeout(50)
        assert host_open.call_count == 1, 'host browser open never happened'
        assert host_open.call_args[0][0] == 'https://example.com/docs?a=1'
        assert await page.locator('.url-menu').count() == 0

        # A host without the negotiated capability must not receive the op.
        await page.evaluate("()=>{for(const h of smaWorkspace.connections.values())h.capabilities.url_open=undefined;}")
        await page.mouse.move(pos['x'], pos['y'] + 40)
        await page.mouse.click(pos['x'], pos['y'])
        await page.wait_for_selector('.url-menu')
        frames_before = (await page.evaluate('window.__frames')).count(25)
        with patch('supermonoagent.daemon.webbrowser.open', return_value=True) as silent_open:
            await page.locator('.url-menu button', has_text='在电脑上打开').click()
            await page.wait_for_timeout(400)
        assert silent_open.call_count == 0
        frames_after = (await page.evaluate('window.__frames')).count(25)
        assert frames_after == frames_before, 'op 25 leaked to a host without the capability'
        notice = await page.evaluate("document.getElementById('notice').textContent")

        artifacts = Path('.omx/artifacts/desktop-url-menu')
        artifacts.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(artifacts / f'{renderer}-menu.png'))
        (artifacts / f'{renderer}-result.json').write_text(json.dumps({
            'renderer': renderer, 'device_url': await page.evaluate('window.__opened[0]'),
            'host_url': host_open.call_args[0][0], 'notice': notice, 'verdict': 'pass',
        }, indent=2), encoding='utf-8')
        assert not errors
