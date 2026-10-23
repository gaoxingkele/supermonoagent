"""Footer recovery is opt-in, read-only, generation-bound and append-only."""
import asyncio
import copy
import json
from pathlib import Path
import subprocess
from unittest.mock import Mock, patch

import pytest

from supermonoagent import console_snapshot as cs
from supermonoagent.protocol import MAX_FRAME, OP_OUTPUT, OP_SNAPSHOT, pack, unpack
from supermonoagent.task_events import TaskStateDetector


def snapshot(cols=100, rows=20):
    text = [' ' * cols for _ in range(rows)]
    text[-2] = 'context 52% left | weekly 81% left'.ljust(cols)
    return {'pid': 101, 'parent': 11, 'created': 123456789,
            'width': cols, 'height': rows, 'cursor': [4, 5], 'rows': text,
            'attributes': [[7] * cols for _ in range(rows)],
            'palette': [[i * 15, i * 15, i * 15] for i in range(16)]}


def configured(tmp_path, **overrides):
    config = {'backend': 'http://127.0.0.1:6769', 'daemon_pid': 11,
              'bindings': {'sid': {'pid': 101, 'created': 123456789}}}
    config.update(overrides)
    (tmp_path / 'console-recovery.json').write_text(json.dumps(config), encoding='utf-8')
    return cs.FooterRecovery(tmp_path, config['backend'])


def catalog():
    return {'windows': [{'sessions': [{'id': 'sid'}]}],
            'controls': {'sid': {'cols': 100, 'rows': 20}}}


def test_patch_contains_only_verified_footer_and_preserves_snapshot():
    data = snapshot()
    before = copy.deepcopy(data)
    result = cs.footer_patch(data, 100, 20)
    assert result.startswith(b'\x1b7\x1b[?6l\x1b[19;1H')
    assert result.endswith(b'\x1b[0m\x1b8')
    assert b'context 52%' in result and b'weekly 81%' in result
    assert b'\x1b[2J' not in result and b'\x1b[3J' not in result
    assert data == before
    assert not cs.footer_patch(data, 99, 20)
    assert not cs.footer_patch(data, 100, 19)


@pytest.mark.parametrize('line', ['context 52%', 'weekly 81%', 'context weekly',
                                 'context 52% weekly 81%\x1b',
                                 'context 52% weekly 81%\u202e',
                                 'context 52% weekly 81%\u0301',
                                 'context 52% weekly 81%\ud800',
                                 'context 52% weekly 81%\u754c'])
def test_nonfooter_and_hostile_glyphs_fail_closed(line):
    data = snapshot()
    data['rows'][-2] = line.ljust(100)
    assert cs.footer_patch(data, 100, 20) == b''


def test_last_row_and_wide_cell_attributes_never_repaint():
    data = snapshot()
    data['rows'][-1], data['rows'][-2] = data['rows'][-2], data['rows'][-1]
    assert not cs.footer_patch(data, 100, 20)
    data = snapshot()
    data['attributes'][-2][3] |= 0x100
    assert not cs.footer_patch(data, 100, 20)
    data = snapshot()
    data['palette'][0] = [256, 0, 0]
    assert not cs.footer_patch(data, 100, 20)


def test_codex_marker_requires_verified_process_and_genuine_footer_and_is_passive():
    data = snapshot()
    marker = b'\x1b]7501;app=codex\x07'
    for agent in (None, '', 'claude', 'Codex', 'codex.exe'):
        data['agent'] = agent
        assert marker not in cs.footer_patch(data, 100, 20)
    data['agent'] = 'codex'
    recovered = cs.footer_patch(data, 100, 20)
    assert recovered.startswith(marker)
    assert b'state=' not in recovered
    detector = TaskStateDetector()
    assert detector.feed(recovered) == 0
    assert detector.app == '' and detector.running is False
    detector.app, detector.running = 'previous', True
    assert detector.feed(recovered) == 0
    assert detector.app == 'previous' and detector.running is True
    data['rows'][-2] = 'not a genuine status footer'.ljust(100)
    assert not cs.footer_patch(data, 100, 20)


