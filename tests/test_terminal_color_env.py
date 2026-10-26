"""New interactive PTY capabilities; no real CLI, login or running session changes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import patch

import pytest

from supermonoagent.pty import WinPty


def spawned_environment(values):
    with patch.dict(os.environ, values, clear=True), patch('winpty.PtyProcess.spawn') as spawn:
        before = dict(os.environ)
        pty = WinPty(['owned-unused'], '.', cols=80, rows=24)
        try:
            result = dict(spawn.call_args.kwargs['env'])
            assert dict(os.environ) == before
            return result
        finally:
            pty.close()


def test_default_interactive_normalizes_inherited_headless_flags_without_forcing_colors():
    source = {'TERM': 'dumb', 'COLORTERM': '', 'NO_COLOR': '1', 'OWNED_SENTINEL': 'keep'}
    result = spawned_environment(source)
    assert result == {'TERM': 'xterm-256color', 'COLORTERM': 'truecolor', 'OWNED_SENTINEL': 'keep'}
    assert source['NO_COLOR'] == '1'


@pytest.mark.parametrize('term', [None, '', 'dumb', 'DUMB', '   '])
def test_missing_or_unusable_term_advertises_actual_interactive_surface(term):
    source = {'OWNED_SENTINEL': 'keep'}
    if term is not None: source['TERM'] = term
    result = spawned_environment(source)
    assert result['TERM'] == 'xterm-256color' and result['COLORTERM'] == 'truecolor'
    assert 'FORCE_COLOR' not in result and 'CLICOLOR_FORCE' not in result


@pytest.mark.parametrize('policy', ['preserve', 'unexpected', ' PRESERVE '])
def test_preserve_and_unknown_policy_leave_original_color_variables_exactly_unchanged(policy):
    for colors in ({}, {'TERM': 'dumb', 'COLORTERM': '', 'NO_COLOR': '1', 'FORCE_COLOR': '0'}):
        source = {**colors, 'SUPERMONO_TERMINAL_COLOR': policy, 'OWNED_SENTINEL': 'keep'}
        assert spawned_environment(source) == source


@pytest.mark.parametrize('force', ['0', '1', '3', ''])
def test_existing_explicit_force_and_other_capability_values_are_preserved(force):
    source = {'TERM': 'screen-256color', 'COLORTERM': '24bit', 'NO_COLOR': '1',
              'FORCE_COLOR': force, 'CLICOLOR_FORCE': '0', 'OWNED_SENTINEL': 'keep'}
    assert spawned_environment(source) == {k: v for k, v in source.items() if k != 'NO_COLOR'}


@pytest.mark.parametrize('policy', ['interactive', ''])
def test_explicit_interactive_or_empty_policy_uses_interactive_default(policy):
    result = spawned_environment({'SUPERMONO_TERMINAL_COLOR': policy, 'TERM': 'dumb', 'NO_COLOR': ''})
    assert result['TERM'] == 'xterm-256color' and 'NO_COLOR' not in result


NODE = r'''
const pipe=process.argv.includes('--pipe');
const result={vars:Object.fromEntries(['TERM','COLORTERM','NO_COLOR','FORCE_COLOR','CLICOLOR_FORCE'].map(k=>[k,process.env[k]??null])),
  isTTY:process.stdout.isTTY===true,depth:process.stdout.getColorDepth?.()??null,
  has16:process.stdout.hasColors?.(16)??false,sentinel:process.env.OWNED_SENTINEL==='keep'};
if(!pipe){const child=require('node:child_process').spawnSync(process.execPath,[__filename,'--pipe'],{encoding:'utf8'});if(child.status!==0)process.exit(2);result.captured=JSON.parse(child.stdout);}
if(pipe)process.stdout.write(JSON.stringify(result));
else process.stdout.write('OWNED_META:'+JSON.stringify(result)+'\r\n'+(result.has16?'\x1b[38;2;96;176;224mOWNED_COLOR\x1b[0m':'OWNED_PLAIN')+'\r\nOWNED_END\r\n');
'''

CHILD = r'''
import json,re,shutil,sys,threading,time
from supermonoagent.pty import WinPty,PtyClosed
p=WinPty([shutil.which('node'),sys.argv[1]],sys.argv[2],cols=500,rows=20)
timer=threading.Timer(8,p.close);timer.daemon=True;timer.start();raw=bytearray()
try:
 while len(raw)<65536 and b'OWNED_END' not in raw:
  try: raw.extend(p.read(4096))
  except PtyClosed: break
finally:
 timer.cancel();p.close()
deadline=time.monotonic()+1
while p.alive() and time.monotonic()<deadline: time.sleep(.01)
text=raw.decode('utf8','replace');match=re.search(r'OWNED_META:(\{[^\r\n]*\})',text)
if not match: raise RuntimeError('Owned color metadata unavailable')
result=json.loads(match[1]);result.update(color_marker='OWNED_COLOR' in text,plain_marker='OWNED_PLAIN' in text,
 foreground_sgr=bool(re.search(r'\x1b\[(?:[0-9;]*;)?(?:3[0-9]|9[0-7])(?:;[0-9;]*)?m',text)),closed=not p.alive())
print(json.dumps(result))
'''


@pytest.mark.skipif(sys.platform != 'win32' or shutil.which('node') is None, reason='Owned Windows ConPTY and installed Node required')
@pytest.mark.parametrize('policy,depth,color', [('interactive', 24, True), ('preserve', 1, False)])
def test_actual_node_tty_new_pty_and_captured_child_color_behavior(tmp_path, policy, depth, color):
    script = tmp_path / 'owned-color.cjs'
    script.write_text(NODE, encoding='utf-8')
    env = dict(os.environ)
    for key in ('FORCE_COLOR', 'CLICOLOR_FORCE'): env.pop(key, None)
    env.update(TERM='dumb', COLORTERM='', NO_COLOR='1', OWNED_SENTINEL='keep', SUPERMONO_TERMINAL_COLOR=policy)
    # Pin this isolated interpreter to the exact worktree under test.
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
    value = subprocess.run([sys.executable, '-c', CHILD, str(script), str(tmp_path)],
                           env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=True)
    result = json.loads(value.stdout)
    assert result['isTTY'] and result['depth'] == depth and result['has16'] is color
    assert result['foreground_sgr'] is color and result['color_marker'] is color
    assert result['plain_marker'] is not color and result['sentinel'] and result['closed']
    assert result['vars']['FORCE_COLOR'] is None and result['vars']['CLICOLOR_FORCE'] is None
    captured = result['captured']
    assert not captured['isTTY'] and captured['depth'] is None and not captured['has16']
    assert captured['vars']['FORCE_COLOR'] is None and captured['sentinel']
