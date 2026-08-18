# 摄像头工位在岗检测集成设计

## 1 背景与目标

现有 `camera-presence-demo` 已在本机摄像头上验证 MediaPipe Pose 检测，可根据核心人体关键点判断用户是否在工位。当前 `launchcrush` 的 Server 负责 HTTP 接入及与 ESP32-S3 的 WebSocket 通信，桌面端后续需要读取稳定、结构化的在岗状态参与 Agent 决策。

本次交付目标是把在岗检测作为独立本地能力接入 Server，并提供清晰的 HTTP 契约和 Windows 一键启动入口。实现必须满足以下约束：

- 摄像头画面、截图和完整人体关键点不得离开本机。
- MediaPipe、OpenCV 与主 Server 的 Python 依赖隔离。
- Server 只保存每个工位的最新状态，不在本期触发机器人动作，也不保存历史事件。
- 检测或网络暂时故障不能阻塞 Server 的其他业务。
- 本地源码部署、远程 Server 和 Docker Server 都使用同一套上报协议。
- 项目文档使用中文，协议字段和代码标识符使用英文。

## 2 范围

### 2.1 本期包含

- 将已验证的姿态检测和状态机代码迁入仓库内的 `presence-agent/`。
- 新增 Server 侧在岗状态注册表和 HTTP handler。
- 新增状态上报、单工位查询两个 HTTP 接口。
- 支持 Bearer Token，沿用 Server 的 `server.auth.enabled` 与 `server.auth_key` 配置。
- 提供 sidecar 单独启动和本地 Server + sidecar 联合启动的 PowerShell 脚本。
- 提供单元测试、HTTP 契约测试、进程级 smoke 测试和真实摄像头 smoke 验证方式。
- 提供面向后续桌面端和 Agent 逻辑的接口文档。

### 2.2 本期不包含

- 人脸识别、身份识别、多目标人数统计。
- 上传或保存摄像头图像、视频、截图、完整 landmark。
- 在 Server 中运行 MediaPipe/OpenCV。
- Server 重启后的状态持久化；agent 会在下一次上报时恢复状态，最迟为 15 秒。
- 历史轨迹、考勤统计、数据库表、消息队列。
- 根据在岗状态直接控制机器人或修改桌面端业务逻辑。
- Linux 容器直接访问 Windows 摄像头。

## 3 方案选择

### 3.1 采用方案：本地 presence-agent sidecar

```text
本地摄像头
    |
    v
presence-agent
  - MediaPipe Pose
  - 在岗状态机
  - 状态变化 + 心跳上报
    |
    | POST /xiaozhi/presence/report
    v
launchcrush Server
  - PresenceRegistry
  - 超时派生 stale
    |
    | GET /xiaozhi/presence/{workstation_id}
    v
后续桌面端 / Agent / 机器人决策逻辑
```

选择 sidecar 的原因：

- 摄像头属于工位本机资源，远程或 Docker Server 通常无法直接访问。
- 视觉依赖体积大且 Python 版本敏感，隔离后不会影响 Server 的语音、LLM 和设备通信依赖。
- 只传状态和最小诊断指标，隐私边界清晰。
- Server 与检测算法通过版本化 JSON 契约解耦，后续可替换检测模型而不影响消费者。

未采用的方案：

- **直接嵌入 Server**：部署位置和摄像头位置耦合，依赖冲突风险高，不适合远程 Server。
- **嵌入 Electron 桌面端**：需要引入 Node 原生视觉依赖或额外 Python 桥接，增加桌面端打包复杂度，且不利于无桌面端场景复用。

## 4 组件设计

### 4.1 presence-agent

`presence-agent/` 是独立 Python 应用，拥有自己的 `.venv` 和锁定依赖。默认无预览窗口，适合长期后台运行；开发联调时可通过 `--preview` 查看画面和状态。支持以下命令行配置：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--server-url` | `http://127.0.0.1:8003` | Server 基础地址 |
| `--workstation-id` | 当前计算机名规范化结果 | 工位唯一标识 |
| `--auth-token` | 空 | Server 开启认证时使用的 Bearer Token |
| `--camera` | `0` | 摄像头索引 |
| `--width` | `640` | 采集宽度 |
| `--height` | `480` | 采集高度 |
| `--absent-after` | `2.0` | 最后一次正样本后进入 absent 的秒数 |
| `--heartbeat-seconds` | `15.0` | 状态不变时的上报间隔 |
| `--preview` | 关闭 | 是否显示本地预览窗口 |
| `--smoke-frames` | `0` | 大于 0 时处理指定帧数后退出 |

