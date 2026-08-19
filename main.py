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
import datetime
import email
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesHeaderParser
from email.utils import getaddresses, format_datetime, make_msgid, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

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
MAIL_STATS_FILE = os.environ.get("MAIL_STATS_FILE", "/data/mail_stats.jsonl")
REPORTS_DIR = os.environ.get("REPORTS_DIR", "/data/reports")
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


def feishu_post_card(card):
    """Send an interactive card (msg_type=interactive) to the configured receive id."""
    if not (FEISHU_APP_ID and FEISHU_APP_SECRET and RECEIVE_ID):
        log("feishu not configured, skip send")
        return None
    token = feishu_token()
    url = ("https://open.feishu.cn/open-apis/im/v1/messages"
           "?receive_id_type=" + urllib.parse.quote(RECEIVE_ID_TYPE))
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


def feishu_send_card(folder_label, label, from_, subject, summary, date, email, name="", show_buttons=False, key=""):
    """Send an interactive card. 'view original' on every card; 'add to CAM-03'
    only on explicit stranger inquiries (show_buttons=True)."""
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
            "text": {"tag": "plain_text", "content": "✍️ 自动回复"},
            "type": "default",
            "value": {"action": "draft_reply", "key": key},
        })
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
    return feishu_post_card(card)


def feishu_send_inquiry_card_v2(folder_label, label, from_, subject, summary, date, email, name, key):
    """Card JSON 2.0 for stranger inquiries: normal buttons + a multi-line guidance
    form whose submit triggers a 'guided_reply' (guide text comes back in form_value)."""
    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": f"{label} · {folder_label}"}},
        "body": {
            "direction": "vertical",
            "elements": [
                {"tag": "div", "text": {"tag": "plain_text",
                 "content": f"发件人：{from_}\n主题：{subject}\n时间：{date}\n摘要：{summary}"}},
                {"tag": "hr"},
                {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "default", "columns": [
                    {"tag": "column", "width": "auto", "vertical_align": "top", "elements": [
                        {"tag": "button", "text": {"tag": "plain_text", "content": "📄 查看原文"}, "type": "default",
                         "value": {"action": "view_original", "key": key}}]},
                    {"tag": "column", "width": "auto", "vertical_align": "top", "elements": [
                        {"tag": "button", "text": {"tag": "plain_text", "content": "✍️ 自动回复"}, "type": "default",
                         "value": {"action": "draft_reply", "key": key}}]},
                    {"tag": "column", "width": "auto", "vertical_align": "top", "elements": [
                        {"tag": "button", "text": {"tag": "plain_text", "content": "➕ 加入 CAM-03"}, "type": "primary",
                         "value": {"action": "add_contact", "email": email, "name": name, "from": from_, "subject": subject}}]},
                ]},
                {"tag": "hr"},
                {"tag": "form", "name": "guide_form", "elements": [
                    {"tag": "input", "name": "guide_input", "input_type": "multiline_text", "rows": 3,
                     "auto_resize": True, "max_length": 1000,
                     "label": {"tag": "plain_text", "content": "💡 指导意见（可选，可多行）"},
                     "placeholder": {"tag": "plain_text", "content": "例如：强调交期优势；报价区间 8-10 USD；附面料样卡"}},
                    {"tag": "button", "name": "submit_guide", "text": {"tag": "plain_text", "content": "✍️ 生成指导型回复"},
                     "type": "primary", "action_type": "form_submit",
                     "value": {"action": "guided_reply", "key": key}},
                ]},
            ],
        },
    }
    return feishu_post_card(card)


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
                email = _extract_email(from_)
                log_stats({
                    "type": "mail", "folder": folder_label, "from": from_, "email": email,
                    "subject": subject, "label": r["label"], "notify": r["should"],
                    "summary": r["summary"],
                })
                if not r["should"]:
                    continue
                log(f"notify card: [{r['label']}] {from_} | {subject}")
                key = f"{item.get('folder', '')}::{item.get('uid', '')}"
                try:
                    cache_mail(key, from_, subject, item.get("date", ""), folder_label, item.get("body", ""))
                except Exception as e:
                    log("mail cache write failed:", e)
                try:
                    if r["buttons"]:
                        feishu_send_inquiry_card_v2(folder_label, r["label"], from_, subject, r["summary"], item.get("date", ""), email, r["name"], key)
                    else:
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


