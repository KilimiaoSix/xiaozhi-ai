# i讯飞每日关注晨报接口

## 1. 能力边界

本模块通过 i讯飞私有飞书域的用户身份 OpenAPI，聚合指定时间窗内用户可见的消息、直接 `@我` 消息和当天日程，生成最多三条优先关注项。

开放接口当前不提供用户逐会话已读游标或客户端未读角标，因此接口只返回“待关注/未处理”，不代表 i讯飞客户端真实未读。模块只读取外部数据，不发送消息、不修改日程、不代替用户回复。

## 2. 前置权限

应用后台和用户 OAuth 都需要具备以下最小权限：

```text
search:message
im:message:get_as_user
```

`POST /open-apis/search/v2/message` 只返回 `message_id`，消息内容、发送人和时间必须再调 `GET /open-apis/im/v1/messages/mget` 补齐。该详情接口在用户令牌下要求 `im:message:get_as_user`，**`im:message:readonly` 不满足**，缺失时返回 `230027 Lack of necessary permissions, ext=need scope: im:message:get_as_user`，对应数据源覆盖状态记为 `FAILED`。

日历源额外需要 `calendar:calendar:readonly`。**i讯飞不发放该权限**，因此 `config.yaml` 默认 `calendar_enabled: false`：服务不请求日历接口，日历一路覆盖状态固定为 `DISABLED`，也不会把整体状态拖成 `PARTIAL`。若某个部署能拿到日历权限，把开关置为 `true` 即可恢复三路采集。

`missing_scopes` 只返回**已启用数据源**真正需要的权限，不会混入已关闭的日历权限。

实测边界（2026-08-18，应用 `cli_aa8b874cfeb8937a`，域 `open.xfchat.iflytek.com`）：

| 接口 | 令牌 | 结果 |
| --- | --- | --- |
| `POST /open-apis/search/v2/message` | tenant | `99991663`，该接口只接受用户令牌 |
| `POST /open-apis/search/v2/message` | user（含 `search:message`） | 成功，只返回 `message_id`，服务端每页固定 20 条 |
| `GET /open-apis/im/v1/messages/mget` | user（含 `im:message:readonly`） | `230027`，需要 `im:message:get_as_user` |
| `POST /open-apis/calendar/v4/calendars/primary` | tenant | `99991672`，缺 `calendar:calendar:readonly` |
| `GET /open-apis/im/v1/chats` | tenant | 成功 |

搜索接口的时间窗字段是 `start_time` / `end_time`，单位为**秒**。传成 `from_time` / `to_time` 或毫秒时间戳都不会报错：前者被静默忽略并返回全部历史消息，后者返回空结果。

消息搜索无法用 tenant token 兜底，用户访问令牌是硬要求；令牌缺失时预览返回 `reauthorization_required: true`。

外部请求固定使用：

```text
https://open.xfchat.iflytek.com
POST /open-apis/search/v2/message
POST /open-apis/calendar/v4/calendars/primary
GET  /open-apis/calendar/v4/calendars/{calendar_id}/events/instance_view
```

消息搜索只返回 ID 时，服务会尝试通过 `GET /open-apis/im/v1/messages/mget` 补充详情。若私有部署不允许用户令牌访问详情接口，该来源会明确标成 `PARTIAL` 或 `FAILED`。

## 3. 配置

默认配置关闭晨报。不要把用户令牌提交到 `config.yaml`，应通过进程环境变量或本地 `data/.config.yaml` 覆盖。

PowerShell 环境变量示例：

```powershell
$env:IFLYTEK_USER_ACCESS_TOKEN = "<user_access_token>"
$env:IFLYTEK_SELF_OPEN_ID = "<current_user_open_id>"
```

`data/.config.yaml` 非敏感配置示例：

```yaml
morning_brief:
  enabled: true
  base_url: https://open.xfchat.iflytek.com
  # i讯飞不发放日历读权限，保持 false；能拿到权限的部署改成 true。
  calendar_enabled: false
  timezone: Asia/Shanghai
  ledger_path: data/morning_brief.sqlite3
  page_size: 50
  max_pages: 40
  timeout_seconds: 15
  overlap_minutes: 10
  excerpt_chars: 240
  excerpt_retention_days: 7
  resolved_retention_days: 30
```

若 `server.auth.enabled: true`，调用下面所有接口都必须携带：

```http
Authorization: Bearer <server.auth_key>
```

此 Bearer 值是 launchcrush HTTP 服务鉴权密钥，不是 i讯飞用户令牌。

## 4. 生成预览

### `POST /xiaozhi/morning-brief/preview`

执行一次只读扫描，更新本地关注台账并保存报告。省略 `report_date` 时使用 `Asia/Shanghai` 当天。

请求：

```json
{
  "report_date": "2026-08-18"
}
```

调用示例：

