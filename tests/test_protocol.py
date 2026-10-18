import unittest

from supermonoagent.protocol import OP_OUTPUT, ProtocolError, pack, unpack, unpack_json, pack_json


class ProtocolTest(unittest.TestCase):
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
