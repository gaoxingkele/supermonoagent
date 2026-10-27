import asyncio
import base64
import json
import unittest

from supermonoagent.release_notifications import ReleaseNotifier, release_hint


def manifest(revision='0.2.50', sequence=50):
    payload = base64.b64encode(json.dumps({'schema': 1, 'channel': 'stable',
        'revision': revision, 'sequence': sequence}).encode()).decode()
    return json.dumps({'payload': payload}).encode()


class Socket:
    def __init__(self):
        self.sent = []

    async def send(self, value):
        self.sent.append(value)


class Room:
    service = 'terminal'

    def __init__(self, phone):
        self.phones = {1: phone}


class ReleaseNotificationTest(unittest.IsolatedAsyncioTestCase):
    async def test_current_release_is_deduplicated_and_sent_to_new_phones(self):
        phone = Socket()
        notifier = ReleaseNotifier(lambda: {'room': Room(phone)}, feed='')
        self.assertTrue(await notifier.observe(manifest()))
        self.assertFalse(await notifier.observe(manifest()))
        self.assertEqual(len(phone.sent), 1)
        self.assertEqual(json.loads(phone.sent[0]),
                         {'op': 'ui_update', 'revision': '0.2.50', 'sequence': 50})
        later = Socket()
        await notifier.send_current(later)
        self.assertEqual(later.sent, phone.sent)

    async def test_polling_keeps_last_valid_release_after_failure(self):
        phone = Socket()
        calls = 0

        def fetch(_feed):
            nonlocal calls
            calls += 1
            if calls == 1:
                return release_hint(manifest())
            raise OSError('offline')

        notifier = ReleaseNotifier(lambda: {'room': Room(phone)}, feed='https://example.test/manifest', interval=.001, fetch=fetch)
        notifier.start()
        await asyncio.sleep(.02)
        await notifier.close()
        self.assertEqual(json.loads(phone.sent[0])['revision'], '0.2.50')
        self.assertEqual(len(phone.sent), 1)
        self.assertGreaterEqual(calls, 2)

    def test_malformed_public_metadata_is_rejected(self):
        for raw in [b'{}', b'not-json', b'x' * (128 * 1024 + 1),
                    manifest('../secret'), manifest('valid', -1)]:
            with self.assertRaises((ValueError, TypeError, KeyError, json.JSONDecodeError)):
                release_hint(raw)
