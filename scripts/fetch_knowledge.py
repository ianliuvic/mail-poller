#!/usr/bin/env python3
"""Fetch curated Wear Hongxiu WordPress pages/posts into knowledge/ as Markdown.

The knowledge/ pack is the business context the mail-poller will use for writing
automatic replies. Refresh after WP content changes:

    python scripts/fetch_knowledge.py

Credentials (env vars, or fall back to ../codex-wp-rest-connector/.env for local dev):
    WORDPRESS_BASE_URL / WORDPRESS_USERNAME / WORDPRESS_APPLICATION_PASSWORD
Read-only: only GETs the WordPress REST API; never writes to WordPress.
"""

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from html.parser import HTMLParser

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
OUT_DIR = os.path.join(BASE_DIR, "knowledge")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_BLOCK_TAGS = {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
               "blockquote", "ul", "ol", "table", "br", "figure", "figcaption"}

# (wp_type, wp_id, category, slug) — curated knowledge set for auto-reply context
KNOWLEDGE = [
    # --- pages (operational, authoritative) ---
    ("page", 8492, "oem-private-label", "oem-swimwear-manufacturing"),
    ("page", 8485, "oem-private-label", "private-label-swimwear"),
    ("page", 8478, "sales-channels", "swimwear-dropshipping"),
    ("page", 5863, "sales-channels", "wholesale-swimwear"),
    ("page", 8499, "sampling", "swimwear-sampling"),
    ("page", 8506, "fabric-trims", "fabrics-trims-sourcing"),
    ("page", 8549, "fabric-trims", "fabric-guide"),
    ("page", 8553, "guides", "color-guide"),
    ("page", 8564, "guides", "size-guide"),
    ("page", 8565, "quality-production", "quality-control"),
    ("page", 8566, "logistics-payment", "logistics"),
    ("page", 8567, "logistics-payment", "payment-methods"),
    # --- posts (business knowledge) ---
    ("post", 4170, "pricing-moq", "swimwear-pricing-guide"),
    ("post", 4911, "pricing-moq", "balancing-moq-and-budget"),
    ("post", 4519, "pricing-moq", "private-label-vs-custom"),
    ("post", 5727, "pricing-moq", "beyond-the-quote-hidden-costs"),
    ("post", 9904, "pricing-moq", "premium-swimwear-costs-workmanship"),
    ("post", 2853, "sampling", "swimwear-sample-approval"),
    ("post", 5918, "sampling", "hidden-cost-ai-sampling"),
    ("post", 3792, "fabric-trims", "swimwear-fabric-selection"),
    ("post", 3837, "fabric-trims", "swimsuit-metal-trims-guide"),
    ("post", 4445, "quality-production", "swimwear-tech-pack-guide"),
    ("post", 1268, "quality-production", "swimwear-manufacturing-process"),
    ("post", 1257, "logistics-payment", "cny-production-planning"),
]


class _Markdownify(HTMLParser):
    """Crude HTML -> Markdown-ish text (headings, lists, tables, emphasis)."""

    def __init__(self):
        super().__init__()
        self.out = []
        self.skip = 0
        self.in_li = False
        self.in_pre = False
        self.in_cell = False

    def _newline(self):
        if self.out and not self.out[-1].endswith("\n"):
            self.out.append("\n")

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head", "noscript", "iframe"):
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "pre":
            self.in_pre = True
        if tag == "li":
            self.in_li = True
            self._newline()
            self.out.append("- ")
        elif tag in ("td", "th"):
            self.in_cell = True
            self.out.append(" | " if self.out and not str(self.out[-1]).endswith(("|", "\n")) else "")
        elif tag in ("tr",):
            self._newline()
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._newline()
            self.out.append("#" * int(tag[1]) + " ")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag in ("a",):
            pass
        elif tag in ("img",):
            alt = dict(attrs).get("alt", "") if attrs else ""
            self.out.append(f"[图: {alt}]" if alt else "[图]")
        elif tag in _BLOCK_TAGS:
            self._newline()

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "noscript", "iframe"):
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "pre":
            self.in_pre = False
        if tag == "li":
            self.in_li = False
            self._newline()
        elif tag in ("td", "th"):
            self.in_cell = False
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "blockquote"):
            self._newline()
        elif tag in ("table", "ul", "ol", "figure"):
            self._newline()

    def handle_data(self, data):
        if self.skip:
            return
        text = data.replace("\xa0", " ")
        if self.in_pre:
            self.out.append(text)
        else:
            self.out.append(re.sub(r"\s+", " ", text))


