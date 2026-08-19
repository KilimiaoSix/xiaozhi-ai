# Server 调用 ESP32-S3 硬件接口文档

本文档描述 LaunchCrush 当前代码中 Server 调用 ESP32-S3 桌面机器人的真实接口、内部协议和执行链路。文档基于当前分支 `194acbd`；未合入 `origin/feat/robot-push-and-actions` 的能力单独标记为候选扩展，不能作为当前联调契约。

| 章节号 | 接口名 | 方法 | 路径 | 接口性质 | 接入方动作 |
| --- | --- | --- | --- | --- | --- |
| 1 | 查询在线硬件设备 | `GET` | `/xiaozhi/event/devices` | 既有接口，无本次协议变更 | 联调前调用，取得可推送的 `device_id` |
| 2 | 推送工作事件到硬件 | `POST` | `/xiaozhi/event/push` | 既有接口，无本次协议变更 | 当前只传文字、状态、表情和可选语音播报 |

## 本次对接结论

### 当前提供的是既有接口说明，不是新接口发布

当前正式 HTTP 控制面只有在线设备查询和工作事件推送两条接口。Server 根据 `device_id` 定位 ESP32-S3 的常连 WebSocket，再发送 `alert` 消息；需要明确控制舵机时，Server 内部通过设备 MCP `tools/call` 调用固件注册的预设动作。

### 接入方必须遵守的边界

| 优先级 | 类型 | 接口/协议 | 当前能力 | 接入方处理 |
| --- | --- | --- | --- | --- |
| P0 | 既有接口 | `POST /xiaozhi/event/push` | `text/status/emotion/speak` | 不要向当前接口传 `action` 等未发布字段 |
| P0 | 内部协议 | WebSocket `alert` | 屏幕、表情、提示音，可选 TTS | 业务端只调用 HTTP，不直接连接机器人 |
| P1 | 内部协议 | MCP `tools/call` | 固件预设舵机动作 | 只能由 Server 调用经过固件注册的工具 |
| P1 | 设备约束 | WebSocket 常连 | 离线时推送返回 404 | 先查询在线设备，并处理设备瞬时掉线 |

### 可以不改的点

| 模块 | 原因 |
| --- | --- |
| Presence API | `/xiaozhi/presence/*` 只负责工位与身份状态，不直接控制硬件 |
| 摄像头 Agent | 只上报状态，不上传画面，也不直接连接 ESP32-S3 |
| 固件舵机角度 | Server 只选择预设动作，不发送原始角度或 PWM 参数 |

## 功能说明

Server 同时监听两个端口：HTTP 默认监听 `8003`，ESP32-S3 WebSocket 默认监听 `8000`。设备通过 `/xiaozhi/v1/` 建立 WebSocket，携带 `device-id` 后被登记到内存中的 `DeviceRegistry`。HTTP 推送到达时，Server 用 `device_id` 查找当前连接并向该连接写入消息。

硬件控制分为两类。`alert` 是轻量通知，负责状态栏、屏显文字、表情和提示音；`speak=true` 时 Server 还会复用 TTS 通道发送语音。明确的双轴舵机动作走设备 MCP：Server 发送 JSON-RPC `tools/call`，固件只执行已注册并测试过的动作，不接受大模型生成的任意舵机角度。

```text
桌面端 / 外部系统
        |
        | HTTP :8003
        v
POST /xiaozhi/event/push
        |
        | device_id -> DeviceRegistry -> active ConnectionHandler
        v
Server WebSocket :8000/xiaozhi/v1/
        |
        +-- type=alert ----------> Application::Alert
        |                          屏幕 / 表情 / 提示音
        |
        +-- type=mcp tools/call -> McpServer::ParseMessage
                                   预设动作队列 -> ServoController
```

## 接口变更摘要

### 新增接口

无。

### 修改接口

当前分支无协议修改。本次只补充接口文档。

### 删除 / 下线

无。

### 候选扩展

远端分支 `origin/feat/robot-push-and-actions` 对 `POST /xiaozhi/event/push` 增加了 `action`、`silent`、`restore_after` 和 `idle_animation`。这些字段尚未进入当前分支，当前调用方不得依赖。详见“候选扩展与合入要求”。

### 兼容性总结

- 现有调用方无需修改。
- 当前 HTTP 响应是历史 `{ok, ...}` 结构，不是统一的 `{code, message, data}` 包装。
- `delivered=true` 只表示 Server 已完成 WebSocket 发送流程，不等价于舵机动作已经物理完成。

## 1 查询在线硬件设备（不变）

