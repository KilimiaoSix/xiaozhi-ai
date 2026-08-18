# 摄像头流式识别与工位状态 API

| 接口 | 方法 | 路径 | 用途 |
| --- | --- | --- | --- |
| 摄像头流式识别 | WebSocket | `/xiaozhi/presence/stream` | 桌面端注册主人、持续发送 JPEG 并接收人体/人脸结果 |
| 上报工位状态 | `POST` | `/xiaozhi/presence/report` | 兼容独立 `presence-agent` |
| 查询工位状态 | `GET` | `/xiaozhi/presence/{workstation_id}` | 查询 Server 内存中的最新有效状态 |

桌面应用是默认摄像头唯一所有者。它通过受控 IPC 把 JPEG 交给 Electron 主进程，再由主进程通过 WebSocket 发送给 Server。Server 对同一解码帧执行 MediaPipe Pose 与 YuNet/SFace，不保存原始画面、完整人体关键点、人脸 embedding 或主人模板内容。独立 `presence-agent` 仍可通过 HTTP 上报，作为无桌面端部署的兼容工具。

监测开关开启后会持续通信，切换页面、最小化窗口、Server 断线或摄像头短暂中断都不会关闭用户意图。只有用户关闭开关或退出应用才停止监测并释放摄像头。Server 超过 30 秒没有收到新报告时，HTTP 查询会把 `effective_state` 派生为 `stale`。

## 1 摄像头流式识别（新）

### 1.1 连接与认证

- WebSocket URL：`ws://<host>:<port>/xiaozhi/presence/stream`；HTTPS 对应 `wss`。
- Server 开启 `server.auth.enabled` 时，握手必须携带 `Authorization: Bearer <server.auth_key>`。
- 第一条客户端消息必须是 UTF-8 JSON `start`，之后只允许完整二进制 JPEG 或 `{"type":"stop"}`。
- 单帧最大 `1 MiB`。客户端目标为 `5 FPS`、最大 `640×360`、JPEG quality `0.72`。
- 客户端和 Server 都使用容量一的背压边界；忙时丢弃旧帧，不建立无界队列。

监测 start：

```json
{
  "type": "start",
  "schema_version": "1.0",
  "mode": "monitoring",
  "session_id": "6c618629-ffef-4c00-ab4f-17dc5ce2eb7a",
  "workstation_id": "desktop-local"
}
```

注册 start 在上述字段基础上把 `mode` 改为 `enrollment`，并增加 1 到 64 字符的 `display_name`。

### 1.2 Server 事件

连接就绪：

```json
{"type":"ready","session_id":"...","sequence":0}
```

监测结果：

```json
{
  "type": "recognition_result",
  "session_id": "...",
  "sequence": 12,
  "processed_at": "2026-08-18T09:10:30.123Z",
  "presence": {"state":"present","changed":false},
  "identity": {
    "state":"owner",
    "previous_state":"owner",
    "changed":false,
    "face_count":1,
    "face_detected":true,
    "similarity":0.712346,
    "threshold":0.45,
    "matched":true
  },
  "metrics": {"processed_frames":12,"server_dropped":1}
}
```

`identity.matched` 由 Server 决定，客户端不得根据相似度重新计算。人脸是否存在由 `face_count > 0` 派生。`similarity` 只在单人脸 `owner` 或 `unknown` 状态提供。

注册进度与完成：

```json
{"type":"enrollment_progress","session_id":"...","sequence":7,"accepted":7,"required":20,"reason":"accepted"}
```

```json
{"type":"enrollment_complete","session_id":"...","sequence":24,"profile_id":"owner","sample_id":"...","display_name":"主人","stored_at":"2026-08-18T09:10:30Z","sample_count":18}
```

Server 每 200 ms 最多接受一个合格样本。累计 20 个样本后剔除 2 个离群样本，以剩余 18 个样本生成模板并原子替换旧模板。取消、失败或连接中断不会覆盖旧模板。

### 1.3 状态与错误

- 人体：`starting`、`present`、`absent`、`camera_error`；HTTP 查询还可能派生 `stale`。
- 人脸：`not_enrolled`、`no_face`、`owner`、`unknown`、`multiple_faces`、`camera_error`。
- 注册质量：`accepted`、`sample_too_soon`、`no_face`、`multiple_faces`、`face_too_small`、`blurry`、`low_quality`。
- 稳定错误码：`PROTOCOL_ERROR`、`FRAME_TOO_LARGE`、`INVALID_JPEG`、`MODEL_UNAVAILABLE`、`INFERENCE_ERROR`。