def test_attached_console_process_image_match_is_exact_and_query_only():
    kernel = Mock()
    def processes(buffer, count):
        buffer[0], buffer[1] = 101, 102
        return 2
    kernel.GetConsoleProcessList.side_effect = processes
    kernel.OpenProcess.side_effect = lambda access, inherit, pid: pid
    images = {101: r'C:\Tools\not-codex.exe', 102: r'C:\Tools\codex.exe'}
    def query(handle, flags, buffer, size):
        buffer.value = images[handle]
        return True
    kernel.QueryFullProcessImageNameW.side_effect = query
    assert cs._console_agent(kernel) == 'codex'
    assert all(call.args[0] == 0x1000 for call in kernel.OpenProcess.call_args_list)
    assert kernel.CloseHandle.call_count == 2
    images[102] = r'C:\codex.exe\powershell.exe'
    assert cs._console_agent(kernel) == ''
    kernel.GetConsoleProcessList.side_effect = lambda *_: 513
    assert cs._console_agent(kernel) == ''


def test_absent_invalid_remote_or_unbound_configuration_never_reads(tmp_path):
    with patch.object(cs, 'read_console') as read, patch.object(cs, '_process_identity') as identity:
        assert not cs.FooterRecovery(tmp_path, 'http://127.0.0.1:6769').patch('sid', 100, 20)
        assert not configured(tmp_path, backend='http://remote:6769').patch('sid', 100, 20)
        recovery = configured(tmp_path)
        assert not recovery.patch('unbound', 100, 20)
        assert not cs.FooterRecovery(tmp_path, 'http://127.0.0.1:9999').patch('sid', 100, 20)
        identity.assert_not_called()
        read.assert_not_called()


def test_parent_and_pid_generation_are_revalidated_before_cached_use(tmp_path):
    recovery = configured(tmp_path)
    with patch.object(cs, '_process_identity', return_value=(11, 123456789)), \
            patch.object(cs, 'read_console', return_value=snapshot()) as read:
        assert recovery.patch('sid', 100, 20)
        assert recovery.patch('sid', 100, 20)
        read.assert_called_once_with(101, expected_created=123456789, expected_parent=11)
    for identity in ((12, 123456789), (11, 123456790), None):
        with patch.object(cs, '_process_identity', return_value=identity), patch.object(cs, 'read_console') as read:
            assert not recovery.patch('sid', 100, 20)
            read.assert_not_called()


def test_augment_authorized_snapshot_only_and_keeps_all_original_bytes(tmp_path):
    recovery = configured(tmp_path)
    raw = b'\x1b[?1049horiginal bytes\x1b[38;2;1;2;3m'
    frame = pack(OP_SNAPSHOT, b'sid\0' + raw)
    with patch.object(recovery, 'patch', return_value=b'FOOTER') as recover:
        result = recovery.augment(frame, catalog())
        assert unpack(result) == (OP_SNAPSHOT, b'sid\0' + raw + b'FOOTER')
        recover.assert_called_once_with('sid', 100, 20)
        recover.reset_mock()
        for invalid in (pack(OP_OUTPUT, b'sid\0bytes'), pack(OP_SNAPSHOT, b'other\0bytes'),
                        pack(OP_SNAPSHOT, b'no separator'), b'garbage'):
            assert recovery.augment(invalid, catalog()) == invalid
        assert recovery.augment(frame, {'windows': [], 'controls': catalog()['controls']}) == frame
        recover.assert_not_called()
        full = pack(OP_SNAPSHOT, b'sid\0' + b'x' * (MAX_FRAME - 5))
        assert recovery.augment(full, catalog()) == full
        assert unpack(recovery.supplement(full, catalog())) == (OP_OUTPUT, b'sid\0FOOTER')
        for invalid in (pack(OP_OUTPUT, b'sid\0bytes'), pack(OP_SNAPSHOT, b'other\0bytes'), b'bad'):
            assert recovery.supplement(invalid, catalog()) is None
        assert recovery.supplement(full, {'windows': [], 'controls': catalog()['controls']}) is None