关键模块边界：

- `presence_agent/pose_detector.py`：MediaPipe 适配和最小 `PoseObservation`。
- `presence_agent/state.py`：纯状态机，不依赖摄像头或网络。
- `presence_agent/snapshot.py`：把状态机结果转成线程安全的最新快照。
- `presence_agent/reporter.py`：生成版本化事件、重试并调用 Server。
- `presence_agent/app.py`：摄像头生命周期和以上组件的编排。
- `server/main/xiaozhi-server/presence_server.py`：只加载 presence 路由的轻量联调入口，复用生产 Registry/Handler，不加载语音、LLM 或机器人模块。

检测线程只更新内存中的最新快照；上报线程读取快照并执行 HTTP 请求，网络超时不会卡住摄像头推理。上报线程在状态改变后尽快发送，并在状态不变时每 15 秒发送一次心跳。同一快照的失败请求保留 `event_id`、`agent_instance_id` 和 `sequence` 重试；重试期间若状态再次改变，则丢弃未确认的旧快照，为最新状态生成更大的 sequence。退避时间为 1、2、4、8、16、30 秒，成功后恢复正常节奏。

摄像头打开或连续读取失败时，状态变为 `camera_error` 并继续按退避策略尝试重新打开摄像头；进程不因暂时摄像头故障退出。模型文件缺失或参数无效属于启动配置错误，打印明确错误并以非零状态退出。

### 4.2 PresenceTracker

状态枚举：

| 状态 | 产生方 | 含义 |
| --- | --- | --- |
| `starting` | agent | 摄像头可用，但尚未形成稳定判断 |
| `present` | agent | 连续 3 帧满足人体姿态规则 |
| `absent` | agent | 距离最后一个正样本达到 2 秒；启动后从未检测到正样本时，从首个健康帧开始计时 |
| `camera_error` | agent | 摄像头无法打开或连续读取失败 |
| `stale` | Server | 最近一次成功上报距当前时间超过 30 秒 |

单帧正样本规则保持与已验证 demo 一致：在鼻、双肩、双肘、双髋 7 个核心 landmark 中至少 4 个的 `visibility >= 0.5`，且至少包含一个肩部 landmark。完整 landmark 不进入上报数据。

状态机规则：

- 连续 3 个正样本后进入 `present`，避免单帧误检。
- `present` 后短暂丢失关键点不会立刻离岗；距离最后正样本满 2 秒才进入 `absent`。
- 摄像头错误立即进入 `camera_error`，恢复后重新从 `starting` 开始确认。
- Server 的 `stale` 只影响查询接口中的 `effective_state`，不改写 agent 最近上报的 `reported_state`。

### 4.3 PresenceReporter

每个 agent 进程启动时生成一个 UUID `agent_instance_id`；`sequence` 从 1 开始，在生成新事件时递增。相同本地快照的失败重试不生成新序号。`previous_state` 和 `changed` 描述本地状态机相邻快照之间的变化；心跳使用 `previous_state == state` 和 `changed=false`。因此，网络恢复后的第一条成功事件携带 agent 当时的最新状态，而不是回放积压心跳。

Server 的幂等和顺序规则：

- 与注册表最新事件相同的 `event_id` 重试返回成功，`duplicate=true`，不重复更新注册表。
- 同一 `agent_instance_id` 下，`sequence` 小于或等于最后已接收序号且 `event_id` 不同，返回 HTTP 409。
- 新的 `agent_instance_id` 只有在 `sequence=1` 且 `observed_at` 不早于当前报告时才能替换当前实例；旧实例其后的 sequence 大于 1，因此返回 HTTP 409。
- `observed_at` 比 Server 当前时间晚 5 分钟以上，或比该工位已接受事件早 5 分钟以上，返回 HTTP 400，防止明显错误的系统时间覆盖新状态。

