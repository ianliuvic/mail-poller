#!/usr/bin/env python3
"""Open-pixel tracking for mail-poller.

Routes (mounted from main.py's Health handler):
  GET  /t/<token>.gif        public, returns a 1x1 gif and records the hit
  GET  /tracking             dashboard (requires TRACKING_PAGE_TOKEN)
  GET  /api/tracking         dashboard data (requires TRACKING_PAGE_TOKEN)

Token format: base64url of one or more recipient addresses, comma separated.
No pre-registration needed: the row is created on the first hit.

Config:
  TRACKING_DB                  sqlite path, default /data/tracking.db
  TRACKING_PAGE_TOKEN          dashboard token; if empty the dashboard is open
  TRACKING_SELF_EMAILS         comma separated, defaults to the Hongxiu mailboxes
  TRACKING_SELF_IPS            comma separated, extra "our own" IPs
  TRACKING_DEDUPE_SECONDS      collapse repeated hits from same client, default 10
"""

import base64
import datetime
import hashlib
import json
import os
import re
import sqlite3
import urllib.parse
from http.cookies import SimpleCookie

DB_PATH = os.environ.get("TRACKING_DB", "/data/tracking.db")
PAGE_TOKEN = os.environ.get("TRACKING_PAGE_TOKEN", "").strip()
DEDUPE_SECONDS = int(os.environ.get("TRACKING_DEDUPE_SECONDS", "300"))

SELF_EMAILS = {
    e.strip().lower()
    for e in os.environ.get(
        "TRACKING_SELF_EMAILS", "ian@wearhongxiu.com,service@wearhongxiu.com"
    ).split(",")
    if e.strip()
}
SELF_IPS = {
    i.strip() for i in os.environ.get("TRACKING_SELF_IPS", "").split(",") if i.strip()
}

PIXEL_GIF = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)

