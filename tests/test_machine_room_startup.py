"""Two-argument startup names new rooms after the computer, preserving old rooms."""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
from websockets.asyncio.server import serve

from supermonoagent import daemon, desktop, token_config
from supermonoagent.relay import Relay


def test_new_room_uses_and_persists_machine_name(tmp_path,monkeypatch):
    monkeypatch.setattr(daemon,'STATE',tmp_path)
    monkeypatch.setattr(daemon.socket,'gethostname',lambda:'COMPUTER-FIXTURE')
    assert daemon.load_code()=='COMPUTER-FIXTURE'
    assert (tmp_path/'relay-code').read_text()=='COMPUTER-FIXTURE'
    monkeypatch.setattr(daemon.socket,'gethostname',lambda:'RENAMED-COMPUTER')
    assert daemon.load_code()=='COMPUTER-FIXTURE'


def test_saved_and_explicit_room_names_take_precedence(tmp_path,monkeypatch):
    monkeypatch.setattr(daemon,'STATE',tmp_path)
    (tmp_path/'relay-code').write_text('previous-room')
    assert daemon.load_code()=='previous-room'
    assert daemon.load_code('explicit-room')=='explicit-room'
    assert daemon.load_code()=='explicit-room'
    assert list(tmp_path.iterdir())==[tmp_path/'relay-code']


def test_blank_state_room_uses_machine_fallback(tmp_path,monkeypatch):
    monkeypatch.setattr(daemon,'STATE',tmp_path)
    monkeypatch.setattr(daemon.socket,'gethostname',lambda:'')
    (tmp_path/'relay-code').write_text(' \n')
    assert daemon.load_code()=='电脑'


@pytest.mark.parametrize('explicit',[False,True])
def test_main_keeps_saved_endpoint_room_unless_explicit(tmp_path,monkeypatch,explicit):
    monkeypatch.setattr(daemon,'STATE',tmp_path)
    monkeypatch.setattr(token_config,'read_default_token',lambda *args,**kwargs:'')
    (tmp_path/'token').write_text('saved-owner-fixture')
    (tmp_path/'relay-code').write_text('different-global-room')
    targets=[{'url':'wss://fixture.invalid/ws','code':'previous-endpoint-room','owner_secret':'owner-proof-fixture'},
             {'url':'wss://other.invalid/ws','code':'unrelated-room'}]
    (tmp_path/'relays.json').write_text(json.dumps(targets))
    captured=[]
    def construct(token):
        assert token=='saved-owner-fixture'
        result=SimpleNamespace();captured.append(result);return result
    monkeypatch.setattr(daemon,'Daemon',construct)
    monkeypatch.setattr(daemon.asyncio,'run',lambda coroutine:coroutine.close())
    args=['supermono','--relay','https://fixture.invalid/']
    if explicit:args+=['--code','explicit-room']
    monkeypatch.setattr(sys,'argv',args)
    daemon.main()
    expected='explicit-room' if explicit else 'previous-endpoint-room'
    assert captured[0].relay_targets[0]['code']==expected
    assert captured[0].relay_targets[0]['owner_secret']=='owner-proof-fixture'
    assert captured[0].relay_targets[1]['code']=='unrelated-room'
    assert (tmp_path/'relay-code').read_text()==('explicit-room' if explicit else 'different-global-room')
    assert (tmp_path/'token').read_text()=='saved-owner-fixture'


def test_desktop_arguments_forward_only_to_new_host(tmp_path):
    (tmp_path/'token').write_text('new-owner-fixture')
    args=desktop.argument_parser().parse_args(['--relay','wss://fixture.invalid/ws','--token','new-owner-fixture',
                                              '--code','requested-room','--state-dir',str(tmp_path)])
    with patch.object(desktop,'port_open',return_value=False),patch.object(desktop,'start_host') as start,patch.object(desktop,'health',return_value={'ok':True}),patch('supermonoagent.relay_bridge.ensure_running'):
        url=desktop.connection_url(args)
    start.assert_called_once_with('127.0.0.1',6769,str(tmp_path),relay='wss://fixture.invalid/ws',token='new-owner-fixture',code='requested-room')
    assert parse_qs(urlsplit(url).fragment)=={'token':['new-owner-fixture']}


