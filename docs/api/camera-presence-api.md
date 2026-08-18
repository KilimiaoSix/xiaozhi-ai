# 摄像头工位在岗状态 API 对接文档

| 章节号 | 接口名 | 方法 | 路径 | 接口性质 | 前端动作 |
| --- | --- | --- | --- | --- | --- |
| 1 | 上报工位在岗状态 | `POST` | `/xiaozhi/presence/report` | 新增接口 | presence-agent 必须接入；桌面端无需调用 |
| 2 | 查询单工位在岗状态 | `GET` | `/xiaozhi/presence/{workstation_id}` | 新增接口 | 桌面端/Agent 可选接入，决策时读取 `effective_state` |

## 本次对接结论

### 这是新增在岗感知能力，不修改既有业务接口

本次提供两条新增接口。既有 `/xiaozhi/event/push`、`/xiaozhi/event/devices`、OTA、视觉和 WebSocket 协议均无本次协议变更，现有消费者可以继续按原逻辑使用。

### 接入方动作

| 优先级 | 类型 | 接口 | 变化 | 接入方处理 |
| --- | --- | --- | --- | --- |
| P0 | 新增接口 | `POST /xiaozhi/presence/report` | 接收本地摄像头推导的状态与心跳 | presence-agent 按本文协议上报 |
| P1 | 新增接口 | `GET /xiaozhi/presence/{workstation_id}` | 返回最新报告和 Server 派生状态 | 后续桌面端/Agent 可选接入，优先判断 `effective_state` |

### 可以不改的点

| 接口/模块 | 原因 |
| --- | --- |
| `/xiaozhi/event/*` | 本次没有协议变化 |
| ESP32-S3 固件 | presence 模块不直接下发机器人动作 |
| 桌面端现有 Mock 流程 | 本期只提供后续接入契约，不强制修改桌面端 |

## 功能说明

摄像头由工位本机的 `presence-agent` 独占使用。agent 在本地完成 MediaPipe Pose 推理，只发送 `starting`、`present`、`absent`、`camera_error` 状态及少量聚合指标，不上传画面、截图、完整人体 landmark 或身份信息。

Server 在内存中保存每个 `workstation_id` 的最新报告。超过 30 秒没有收到新报告时，查询接口把 `effective_state` 派生为 `stale`，同时保留 `reported_state`，供接入方区分“最后报告了什么”和“当前是否仍可信”。

## 接口变更摘要

### 新增接口

- `POST /xiaozhi/presence/report`：接收状态变化和 15 秒心跳。
- `GET /xiaozhi/presence/{workstation_id}`：查询最新状态，Server 自动派生 `stale`。

### 修改接口

无。

### 删除 / 下线

无。

### 兼容性总结

- 老客户端不升级可以继续使用，既有接口无协议变化。
- 新消费者必须把 HTTP 404 解释为“尚无数据”，不能解释为 `absent`。
- 新消费者必须使用 `effective_state` 做当前决策；`reported_state` 仅表示最后一次 agent 报告。

## 1 上报工位在岗状态（新）

- **路径**：`/xiaozhi/presence/report`
- **方法**：`POST`

### 1.1 输入参数

#### 1.1.1 Headers

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `Content-Type（新）` | string | 是 | 固定为 `application/json` |
| `Authorization（新）` | string | 条件必填 | Server 开启 `server.auth.enabled` 时传 `Bearer <server.auth_key>` |

