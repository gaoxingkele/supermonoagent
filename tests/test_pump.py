import unittest

from supermonoagent.pump import OutputPump


class Loop:
    def __init__(self):
        self.pending = []

    def call_soon_threadsafe(self, fn, *args):
        fn(*args)

    def call_later(self, _delay, fn, *args):
        self.pending.append((fn, args))


class PumpTest(unittest.TestCase):
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