错误事件包含 `code`、`message` 和 `retryable`。监测遇到可恢复错误时保留开关并自动重连；注册失败会结束本次采集且不修改旧模板。

## 2 上报工位在岗状态（兼容）

- **路径**：`/xiaozhi/presence/report`
- **方法**：`POST`

### 2.1 输入参数

#### 1.1.1 Headers

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `Content-Type（不变）` | string | 是 | 固定为 `application/json` |
| `Authorization（不变）` | string | 条件必填 | Server 开启 `server.auth.enabled` 时传 `Bearer <server.auth_key>` |

#### 1.1.2 Body

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `schema_version（不变）` | string | 是 | 固定为 `1.0` |
| `event_id（不变）` | UUID string | 是 | 单次事件 ID；失败重试保持不变 |
| `agent_instance_id（不变）` | UUID string | 是 | agent 进程实例 ID；进程重启后改变 |
| `workstation_id（不变）` | string | 是 | 1-64 字符，只允许字母、数字、`.`、`_`、`-` |
| `source（不变）` | string | 是 | 固定为 `camera_pose` |
| `state（不变）` | enum | 是 | `starting` / `present` / `absent` / `camera_error` |
| `previous_state（不变）` | enum | 是 | 本地状态机前一状态；不允许 `stale` |
| `changed（不变）` | boolean | 是 | 必须等于 `state != previous_state` |
| `reason（不变）` | enum | 是 | 见附录 4.2 |
| `sequence（不变）` | integer | 是 | 当前进程内从 1 递增；失败重试保持不变 |
| `observed_at（不变）` | RFC 3339 string | 是 | 带时区，agent 发送 UTC `Z` |
| `metrics（不变）` | object | 是 | 聚合指标对象，不允许额外字段 |
| `metrics.visible_core_landmarks（不变）` | integer | 否 | 0-7 个可见核心关键点 |
| `metrics.has_visible_shoulder（不变）` | boolean | 否 | 是否至少有一个可见肩部关键点 |
| `metrics.positive_streak（不变）` | integer | 否 | 当前连续正样本数，大于等于 0 |
| `metrics.seconds_since_last_positive（不变）` | number | 否 | 距最后正样本秒数，大于等于 0 |
| `identity（新）` | object | 否 | 本地身份识别稳定状态；关闭识别的 Agent 可不传 |
| `identity.state（新）` | enum | 是 | 传 `identity` 时必填，见附录 4.2 |
| `identity.previous_state（新）` | enum | 是 | 前一个稳定身份状态 |
| `identity.changed（新）` | boolean | 是 | 必须等于 `state != previous_state` |
| `identity.face_count（新）` | integer | 是 | 非负人脸数；`owner/unknown` 固定 1，`multiple_faces` 至少 2 |
| `identity.similarity（新）` | number | 条件必填 | `owner/unknown` 必填，范围 -1 到 1 |
| `identity.camera（新）` | integer | 否 | 仅 `camera_error` 可传，非负摄像头序号 |

请求体最大 16 KiB。Body 不接受 `frame`、`image`、`landmarks`、`embedding`、模板或其他未声明字段。

### 2.2 输出参数

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `code（不变）` | string | `OK` 或附录错误码 |
| `message（不变）` | string | 联调文本，不作为业务判断依据 |
| `data（不变）` | object/null | 失败时通常为 `null` |
| `data.accepted（不变）` | boolean | 合法首报和幂等重试均为 `true` |
| `data.duplicate（不变）` | boolean | 是否为最新 `event_id` 的幂等重试 |
| `data.workstation_id（不变）` | string | 已接收工位 ID |
| `data.sequence（不变）` | integer | 已接收序号 |
| `data.received_at（不变）` | RFC 3339 string | Server 首次接收该事件的 UTC 时间 |

### 2.3 curl 实例

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
    },
    "identity":{
      "state":"owner",
      "previous_state":"starting",
      "changed":true,
      "face_count":1,
      "similarity":0.712346
    }
  }'
