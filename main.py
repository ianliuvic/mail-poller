#!/usr/bin/env python3
"""Mail poller: poll IMAP mailboxes and push new-mail notifications to Feishu.

Config (all via environment variables, no secrets in code):
  MAILBOXES_JSON        JSON array of mailboxes:
                        [{"name":"zoho","host":"imap.zoho.com.cn","port":993,
                          "user":"x@y.com","password":"app-password",
                          "folders":["INBOX","Notification","Newsletter","&V4NXPpCuTvY-"],
                          "sent_folder":"&XfJT0ZABkK5O9g-"}]
                        ("folder" is still accepted as a single-folder shorthand;
                         folder names are IMAP names, possibly modified UTF-7.)
  FEISHU_APP_ID / FEISHU_APP_SECRET
  FEISHU_RECEIVE_ID        target chat_id / open_id
  FEISHU_RECEIVE_ID_TYPE   chat_id | open_id | user_id | email (default chat_id)
  POLL_INTERVAL_SECONDS    default 300
  STATE_FILE               default /data/last_uid.json
  SENT_SET_FILE            default /data/sent_set.json (outgoing Message-IDs + recipients)
  PORT                     health HTTP port, default 8000
"""

import base64
import email
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from email.header import decode_header, make_header
from email.parser import BytesHeaderParser
from email.utils import getaddresses, parsedate_to_datetime
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
SENT_SET_FILE = os.environ.get("SENT_SET_FILE", "/data/sent_set.json")
FEISHU_VERIFICATION_TOKEN = os.environ.get("FEISHU_VERIFICATION_TOKEN", "")
MAIL_CACHE_FILE = os.environ.get("MAIL_CACHE_FILE", "/data/mail_cache.json")
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


