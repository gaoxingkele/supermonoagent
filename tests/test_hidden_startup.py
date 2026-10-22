import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

from supermonoagent import desktop, frontend

ROOT = Path(__file__).resolve().parents[1]


def test_host_creation_does_not_cancel_no_window_flag():
    process = Mock()
    with patch.object(desktop.sys, 'platform', 'win32'), patch.object(desktop.subprocess, 'Popen', return_value=process) as spawn, patch.object(desktop, 'port_open', return_value=True):
        desktop.start_host(port=9911)
    flags = spawn.call_args.kwargs['creationflags']
    assert flags & subprocess.CREATE_NO_WINDOW
    assert not flags & subprocess.DETACHED_PROCESS


def test_frontend_creation_does_not_cancel_no_window_flag():
    with patch.object(frontend.sys, 'platform', 'win32'), patch.object(frontend, 'port_open', side_effect=[False]*18+[True]), patch.object(frontend, 'revision', return_value='test'), patch.object(frontend, 'health', return_value={'role':'frontend','backend':'http://127.0.0.1:6769','revision':'test'}), patch.object(frontend.subprocess, 'Popen') as spawn:
        frontend.local_page_url('http://127.0.0.1:6769/workspace')
    flags = spawn.call_args.kwargs['creationflags']
    assert flags & subprocess.CREATE_NO_WINDOW
    assert not flags & subprocess.DETACHED_PROCESS


def test_hidden_wrapper_quotes_arguments_and_has_no_visible_console(tmp_path):
    payload=tmp_path/'probe.py'
    result=tmp_path/'result.json'
    payload.write_text('import ctypes,json,sys; from ctypes import wintypes as w; k=ctypes.windll.kernel32; k.GetConsoleWindow.restype=w.HWND; h=k.GetConsoleWindow(); u=ctypes.windll.user32; u.IsWindowVisible.argtypes=[w.HWND]; open(sys.argv[1],"w").write(json.dumps({"args":sys.argv[2:],"visible":bool(h and u.IsWindowVisible(h))}))',encoding='utf-8')
    # GUI options accept paths/URLs, not an embedded shell command string.
    args=['with spaces','trailing slash\\','中文','https://relay.example/?a=1&b=2']
    # WSH uses its own argument parsing; emulate the .cmd caller, not CRT quoting.
    command='cscript.exe //nologo '+' '.join('"'+value+'"' for value in [str(ROOT/'scripts/launch-hidden.vbs'),sys.executable,str(payload),str(result),*args])
    subprocess.run(command,check=True,capture_output=True,timeout=15)
    data=json.loads(result.read_text())
    assert data['args']==args
    assert data['visible'] is False
