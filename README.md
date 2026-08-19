# mail-poller

轮询 IMAP 邮箱，把新邮件通知推到飞书（Lark）。纯 Python 标准库，无第三方依赖。

## 环境变量

| 变量 | 说明 |
|---|---|
| `MAILBOXES_JSON` | 邮箱数组：`[{"name","host","port","user","password","folders":[...],"sent_folder":"..."}]`；`folders` 是要轮询的收件文件夹列表（IMAP 名称），`sent_folder` 是要索引的已发送文件夹 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书自建应用凭据 |
| `FEISHU_RECEIVE_ID` | 通知目标（chat_id / open_id / user_id / email） |
| `FEISHU_RECEIVE_ID_TYPE` | 默认 `chat_id` |
| `FEISHU_VERIFICATION_TOKEN` | 飞书事件订阅的 Verification Token（用于校验回调，可留空先不校验） |
| `POLL_INTERVAL_SECONDS` | 轮询间隔，默认 300 |
| `STATE_FILE` | 状态文件（记录每个邮箱的 last_uid），默认 `/data/last_uid.json` |
| `SENT_SET_FILE` | 已发送邮件索引（Message-ID + 收件人），默认 `/data/sent_set.json` |
| `PORT` | 健康检查 HTTP 端口，默认 8000 |
| `ZOHO_REGION` | `cn`（中国 DC）或 `com`，默认 `cn` |
| `ZOHO_CLIENT_ID` / `ZOHO_CLIENT_SECRET` / `ZOHO_REFRESH_TOKEN` | Zoho Campaigns OAuth2 Self Client 凭据（用于拉取联系人） |
| `CAMPAIGNS_LISTKEY` | 要缓存的邮寄列表 listkey（如 CAM-03） |
| `CONTACTS_CACHE_FILE` | 联系人缓存文件，默认 `/data/contacts.json` |
| `CONTACTS_SYNC_TZ` | 每日同步的时区，默认 `Asia/Shanghai` |

## 说明

- 每个邮箱用 IMAP UID 去重，只推新邮件；每个文件夹独立记录 UID。
- 文件夹名用 IMAP 名称，非 ASCII 的可能是 modified UTF-7 编码（如中文「垃圾邮件」= `&V4NXPpCuTvY-`、中文「已发送」= `&XfJT0ZABkK5O9g-`）。
- `sent_folder`（已发送）只做索引、不推飞书：首启全量回填建立 Message-ID + 收件人集合，之后增量追加。
- 提供 `/` 健康检查端点（返回 `ok`），供 Coolify 健康检查。
- 邮箱密码用「应用专用密码 / 授权码」，不是登录密码。
- 启动时会立即拉一次 CAM-03 联系人并缓存到 `CONTACTS_CACHE_FILE`，之后每天 00:00（`CONTACTS_SYNC_TZ` 时区）刷新一次。

## 知识库（自动回复上下文）

- `knowledge/` 是从 Wear Hongxiu WordPress 拉取的**精选业务知识**（OEM/私标、面料、打样、MOQ/报价、物流付款、质检、尺码等 24 篇，纯 Markdown），用于后续自动回复撰写，不依赖 RAG。
- 刷新：`python scripts/fetch_knowledge.py`（只读 WP REST；凭据用 `WORDPRESS_BASE_URL`/`WORDPRESS_USERNAME`/`WORDPRESS_APPLICATION_PASSWORD` 环境变量，或 `WORDPRESS_ENV_FILE` 指向 .env）。
- 文件清单见 `knowledge/INDEX.md`；容器内路径 `/app/knowledge/`。

## 处理流程（每封新邮件）

1. 发件人在 CAM-03 → 放行（标签「客户」）。
2. 否则判断是否「回复我」：有 `In-Reply-To`/`References`/`Re:` 且（引用了我发出的 Message-ID，或发件人在我的发件收件人集合里）→ 放行（标签「回复」）。转发不算回复。
3. 否则交给 LLM 判定：`useful`（泳装相关）→ 放行；`neutral`（中性）→ 放行；`unrelated`（绝对无关：SEO/推广/通知/新闻/垃圾）→ 阻挡。
4. 放行的邮件用 LLM 生成一句中文摘要，推送到飞书（卡片消息）。**所有卡片**带「📄 查看原文」按钮（从 `/data/mail_cache.json` 取正文发给飞书）；**只有「泳装相关」的陌生询盘**额外带「➕ 加入 CAM-03」按钮。

## 飞书事件回调

- 端点：`POST /feishu/callback`（也兼容 `GET /feishu/callback?challenge=...`）。
- 飞书要求卡片回调 **3 秒内响应**：耗时的动作（如 `add_contact` 调 Zoho）在后台线程执行，先秒回「正在处理」，结果以一条飞书消息（✅/❌）送达。
- 用途：接收飞书卡片按钮点击（`card.action.trigger`），`value` 里带 `{action, email, name, from, subject}`。
- 按钮动作通过 `ACTION_HANDLERS` 注册表分发（`@register_action("xxx")`），便于扩展；当前内置：
  - `view_original`：把缓存的邮件正文（`/data/mail_cache.json`，截断 6000 字）作为消息发到飞书；
  - `add_contact`：把邮箱 + LLM 提取的姓名加入 CAM-03，并刷新本地联系人缓存。
- 在飞书开放平台给应用配置「事件订阅」时，把请求地址填为 `https://<域名>/feishu/callback`，并填写 `FEISHU_VERIFICATION_TOKEN` 环境变量。
