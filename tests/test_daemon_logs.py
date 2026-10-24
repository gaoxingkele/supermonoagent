"""Log viewing never imports the daemon, locks rotation, or launches real windows."""
import io
from contextlib import nullcontext
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import Mock

import pytest

from supermonoagent import daemon_logs as D


@pytest.fixture(autouse=True)
def clean_configuration():
    D.shutdown_logging()
    D._launched.clear()
    yield
    D.shutdown_logging()
    D._launched.clear()


def test_redacts_formatted_values_exceptions_and_encoded_urls(tmp_path, monkeypatch):
    stream=io.StringIO();monkeypatch.setattr(sys,'stderr',stream)
    root=logging.getLogger();before=(list(root.handlers),list(root.filters),root.level)
    output=sys.stdout
    secret='fixture:owner&secret'
    logger=D.configure_logging(tmp_path,secrets=[secret])
    logger.info('Connected with %s and fixture%%3Aowner%%26secret',secret)
    try:raise ValueError('Failure for '+secret)
    except ValueError:logger.exception('Transport failed')
    D.shutdown_logging(logger)
    text=(tmp_path/'logs/daemon.log').read_text(encoding='utf8')
    for value in (secret,'fixture%3Aowner%26secret'):
        assert value not in text and value not in stream.getvalue()
    assert '[redacted]' in text and 'ValueError: Failure for [redacted]' in text
    assert (list(root.handlers),list(root.filters),root.level)==before
    assert sys.stdout is output


def test_configuration_is_idempotent_and_shutdown_preserves_foreign_handlers(tmp_path,monkeypatch):
    monkeypatch.setattr(sys,'stderr',io.StringIO())
    logger=logging.getLogger(D.LOGGER_NAME);foreign=logging.NullHandler();logger.addHandler(foreign)
    try:
        assert D.configure_logging(tmp_path,['first-fixture']) is logger
        assert D.configure_logging(tmp_path,['second-fixture']) is logger
        managed=[handler for handler in logger.handlers if getattr(handler,'_sma_daemon_log',False)]
        assert len(managed)==2
        file=next(handler for handler in managed if isinstance(handler,RotatingFileHandler))
        assert file.maxBytes==2*1024*1024 and file.backupCount==3
        logger.warning('first-fixture second-fixture')
        D.shutdown_logging(logger)
        assert foreign in logger.handlers
        assert not any(getattr(handler,'_sma_daemon_log',False) for handler in logger.handlers)
        assert 'fixture' not in (tmp_path/'logs/daemon.log').read_text()
        # Windows deletion is proof the owned writer handle was released.
        (tmp_path/'logs/daemon.log').unlink()
    finally:logger.removeHandler(foreign)


def test_file_rotation_is_bounded_and_child_logger_output_redacted(tmp_path,monkeypatch):
    monkeypatch.setattr(sys,'stderr',io.StringIO())
    logger=D.configure_logging(tmp_path,['private-child-fixture'])
    writer=next(handler for handler in logger.handlers if isinstance(handler,RotatingFileHandler))
    writer.maxBytes=256
    child=logging.getLogger(D.LOGGER_NAME+'.transport')
    for index in range(30):child.info('%s %s %s',index,'private-child-fixture','x'*70)
    D.shutdown_logging(logger)
    files=list((tmp_path/'logs').iterdir())
    assert len(files)==4 and {path.name for path in files}=={'daemon.log','daemon.log.1','daemon.log.2','daemon.log.3'}
    assert all('private-child-fixture' not in path.read_text() for path in files)


def test_once_tail_is_bounded_and_does_not_create_missing_files(tmp_path,capsys):
    assert D.main(['--state-dir',str(tmp_path),'--once'])==0
    assert not list(tmp_path.iterdir())
    directory=tmp_path/'logs';directory.mkdir()
    (directory/'daemon.log').write_bytes(''.join(f'ROW_{index:03d}\n' for index in range(100)).encode('utf8'))
    assert D.main(['--state-dir',str(tmp_path),'--once','--tail','3'])==0
    assert capsys.readouterr().out=='ROW_097\nROW_098\nROW_099\n'
    reader=D.LogTail(directory/'daemon.log',80,initial_bytes=40)
    text=reader.poll()
    assert text==''.join(f'ROW_{index:03d}\n' for index in range(95,100))
    with pytest.raises(SystemExit):D.main(['--tail','1001','--once'])


def test_follow_handles_partial_utf8_and_newline_boundaries(tmp_path):
    path=tmp_path/'daemon.log';path.write_bytes(b'first\r\n')
    reader=D.LogTail(path,read_bytes=1)
    assert reader.poll()=='first\n'
    raw='中文😀'.encode('utf8')+b'\r\n'
    received=''
    for byte in raw:
        with path.open('ab') as file:file.write(bytes([byte]))
        received+=reader.poll()
    assert received=='中文😀\n'
    assert reader.poll()==''


