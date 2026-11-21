"""Cross-repository pilot acceptance with the real MCP and WS server and disposable identities."""
import asyncio
import json
import math
import os
from pathlib import Path
import secrets
import socket
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest
from websockets.asyncio.server import serve

from android_journal_fixture import Fixture
from test_workspace_profile_rooms import fixture, run


async def measure_open_dashboard(page, endpoint, publishers):
    """Time from a real MCP receipt to the first visible task row in an existing viewport."""
    from agent_hub.client import call_tool

    panel = page.locator('#sma-cockpit')
    await panel.locator('[data-tab=tasks]').click()
    await panel.get_by_text('alpha evidence',exact=True).wait_for()
    first_seen, committed = {}, {}
    def report_visible(keys):
        timestamp = time.perf_counter()
        for key in keys:
            first_seen.setdefault(key,timestamp)
    await page.expose_function('hubPerfVisible',report_visible)
    await page.evaluate('''() => {
      const main=document.querySelector('#sma-cockpit .hub-body');
      const seen=new Set();
      new MutationObserver(()=>{
        const added=[];
        for(const node of main.querySelectorAll('article strong')){
          if(node.textContent.startsWith('bench-') && !seen.has(node.textContent)){
            seen.add(node.textContent);added.push(node.textContent);
          }
        }
        if(added.length)window.hubPerfVisible(added);
      }).observe(main,{childList:true,subtree:true});
      window.benchTerminalFrames=[];
      for(const host of smaWorkspace.connections.values())if(host.ws){
        const send=host.ws.send.bind(host.ws);
        host.ws.send=frame=>{
          if(typeof frame!=='string'){
            const op=TerminalClient.unpack(new Uint8Array(frame)).op;
            if(op===5 || op===17)benchTerminalFrames.push(op);
          }
          return send(frame);
        };
      }
    }''')
    def publish(project, token):
        for index in range(10):
            summary=f'bench-{project}-{index}'
            event=dict(event_id=f'e{index}',task_id=f't{index}',attempt_id=f'a{index}',session_id=f's{index}',
                       account='shared-account',host='host-'+project,window='same window',window_id=f'w{index}',
                       tool='codex',status='blocked',summary=summary)
            call_tool('report_step',dict(project=project,event=event),endpoint=endpoint,token=token)
            committed[summary]=time.perf_counter()
    # Client connection/TLS initialization must not block the separate server
    # and Playwright event loop; in production each publisher lives on its host.
    await asyncio.gather(*(asyncio.to_thread(publish,project,token) for project,token in publishers.items()))
    async with asyncio.timeout(12):
        while len(first_seen)<len(committed):
            await asyncio.sleep(.02)
    delays=sorted(round(max(0,first_seen[key]-at)*1000) for key,at in committed.items())
    frames=await page.evaluate('benchTerminalFrames')
    evidence=dict(synthetic=True,transport='MCP + WS on loopback',hosts=2,sessions=len(committed),
                  p95_ms=delays[math.ceil(len(delays)*.95)-1],maximum_ms=max(delays),
                  input_or_resize_frames=frames,values_ms=delays)
    if directory:=os.environ.get('SMA_COCKPIT_VISUAL_DIR'):
        path=Path(directory);path.mkdir(parents=True,exist_ok=True)
        (path/'20-session-latency.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
    print('Dashboard receipt-to-visible:',evidence)
    assert len(committed)==20 and not frames
    assert evidence['p95_ms']<=5000


def test_hub_mcp_events_visible_in_desktop_and_android_documents(tmp_path):
    # CI may run the existing terminal project without the separate Hub environment.
    pytest.importorskip("agent_hub.server")
    run(lambda playwright: verify(playwright, tmp_path))


async def verify(playwright, tmp_path):
    import uvicorn
    from agent_hub.client import call_tool_async
    from agent_hub.server import create_app, token_digest
    from agent_hub.storage import Store

    accounts = ["shared-account"]
    reader = secrets.token_urlsafe(32)
    publisher = secrets.token_urlsafe(32)
    publishers = {p:secrets.token_urlsafe(32) for p in ('alpha','beta')}
    grants = []
    for project in ("alpha","beta"):
        for token, subject, scopes in ((reader,"viewer",["tasks:read","usage:read"]),
                                       (publisher,"worker",["events:append","usage:collect"])):
            grants.append(dict(project=project,subject=subject,sha256=token_digest(token),scopes=scopes,
                               accounts=accounts,host="test-host",tools=["codex"]))
        grants.append(dict(project=project,subject='producer-'+project,sha256=token_digest(publishers[project]),
                           scopes=['events:append'],accounts=accounts,host='host-'+project,tools=['codex']))
    auth = tmp_path / "grants.json"
    auth.write_text(json.dumps({"grants":grants}), encoding="utf-8")
    auth.chmod(0o600)
    store = Store(tmp_path / "hub.sqlite3")
    async with fixture(playwright) as (browser,url,_endpoint,_trap,_messages,_daemons,_ops,_wins,sessions):
        mobile = Fixture()
        async with serve(mobile.socket,"127.0.0.1",0,process_request=mobile.http) as mobile_server:
            mobile.origin = f"http://127.0.0.1:{mobile_server.sockets[0].getsockname()[1]}"
            workspace = urlsplit(url)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1",0))
                authority = f"127.0.0.1:{listener.getsockname()[1]}"
                server = create_app(store,auth,hosts=[authority],
                                    origins=[mobile.origin,workspace.scheme+"://"+workspace.netloc])
                service = uvicorn.Server(uvicorn.Config(server,log_level="critical",access_log=False))
                job = asyncio.create_task(service.serve(sockets=[listener]))
                try:
                    async with asyncio.timeout(10):
                        while not service.started:
                            if job.done():
                                await job
                            await asyncio.sleep(.05)
                    async def call(name, project, **args):
                        return await call_tool_async(name,{"project":project,**args},
                                                     endpoint=f"http://{authority}/hub/mcp",token=publisher)
                    for project in ("alpha","beta"):
                        event = dict(event_id="e1",task_id="same-task",attempt_id="a1",tool="codex",
                                     account=accounts[0],status="blocked",host="test-host",window=project+" window",
                                     summary=project+" evidence",
                                     wiki=dict(entry_id="note", version="1",path="omx_wiki/example.md",
                                               body=project+" linked wiki proof"))
                        await call("report_step",project,event=event)
                        usage = await call("claim_usage_job",project,account=accounts[0],worker="worker")
                        now = datetime.now(timezone.utc)
                        await call("record_usage_snapshot",project,account=accounts[0],
                                   snapshot=dict(job_id=usage["job_id"],lease_token=usage["lease_token"],status="ok",
                                                 observed_at=now.isoformat(),pool="plan",windows=[
                                                     dict(period="weekly",unit="percent",remaining=77,
                                                          reset_at=(now+timedelta(days=2)).isoformat())]))
                    for surface in ("android","desktop"):
                        page = await browser.new_page(viewport={"width":1280 if surface=="desktop" else 390,
                                                                "height":820},is_mobile=surface=="android")
                        page.set_default_timeout(7000)
                        try:
                            await page.goto(url if surface=="desktop" else mobile.origin)
                            if surface == "android":
                                await page.locator("a").first.click()
                                await page.wait_for_function("window.MobileTerminal?.ready")
                            else:
                                await page.wait_for_function("window.smaWorkspace?.activePane?.ready")
                            await page.locator("#app-menu-toggle" if surface=="desktop" else "#options").click()
                            await page.locator("#cockpit-open").click()
                            panel=page.locator("#sma-cockpit")
                            await panel.get_by_label("驾驶舱服务地址").fill(f"ws://{authority}/hub/view")
                            await panel.get_by_label("项目",exact=True).fill("*")
                            await panel.get_by_label("只读访问凭据").fill(reader)
                            await panel.get_by_role("button",name="连接项目").click()
                            await panel.get_by_label("向总管提问").wait_for()
                            await panel.locator("[data-tab=tasks]").click()
                            item = panel.get_by_text("beta evidence",exact=True).locator("..")
                            await item.get_by_role("button",name="查看任务").click()
                            await panel.get_by_text("beta window",exact=True).wait_for()
                            await panel.get_by_role("button",name="查看 wiki 证据").click()
                            await panel.get_by_text("beta linked wiki proof",exact=True).wait_for()
                            await panel.locator("[data-tab=usage]").click()
                            await panel.get_by_text("本周 · 剩余 77 %",exact=True).first.wait_for()
                            assert await panel.get_by_text("shared-account",exact=True).count() == 2
                            await panel.locator("[data-tab=chat]").click()
                            await panel.get_by_label("向总管提问").fill("status?")
                            await panel.get_by_role("button",name="发送给总管").click()
                            await page.wait_for_function('document.querySelector("#sma-cockpit .hub-chat").textContent.includes("beta evidence")')
                            assert not any(term.pty.written for group in sessions for term in group)
                            assert mobile.input_frames == mobile.resize_frames == 0
                            if surface=='desktop':
                                await measure_open_dashboard(page,f'http://{authority}/hub/mcp',publishers)
                        finally:
                            await page.close()
                finally:
                    service.should_exit=True
                    await asyncio.wait_for(job,10)
