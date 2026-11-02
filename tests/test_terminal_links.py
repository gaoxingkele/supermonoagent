"""Clicking an http(s) URL in terminal output asks the local shell to open it."""
from pathlib import Path
import pytest

from test_workspace_profile_rooms import fixture, run


async def click_url_and_expect(page, needle, expected):
    await page.evaluate('''()=>{
      window.opened=[];window.pywebview={api:{open_external:async value=>opened.push(value)}};
    }''')
    await page.evaluate(r'''async needle=>{
      const t=smaWorkspace.activePane.term;
      await new Promise(done=>t.write('\x1b[2J\x1b[Hsee '+needle+' owned',done));
      t.focus();
    }''', needle)
    await page.wait_for_timeout(300)
    # The PTY may echo and shift rows; locate the URL's current buffer row first.
    point = await page.evaluate('''needle=>{
      const pane=smaWorkspace.activePane,t=pane.term,buffer=t.buffer.active;
      const ydisp=t._core?._bufferService?.buffer?.ydisp||0;
      for(let y=0;y<buffer.length;y++){
        const text=buffer.getLine(y)?.translateToString(true)||'';
        const at=text.indexOf(needle);
        if(at>=0)return {y,startX:at,endX:at+needle.length,ydisp};
      }
      return null;
    }''', needle)
    assert point, 'URL row not found in buffer'
    rect = await page.evaluate('''()=>{
      const r=smaWorkspace.activePane.term.element.getBoundingClientRect();
      return {x:r.x,y:r.y,w:r.width,h:r.height,cols:smaWorkspace.activePane.term.cols,rows:smaWorkspace.activePane.term.rows};
    }''')
    cw=rect['w']/rect['cols']; ch=rect['h']/rect['rows']
    click_x=rect['x']+(point['startX']+2+0.5)*cw
    click_y=rect['y']+(point['y']-point['ydisp']+0.5)*ch
    await page.mouse.move(click_x,click_y)
    await page.wait_for_timeout(200)
    await page.mouse.down(); await page.mouse.up()
    await page.wait_for_function('opened.length===1')
    assert await page.evaluate('opened') == [expected]


async def check(playwright):
    async with fixture(playwright) as (browser, url, *_):
        page = await browser.new_page()
        await page.goto(url)
        await page.wait_for_function('smaWorkspace.activePane?.ready')
        await click_url_and_expect(page, 'http://127.0.0.1:8777/', 'http://127.0.0.1:8777/')


def test_click_terminal_url_opens_local_browser():
    run(check)


async def check_www(playwright):
    async with fixture(playwright) as (browser, url, *_):
        page = await browser.new_page()
        await page.goto(url)
        await page.wait_for_function('smaWorkspace.activePane?.ready')
        await click_url_and_expect(page, 'www.example.com/docs', 'https://www.example.com/docs')


def test_click_bare_www_link_normalizes_scheme():
    run(check_www)