def feishu_send_card(folder_label, label, from_, subject, summary, date, email, name="", show_buttons=False, key=""):
    """Send an interactive card. 'view original' on every card; 'add to CAM-03'
    only on explicit stranger inquiries (show_buttons=True)."""
    if not (FEISHU_APP_ID and FEISHU_APP_SECRET and RECEIVE_ID):
        log("feishu not configured, skip send")
        return None
    token = feishu_token()
    url = ("https://open.feishu.cn/open-apis/im/v1/messages"
           "?receive_id_type=" + urllib.parse.quote(RECEIVE_ID_TYPE))
    actions = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "📄 查看原文"},
            "type": "default",
            "value": {"action": "view_original", "key": key},
        },
    ]
    if show_buttons:
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "➕ 加入 CAM-03"},
            "type": "primary",
            "value": {"action": "add_contact", "email": email, "name": name, "from": from_, "subject": subject},
        })
    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": f"发件人：{from_}\n主题：{subject}\n时间：{date}\n摘要：{summary}",
            },
        },
        {"tag": "hr"},
        {"tag": "action", "actions": actions},
    ]
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": f"{label} · {folder_label}"},
        },
        "elements": elements,
    }
    body = json.dumps({
        "receive_id": RECEIVE_ID,
        "msg_type": "interactive",
        "content": json.dumps(card),
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


# ---------- mail cache (for "view original" button) ----------

def load_mail_cache():
    try:
        with open(MAIL_CACHE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_mail_cache(cache):
    d = os.path.dirname(MAIL_CACHE_FILE)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = MAIL_CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False)
    os.replace(tmp, MAIL_CACHE_FILE)


def cache_mail(key, from_, subject, date, folder, body, max_entries=100):
    """Cache an email's body so the 'view original' button can retrieve it later."""
    cache = load_mail_cache()
    cache[key] = {
        "from": from_,
        "subject": subject,
        "date": date,
        "folder": folder,
        "body": (body or "")[:6000],
    }
    if len(cache) > max_entries:
        for k in list(cache)[:len(cache) - max_entries]:
            cache.pop(k, None)
    save_mail_cache(cache)


# ---------- IMAP ----------

def decode_mime(v):
    if v is None:
        return ""
    try:
        return str(make_header(decode_header(v)))
    except Exception:
        return str(v)


def imap_utf7_decode(s):
    """Decode an IMAP modified-UTF-7 folder name (e.g. '&V4NXPpCuTvY-' -> spam)."""
    if not s:
        return s
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "&":
            j = s.find("-", i)
            if j == -1:
                out.append(s[i:])
                break
            if j == i + 1:  # "&-" encodes a literal ampersand
                out.append("&")
                i = j + 1
                continue
            b64 = s[i + 1:j].replace(",", "/")
            try:
                raw = base64.b64decode(b64 + "=" * (-len(b64) % 4))
                out.append(raw.decode("utf-16-be", "replace"))
            except Exception:
                out.append(s[i:j])
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def poll_mailbox(mb):
    host = mb["host"]
    user = mb["user"]
    pwd = mb["password"]
    port = int(mb.get("port", 993))
    name = mb.get("name", user)
    folders = mb.get("folders") or [mb.get("folder", "INBOX")]

    state = load_state()
    # migrate legacy single-key state (pre-folder support) to per-folder INBOX key
    migrated = False
    if name in state:
        state.setdefault(f"{name}::INBOX", state.pop(name))
        migrated = True

    M = imaplib_connect(host, port, user, pwd)
    found = []
    updated = False
    for folder in folders:
        try:
            M.select(folder)
        except Exception as e:
            log(f"select failed [{name}/{folder}]:", e)
            continue
        key = f"{name}::{folder}"
        if key in state:
            last_uid = int(state[key])
        else:
            # First time this folder is seen: start from its current highest UID,
            # so existing mail is NOT re-processed. To backfill later, delete this
            # folder's key from the state file and redeploy/restart.
            typ0, data0 = M.uid("search", None, "ALL")
            existing = [int(x) for x in data0[0].split()] if data0 and data0[0] else []
            last_uid = max(existing) if existing else 0
            state[key] = last_uid
            updated = True
        typ, data = M.uid("search", None, f"UID {last_uid + 1}:*")
        uids = [int(x) for x in data[0].split()] if data and data[0] else []

        for uid in uids:
            typ2, msgdata = M.uid("fetch", str(uid), "(RFC822)")
            if not msgdata or not msgdata[0]:
                continue
            raw = msgdata[0][1]
            msg = email.message_from_bytes(raw)
            from_ = decode_mime(msg.get("From"))
            subject = decode_mime(msg.get("Subject"))
            body, _ = mailai.extract_body(msg)
            date_str = ""
            try:
                date_str = parsedate_to_datetime(msg.get("Date")).strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass
            found.append({
                "uid": uid,
                "folder": folder,
                "from": from_,
                "subject": subject,
                "body": body,
                "date": date_str,
                "in_reply_to": msg.get("In-Reply-To") or "",
                "references": msg.get("References") or "",
            })
        if uids:
            state[key] = max(uids)
            updated = True
    M.logout()

    if updated or migrated:
        save_state(state)
    return found


def imaplib_connect(host, port, user, pwd):
    import imaplib
    M = imaplib.IMAP4_SSL(host, port)
    M.login(user, pwd)
    return M


# ---------- Sent set (outgoing Message-IDs + recipients) ----------

def _norm_mid(mid):
    return (mid or "").strip().strip("<>").lower()


def _extract_addrs(header_value):
    if not header_value:
        return []
    try:
        return [addr.lower() for _name, addr in getaddresses([header_value]) if addr and "@" in addr]
    except Exception:
        return []


def load_sent_set():
    """Return (set_data, last_uid). last_uid is None if not initialized yet."""
    try:
        with open(SENT_SET_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return (
            {
                "message_ids": set(d.get("message_ids", [])),
                "recipient_emails": set(d.get("recipient_emails", [])),
            },
            int(d.get("last_uid", 0)),
        )
    except Exception:
        return {"message_ids": set(), "recipient_emails": set()}, None


def save_sent_set(sent, last_uid):
    d = os.path.dirname(SENT_SET_FILE)
    if d:
        os.makedirs(d, exist_ok=True)
    payload = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(sent["message_ids"]),
        "recipient_count": len(sent["recipient_emails"]),
        "last_uid": last_uid,
        "message_ids": sorted(sent["message_ids"]),
        "recipient_emails": sorted(sent["recipient_emails"]),
    }
    tmp = SENT_SET_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.replace(tmp, SENT_SET_FILE)


def collect_sent(mb):
    """Incrementally index the mailbox's Sent folder.

    First run does a full backfill (builds the initial Message-ID + recipient set);
    later runs only fetch new UIDs. Returns a stats dict, or None if no sent_folder.
    """
    sent_folder = mb.get("sent_folder")
    if not sent_folder:
        return None
    host = mb["host"]
    user = mb["user"]
    pwd = mb["password"]
    port = int(mb.get("port", 993))
    name = mb.get("name", user)

    sent, last_uid = load_sent_set()
    M = imaplib_connect(host, port, user, pwd)
    try:
        try:
            M.select(sent_folder)
        except Exception as e:
            log(f"sent select failed [{name}/{sent_folder}]:", e)
            return None
        if last_uid is None:
            typ, data = M.uid("search", None, "ALL")
        else:
            typ, data = M.uid("search", None, f"UID {last_uid + 1}:*")
        uids = [int(x) for x in data[0].split()] if data and data[0] else []
        added = 0
        header_parser = BytesHeaderParser()
        for uid in uids:
            # only need Message-ID + To/Cc headers, so fetch headers (not full body/attachments)
            typ2, msgdata = M.uid("fetch", str(uid), "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID TO CC)])")
            if not msgdata or not msgdata[0]:
                continue
            header_bytes = b""
            for part in msgdata:
                if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], bytes):
                    header_bytes += part[1]
            if not header_bytes:
                continue
            msg = header_parser.parsebytes(header_bytes)
            mid = _norm_mid(msg.get("Message-ID"))
            if mid:
                sent["message_ids"].add(mid)
            for hdr in ("To", "Cc"):
                for addr in _extract_addrs(msg.get(hdr)):
                    sent["recipient_emails"].add(addr)
            added += 1
        if uids:
            last_uid = max(uids)
        elif last_uid is None:
            last_uid = 0
        save_sent_set(sent, last_uid)
        return {
            "added": added,
            "message_ids": len(sent["message_ids"]),
            "recipients": len(sent["recipient_emails"]),
            "last_uid": last_uid,
        }
    finally:
        M.logout()