- **路径**：`/xiaozhi/event/devices`
- **方法**：`GET`

### 1.1 输入参数

#### 1.1.1 Headers

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `Authorization（不变）` | string | 条件必填 | `server.auth.enabled=true` 时传 `Bearer <server.auth_key>` |

#### 1.1.2 Path

无。

#### 1.1.3 Query

无。

#### 1.1.4 Body

无。

### 1.2 输出参数

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ok（不变）` | boolean | 请求是否成功 |
| `count（不变）` | integer | 当前进程内登记的在线设备数量 |
| `devices（不变）` | string[] | 在线设备 ID 数组，值来自设备 WebSocket 的 `device-id` |
| `message（不变）` | string | 仅失败时返回，例如 `unauthorized` |

### 1.3 curl 实例

```bash
curl "http://127.0.0.1:8003/xiaozhi/event/devices" \
  -H "Authorization: Bearer <server.auth_key>"
```

认证关闭时删除 `Authorization` header。

### 1.4 JSON 范例

成功：

```json
{
  "ok": true,
  "count": 1,
  "devices": [
    "34:85:18:00:00:01"
  ]
}
```

未授权：

```json
{
  "ok": false,
  "message": "unauthorized"
}
```

### 1.5 HTTP 状态码

| HTTP | 场景 | 接入方处理 |
| --- | --- | --- |
| 200 | 查询成功 | 使用 `devices` 渲染或选择目标设备 |
| 401 | Token 缺失或不匹配 | 检查 Server 鉴权配置 |

### 1.6 注意事项

- 该接口只在完整 Server 启动并持有 `WebSocketServer` 时注册；`presence_server.py` 不提供该接口。
- 设备列表是进程内实时连接快照，不是持久化设备清单。
- 查询成功后设备仍可能立即离线，调用方必须处理后续推送的 404。
- 当前路由只为 `/xiaozhi/event/push` 注册了 `OPTIONS`，浏览器跨域直接调用设备列表前需要补齐 CORS 预检路由。

## 2 推送工作事件到硬件（不变）

- **路径**：`/xiaozhi/event/push`
- **方法**：`POST`

### 2.1 输入参数

#### 2.1.1 Headers

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `Content-Type（不变）` | string | 是 | 固定为 `application/json` |
| `Authorization（不变）` | string | 条件必填 | `server.auth.enabled=true` 时传 `Bearer <server.auth_key>` |
| `device-id（兼容保留）` | string | 否 | Body 未传 `device_id` 时作为兼容来源；推荐使用 Body |

#### 2.1.2 Path

无。

#### 2.1.3 Query

无。

#### 2.1.4 Body

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `device_id（不变）` | string | 是 | 目标在线设备 ID；也可通过 `device-id` header 兼容传入 |
| `text（不变）` | string | 是 | 显示给用户的非空文本 |
| `emotion（不变）` | string | 否 | 固件表情名，默认 `neutral`；当前 Server 不校验枚举 |
| `status（不变）` | string | 否 | 屏幕状态栏文案，默认 `通知` |
| `speak（不变）` | boolean | 否 | 是否额外走 TTS 播报，默认 `false` |

当前正式接口不包含 `action`、`silent`、`restore_after` 或 `idle_animation`。

### 2.2 输出参数

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ok（不变）` | boolean | HTTP 处理是否成功 |
| `device_id（不变）` | string | 目标设备 ID |
| `delivered（不变）` | boolean | 是否完成 Server 侧发送流程 |
| `spoke（不变）` | boolean | 是否成功进入 Server TTS 播报流程 |
| `message（不变）` | string | 失败原因 |
| `online_devices（不变）` | string[] | 目标离线时附带的当前在线设备列表 |

### 2.3 curl 实例

```bash
curl -X POST "http://127.0.0.1:8003/xiaozhi/event/push" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <server.auth_key>" \
  -d '{
    "device_id": "34:85:18:00:00:01",
    "text": "Codex 任务已完成",
    "status": "任务完成",
    "emotion": "happy",
    "speak": false
  }'
```

认证关闭时删除 `Authorization` header。

### 2.4 JSON 范例

成功：

```json
{
  "ok": true,
  "device_id": "34:85:18:00:00:01",
  "delivered": true,
  "spoke": false
}
```

目标设备离线：

```json
{
  "ok": false,
  "message": "device 34:85:18:00:00:01 is not online",
  "online_devices": []
}
```

Server 向 WebSocket 发送失败：

```json
{
  "ok": false,
  "message": "push failed: <error detail>",
  "delivered": false
}
```

### 2.5 HTTP 状态码

