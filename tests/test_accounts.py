import tempfile
import unittest
from pathlib import Path

from supermonoagent.accounts import Accounts


class AccountTest(unittest.TestCase):
    def test_register_login_and_rotate(self):
        with tempfile.TemporaryDirectory() as folder:
            book = Accounts(Path(folder) / "accounts.json")
            token = book.register("me@example.com", "secret1")
            self.assertEqual(book.login("me@example.com", "secret1"), token)
            with self.assertRaises(ValueError):
                book.register("me", "secret1")
            with self.assertRaises(ValueError):
                book.login("me@example.com", "nope")
            fresh = book.rotate("me@example.com", "secret1")
            self.assertNotEqual(fresh, token)
            self.assertEqual(book.login("Me@Example.com", "secret1"), fresh)
            again = Accounts(Path(folder) / "accounts.json")
            self.assertEqual(again.tokens(), [fresh])


if __name__ == "__main__":
    unittest.main()
