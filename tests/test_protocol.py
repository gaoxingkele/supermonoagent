import unittest

from supermonoagent.protocol import OP_OUTPUT, OP_SNAPSHOT, ProtocolError, pack, pack_feed, unpack, unpack_json, pack_json


class ProtocolTest(unittest.TestCase):
    def test_snapshot_frame_recrop_keeps_sgr_but_live_output_stays_raw(self):
        from unittest.mock import patch
        data = b"\x1b[0;38;2;12;34;56m" + b"A" * 100
        with patch("supermonoagent.protocol.MAX_FRAME", 64):
            _, payload = unpack(pack_feed(OP_SNAPSHOT, "session1", data))
            self.assertTrue(payload.partition(b"\0")[2].startswith(b"\x1b[0;38;2;12;34;56m"))
            _, live = unpack(pack_feed(OP_OUTPUT, "session1", data))
            self.assertEqual(live.partition(b"\0")[2], b"A" * 54)

    def test_small_and_input_frames_remain_exact_and_legacy_snapshot_can_opt_in(self):
        from unittest.mock import patch
        from supermonoagent.protocol import fit_payload, OP_INPUT_SESSION
        body = b"\x1b[31mZ\x1b[0m"
        for op in (OP_SNAPSHOT, OP_OUTPUT, OP_INPUT_SESSION):
            self.assertEqual(unpack(pack_feed(op, "session1", body))[1], b"session1\0" + body)
        data = b"\x1b[32m" + b"X" * 100
        with patch("supermonoagent.protocol.MAX_FRAME", 64):
            self.assertEqual(fit_payload(data), b"X" * 63)
            self.assertTrue(fit_payload(data, snapshot=True).startswith(b"\x1b[0;32m"))
            self.assertLessEqual(len(fit_payload(data, snapshot=True)), 63)

    def test_roundtrip(self):
        frame = pack(OP_OUTPUT, "目录".encode())
        op, payload = unpack(frame)
        self.assertEqual(op, OP_OUTPUT)
        self.assertEqual(payload.decode(), "目录")

    def test_json(self):
        frame = pack_json(1, {"shell": "powershell"})
        op, payload = unpack(frame)
        self.assertEqual(op, 1)
        self.assertEqual(unpack_json(payload)["shell"], "powershell")

    def test_rejects_short_frame(self):
        with self.assertRaises(ProtocolError):
            unpack(b"abc")


if __name__ == "__main__":
    unittest.main()
