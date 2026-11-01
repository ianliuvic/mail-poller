"""Cross-process idempotency for email notifications.

A Coolify deploy can briefly run the old and the new container at the same time.
Both poll the same mailbox, see the same new UID (the state file is written at
the end of a poll) and would each send the same Feishu card. This ledger is a
single SQLite file on the shared /data volume: the first process to claim a
message sends it, the other one skips.
"""

import os
import sqlite3
import threading
import time

DB_PATH = os.environ.get("NOTIFY_LEDGER_PATH", "/data/notify_ledger.db")

_lock = threading.Lock()


def claim(key):
    """Return True when this process is the first to claim `key`."""
    if not key:
        return True
    try:
        with _lock:
            conn = sqlite3.connect(DB_PATH, timeout=30)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS sent (key TEXT PRIMARY KEY, at TEXT NOT NULL)"
                )
                cur = conn.execute(
                    "INSERT OR IGNORE INTO sent (key, at) VALUES (?, ?)",
                    (key, time.strftime("%Y-%m-%d %H:%M:%S")),
                )
                conn.commit()
                return cur.rowcount == 1
            finally:
                conn.close()
    except Exception:
        # Fail open: a duplicate card is better than a silently dropped inquiry.
        return True
