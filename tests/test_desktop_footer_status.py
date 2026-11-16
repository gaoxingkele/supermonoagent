"""Bottom alignment after late native-directory startup; owned browser only."""
from pathlib import Path
import json

import pytest

from test_workspace_profile_rooms import fixture, run, server_barrier

STATIC = Path(__file__).resolve().parents[1] / 'src/supermonoagent/static'


@pytest.mark.parametrize('renderer', ['dom', 'webgl'])
def test_directory_and_launch_status_never_precede_input(renderer):
    run(lambda playwright: check_status(playwright, renderer))


async def check_status(playwright, renderer):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, daemons, ops, _windows, sessions):
        launch_dirs = {s.id: 'C:\\owned\\launch-' + s.id for s in sessions[0]}
        for s in sessions[0]:
            s.cwd = launch_dirs[s.id]
        page = await browser.new_page(viewport={'width': 1536, 'height': 960})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.goto(url.replace('renderer=dom', 'renderer=' + renderer))
        await page.wait_for_function('smaWorkspace?.activePane?.ready&&window.SuperMonoTerminalComposer')
        await page.locator('#terminal-composer-text').fill('retain local draft')
        # The native configuration becomes available after the composer, as in
        # the desktop pywebview boot. File RPC stays in this owned stub.
        await page.evaluate(r'''()=>{
          window.SuperMonoFileWorkspaceConfig={enabled:true,broker_url:'ws://127.0.0.1:1',local_room:'layout'};
          window.SuperMonoFileClient={brokerURL:v=>v,createClient:()=>({
            connect:async()=>{},close(){},request:async(action,sid)=>{
              if(action!=='workspace.get')throw Error('Unexpected file mutation');
              const p=smaWorkspace.activePane,w=p.host.catalog.find(w=>w.sessions.some(s=>s.id===sid));
              return {window_id:w.id,session_id:sid,daemon_epoch:'owned-layout',revision:1,
                project_status:'ready',source:'configured',project_dir:'C:\\owned\\configured-'+sid,
                exchange_dir:'C:\\owned\\configured-'+sid+'\\tmp'};
            }})};
        }''')
        await page.evaluate((STATIC / 'file-workspace-ui.js').read_text(encoding='utf-8'))
        await page.wait_for_function('document.querySelector("#file-workspace-project")?.title.includes("configured-")')
        assert await page.locator('footer #file-workspace-footer').count() == 0
        assert await page.locator('footer #status').count() == 0
        assert await page.locator('.workspace-toolbar #status').count() == 1
        for index in (1, 0):
            await page.locator('#tabs .tab>button:first-child').nth(index).click()
            sid = await page.evaluate('smaWorkspace.activePane.raw')
            await page.wait_for_function('sid=>document.querySelector("#file-workspace-project").title.includes(sid)', arg=sid)
            assert launch_dirs[sid] in await page.locator('#status').inner_text()
            assert (await page.locator('#status').inner_text()).startswith('启动目录：')
            assert await page.locator('#status').get_attribute('title') == await page.locator('#status').inner_text()
        assert await page.locator('#terminal-composer-text').input_value() == 'retain local draft'
        artifacts = Path('.omx/artifacts/desktop-footer-status')
        artifacts.mkdir(parents=True, exist_ok=True)
        verdicts = []
        for width, height in [(1536, 960), (1100, 760), (390, 844), (844, 390)]:
            await page.set_viewport_size({'width': width, 'height': height})
            await page.locator('#terminal-composer-text').click()
            geometry = await page.evaluate('''()=>{const rect=id=>{const r=document.querySelector(id).getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height};};return {input:rect('#terminal-composer-text'),footer:rect('main>footer'),status:rect('#status'),stage:rect('#stage'),overflow:document.documentElement.scrollWidth>innerWidth+1};}''')
            assert abs(geometry['input']['left'] - geometry['footer']['left']) <= 1, geometry
            assert geometry['footer']['height'] == 44, geometry
            assert geometry['status']['bottom'] <= geometry['stage']['top'] + 1, geometry
            assert not geometry['overflow'], geometry
            await page.screenshot(path=str(artifacts / f'{renderer}-{width}x{height}.png'))
            verdicts.append(dict(viewport=[width, height], geometry=geometry, verdict='pass'))
        await server_barrier(page, 'local')
        assert not any(op == 17 for op, _body, _client in ops[0])
        assert not errors
        (artifacts / f'{renderer}-verdict.json').write_text(json.dumps(verdicts, indent=2), encoding='utf-8')
