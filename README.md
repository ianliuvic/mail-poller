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
| `KNOWLEDGE_DIR` | 自动回复知识库目录，默认 `/data/knowledge`；生产环境使用 Coolify 持久化存储 |
| `HONGXIU_RAG_URL` | Hongxiu RAG MCP 地址，默认 `https://rag-mcp.yiswim.cloud/mcp` |
| `HONGXIU_RAG_TOKEN` | Hongxiu RAG MCP bearer token（必填） |
| `HONGXIU_RAG_TOP_K` | 自动回复检索的来源数量，默认 8 |
| `HONGXIU_RAG_TIMEOUT_SECONDS` | RAG 查询超时，默认 90 秒 |

## 说明

- 每个邮箱用 IMAP UID 去重，只推新邮件；每个文件夹独立记录 UID。
- 文件夹名用 IMAP 名称，非 ASCII 的可能是 modified UTF-7 编码（如中文「垃圾邮件」= `&V4NXPpCuTvY-`、中文「已发送」= `&XfJT0ZABkK5O9g-`）。
- `sent_folder`（已发送）只做索引、不推飞书：首启全量回填建立 Message-ID + 收件人集合，之后增量追加。
- 提供 `/` 健康检查端点（返回 `ok`），供 Coolify 健康检查。
- 邮箱密码用「应用专用密码 / 授权码」，不是登录密码。
- 启动时会立即拉一次 CAM-03 联系人并缓存到 `CONTACTS_CACHE_FILE`，之后每天 00:00（`CONTACTS_SYNC_TZ` 时区）刷新一次。

## 每周邮件报告

- **实时记账**：每处理一封邮件（含标签/文件夹/是否推送/摘要）和每次按钮操作都会追加到 `MAIL_STATS_FILE`（默认 `/data/mail_stats.jsonl`）。
- **每周日 08:00（Asia/Shanghai）**生成周报：总览（收件/推送/过滤/垃圾捞回）、询盘话题、Top 发件人、按钮操作统计 + DeepSeek AI 洞察，以飞书卡片推送，明细归档到 `REPORTS_DIR`（默认 `/data/reports/`）。
- 从启用之日起积累数据；此前邮件无历史统计。

## 知识库（自动回复上下文）

- MOQ、价格、面料、打样、交期、物流和付款等业务事实，在点击自动回复时实时查询 Coolify 上的 Hongxiu RAG。
- Coolify 持久化存储 `/data/knowledge/` 只保留 `voice.md`、`reply-rules.md` 和 `reply-samples/`，用于语气、硬规则与结构参考。
- RAG 查询失败或没有可靠答案时停止生成草稿并通知飞书，不允许模型自行补全业务事实。

## 处理流程（每封新邮件）

1. 发件人在 CAM-03 → 放行（标签「客户」）。
2. 否则判断是否「回复我」：有 `In-Reply-To`/`References`/`Re:` 且（引用了我发出的 Message-ID，或发件人在我的发件收件人集合里）→ 放行（标签「回复」）。转发不算回复。
3. 否则交给 LLM 判定：只有具有明确买方/客户意图的 `useful`（泳装相关）才放行；`neutral`（含义不明、没有具体诉求）和 `unrelated`（广告、陌生服务商或供应商开发信、通知、新闻、垃圾）均阻挡。
4. 放行的邮件用 LLM 生成一句中文摘要，推送到飞书（卡片消息）。**所有卡片**带「📄 查看原文」按钮（从 `/data/mail_cache.json` 取正文发给飞书）；**只有「泳装相关」的陌生询盘**额外带「➕ 加入 CAM-03」按钮。

## 飞书事件回调

- 端点：`POST /feishu/callback`（也兼容 `GET /feishu/callback?challenge=...`）。
- 飞书要求卡片回调 **3 秒内响应**：耗时的动作（如 `add_contact` 调 Zoho）在后台线程执行，先秒回「正在处理」，结果以一条飞书消息（✅/❌）送达。
- 用途：接收飞书卡片按钮点击（`card.action.trigger`），`value` 里带 `{action, email, name, from, subject}`。
- 按钮动作通过 `ACTION_HANDLERS` 注册表分发（`@register_action("xxx")`），便于扩展；当前内置：
  - `view_original`：把缓存的邮件正文（`/data/mail_cache.json`，截断 6000 字）作为消息发到飞书；
  - `add_contact`：把邮箱 + LLM 提取的姓名加入 CAM-03，并刷新本地联系人缓存；
  - `draft_reply`：陌生询盘卡片上的「✍️ 自动回复」——后台用 Hongxiu RAG 业务事实 + 持久化目录中的 `voice.md`（声音）、`reply-rules.md`（规则）和回复样例生成草稿推给你审核，**不自动发送**；
  - `guided_reply`：询盘卡片底部「指导意见」多行输入框（卡片 JSON 2.0 textarea）——你输入观点后点「生成指导型回复」，LLM 结合你的指导意见生成草稿推给你，同样不发送；
  - `save_draft`：草稿卡片上的「📥 存入 Zoho 草稿」——通过 **Zoho Mail 官方「保存草稿」API**（`mode=draft` + `inReplyTo`/`refHeader`）存为**原邮件的回复草稿**（Zoho 保证串线），不发送。
- 在飞书开放平台给应用配置「事件订阅」时，把请求地址填为 `https://<域名>/feishu/callback`，并填写 `FEISHU_VERIFICATION_TOKEN` 环境变量。
