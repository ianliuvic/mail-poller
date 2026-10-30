#!/usr/bin/env python3
"""WhatsApp follow-ups driven by the Evolution API.

Pulls recent messages, works out who is still waiting for a reply, and feeds the
same daily digest / dashboard as the email tracker.

Config:
  TRACKING_DB                 shared sqlite (default /data/tracking.db)
  EVOLUTION_BASE_URL          e.g. http://evo-xxxx.sslip.io
  EVOLUTION_API_KEY           global api key
  EVOLUTION_INSTANCE          instance name, default "WA: +86 17711014152"
  WA_SYNC_DAYS                how far back to read, default 7
  WA_SYNC_INTERVAL_SECONDS    background sync cadence, default 900
"""

import datetime
import json
import os
import sqlite3
import urllib.parse
import urllib.request

from followups import INTERVALS, MAX_ATTEMPTS

DB_PATH = os.environ.get("TRACKING_DB", "/data/tracking.db")
BASE_URL = os.environ.get("EVOLUTION_BASE_URL", "").rstrip("/")
API_KEY = os.environ.get("EVOLUTION_API_KEY", "")
INSTANCE = os.environ.get("EVOLUTION_INSTANCE", "WA: +86 17711014152")
SYNC_DAYS = int(os.environ.get("WA_SYNC_DAYS", "7"))

STATUS_ACTIVE = "active"
STATUS_REPLIED = "replied"
STATUS_PAUSED = "paused"
STATUS_GIVEN_UP = "given_up"


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _from_ts(ts):
    return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc)