TZ = datetime.timezone(datetime.timedelta(hours=8))


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tracking (
                token                TEXT PRIMARY KEY,
                recipient            TEXT,
                created_at           TEXT,
                first_open_at        TEXT,
                first_human_open_at  TEXT,
                last_open_at         TEXT,
                open_count           INTEGER DEFAULT 0,
                human_open_count     INTEGER DEFAULT 0,
                proxy_open_count     INTEGER DEFAULT 0,
                self_open_count      INTEGER DEFAULT 0,
                bot_count            INTEGER DEFAULT 0,
                direct_count         INTEGER DEFAULT 0,
                last_client_type     TEXT,
                last_ip              TEXT,
                subject              TEXT,
                sender               TEXT,
                source               TEXT,
                created_via          TEXT,
                thread_key           TEXT,
                parent_token         TEXT,
                manual_read_at       TEXT
            );
            CREATE TABLE IF NOT EXISTS tracking_events (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                token          TEXT NOT NULL,
                at             TEXT NOT NULL,
                ip             TEXT,
                ua             TEXT,
                referer        TEXT,
                accept_language TEXT,
                sec_fetch_dest TEXT,
                client_type    TEXT,
                is_proxy       INTEGER DEFAULT 0,
                is_self        INTEGER DEFAULT 0,
                is_bot         INTEGER DEFAULT 0,
                is_direct      INTEGER DEFAULT 0,
                method         TEXT,
                extra          TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_events_token ON tracking_events(token, at);
            CREATE INDEX IF NOT EXISTS ix_events_at ON tracking_events(at);
            """
        )
        # 兼容老库：新增列
        existing = {r[1] for r in conn.execute("PRAGMA table_info(tracking)")}
        for col in ("subject", "sender", "source", "created_via", "thread_key", "parent_token", "manual_read_at"):
            if col not in existing:
                conn.execute(f"ALTER TABLE tracking ADD COLUMN {col} TEXT")


# ---------------------------------------------------------------- decoding


def _b64url_decode(value: str) -> str:
    pad = "=" * (-len(value) % 4)
    raw = base64.urlsafe_b64decode(value + pad)
    return raw.decode("utf-8", "replace")


def decode_token(token: str) -> str:
    """Return the recipient(s) encoded in the token, or '' when opaque.

    New tokens look like "<base64url(email)>~<unique>"; old ones are just base64.
    """
    token = (token or "").strip()
    if not token:
        return ""
    base = token.split("~", 1)[0]
    try:
        text = _b64url_decode(base)
    except Exception:
        return ""
    if "@" not in text:
        return ""
    return text


_SUBJECT_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|回复|答复|转发)\s*[:：]\s*)+", re.I)


def normalize_subject(value: str) -> str:
    text = _SUBJECT_PREFIX.sub("", value or "")
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def thread_key(recipient: str, subject: str) -> str:
    raw = (recipient or "").strip().lower() + "|" + normalize_subject(subject)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- classify


def classify(ua: str, dest: str, email: str, ip: str, referer: str = "") -> dict:
    ua_l = (ua or "").lower()
    ref_l = (referer or "").lower()
    is_proxy = 0
    client_type = "网页/客户端"
    if "googleimageproxy" in ua_l:
        client_type = "Gmail（已读信号）"
    elif "yahoomailproxy" in ua_l:
        client_type = "Yahoo（已读信号）"
    elif "outlook" in ua_l or "microsoft office" in ua_l or "office365" in ua_l:
        client_type = "Outlook（已读信号）"
    elif "applewebkit" in ua_l and ("mail/" in ua_l or "applemail" in ua_l):
        client_type = "Apple Mail（可能预取）"
        is_proxy = 1
    elif "thunderbird" in ua_l:
        client_type = "Thunderbird"

    # 本机 Zoho 网页版打开「已发送」时也会加载图片，这类命中不算对外打开
    zoho_webmail = "mail.zoho.com.cn" in ref_l or "mail.zoho.com" in ref_l
    if zoho_webmail:
        client_type = "Zoho 网页版（可能是自己查看）"

    # Zoho 手机 App 打开「已发送」同样会加载像素；它的 UA 形如
    # "Zoho%20Mail/478 CFNetwork/3860.700.2 Darwin/25.6.0"
    zoho_app = ("zoho%20mail" in ua_l) or ("zoho mail" in ua_l) or ("zohomail" in ua_l)
    if zoho_app:
        client_type = "Zoho App（可能是自己查看）"

    is_bot = any(
        k in ua_l
        for k in (
            "mimecast",
            "proofpoint",
            "barracuda",
            "sophos",
            "defender",
            "curl/",
            "wget",
            "python-requests",
            "go-http-client",
            "headlesschrome",
            "bot/",
            "spider",
        )
    )

    is_self = (email.lower() in SELF_EMAILS) or (ip in SELF_IPS) or zoho_webmail or zoho_app
    is_direct = (dest or "").lower() == "document"

    return {
        "client_type": client_type,
        "is_proxy": int(is_proxy),
        "is_self": int(is_self),
        "is_bot": int(is_bot or is_direct),
        "is_direct": int(is_direct),
    }


# ---------------------------------------------------------------- recording


def record(token: str, headers: dict, method: str, ip: str, query: dict) -> None:
    """Store one pixel hit, with light de-duplication."""
    email = decode_token(token)
    ua = headers.get("user-agent", "")
    dest = headers.get("sec-fetch-dest", "")
    info = classify(ua, dest, email, ip, headers.get("referer", ""))
    if query.get("test"):
        info["is_self"] = 1

    at = _now()
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()

    with _connect() as conn:
        cur = conn.execute(
            """SELECT at, ua, ip, is_self, is_bot, is_proxy, client_type FROM tracking_events
               WHERE token = ? ORDER BY id DESC LIMIT 1""",
            (token,),
        )
        last = cur.fetchone()
        if last:
            try:
                last_ts = datetime.datetime.fromisoformat(last[0]).timestamp()
            except Exception:
                last_ts = 0
            # 只在「同一种命中」时才折叠：自己查看、代理预取、真人打开要分别记录，
            # 否则先看过已发送（self）会把紧接着的真人打开吞掉。
            same_kind = (
                last[3] == info["is_self"]
                and last[4] == info["is_bot"]
                and last[5] == info["is_proxy"]
            )
            # Gmail/Outlook 的图片代理常从不同出口 IP 连发两次，IP 不能作为唯一依据
            same_client = (last[6] or "") == info["client_type"]
            if (
                same_kind
                and now_ts - last_ts < DEDUPE_SECONDS
                and ((last[1] == ua and last[2] == ip) or same_client)
            ):
                return  # 同一客户端瞬时重复命中

        conn.execute(
            """INSERT INTO tracking_events
               (token, at, ip, ua, referer, accept_language, sec_fetch_dest,
                client_type, is_proxy, is_self, is_bot, is_direct, method, extra)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                token,
                at,
                ip,
                ua,
                headers.get("referer", ""),
                headers.get("accept-language", ""),
                dest,
                info["client_type"],
                info["is_proxy"],
                info["is_self"],
                info["is_bot"],
                info["is_direct"],
                method,
                json.dumps(
                    {
                        "accept": headers.get("accept", ""),
                        "host": headers.get("host", ""),
                        "query": query,
                    },
                    ensure_ascii=False,
                ),
            ),
        )

        counted = not info["is_bot"]
        human = not info["is_proxy"] and not info["is_self"] and not info["is_bot"]

        conn.execute(
            """INSERT INTO tracking (token, recipient, created_at, thread_key)
               VALUES (?,?,?,?)
               ON CONFLICT(token) DO NOTHING""",
            (token, email, at, thread_key(email, "")),
        )
        conn.execute(
            """UPDATE tracking SET
                 open_count       = open_count + ?,
                 proxy_open_count = proxy_open_count + ?,
                 self_open_count  = self_open_count + ?,
                 bot_count        = bot_count + ?,
                 direct_count     = direct_count + ?,
                 human_open_count = human_open_count + ?,
                 first_open_at    = COALESCE(first_open_at, ?),
                 first_human_open_at = COALESCE(first_human_open_at, ?),
                 last_open_at     = COALESCE(?, last_open_at),
                 last_client_type = ?,
                 last_ip          = ?
               WHERE token = ?""",
            (
                1 if counted else 0,
                info["is_proxy"] if counted else 0,
                info["is_self"] if counted else 0,
                info["is_bot"],
                info["is_direct"],
                1 if human else 0,
                at if counted else None,
                at if human else None,
                at if counted else None,
                info["client_type"],
                ip,
                token,
            ),
        )


