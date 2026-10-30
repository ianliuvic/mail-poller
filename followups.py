#!/usr/bin/env python3
"""Follow-up reminders for outbound mail.

A follow-up cycle starts when we send a tracked email to someone. If they never
reply, the customer shows up in the daily digest and on the dashboard until the
user marks it handled, pauses it, gives up, or (automatically) the customer replies.

Config:
  TRACKING_DB                     shared sqlite path (default /data/tracking.db)
  FOLLOWUP_MAX_ATTEMPTS           outbound emails per cycle, default 4 (初次 + 3 次跟进)
  FOLLOWUP_SNOOZE_DAYS            "稍后提醒" push-out, default 3
  FOLLOWUP_GIVE_UP_AFTER_DAYS     after the last attempt, wait this long then close
"""

import datetime
import os
import sqlite3

DB_PATH = os.environ.get("TRACKING_DB", "/data/tracking.db")
MAX_ATTEMPTS = int(os.environ.get("FOLLOWUP_MAX_ATTEMPTS", "4"))
SNOOZE_DAYS = int(os.environ.get("FOLLOWUP_SNOOZE_DAYS", "3"))
GIVE_UP_AFTER_DAYS = int(os.environ.get("FOLLOWUP_GIVE_UP_AFTER_DAYS", "7"))

# attempts → days until the next nudge
INTERVALS = {1: 2, 2: 3, 3: 7, 4: 7}

STATUS_ACTIVE = "active"
STATUS_REPLIED = "replied"
STATUS_PAUSED = "paused"
STATUS_GIVEN_UP = "given_up"