```bash
curl -X POST http://127.0.0.1:8003/xiaozhi/morning-brief/preview \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <server-auth-key>" \
  -d '{"report_date":"2026-08-18"}'
```

成功响应：

```json
{
  "code": "OK",
  "message": "success",
  "data": {
    "report_type": "OPEN_ATTENTION",
    "report_date": "2026-08-18",
    "generated_at": "2026-08-18T01:00:00+00:00",
    "scan_window": {
      "start": "2026-08-17T18:00:00+08:00",
      "end": "2026-08-18T09:00:00+08:00"
    },
    "coverage_status": "COMPLETE",
    "coverage": [
      {
        "source": "messages",
        "status": "COMPLETE",
        "pages": 2,
        "item_count": 63,
        "next_page_token_present": false,
        "error": null
      }
    ],
    "top_three": [],
    "other_unhandled_mentions": [],
    "calendar": [],
    "reauthorization_required": false,
    "permission_required": false,
    "missing_scopes": [],
    "disclaimer": "待关注/未处理不等同于 i讯飞客户端真实未读。"
  }
}
```

覆盖状态含义：

- `COMPLETE`：所有**已启用**数据源都完整成功。
- `PARTIAL`：至少一路可用，但另一路失败、分页被截断或部分记录无法归一化。
- `FAILED`：已启用的数据源全部失败，结果只用于诊断，不能作为当天完整晨报。

单个数据源的 `status` 还可能是 `DISABLED`，表示该源被配置主动关闭（当前部署即为日历）。`DISABLED` 不参与整体评价，`error` 恒为 `null`——它是既定配置，不是故障。

当 OpenAPI 返回缺权码 `99991672` 时，`permission_required` 为 `true`，`missing_scopes` 返回应用需要开通并由用户授权的完整 scope 列表。令牌失效则通过 `reauthorization_required` 单独表示。

只有普通消息与 `@我` 两路都为 `COMPLETE` 时，消息高水位才推进。后续扫描从上次成功水位向前重叠十分钟，消息 ID 保证幂等。

常见错误：

- `401 UNAUTHORIZED`：launchcrush HTTP Bearer 鉴权失败。
- `400 INVALID_JSON`：请求体不是合法 JSON。
- `400 MORNING_BRIEF_INVALID_REQUEST`：日期格式或字段不合法。
- `503 MORNING_BRIEF_DISABLED`：配置未启用。
- `502 MORNING_BRIEF_UPSTREAM_ERROR`：服务内部未捕获异常；正常的单源 OpenAPI 失败会进入报告的覆盖说明，不返回 502。

## 5. 查询最近报告

### `GET /xiaozhi/morning-brief/latest`

返回最近一次已保存的报告，不触发外部请求。

```bash
curl http://127.0.0.1:8003/xiaozhi/morning-brief/latest \
  -H "Authorization: Bearer <server-auth-key>"
```

首次预览前返回：

```json
{
  "code": "MORNING_BRIEF_NOT_FOUND",
  "message": "no morning brief preview is available",
  "data": null
}
```

## 6. 健康检查

### `GET /xiaozhi/morning-brief/health`

返回功能开关、用户 ID/令牌是否已配置、所需权限、接口能力和最近报告是否存在。响应不会包含用户令牌。

`capabilities.calendar_enabled` 表明日历源是否启用，`capabilities.required_scopes` 随之变化——关闭日历时不再声明日历权限。

```bash
curl http://127.0.0.1:8003/xiaozhi/morning-brief/health \
  -H "Authorization: Bearer <server-auth-key>"
```

关键状态：

- `DISABLED`：功能开关关闭。
- `CONFIGURATION_REQUIRED`：缺少用户 open_id 或用户访问令牌。
- `PERMISSION_REQUIRED`：最近一次预览收到 OpenAPI 缺权码，需要先在应用后台开通报告中的 `missing_scopes`，再重新进行用户授权。
- `READY`：本地配置齐全，且最近一次预览未发现缺权；首次预览前不代表外部权限已完成验证。

## 7. 本地数据与保留

SQLite 默认位于 `data/morning_brief.sqlite3`。关注项只保存消息 ID、会话/话题 ID、发送者 ID、短摘要、来源定位和状态，不保存完整消息正文。

短摘要默认最多 240 字符，七天后清空；包含短摘要的历史报告也在七天后删除；已完成或忽略记录三十天后删除。要紧急回滚时先将 `morning_brief.enabled` 设为 `false`，再备份并移走 SQLite 文件。移走台账只会丢失本地状态和水位，不会修改源消息或日程。

## 8. 当前未实现

- 工作日 08:45 自动调度和 09:00 主动发送。
- 私聊卡片及 Done、Snooze、Dismiss 回调。
- 与 i讯飞客户端未读游标或未读数完全一致。
- 公司节假日、调休工作日历。
