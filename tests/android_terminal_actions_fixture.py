"""Loopback-only Android lifecycle fixture. Each PTY owns a fresh, profile-free PowerShell child."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from urllib.parse import urlsplit

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from supermonoagent.daemon import Daemon, relay_uplink
from supermonoagent.protocol import OP_CLOSE, OP_GROUP, unpack_json
from supermonoagent.pty import WinPty
from supermonoagent.relay import Relay

HOST = '127.0.0.1'
PORT = 18835
ROOM = 'qa-actions'
TOKEN = 'qa-actions-terminal-token'


async def run(state):
    import _winapi
    daemon=Daemon(TOKEN)
    daemon.hub.persist=False
    daemon.hub.attach_loop(asyncio.get_running_loop())
    daemon.hub.set_identity('QA Actions host','Synthetic terminal actions')
    powershell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
    daemon.hub.pty_factory=lambda *_: WinPty([str(powershell),'-NoLogo','-NoProfile','-NonInteractive','-Command','Start-Sleep -Seconds 3600'],str(state))
    source=daemon.hub.open_window('QA Actions Target',owner=TOKEN)
    target=daemon.hub.open_cli(source.id,'powershell')
    other=daemon.hub.open_window('QA Actions Neighbor',owner=TOKEN)
    neighbor=daemon.hub.open_cli(other.id,'powershell')
    processes=[s.pty._proc.pid for s in (target,neighbor)]
    handles=[_winapi.OpenProcess(_winapi.SYNCHRONIZE,False,pid) for pid in processes]
    counts={'rename_requests':0,'close_requests':0,'raw_input_frames':0,'resize_frames':0}
    original=daemon._op
    async def observe(client,op,payload):
        if op==OP_CLOSE: counts['close_requests']+=1
        if op==OP_GROUP and unpack_json(payload).get('action')=='title':counts['rename_requests']+=1
        if op in (4,17):counts['raw_input_frames']+=1
        if op==5:counts['resize_frames']+=1
        return await original(client,op,payload)
    daemon._op=observe
    relay=Relay(development=True,allowed_origins=[f'http://{HOST}:{PORT}'])
    async def http(connection,request):
        if urlsplit(request.path).path=='/fixture/metrics':
            value=dict(counts,target_sid=target.id,neighbor_sid=neighbor.id,target_pid=processes[0],neighbor_pid=processes[1],
                       target_alive=_winapi.WaitForSingleObject(handles[0],0)==_winapi.WAIT_TIMEOUT,
                       neighbor_alive=_winapi.WaitForSingleObject(handles[1],0)==_winapi.WAIT_TIMEOUT,
                       target_in_catalog=target.id in daemon.hub.sessions,neighbor_in_catalog=neighbor.id in daemon.hub.sessions,
                       target_window_title=source.title)
            body=json.dumps(value).encode()
            return Response(200,'OK',Headers({'Content-Type':'application/json','Content-Length':str(len(body))}),body)
        return await relay.serve_page(connection,request)
    connection=None
    try:
        async with serve(relay.handler,HOST,PORT,process_request=http):
            connection=asyncio.create_task(relay_uplink(daemon,f'ws://{HOST}:{PORT}/ws',ROOM,'qa-actions-host'))
            async with asyncio.timeout(10):
                while not (relay.rooms.get(ROOM) and relay.rooms[ROOM].host):
                    if connection.done():await connection
                    await asyncio.sleep(.05)
            print(f'READY {HOST}:{PORT} room={ROOM} stop={state/"stop-request"}',flush=True)
            while not (state/'stop-request').exists():
                if connection.done():await connection
                await asyncio.sleep(.15)
    finally:
        if connection:
            connection.cancel()
            await asyncio.gather(connection,return_exceptions=True)
        for wid in list(daemon.hub.windows):daemon.hub.close_window(wid)
        for handle in handles:_winapi.CloseHandle(handle)
        await daemon.close_sidecars()


if __name__=='__main__':
    with tempfile.TemporaryDirectory(prefix='sma-android-actions-') as tmp:
        state=Path(tmp)
        with patch('supermonoagent.daemon.STATE',state),patch.dict(os.environ,{'SUPERMONO_STATE_DIR':tmp}):
            asyncio.run(run(state))
