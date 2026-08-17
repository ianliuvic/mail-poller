# mail-poller

轮询 IMAP 邮箱，把新邮件通知推到飞书（Lark）。纯 Python 标准库，无第三方依赖。

## 环境变量

| 变量 | 说明 |
|---|---|
| `MAILBOXES_JSON` | 邮箱数组：`[{"name","host","port","user","password","folder"}]` |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书自建应用凭据 |
| `FEISHU_RECEIVE_ID` | 通知目标（chat_id / open_id / user_id / email） |
| `FEISHU_RECEIVE_ID_TYPE` | 默认 `chat_id` |
| `POLL_INTERVAL_SECONDS` | 轮询间隔，默认 300 |
| `STATE_FILE` | 状态文件（记录每个邮箱的 last_uid），默认 `/data/last_uid.json` |
| `PORT` | 健康检查 HTTP 端口，默认 8000 |

## 说明

- 每个邮箱用 IMAP UID 去重，只推新邮件。
- 提供 `/` 健康检查端点（返回 `ok`），供 Coolify 健康检查。
- 邮箱密码用「应用专用密码 / 授权码」，不是登录密码。
