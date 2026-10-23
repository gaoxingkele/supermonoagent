"""Private defaults are read locally and delivered only to the verified owner."""
import asyncio
import json
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed, InvalidStatus

from supermonoagent import token_config as T
from supermonoagent.frontend import Frontend
from supermonoagent.protocol import pack_json, unpack
from test_client_integration import barrier, isolated_host, receive_op


@pytest.fixture
def private_paths(tmp_path, monkeypatch):
    cwd=tmp_path/'cwd';cwd.mkdir()
    state=tmp_path/'state';state.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.delenv('SUPERMONO_TOKEN_FILE', raising=False)
    monkeypatch.setattr(T.Path,'home',lambda:tmp_path/'home')
    monkeypatch.setattr(sys,'frozen',False,raising=False)
    return cwd,state


def test_file_selection_priority_and_each_read_observes_changes(private_paths, monkeypatch):
    cwd,state=private_paths
    assert T.read_default_token(state_dir=state)==''
    (state/'token.txt').write_text('state-fixture')
    assert T.read_default_token(state_dir=state)=='state-fixture'
    (cwd/'token.txt').write_text('\ufeffcwd-fixture\r\n',encoding='utf8')
    assert T.read_default_token(state_dir=state)=='cwd-fixture'
    pointer=state/'selected.txt';pointer.write_text('pointer-fixture')
    (state/'token-source.json').write_text(json.dumps({'path':'selected.txt'}))
    assert T.read_default_token(state_dir=state)=='pointer-fixture'
    env=cwd/'env.txt';env.write_text('environment-fixture')
    monkeypatch.setenv('SUPERMONO_TOKEN_FILE',str(env))
    assert T.read_default_token(state_dir=state)=='environment-fixture'
    explicit=cwd/'explicit.txt';explicit.write_text('explicit-fixture')
    assert T.read_default_token(explicit,state_dir=state)=='explicit-fixture'
    explicit.write_text('updated-fixture')
    assert T.read_default_token(explicit,state_dir=state)=='updated-fixture'
    assert T.read_default_token(cwd/'missing-explicit.txt',state_dir=state)==''
    monkeypatch.setenv('SUPERMONO_TOKEN_FILE',str(cwd/'missing-env.txt'))
    assert T.read_default_token(state_dir=state)==''


def test_portable_locations_do_not_require_directory_search(private_paths, monkeypatch):
    cwd,state=private_paths
    parent=cwd/'release';exedir=parent/'SuperMonoAgent';exedir.mkdir(parents=True)
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setattr(sys,'executable',str(exedir/'SuperMonoAgent.exe'))
    (state/'token.txt').write_text('state-fixture')
    (parent/'token.txt').write_text('launcher-fixture')
    assert T.read_default_token(state_dir=state)=='launcher-fixture'
    (exedir/'token.txt').write_text('exe-fixture')
    assert T.read_default_token(state_dir=state)=='exe-fixture'
    (cwd/'token.txt').write_text('cwd-fixture')
    assert T.read_default_token(state_dir=state)=='cwd-fixture'


@pytest.mark.parametrize('data',[b'',b'first second',b'first\nsecond',b'tab\tvalue',b'bad\x1bvalue',b'bad\0value',b'bad\x7fvalue',b'\xff',b'x'*4097,b'x'*20000])
def test_invalid_files_fail_without_disclosing_contents(private_paths,data):
    cwd,state=private_paths;source=cwd/'token.txt';source.write_bytes(data)
    before={p.name:p.read_bytes() for p in cwd.iterdir()}
    with pytest.raises(T.TokenConfigurationError) as error:T.read_default_token(source,state_dir=state)
    assert str(error.value)=='Default Token file must contain one token of 1 to 4096 characters'
    assert {p.name:p.read_bytes() for p in cwd.iterdir()}==before
    assert not list(state.iterdir())


def test_unicode_limit_and_bad_pointer_are_bounded(private_paths):
    cwd,state=private_paths;source=cwd/'token.txt';source.write_text('界'*4096,encoding='utf8')
    assert len(T.read_default_token(source))==4096
    (state/'token-source.json').write_bytes(b'{'*16385)
    with pytest.raises(T.TokenConfigurationError,match='configuration is invalid'):
        T.read_default_token(state_dir=state)


@pytest.mark.parametrize('path',['bad\0path','bad\npath','x'*4097])
def test_invalid_path_is_a_safe_configuration_error(private_paths,path,monkeypatch):
    cwd,state=private_paths
    with pytest.raises(T.TokenConfigurationError,match='file path is invalid'):
        T.read_default_token(path,state_dir=state)
    monkeypatch.setenv('SUPERMONO_TOKEN_FILE',path.replace('\0','\x01'))
    with pytest.raises(T.TokenConfigurationError,match='file path is invalid'):
        T.read_default_token(state_dir=state)