| HTTP | 场景 | 接入方处理 |
| --- | --- | --- |
| 200 | 推送流程完成 | 根据 `delivered` 和 `spoke` 更新 UI |
| 400 | JSON 非法、Body 不是对象、缺少 `device_id` 或 `text` | 修正请求，不要原样无限重试 |
| 401 | Token 缺失或不匹配 | 检查鉴权配置 |
| 404 | 目标设备不在线 | 刷新设备列表；不要补播已经过期的动作 |
| 502 | WebSocket 或 TTS 推送过程中发生异常 | 有界退避重试，并记录事件 ID 防止重复提示 |

### 2.6 行为说明

Server 按以下顺序处理：

1. 校验 HTTP 鉴权和 JSON 结构。
2. 从 Body `device_id` 或 header `device-id` 取得设备 ID。
3. 在 `DeviceRegistry` 中查找当前 `ConnectionHandler`。
4. 发送 `type=alert` WebSocket 消息。
5. `speak=true` 时把文本放入该连接的 TTS 队列。
6. 返回 Server 侧投递结果。

`delivered=true` 不包含硬件完成回执。当前 `alert` 协议没有应用级 ACK，物理屏幕、提示音或舵机是否完成需要设备日志或后续协议扩展确认。

## 3 ESP32-S3 WebSocket 连接协议（内部）

### 3.1 连接地址

```text
ws://<server-host>:8000/xiaozhi/v1/
```

### 3.2 握手 Headers

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `device-id` | 是 | 设备唯一 ID，Server 用于登记和查找连接 |
| `client-id` | 认证开启时需要 | 参与设备 Token 校验 |
| `Authorization` | 认证开启时需要 | `Bearer <device token>` |

Server 认证通过后创建独立 `ConnectionHandler`，并在进入会话循环前将它登记到 `DeviceRegistry`。设备重连时新连接替换旧连接；旧连接退出时只注销自身，避免误删新连接。

## 4 Alert 硬件通知协议（内部）

### 4.1 Server 下行帧

```json
{
  "type": "alert",
  "status": "任务完成",
  "message": "Codex 任务已完成",
  "emotion": "happy",
  "session_id": "<current session id>"
}
```

### 4.2 固件执行

固件 `Application` 收到 `type=alert` 后校验 `status/message/emotion` 均为字符串，然后调用：

```cpp
Alert(status, message, emotion, Lang::Sounds::OGG_VIBRATION);
```

该调用更新屏幕状态、显示文本、设置表情并播放提示音。Emoji Board 会根据表情映射执行固件内置动画；业务端不应把表情名当作稳定的舵机动作契约。需要确定的点头、摇头、庆祝或转向动作时，应使用 MCP 预设动作。

### 4.3 回执语义

`alert` 当前没有应用级响应。Server 的 `await websocket.send(...)` 成功只证明消息交给 WebSocket 发送层，不能证明硬件动作已完成。

## 5 MCP 预设动作协议（内部）

### 5.1 初始化过程

设备声明支持 MCP 后，Server 依次发送：

1. JSON-RPC `initialize`。
2. JSON-RPC `tools/list`。
3. 固件返回工具清单，Server 建立清洗名称到原始工具名称的映射。
4. MCP 客户端进入 ready 状态后才允许调用工具。

### 5.2 动作调用帧

```json
{
  "type": "mcp",
  "payload": {
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "self.robot.play_action",
      "arguments": {
        "action": "nod"
      }
    }
  }
}
```

Server 内部清洗后的工具名是 `self_robot_play_action`，发送到固件时通过名称映射恢复为 `self.robot.play_action`。

### 5.3 当前固件动作枚举

| `action` | 硬件行为 |
| --- | --- |
| `nod` | 点头 |
| `shake` | 摇头 |
| `roll` | 环绕摆头，用于庆祝 |
| `look_left` | 向左看后回中 |
| `look_right` | 向右看后回中 |
| `look_up` | 抬头后回中 |
| `look_down` | 低头后回中 |
| `center` | 回到中心位置 |

固件使用 FreeRTOS 动作队列执行舵机操作，避免阻塞 WebSocket 主事件循环。MCP 调用具有 JSON-RPC 响应，Server 会等待结果或在默认 30 秒后超时。

### 5.4 安全边界

- Server 只发送固件已经通过 `tools/list` 公布的工具。
- 固件只接受枚举动作，不接受任意角度、速度或 PWM。
- MCP 未 ready、工具不存在、参数非法或响应超时时，Server 必须将本次动作判为失败。
- 表情和物理动作是两个不同契约；`emotion=happy` 不等价于 `action=roll`。

