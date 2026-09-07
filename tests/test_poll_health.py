import unittest
from unittest.mock import MagicMock, patch

import main


class ImapTimeoutTests(unittest.TestCase):
    @patch("imaplib.IMAP4_SSL")
    def test_imap_connect_sets_timeout(self, imap_ssl):
        connection = MagicMock()
        imap_ssl.return_value = connection

        result = main.imaplib_connect("imap.example.com", 993, "user", "password")

        self.assertIs(result, connection)
        imap_ssl.assert_called_once_with("imap.example.com", 993, timeout=main.IMAP_TIMEOUT)
        connection.sock.settimeout.assert_called_once_with(main.IMAP_TIMEOUT)
        connection.login.assert_called_once_with("user", "password")


class PollHealthTests(unittest.TestCase):
    def setUp(self):
        self.original_mailboxes = main.MAILBOXES
        self.original_stale_seconds = main.POLL_STALE_SECONDS
        with main._poll_health_lock:
            self.original_health = dict(main._poll_health)

    def tearDown(self):
        main.MAILBOXES = self.original_mailboxes
        main.POLL_STALE_SECONDS = self.original_stale_seconds
        with main._poll_health_lock:
            main._poll_health.clear()
            main._poll_health.update(self.original_health)

    def test_configured_poller_becomes_stale(self):
        main.MAILBOXES = [{"name": "test"}]
        main.POLL_STALE_SECONDS = 100
        with main._poll_health_lock:
            main._poll_health["last_progress_at"] = 10

        self.assertTrue(main.poll_is_stale(now=111))

    def test_unconfigured_poller_is_healthy(self):
        main.MAILBOXES = []
        main.POLL_STALE_SECONDS = 100
        with main._poll_health_lock:
            main._poll_health["last_progress_at"] = 10

        self.assertFalse(main.poll_is_stale(now=111))

    def test_progress_clears_error_after_completed_cycle(self):
        main.poll_progress("inbox-error:test", "temporary failure")
        main.poll_progress("cycle-complete")

        snapshot = main.poll_health_snapshot()
        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["stage"], "cycle-complete")
        self.assertEqual(snapshot["last_error"], "")


if __name__ == "__main__":
    unittest.main()
