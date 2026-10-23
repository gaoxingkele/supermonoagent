"""Host defaults use a private runtime file without touching another live host."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest
from websockets.asyncio.client import connect

from supermonoagent import daemon, token_config
from supermonoagent.protocol import pack_json, unpack

ROOT=Path(__file__).resolve().parents[1]


def available_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


def command(state,cwd,arguments=(),port=None,environment=None):
    env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'SUPERMONO_STATE_DIR':str(state)}
    env.pop('SUPERMONO_TOKEN_FILE',None)
    if environment:env.update(environment)
    argv=[sys.executable,'-m','supermonoagent.launcher','host','--host','127.0.0.1',
          '--port',str(port if port is not None else available_port()),'--no-relay',*map(str,arguments)]
    return argv,{'cwd':cwd,'env':env,'creationflags':subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0}


@asynccontextmanager
async def running(state,cwd,arguments=(),environment=None):
    port=available_port()
    argv,options=command(state,cwd,arguments,port,environment)
    process=subprocess.Popen(argv,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,**options)
    try:
        async with asyncio.timeout(10):
            while True:
                if process.poll() is not None:
                    raise AssertionError('Fixture host failed to start: exit '+str(process.returncode))
                try:
                    _,writer=await asyncio.open_connection('127.0.0.1',port)
                    writer.close();await writer.wait_closed();break
                except OSError:await asyncio.sleep(.03)
        yield f'ws://127.0.0.1:{port}/ws'
    finally:
        if process.poll() is None:process.terminate()
        process.communicate(timeout=5)


async def authenticate(url,token):
    async with connect(url,additional_headers={'Authorization':'Bearer '+token}) as socket:
        await socket.send(pack_json(1,{'token':token,'multiplex':True}))
        op,payload=unpack(await asyncio.wait_for(socket.recv(),3))
        assert op==2 and json.loads(payload)['service']['owner_authenticated'] is True


def test_default_program_token_file_precedes_saved_owner(tmp_path):
    asyncio.run(check_default_file(tmp_path))


async def check_default_file(tmp_path):
    cwd=tmp_path/'program';cwd.mkdir();state=tmp_path/'state';state.mkdir()
    source=cwd/'token.txt';source.write_text('\ufeffprogram-file-fixture\r\n',encoding='utf8')
    (state/'token').write_text('previous-saved-fixture')
    original=source.read_bytes()
    async with running(state,cwd) as url:
        await authenticate(url,'program-file-fixture')
        assert (state/'token').read_text()=='program-file-fixture'
    assert source.read_bytes()==original


@pytest.mark.parametrize('saved',[False,True])
def test_absent_default_file_keeps_saved_or_generated_behavior(tmp_path,saved):
    asyncio.run(check_absent_file(tmp_path,saved))


async def check_absent_file(tmp_path,saved):
    cwd=tmp_path/'program';cwd.mkdir();state=tmp_path/'state';state.mkdir()
    if saved:(state/'token').write_text('saved-file-fixture')
    async with running(state,cwd) as url:
        token=(state/'token').read_text()
        assert token=='saved-file-fixture' if saved else bool(token)
        await authenticate(url,token)


@pytest.mark.parametrize('explicit_file',[False,True])
def test_isolated_state_ignores_global_file_unless_explicit(tmp_path,explicit_file):
    asyncio.run(check_isolated_file(tmp_path,explicit_file))


async def check_isolated_file(tmp_path,explicit_file):
    cwd=tmp_path/'program';cwd.mkdir();state=tmp_path/'state';state.mkdir()
    (state/'token').write_text('isolated-saved-fixture')
    (cwd/'token.txt').write_text('invalid global contents must never be read')
    global_file=tmp_path/'global.txt';global_file.write_text('another invalid global default')
    selected=tmp_path/'selected.txt';selected.write_text('explicit-file-fixture')
    arguments=['--state-dir',state]
    if explicit_file:arguments+=['--token-file',selected]
    async with running(state,cwd,arguments,{'SUPERMONO_TOKEN_FILE':str(global_file)}) as url:
        expected='explicit-file-fixture' if explicit_file else 'isolated-saved-fixture'
        await authenticate(url,expected)
        assert (state/'token').read_text()==expected


def test_explicit_token_takes_precedence_over_default_file(tmp_path):
    asyncio.run(check_explicit(tmp_path))


async def check_explicit(tmp_path):
    cwd=tmp_path/'program';cwd.mkdir();state=tmp_path/'state';state.mkdir()
    (cwd/'token.txt').write_text('invalid default must not be read')
    async with running(state,cwd,['--token','explicit-cli-fixture']) as url:
        await authenticate(url,'explicit-cli-fixture')
        assert (state/'token').read_text()=='explicit-cli-fixture'


@pytest.mark.parametrize('explicit_file',[False,True])
def test_occupied_port_never_replaces_owner_from_token_file(tmp_path,explicit_file):
    cwd=tmp_path/'program';cwd.mkdir();state=tmp_path/'state';state.mkdir()
    (state/'token').write_text('untouched-saved-fixture')
    source=cwd/'token.txt';source.write_text('attempted-replacement-fixture')
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1',0));occupied.listen()
        arguments=['--token-file',source] if explicit_file else []
        argv,options=command(state,cwd,arguments,occupied.getsockname()[1])
        result=subprocess.run(argv,capture_output=True,timeout=5,**options)
    assert result.returncode==2
    assert b'saved token was not changed' in result.stderr
    assert (state/'token').read_text()=='untouched-saved-fixture'


@pytest.mark.parametrize('kind',['invalid-default','invalid-explicit','missing-explicit','mutually-exclusive'])
def test_bad_file_or_conflicting_options_do_not_write_credentials(tmp_path,kind):
    cwd=tmp_path/'program';cwd.mkdir();state=tmp_path/'state';state.mkdir()
    (state/'token').write_text('untouched-saved-fixture')
    secret_marker='must-not-appear-in-errors'
    source=cwd/'token.txt'
    if kind!='missing-explicit':source.write_text(secret_marker+' invalid text')
    arguments=[]
    if kind!='invalid-default':arguments=['--token-file',source]
    if kind=='mutually-exclusive':arguments+=['--token','another-cli-fixture']
    argv,options=command(state,cwd,arguments)
    result=subprocess.run(argv,capture_output=True,timeout=5,**options)
    assert result.returncode==2
    assert secret_marker.encode() not in result.stdout+result.stderr
    assert (state/'token').read_text()=='untouched-saved-fixture'
    if kind=='missing-explicit':assert b'Token file' in result.stderr
    if kind=='mutually-exclusive':assert b'not allowed with argument' in result.stderr


def test_effective_file_is_read_once_before_probe_and_save(tmp_path,monkeypatch):
    state=tmp_path/'state';state.mkdir();(state/'token').write_text('old-fixture')
    calls=[]
    def read(path=None,*,state_dir=None):
        calls.append((path,state_dir))
        return 'one-read-fixture' if len(calls)==1 else 'different-second-read-fixture'
    class BeforeRun(Exception):pass
    def construct(token):
        assert token=='one-read-fixture'
        raise BeforeRun()
    monkeypatch.setattr(token_config,'read_default_token',read)
    monkeypatch.setattr(daemon,'STATE',state)
    monkeypatch.setattr(daemon,'Daemon',construct)
    monkeypatch.setattr(sys,'argv',['supermono','--port',str(available_port()),'--no-relay'])
    with pytest.raises(BeforeRun):daemon.main()
    assert calls==[(None,state)]
    assert (state/'token').read_text()=='one-read-fixture'


@pytest.mark.parametrize('operation',['--bind-relay-account','--unbind-relay-account'])
def test_binding_metadata_does_not_implicitly_load_configuration(tmp_path,monkeypatch,operation):
    state=tmp_path/'state';state.mkdir();(state/'token').write_text('saved-binding-owner-fixture')
    def forbidden(*args,**kwargs):raise AssertionError('Metadata operation read global Token configuration')
    monkeypatch.setattr(token_config,'read_default_token',forbidden)
    monkeypatch.setattr(daemon,'STATE',state)
    monkeypatch.setattr(sys,'argv',['supermono',operation,'fixture-account','--relay','wss://fixture.invalid/ws','--code','fixture-room'])
    daemon.main()
    assert (state/'token').read_text()=='saved-binding-owner-fixture'