该模型支持 agent 正常重启时 sequence 重新从 1 开始，也能阻止旧进程延迟请求覆盖新进程状态。一个 `workstation_id` 同时只允许运行一个有效 agent；重复配置属于部署错误。Server 重启会清空排序元数据，之后收到的第一条合法报告建立新基线。

### 4.4 Server PresenceRegistry

`PresenceRegistry` 是无外部依赖的进程内注册表，职责仅包括：

- 校验和接收已解析的 `PresenceReport`。
- 按 `workstation_id` 保存最新报告、Server 接收时间和进程排序元数据。
- 通过注入时钟计算 `age_seconds` 和 `effective_state`，便于确定性测试。
- 提供单工位查询。

注册表不持有 aiohttp request/response，不调用机器人，不写磁盘。`PresenceHandler` 负责认证、JSON 解析、字段校验、HTTP 状态码和 CORS。

### 4.5 Server 路由接入

`SimpleHttpServer` 无论是否传入 `ws_server` 都创建 `PresenceRegistry` 和 `PresenceHandler`。presence 接口不依赖在线 ESP32 设备，因此不能像现有 event push 路由一样受 `ws_server` 是否存在影响。

新增路由：

- `POST /xiaozhi/presence/report`
- `OPTIONS /xiaozhi/presence/report`
- `GET /xiaozhi/presence/{workstation_id}`
- `OPTIONS /xiaozhi/presence/{workstation_id}`

## 5 HTTP 协议

### 5.1 通用响应结构

所有新增接口使用统一结构：

```json
{
  "code": "OK",
  "message": "success",
  "data": {}
}
```

- `code`：稳定、可供程序判断的字符串错误码。
- `message`：供联调和日志查看的文本，不作为业务判断依据。
- `data`：成功时为对象；失败时为 `null` 或包含可操作诊断字段的对象。

认证开启时，请求使用 `Authorization: Bearer <server.auth_key>`。认证关闭时该 header 可省略。

### 5.2 上报请求

`POST /xiaozhi/presence/report`

```json
{
  "schema_version": "1.0",
  "event_id": "6c618629-ffef-4c00-ab4f-17dc5ce2eb7a",
  "agent_instance_id": "45912c0c-144b-4ac7-970b-527add7b4dcc",
  "workstation_id": "desk-tfzhang11",
  "source": "camera_pose",
  "state": "present",
  "previous_state": "starting",
  "changed": true,
  "reason": "pose_confirmed",
  "sequence": 12,
  "observed_at": "2026-08-18T09:10:30.123Z",
  "metrics": {
    "visible_core_landmarks": 5,
    "has_visible_shoulder": true,
    "positive_streak": 3,
    "seconds_since_last_positive": 0.0
  }
}
```

字段约束：

- `schema_version` 必须为 `1.0`。
- `event_id`、`agent_instance_id` 必须为 UUID 字符串。
- `workstation_id` 长度 1 到 64，只允许 ASCII 字母、数字、点、下划线和连字符。
- `source` 本期必须为 `camera_pose`。
- `state` 和 `previous_state` 只允许 agent 状态，不允许上报 `stale`。
- `changed` 必须等于 `state != previous_state`。
- `reason` 必须为 `initializing`、`pose_confirmed`、`absence_timeout`、`camera_open_failed`、`camera_read_failed`、`camera_recovered` 或 `heartbeat`。
- `sequence` 为大于等于 1 的整数，布尔值不视为整数。
- `observed_at` 必须为带时区的 RFC 3339 时间，agent 固定发送 UTC `Z`。
- `metrics` 必须为对象，仅允许上例四个可选字段：`visible_core_landmarks` 是 0 到 7 的整数，`has_visible_shoulder` 是布尔值，`positive_streak` 是大于等于 0 的整数，`seconds_since_last_positive` 是大于等于 0 的有限数值。四个字段均可省略；不接受额外字段、图像、编码帧或完整 landmark。
- 请求体最大 16 KiB。

成功响应：

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

### 5.3 单工位查询

`GET /xiaozhi/presence/{workstation_id}`

正常且未过期的响应：

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