#### 1.1.2 Body

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `schema_version（新）` | string | 是 | 固定为 `1.0` |
| `event_id（新）` | UUID string | 是 | 单次事件 ID；失败重试保持不变 |
| `agent_instance_id（新）` | UUID string | 是 | agent 进程实例 ID；进程重启后改变 |
| `workstation_id（新）` | string | 是 | 1-64 字符，只允许字母、数字、`.`、`_`、`-` |
| `source（新）` | string | 是 | 固定为 `camera_pose` |
| `state（新）` | enum | 是 | `starting` / `present` / `absent` / `camera_error` |
| `previous_state（新）` | enum | 是 | 本地状态机前一状态；不允许 `stale` |
| `changed（新）` | boolean | 是 | 必须等于 `state != previous_state` |
| `reason（新）` | enum | 是 | 见附录 3.2 |
| `sequence（新）` | integer | 是 | 当前进程内从 1 递增；失败重试保持不变 |
| `observed_at（新）` | RFC 3339 string | 是 | 带时区，agent 发送 UTC `Z` |
| `metrics（新）` | object | 是 | 聚合指标对象，不允许额外字段 |
| `metrics.visible_core_landmarks（新）` | integer | 否 | 0-7 个可见核心关键点 |
| `metrics.has_visible_shoulder（新）` | boolean | 否 | 是否至少有一个可见肩部关键点 |
| `metrics.positive_streak（新）` | integer | 否 | 当前连续正样本数，大于等于 0 |
| `metrics.seconds_since_last_positive（新）` | number | 否 | 距最后正样本秒数，大于等于 0 |

请求体最大 16 KiB。Body 不接受 `frame`、`image`、`landmarks` 或其他未声明字段。

### 1.2 输出参数

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `code（新）` | string | `OK` 或附录错误码 |
| `message（新）` | string | 联调文本，不作为业务判断依据 |
| `data（新）` | object/null | 失败时通常为 `null` |
| `data.accepted（新）` | boolean | 合法首报和幂等重试均为 `true` |
| `data.duplicate（新）` | boolean | 是否为最新 `event_id` 的幂等重试 |
| `data.workstation_id（新）` | string | 已接收工位 ID |
| `data.sequence（新）` | integer | 已接收序号 |
| `data.received_at（新）` | RFC 3339 string | Server 首次接收该事件的 UTC 时间 |

### 1.3 curl 实例

```bash
curl -X POST "http://127.0.0.1:8003/xiaozhi/presence/report" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <server.auth_key>" \
  -d '{
    "schema_version":"1.0",
    "event_id":"6c618629-ffef-4c00-ab4f-17dc5ce2eb7a",
    "agent_instance_id":"45912c0c-144b-4ac7-970b-527add7b4dcc",
    "workstation_id":"desk-tfzhang11",
    "source":"camera_pose",
    "state":"present",
    "previous_state":"starting",
    "changed":true,
    "reason":"pose_confirmed",
    "sequence":12,
    "observed_at":"2026-08-18T09:10:30.123Z",
    "metrics":{
      "visible_core_landmarks":5,
      "has_visible_shoulder":true,
      "positive_streak":3,
      "seconds_since_last_positive":0.0
    }
  }'
```

认证关闭时删除 `Authorization` header。

### 1.4 JSON 范例

```json
{
  "code": "OK",
  "message": "success",
  "data": {
    "accepted": true,
    "duplicate": false,
    "workstation_id": "desk-tfzhang11",
    "sequence": 12,
    "received_at": "2026-08-18T09:10:30.220Z"
  }
}
```

## 2 查询单工位在岗状态（新）

- **路径**：`/xiaozhi/presence/{workstation_id}`
- **方法**：`GET`

### 2.1 输入参数

#### 2.1.1 Headers

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `Authorization（新）` | string | 条件必填 | Server 开启认证时传 `Bearer <server.auth_key>` |

#### 2.1.2 Path

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `workstation_id（新）` | string | 是 | 与上报时一致；1-64 字符 |

#### 2.1.3 Query

无。

### 2.2 输出参数

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `code（新）` | string | `OK` 或附录错误码 |
| `message（新）` | string | 联调文本 |
| `data（新）` | object/null | 未知工位时为 `null` |
| `data.workstation_id（新）` | string | 工位 ID |
| `data.source（新）` | string | 当前固定 `camera_pose` |
| `data.effective_state（新）` | enum | 当前有效状态，可能为 Server 派生的 `stale` |
| `data.reported_state（新）` | enum | agent 最后报告状态，不包含 `stale` |
| `data.reason（新）` | enum | 最后报告原因 |
| `data.changed（新）` | boolean | 最后报告是否为状态变化 |
| `data.sequence（新）` | integer | 最后报告序号 |
| `data.event_id（新）` | UUID string | 最后事件 ID |
| `data.agent_instance_id（新）` | UUID string | 当前 agent 进程实例 ID |
| `data.observed_at（新）` | RFC 3339 string | agent 观测时间 |
| `data.received_at（新）` | RFC 3339 string | Server 接收时间 |
| `data.age_seconds（新）` | number | 距 Server 接收时间的秒数，保留 3 位小数 |
| `data.stale_after_seconds（新）` | number | 当前固定 `30.0` |
| `data.metrics（新）` | object | 最后报告的聚合指标 |