# ---------- mail stats (weekly report data) ----------

_stats_lock = threading.Lock()


def log_stats(event):
    """Append one event (mail or action) to the JSONL stats file."""
    try:
        event = dict(event)
        event.setdefault("ts", time.strftime("%Y-%m-%d %H:%M:%S"))
        with _stats_lock:
            d = os.path.dirname(MAIL_STATS_FILE)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(MAIL_STATS_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        log("log_stats failed:", e)


def _topic_bucket(text):
    t = (text or "").lower()
    for cat, kws in CATEGORY_KEYWORDS.items():
        if any(k in t for k in kws):
            return cat
    return "other"


def _aggregate_week(records):
    agg = {
        "total": 0, "by_folder": {}, "by_label": {}, "notify": 0, "filtered": 0,
        "spam_recovered": 0, "top_senders": {}, "topics": {}, "actions": {},
    }
    for r in records:
        if r.get("type") == "action":
            action = r.get("action", "?")
            agg["actions"][action] = agg["actions"].get(action, 0) + 1
            continue
        agg["total"] += 1
        folder = r.get("folder", "?")
        agg["by_folder"][folder] = agg["by_folder"].get(folder, 0) + 1
        label = r.get("label", "?")
        agg["by_label"][label] = agg["by_label"].get(label, 0) + 1
        if r.get("notify"):
            agg["notify"] += 1
            if folder == "垃圾邮件":
                agg["spam_recovered"] += 1
        else:
            agg["filtered"] += 1
        email = r.get("email") or ""
        if email:
            agg["top_senders"][email] = agg["top_senders"].get(email, 0) + 1
        if label == "泳装相关":
            topic = _topic_bucket(f"{r.get('subject', '')} {r.get('summary', '')}")
            agg["topics"][topic] = agg["topics"].get(topic, 0) + 1
    return agg


def _load_stats_window(since_ts):
    records = []
    try:
        with open(MAIL_STATS_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if since_ts and rec.get("ts", "") < since_ts:
                    continue
                records.append(rec)
    except Exception:
        pass
    return records


def seconds_until_sunday_8am(tz_name):
    tz = ZoneInfo(tz_name)
    now = datetime.datetime.now(tz)
    days_ahead = (6 - now.weekday()) % 7  # days until next Sunday
    nxt = (now + datetime.timedelta(days=days_ahead)).replace(hour=8, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += datetime.timedelta(days=7)
    return max(1.0, (nxt - now).total_seconds())


def generate_weekly_report():
    """Aggregate the past week's stats, add LLM insights, send a Feishu card + archive."""
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime.datetime.now(tz)
    since = (now - datetime.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    records = _load_stats_window(since)
    agg = _aggregate_week(records)

    by_folder, by_label = agg["by_folder"], agg["by_label"]
    overview = (
        f"**收件总数：{agg['total']}**\n"
        f"收件箱 {by_folder.get('收件箱', 0)} ｜ 通知 {by_folder.get('通知', 0)} ｜ "
        f"订阅 {by_folder.get('订阅', 0)} ｜ 垃圾 {by_folder.get('垃圾邮件', 0)}\n"
        f"有效推送 {agg['notify']} ｜ 过滤 {agg['filtered']}"
        + (f" ｜ 垃圾中捞回 {agg['spam_recovered']}" if agg['spam_recovered'] else "")
    )
    labels = " ｜ ".join(
        f"{name} {by_label.get(name, 0)}" for name in ("泳装相关", "客户", "回复", "中性", "未知") if by_label.get(name)
    )
    if labels:
        overview += f"\n{labels}"

    topic_labels = {
        "pricing-moq": "报价/MOQ", "fabric-trims": "面料/辅料", "sampling": "打样",
        "quality-production": "生产/质检", "logistics-payment": "物流/付款",
        "oem-private-label": "OEM/私标", "sales-channels": "批发/分销",
        "guides": "尺码/颜色", "other": "其他",
    }
    topics = "、".join(f"{topic_labels.get(k, k)} {v}" for k, v in sorted(agg['topics'].items(), key=lambda x: -x[1])) or "（无）"
    top_senders = sorted(agg["top_senders"].items(), key=lambda x: -x[1])[:5]
    senders = "、".join(f"{em.split('@')[0]}×{n}" for em, n in top_senders) or "（无）"
    action_labels = {
        "view_original": "查看原文", "draft_reply": "自动回复", "save_draft": "存入草稿", "add_contact": "加入CAM-03",
    }
    actions = " ｜ ".join(
        f"{action_labels.get(k, k)} {v}" for k, v in sorted(agg['actions'].items(), key=lambda x: -x[1])
    ) or "（无）"

    insight_lines = [
        f"[{r.get('label', '')}] {r.get('email', '')} | {r.get('subject', '')[:60]} | {r.get('summary', '')[:100]}"
        for r in records if r.get("type") == "mail" and r.get("notify")
    ]
    insights = "（本周数据较少，暂不生成洞察）"
    if insight_lines:
        try:
            insights = mailai.weekly_insights("\n".join(insight_lines[-200:]))
        except Exception as e:
            log("weekly insights failed:", e)

    date_range = f"{since[:10]} ~ {now.strftime('%Y-%m-%d')}"
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": f"📊 本周邮件分析 · {date_range}"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": overview}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**询盘话题：** {topics}\n**Top 发件人：** {senders}"}},
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**按钮操作：** {actions}"}},
            {"tag": "note", "elements": [{"tag": "lark_md", "content": f"**AI 洞察**\n{insights}"}]},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": f"区间：{date_range}（Asia/Shanghai）；明细已归档 /data/reports/"}]},
        ],
    }
    try:
        feishu_post_card(card)
        log(f"weekly report sent: {agg['total']} mails in window")
    except Exception as e:
        log("weekly report send failed:", e)

    try:
        os.makedirs(REPORTS_DIR, exist_ok=True)
        fname = os.path.join(REPORTS_DIR, f"weekly-{now.strftime('%Y-%m-%d')}.md")
        with open(fname, "w", encoding="utf-8") as fh:
            fh.write(f"# 本周邮件分析 {date_range}\n\n## 总览\n{overview}\n\n## 询盘话题\n{topics}\n\n## Top 发件人\n")
            for em, n in top_senders:
                fh.write(f"- {em} x{n}\n")
            fh.write(f"\n## 按钮操作\n{actions}\n\n## AI 洞察\n{insights}\n")
    except Exception as e:
        log("weekly report archive failed:", e)


def weekly_report_loop():
    """Every Sunday 08:00 Asia/Shanghai: generate and send the weekly report."""
    if not MAILBOXES:
        log("weekly report: no mailboxes configured, skip")
        return
    while True:
        try:
            delay = seconds_until_sunday_8am("Asia/Shanghai")
            log(f"weekly report: next run in {int(delay)}s (Sunday 08:00 Asia/Shanghai)")
            time.sleep(delay)
            generate_weekly_report()
        except Exception as e:
            log("weekly report error:", e)
            time.sleep(3600)


# ---------- knowledge pack (reply-draft context) ----------

KNOWLEDGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge")

CATEGORY_KEYWORDS = {
    "pricing-moq": ["moq", "price", "pricing", "quote", "cost", "budget", "报价", "价格", "起订量"],
    "fabric-trims": ["fabric", "spandex", "nylon", "polyester", "trim", "print", "面料", "材质", "辅料"],
    "sampling": ["sample", "sampling", "prototype", "打样", "样品", "样衣"],
    "quality-production": ["quality", "inspection", "production", "lead time", "交期", "生产", "质检"],
    "logistics-payment": ["shipping", "logistic", "delivery", "payment", "deposit", "fob", "ddu", "运输", "付款", "物流", "海运"],
    "oem-private-label": ["oem", "odm", "private label", "custom", "贴牌", "定制"],
    "sales-channels": ["dropshipping", "wholesale", "retail", "分销", "批发"],
    "guides": ["size", "color", "尺码", "颜色", "色卡"],
}


def _read_file(path, default=""):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return default


def load_voice_and_rules():
    return (
        _read_file(os.path.join(KNOWLEDGE_DIR, "voice.md")),
        _read_file(os.path.join(KNOWLEDGE_DIR, "reply-rules.md")),
    )


def select_knowledge_categories(text):
    text = (text or "").lower()
    matched = [cat for cat, kws in CATEGORY_KEYWORDS.items() if any(k in text for k in kws)]
    return matched or ["pricing-moq", "logistics-payment"]


def load_knowledge_for(text, max_chars=14000):
    """Load knowledge .md files whose 分类 matches the email's topic categories."""
    cats = set(select_knowledge_categories(text))
    parts, used = [], 0
    try:
        names = sorted(os.listdir(KNOWLEDGE_DIR))
    except Exception:
        names = []
    for fname in names:
        if not fname.endswith(".md") or fname == "INDEX.md":
            continue
        content = _read_file(os.path.join(KNOWLEDGE_DIR, fname))
        if not content or not any(f"分类：{cat}" in content for cat in cats):
            continue
        if used + len(content) > max_chars:
            continue
        parts.append(content)
        used += len(content)
    return "\n\n---\n\n".join(parts)


def load_sample_reply(categories):
    if "sampling" in categories:
        fname = "sample-order-reply.md"
    elif "pricing-moq" in categories:
        fname = "moq-pricing-reply.md"
    else:
        fname = "rfq-detailed-reply.md"
    return _read_file(os.path.join(KNOWLEDGE_DIR, "reply-samples", fname))


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
            log_stats({"type": "action", "action": "add_contact", "email": email})
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
    log_stats({"type": "action", "action": "view_original", "key": key})
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


def _do_draft_reply(key, guide=""):
    """Background: build a reply draft (voice + rules + knowledge [+ guide]) and send it to Feishu."""
    try:
        entry = load_mail_cache().get(key)
        if not entry:
            feishu_send("❌ 找不到该邮件（缓存可能已过期）")
            return
        from_ = entry.get("from", "")
        subject = entry.get("subject", "")
        body = entry.get("body", "")
        text = f"{subject}\n{body}"
        categories = select_knowledge_categories(text)
        knowledge = load_knowledge_for(text)
        voice, rules = load_voice_and_rules()
        sample = load_sample_reply(categories)
        draft, err = mailai.draft_reply(from_, subject, body, knowledge, sample, voice, rules, guide)
        if err or not draft:
            feishu_send(f"❌ 草稿生成失败：{err or '空结果'}")
            return
        # persist the draft so "save to Zoho draft" can reuse the exact reviewed text
        try:
            cache = load_mail_cache()
            if key in cache:
                cache[key]["draft"] = draft
                save_mail_cache(cache)
        except Exception as e:
            log("draft cache save failed:", e)
        guided = bool(guide and guide.strip())
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"template": "blue", "title": {"tag": "plain_text", "content": "✍️ 指导型回复草稿" if guided else "✍️ 回复草稿"}},
            "elements": [
                {"tag": "div", "text": {"tag": "plain_text", "content": f"致：{from_}\n建议主题：Re: {subject}\n\n{draft}"}},
                {"tag": "hr"},
                {"tag": "action", "actions": [
                    {"tag": "button", "text": {"tag": "plain_text", "content": "📥 存入 Zoho 草稿"},
                     "type": "primary", "value": {"action": "save_draft", "key": key}},
                ]},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": "⚠️ 未发送；存入草稿后可到 Zoho Mail 草稿箱编辑再发送"}]},
            ],
        }
        try:
            feishu_post_card(card)
            log(f"draft card sent: {len(draft)} chars, for {from_}" + (" (guided)" if guided else ""))
        except Exception as e:
            log("draft card send failed:", e)
    except Exception as e:
        log("draft_reply failed:", e)
        try:
            feishu_send(f"❌ 草稿生成失败：{e}")
        except Exception:
            pass