## 6 TTS 扬声器链路（内部）

`POST /xiaozhi/event/push` 传 `speak=true` 后，Server 复用当前设备连接的 TTS provider，将文本写入 TTS 队列并通过同一 WebSocket 发送音频数据。`spoke=true` 表示 Server 成功启动该流程，不表示用户已经听到完整语音。

主动推送与正在进行的语音会话共享音频通道。当前分支缺少完整的忙碌状态保护；用户正在说话或设备正在播音时，主动 TTS 可能互相干扰。演示和生产默认建议 `speak=false`，以屏显、表情和动作作为主要反馈。

## 7 候选扩展与合入要求

`origin/feat/robot-push-and-actions` 已实现但尚未进入当前分支的请求字段如下。

| 字段 | 类型 | 候选语义 | 当前可用 |
| --- | --- | --- | --- |
| `action` | string | 调用 `self.robot.play_action` | 否 |
| `silent` | boolean | Alert 不播放提示音 | 否 |
| `restore_after` | number | 指定秒数后静音恢复设备基态 | 否 |
| `idle_animation` | boolean | 开关随机空闲动画 | 否 |

候选分支还为固件增加 `hold_left/right/up/down` 保持注视动作、`self.robot.set_idle_animation` 工具、动作重试、TTS 开始信号和忙碌降级。

正式合入前至少需要完成：

1. 将候选分支合并到目标分支并解决测试、文档和固件版本一致性。
2. 为 EventHandler、DeviceRegistry、MCP 动作编排补自动化测试。
3. 在推送响应中补充动作执行结果，避免动作失败仍只返回 `delivered=true`。
4. 增加 `event_id`、`expires_at` 和幂等处理，禁止补播过期机器人动作。
5. 明确动作参数白名单和字段类型，禁止 Python `bool(...)` 把任意非空字符串解析成 `true`。
6. 更新本文档，将候选字段改为正式的 `（新）` 参数并给出兼容性说明。

## 8 推荐的桌面端动作映射

以下是接入建议，不是当前 HTTP 接口已经实现的自动映射。

| 桌面端意图 | `status` | `emotion` | 候选 `action` | TTL 建议 |
| --- | --- | --- | --- | --- |
| `quiet_companion` | `专注中` | `neutral` | 无 | 15 秒 |
| `task_completed` | `任务完成` | `happy` | `roll` | 30 秒 |
| `task_failed` | `需要关注` | `sad` | `look_down` | 30 秒 |
| `needs_user` | `需要你` | `confused` | `look_up` | 10 分钟 |

Server 应根据可信的业务意图选择预设动作，不应允许大模型或桌面端直接传舵机角度。

## 9 联调检查清单

- [ ] 完整 Server 已启动，HTTP `8003` 和 WebSocket `8000` 均可访问。
- [ ] ESP32-S3 使用预期 `device-id` 建立 WebSocket 常连。
- [ ] `GET /xiaozhi/event/devices` 能看到目标设备。
- [ ] `POST /xiaozhi/event/push` 能更新屏幕文字和表情。
- [ ] `speak=false` 时设备不会打开主动 TTS。
- [ ] 设备离线时接口返回 404，客户端不会补播过期动作。
- [ ] MCP 初始化完成后，`self.robot.play_action` 能返回调用结果。
- [ ] 舵机动作来自固定枚举，没有原始角度或 PWM 输入。
- [ ] 认证开启时，缺少或错误 Token 返回 401。

## 10 源码索引

| 职责 | 路径 |
| --- | --- |
| HTTP 路由注册 | `server/main/xiaozhi-server/core/http_server.py` |
| 在线设备查询与事件推送 | `server/main/xiaozhi-server/core/api/event_handler.py` |
| 在线连接注册表 | `server/main/xiaozhi-server/core/device_registry.py` |
| Alert 与 TTS 推送 | `server/main/xiaozhi-server/core/handle/pushHandle.py` |
| WebSocket 接入与设备登记 | `server/main/xiaozhi-server/core/websocket_server.py` |
| MCP 初始化与工具调用 | `server/main/xiaozhi-server/core/providers/tools/device_mcp/mcp_handler.py` |
| 固件消息分发 | `firmware/main/application.cc` |
| 固件 MCP Server | `firmware/main/mcp_server.cc` |
| 机器人动作工具 | `firmware/main/boards/esp32-s3n16r8-emoji/emoji_board.cc` |
| 双轴舵机控制 | `firmware/main/boards/esp32-s3n16r8-emoji/servo_controller.cc` |