def test_frontend_decorator_is_owner_only_and_never_mutates_state(private_paths):
    cwd,state=private_paths;source=cwd/'token.txt';source.write_text('private-relay-fixture')
    owner=state/'token';owner.write_text('local-owner-fixture')
    app=Frontend('http://127.0.0.1:1',state/'themes.json',default_token_file=source,owner_token_file=owner)
    original={'capabilities':{'window_themes':True},'service':{'relays':[{'url':'wss://example.invalid/ws'}]}}
    serialized=json.dumps(original)
    assert app.default_token_state(original,'other-account') is original
    allowed=app.default_token_state(original,'local-owner-fixture')
    assert allowed['service']['default_relay_token']=='private-relay-fixture'
    assert allowed['service']['default_relay_token_source']=='token.txt'
    assert json.dumps(original)==serialized
    source.write_text('private-invalid-fixture has spaces')
    rejected=app.default_token_state(original,'local-owner-fixture')
    assert rejected['service']['default_relay_token']==''
    assert 'private-invalid-fixture' not in json.dumps(rejected)
    assert rejected['service']['default_relay_token_error']
    source.unlink()
    assert app.default_token_state(original,'local-owner-fixture')['service']['default_relay_token']==''
    source.write_text('private-relay-fixture')
    denied={**original,'service':{'owner_authenticated':False}}
    assert app.default_token_state(denied,'local-owner-fixture',owner_authenticated=True) is denied
    owner.unlink()
    allowed={**original,'service':{'owner_authenticated':True}}
    assert app.default_token_state(allowed,'local-owner-fixture')['service']['default_relay_token']=='private-relay-fixture'


def test_authenticated_wire_does_not_leak_to_other_accounts_or_http(tmp_path, monkeypatch):
    monkeypatch.delenv('SUPERMONO_TOKEN_FILE',raising=False)
    asyncio.run(check_wire(tmp_path))


async def check_wire(tmp_path):
    secret='private-relay-default-fixture'
    async with isolated_host() as (daemon,_direct,_relay):
        source=tmp_path/'token.txt';source.write_text(secret)
        owner=tmp_path/'owner-token';owner.write_text(daemon.token)
        other=daemon.accounts.register('other@fixture.invalid','fixture-password')
        async with serve(daemon.handle_socket,'127.0.0.1',0,process_request=daemon.serve_http) as backend:
            app=Frontend(f'http://127.0.0.1:{backend.sockets[0].getsockname()[1]}',tmp_path/'themes.json',default_token_file=source,owner_token_file=owner)
            async with serve(app.handler,'127.0.0.1',0,process_request=app.serve_http) as server:
                origin=f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
                url=origin.replace('http:','ws:')+'/ws'
                try:
                    for path in ('/workspace','/health','/token.txt'):
                        def get():
                            try:
                                with urlopen(origin+path,timeout=2) as response:return response.read()
                            except HTTPError as error:return error.read()
                        assert secret.encode() not in await asyncio.to_thread(get)
                    async with connect(url,origin=origin) as valid:
                        await valid.send(pack_json(1,{'token':daemon.token,'multiplex':True}))
                        state=json.loads(await receive_op(valid,2))
                        assert state['service']['default_relay_token']==secret
                        assert all(secret not in json.dumps(value) for value in app.views.values())
                        source.write_text('rotated-private-fixture')
                        await valid.send(pack_json(16,{'sessions':[]}))
                        changed=json.loads(await receive_op(valid,2))
                        assert changed['service']['default_relay_token']=='rotated-private-fixture'
                        source.write_text('invalid token contents')
                        daemon.hub._catalog()
                        bad=json.loads(await receive_op(valid,7))
                        assert bad['service']['default_relay_token']==''
                        assert 'invalid token contents' not in json.dumps(bad)
                        await barrier(valid)
                    async with connect(url,origin=origin) as unrelated:
                        await unrelated.send(pack_json(1,{'token':other,'multiplex':True}))
                        state=json.loads(await receive_op(unrelated,2))
                        assert 'default_relay_token' not in state.get('service',{})
                        assert secret not in json.dumps(state)
                    async with connect(url,origin=origin) as invalid:
                        await invalid.send(pack_json(1,{'token':'not-an-owner','multiplex':True}))
                        with pytest.raises(ConnectionClosed):await invalid.recv()
                    with pytest.raises(InvalidStatus):
                        async with connect(url,origin='https://untrusted.invalid'):pass
                finally:
                    await app.close_updates()