@register_action("draft_reply")
def action_draft_reply(value):
    """Generate a reply draft for a cached stranger inquiry (background, no auto-send)."""
    key = (value.get("key") or "").strip()
    if not key:
        return {"toast": {"type": "error", "content": "缺少邮件标识"}}
    log_stats({"type": "action", "action": "draft_reply", "key": key})
    threading.Thread(target=_do_draft_reply, args=(key,), daemon=True).start()
    return {"toast": {"type": "info", "content": "正在生成回复草稿…"}}


@register_action("guided_reply")
def action_guided_reply(value):
    """Generate a reply draft following the user's typed guidance (multi-line form input)."""
    key = (value.get("key") or "").strip()
    form_value = value.get("form_value") or {}
    guide = (form_value.get("guide_input") or "").strip() if isinstance(form_value, dict) else ""
    if not key:
        return {"toast": {"type": "error", "content": "缺少邮件标识"}}
    if not guide:
        return {"toast": {"type": "error", "content": "请先输入指导意见"}}
    log_stats({"type": "action", "action": "guided_reply", "key": key})
    threading.Thread(target=_do_draft_reply, args=(key, guide), daemon=True).start()
    return {"toast": {"type": "info", "content": "正在按指导意见生成草稿…"}}


# ---------- save draft to Zoho (never send) ----------

