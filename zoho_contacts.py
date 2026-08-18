#!/usr/bin/env python3
"""Zoho Campaigns contact cache.

Pulls the CAM-03 (or any configured) mailing list's ACTIVE contacts via the
Campaigns v1.1 API and caches them to a JSON file, so the poller can answer
"is this sender a known contact?" with a local lookup instead of an API call
per email.

Config (environment variables):
  ZOHO_REGION            "cn" (China DC) or "com" (global). Default "cn".
  ZOHO_CLIENT_ID         OAuth2 Self Client client id.
  ZOHO_CLIENT_SECRET     OAuth2 Self Client client secret.
  ZOHO_REFRESH_TOKEN     OAuth2 refresh token (offline).
  CAMPAIGNS_LISTKEY      Mailing list key to pull contacts from.
  CONTACTS_CACHE_FILE    Where to write the cache. Default /data/contacts.json.

Only standard library; no third-party deps.
"""

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ZOHO_REGION = os.environ.get("ZOHO_REGION", "cn").lower()
CLIENT_ID = os.environ.get("ZOHO_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("ZOHO_CLIENT_SECRET", "")
REFRESH_TOKEN = os.environ.get("ZOHO_REFRESH_TOKEN", "")
LISTKEY = os.environ.get("CAMPAIGNS_LISTKEY", "")
CACHE_FILE = os.environ.get("CONTACTS_CACHE_FILE", "/data/contacts.json")

TOKEN_URLS = {
    "cn": "https://accounts.zoho.com.cn/oauth/v2/token",
    "com": "https://accounts.zoho.com/oauth/v2/token",
}
CAMPAIGNS_BASE = {
    "cn": "https://campaigns.zoho.com.cn/api/v1.1",
    "com": "https://campaigns.zoho.com/api/v1.1",
}
PAGE_SIZE = 250

_token_cache = {"value": "", "expires_at": 0}


def is_configured():
    return bool(CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN and LISTKEY)


def _refresh_token():
    """Exchange the refresh token for an access token."""
    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }).encode("utf-8")
    req = urllib.request.Request(TOKEN_URLS[ZOHO_REGION], data=data, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if "access_token" not in body:
        raise RuntimeError(f"zoho token refresh failed: {body}")
    _token_cache["value"] = body["access_token"]
    _token_cache["expires_at"] = time.time() + int(body.get("expires_in", 3600)) - 60
    return _token_cache["value"]


def _access_token():
    if _token_cache["value"] and _token_cache["expires_at"] > time.time() + 30:
        return _token_cache["value"]
    return _refresh_token()


def _call_getlistsubscribers(token, fromindex):
    params = {
        "resfmt": "JSON",
        "listkey": LISTKEY,
        "status": "active",
        "fromindex": fromindex,
        "range": PAGE_SIZE,
    }
    url = CAMPAIGNS_BASE[ZOHO_REGION] + "/getlistsubscribers?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": "Zoho-oauthtoken " + token,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_contacts():
    """Return list of active contacts: [{"email","first_name","last_name"}].

    Emails are lower-cased and de-duplicated (first occurrence wins).
    """
    if not is_configured():
        raise RuntimeError("zoho contacts sync not configured (missing ZOHO_* / CAMPAIGNS_LISTKEY)")
    token = _access_token()
    contacts = []
    seen = set()
    fromindex = 1
    while True:
        data = _call_getlistsubscribers(token, fromindex)
        if str(data.get("code", "")) != "0":
            raise RuntimeError(f"getlistsubscribers failed: {data.get('message', data)}")
        details = data.get("list_of_details") or []
        for c in details:
            if not isinstance(c, dict):
                continue
            email = (c.get("contact_email") or "").strip().lower()
            if not email or email in seen:
                continue
            seen.add(email)
            contacts.append({
                "email": email,
                "first_name": c.get("firstname") or "",
                "last_name": c.get("lastname") or "",
            })
        if len(details) < PAGE_SIZE:
            break
        fromindex += PAGE_SIZE
    return contacts


def sync_contacts():
    """Fetch CAM-03 active contacts and write the cache file atomically."""
    contacts = fetch_contacts()
    payload = {
        "updated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "count": len(contacts),
        "contacts": contacts,
    }
    d = os.path.dirname(CACHE_FILE)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.replace(tmp, CACHE_FILE)
    return payload


def seconds_until_next_midnight(tz_name):
    """Seconds until the next local 00:00 in the given IANA timezone."""
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1.0, (nxt - now).total_seconds())
