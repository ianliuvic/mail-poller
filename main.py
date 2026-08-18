#!/usr/bin/env python3
"""Mail poller: poll IMAP mailboxes and push new-mail notifications to Feishu.

Config (all via environment variables, no secrets in code):
  MAILBOXES_JSON        JSON array of mailboxes:
                        [{"name":"zoho","host":"imap.zoho.com.cn","port":993,
                          "user":"x@y.com","password":"app-password","folder":"INBOX"}]
  FEISHU_APP_ID / FEISHU_APP_SECRET
  FEISHU_RECEIVE_ID        target chat_id / open_id
  FEISHU_RECEIVE_ID_TYPE   chat_id | open_id | user_id | email (default chat_id)
  POLL_INTERVAL_SECONDS    default 300
  STATE_FILE               default /data/last_uid.json
  PORT                     health HTTP port, default 8000
"""

import email
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from email.header import decode_header, make_header
from http.server import BaseHTTPRequestHandler, HTTPServer

import mailai
import zoho_contacts

FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
RECEIVE_ID = os.environ.get("FEISHU_RECEIVE_ID", "")
RECEIVE_ID_TYPE = os.environ.get("FEISHU_RECEIVE_ID_TYPE", "chat_id")
MAILBOXES = json.loads(os.environ.get("MAILBOXES_JSON", "[]"))
CONTACTS = json.loads(os.environ.get("CONTACTS_JSON", "[]"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))
START_FROM_UID = int(os.environ.get("START_FROM_UID", "0"))
STATE_FILE = os.environ.get("STATE_FILE", "/data/last_uid.json")
PORT = int(os.environ.get("PORT", "8000"))
CONTACTS_SYNC_TZ = os.environ.get("CONTACTS_SYNC_TZ", "Asia/Shanghai")

_lock = threading.Lock()
_token = {"value": "", "expires_at": 0}


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ---------- Feishu ----------

def feishu_token():
    now = time.time()
    if _token["value"] and _token["expires_at"] > now + 60:
        return _token["value"]
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    data = json.dumps({"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
    _token["value"] = d["tenant_access_token"]
    _token["expires_at"] = now + int(d.get("expire", 7200))
    return _token["value"]


def feishu_send(text):
    if not (FEISHU_APP_ID and FEISHU_APP_SECRET and RECEIVE_ID):
        log("feishu not configured, skip send")
        return None
    token = feishu_token()
    url = ("https://open.feishu.cn/open-apis/im/v1/messages"
           "?receive_id_type=" + urllib.parse.quote(RECEIVE_ID_TYPE))
    body = json.dumps({
        "receive_id": RECEIVE_ID,
        "msg_type": "text",
        "content": json.dumps({"text": text}),
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    })
    return json.loads(urllib.request.urlopen(req, timeout=30).read().decode())


# ---------- state ----------

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(state):
    d = os.path.dirname(STATE_FILE)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE_FILE)


# ---------- IMAP ----------

def decode_mime(v):
    if v is None:
        return ""
    try:
        return str(make_header(decode_header(v)))
    except Exception:
        return str(v)


def poll_mailbox(mb):
    host = mb["host"]
    user = mb["user"]
    pwd = mb["password"]
    port = int(mb.get("port", 993))
    folder = mb.get("folder", "INBOX")
    name = mb.get("name", user)

    state = load_state()
    last_uid = int(state.get(name, START_FROM_UID))

    M = imaplib_connect(host, port, user, pwd)
    M.select(folder)
    typ, data = M.uid("search", None, f"UID {last_uid + 1}:*")
    uids = [int(x) for x in data[0].split()] if data and data[0] else []

    found = []
    for uid in uids:
        typ2, msgdata = M.uid("fetch", str(uid), "(RFC822)")
        if not msgdata or not msgdata[0]:
            continue
        raw = msgdata[0][1]
        msg = email.message_from_bytes(raw)
        from_ = decode_mime(msg.get("From"))
        subject = decode_mime(msg.get("Subject"))
        body, _ = mailai.extract_body(msg)
        is_reply = bool(msg.get("In-Reply-To") or msg.get("References")) or \
            (re.match(r"^\s*(re|回复|答复|fw|fwd|转发)\s*[:：]", subject or "", re.I) is not None)
        found.append({
            "uid": uid,
            "from": from_,
            "subject": subject,
            "body": body,
            "is_reply": is_reply,
        })
    M.logout()

    if uids:
        state[name] = max(uids)
        save_state(state)
    return found


def imaplib_connect(host, port, user, pwd):
    import imaplib
    M = imaplib.IMAP4_SSL(host, port)
    M.login(user, pwd)
    return M


# ---------- loop ----------

def poll_once():
    if not MAILBOXES:
        return
    for mb in MAILBOXES:
        name = mb.get("name", mb.get("user", "?"))
        try:
            for item in poll_mailbox(mb):
                from_ = item["from"]
                subject = item["subject"]
                clean = mailai.strip_quotes(item["body"])
                known = any(c and c.lower() in from_.lower() for c in CONTACTS)
                result, err = mailai.classify_email(from_, subject, clean, known, item["is_reply"])
                should, label, summary = mailai.decide(result, known, item["is_reply"])
                log(f"mail [{name}] from={from_} label={label} notify={should} err={err or ''}")
                if not should:
                    continue
                text = f"[{name}] {label}\n发件人: {from_}\n主题: {subject}\n摘要: {summary}"
                log("notify:", text)
                try:
                    feishu_send(text)
                except Exception as e:
                    log("feishu send failed:", e)
        except Exception as e:
            log(f"poll failed [{name}]:", e)


def loop():
    while True:
        try:
            poll_once()
        except Exception as e:
            log("loop error:", e)
        time.sleep(POLL_INTERVAL)


def contacts_sync_loop():
    """Pull the CAM-03 contact list once at startup, then daily at 00:00 (local)."""
    if not zoho_contacts.is_configured():
        log("zoho contacts sync: not configured, skip")
        return
    try:
        payload = zoho_contacts.sync_contacts()
        log(f"zoho contacts sync: cached {payload['count']} contacts -> {zoho_contacts.CACHE_FILE}")
    except Exception as e:
        log("zoho contacts sync failed:", e)
    while True:
        try:
            delay = zoho_contacts.seconds_until_next_midnight(CONTACTS_SYNC_TZ)
            log(f"zoho contacts sync: next run in {int(delay)}s (daily 00:00 {CONTACTS_SYNC_TZ})")
            time.sleep(delay)
            payload = zoho_contacts.sync_contacts()
            log(f"zoho contacts sync: cached {payload['count']} contacts -> {zoho_contacts.CACHE_FILE}")
        except Exception as e:
            log("zoho contacts sync error:", e)
            time.sleep(3600)


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    log(f"mail-poller starting: {len(MAILBOXES)} mailbox(es), interval={POLL_INTERVAL}s, port={PORT}")
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=contacts_sync_loop, daemon=True).start()
    HTTPServer(("0.0.0.0", PORT), Health).serve_forever()