DRAFTS_FOLDER = "&g0l6P3ux-"  # Zoho CN Drafts (modified UTF-7 of 草稿)
REPLY_FROM_NAME = "Ian at Hongxiu"
REPLY_FROM_ADDR = "ian@wearhongxiu.com"
ZOHO_MAIL_ACCOUNT_ID = os.environ.get("ZOHO_MAIL_ACCOUNT_ID", "1486660000000002002")


def _zoho_mail_save_draft(from_addr, to_addr, subject, content, in_reply_to="", ref_header=""):
    """Save a reply draft via the Zoho Mail official API (mode=draft, NEVER sends).

    inReplyTo/refHeader make Zoho treat the draft as a reply to the original message,
    so it threads correctly when sent (equivalent to web-UI reply -> save draft).
    """
    token = zoho_contacts._access_token()
    base = "https://mail.zoho.com.cn/api" if zoho_contacts.ZOHO_REGION == "cn" else "https://mail.zoho.com/api"
    url = f"{base}/accounts/{ZOHO_MAIL_ACCOUNT_ID}/messages"
    payload = {
        "mode": "draft",
        "fromAddress": from_addr,
        "toAddress": to_addr,
        "subject": subject,
        "content": content,
        "mailFormat": "plaintext",
    }
    if in_reply_to:
        payload["inReplyTo"] = in_reply_to
    if ref_header:
        payload["refHeader"] = ref_header
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST", headers={
        "Authorization": "Zoho-oauthtoken " + token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_mail_key(key):
    if "::" not in key:
        return None, None
    folder, _, uid = key.partition("::")
    return folder, uid


