# mail-poller

轮询 IMAP 邮箱，把新邮件通知推到飞书（Lark）。纯 Python 标准库，无第三方依赖。

## 环境变量

| 变量 | 说明 |
|---|---|
| `MAILBOXES_JSON` | 邮箱数组：`[{"name","host","port","user","password","folders":[...],"sent_folder":"..."}]`；`folders` 是要轮询的收件文件夹列表（IMAP 名称），`sent_folder` 是要索引的已发送文件夹 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书自建应用凭据 |
| `FEISHU_RECEIVE_ID` | 通知目标（chat_id / open_id / user_id / email） |
| `FEISHU_RECEIVE_ID_TYPE` | 默认 `chat_id` |
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

## 处理流程（每封新邮件）

1. 发件人在 CAM-03 → 放行（标签「客户」）。
2. 否则判断是否「回复我」：有 `In-Reply-To`/`References`/`Re:` 且（引用了我发出的 Message-ID，或发件人在我的发件收件人集合里）→ 放行（标签「回复」）。转发不算回复。
3. 否则交给 LLM 判定：`useful`（泳装相关）→ 放行；`neutral`（中性）→ 放行；`unrelated`（绝对无关：SEO/推广/通知/新闻/垃圾）→ 阻挡。
4. 放行的邮件用 LLM 生成一句中文摘要，推送到飞书。