# ---------- decision pipeline ----------

def _extract_email(from_):
    try:
        for _name, addr in getaddresses([from_]):
            if addr and "@" in addr:
                return addr.strip().lower()
    except Exception:
        pass
    return ""


def _extract_name(from_):
    try:
        for name, addr in getaddresses([from_]):
            if name:
                return name.strip()
    except Exception:
        pass
    return ""


def _extract_message_ids(header_value):
    return re.findall(r"<([^>]+)>", header_value or "")


def _is_reply(in_reply_to, references, subject):
    if in_reply_to or references:
        return True
    return re.match(r"^\s*(re|回复|答复|aw|res)\s*[:：]", subject or "", re.I) is not None


def load_contacts_emails():
    try:
        with open(zoho_contacts.CACHE_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return {c.get("email", "").lower() for c in d.get("contacts", []) if c.get("email")}
    except Exception:
        return set()


def process_mail(item, contacts_emails, sent_mids, sent_recipients):
    """Return a dict: {should, label, summary, name, buttons}.

    `buttons` is True only for an explicit stranger inquiry (useful verdict) —
    those are the emails where the "加入 CAM-03" button should appear.
    """
    from_ = item["from"]
    subject = item["subject"]
    clean = mailai.strip_quotes(item["body"])
    from_addr = _extract_email(from_)
    in_reply_to = item.get("in_reply_to", "")
    references = item.get("references", "")
    is_reply = _is_reply(in_reply_to, references, subject)

    is_contact = bool(from_addr and from_addr in contacts_emails)
    refs_my_mid = any(
        _norm_mid(m) in sent_mids
        for m in _extract_message_ids(in_reply_to) + _extract_message_ids(references)
    )
    reply_to_me = is_reply and (refs_my_mid or (from_addr and from_addr in sent_recipients))

    verdict, summary, name, err = mailai.classify_useful(
        from_, subject, clean,
        is_contact=is_contact,
        is_reply=reply_to_me,
    )
    name = name or _extract_name(from_)

    if is_contact:
        return {"should": True, "label": "客户", "summary": summary or subject or f"来自 {from_}", "name": name, "buttons": False}
    if reply_to_me:
        return {"should": True, "label": "回复", "summary": summary or subject or f"回复：{subject}", "name": name, "buttons": False}
    if err:
        return {"should": True, "label": "未知", "summary": summary or subject or f"来自 {from_}", "name": name, "buttons": False}
    if verdict == "unrelated":
        return {"should": False, "label": "无关", "summary": summary, "name": name, "buttons": False}
    if verdict == "useful":
        return {"should": True, "label": "泳装相关", "summary": summary, "name": name, "buttons": True}
    return {"should": True, "label": "中性", "summary": summary, "name": name, "buttons": False}


FOLDER_DISPLAY = {
    "INBOX": "收件箱",
    "Notification": "通知",
    "Newsletter": "订阅",
}


def _folder_display(folder):
    if folder in FOLDER_DISPLAY:
        return FOLDER_DISPLAY[folder]
    return imap_utf7_decode(folder)


def format_notify_text(folder_label, label, from_, subject, summary, date):
    lines = [f"📬【{label}】{folder_label}"]
    if from_:
        lines.append(f"发件人：{from_}")
    if subject:
        lines.append(f"主题：{subject}")
    if date:
        lines.append(f"时间：{date}")
    if summary:
        lines.append(f"摘要：{summary}")
    return "\n".join(lines)


# ---------- loop ----------

def poll_once():
    if not MAILBOXES:
        return
    contacts_emails = load_contacts_emails()
    sent, _ = load_sent_set()
    sent_mids = sent["message_ids"]
    sent_recipients = sent["recipient_emails"]
    for mb in MAILBOXES:
        name = mb.get("name", mb.get("user", "?"))
        try:
            stats = collect_sent(mb)
            if stats:
                log(f"sent sync [{name}]: +{stats['added']} new | {stats['message_ids']} msg-ids | {stats['recipients']} recipients | last_uid={stats['last_uid']}")
        except Exception as e:
            log(f"sent collect failed [{name}]:", e)
        try:
            for item in poll_mailbox(mb):
                from_ = item["from"]
                subject = item["subject"]
                folder_label = _folder_display(item.get("folder", ""))
                r = process_mail(item, contacts_emails, sent_mids, sent_recipients)
                log(f"mail [{name}/{folder_label}] from={from_} label={r['label']} notify={r['should']} buttons={r['buttons']}")
                if not r["should"]:
                    continue
                email = _extract_email(from_)
                log(f"notify card: [{r['label']}] {from_} | {subject}")
                key = f"{item.get('folder', '')}::{item.get('uid', '')}"
                try:
                    cache_mail(key, from_, subject, item.get("date", ""), folder_label, item.get("body", ""))
                except Exception as e:
                    log("mail cache write failed:", e)
                try:
                    feishu_send_card(folder_label, r["label"], from_, subject, r["summary"], item.get("date", ""), email, r["name"], r["buttons"], key)
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


# ---------- Feishu card action dispatch ----------

ACTION_HANDLERS = {}


def register_action(name):
    def decorator(fn):
        ACTION_HANDLERS[name] = fn
        return fn
    return decorator


def _refresh_contacts_cache():
    try:
        payload = zoho_contacts.sync_contacts()
        log(f"contacts cache refreshed: {payload['count']} contacts")
    except Exception as e:
        log("contacts cache refresh failed:", e)


def _do_add_contact(email, name):
    """Run in a background thread: add to CAM-03, then report the result via Feishu."""
    try:
        resp = zoho_contacts.subscribe_contact(email, name)
        log(f"add_contact: {email} name={name!r} -> {resp.get('message', resp)}")
        ok = str(resp.get("code", "")) == "0"
        if ok:
            threading.Thread(target=_refresh_contacts_cache, daemon=True).start()
        text = f"{'✅' if ok else '❌'} 加入 CAM-03：{email}"
        if name:
            text += f"（{name}）"
        if not ok:
            text += f"\n原因：{resp.get('message', '未知错误')}"
        feishu_send(text)
    except Exception as e:
        log("add_contact failed:", e)
        try:
            feishu_send(f"❌ 加入 CAM-03 失败：{e}")
        except Exception as e2:
            log("feishu send failed:", e2)


@register_action("add_contact")
def action_add_contact(value):
    """Add the stranger's email (and extracted name) to CAM-03 in the background.

    Feishu requires the card callback to respond within 3s, so the actual work
    runs in a thread and the result is delivered as a follow-up Feishu message.
    """
    email = (value.get("email") or "").strip().lower()
    name = (value.get("name") or "").strip() or _extract_name(value.get("from", ""))
    if not email or "@" not in email:
        return {"toast": {"type": "error", "content": "缺少有效邮箱"}}
    threading.Thread(target=_do_add_contact, args=(email, name), daemon=True).start()
    return {"toast": {"type": "info", "content": "正在加入 CAM-03…"}}


@register_action("view_original")
def action_view_original(value):
    """Send the cached email body back to the user via Feishu."""
    key = value.get("key", "")
    entry = load_mail_cache().get(key)
    if not entry:
        return {"toast": {"type": "error", "content": "原文不存在或已过期"}}
    body = entry.get("body", "")
    truncated = len(body) >= 6000
    text = (
        f"📄 邮件原文\n"
        f"发件人：{entry.get('from', '')}\n"
        f"主题：{entry.get('subject', '')}\n"
        f"时间：{entry.get('date', '')}\n"
        f"文件夹：{entry.get('folder', '')}\n"
        + ("（正文过长，已截断）\n" if truncated else "")
        + "————————————\n"
        + (body if body else "（无正文）")
    )
    try:
        feishu_send(text)
        return {"toast": {"type": "success", "content": "原文已发送"}}
    except Exception as e:
        log("view_original send failed:", e)
        return {"toast": {"type": "error", "content": f"发送失败：{e}"}}


def handle_feishu_callback(body_bytes):
    """Handle a Feishu event-subscription POST. Returns a JSON-serializable dict."""
    try:
        body = json.loads(body_bytes.decode("utf-8"))
    except Exception as e:
        log("feishu callback: bad json:", e)
        return {"code": 1, "msg": "bad json"}

    # URL verification (configured when saving the event subscription)
    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge", "")}

    header = body.get("header") or {}
    token = header.get("token") or body.get("token") or ""
    if FEISHU_VERIFICATION_TOKEN and token and token != FEISHU_VERIFICATION_TOKEN:
        log("feishu callback: token mismatch, ignoring")
        return {"code": 1, "msg": "invalid token"}

    event_type = header.get("event_type", "")
    if event_type == "card.action.trigger":
        event = body.get("event") or {}
        action = event.get("action") or {}
        value = action.get("value") or {}
        operator = event.get("operator") or {}
        log("feishu card action:", json.dumps({
            "action": value.get("action"),
            "email": value.get("email"),
            "operator_open_id": operator.get("open_id", ""),
        }, ensure_ascii=False))
        handler = ACTION_HANDLERS.get(value.get("action", ""))
        if handler:
            return handler(value)
        return {"toast": {"type": "warning", "content": "未知操作"}}
    log("feishu callback event:", event_type)
    return {"code": 0}


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") == "/feishu/callback":
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            if "challenge" in params:
                data = json.dumps({"challenge": params["challenge"][0]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self):
        if self.path.rstrip("/") == "/feishu/callback":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length > 0 else b""
            resp = handle_feishu_callback(raw)
            data = json.dumps(resp).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    log(f"mail-poller starting: {len(MAILBOXES)} mailbox(es), interval={POLL_INTERVAL}s, port={PORT}")
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=contacts_sync_loop, daemon=True).start()
    HTTPServer(("0.0.0.0", PORT), Health).serve_forever()
