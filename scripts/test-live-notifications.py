"""Test the enabled native monitor through the existing relay, without PTY input.

Credentials stay in memory. Probe events are explicitly labelled as tests. This
does not configure accounts, replace saved targets, or bypass a phone keyguard.
"""
import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
import secrets
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from websockets.asyncio.client import connect
from supermonoagent.protocol import pack_json, unpack, unpack_json


async def run(args):
    spec = importlib.util.spec_from_file_location('notification_fixture', ROOT / 'scripts/test-android-notifications.py')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    device = helper.Device(args.adb, args.serial)
    device.choose()
    config = json.loads(args.credentials.read_text(encoding='utf-8-sig'))
    report = {'passed':False, 'existing_public_relay':True, 'user_terminal_input_sent':False,
              'probe_messages_labelled':True, 'checks':[], 'errors':[]}
    args.output.mkdir(parents=True, exist_ok=True)
    doze = False
    try:
        health = json.load(urllib.request.urlopen('http://127.0.0.1:6790/health', timeout=3))
        assert health.get('subscribers', 0) > 0, 'Enable the phone notification monitor first'
        url = config['endpoint'].replace('https:', 'wss:').replace('http:', 'ws:').rstrip('/')+'/ws'
        async with connect(url, open_timeout=8) as ws:
            await ws.send(json.dumps({'op':'phone', 'code':config['code']+'-events', 'token':config['token']}))
            assert json.loads(await asyncio.wait_for(ws.recv(),8)).get('op') == 'ready'
            await ws.send(pack_json(1, {'token':config['token'], 'multiplex':True}))
            op, _ = unpack(await asyncio.wait_for(ws.recv(),8))
            assert op == 2
            for phase in ['background','locked'] + (['doze'] if args.doze else []):
                if phase == 'background':
                    device.run('shell','input','keyevent','KEYCODE_HOME')
                elif phase == 'locked':
                    device.run('shell','input','keyevent','223')
                else:
                    doze = True
                    response = device.run('shell','dumpsys','deviceidle','force-idle')
                    report['doze_command_result'] = response[:120]
                    report['doze_state'] = device.run('shell','dumpsys','deviceidle','get','deep')[:80]
                await asyncio.sleep(6)  # Respect owner probe rate limit, allow sleep to settle.
                nonce = secrets.token_hex(6)
                started = time.monotonic()
                await ws.send(pack_json(20, {'probe':nonce}))
                op, payload = unpack(await asyncio.wait_for(ws.recv(),8))
                event = unpack_json(payload)
                assert op == 19 and nonce in event['title']
                found = None
                async with asyncio.timeout(35):
                    while not found:
                        raw = await asyncio.to_thread(device.run,'shell','dumpsys','notification','--noredact')
                        found = helper.fixture_notification(raw,event['title'])
                        if not found:
                            await asyncio.sleep(.5)
                report['checks'].append({'phase':phase, 'event_id':event['event_id'],
                                         'latency_ms':round((time.monotonic()-started)*1000), **found})
        report['passed'] = True
    except Exception as error:
        report['errors'].append(helper.ui.safe_error(error) or type(error).__name__)
    finally:
        if doze:
            device.run('shell','dumpsys','deviceidle','unforce',check=False)
        device.run('shell','input','keyevent','224',check=False)
        # Only wake the screen; do not bypass fingerprint/password lock.
        (args.output/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'passed':report['passed'],'checks':report['checks'],'errors':report['errors']},ensure_ascii=True))
    return 0 if report['passed'] else 1


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--credentials',type=Path,required=True)
    p.add_argument('--adb',type=Path,default=ROOT/'.omx/tools/android-sdk/platform-tools/adb.exe')
    p.add_argument('--serial')
    p.add_argument('--doze',action='store_true')
    p.add_argument('--output',type=Path,default=ROOT/'.omx/artifacts/terminal-notifications/live-device')
    return asyncio.run(run(p.parse_args()))


if __name__=='__main__':
    raise SystemExit(main())
