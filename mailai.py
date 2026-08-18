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


# ---------- LLM classification (usefulness + summary) ----------

_SWIMWEAR_PROMPT = """你是一家中国泳装公司的邮件筛选助手。公司做泳装/比基尼/沙滩装的外贸生意（B2B 批发、OEM/ODM 贴牌、电商）。

请对邮件做两件事：
1. 判断这封邮件对公司是否值得关注（verdict）。
2. 用一句话中文总结邮件要点（summary，40 字内）。

严格输出 JSON（不要输出任何其他文字）：
{"verdict": "useful | neutral | unrelated", "summary": "一句话中文摘要"}

判定标准：
- useful（泳装相关，有价值）：询价、报价、下单、付款、样品/寄样、OEM/ODM/贴牌合作、面料/辅料/包装/物流等供应链合作、客户问题/投诉、合作意向、展会/行业活动等与泳装生意相关的内容。
- unrelated（绝对无关，阻挡）：SEO 优化、代运营、店铺推广、建站/软件/SaaS、招聘、培训、新闻资讯、订阅/Newsletter、营销广告、系统通知（账单/登录/服务器）、垃圾/钓鱼/诈骗等与泳装业务完全无关的内容。
- neutral（中性，放行）：信息不完整、含义不明、无法判断是否与泳装相关。存疑时优先判 neutral。

原则：只要可能和泳装生意沾边，就判 useful 或 neutral；只有「绝对无关」才判 unrelated。宁可多放行，不要漏掉生意。"""


def classify_useful(from_, subject, body, is_contact=False, is_reply=False):
    """Return (verdict, summary, error). verdict in {'useful','neutral','unrelated'}."""
    if not DEEPSEEK_API_KEY:
        return None, "", "no deepseek key"
    body = (body or "").strip()[:MAX_BODY_CHARS]
    context = []
    if is_contact:
        context.append("发件人在公司联系人列表(CAM-03)中")
    if is_reply:
        context.append("这是对方回复我方的邮件")
    user_msg = (
        f"发件人: {from_}\n"
        f"主题: {subject}\n"
        f"背景: {'；'.join(context) if context else '陌生人来信'}\n"
        f"正文(已预剥引用):\n{body if body else '(空)'}"
    )
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": _SWIMWEAR_PROMPT},
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
        return None, "", f"llm error: {e}"
    try:
        obj = json.loads(content)
    except Exception:
        m = re.search(r"\{.*\}", content, re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
            except Exception:
                return None, "", f"json parse error: {content[:200]}"
        else:
            return None, "", f"json parse error: {content[:200]}"
    verdict = (obj.get("verdict") or "neutral").strip().lower()
    if verdict not in ("useful", "neutral", "unrelated"):
        verdict = "neutral"
    summary = (obj.get("summary") or "").strip()
    return verdict, summary, None