def html_to_markdown(html):
    parser = _Markdownify()
    try:
        parser.feed(html or "")
    except Exception:
        pass
    text = "".join(parser.out)
    text = re.sub(r" +\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def load_credentials():
    keys = ["WORDPRESS_BASE_URL", "WORDPRESS_USERNAME", "WORDPRESS_APPLICATION_PASSWORD"]

    def parse(path):
        out = {}
        if os.path.exists(path):
            for line in open(path, encoding="utf-8-sig"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    k = k.strip()
                    if k in keys:
                        out[k] = v.strip().strip('"').strip("'")
        return out

    creds = {k: os.environ[k] for k in keys if os.environ.get(k)}
    if not creds.get("WORDPRESS_BASE_URL"):
        candidates = [
            os.environ.get("WORDPRESS_ENV_FILE", ""),
            os.path.join(BASE_DIR, "..", "codex-wp-rest-connector", ".env"),
            r"E:\cc\wearhongxiu\wordpress\codex-wp-rest-connector\.env",
        ]
        for path in candidates:
            if path:
                creds.update(parse(path))
                if creds.get("WORDPRESS_BASE_URL") and creds.get("WORDPRESS_APPLICATION_PASSWORD"):
                    break
    if not (creds.get("WORDPRESS_BASE_URL") and creds.get("WORDPRESS_APPLICATION_PASSWORD")):
        sys.exit("Missing WP credentials (env, WORDPRESS_ENV_FILE, or ../codex-wp-rest-connector/.env)")
    return creds


def wp_get(base, auth, path):
    req = urllib.request.Request(base + path, headers={
        "Authorization": auth, "Accept": "application/json", "User-Agent": UA,
    })
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def main():
    creds = load_credentials()
    base = creds["WORDPRESS_BASE_URL"].rstrip("/")
    auth = "Basic " + base64.b64encode(
        f"{creds['WORDPRESS_USERNAME']}:{creds['WORDPRESS_APPLICATION_PASSWORD']}".encode()
    ).decode()
    os.makedirs(OUT_DIR, exist_ok=True)

    index_rows = []
    for index, (wp_type, wp_id, category, slug) in enumerate(KNOWLEDGE, start=1):
        filename = f"{index:02d}-{slug}.md"
        try:
            item = wp_get(base, auth, f"/wp-json/wp/v2/{wp_type}s/{wp_id}?_fields=id,title,link,modified,content")
        except urllib.error.HTTPError as exc:
            print(f"FAIL {wp_id} {wp_type} ({exc.code})")
            continue
        title = re.sub(r"<[^>]+>", "", item.get("title", {}).get("rendered", "") or "").strip()
        link = item.get("link", "")
        modified = item.get("modified", "")
        body = html_to_markdown(item.get("content", {}).get("rendered", ""))
        if not body:
            print(f"EMPTY {wp_id} {wp_type} {slug}")
            continue

        header = (
            f"# {title}\n\n"
            f"> 来源：Wear Hongxiu WordPress（{wp_type}，wp_id={wp_id}）\n"
            f"> URL：{link}\n"
            f"> 修改时间：{modified}\n"
            f"> 分类：{category}\n\n"
            "---\n\n"
        )
        with open(os.path.join(OUT_DIR, filename), "w", encoding="utf-8") as fh:
            fh.write(header + body + "\n")
        index_rows.append((filename, title, category, wp_id, wp_type, modified[:10]))
        print(f"OK {filename} | {title[:50]}")

    # INDEX.md (not README.md — .dockerignore excludes README.md anywhere)
    lines = ["# Mail-Poller 知识库（自动回复上下文）\n",
             "从 Wear Hongxiu WordPress 拉取的精选业务知识，用于撰写自动回复。",
             "来源全部为 WordPress REST（只读），不依赖 RAG。刷新：`python scripts/fetch_knowledge.py`。\n",
             "## 声音与规则（自动回复风格）\n",
             "| 文件 | 说明 |",
             "|---|---|",
             "| voice.md | Ian 的声音画像（语气/称呼/落款/句式/数字表达），28 封真实回复提取 |",
             "| reply-rules.md | 回复必做动作（必问/必给/CTA/禁忌），与 voice.md 配合 |",
             "| reply-samples/rfq-detailed-reply.md | 范文：RFQ 详细回复 |",
             "| reply-samples/moq-pricing-reply.md | 范文：MOQ/报价回复 |",
             "| reply-samples/sample-order-reply.md | 范文：样品订单回复 |\n",
             "## 业务知识（WordPress 精选）\n",
             "| 文件 | 标题 | 分类 | wp_id | 类型 | 更新 |",
             "|---|---|---|---|---|---|"]
    for row in index_rows:
        lines.append(f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]} | {row[5]} |")
    with open(os.path.join(OUT_DIR, "INDEX.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\nDONE: {len(index_rows)} files -> {OUT_DIR}")


if __name__ == "__main__":
    main()