当 `age_seconds > 30.0` 时，`effective_state` 为 `stale`，其他最近上报字段原样保留。未知工位返回 HTTP 404 和 `PRESENCE_NOT_FOUND`，消费者不得把 404 解释为 `absent`。

### 5.4 HTTP 状态码和错误码

| HTTP | `code` | 场景 |
| --- | --- | --- |
| 200 | `OK` | 接收、幂等重试或查询成功 |
| 400 | `INVALID_JSON` | 请求体不是合法 JSON 对象 |
| 400 | `PRESENCE_INVALID_REQUEST` | 缺字段、字段类型或取值不合法、时间明显异常 |
| 401 | `UNAUTHORIZED` | Server 开启认证但 Token 缺失或错误 |
| 404 | `PRESENCE_NOT_FOUND` | 工位尚无已接受报告 |
| 409 | `PRESENCE_OUT_OF_ORDER` | 旧 sequence 或已退休 agent 实例上报 |
| 413 | `PAYLOAD_TOO_LARGE` | 请求体超过 16 KiB |
| 500 | `INTERNAL_ERROR` | 未预期的 Server 错误；详细堆栈只写服务端日志 |

## 6 数据流

### 6.1 正常启动

1. 联合启动脚本检查并准备 presence-agent 独立虚拟环境。
2. 脚本复用已运行的本地 Server，或启动 `server/main/xiaozhi-server/app.py`。
3. agent 加载模型并打开摄像头，生成 `agent_instance_id`。
4. agent 发布 `starting` 快照，上报线程向 Server 发送首条事件。
5. 连续 3 帧正样本后，agent 生成 `present` 变化事件。
6. Server 更新注册表；后续消费者通过 GET 查询。
7. 状态不变时 agent 每 15 秒上报心跳。

### 6.2 网络故障

1. 检测线程继续本地推理和更新最新快照。
2. 上报线程按 1 到 30 秒退避重试；若本地状态改变，则用更大 sequence 替换待确认事件，只保留最新状态。
3. Server 在最后接收时间超过 30 秒后，将查询结果的 `effective_state` 派生为 `stale`。
4. 网络恢复后，agent 先确认待发送的最新状态，再恢复 15 秒心跳。

本期注册表是“最新值”模型，不保证网络中断期间每个短暂中间状态都被 Server 观察到。

### 6.3 摄像头故障

1. agent 立即生成 `camera_error` 快照并上报。
2. agent 定期重新打开摄像头，不退出进程。
3. 摄像头恢复后状态回到 `starting`，重新执行连续帧确认。
4. `camera_error` 与 `stale` 语义不同：前者表示 agent 在线并明确报告摄像头故障，后者表示 Server 无法确认 agent 是否在线。

## 7 部署设计

### 7.1 前置条件

- Windows PowerShell 5.1 或 PowerShell 7。
- Python 3.10 到 3.13；主 Server 仍以项目推荐的 Python 3.10 为基准。
- 可用摄像头和首次安装 Python 包时的网络访问。
- Server 现有业务配置已按项目文档准备；真实密钥只放在已忽略的 `data/.config.yaml`。

### 7.2 一键入口

仓库根目录新增：

```powershell
.\run-presence-stack.ps1 -WorkstationId desk-tfzhang11
```

脚本行为：

- 若 `http://127.0.0.1:8003` 已有兼容 Server，则复用，不重复启动。
- 若显式传入 `-ServerPython`，使用该解释器启动完整 `server/main/xiaozhi-server/app.py`。
- 未传入 `-ServerPython` 且本地没有兼容 Server 时，使用 presence-agent 的隔离环境启动 `presence_server.py`。该轻量入口暴露与完整 Server 完全相同的 presence 路由，适合首次演示和接口联调，但不启动 WebSocket、语音、LLM 或机器人能力。
- 调用 `presence-agent/run.ps1`；首次运行自动创建 `presence-agent/.venv` 并安装锁定依赖。已验证的 `pose_landmarker_lite.task` 随 `presence-agent/models/` 分发，正常启动不下载模型；文件 SHA-256 为 `59929E1D1EE95287735DDD833B19CF4AC46D29BC7AFDDBBF6753C459690D574A`。
- Ctrl+C 时停止由本脚本创建的子进程，不终止启动前已存在的 Server。
- 参数原样支持覆盖 Server URL、工位 ID、摄像头索引、认证 Token 和 Python 路径。

