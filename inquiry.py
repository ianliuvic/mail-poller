"""Stranger-inquiry screening and delivery to the dedicated Feishu group.

Only a *new* stranger is screened (the first email received from an address), and
only a confident, concrete swimwear buyer inquiry is forwarded. Vendor pitches,
generic "let's cooperate" templates and phishing-style leads are rejected by the
LLM and never reach the group.

Delivery uses a Feishu group custom-bot webhook (no app token needed, works in an
external group). The webhook is read-only by design: cards carry no buttons.
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import mailai

WEBHOOK_URL = os.environ.get("INQUIRY_WEBHOOK_URL", "").strip()
STATE_FILE = os.environ.get("INQUIRY_STATE_FILE", "/data/inquiry_seen.json")
MIN_CONFIDENCE = float(os.environ.get("INQUIRY_MIN_CONFIDENCE", "0.65"))
MAX_TEXT = int(os.environ.get("INQUIRY_MAX_TEXT_CHARS", "4000"))
# Website form notifications (and other self-sent mail) share one From address.
# They still go through screening, but must not consume a single "first contact"
# slot that would swallow every later form submission.
REPEAT_SENDERS = {
    item.strip().lower()
    for item in os.environ.get(
        "INQUIRY_REPEAT_SENDERS",
        "ian@wearhongxiu.com,service@wearhongxiu.com,wordpress@wearhongxiu.com",
    ).split(",")
    if item.strip()
}
BEIJING = timezone(timedelta(hours=8))

_lock = threading.Lock()


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "[inquiry]", *a, flush=True)


def is_configured():
    return bool(WEBHOOK_URL)


# ---------- first-contact state ----------

def _load():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    data.setdefault("senders", {})
    return data


def _save(data):
    directory = os.path.dirname(STATE_FILE) or "."
    try:
        os.makedirs(directory, exist_ok=True)
    except Exception:
        pass
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def already_screened(email):
    if not email:
        return False
    with _lock:
        return email.lower() in _load()["senders"]


def _remember(email, verdict, confidence, reason, subject):
    if not email:
        return
    with _lock:
        data = _load()
        data["senders"][email.lower()] = {
            "first_seen": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S"),
            "is_inquiry": bool(verdict),
            "confidence": confidence,
            "reason": reason,
            "subject": (subject or "")[:200],
        }
        _save(data)


# ---------- LLM screening ----------

_SYSTEM = """你是红绣泳装（中国泳装/瑜伽服工厂）的询盘审核员。
任务：判断一封陌生来信是否是【真实的泳装采购询盘】——对方是买家，想向我们采购或委托生产泳装类产品。

判 true 必须同时满足：
1. 明确涉及泳装/泳衣/比基尼/沙滩装/瑜伽服/运动内衣等我们生产的产品；
2. 明确表达买方意图：询价、要报价、要目录、打样、下单、OEM/ODM、贴牌、批发采购；
3. 有具体诉求或可执行下一步（数量、款式、目标价、交期、MOQ 至少一项，或明确要求我们报价/发目录）。

以下一律判 false：
- 真正的钓鱼/诈骗：索要银行账户/证件/验证码；承诺大单但先要样品费/运费/手续费/保证金；要求超额付款后退差价；临时更换收款账户；要求点击外部链接填写付款或身份信息；冒充采购却始终不谈产品细节，也不给任何可核实的公司或物流信息。
- 推销：货代、面料辅料、包装、代运营、建站、SEO、广告、网红、招聘、投资、贷款、展会；
- 只寒暄、只问“你们是工厂吗/在吗”、正文为空或语义不明；
- 群发广告、通知、newsletter、自动回复、退订；
- 只泛泛说“想合作/代理/分销”，没有任何泳装细节或采购诉求；
- 前后矛盾、明显模板化且无法核实身份的“大额询盘”。

以下情况是正常 B2B 行为，不能据此判 false：
- 提供 WhatsApp/电话作为联系方式，或邀请我们加 WhatsApp（只要没有要求把付款、合同、验货转到站外）；
- 买家自备中国货代、指定深圳/广州仓库，并采用 EXW；
- 使用 Gmail 等通用邮箱；
- 索要目录、阿里/中国制造网店铺链接、首单交期和付款条件；
- 先试单、再按供应商表现放量。

判断依据只看邮件本身，不要因为出现 Re:、urgent、big order、partnership 等词就放行。
不确定时一律判 false（宁缺勿滥）。