def test_running_host_credentials_are_preserved_despite_new_startup_options(tmp_path):
    (tmp_path/'token').write_text('saved-running-owner-fixture')
    args=desktop.argument_parser().parse_args(['--relay','wss://new.invalid/ws','--token','requested-new-owner-fixture','--state-dir',str(tmp_path)])
    with patch.object(desktop,'port_open',return_value=True),patch.object(desktop,'start_host') as start,patch.object(desktop,'health',return_value={'ok':True,'default_token':'live-running-owner-fixture'}),patch('supermonoagent.relay_bridge.ensure_running'):
        url=desktop.connection_url(args)
    start.assert_not_called()
    assert parse_qs(urlsplit(url).fragment)=={'token':['live-running-owner-fixture']}
    assert (tmp_path/'token').read_text()=='saved-running-owner-fixture'


def test_desktop_command_and_remote_token_are_explicit():
    with patch.object(desktop.sys,'frozen',True,create=True),patch.object(desktop.sys,'executable','C:/fixture/SuperMonoAgent.exe'):
        argv=desktop.host_command(relay='wss://fixture.invalid/ws',token_file='C:/private/token.txt')
    assert argv==['C:/fixture/SuperMonoAgent.exe','host','--host','127.0.0.1','--port','6769',
                  '--relay','wss://fixture.invalid/ws','--token-file','C:/private/token.txt']
    args=desktop.argument_parser().parse_args(['--url','https://remote.invalid','--token','explicit-remote-fixture'])
    with patch.object(desktop,'start_host') as start,patch.object(desktop,'health') as health:
        url=desktop.connection_url(args)
    assert parse_qs(urlsplit(url).fragment)=={'token':['explicit-remote-fixture']}
    start.assert_not_called();health.assert_not_called()
    with pytest.raises(SystemExit):desktop.argument_parser().parse_args(['--url','https://remote.invalid','--relay','wss://relay.invalid/ws'])


def test_desktop_start_process_receives_relay_and_owner_options():
    process=Mock()
    with patch.object(desktop.subprocess,'Popen',return_value=process) as spawn,patch.object(desktop,'port_open',return_value=True):
        desktop.start_host(relay='ws://127.0.0.1:9900/ws',token='fixture-owner')
    argv=spawn.call_args.args[0]
    assert argv[-4:]==['--relay','ws://127.0.0.1:9900/ws','--token','fixture-owner']


def test_desktop_does_not_silently_replace_an_explicit_empty_token():
    args=desktop.argument_parser().parse_args(['--token',''])
    with patch.object(desktop,'port_open',return_value=False),patch.object(desktop,'start_host',side_effect=RuntimeError('stop before process')) as start:
        with pytest.raises(RuntimeError):desktop.connection_url(args)
    start.assert_called_once_with('127.0.0.1',6769,None,token='')


@pytest.mark.parametrize('code',['x'*129,'room\0code','room\x1bcode'])
def test_invalid_room_does_not_change_credentials_or_saved_room(tmp_path,monkeypatch,code):
    monkeypatch.setattr(daemon,'STATE',tmp_path)
    (tmp_path/'token').write_text('unchanged-owner-fixture')
    (tmp_path/'relay-code').write_text('unchanged-room-fixture')
    monkeypatch.setattr(sys,'argv',['supermono','--token','would-change-owner','--code',code])
    with pytest.raises(SystemExit) as error:daemon.main()
    assert error.value.code==2
    assert (tmp_path/'token').read_text()=='unchanged-owner-fixture'
    assert (tmp_path/'relay-code').read_text()=='unchanged-room-fixture'


def test_actual_two_argument_host_registers_machine_room(tmp_path):
    asyncio.run(check_actual_start(tmp_path))


async def check_actual_start(tmp_path):
    relay=Relay(development=True)
    async with serve(relay.handler,'127.0.0.1',0) as server:
        url=f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ws'
        with socket.socket() as free:
            free.bind(('127.0.0.1',0));port=free.getsockname()[1]
        env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')}
        env.pop('SUPERMONO_TOKEN_FILE',None)
        argv=desktop.host_command(port=port,state_dir=str(tmp_path),relay=url,token='startup-owner-fixture')
        process=subprocess.Popen(argv,cwd=tmp_path,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
                                 creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
        try:
            name=daemon.default_machine()
            async with asyncio.timeout(10):
                while name not in relay.rooms or relay.rooms[name].host is None:
                    assert process.poll() is None, 'Isolated startup host exited'
                    await asyncio.sleep(.03)
            assert (tmp_path/'relay-code').read_text()==name
            assert (tmp_path/'token').read_text()=='startup-owner-fixture'
            assert json.loads((tmp_path/'relays.json').read_text())[0]['code']==name
        finally:
            if process.poll() is None:process.terminate()
            process.communicate(timeout=5)