### 2.3 curl 实例

```bash
curl "http://127.0.0.1:8003/xiaozhi/presence/desk-tfzhang11" \
  -H "Authorization: Bearer <server.auth_key>"
```

认证关闭时删除 `Authorization` header。

### 2.4 JSON 范例

```json
{
  "code": "OK",
  "message": "success",
  "data": {
    "workstation_id": "desk-tfzhang11",
    "source": "camera_pose",
    "effective_state": "present",
    "reported_state": "present",
    "reason": "pose_confirmed",
    "changed": true,
    "sequence": 12,
    "event_id": "6c618629-ffef-4c00-ab4f-17dc5ce2eb7a",
    "agent_instance_id": "45912c0c-144b-4ac7-970b-527add7b4dcc",
    "observed_at": "2026-08-18T09:10:30.123Z",
    "received_at": "2026-08-18T09:10:30.220Z",
    "age_seconds": 0.4,
    "stale_after_seconds": 30.0,
    "metrics": {
      "visible_core_landmarks": 5,
      "has_visible_shoulder": true,
      "positive_streak": 3,
      "seconds_since_last_positive": 0.0
    }
  }
}
```

## 3 附录

### 3.1 错误码

| HTTP | `code` | 接入方处理 |
| --- | --- | --- |
| 400 | `INVALID_JSON` | 修正 JSON 编码或语法 |
| 400 | `PRESENCE_INVALID_REQUEST` | 根据 `message` 修正字段；不要无限重试同一请求 |
| 401 | `UNAUTHORIZED` | 检查 Server 认证配置和 Token |
| 404 | `PRESENCE_NOT_FOUND` | 显示“尚无状态/等待 agent”，不得当作 `absent` |
| 409 | `PRESENCE_OUT_OF_ORDER` | 检查重复 agent、实例 ID 和 sequence；不要重试旧事件 |
| 413 | `PAYLOAD_TOO_LARGE` | 删除非协议字段；标准 agent 请求远小于 16 KiB |
| 500 | `INTERNAL_ERROR` | 退避重试并检查 Server 日志 |

错误响应示例：

```json
{
  "code": "PRESENCE_NOT_FOUND",
  "message": "no presence report for workstation desk-tfzhang11",
  "data": null
}
```

### 3.2 枚举值

| 枚举 | 产生方 | 含义 |
| --- | --- | --- |
| `starting` | agent | 摄像头可用，等待稳定判断 |
| `present` | agent | 连续 3 帧满足人体姿态规则 |
| `absent` | agent | 最后正样本后达到 2 秒 |
| `camera_error` | agent | 摄像头无法打开或读取 |
| `stale` | Server | 最后成功上报超过 30 秒，仅出现在 `effective_state` |

`reason` 允许值：`initializing`、`pose_confirmed`、`absence_timeout`、`camera_open_failed`、`camera_read_failed`、`camera_recovered`、`heartbeat`。

### 3.3 状态机

```text
starting --连续 3 个正样本--> present
starting --2 秒无正样本----> absent
present  --2 秒无正样本----> absent
absent   --连续 3 个正样本--> present
任意 agent 状态 --摄像头故障--> camera_error
camera_error --摄像头恢复--> starting
任意 reported_state --超过 30 秒无报告--> effective_state=stale
```

### 3.4 消费建议

- 需要即时状态时按业务节奏查询；建议前台每 3-5 秒一次，后台降低频率或停止轮询。
- `present` 可允许需要用户在场的温和提醒。
- `absent` 可延后非紧急提醒，但不能用于身份、考勤或访问控制。
- `starting`、`camera_error`、`stale` 都属于“不足以确认在场”，不要沿用旧 `present` 自动触发动作。
- 页面卸载、休眠或切换工位时停止旧轮询，避免多个定时器并发。
