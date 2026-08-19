# 飞书任务与会议服务端 OpenAPI 迁移设计

日期：2026-08-19
状态：已实现
Scope：`server`、`desktop`

## 目标

桌面端“飞书任务与会议”不再执行或依赖本机 `lark-cli`。数据链路统一为：

```text
Desktop Renderer → Electron IPC → Desktop HTTP Client
                 → Server HTTP → 飞书 OpenAPI（user_access_token）
```

迁移后保留现有可见能力：

- 检查 Server 侧飞书凭据配置状态；
- 展示当前用户今日日程；
- 展示分配给当前用户的未完成任务、截止时间、任务链接和所属清单名；
- 日历、任务或清单名称任一数据源失败时，保留其他已经成功的数据并返回明确警告。

## OpenAPI 契约

使用现有晨报的 `FEISHU_USER_ACCESS_TOKEN` 和 `FEISHU_SELF_OPEN_ID`，直接调用：

| 能力 | 方法与路径 | 权限 |
|---|---|---|
| 我的未完成任务 | `GET /open-apis/task/v2/tasks` | `task:task:read` |
| 任务清单名称 | `GET /open-apis/task/v2/tasklists/{tasklist_guid}` | `task:tasklist:read` |
| 当前用户主日历 | `POST /open-apis/calendar/v4/calendars/primary` | `calendar:calendar:readonly` |
| 今日日程视图 | `GET /open-apis/calendar/v4/calendars/{calendar_id}/events/instance_view` | `calendar:calendar:readonly` |

任务列表固定传 `type=my_tasks`、`completed=false`、`user_id_type=open_id`，每页最多
100 条并按 `page_token` 拉完，受现有 `max_pages` 上限保护。清单名称是增强信息：缺少
`task:tasklist:read` 时不丢弃任务，只省略清单名并给出警告。

## Server 接口

新增两个只读接口，认证规则与其他 `/xiaozhi` HTTP 接口一致：

- `GET /xiaozhi/feishu/status`
  - 返回 Server 是否配置用户令牌、用户 open_id、数据源和所需 scopes；
  - 不返回 access token。
- `GET /xiaozhi/feishu/briefing?date=YYYY-MM-DD`
  - `date` 可选，默认使用配置时区中的当天；
  - 并发读取任务与日历，返回 `meetings`、`tasks`、`warnings`、`fetched_at`；
  - 两个主数据源都失败时返回 `502`；单源失败仍返回 `200`。

Server 输出使用 snake_case。Desktop HTTP Client 负责校验信封并转换为现有 renderer
使用的 camelCase 契约。

## 代码结构

- 扩展 `core/morning_brief/feishu_client.py`：增加任务分页和清单详情只读方法；保留
  晨报已有消息、日历行为不变。
- 新增 `core/feishu_workspace/service.py`：归一化任务/日程并执行部分失败降级。
- 新增 `core/feishu_workspace/factory.py`：复用晨报的本地凭据加载规则。
- 新增 `core/api/feishu_workspace_handler.py` 与 `core/feishu_workspace_routes.py`。
- Desktop 用 `FeishuHttpClient` 替换 `LarkCliClient`，删除 CLI 探测、命令执行和 CLI
  授权提示。

## 错误与安全语义

- 未配置用户令牌：status 返回 `configuration_required`；briefing 返回 `503`。
- token 失效：不记录 token，向 Desktop 返回重新配置 Server 凭据的提示。
- 缺少某一 scope：错误只写数据源和缺失 scope，不把完整上游响应或凭据写入界面。
- 全部接口只读；不创建、更新、完成或删除飞书任务和日程。
- Server 开启认证时，Desktop 从 `DESKPET_SERVER_AUTH_TOKEN` 发送 Bearer Token。

## 非目标

- 不在本次实现 OAuth refresh token 自动轮换。
- 不把晨报的消息关注项替换成飞书任务。
- 不增加任务编辑、完成、创建或日程回复能力。

## 验收标准

1. Desktop 和打包产物中不再引用或执行 `lark-cli`。
2. Server 通过真实 OpenAPI 契约分页读取未完成任务，并能读取今日日程。
3. 任务清单名读取失败不影响任务展示；任务与日历单边失败不影响另一边展示。
4. Server 路由、认证、请求校验、OpenAPI 客户端、Desktop HTTP 转换均有自动化测试。
5. Server 相关 pytest、Desktop Vitest、TypeScript 类型检查和 Electron 打包通过。