@pytest.mark.parametrize('legacy',[True,False])
def test_live_owner_proof_overrides_stale_owner_file(tmp_path,monkeypatch,legacy):
    monkeypatch.delenv('SUPERMONO_TOKEN_FILE',raising=False)
    asyncio.run(check_stale_file(tmp_path,legacy))


async def check_stale_file(tmp_path,legacy):
    async with isolated_host() as (daemon,_direct,_relay):
        secret='default-relay-private-fixture'
        source=tmp_path/'token.txt';source.write_text(secret)
        other=daemon.accounts.register('stale-file@fixture.invalid','fixture-password')
        owner=tmp_path/'owner-token';owner.write_text(other)
        original_state=daemon._state
        def state(client,*args,**kwargs):
            frame=original_state(client,*args,**kwargs)
            if not legacy:return frame
            op,payload=unpack(frame);body=json.loads(payload)
            body['service'].pop('owner_authenticated')
            return pack_json(op,body)
        daemon._state=state
        health_calls=[]
        async def http(connection,request):
            if request.path=='/health':
                health_calls.append(True)
                # Modern state flags must make this endpoint irrelevant.
                body={'ok':True,'default_token':daemon.token} if legacy else {'ok':False}
                return connection.respond(200,json.dumps(body))
            return await daemon.serve_http(connection,request)
        async with serve(daemon.handle_socket,'127.0.0.1',0,process_request=http) as backend:
            app=Frontend(f'http://127.0.0.1:{backend.sockets[0].getsockname()[1]}',tmp_path/'themes.json',
                         default_token_file=source,owner_token_file=owner)
            async with serve(app.handler,'127.0.0.1',0,process_request=app.serve_http) as server:
                origin=f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
                url=origin.replace('http:','ws:')+'/ws'
                try:
                    async with connect(url,origin=origin) as actual_owner:
                        await actual_owner.send(pack_json(1,{'token':daemon.token,'multiplex':True}))
                        state=json.loads(await receive_op(actual_owner,2))
                        assert state['service']['default_relay_token']==secret
                        assert len(health_calls)==(1 if legacy else 0)
                        source.write_text('new-default-relay-fixture')
                        await actual_owner.send(pack_json(16,{'sessions':[]}))
                        state=json.loads(await receive_op(actual_owner,2))
                        assert state['service']['default_relay_token']=='new-default-relay-fixture'
                        daemon.hub._catalog()
                        state=json.loads(await receive_op(actual_owner,7))
                        assert state['service']['default_relay_token']=='new-default-relay-fixture'
                        assert len(health_calls)==(1 if legacy else 0), 'Legacy owner proof must be per connection'
                    async with connect(url,origin=origin) as stale_file_account:
                        await stale_file_account.send(pack_json(1,{'token':other,'multiplex':True}))
                        state=json.loads(await receive_op(stale_file_account,2))
                        assert 'default_relay_token' not in state.get('service',{})
                        assert secret not in json.dumps(state)
                        assert len(health_calls)==(2 if legacy else 0)
                finally:
                    await app.close_updates()


@pytest.mark.parametrize('response',['missing','unavailable','redirect','invalid-present'])
def test_legacy_probe_has_bounded_local_fallback_and_no_redirect(tmp_path,monkeypatch,response):
    monkeypatch.delenv('SUPERMONO_TOKEN_FILE',raising=False)
    asyncio.run(check_fallback(tmp_path,response))


async def check_fallback(tmp_path,response):
    redirects=[]
    async def unrelated(connection,request):
        redirects.append(request.path)
        return connection.respond(200,'{"ok":true,"default_token":"owner-fixture"}')
    async with serve(lambda _socket:None,'127.0.0.1',0,process_request=unrelated) as other:
        async def http(connection,request):
            if response=='unavailable':return connection.respond(503,'unavailable')
            if response=='redirect':
                result=connection.respond(302,'redirect')
                result.headers['Location']=f'http://127.0.0.1:{other.sockets[0].getsockname()[1]}/private-health'
                return result
            return connection.respond(200,json.dumps({'ok':True,**({'default_token':None} if response=='invalid-present' else {})}))
        async with serve(lambda _socket:None,'127.0.0.1',0,process_request=http) as backend:
            owner=tmp_path/'owner-token';owner.write_text('owner-fixture')
            app=Frontend(f'http://127.0.0.1:{backend.sockets[0].getsockname()[1]}',tmp_path/'themes.json',owner_token_file=owner)
            # A real loopback HTTP probe must not block the server's event loop.
            result=await asyncio.wait_for(asyncio.to_thread(app._legacy_owner_authenticated,'owner-fixture'),3)
            assert result is (response!='invalid-present')
            assert not await asyncio.to_thread(app._legacy_owner_authenticated,'other-account')
            assert not redirects