SELF_EMAILS = {
    e.strip().lower()
    for e in os.environ.get(
        "TRACKING_SELF_EMAILS", "ian@wearhongxiu.com,service@wearhongxiu.com"
    ).split(",")
    if e.strip()
}


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt: datetime.datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(value: str):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value)
    except Exception:
        return None


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS followups (
                recipient        TEXT PRIMARY KEY,
                status           TEXT DEFAULT 'active',
                attempts         INTEGER DEFAULT 0,
                first_outbound_at TEXT,
                last_outbound_at TEXT,
                last_subject     TEXT,
                next_due_at      TEXT,
                last_reminded_at TEXT,
                replied_at       TEXT,
                updated_at       TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_followups_due ON followups(status, next_due_at);
            """
        )
        # 自己给自己发的测试邮件不该进入跟进列表
        if SELF_EMAILS:
            marks = ",".join("?" * len(SELF_EMAILS))
            conn.execute(
                f"DELETE FROM followups WHERE recipient IN ({marks})", tuple(SELF_EMAILS)
            )


# ---------------------------------------------------------------- events


def note_outbound(recipient: str, subject: str = "", when: datetime.datetime = None) -> None:
    """Record one outbound email; (re)start or advance the follow-up cycle."""
    recipient = (recipient or "").strip().lower()
    if not recipient or "@" not in recipient or recipient in SELF_EMAILS:
        return
    now = when or _now()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM followups WHERE recipient = ?", (recipient,)
        ).fetchone()

        if row and row["status"] in (STATUS_REPLIED, STATUS_GIVEN_UP):
            # 新的一轮：对方已经回过或已放弃，重新从第 1 封算起
            attempts = 1
            first_at = _iso(now)
        elif row:
            attempts = (row["attempts"] or 0) + 1
            first_at = row["first_outbound_at"] or _iso(now)
        else:
            attempts = 1
            first_at = _iso(now)

        if attempts > MAX_ATTEMPTS:
            status = STATUS_GIVEN_UP
            due = None
        else:
            status = STATUS_ACTIVE if (not row or row["status"] != STATUS_PAUSED) else STATUS_PAUSED
            due = _iso(now + datetime.timedelta(days=INTERVALS.get(attempts, 7)))

        conn.execute(
            """INSERT INTO followups
                 (recipient, status, attempts, first_outbound_at, last_outbound_at,
                  last_subject, next_due_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(recipient) DO UPDATE SET
                 status = excluded.status,
                 attempts = excluded.attempts,
                 first_outbound_at = excluded.first_outbound_at,
                 last_outbound_at = excluded.last_outbound_at,
                 last_subject = excluded.last_subject,
                 next_due_at = excluded.next_due_at,
                 updated_at = excluded.updated_at
            """,
            (
                recipient,
                status,
                attempts,
                first_at,
                _iso(now),
                (subject or "").strip()[:300],
                due,
                _iso(now),
            ),
        )


def note_reply(recipient: str, when: datetime.datetime = None) -> bool:
    """Mark the customer as replied; stops all reminders. Returns True if matched."""
    recipient = (recipient or "").strip().lower()
    if not recipient or "@" not in recipient:
        return False
    now = when or _now()
    with _connect() as conn:
        row = conn.execute(
            "SELECT status FROM followups WHERE recipient = ?", (recipient,)
        ).fetchone()
        if not row:
            return False
        if row["status"] == STATUS_REPLIED:
            return False
        conn.execute(
            """UPDATE followups
               SET status = ?, replied_at = ?, next_due_at = NULL, updated_at = ?
               WHERE recipient = ?""",
            (STATUS_REPLIED, _iso(now), _iso(now), recipient),
        )
    return True


def act(recipient: str, action: str) -> bool:
    recipient = (recipient or "").strip().lower()
    if not recipient:
        return False
    now = _now()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM followups WHERE recipient = ?", (recipient,)
        ).fetchone()
        if not row:
            return False

        if action == "snooze":
            conn.execute(
                "UPDATE followups SET next_due_at = ?, updated_at = ? WHERE recipient = ?",
                (_iso(now + datetime.timedelta(days=SNOOZE_DAYS)), _iso(now), recipient),
            )
        elif action == "pause":
            conn.execute(
                "UPDATE followups SET status = ?, updated_at = ? WHERE recipient = ?",
                (STATUS_PAUSED, _iso(now), recipient),
            )
        elif action == "resume":
            conn.execute(
                "UPDATE followups SET status = ?, next_due_at = ?, updated_at = ? WHERE recipient = ?",
                (STATUS_ACTIVE, _iso(now + datetime.timedelta(days=2)), _iso(now), recipient),
            )
        elif action == "give_up":
            conn.execute(
                "UPDATE followups SET status = ?, next_due_at = NULL, updated_at = ? WHERE recipient = ?",
                (STATUS_GIVEN_UP, _iso(now), recipient),
            )
        elif action == "replied":
            conn.execute(
                "UPDATE followups SET status = ?, replied_at = ?, next_due_at = NULL, updated_at = ? WHERE recipient = ?",
                (STATUS_REPLIED, _iso(now), _iso(now), recipient),
            )
        elif action == "delete":
            conn.execute("DELETE FROM followups WHERE recipient = ?", (recipient,))
        else:
            return False
    return True


# ---------------------------------------------------------------- reads


_TRACKING_JOIN = """
    LEFT JOIN (
        SELECT lower(recipient) AS rcpt,
               SUM(human_open_count) AS human_opens,
               SUM(proxy_open_count) AS proxy_opens,
               MAX(last_open_at)     AS last_open_at
        FROM tracking
        WHERE recipient IS NOT NULL AND recipient <> ''
        GROUP BY lower(recipient)
    ) t ON t.rcpt = f.recipient
"""


def _decorate(row: sqlite3.Row) -> dict:
    item = dict(row)
    due = _parse(item.get("next_due_at"))
    now = _now()
    item["overdue_days"] = (
        max(0, (now - due).days) if due and item.get("status") == STATUS_ACTIVE else 0
    )
    last = _parse(item.get("last_outbound_at"))
    item["days_since_last"] = (now - last).days if last else None
    item["human_opens"] = item.get("human_opens") or 0
    item["opened"] = bool(item["human_opens"])
    return item


def list_items() -> list:
    with _connect() as conn:
        try:
            rows = conn.execute(
                f"""SELECT f.*, t.human_opens, t.proxy_opens, t.last_open_at
                    FROM followups f {_TRACKING_JOIN}
                    ORDER BY CASE f.status
                               WHEN 'active' THEN 0
                               WHEN 'paused' THEN 1
                               WHEN 'replied' THEN 2
                               ELSE 3 END,
                             COALESCE(f.next_due_at, '9999') ASC,
                             f.last_outbound_at DESC"""
            ).fetchall()
        except sqlite3.Error:
            rows = conn.execute(
                "SELECT *, 0 AS human_opens, 0 AS proxy_opens, NULL AS last_open_at FROM followups"
            ).fetchall()
    return [_decorate(r) for r in rows]


def due_items(now: datetime.datetime = None) -> list:
    now = now or _now()
    with _connect() as conn:
        try:
            rows = conn.execute(
                f"""SELECT f.*, t.human_opens, t.proxy_opens, t.last_open_at
                    FROM followups f {_TRACKING_JOIN}
                    WHERE f.status = ? AND f.next_due_at IS NOT NULL AND f.next_due_at <= ?
                    ORDER BY (COALESCE(t.human_opens, 0) > 0) DESC, f.next_due_at ASC""",
                (STATUS_ACTIVE, _iso(now)),
            ).fetchall()
        except sqlite3.Error:
            rows = conn.execute(
                """SELECT *, 0 AS human_opens, 0 AS proxy_opens, NULL AS last_open_at
                   FROM followups
                   WHERE status = ? AND next_due_at IS NOT NULL AND next_due_at <= ?
                   ORDER BY next_due_at ASC""",
                (STATUS_ACTIVE, _iso(now)),
            ).fetchall()
    return [_decorate(r) for r in rows]


def expire_stale(now: datetime.datetime = None) -> int:
    """Close cycles that got their last attempt long ago and still no reply."""
    now = now or _now()
    cutoff = _iso(now - datetime.timedelta(days=GIVE_UP_AFTER_DAYS))
    with _connect() as conn:
        cur = conn.execute(
            """UPDATE followups SET status = ?, next_due_at = NULL, updated_at = ?
               WHERE status = ? AND attempts >= ? AND last_outbound_at <= ?""",
            (STATUS_GIVEN_UP, _iso(now), STATUS_ACTIVE, MAX_ATTEMPTS, cutoff),
        )
        return cur.rowcount or 0


def backfill_from_tracking() -> int:
    """Seed follow-up cycles from emails already tracked (one-time)."""
    with _connect() as conn:
        if conn.execute("SELECT COUNT(*) FROM followups").fetchone()[0]:
            return 0
        try:
            rows = conn.execute(
                """SELECT lower(recipient) AS rcpt,
                          COUNT(*) AS n,
                          MAX(created_at) AS last_at,
                          MAX(subject) AS any_subject
                   FROM tracking
                   WHERE recipient IS NOT NULL AND recipient <> ''
                   GROUP BY lower(recipient)"""
            ).fetchall()
        except sqlite3.Error:
            return 0

        added = 0
        for row in rows:
            if (row["rcpt"] or "") in SELF_EMAILS:
                continue
            attempts = min(int(row["n"] or 1), MAX_ATTEMPTS)
            last_at = _parse(row["last_at"]) or _now()
            due = _iso(last_at + datetime.timedelta(days=INTERVALS.get(attempts, 7)))
            conn.execute(
                """INSERT INTO followups
                     (recipient, status, attempts, first_outbound_at, last_outbound_at,
                      last_subject, next_due_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    row["rcpt"],
                    STATUS_ACTIVE,
                    attempts,
                    _iso(last_at),
                    _iso(last_at),
                    (row["any_subject"] or "")[:300],
                    due,
                    _iso(_now()),
                ),
            )
            added += 1
        return added


def reconcile_with_tracking() -> int:
    """Catch outbound emails that the live hook missed (restart/race).

    Any tracked email newer than the customer's last_outbound_at counts as a new
    touch. Runs at startup and before each digest, so reminders self-heal.
    """
    with _connect() as conn:
        try:
            rows = conn.execute(
                """SELECT lower(t.recipient) AS rcpt, t.subject, t.created_at
                   FROM tracking t
                   LEFT JOIN followups f ON f.recipient = lower(t.recipient)
                   WHERE t.recipient IS NOT NULL AND t.recipient <> ''
                     AND t.created_at IS NOT NULL
                     AND (f.recipient IS NULL OR t.created_at > f.last_outbound_at)
                   ORDER BY t.created_at ASC"""
            ).fetchall()
        except sqlite3.Error:
            return 0

    added = 0
    for row in rows:
        when = _parse(row["created_at"]) or _now()
        try:
            note_outbound(row["rcpt"], row["subject"], when)
            added += 1
        except Exception:
            continue
    return added


# ---------------------------------------------------------------- digest


def build_digest(items: list) -> str:
    shown = items[:10]
    hot = sum(1 for it in items if it.get("opened"))
    lines = [f"📬 今天该跟进（{len(items)}，其中 {hot} 个读过邮件）"]
    for it in shown:
        stage = it.get("attempts") or 1
        overdue = it.get("overdue_days") or 0
        since = it.get("days_since_last")
        bits = []
        bits.append("🔥" if it.get("opened") else "·")
        bits.append(it["recipient"])
        bits.append(f"第{stage}封")
        if since is not None:
            bits.append(f"已{since}天")
        if overdue > 0:
            bits.append(f"逾期{overdue}天")
        if it.get("opened"):
            bits.append(f"已读{it['human_opens']}次")
        subj = (it.get("last_subject") or "").strip()
        if subj:
            bits.append(subj[:24])
        lines.append(" ".join(bits))
    if len(items) > len(shown):
        lines.append(f"…还有 {len(items) - len(shown)} 个")
    lines.append("🔥 = 读过但没回，优先跟进；其余建议换渠道")
    lines.append("打开面板：https://mail.wearhongxiu.com/tracking")
    return "\n".join(lines)
