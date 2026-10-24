"""Daemon diagnostics report metadata, never terminal contents or credentials."""
import asyncio
import json
import logging
import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from supermonoagent import daemon, desktop, launcher
from supermonoagent.protocol import OP_OPEN, OP_CLOSE, OP_INPUT_SESSION
from test_client_integration import isolated_host


def test_log_endpoint_omits_credentials_queries_and_private_paths():
    assert daemon.log_endpoint('wss://user:private@relay.example:8443/private-token/ws?token=private#secret') == 'wss://relay.example:8443'
    assert daemon.log_endpoint('ws://[::1]:7000/ws') == 'ws://[::1]:7000'
    assert daemon.log_endpoint('wss://relay.example:invalid/ws') == '<invalid endpoint>'


@pytest.mark.parametrize('enabled', [True, False])
def test_desktop_can_override_log_window_without_changing_default_command(enabled):
    option='--log-window' if enabled else '--no-log-window'
    args=desktop.argument_parser().parse_args([option])
    assert args.log_window is enabled
    command=desktop.host_command(log_window=enabled)
    assert option in command
    assert '--log-window' not in desktop.host_command() and '--no-log-window' not in desktop.host_command()


def test_logs_mode_never_loads_or_starts_the_daemon():
    module=SimpleNamespace(main=Mock())
    with patch.object(launcher.importlib,'import_module',return_value=module) as load:
        launcher.main(['logs','--once'])
    load.assert_called_once_with('supermonoagent.daemon_logs')
    module.main.assert_called_once_with()


def test_logs_read_failure_is_a_nonzero_command_exit():
    module=SimpleNamespace(main=Mock(return_value=1))
    with patch.object(launcher.importlib,'import_module',return_value=module):
        with pytest.raises(SystemExit) as error:
            launcher.main(['logs','--once'])
    assert error.value.code==1


def test_heartbeat_and_terminal_lifecycle_never_capture_terminal_data(caplog,monkeypatch):
    monkeypatch.setattr(daemon.LOGGER,'propagate',True)
    asyncio.run(check_metadata(caplog))


async def check_metadata(caplog):
    marker='private-terminal-content-and-command-fixture'
    async with isolated_host() as (host, _direct, _relay):
        with caplog.at_level(logging.INFO, logger='supermonoagent.daemon'):
            client=host.hub.attach(asyncio.Queue(),token=host.token)
            await host._op(client,OP_OPEN,json.dumps({'shell':'cmd '+marker}).encode())
            session=next(iter(host.hub.sessions.values()))
            client.multiplex=True
            host.hub.set_subscriptions(client,[session.id])
            await host._op(client,OP_INPUT_SESSION,session.id.encode()+b'\0'+marker.encode())
            assert session.pty.written == marker.encode()
            job=asyncio.create_task(daemon.log_daemon_status(host,.01))
            try:
                async with asyncio.timeout(2):
                    while not any('heartbeat ' in record.getMessage() for record in caplog.records):
                        await asyncio.sleep(.01)
            finally:
                job.cancel()
                await asyncio.gather(job,return_exceptions=True)
            await host._op(client,OP_CLOSE,json.dumps({'session':session.id}).encode())
            host.hub.detach(client.key)
        messages='\n'.join(record.getMessage() for record in caplog.records if record.name=='supermonoagent.daemon')
        assert 'terminal_opened' in messages and 'terminal_closed' in messages and 'heartbeat' in messages
        assert marker not in messages and host.token not in messages


def test_main_file_log_has_start_stop_and_closes_handlers(tmp_path,monkeypatch,capsys):
    marker='private-log-owner-fixture'
    monkeypatch.setattr(daemon,'STATE',tmp_path)
    monkeypatch.setenv('SUPERMONO_STATE_DIR',str(tmp_path))
    monkeypatch.setattr(sys,'argv',['supermono','--state-dir',str(tmp_path),'--token',marker,'--port','0','--no-relay','--no-log-window'])
    monkeypatch.setattr(daemon.asyncio,'run',lambda coroutine:coroutine.close())
    daemon.main()
    text=(tmp_path/'logs/daemon.log').read_text(encoding='utf-8')
    assert 'daemon_start' in text and 'daemon_stopped' in text
    output=capsys.readouterr()
    assert marker not in text+output.out+output.err and '#token=' not in text
    # Windows cannot rename a log that was left open without delete sharing.
    (tmp_path/'logs/daemon.log').rename(tmp_path/'logs/closed.log')


@pytest.mark.parametrize('mode',['default','enable','disable','isolated'])
def test_ready_daemon_opens_only_the_requested_independent_viewer(tmp_path,monkeypatch,mode):
    monkeypatch.setattr(daemon,'STATE',tmp_path)
    monkeypatch.setenv('SUPERMONO_STATE_DIR',str(tmp_path))
    arguments=['supermono','--token','log-lifecycle-fixture','--port','0','--no-relay']
    if mode=='isolated':arguments+=['--state-dir',str(tmp_path)]
    if mode=='enable':arguments+=['--log-window']
    if mode=='disable':arguments+=['--no-log-window']
    monkeypatch.setattr(sys,'argv',arguments)
    original_run=asyncio.run
    async def observe(coroutine):
        task=asyncio.create_task(coroutine)
        try:
            async with asyncio.timeout(3):
                while 'daemon_listening' not in (tmp_path/'logs/daemon.log').read_text(encoding='utf-8'):
                    if task.done():await task
                    await asyncio.sleep(.01)
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
    monkeypatch.setattr(daemon.asyncio,'run',lambda coroutine:original_run(observe(coroutine)))
    with patch('supermonoagent.daemon_logs.open_log_window',return_value=True) as show:
        daemon.main()
    if mode=='enable':show.assert_called_once_with(tmp_path.resolve())
    else:show.assert_not_called()
