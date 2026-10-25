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
import json
import os
import sqlite3
import urllib.parse
from http.cookies import SimpleCookie

DB_PATH = os.environ.get("TRACKING_DB", "/data/tracking.db")
PAGE_TOKEN = os.environ.get("TRACKING_PAGE_TOKEN", "").strip()
DEDUPE_SECONDS = int(os.environ.get("TRACKING_DEDUPE_SECONDS", "10"))

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
                last_ip              TEXT
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


# ---------------------------------------------------------------- decoding


def _b64url_decode(value: str) -> str:
    pad = "=" * (-len(value) % 4)
    raw = base64.urlsafe_b64decode(value + pad)
    return raw.decode("utf-8", "replace")


def decode_token(token: str) -> str:
    """Return the recipient(s) encoded in the token, or '' when opaque."""
    token = (token or "").strip()
    if not token:
        return ""
    try:
        text = _b64url_decode(token)
    except Exception:
        return ""
    if "@" not in text:
        return ""
    return text


# ---------------------------------------------------------------- classify


def classify(ua: str, dest: str, email: str, ip: str) -> dict:
    ua_l = (ua or "").lower()
    is_proxy = any(
        k in ua_l for k in ("googleimageproxy", "yahoomailproxy", "outlookimageproxy")
    )
    client_type = "网页/客户端"
    if "googleimageproxy" in ua_l:
        client_type = "Gmail 代理预取"
    elif "yahoomailproxy" in ua_l:
        client_type = "Yahoo 代理预取"
    elif "outlook" in ua_l or "microsoft office" in ua_l or "office365" in ua_l:
        client_type = "Outlook"
    elif "applewebkit" in ua_l and ("mail/" in ua_l or "applemail" in ua_l):
        client_type = "Apple Mail（可能预取）"
    elif "thunderbird" in ua_l:
        client_type = "Thunderbird"

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

    is_self = (email.lower() in SELF_EMAILS) or (ip in SELF_IPS)
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
    info = classify(ua, dest, email, ip)
    if query.get("test"):
        info["is_self"] = 1

    at = _now()
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()

    with _connect() as conn:
        cur = conn.execute(
            """SELECT at, ua, ip FROM tracking_events
               WHERE token = ? ORDER BY id DESC LIMIT 1""",
            (token,),
        )
        last = cur.fetchone()
        if last:
            try:
                last_ts = datetime.datetime.fromisoformat(last[0]).timestamp()
            except Exception:
                last_ts = 0
            if last[1] == ua and last[2] == ip and now_ts - last_ts < DEDUPE_SECONDS:
                return  # client fired twice in the same instant

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
            """INSERT INTO tracking (token, recipient, created_at)
               VALUES (?,?,?)
               ON CONFLICT(token) DO NOTHING""",
            (token, email, at),
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
               ORDER BY COALESCE(last_open_at, created_at) DESC
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
            item["events"] = [dict(e) for e in events]
            result.append(item)

        today = datetime.datetime.now(TZ).date().isoformat()
        summary = {
            "tracked": len(rows),
            "opened": sum(1 for r in rows if r["first_open_at"]),
            "human_opened": sum(1 for r in rows if r["first_human_open_at"]),
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
        return {"summary": summary, "rows": result, "generated_at": _now()}


def purge(target: str) -> int:
    """Delete one tracking row (by token or recipient email). Returns rows removed."""
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


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>邮件打开追踪</title>
<style>
  :root{--bg:#f6f7f9;--card:#fff;--line:#e6e8eb;--text:#1f2328;--muted:#6b7280;
        --green:#137333;--amber:#b06000;--red:#c5221f;--blue:#1a73e8}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",Arial,sans-serif}
  .wrap{max-width:1080px;margin:0 auto;padding:24px 20px 60px}
  h1{font-size:19px;margin:0 0 4px;display:flex;align-items:center;gap:8px}
  .sub{color:var(--muted);font-size:12.5px;margin-bottom:18px}
  .cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
  .card .k{font-size:12px;color:var(--muted);margin-bottom:6px}
  .card .v{font-size:24px;font-weight:700;letter-spacing:-.02em}
  .card .v.green{color:var(--green)}
  .toolbar{display:flex;gap:10px;align-items:center;margin-bottom:10px}
  input[type=search]{flex:1;padding:8px 11px;border:1px solid var(--line);border-radius:8px;
        font:inherit;background:#fff;outline:none}
  input[type=search]:focus{border-color:#c7cbd1}
  button{padding:8px 14px;border:1px solid var(--line);background:#fff;border-radius:8px;
        font:inherit;cursor:pointer}
  button:hover{background:#f2f3f5}
  table{width:100%;border-collapse:collapse;background:var(--card);
        border:1px solid var(--line);border-radius:10px;overflow:hidden}
  th,td{padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);
        font-size:13px;vertical-align:top}
  th{background:#fafbfc;color:var(--muted);font-weight:600;font-size:12px;
     position:sticky;top:0;cursor:default}
  tr.row{cursor:pointer}
  tr.row:hover{background:#fafbfc}
  tr.row:last-child td{border-bottom:0}
  .email{font-weight:600}
  .badge{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;
         border:1px solid transparent;margin-left:6px;vertical-align:1px}
  .b-green{background:#e8f5ec;color:var(--green);border-color:#c6e3cf}
  .b-amber{background:#fdf1e0;color:var(--amber);border-color:#f2d9b0}
  .b-gray{background:#f1f3f4;color:var(--muted);border-color:#e3e5e8}
  .b-red{background:#fce8e6;color:var(--red);border-color:#f3c8c3}
  .muted{color:var(--muted)}
  .events{background:#fbfcfd}
  .events td{padding:0 12px 12px}
  .ev{display:grid;grid-template-columns:150px 130px 150px 1fr;gap:8px;
      padding:7px 0;border-top:1px dashed var(--line);font-size:12px}
  .ev:first-child{border-top:0}
  .ua{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .empty{padding:28px;text-align:center;color:var(--muted)}
  code{background:#f1f3f4;padding:1px 5px;border-radius:4px;font-size:12px}
  @media(max-width:760px){.cards{grid-template-columns:repeat(2,1fr)}
    .ev{grid-template-columns:1fr 1fr}}
</style>
</head>
<body>
<div class="wrap">
  <h1>📮 邮件打开追踪</h1>
  <div class="sub" id="sub">加载中…</div>

  <div class="cards">
    <div class="card"><div class="k">追踪对象</div><div class="v" id="m-tracked">–</div></div>
    <div class="card"><div class="k">已打开</div><div class="v green" id="m-opened">–</div></div>
    <div class="card"><div class="k">真人打开</div><div class="v" id="m-human">–</div></div>
    <div class="card"><div class="k">今日事件</div><div class="v" id="m-today">–</div></div>
  </div>

  <div class="toolbar">
    <input type="search" id="q" placeholder="搜索收件人邮箱…">
    <button id="refresh">刷新</button>
  </div>

  <table>
    <thead>
      <tr>
        <th style="width:26%">收件人</th>
        <th style="width:17%">首次打开</th>
        <th style="width:17%">最近打开</th>
        <th style="width:8%">次数</th>
        <th style="width:16%">客户端</th>
        <th style="width:16%">标记</th>
      </tr>
    </thead>
    <tbody id="rows"><tr><td colspan="6" class="empty">加载中…</td></tr></tbody>
  </table>
</div>

<script>
const $ = (id) => document.getElementById(id);
let DATA = { rows: [] };

function fmt(iso){
  if(!iso) return '<span class="muted">—</span>';
  const d = new Date(iso);
  if(isNaN(d)) return iso;
  const p = (n)=>String(n).padStart(2,'0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function ago(iso){
  if(!iso) return '';
  const s = (Date.now() - new Date(iso).getTime())/1000;
  if(s < 60) return '刚刚';
  if(s < 3600) return Math.round(s/60)+' 分钟前';
  if(s < 86400) return Math.round(s/3600)+' 小时前';
  return Math.round(s/86400)+' 天前';
}
function esc(s){
  return String(s??'').replace(/[&<>"']/g, c=>({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
}
function badges(r){
  const out=[];
  if(r.first_human_open_at) out.push('<span class="badge b-green">真人已读</span>');
  else if(r.first_open_at) out.push('<span class="badge b-amber">仅代理/自助</span>');
  else out.push('<span class="badge b-gray">未打开</span>');
  if(r.self_open_count) out.push('<span class="badge b-gray">自己打开 '+r.self_open_count+'</span>');
  if(r.proxy_open_count) out.push('<span class="badge b-amber">代理 '+r.proxy_open_count+'</span>');
  if(r.bot_count) out.push('<span class="badge b-red">扫描 '+r.bot_count+'</span>');
  return out.join('');
}
function render(){
  const q = $('q').value.trim().toLowerCase();
  const rows = DATA.rows.filter(r=>!q || (r.recipient||'').toLowerCase().includes(q));
  if(!rows.length){
    $('rows').innerHTML = '<tr><td colspan="6" class="empty">没有记录</td></tr>';
    return;
  }
  $('rows').innerHTML = rows.map((r,i)=>`
    <tr class="row" data-i="${i}">
      <td><div class="email">${esc(r.recipient || r.token)}</div>
          <div class="muted" style="font-size:11.5px">${r.open_count} 次命中 · ${r.human_open_count} 次真人</div></td>
      <td>${fmt(r.first_human_open_at || r.first_open_at)}</td>
      <td>${fmt(r.last_open_at)} <span class="muted" style="font-size:11px">${ago(r.last_open_at)}</span></td>
      <td>${r.open_count}</td>
      <td>${esc(r.last_client_type || '—')}</td>
      <td>${badges(r)}</td>
    </tr>
    <tr class="events" id="ev-${i}" style="display:none"><td colspan="6">
      ${(r.events||[]).map(e=>`
        <div class="ev">
          <div>${fmt(e.at)}</div>
          <div>${esc(e.client_type||'')}</div>
          <div>${esc(e.ip||'')}</div>
          <div class="ua" title="${esc(e.ua||'')}">${esc(e.ua||'')}${e.is_self?' · 自己':''}${e.is_bot?' · 机器人':''}${e.is_direct?' · 直接访问':''}</div>
        </div>`).join('') || '<div class="muted">无事件</div>'}
    </td></tr>`).join('');

  document.querySelectorAll('tr.row').forEach(tr=>{
    tr.addEventListener('click',()=>{
      const ev = document.getElementById('ev-'+tr.dataset.i);
      ev.style.display = ev.style.display === 'none' ? '' : 'none';
    });
  });
}
async function load(){
  $('sub').textContent='加载中…';
  try{
    const res = await fetch('/api/tracking', {credentials:'same-origin'});
    if(res.status===401){ location.href='/tracking'; return; }
    DATA = await res.json();
    $('m-tracked').textContent = DATA.summary.tracked;
    $('m-opened').textContent  = DATA.summary.opened;
    $('m-human').textContent   = DATA.summary.human_opened;
    $('m-today').textContent   = DATA.summary.events_today;
    $('sub').textContent = '更新于 ' + fmt(DATA.generated_at) + ' · 点击任意一行查看明细';
    render();
  }catch(err){
    $('sub').textContent = '加载失败：' + err.message;
  }
}
$('q').addEventListener('input', render);
$('refresh').addEventListener('click', load);
load();
</script>
</body>
</html>
"""

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
        if not authorized(handler.headers, query):
            _send(handler, 401, b'{"error":"unauthorized"}', "application/json")
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
