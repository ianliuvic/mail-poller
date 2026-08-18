#!/usr/bin/env python3
"""Email AI: extract new content (quote stripping) + LLM classification.

Strategy (quality first):
  1. Regex pre-strip: remove obvious quoted lines / separator history / signature.
  2. LLM (DeepSeek flash): authoritative extraction of the "new content" plus
     classification, so messy quotes / non-English separators are still handled.
"""

import json
import os
import re
import urllib.request
from email.header import decode_header, make_header
from html.parser import HTMLParser

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
MAX_BODY_CHARS = 3000


# ---------- header decode ----------

def decode_mime(v):
    if v is None:
        return ""
    try:
        return str(make_header(decode_header(v)))
    except Exception:
        return str(v)


# ---------- HTML -> text (blockquote-aware) ----------

class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag == "blockquote":
            self.skip += 1
        elif tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "td", "th"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "blockquote" and self.skip > 0:
            self.skip -= 1
        elif tag in ("p", "div", "li", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skip == 0:
            self.parts.append(data)


def html_to_text(h):
    p = _TextExtractor()
    try:
        p.feed(h)
    except Exception:
        pass
    return "".join(p.parts)


# ---------- extract plain body from an email.message ----------

def extract_body(msg):
    """Return (body_text, used_html). Prefers text/plain, falls back to HTML->text."""
    if msg.is_multipart():
        text_parts = []
        html_parts = []
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                txt = payload.decode(charset, errors="replace")
            except Exception:
                txt = payload.decode("utf-8", errors="replace")
            if ctype == "text/plain":
                text_parts.append(txt)
            elif ctype == "text/html":
                html_parts.append(txt)
        if text_parts:
            return "\n".join(text_parts), False
        if html_parts:
            return html_to_text("\n".join(html_parts)), True
        return "", False
    ctype = msg.get_content_type()
    payload = msg.get_payload(decode=True)
    if payload:
        charset = msg.get_content_charset() or "utf-8"
        try:
            txt = payload.decode(charset, errors="replace")
        except Exception:
            txt = payload.decode("utf-8", errors="replace")
        if ctype == "text/html":
            return html_to_text(txt), True
        return txt, False
    return "", False


# ---------- regex quote stripping (first layer) ----------

_SEPARATORS = [
    r"-----*\s*(Original\s+Message|原始邮件|转发邮件|原邮件|Forwarded\s+Message|----------)",
    r"On\s+.{0,120}\s+wrote\s*:",
    r"On\s+.{0,120}\s+schrieb\s*:",
    r"发自我的\s+.{0,60}",
    r"Sent\s+from\s+my\s+.{0,60}",
    r"元のメール",
    r"이전\s*메시지",
]


def strip_quotes(text):
    """Remove quoted lines (> / |), separator history, and signature."""
    if not text:
        return ""
    lines = text.split("\n")
    out = []
    for line in lines:
        if re.match(r"^\s*[>|]\s?", line):
            continue
        if any(re.search(sep, line, re.I) for sep in _SEPARATORS):
            break
        out.append(line)
    result = "\n".join(out)
    m = re.search(r"\n\s*-{2,}\s*\n", result)
    if m:
        result = result[: m.start()]
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    return result


# ---------- LLM classification ----------

_SYSTEM_PROMPT = """你是一名邮件分析助手。给定一封邮件，请完成两件事：
1. 提取「发件人本次新写的内容」：忽略所有历史引用（以 > 开头的行、原文引用、"原始邮件 / Original Message / On ... wrote" 之后的内容、签名、免责声明）。只基于本次新增内容判断。
2. 分类并总结。

严格输出 JSON（不要输出任何其他文字），字段：
- category: order | inquiry | notification | newsletter | spam | other
- priority: high | medium | low
- summary: 一句话中文摘要（30字内）
- reply_needed: true | false

判定规则：
- 询价/报价/下单/付款/合作/客户问题/投诉 = inquiry 或 order，priority high
- 陌生人的业务询价 = inquiry，priority high（新线索，很重要）
- 系统通知(GitHub/Shopify/账单/登录/服务器提醒等) = notification，priority low
- 订阅/营销/推广/newsletter = newsletter，priority low
- 垃圾/钓鱼/诈骗 = spam，priority low
- 回复且来自联系人 = 优先 high
- 无法判断 = other，priority medium"""


def classify_email(from_, subject, body, known, is_reply):
    """Return (result_dict, error). result_dict is None on failure."""
    if not DEEPSEEK_API_KEY:
        return None, "no deepseek key"
    body = (body or "").strip()[:MAX_BODY_CHARS]
    user_msg = (
        f"发件人: {from_}\n"
        f"主题: {subject}\n"
        f"是否联系人: {'是' if known else '否'}\n"
        f"是否回复: {'是' if is_reply else '否'}\n"
        f"正文(已预剥引用):\n{body if body else '(空)'}"
    )
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    url = DEEPSEEK_BASE_URL.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={
        "Authorization": "Bearer " + DEEPSEEK_API_KEY,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            d = json.loads(r.read().decode("utf-8"))
        content = d["choices"][0]["message"]["content"]
    except Exception as e:
        return None, f"llm error: {e}"
    try:
        return json.loads(content), None
    except Exception:
        m = re.search(r"\{.*\}", content, re.S)
        if m:
            try:
                return json.loads(m.group(0)), None
            except Exception:
                pass
        return None, f"json parse error: {content[:200]}"


CATEGORY_LABEL = {
    "order": "订单",
    "inquiry": "询盘",
    "notification": "通知",
    "newsletter": "订阅",
    "spam": "垃圾",
    "other": "其他",
}


def decide(result, known, is_reply):
    """Return (should_notify, label, summary). label like '询盘·高'."""
    if result is None:
        return True, "未知", "（分类失败，按默认推送）"
    category = (result.get("category") or "other").lower()
    priority = (result.get("priority") or "medium").lower()
    summary = (result.get("summary") or "").strip()
    if category in ("spam", "newsletter"):
        return False, CATEGORY_LABEL.get(category, category), summary
    if priority in ("high", "medium"):
        label = CATEGORY_LABEL.get(category, category) + ("·高" if priority == "high" else "·中")
        return True, label, summary
    return False, CATEGORY_LABEL.get(category, category), summary