def _parse(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value)
    except Exception:
        return None


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS wa_messages (
                id        TEXT PRIMARY KEY,
                jid       TEXT NOT NULL,
                from_me   INTEGER NOT NULL,
                ts        INTEGER NOT NULL,
                mtype     TEXT,
                text      TEXT,
                push_name TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_wa_msg_jid ON wa_messages(jid, ts);
            CREATE TABLE IF NOT EXISTS wa_contacts (
                jid              TEXT PRIMARY KEY,
                name             TEXT,
                last_inbound_at  TEXT,
                last_outbound_at TEXT,
                last_message_at  TEXT,
                last_text        TEXT,
                out_since        INTEGER DEFAULT 0,
                updated_at       TEXT
            );
            CREATE TABLE IF NOT EXISTS wa_overrides (
                jid          TEXT PRIMARY KEY,
                status       TEXT,
                snooze_until TEXT,
                updated_at   TEXT
            );
            """
        )


# ---------------------------------------------------------------- evolution


def configured() -> bool:
    return bool(BASE_URL and API_KEY)


def _post(path, body):
    req = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(body).encode(),
        headers={"apikey": API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def _message_text(msg):
    m = msg.get("message") or {}
    if isinstance(m.get("conversation"), str):
        return m["conversation"]
    for key in ("extendedTextMessage", "imageMessage", "videoMessage", "documentMessage"):
        block = m.get(key)
        if isinstance(block, dict) and block.get("text"):
            return block["text"]
        if isinstance(block, dict) and block.get("caption"):
            return block["caption"]
    return ""


def fetch_messages(days: int = None):
    """Return newest-first message records covering the last `days` days."""
    days = days or SYNC_DAYS
    cutoff = int(_now().timestamp()) - days * 86400
    out = []
    page = 1
    while page <= 60:
        data = _post(
            "/chat/findMessages/" + urllib.parse.quote(INSTANCE),
            {"limit": 50, "page": page},
        )
        records = (data.get("messages") or {}).get("records") or []
        if not records:
            break
        oldest = None
        for rec in records:
            ts = int(rec.get("messageTimestamp") or 0)
            if not ts:
                continue
            oldest = ts if oldest is None else min(oldest, ts)
            if ts >= cutoff:
                out.append(rec)
        if oldest is not None and oldest < cutoff:
            break
        page += 1
    return out


def _skip_jid(jid: str) -> bool:
    return (
        not jid
        or jid.endswith("@g.us")
        or jid.endswith("@broadcast")
        or jid.startswith("status@")
        or "newsletter" in jid
    )


def _store(records):
    touched = set()
    with _connect() as conn:
        for rec in records:
            key = rec.get("key") or {}
            jid = key.get("remoteJid") or ""
            if _skip_jid(jid):
                continue
            mid = key.get("id") or rec.get("id")
            ts = int(rec.get("messageTimestamp") or 0)
            if not mid or not ts:
                continue
            conn.execute(
                """INSERT INTO wa_messages (id, jid, from_me, ts, mtype, text, push_name)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO NOTHING""",
                (
                    mid,
                    jid,
                    1 if key.get("fromMe") else 0,
                    ts,
                    rec.get("messageType") or "",
                    (_message_text(rec) or "")[:2000],
                    rec.get("pushName") or "",
                ),
            )
            touched.add(jid)
    return touched


def _recompute(jid):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT from_me, ts, text, push_name FROM wa_messages WHERE jid = ? ORDER BY ts ASC",
            (jid,),
        ).fetchall()
        if not rows:
            return
        last_in = None
        last_out = None
        last_text = ""
        name = ""
        for r in rows:
            if r["from_me"]:
                last_out = r["ts"]
            else:
                last_in = r["ts"]
                name = name or (r["push_name"] or "")
            if r["text"]:
                last_text = r["text"]
        out_since = 0
        for r in reversed(rows):
            if r["from_me"]:
                out_since += 1
            elif last_in is not None and r["ts"] == last_in:
                break
        conn.execute(
            """INSERT INTO wa_contacts
                 (jid, name, last_inbound_at, last_outbound_at, last_message_at,
                  last_text, out_since, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(jid) DO UPDATE SET
                 name = COALESCE(NULLIF(excluded.name,''), wa_contacts.name),
                 last_inbound_at = excluded.last_inbound_at,
                 last_outbound_at = excluded.last_outbound_at,
                 last_message_at = excluded.last_message_at,
                 last_text = excluded.last_text,
                 out_since = excluded.out_since,
                 updated_at = excluded.updated_at""",
            (
                jid,
                name,
                _iso(_from_ts(last_in)) if last_in else None,
                _iso(_from_ts(last_out)) if last_out else None,
                _iso(_from_ts(max(last_in or 0, last_out or 0))),
                last_text[:300],
                out_since,
                _iso(_now()),
            ),
        )


def sync(days: int = None) -> dict:
    if not configured():
        return {"ok": False, "reason": "evolution not configured"}
    records = fetch_messages(days)
    touched = _store(records)
    for jid in touched:
        _recompute(jid)
    return {"ok": True, "messages": len(records), "chats": len(touched)}


# ---------------------------------------------------------------- read side


def _decorate(row, override):
    item = dict(row)
    last_in = _parse(item.get("last_inbound_at"))
    last_out = _parse(item.get("last_outbound_at"))
    now = _now()

    status = STATUS_ACTIVE
    if last_in and last_out and last_in >= last_out:
        status = STATUS_REPLIED
    elif not last_out:
        status = STATUS_REPLIED

    due = None
    if status == STATUS_ACTIVE:
        stage = max(1, min(int(item.get("out_since") or 1), MAX_ATTEMPTS))
        due = last_out + datetime.timedelta(days=INTERVALS.get(stage, 7))

    if override:
        ov_status = (override.get("status") or "").strip()
        if ov_status == STATUS_PAUSED:
            status = STATUS_PAUSED
        elif ov_status == STATUS_GIVEN_UP:
            status = STATUS_GIVEN_UP
        elif ov_status == STATUS_REPLIED and status != STATUS_REPLIED:
            status = STATUS_REPLIED
        snooze = _parse(override.get("snooze_until"))
        if snooze and status == STATUS_ACTIVE and (not due or snooze > due):
            due = snooze

    item["status"] = status
    item["due_at"] = _iso(due) if due else None
    item["overdue_days"] = max(0, (now - due).days) if due and status == STATUS_ACTIVE else 0
    item["days_since_out"] = (now - last_out).days if last_out else None
    item["replied_at"] = _iso(last_in) if last_in else None
    item["display"] = item.get("name") or _pretty_jid(item.get("jid"))
    return item


def _pretty_jid(jid: str) -> str:
    if not jid:
        return ""
    number = jid.split("@")[0]
    return "+" + number if number.isdigit() else number


def list_items():
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM wa_contacts ORDER BY COALESCE(last_message_at,'') DESC"
        ).fetchall()
        overrides = {
            r["jid"]: dict(r) for r in conn.execute("SELECT * FROM wa_overrides").fetchall()
        }
    items = [_decorate(r, overrides.get(r["jid"])) for r in rows]
    items.sort(
        key=lambda i: (
            0 if i["status"] == STATUS_ACTIVE else 1,
            0 if i["overdue_days"] else 1,
            -(int(_parse(i["last_message_at"]).timestamp()) if i.get("last_message_at") else 0),
        )
    )
    return items


def due_items(now=None):
    now = now or _now()
    out = []
    for item in list_items():
        if item["status"] != STATUS_ACTIVE or not item.get("due_at"):
            continue
        due = _parse(item["due_at"])
        if due and due <= now:
            out.append(item)
    return out


def act(jid: str, action: str) -> bool:
    jid = (jid or "").strip()
    if not jid:
        return False
    now = _now()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM wa_overrides WHERE jid = ?", (jid,)).fetchone()
        current = dict(row) if row else {"status": "", "snooze_until": None}

        if action == "snooze":
            current["status"] = ""
            current["snooze_until"] = _iso(now + datetime.timedelta(days=3))
        elif action == "pause":
            current["status"] = STATUS_PAUSED
        elif action == "resume":
            current["status"] = ""
            current["snooze_until"] = _iso(now + datetime.timedelta(days=2))
        elif action == "replied":
            current["status"] = STATUS_REPLIED
        elif action == "give_up":
            current["status"] = STATUS_GIVEN_UP
        else:
            return False

        conn.execute(
            """INSERT INTO wa_overrides (jid, status, snooze_until, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(jid) DO UPDATE SET
                 status = excluded.status,
                 snooze_until = excluded.snooze_until,
                 updated_at = excluded.updated_at""",
            (jid, current["status"], current["snooze_until"], _iso(now)),
        )
    return True


def build_digest(items):
    shown = items[:8]
    lines = [f"💬 WhatsApp 待跟进（{len(items)}）"]
    for it in shown:
        bits = [it.get("display") or _pretty_jid(it.get("jid"))]
        bits.append(f"第{it.get('out_since') or 1}封")
        if it.get("days_since_out") is not None:
            bits.append(f"已{it['days_since_out']}天")
        if it.get("overdue_days"):
            bits.append(f"逾期{it['overdue_days']}天")
        if it.get("last_text"):
            bits.append(it["last_text"][:26].replace("\n", " "))
        lines.append("· " + " · ".join(bits))
    if len(items) > len(shown):
        lines.append(f"…还有 {len(items) - len(shown)} 个")
    return "\n".join(lines)