def test_child_errors_and_identity_changes_are_generic(monkeypatch):
    monkeypatch.setattr(cs.sys, 'platform', 'win32')
    monkeypatch.setattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000, raising=False)
    with patch.object(cs, '_process_identity', return_value=(11, 123456789)), \
            patch.object(cs.subprocess, 'run', side_effect=subprocess.TimeoutExpired('private', 2)):
        assert cs.read_console(101, 123456789, 11) is None
    with patch.object(cs, '_process_identity', return_value=(11, 999)), patch.object(cs.subprocess, 'run') as run:
        assert cs.read_console(101, 123456789, 11) is None
        run.assert_not_called()
    completed = subprocess.CompletedProcess([], 0, json.dumps(snapshot()).encode())
    with patch.object(cs, '_process_identity', side_effect=[(11, 123456789), (11, 999)]), \
            patch.object(cs.subprocess, 'run', return_value=completed):
        assert cs.read_console(101, 123456789, 11) is None
    completed.stdout = b'x' * (cs.MAX_BYTES + 1)
    with patch.object(cs, '_process_identity', return_value=(11, 123456789)), \
            patch.object(cs.subprocess, 'run', return_value=completed):
        assert cs.read_console(101, 123456789, 11) is None


def test_footer_patch_in_real_xterm_preserves_cursor_history_and_other_rows():
    playwright = pytest.importorskip('playwright.async_api')
    edge = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
    if not edge.exists():
        pytest.skip('Hidden Edge is required for real xterm verification')

    async def check():
        async with playwright.async_playwright() as runtime:
            browser = await runtime.chromium.launch(executable_path=str(edge), headless=True)
            try:
                page = await browser.new_page()
                await page.set_content('<div id="terminal"></div>')
                await page.add_script_tag(path=str(Path(__file__).parents[1] / 'src/supermonoagent/static/xterm.js'))
                await page.evaluate("""async () => {
                    window.term = new Terminal({cols:100,rows:20});term.open(document.querySelector('#terminal'));
                    const write = data => new Promise(done=>term.write(data,done));
                    await write('history\\r\\n'.repeat(35));
                    await write('\\x1b[3;16r\\x1b[?6h\\x1b[6;5H\\x1b[32mcurrent input');
                    window.before={x:term.buffer.active.cursorX,y:term.buffer.active.cursorY,
                      base:term.buffer.active.baseY,line:term.buffer.active.getLine(term.buffer.active.baseY+term.buffer.active.cursorY).translateToString()};
                }""")
                data = snapshot()
                data['agent'] = 'codex'
                # The exact live console palette is used, not theme guesses.
                data['palette'][7] = [204, 204, 204]
                recovery = cs.FooterRecovery(Path('missing-config'), 'http://127.0.0.1:6769')
                # A maximally sized original OP8 is forwarded byte-for-byte.
                # NUL padding is ignored by xterm and isolates framing limits
                # from the cursor/history setup above.
                frame = pack(OP_SNAPSHOT, b'sid\0' + b'\0' * (MAX_FRAME - 5))
                original = bytes(frame)
                with patch.object(recovery, 'patch', return_value=cs.footer_patch(data, 100, 20)):
                    extra = recovery.supplement(frame, catalog())
                assert frame == original
                opcode, payload = unpack(extra)
                assert opcode == OP_OUTPUT
                assert payload.startswith(b'sid\0')
                await page.evaluate("data=>new Promise(done=>term.write(new Uint8Array(data),done))",
                                    list(unpack(frame)[1][4:]))
                await page.evaluate("data=>new Promise(done=>term.write(new Uint8Array(data),done))",
                                    list(payload[4:]))
                result = await page.evaluate("""() => ({
                  x:term.buffer.active.cursorX,y:term.buffer.active.cursorY,base:term.buffer.active.baseY,
                  input:term.buffer.active.getLine(term.buffer.active.baseY+before.y).translateToString(),
                  footer:term.buffer.active.getLine(term.buffer.active.baseY+18).translateToString(false),
                  fg:term.buffer.active.getLine(term.buffer.active.baseY+18).getCell(0).getFgColor(),before})""")
                assert result['footer'] == data['rows'][-2]
                assert result['fg'] == 0xCCCCCC
                assert result['x'] == result['before']['x'] and result['y'] == result['before']['y']
                assert result['base'] == result['before']['base'] and result['input'] == result['before']['line']
                await page.evaluate("() => new Promise(done=>term.write('Z',done))")
                assert await page.evaluate("term.buffer.active.getLine(term.buffer.active.baseY+before.y).getCell(before.x).getFgColor()") == 2
            finally:
                await browser.close()
    asyncio.run(check())