```

认证关闭时删除 `Authorization` header。

### 2.4 JSON 范例

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

## 3 查询单工位在岗状态

- **路径**：`/xiaozhi/presence/{workstation_id}`
- **方法**：`GET`

### 3.1 输入参数

#### 2.1.1 Headers

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `Authorization（不变）` | string | 条件必填 | Server 开启认证时传 `Bearer <server.auth_key>` |

#### 2.1.2 Path

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `workstation_id（不变）` | string | 是 | 与上报时一致；1-64 字符 |

#### 2.1.3 Query

无。

### 3.2 输出参数

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `code（不变）` | string | `OK` 或附录错误码 |
| `message（不变）` | string | 联调文本 |
| `data（不变）` | object/null | 未知工位时为 `null` |
| `data.workstation_id（不变）` | string | 工位 ID |
| `data.source（不变）` | string | 当前固定 `camera_pose` |
| `data.effective_state（不变）` | enum | 当前有效状态，可能为 Server 派生的 `stale` |
| `data.reported_state（不变）` | enum | agent 最后报告状态，不包含 `stale` |
| `data.reason（不变）` | enum | 最后报告原因 |
| `data.changed（不变）` | boolean | 最后报告是否为状态变化 |
| `data.sequence（不变）` | integer | 最后报告序号 |
| `data.event_id（不变）` | UUID string | 最后事件 ID |
| `data.agent_instance_id（不变）` | UUID string | 当前 agent 进程实例 ID |
| `data.observed_at（不变）` | RFC 3339 string | agent 观测时间 |
| `data.received_at（不变）` | RFC 3339 string | Server 接收时间 |
| `data.age_seconds（不变）` | number | 距 Server 接收时间的秒数，保留 3 位小数 |
| `data.stale_after_seconds（不变）` | number | 当前固定 `30.0` |
| `data.metrics（不变）` | object | 最后报告的聚合指标 |
| `data.identity（新）` | object | Agent 上报身份时返回；结构同请求 `identity` |

### 3.3 curl 实例

```bash
curl "http://127.0.0.1:8003/xiaozhi/presence/desk-tfzhang11" \
  -H "Authorization: Bearer <server.auth_key>"
```

认证关闭时删除 `Authorization` header。

### 3.4 JSON 范例

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
    },
    "identity": {
      "state": "owner",
      "previous_state": "starting",
      "changed": true,
      "face_count": 1,
      "similarity": 0.712346
    }
  }
}
```

## 4 附录

### 4.1 错误码

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

### 4.2 枚举值

| 枚举 | 产生方 | 含义 |
| --- | --- | --- |
| `starting` | agent | 摄像头可用，等待稳定判断 |
| `present` | agent | 连续 3 帧满足人体姿态规则 |
| `absent` | agent | 最后正样本后达到 2 秒 |
| `camera_error` | agent | 摄像头无法打开或读取 |
| `stale` | Server | 最后成功上报超过 30 秒，仅出现在 `effective_state` |

身份 `identity.state` 允许值：

| 枚举 | 含义 |
| --- | --- |
| `starting` | 已登记，等待身份判断稳定 |
| `not_enrolled` | 本机尚未登记本人模板 |
| `owner` | 单人脸达到相似度阈值并连续 3 帧确认 |
| `unknown` | 单人脸未达到阈值并连续 3 帧确认 |
| `multiple_faces` | 连续 3 帧检测到至少两张人脸，不猜测身份 |
| `no_face` | 连续 1 秒没有检测到人脸 |
| `camera_error` | 摄像头无法打开或读取 |

`reason` 允许值：`initializing`、`pose_confirmed`、`absence_timeout`、`camera_open_failed`、`camera_read_failed`、`camera_recovered`、`identity_changed`、`heartbeat`。

### 4.3 状态机

```text
starting --连续 3 个正样本--> present
starting --2 秒无正样本----> absent
present  --2 秒无正样本----> absent
absent   --连续 3 个正样本--> present
任意 agent 状态 --摄像头故障--> camera_error
camera_error --摄像头恢复--> starting
任意 reported_state --超过 30 秒无报告--> effective_state=stale
```

### 4.4 消费建议

- 需要即时状态时按业务节奏查询；建议前台每 3-5 秒一次，后台降低频率或停止轮询。
- `present` 可允许需要用户在场的温和提醒。
- `absent` 可延后非紧急提醒，但不能用于身份、考勤或访问控制。
- `starting`、`camera_error`、`stale` 都属于“不足以确认在场”，不要沿用旧 `present` 自动触发动作。
- `effective_state=stale` 时整条观测都已过期，不得继续信任最后一次 `identity`。
- `identity.state=owner` 没有活体检测，只能用于低风险个性化提示，不能用于授权、考勤、门禁、支付或其他高风险决策。
- 页面卸载、休眠或切换工位时停止旧轮询，避免多个定时器并发。
