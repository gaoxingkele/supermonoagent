import unittest
from unittest.mock import patch
import base64
import json
from pathlib import Path
import shutil
import subprocess

from supermonoagent.ansi_state import SgrState, bounded_snapshot

from supermonoagent.pump import OutputPump


class Loop:
    def __init__(self):
        self.pending = []

    def call_soon_threadsafe(self, fn, *args):
        fn(*args)

    def call_later(self, _delay, fn, *args):
        self.pending.append((fn, args))


class PumpTest(unittest.TestCase):
    def test_bounded_history_restores_discarded_sgr_without_changing_position(self):
        pump = OutputPump()
        with patch("supermonoagent.pump.RING_BYTES", 64):
            pump.remember(b"\x1b[31m" + b"A" * 100)
            snapshot, position = pump.snapshot_position()
        self.assertTrue(snapshot.startswith(b"\x1b[0;31m"))
        self.assertLessEqual(len(snapshot), 64)
        self.assertEqual(position, 105)
        self.assertTrue(snapshot.endswith(b"A" * 20))

    def test_fragmented_sgr_ignores_osc_dcs_and_honors_resets(self):
        state = SgrState()
        for chunk in (b"\x1b[3", b"2m\x1b]0;title \x1b[31m", b"\x1b", b"\\", b"\x1bPignored \x1b[35m\x1b\\"):
            state.feed(chunk)
        self.assertEqual(state.prefix(), b"\x1b[0;32m")
        for byte in b"\x1b[1;48;5;123;38:2::12:34:56;4:3m":
            state.feed(bytes([byte]))
        prefix = state.prefix()
        for part in (b"1", b"48;5;123", b"38;2;12;34;56", b"4:3"):
            self.assertIn(part, prefix)
        state.feed(b"\x1b[22;24;39;49m")
        self.assertEqual(state.prefix(), b"\x1b[0m")
        for reset in (b"\x1b[0m", b"\x1b[m", b"\x1bc", b"\x1b[!p"):
            state.feed(b"\x1b[91;104;1m" + reset)
            self.assertEqual(state.prefix(), b"\x1b[0m")

    def test_control_sequence_state_and_utf8_boundaries_are_bounded(self):
        state = SgrState()
        state.feed(b"\x1b[" + b"1;" * 10000 + b"31m")
        self.assertEqual(state.prefix(), b"\x1b[0m")
        self.assertLessEqual(len(state.csi), state.MAX_CSI)
        state.feed(b"\x1b[32m\x1b]0;" + b"fake\x1b[31m" * 10000)
        self.assertEqual(state.prefix(), b"\x1b[0;32m")
        self.assertEqual(state.csi, b"")
        self.assertEqual(state.mode, "osc")
        restored = bounded_snapshot(b"body\x1b[0m\x07" + "中文Z".encode(), 64, state, cropped=True)
        self.assertEqual(restored, b"\x1b[0;32m" + "中文Z".encode())
        state = SgrState()
        state.feed(b"\x1b[38;2;12;")
        restored = bounded_snapshot(b"34;56m" + "中文".encode() * 20 + b"Z", 64, state, cropped=True)
        self.assertTrue(restored.startswith(b"\x1b[0;38;2;12;34;56m"))
        restored[len(b"\x1b[0;38;2;12;34;56m"):].decode("utf8")
        self.assertLessEqual(len(restored), 64)
        repeated = SgrState()
        repeated.feed(b"\x1b[1;2;3;4;5;7;8;9;11;38;2;255;255;255;48;5;255;58;5;255;51;53;60;73m" * 1000)
        self.assertLessEqual(len(repeated.attributes), 16)
        self.assertLess(len(repeated.prefix()), 256)
        self.assertEqual(repeated.csi, b"")

    def test_style_history_does_not_modify_live_batches_or_raw_offsets(self):
        loop, pump, flushed = Loop(), OutputPump(), []
        chunks = [b"\x1b[31m", b"A" * 100]
        with patch("supermonoagent.pump.RING_BYTES", 64):
            for chunk in chunks:
                pump.add(chunk, loop, lambda data, overflow: flushed.append((data, overflow)))
            snapshot, position = pump.snapshot_position()
            self.assertEqual(pump.snapshot(), snapshot)
        fn, args = loop.pending.pop()
        fn(*args)
        self.assertEqual(flushed, [(b"".join(chunks), False)])
        self.assertEqual(position, 105)
        self.assertEqual(pump.flushed_position, 105)
        pump.add(b"\x1b[0mnext", loop, lambda data, overflow: flushed.append((data, overflow)))
        fn, args = loop.pending.pop()
        fn(*args)
        self.assertEqual(flushed[-1], (b"\x1b[0mnext", False))
        self.assertEqual(pump.flushed_position, 113)

    @unittest.skipUnless(shutil.which("node"), "bundled xterm requires Node")
    def test_bundled_xterm_live_and_twice_cropped_snapshot_have_matching_style(self):
        from supermonoagent.protocol import OP_SNAPSHOT, pack_feed, unpack
        vectors = []
        styles = [b"\x1b[31m", b"\x1b[93;104;1;3;4;7m", b"\x1b[38;5;123;48;5;45m",
                  b"\x1b[38;2;12;34;56;48;2;76;54;32m", b"\x1b[31m\x1b[39m",
                  b"\x1b]0;not color \x1b[31m\x07\x1b[32m"]
        for style in styles:
            stream = style + b"A" * 200 + b"Z"
            pump = OutputPump()
            with patch("supermonoagent.pump.RING_BYTES", 128):
                for byte in stream:
                    pump.remember(bytes([byte]))
                history = pump.snapshot()
            with patch("supermonoagent.protocol.MAX_FRAME", 96):
                _, payload = unpack(pack_feed(OP_SNAPSHOT, "session1", history))
            vectors.append([base64.b64encode(value).decode() for value in (stream, payload.partition(b"\0")[2])])
        script = r"""
const {Terminal}=require('./src/supermonoagent/static/xterm.js');
const fs=require('fs');
(async()=>{for(const pair of JSON.parse(fs.readFileSync(0,'utf8'))){const results=[];
 for(const bytes of pair){const t=new Terminal({cols:32,rows:8,scrollback:100});
  await new Promise(done=>t.write(Uint8Array.from(Buffer.from(bytes,'base64')),done));
  const b=t.buffer.active;let found;
  for(let y=0;y<b.length;y++)for(let x=0;x<t.cols;x++){const c=b.getLine(y).getCell(x);if(c.getChars()==='Z')found={fg:c.getFgColor(),fgMode:c.getFgColorMode(),bg:c.getBgColor(),bgMode:c.getBgColorMode(),bold:c.isBold(),italic:c.isItalic(),underline:c.isUnderline(),inverse:c.isInverse()};}
  results.push(found);t.dispose();}
 if(!results[0]||JSON.stringify(results[0])!==JSON.stringify(results[1]))throw Error(JSON.stringify(results));
}console.log('live/replay SGR styles match');})().catch(e=>{console.error(e);process.exit(1)});
"""
        result = subprocess.run([shutil.which("node"), "-e", script], input=json.dumps(vectors),
                                cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


    def test_many_chunks_wake_once(self):
        loop = Loop()
        pump = OutputPump()
        flushed = []
        for i in range(1000):
            pump.add(f"{i},".encode(), loop, lambda data, overflow: flushed.append((data, overflow)))
        self.assertEqual(len(loop.pending), 1)
        fn, args = loop.pending[0]
        fn(*args)
        self.assertEqual(len(flushed), 1)
        self.assertFalse(flushed[0][1])
        self.assertEqual(flushed[0][0], b"".join(f"{i},".encode() for i in range(1000)))

    def test_overflow_sends_snapshot_once(self):
        loop = Loop()
        pump = OutputPump()
        flushed = []
        pump.add(b"x" * (pump_limit() + 10), loop, lambda data, overflow: flushed.append((data, overflow)))
        fn, args = loop.pending[0]
        fn(*args)
        self.assertTrue(flushed[0][1])
        self.assertLessEqual(len(flushed[0][0]), 256 * 1024)


def pump_limit():
    from supermonoagent.pump import PENDING_BYTES
    return PENDING_BYTES


if __name__ == "__main__":
    unittest.main()
