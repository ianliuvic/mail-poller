import os
import tempfile
import unittest

import notify_ledger


class NotifyLedgerTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(prefix="notify-", suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        self.original = notify_ledger.DB_PATH
        notify_ledger.DB_PATH = self.path

    def tearDown(self):
        notify_ledger.DB_PATH = self.original
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.unlink(self.path + suffix)

    def test_same_message_is_claimed_once(self):
        self.assertTrue(notify_ledger.claim("zoho::INBOX::661"))
        self.assertFalse(notify_ledger.claim("zoho::INBOX::661"))

    def test_different_messages_are_independent(self):
        self.assertTrue(notify_ledger.claim("zoho::INBOX::661"))
        self.assertTrue(notify_ledger.claim("zoho::INBOX::662"))


if __name__ == "__main__":
    unittest.main()