严格输出 JSON，不要输出其他文字：
{"is_inquiry": true|false, "confidence": 0到1的小数, "reason": "≤30字中文理由", "summary": "≤40字中文摘要", "translation": "原文的完整中文翻译"}
若 is_inquiry=false：summary 与 translation 均返回空字符串；原文已是中文时 translation 也返回空字符串。"""


def judge(from_, subject, body):
    """Return (result_dict, error). result has is_inquiry/confidence/reason/summary/translation."""
    if not mailai.DEEPSEEK_API_KEY:
        return None, "no deepseek key"
    body = (body or "").strip()[: mailai.MAX_BODY_CHARS]
    user_msg = f"发件人: {from_}\n主题: {subject}\n正文:\n{body if body else '(空)'}"
    payload = {
        "model": mailai.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    url = mailai.DEEPSEEK_BASE_URL.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + mailai.DEEPSEEK_API_KEY,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        return None, f"llm error: {exc}"

    try:
        obj = json.loads(content)
    except Exception:
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            return None, f"json parse error: {content[:200]}"
        try:
            obj = json.loads(match.group(0))
        except Exception:
            return None, f"json parse error: {content[:200]}"

    try:
        confidence = float(obj.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "is_inquiry": bool(obj.get("is_inquiry")),
        "confidence": max(0.0, min(1.0, confidence)),
        "reason": str(obj.get("reason") or "").strip()[:120],
        "summary": str(obj.get("summary") or "").strip()[:200],
        "translation": str(obj.get("translation") or "").strip()[:MAX_TEXT],
    }, None


# ---------- card ----------

def _md(text, limit=200):
    """Flatten a user-controlled value so it cannot inject card markdown tags."""
    text = " ".join((text or "").split())
    return text.replace("<", "＜").replace(">", "＞")[:limit]


def _panel(title, text):
    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "header": {
            "title": {"tag": "markdown", "content": title},
            "vertical_align": "center",
            "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined", "size": "16px 16px"},
            "icon_position": "right",
            "icon_expanded_angle": -180,
        },
        "border": {"color": "grey", "corner_radius": "5px"},
        "padding": "8px 8px 8px 8px",
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "plain_text",
                    "content": (text or "（空）").replace("<", "＜").replace(">", "＞"),
                },
            }
        ],
    }


def _meta_lines(mailbox, from_, email, subject, date, channel):
    sender = from_ or email or ""
    return (
        f"**来源**　{_md(channel, 20)} · {_md(mailbox, 60)}\n"
        f"**时间**　{_md(date or datetime.now(BEIJING).strftime('%Y-%m-%d %H:%M'), 40)}\n"
        f"**发件人**　{_md(sender, 100)}\n"
        f"**主题**　{_md(subject, 140) or '（无主题）'}"
    )


def short_text(text):
    """Plain text is rendered by Feishu as-is; only strip card-tag characters."""
    return (text or "").replace("<", "＜").replace(">", "＞")


def build_card(*, mailbox, from_, email, subject, date, summary, original, translation, inquiry_id, channel="邮件"):
    elements = [
        {"tag": "markdown", "content": f"**{_md(summary, 80) or '（无摘要）'}**"},
        {"tag": "markdown", "content": _meta_lines(mailbox, from_, email, subject, date, channel)},
        {"tag": "hr"},
        _panel("📄 原文", short_text(original)[:MAX_TEXT]),
    ]
    if translation and translation.strip() != (original or "").strip():
        elements.append(_panel("🌐 译文", short_text(translation)[:MAX_TEXT]))
    elements += [
        {"tag": "hr"},
        {"tag": "markdown", "content": f"<font color='grey'>{_md(inquiry_id, 40)}</font>"},
    ]
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": False, "update_multi": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": f"🆕 新询盘 · {channel}"}},
        "body": {"direction": "vertical", "elements": elements},
    }


def build_flat_card(*, mailbox, from_, email, subject, date, summary, original, translation, inquiry_id, channel="邮件"):
    """Fallback without collapsible panels, for webhook schema quirks."""
    elements = [
        {"tag": "markdown", "content": f"**{_md(summary, 80) or '（无摘要）'}**"},
        {"tag": "markdown", "content": _meta_lines(mailbox, from_, email, subject, date, channel)},
        {"tag": "hr"},
        {"tag": "markdown", "content": "**📄 原文**"},
        {"tag": "div", "text": {"tag": "plain_text", "content": short_text(original)[:MAX_TEXT] or "（空）"}},
    ]
    if translation and translation.strip() != (original or "").strip():
        elements += [
            {"tag": "markdown", "content": "**🌐 译文**"},
            {"tag": "div", "text": {"tag": "plain_text", "content": short_text(translation)[:MAX_TEXT]}},
        ]
    elements += [
        {"tag": "hr"},
        {"tag": "markdown", "content": f"<font color='grey'>{_md(inquiry_id, 40)}</font>"},
    ]
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": False, "update_multi": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": f"🆕 新询盘 · {channel}"}},
        "body": {"direction": "vertical", "elements": elements},
    }


def _post(card, fallback=None):
    payload = json.dumps({"msg_type": "interactive", "card": card}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        log("webhook send failed:", exc)
        return False
    if data.get("code") not in (0, None):
        log("webhook rejected:", data.get("code"), data.get("msg"))
        if fallback is not None:
            log("retrying with the flat card layout")
            return _post(fallback)
        return False
    return True


# ---------- entry point ----------

def notify_if_new_inquiry(*, mailbox, folder, from_, email, subject, body, date):
    """Screen a stranger's first email and post it to the group.

    Returns True only when a card was delivered, so the caller can skip the
    one-to-one Feishu notification for that email.
    """
    if not is_configured() or not email:
        return False
    email = email.lower()
    if email not in REPEAT_SENDERS and already_screened(email):
        return False
    if email in REPEAT_SENDERS:
        log(f"repeat sender {email}: screening without first-contact dedupe")

    clean = mailai.strip_quotes(body or "")
    result, err = judge(from_, subject, clean)
    if err or result is None:
        log(f"screening failed for {email}: {err}")
        return False

    _remember(email, result["is_inquiry"], result["confidence"], result["reason"], subject)
    if not result["is_inquiry"] or result["confidence"] < MIN_CONFIDENCE:
        log(f"not an inquiry: {email} confidence={result['confidence']:.2f} reason={result['reason']}")
        return False

    inquiry_id = "INQ-" + datetime.now(BEIJING).strftime("%Y%m%d-%H%M%S")
    fields = dict(
        mailbox=mailbox,
        from_=from_,
        email=email,
        subject=subject,
        date=date,
        summary=result["summary"],
        original=clean,
        translation=result["translation"],
        inquiry_id=inquiry_id,
    )
    card = build_card(**fields)
    if not _post(card, build_flat_card(**fields)):
        return False
    log(f"new inquiry forwarded: {email} confidence={result['confidence']:.2f} id={inquiry_id}")
    return True
