"""Operational log snapshots are bounded, owner-only and generation-safe."""
import asyncio
import json
import threading
import time
from unittest.mock import Mock, patch

import pytest

from supermonoagent.protocol import OP_DAEMON_LOGS, ProtocolError, unpack, unpack_json
from test_client_integration import isolated_host


def test_log_rpc_reads_only_bound_log_file_and_never_mutates_terminals():
    asyncio.run(check_read())


async def check_read():
    async with isolated_host() as (host, _direct, _relay):
        directory=host.log_state_dir/'logs';directory.mkdir()
        (directory/'daemon.log').write_text('old\n运行日志 <script>fixture</script>\n',encoding='utf-8')
        (host.log_state_dir/'token.txt').write_text('private-file-fixture')
        client=host.hub.attach(asyncio.Queue(),token=host.token)
        frames,close=await host._op(client,OP_DAEMON_LOGS,json.dumps({'request':'selected-host','lines':1,'path':'token.txt'}).encode())
        op,payload=unpack(frames[0]);body=unpack_json(payload)
        assert close is None and op==OP_DAEMON_LOGS and body['request']=='selected-host'
        assert body['text']=='运行日志 <script>fixture</script>\n'
        assert body['pid']>0 and body['uptime_seconds']>=0
        assert not host.hub.sessions and not host.hub.windows
        assert 'private-file-fixture' not in body['text']
        host.hub.detach(client.key)


def test_log_rpc_refuses_other_accounts_and_valid_delegated_owner_tokens_before_read():
    asyncio.run(check_authority())


async def check_authority():
    async with isolated_host() as (host, _direct, relay):
        token=host.accounts.register('log-viewer@example.test','fixture-password')
        account_client=host.hub.attach(asyncio.Queue(),token=token)
        host.relay_bindings.bind(relay,'local-test','delegated-account',host.token)
        delegated=host.delegated_client(relay,'local-test',host._relay_generations[relay],321,
                                       {'account':'delegated-account','expires':time.time()+30},'relay-log-fixture')
        assert delegated is not None and host.client_authorized(delegated)
        with patch('supermonoagent.daemon_logs.read_log_tail') as read:
            for client in [account_client,delegated]:
                frames,close=await host._op(client,OP_DAEMON_LOGS,b'{"request":"denied"}')
                body=unpack_json(unpack(frames[0])[1])
                assert close is None and body.get('error') and 'text' not in body
            read.assert_not_called()
        host.hub.detach(account_client.key)
        host.forget_relay_client(delegated)


def test_log_rpc_sanitizes_io_errors_and_rechecks_authorization_after_file_read():
    asyncio.run(check_read_race())


async def check_read_race():
    async with isolated_host() as (host, _direct, _relay):
        client=host.hub.attach(asyncio.Queue(),token=host.token)
        with patch('supermonoagent.daemon_logs.read_log_tail',side_effect=PermissionError('private-path-and-token')):
            frames,_=await host._op(client,OP_DAEMON_LOGS,b'{"request":"failed"}')
        body=unpack_json(unpack(frames[0])[1])
        assert body.get('error') and 'text' not in body and 'private-path-and-token' not in str(body)
        entered,release=threading.Event(),threading.Event()
        def blocked_read(*_):
            entered.set();assert release.wait(3);return 'must-not-arrive'
        with patch('supermonoagent.daemon_logs.read_log_tail',side_effect=blocked_read):
            job=asyncio.create_task(host._op(client,OP_DAEMON_LOGS,b'{"request":"stale"}'))
            try:
                assert await asyncio.to_thread(entered.wait,2)
                host.hub.detach(client.key)
            finally:
                release.set()
            assert await job==([],4001)


@pytest.mark.parametrize('body',[{'lines':True},{'lines':-1},{'lines':'80'},{'request':'x'*65}])
def test_log_rpc_rejects_invalid_bounds(body):
    async def check():
        async with isolated_host() as (host,*_):
            client=host.hub.attach(asyncio.Queue(),token=host.token)
            try:
                with pytest.raises(ProtocolError):
                    await host._op(client,OP_DAEMON_LOGS,json.dumps(body).encode())
            finally:host.hub.detach(client.key)
    asyncio.run(check())
