"""Real mobile assets against owned synthetic transport; no user session."""
import asyncio
import json
from pathlib import Path
import time
from urllib.parse import quote

import pytest
from websockets.asyncio.server import serve
from test_android_unified import UnifiedFixture, EDGE, TOKEN, ROOM, choose


@pytest.mark.parametrize('renderer', ['dom', 'webgl'])
def test_compact_mobile_bar_keeps_one_row_and_expanded_draft(renderer):
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Owned headless Edge unavailable')
    asyncio.run(check_compact(playwright, renderer))


async def check_compact(playwright, renderer):
    fixture = UnifiedFixture()
    async with serve(fixture.socket, '127.0.0.1', 0, process_request=fixture.http) as server:
        fixture.origin = 'http://127.0.0.1:' + str(server.sockets[0].getsockname()[1])
        config = dict(endpoint=fixture.origin, code=ROOM, token=TOKEN, renderer=renderer)
        url = fixture.origin + '/mobile/index.html#' + quote(json.dumps(config))
        async with playwright.async_playwright() as runtime:
            browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
            try:
                page = await browser.new_page(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
                await page.goto(url)
                await page.wait_for_function('window.MobileTerminal?.ready')
                artifacts = Path(__file__).resolve().parents[1] / '.omx/artifacts/compact-mobile' / (renderer + '-' + str(time.time_ns()))
                artifacts.mkdir(parents=True)
                assert await page.locator('#keys').is_hidden()
                assert await page.locator('#terminal-actions').is_hidden()
                assert await page.locator('#raw-input').get_attribute('aria-pressed') == 'true'
                assert await page.locator('#raw-input').inner_text() == '原始'
                assert await page.evaluate('!MobileTerminal.terminal.textarea.readOnly && document.activeElement!==MobileTerminal.terminal.textarea')
                assert not fixture.inputs and not fixture.resizes
                await page.screenshot(path=str(artifacts / 'default-raw.png'))
                await page.locator('#raw-input').click()
                assert await page.locator('#raw-input').get_attribute('aria-pressed') == 'false'
                assert await page.locator('#raw-input').inner_text() == '草稿'
                await choose(page, 'grok')
                assert await page.locator('#raw-input').get_attribute('aria-pressed') == 'true'
                await choose(page, 'codex')
                assert await page.locator('#raw-input').get_attribute('aria-pressed') == 'false'
                await page.locator('#raw-input').click()
                assert await page.locator('#raw-input').get_attribute('aria-pressed') == 'true'
                await page.locator('#raw-input').click()
                draft = 'long local draft ' * 30 + '\nsecond line'
                await page.locator('#quick-draft').fill(draft)
                verdicts = []
                for width, height in [(390, 844), (844, 390), (768, 1200), (1200, 768), (320, 640)]:
                    await page.set_viewport_size(dict(width=width, height=height))
                    await page.evaluate('()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
                    evidence = await page.evaluate('''()=>{
                      const bar=document.getElementById('bottom-toolbar').getBoundingClientRect();
                      const elements=[document.getElementById('quick-draft'),...document.querySelectorAll('#bottom-toolbar button:not([hidden])')];
                      return {height:bar.height,overflow:document.documentElement.scrollWidth>innerWidth,
                        row:elements.map(el=>{const r=el.getBoundingClientRect();return {id:el.id,y:r.y,height:r.height,left:r.left,right:r.right};})};
                    }''')
                    assert evidence['height'] <= 49 and not evidence['overflow'], evidence
                    row = evidence['row']
                    assert all(abs(item['y'] - row[0]['y']) < 1 and item['height'] == 40 and item['left'] >= 0 and item['right'] <= width for item in row), evidence
                    assert row[0]['left'] == 4 and row[0]['right'] - row[0]['left'] >= width - 210, evidence
                    assert await page.locator('#quick-draft').input_value() == draft
                    image = f'{width}x{height}.png'
                    await page.screenshot(path=str(artifacts / image))
                    verdicts.append(dict(viewport=dict(width=width, height=height), screenshot=image, verdict='pass', evidence=evidence))
                await page.locator('#tools-toggle').click()
                await page.locator('#mode').click()
                assert await page.locator('#composer').is_visible()
                assert await page.locator('#draft').input_value() == draft
                assert await page.locator('#draft').evaluate('el=>el.getBoundingClientRect().height') > 40
                await page.locator('#composer-close').click()
                assert await page.locator('#quick-draft').input_value() == draft
                assert await page.locator('#keys').is_visible()
                await page.goto(url)
                await page.wait_for_function('window.MobileTerminal?.ready')
                assert await page.locator('#keys').is_visible(), 'Explicit expanded preference must survive reload'
                assert not fixture.inputs and not fixture.resizes
                (artifacts / 'visual-verdict.json').write_text(json.dumps(dict(verdict='pass', renderer=renderer, physical_android=False, verdicts=verdicts), indent=2), encoding='utf-8')
                await page.evaluate('MobileTerminal.pause()')
            finally:
                await browser.close()
                for job in list(fixture.jobs):
                    job.cancel()
                await asyncio.gather(*fixture.jobs, return_exceptions=True)
