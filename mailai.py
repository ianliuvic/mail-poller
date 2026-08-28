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
DEEPSEEK_REPLY_MODEL = os.environ.get("DEEPSEEK_REPLY_MODEL", "deepseek-v4-pro")
MAX_BODY_CHARS = 3000

SIGNATURE = """Best regards,
Ian
Operations Manager at Hongxiu Clothing Co., Ltd.

T (+86) 177-1101-4152
W www.wearhongxiu.com
L www.linkedin.com/in/yin-liu-hongxiu/

Your trusted swimwear manufacturing partner in China."""


def normalize_signature(text):
    """Replace the model's closing block with the canonical company signature."""
    text = (text or "").strip()
    marker = re.search(r"(?im)^\s*Best regards,\s*$", text)
    if marker:
        text = text[:marker.start()].rstrip()
        return (text + "\n\n\n" if text else "") + SIGNATURE
    return text + "\n\n\n" + SIGNATURE if text else SIGNATURE


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


def extract_attachments(msg):
    """Return attachment metadata without retaining or sending attachment content."""
    attachments = []
    if not msg.is_multipart():
        return attachments
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = str(part.get("Content-Disposition") or "").lower()
        filename = decode_mime(part.get_filename() or "")
        if "attachment" not in disposition and not filename:
            continue
        payload = part.get_payload(decode=True) or b""
        attachments.append({
            "filename": filename or "（未命名）",
            "content_type": part.get_content_type() or "application/octet-stream",
            "size": len(payload),
        })
    return attachments


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

请对邮件做三件事：
1. 判断这封邮件对公司是否值得关注（verdict）。
2. 用一句话中文总结邮件要点（summary，40 字内）。
3. 提取发件人姓名（name）：优先从 From 头的显示名提取，否则从正文落款/签名里提取；无法确定就返回空字符串。

严格输出 JSON（不要输出任何其他文字）：
{"verdict": "useful | neutral | unrelated", "summary": "一句话中文摘要", "name": "发件人姓名或空字符串"}

判定标准（重点判断陌生来信的商业方向）：
- useful（推送）：发件人明显是潜在买家、品牌方、批发商或现有客户，并提出了需要我方处理的需求，例如泳装询价、报价、下单、付款、样品、OEM/ODM/贴牌、面料选择、生产进度、物流、售后、投诉。即使内容很短，只要能看出对方想采购或委托我方生产泳装，也可判 useful。
- unrelated（阻挡）：发件人想向我方销售产品或服务、开发我方成为其客户，或发送广告/群发推广。包括但不限于 SEO、广告投放、代运营、建站、软件/SaaS、AI 工具、招聘、培训、咨询、融资、贷款、支付服务、物流揽客、货代、面料辅料推销、包装推销、摄影设计、网红推广、展会招展、媒体投稿和链接交换。即使对方提到 swimwear、fashion、manufacturer、our business，也不能因此判 useful；判断关键是“对方想买我们的产品/服务”，还是“对方想卖东西给我们”。后者一律 unrelated。
- neutral（阻挡）：正文为空、只有问候/寒暄、语义破碎或意义不明、没有具体诉求、无法确认商业意图，或仅泛泛表示“合作”但看不出对方是买家。

特别规则：
1. 陌生人只有在存在明确或高度可信的买方意图时才判 useful。
2. 不要因为邮件使用 Re:、urgent、business proposal、partnership、collaboration 等词就放行。
3. 不确定时判 neutral；neutral 不会推送到飞书。
4. 已知联系人或对我方邮件的真实回复会由程序另行优先放行，不需要为了避免漏信而把陌生邮件判 useful。"""


def classify_useful(from_, subject, body, is_contact=False, is_reply=False):
    """Return (verdict, summary, name, error). verdict in {'useful','neutral','unrelated'}."""
    if not DEEPSEEK_API_KEY:
        return None, "", "", "no deepseek key"
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
        return None, "", "", f"llm error: {e}"
    try:
        obj = json.loads(content)
    except Exception:
        m = re.search(r"\{.*\}", content, re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
            except Exception:
                return None, "", "", f"json parse error: {content[:200]}"
        else:
            return None, "", "", f"json parse error: {content[:200]}"
    verdict = (obj.get("verdict") or "neutral").strip().lower()
    if verdict not in ("useful", "neutral", "unrelated"):
        verdict = "neutral"
    summary = (obj.get("summary") or "").strip()
    name = (obj.get("name") or "").strip()
    return verdict, summary, name, None


# ---------- reply draft generation ----------

def draft_reply(from_, subject, body, knowledge_text, sample_text, voice_text, rules_text, guide=""):
    """Generate a reply draft in Ian's voice. Returns (draft_text, error)."""
    if not DEEPSEEK_API_KEY:
        return None, "no deepseek key"
    system = (
        "You are drafting a business reply email for Ian, Operations Manager at Hongxiu "
        "Clothing Co., Ltd. (a Chinese swimwear and yogawear manufacturer).\n"
        "Write in the SAME language as the customer's email.\n"
        "Follow Ian's voice and reply rules strictly - they are authoritative:\n\n"
        f"{voice_text}\n\n{rules_text}\n\n"
        "IMPORTANT shipping rule: when the customer asks about shipping cost or delivery methods, "
        "you MUST mention all three options (do not omit air freight): international express "
        "(around 7 working days, usually the most expensive), air freight (around 10-14 working days), "
        "and sea freight (35+ working days). State that the exact price depends on destination, quantity, "
        "weight and package dimensions.\n\n"
        "Output ONLY the reply email body, from greeting to signature. No commentary, "
        "no markdown fences. Where an attachment or link would go, write a placeholder "
        "like [附：目录] / [附：价格表] for the human to fill in."
    )
    user_msg = (
        f"Customer email to reply to:\nFrom: {from_}\nSubject: {subject}\nBody:\n{(body or '')[:4000]}\n\n"
        f"Business knowledge to use (authoritative facts - MOQ, pricing, lead time, fabric etc.):\n{knowledge_text[:14000]}\n\n"
        f"Style reference (imitate tone and structure, do NOT copy its content):\n{sample_text[:2500]}"
    )
    if guide and guide.strip():
        user_msg += (
            f"\n\nIan's guidance for THIS reply (must be followed strictly, incorporate all of it):\n{guide.strip()[:1500]}"
        )
    payload = {
        "model": DEEPSEEK_REPLY_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.5,
    }
    url = DEEPSEEK_BASE_URL.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={
        "Authorization": "Bearer " + DEEPSEEK_API_KEY,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read().decode("utf-8"))
        draft = normalize_signature(d["choices"][0]["message"]["content"])
    except Exception as e:
        return None, f"llm error: {e}"
    return draft, None


def weekly_insights(records_text):
    """Summarize this week's email records into 3-5 business insights (Chinese)."""
    if not DEEPSEEK_API_KEY:
        return "（未配置 LLM，跳过洞察）"
    system = (
        "You are a business email analyst for a Chinese swimwear manufacturer. "
        "Given this week's email records (label | sender | subject | summary), produce 3-5 "
        "concise insights: business trends, customer behavior, recurring questions, and 1-2 "
        "actionable suggestions. Answer in Chinese. Output each insight as one line starting "
        "with '- '. Be specific and concrete; no fluff."
    )
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": records_text[:12000]}],
        "temperature": 0.4,
    }
    url = DEEPSEEK_BASE_URL.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={
        "Authorization": "Bearer " + DEEPSEEK_API_KEY,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read().decode("utf-8"))
        return d["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"（洞察生成失败：{e}）"