# ---------------------------------------------------------------- read side


def snapshot(limit: int = 500) -> dict:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM tracking
               ORDER BY COALESCE(created_at, '') DESC, COALESCE(last_open_at, '') DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()

        result = []
        for row in rows:
            events = conn.execute(
                """SELECT at, ip, ua, referer, client_type, is_proxy, is_self,
                          is_bot, is_direct, method, sec_fetch_dest
                   FROM tracking_events WHERE token = ?
                   ORDER BY id DESC LIMIT 40""",
                (row["token"],),
            ).fetchall()
            item = dict(row)
            if not item.get("thread_key"):
                item["thread_key"] = thread_key(
                    item.get("recipient") or "", item.get("subject") or ""
                )
            item["events"] = [dict(e) for e in events]
            result.append(item)

        today = datetime.datetime.now(TZ).date().isoformat()
        summary = {
            "tracked": len(rows),
            "opened": sum(1 for r in rows if r["first_open_at"] or r["manual_read_at"]),
            "human_opened": sum(1 for r in rows if r["first_human_open_at"] or r["manual_read_at"]),
            "manual": sum(1 for r in rows if r["manual_read_at"]),
            "unopened": sum(1 for r in rows if not r["first_open_at"] and not r["manual_read_at"]),
            "contacts": len({(r["recipient"] or "").lower() for r in rows if r["recipient"]}),
            "today_human": sum(
                1
                for r in rows
                if (r["first_human_open_at"] or "")[:10]
                == datetime.datetime.now(datetime.timezone.utc).date().isoformat()
            ),
            "events_today": conn.execute(
                "SELECT COUNT(*) FROM tracking_events WHERE substr(at,1,10)=?",
                (datetime.datetime.now(datetime.timezone.utc).date().isoformat(),),
            ).fetchone()[0],
        }
        del today

        # 按客户聚合
        contacts = {}
        for r in rows:
            key = (r["recipient"] or r["token"]).lower()
            c = contacts.get(key)
            if not c:
                c = contacts[key] = {
                    "recipient": r["recipient"] or r["token"],
                    "emails": 0,
                    "opened": 0,
                    "human_opens": 0,
                    "first_human_open_at": None,
                    "last_open_at": None,
                    "last_subject": "",
                    "tokens": [],
                }
            c["emails"] += 1
            if r["first_open_at"] or r["manual_read_at"]:
                c["opened"] += 1
            c["human_opens"] += r["human_open_count"] or 0
            if r["manual_read_at"]:
                c["human_opens"] += 1
                if not c["first_human_open_at"] or r["manual_read_at"] < c["first_human_open_at"]:
                    c["first_human_open_at"] = r["manual_read_at"]
            if r["first_human_open_at"]:
                if not c["first_human_open_at"] or r["first_human_open_at"] < c["first_human_open_at"]:
                    c["first_human_open_at"] = r["first_human_open_at"]
            if r["last_open_at"] and (not c["last_open_at"] or r["last_open_at"] > c["last_open_at"]):
                c["last_open_at"] = r["last_open_at"]
                c["last_subject"] = r["subject"] or ""
            if not c["last_subject"] and r["subject"]:
                c["last_subject"] = r["subject"]
            c["tokens"].append(r["token"])

        contact_rows = sorted(
            contacts.values(),
            key=lambda c: (c["last_open_at"] or "", c["emails"]),
            reverse=True,
        )

        return {
            "summary": summary,
            "rows": result,
            "contacts": contact_rows,
            "generated_at": _now(),
        }