def _fetch_original_by_key(folder, uid):
    """Fetch the original message (RFC822) from folder+uid on the first configured mailbox."""
    if not MAILBOXES:
        return None
    mb = MAILBOXES[0]
    M = imaplib_connect(mb["host"], int(mb.get("port", 993)), mb["user"], mb["password"])
    try:
        try:
            M.select(folder)
        except Exception as e:
            log(f"save_draft: select {folder} failed:", e)
            return None
        typ, data = M.uid("fetch", str(uid), "(RFC822)")
        if not data or not data[0]:
            return None
        return email.message_from_bytes(data[0][1])
    finally:
        M.logout()


def _quote_original(msg):
    body, _ = mailai.extract_body(msg)
    body = (body or "").strip()[:12000]
    if not body:
        return ""
    return "> " + body.replace("\n", "\n> ")


def _do_save_draft(key):
    """Background: save the reviewed reply as a threaded draft via the Zoho Mail API."""
    try:
        folder, uid = _parse_mail_key(key)
        if not folder or not uid:
            feishu_send("❌ 存入草稿失败：无效的邮件标识")
            return
        entry = load_mail_cache().get(key)
        draft = (entry or {}).get("draft", "")
        if not draft:
            feishu_send("❌ 找不到草稿内容（请先点「✍️ 自动回复」生成）")
            return
        msg = _fetch_original_by_key(folder, uid)
        if msg is None:
            feishu_send("❌ 无法读取原始邮件")
            return

        from_ = decode_mime(msg.get("From"))
        subject = decode_mime(msg.get("Subject"))
        msg_id = (msg.get("Message-ID") or "").strip()
        refs = (msg.get("References") or "").strip()
        date = msg.get("Date") or ""

        to_value = from_ if from_ else REPLY_FROM_ADDR
        reply_subject = subject or ""
        if not re.match(r"^\s*(re|回复)\s*[:：]", reply_subject, re.I):
            reply_subject = f"Re: {reply_subject}"

        content = (
            f"{draft}\n\n发件人: {from_}\n到: {REPLY_FROM_ADDR}\n日期: {date}\n主题: {subject}\n\n{_quote_original(msg)}"
        )
        ref_header = (refs + " " + msg_id).strip() if msg_id else refs

        resp = _zoho_mail_save_draft(
            REPLY_FROM_ADDR, to_value, reply_subject, content,
            in_reply_to=msg_id, ref_header=ref_header,
        )
        log(f"draft saved via Zoho API: {to_value} | {reply_subject} | inReplyTo={bool(msg_id)} | resp={json.dumps(resp, ensure_ascii=False)[:160]}")
        feishu_send(
            f"✅ 已存入 Zoho 草稿箱（作为原邮件回复）\n致：{to_value}\n主题：{reply_subject}\n"
            "（草稿未发送；可在 Zoho Mail 草稿箱编辑后发送）"
        )
    except Exception as e:
        log("save_draft failed:", e)
        try:
            feishu_send(f"❌ 存入草稿失败：{e}")
        except Exception:
            pass


@register_action("save_draft")
def action_save_draft(value):
    """Save the reviewed reply draft into the Zoho Mail Drafts folder (background, no send)."""
    key = (value.get("key") or "").strip()
    if not key:
        return {"toast": {"type": "error", "content": "缺少邮件标识"}}
    log_stats({"type": "action", "action": "save_draft", "key": key})
    threading.Thread(target=_do_save_draft, args=(key,), daemon=True).start()
    return {"toast": {"type": "info", "content": "正在存入草稿箱…"}}


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
        if not isinstance(value, dict):
            value = {"raw": value}
        form_value = action.get("form_value") or {}
        if isinstance(form_value, dict):
            value["form_value"] = form_value  # expose form inputs to handlers
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
    threading.Thread(target=weekly_report_loop, daemon=True).start()
    HTTPServer(("0.0.0.0", PORT), Health).serve_forever()