def test_follow_detects_rename_truncate_and_same_size_rewrite(tmp_path):
    path=tmp_path/'daemon.log';path.write_text('first\n',encoding='utf8')
    reader=D.LogTail(path)
    assert reader.poll()=='first\n'
    path.rename(tmp_path/'daemon.log.1');path.write_text('second\n',encoding='utf8')
    assert reader.poll()=='second\n'
    path.write_text('short\n',encoding='utf8')
    assert reader.poll()=='short\n'
    path.write_text('other\n',encoding='utf8')
    assert reader.poll()=='other\n'
    assert reader.poll()==''


def test_real_follow_does_not_prevent_rotating_writer(tmp_path,monkeypatch):
    monkeypatch.setattr(sys,'stderr',io.StringIO())
    logger=D.configure_logging(tmp_path)
    handler=next(item for item in logger.handlers if isinstance(item,RotatingFileHandler));handler.maxBytes=300
    logger.info('initial')
    stop=threading.Event();stream=io.StringIO()
    thread=threading.Thread(target=D.follow_log,args=(D.LogTail(tmp_path/'logs/daemon.log'),stream),kwargs={'stop_event':stop,'poll_interval':.005})
    thread.start()
    try:
        for index in range(12):
            logger.info('ENTRY_%02d %s',index,'x'*60)
            time.sleep(.01)
        deadline=time.monotonic()+2
        while 'ENTRY_11' not in stream.getvalue() and time.monotonic()<deadline:time.sleep(.01)
        assert 'ENTRY_11' in stream.getvalue()
        assert (tmp_path/'logs/daemon.log.1').is_file()
    finally:
        stop.set();thread.join(timeout=3)
    assert not thread.is_alive()
    D.shutdown_logging(logger)


@pytest.mark.parametrize('frozen',[True,False])
def test_window_launcher_uses_own_console_and_no_credentials(tmp_path,monkeypatch,frozen):
    monkeypatch.setattr(sys,'platform','win32')
    monkeypatch.setattr(sys,'frozen',frozen,raising=False)
    monkeypatch.setattr(sys,'executable','C:/fixture/SuperMonoAgent.exe' if frozen else 'C:/fixture/python.exe')
    monkeypatch.setattr(D,'_mutex_exists',lambda _:False)
    process=Mock();process.poll.return_value=None
    spawn=Mock(return_value=process);monkeypatch.setattr(D.subprocess,'Popen',spawn)
    assert D.open_log_window(tmp_path)
    assert D.open_log_window(tmp_path)
    spawn.assert_called_once()
    argv=spawn.call_args.args[0]
    assert argv[1:2]==['logs'] if frozen else argv[1:3]==['-m','supermonoagent.daemon_logs']
    assert argv[-4:]==['--state-dir',str(tmp_path.resolve()),'--tail','80']
    assert '--token' not in argv and '--code' not in argv
    flags=spawn.call_args.kwargs['creationflags']
    assert flags & 0x10 and not flags & 0x08000000
    assert 'shell' not in spawn.call_args.kwargs
    assert 'stdout' not in spawn.call_args.kwargs and 'stderr' not in spawn.call_args.kwargs


def test_parent_mutex_check_avoids_second_console(tmp_path,monkeypatch):
    monkeypatch.setattr(sys,'platform','win32')
    monkeypatch.setattr(D,'_mutex_exists',lambda _:True)
    spawn=Mock();monkeypatch.setattr(D.subprocess,'Popen',spawn)
    assert D.open_log_window(tmp_path)
    spawn.assert_not_called()


@pytest.mark.skipif(sys.platform!='win32',reason='Windows named mutex semantics')
def test_child_mutex_claim_has_no_stale_file_after_close(tmp_path):
    assert not D._mutex_exists(tmp_path)
    with D._viewer_instance(tmp_path) as first:
        assert first and D._mutex_exists(tmp_path)
        with D._viewer_instance(tmp_path) as second:assert not second
        assert D._mutex_exists(tmp_path)
    assert not D._mutex_exists(tmp_path)
    assert not list(tmp_path.iterdir())