def register(
    token: str,
    recipient: str,
    subject: str,
    sender: str,
    source: str,
    parent: str = "",
) -> bool:
    """Create or refresh a pixel record before any open happens.

    Used by the browser extension so the dashboard can list unopened pixels too.
    """
    token = (token or "").strip()
    if not token or len(token) > 512:
        return False
    email = (recipient or "").strip() or decode_token(token)
    if "@" not in email:
        return False
    at = _now()
    with _connect() as conn:
        thread = ""
        parent = (parent or "").strip()
        if parent:
            row = conn.execute(
                "SELECT thread_key FROM tracking WHERE token = ?", (parent,)
            ).fetchone()
            if row and row[0]:
                thread = row[0]
        if not thread:
            thread = thread_key(email, subject)

        conn.execute(
            """INSERT INTO tracking
                 (token, recipient, created_at, subject, sender, source, created_via,
                  thread_key, parent_token)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(token) DO UPDATE SET
                 recipient    = COALESCE(NULLIF(excluded.recipient,''), tracking.recipient),
                 subject      = COALESCE(NULLIF(excluded.subject,''),   tracking.subject),
                 sender       = COALESCE(NULLIF(excluded.sender,''),    tracking.sender),
                 source       = COALESCE(NULLIF(excluded.source,''),    tracking.source),
                 thread_key   = COALESCE(NULLIF(excluded.thread_key,''), tracking.thread_key),
                 parent_token = COALESCE(NULLIF(excluded.parent_token,''), tracking.parent_token)""",
            (
                token,
                email,
                at,
                subject or "",
                sender or "",
                source or "",
                "extension",
                thread,
                parent or "",
            ),
        )
    return True


def mark_read(token: str, clear: bool = False) -> bool:
    """Manually mark an email as read (e.g. the buyer confirmed via WhatsApp)."""
    token = (token or "").strip()
    if not token:
        return False
    with _connect() as conn:
        row = conn.execute("SELECT token FROM tracking WHERE token = ?", (token,)).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE tracking SET manual_read_at = ? WHERE token = ?",
            (None if clear else _now(), token),
        )
    return True


def purge(target: str) -> int:
    if not target:
        return 0
    with _connect() as conn:
        rows = conn.execute(
            "SELECT token FROM tracking WHERE token = ? OR lower(recipient) = lower(?)",
            (target, target),
        ).fetchall()
        tokens = [r[0] for r in rows]
        for t in tokens:
            conn.execute("DELETE FROM tracking_events WHERE token = ?", (t,))
            conn.execute("DELETE FROM tracking WHERE token = ?", (t,))
        return len(tokens)


# ---------------------------------------------------------------- dashboard