远程或 Docker Server 场景只运行 sidecar：

```powershell
.\presence-agent\run.ps1 `
  -ServerUrl http://server-host:8003 `
  -WorkstationId desk-tfzhang11 `
  -AuthToken $env:PRESENCE_AUTH_TOKEN
```

脚本不得把 Token 打印到控制台或写入仓库。环境变量 `PRESENCE_AUTH_TOKEN` 是推荐传递方式。

## 8 测试策略

实现严格采用 RED-GREEN-REFACTOR，每个生产行为先有失败测试并观察预期失败。

### 8.1 agent 单元测试

- landmark 可见数量和肩部规则。
- 3 帧确认 `present`。
- 2 秒离岗防抖和从未出现正样本的离岗计时。
- 摄像头错误、恢复后回到 `starting`。
- 变化事件、15 秒心跳、失败重试保留事件身份。
- 退避上限为 30 秒，成功后重置。
- CLI 参数校验和 `--smoke-frames` 退出行为。

### 8.2 Server 单元与契约测试

- 合法首报、同事件幂等、旧 sequence、agent 重启和退休实例。
- 30 秒边界：恰好 30 秒不 stale，超过 30 秒为 stale。
- 未知工位、认证失败、非法 JSON、字段类型、枚举、时间和 16 KiB 限制。
- aiohttp 测试客户端验证路由、状态码、CORS 和完整 JSON 响应。
- presence 路由在没有 `ws_server` 时仍然注册。

### 8.3 集成与 smoke 测试

- 用 fake detector/camera 启动 agent，对真实 aiohttp 测试 Server 上报并查询最终状态。
- PowerShell 脚本语法检查和参数透传测试。
- 本机执行 `presence-agent/run.ps1 -SmokeFrames 30`，确认真实摄像头、模型加载和正常退出。
- 运行新增测试后，再运行仓库原有桌面端测试与 TypeScript 检查，确认无回归。

## 9 可观测性与运维

- agent 日志记录状态变化、上报成功、重试等待、摄像头重连；不记录图像、landmark 或认证 Token。
- Server 日志记录工位 ID、agent 实例、sequence、reported state 和拒绝原因。
- 正常心跳不以 info 级别逐条刷屏，使用 debug；状态变化和从 stale 恢复使用 info。
- 查询端优先使用 `effective_state`；`reported_state` 用于区分 stale 前的最后已知状态。
- 进程退出码：参数/模型配置错误为非零；Ctrl+C 正常退出为 0；暂时网络或摄像头错误不退出。

## 10 后续接入约定

后续桌面端或 Agent 逻辑只能依赖接口文档中的字段和状态语义，不直接读取 sidecar 内部文件。建议决策规则为：

- `present`：可执行需要用户在场的温和提醒。
- `absent`：延后非紧急提醒，不代表用户身份或考勤结论。
- `starting`：等待稳定判断，不按在岗或离岗处理。
- `camera_error`：显示感知不可用，可提供排查入口。
- `stale`：显示状态未知，不沿用旧的 `present`/`absent` 做自动动作。

机器人动作仍由桌面端/Agent 的业务策略决定，Server presence 模块不得直接下发动作。这样可保持“感知事实”和“业务决策”分离。

## 11 验收标准

- 一条命令能在 Windows 上准备并启动本地 presence-agent；联合命令能启动或复用本地 Server。
- 真实摄像头 smoke 运行至少 30 帧并正常退出。
- 连续 3 个正样本后查询得到 `effective_state=present`。
- 2 秒无正样本后查询得到 `effective_state=absent`。
- 停止 agent 超过 30 秒后查询得到 `effective_state=stale`，且 `reported_state` 保留。
- 摄像头画面和完整 landmark 不出现在 HTTP body、Server 日志或磁盘文件中。
- Server 不安装 MediaPipe/OpenCV，presence-agent 使用独立虚拟环境。
- 新增单元、契约和集成测试全部通过，原有桌面端测试和类型检查无回归。
- 接口文档完整说明新增接口、字段、curl、成功/失败示例、错误码和状态机。