def test_once_module_does_not_import_or_start_daemon(tmp_path):
    directory=tmp_path/'logs';directory.mkdir();(directory/'daemon.log').write_text('read-only-fixture\n',encoding='utf8')
    source=Path(__file__).resolve().parents[1]/'src'
    code="import runpy,sys;sys.argv=['logs','--state-dir',sys.argv[1],'--once'];\ntry:runpy.run_module('supermonoagent.daemon_logs',run_name='__main__')\nexcept SystemExit as e:assert e.code==0\nassert 'supermonoagent.daemon' not in sys.modules"
    result=subprocess.run([sys.executable,'-c',code,str(tmp_path)],env={**os.environ,'PYTHONPATH':str(source)},capture_output=True,timeout=5,
                          creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
    assert result.returncode==0,result.stderr.decode(errors='replace')
    assert result.stdout.decode().splitlines()==['read-only-fixture']


def test_once_uses_same_state_environment_as_daemon(tmp_path,monkeypatch,capsys):
    monkeypatch.setenv('SUPERMONO_STATE_DIR',str(tmp_path))
    directory=tmp_path/'logs';directory.mkdir()
    (directory/'daemon.log').write_text('环境目录中文\n',encoding='utf8')
    assert D.main(['--once'])==0
    assert capsys.readouterr().out=='环境目录中文\n'


def test_follow_missing_file_explains_wait_without_starting_a_daemon(tmp_path,monkeypatch,capsys):
    monkeypatch.setattr(D,'_viewer_instance',lambda _:nullcontext(True))
    follow=Mock(return_value=0);monkeypatch.setattr(D,'follow_log',follow)
    assert D.main(['--state-dir',str(tmp_path)])==0
    text=capsys.readouterr().out
    assert '等待守护进程日志' in text and '旧后台需在会话结束后正常重启' in text
    assert str(tmp_path/'logs/daemon.log') in text
    assert text.count('等待守护进程日志')==1
    assert not list(tmp_path.iterdir())
    follow.assert_called_once()


def test_read_snapshot_missing_and_zero_limits_are_read_only(tmp_path):
    assert D.read_log_tail(tmp_path)==''
    assert D.read_log_tail(tmp_path,limit=0)==''
    assert D.read_log_tail(tmp_path,max_bytes=0)==''
    assert not list(tmp_path.iterdir())


def test_read_snapshot_limits_lines_and_never_reads_other_files(tmp_path):
    directory=tmp_path/'logs';directory.mkdir()
    (directory/'daemon.log.1').write_text('ROTATED_OLD_PRIVATE_FIXTURE\n',encoding='utf8')
    (tmp_path/'token.txt').write_text('NOT_A_LOG_PRIVATE_FIXTURE',encoding='utf8')
    path=directory/'daemon.log'
    path.write_bytes(''.join(f'ROW_{i:03d}\r\n' for i in range(200)).encode())
    assert D.read_log_tail(tmp_path)==''.join(f'ROW_{i:03d}\n' for i in range(120,200))
    assert D.read_log_tail(tmp_path,limit=2)=='ROW_198\nROW_199\n'
    # The API holds no handle between calls, including on Windows.
    path.rename(directory/'saved-current.log')
    assert D.read_log_tail(tmp_path)==''


def test_read_snapshot_byte_ceiling_and_complete_utf8_lines(tmp_path):
    directory=tmp_path/'logs';directory.mkdir();path=directory/'daemon.log'
    lines=[f'中文😀_{i:04d}'+('x'*60)+'\n' for i in range(2000)]
    path.write_bytes(''.join(lines).encode('utf8'))
    result=D.read_log_tail(tmp_path,limit=100000,max_bytes=1000000)
    assert len(result.encode('utf8'))<=32768
    assert len(result.splitlines())<=1000
    assert result.endswith(lines[-1]) and result.startswith('中文😀_')
    budget=len(''.join(lines[-2:]).encode('utf8'))
    assert D.read_log_tail(tmp_path,max_bytes=budget)==''.join(lines[-2:])
    path.write_bytes(b'initial\nlast-'+b'\xf0\x9f')
    assert D.read_log_tail(tmp_path)=='initial\nlast-'
    with path.open('ab') as stream:stream.write(b'\x98\x80\n')
    assert D.read_log_tail(tmp_path)=='initial\nlast-😀\n'


def test_read_snapshot_damaged_utf8_cannot_expand_past_byte_limit(tmp_path):
    directory=tmp_path/'logs';directory.mkdir()
    (directory/'daemon.log').write_bytes((b'\xff\n'*40)+b'END\n')
    result=D.read_log_tail(tmp_path,max_bytes=32)
    assert len(result.encode('utf8'))<=32 and result.endswith('END\n')
    assert '\ufffd' in result


@pytest.mark.parametrize('limits',[{'limit':-1},{'max_bytes':-1},{'limit':True},{'max_bytes':1.5},{'limit':'80'}])
def test_read_snapshot_rejects_invalid_bounds(tmp_path,limits):
    with pytest.raises(ValueError,match='snapshot bounds'):D.read_log_tail(tmp_path,**limits)
    assert not list(tmp_path.iterdir())