_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_dashboard() -> str:
    try:
        with open(os.path.join(_HERE, "tracking_dashboard.html"), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return "<h1>dashboard file missing</h1>"


DASHBOARD_HTML = _load_dashboard()

LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>邮件打开追踪</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font:14px -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",Arial,sans-serif;
display:flex;min-height:100vh;align-items:center;justify-content:center;background:#f6f7f9;margin:0}
form{background:#fff;border:1px solid #e6e8eb;border-radius:12px;padding:24px;width:320px;
box-shadow:0 6px 24px rgba(0,0,0,.06)}h1{font-size:16px;margin:0 0 14px}
input{width:100%;padding:9px 11px;border:1px solid #e6e8eb;border-radius:8px;font:inherit;margin-bottom:12px}
button{width:100%;padding:10px;border:0;border-radius:8px;background:#1f2328;color:#fff;
font:inherit;font-weight:600;cursor:pointer}</style></head>
<body><form method="get" action="/tracking">
<h1>📮 邮件打开追踪</h1>
<input type="password" name="token" placeholder="访问口令" autofocus>
<button type="submit">进入</button>
</form></body></html>
"""


def _cookie_token(headers) -> str:
    raw = headers.get("cookie", "")
    if not raw:
        return ""
    try:
        jar = SimpleCookie()
        jar.load(raw)
        morsel = jar.get("tracking_auth")
        return morsel.value if morsel else ""
    except Exception:
        return ""


def authorized(headers, query: dict) -> bool:
    if not PAGE_TOKEN:
        return True
    supplied = (query.get("token") or [""])[0] if isinstance(query.get("token"), list) else query.get("token", "")
    return supplied == PAGE_TOKEN or _cookie_token(headers) == PAGE_TOKEN


def _send(handler, status: int, body: bytes, content_type: str, extra_headers=None):
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    for k, v in (extra_headers or []):
        handler.send_header(k, v)
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass


def _client_ip(handler) -> str:
    # 有 Cloudflare 时取它给出的真实客户端地址
    cf = handler.headers.get("cf-connecting-ip", "")
    if cf:
        return cf.strip()
    fwd = handler.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return handler.client_address[0] if handler.client_address else ""


def handle(handler, method: str) -> bool:
    """Entry point from main.py. Returns True when the route was handled."""
    parsed = urllib.parse.urlparse(handler.path)
    path = parsed.path
    query = urllib.parse.parse_qs(parsed.query)

    if path.startswith("/t/"):
        token = path[3:]
        if token.endswith(".gif"):
            token = token[:-4]
        if not token:
            _send(handler, 404, b"not found", "text/plain")
            return True
        if method == "GET":
            record(token.strip("/"), handler.headers, method, _client_ip(handler), query)
        _send(
            handler,
            200,
            PIXEL_GIF,
            "image/gif",
            [
                ("Cache-Control", "no-cache, no-store, must-revalidate, private"),
                ("Pragma", "no-cache"),
                ("Expires", "0"),
            ],
        )
        return True

    if path.rstrip("/") in ("/tracking", "/tracking/index.html"):
        if not authorized(handler.headers, query):
            _send(handler, 401, LOGIN_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return True
        headers = []
        supplied = query.get("token")
        if supplied:
            headers.append(
                ("Set-Cookie", f"tracking_auth={PAGE_TOKEN}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
            )
        _send(
            handler,
            200,
            DASHBOARD_HTML.encode("utf-8"),
            "text/html; charset=utf-8",
            headers,
        )
        return True

    if path.rstrip("/").startswith("/api/tracking"):
        if path.rstrip("/").endswith("/register"):
            origin = handler.headers.get("origin", "")
            cors = []
            if origin in ("https://mail.zoho.com.cn", "https://mail.zoho.com"):
                cors = [
                    ("Access-Control-Allow-Origin", origin),
                    ("Access-Control-Allow-Headers", "content-type, x-tracking-key"),
                    ("Access-Control-Allow-Methods", "POST, OPTIONS"),
                    ("Vary", "Origin"),
                ]
            if method == "OPTIONS":
                _send(handler, 204, b"", "text/plain", cors)
                return True
            expected = os.environ.get("TRACKING_REGISTER_KEY", "") or PAGE_TOKEN
            supplied = handler.headers.get("x-tracking-key", "") or (query.get("key") or [""])[0]
            if expected and supplied != expected:
                _send(handler, 401, b'{"error":"unauthorized"}', "application/json", cors)
                return True
            try:
                length = int(handler.headers.get("content-length", 0) or 0)
                payload = json.loads(handler.rfile.read(length) or b"{}")
            except Exception:
                payload = {}
            ok = register(
                payload.get("token", ""),
                payload.get("recipient", ""),
                payload.get("subject", ""),
                payload.get("sender", ""),
                payload.get("source", ""),
                payload.get("parent", ""),
            )
            _send(
                handler,
                200 if ok else 400,
                json.dumps({"ok": ok}, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
                cors,
            )
            return True
        if not authorized(handler.headers, query):
            _send(handler, 401, b'{"error":"unauthorized"}', "application/json")
            return True
        if path.rstrip("/").endswith("/mark"):
            try:
                length = int(handler.headers.get("content-length", 0) or 0)
                payload = json.loads(handler.rfile.read(length) or b"{}")
            except Exception:
                payload = {}
            ok = mark_read(payload.get("token", ""), bool(payload.get("clear")))
            _send(
                handler,
                200 if ok else 404,
                json.dumps({"ok": ok}, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )
            return True
        if path.rstrip("/").endswith("/purge"):
            target = (query.get("target") or [""])[0]
            removed = purge(target)
            _send(
                handler,
                200,
                json.dumps({"removed": removed, "target": target}, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )
            return True
        data = json.dumps(snapshot(), ensure_ascii=False).encode("utf-8")
        _send(handler, 200, data, "application/json; charset=utf-8")
        return True

    return False
