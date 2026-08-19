# 在岗状态驱动机器人迎接设计

## 1 背景与目标

`presence-agent → Server` 这条链路已经跑通，但 Server 收下在岗状态后只存进内存字典，
没有任何消费方（AGENTS.md「当前尚未接通的地方」第 3 条）。演示流程一「早晨到岗」要求：
工程师坐下 → 小智识别到主人 → 从休眠醒来、抬头、笑脸、说欢迎语。这中间缺的正是消费方。

本期目标：在 Server 内实现在岗状态到机器人表演的编排，让「主人到岗」自动触发迎接，
「工位无人」自动进入休眠，无需人在镜头外手动 curl。

职责归属依据 AGENTS.md：Server 是主要大脑，presence 汇聚与机器人通信都在 Server，
编排逻辑放 Server 不引入新的跨组件依赖。

## 2 范围

### 2.1 本期包含

- 新增 `core/presence_arrival.py`：到岗/离岗判定状态机与机器人指令编排。
- `PresenceHandler` 增加可选观察者回调，在 `registry.accept()` 成功后通知编排器。
- `core/http_server.py` 装配编排器（最小改动，不整文件覆盖）。
- `config.yaml` 增加 `presence_robot` 配置段（默认关闭），真实值写 `data/.config.yaml`。
- pytest 单元测试覆盖状态机、去重、降级与失败隔离。

### 2.2 本期不包含

- 桌面端到 Server 的链路（缺口 1，另行处理）。
- 来访者识别、留言、离席汇总（演示流程四/五）。
- 固件改动。迎接效果全部用既有 `push` 字段表达。
- 主动查询 presence 的 LLM 工具（用户口头问「我在不在工位」不在本期）。

## 3 方案选择

### 3.1 采用方案：在 HTTP 接入层挂观察者

`PresenceRegistry.accept()` 是所有上报的必经之路，但它是带锁的同步纯数据结构，
在其中做网络 I/O 会把锁持有时间拉长并引入同步/异步边界问题。
`PresenceHandler.handle_report()` 已在 aiohttp 事件循环内，且该类本就是依赖注入风格
（`now_provider`、`logger` 均可注入），在 accept 成功之后回调是最小且同构的接缝。

### 3.2 放弃的方案

- **轮询 `registry.get()`**：需要常驻定时任务，且上报本身是事件驱动的，轮询只会增加延迟与空转。
- **在 `PresenceRegistry` 内部发事件**：违反其「纯内存最新值存储」的定位，且它是同步代码。
- **放到桌面端判断**：桌面端拿不到 presence（它不订阅 Server），且 AGENTS.md 明确桌面端不是大脑。

## 4 状态机

### 4.1 输入

每条上报携带 presence 状态（`starting` / `present` / `absent` / `camera_error`）与
可选 identity 状态（`starting` / `not_enrolled` / `owner` / `unknown` / `multiple_faces` /
`no_face` / `camera_error`）。上报只在状态变化时发送，另有每 15 秒一条 `reason=heartbeat`
的心跳，因此编排器天然收到的是变化事件流而非帧流。

### 4.2 到岗

`presence == present` 且 identity **已收敛**时触发一次迎接，每个到岗周期只触发一次。

- identity 已收敛：`owner` / `unknown` / `not_enrolled` / `multiple_faces`
- identity 未收敛：`starting` / `no_face` / 缺失 → 继续等待
- 兜底：`present` 持续达到 `identity_wait_seconds` 后仍未收敛，按通用问候发出

问候语选择：

| identity | 问候 | 依据 |
|---|---|---|
| `owner` | `greeting_owner`，可含称呼 | 需求「识别成功后调用该用户的称呼」 |
| 其余已收敛状态 | `greeting_generic`，不含姓名与个人信息 | 需求「识别不确定时只说通用欢迎语，不错误喊出姓名」「陌生人不能读取个人信息」 |

先等 identity 收敛再问好，而不是先通用问候、识别出主人后再补一句——后者会连说两句，听感是坏的。

### 4.3 离岗

`presence == absent` 连续持续达到 `absent_grace_seconds` 后进入休眠一次，并复位到岗标记，
使下次到岗重新迎接。宽限期避免了「短暂离开镜头就睡着」以及来回抖动导致的反复问候。

`starting` 与 `camera_error` 两个状态一律忽略：前者是 agent 尚未判定，后者说明摄像头本身有问题，
此时的在岗结论不可信。

### 4.4 机器人指令

| 事件 | 调用 | 参数 | 依据 |
|---|---|---|---|
| 迎接 | `push_work_event` | `emotion=happy`，`speak=true` | 固件 happy 动画内部调 `HeadUp(15)`（`emoji_controller.cc:971`），一条 emotion 同时完成抬头与笑脸，无需再带 action，避免动作队列与动画队列争抢舵机互斥锁 |
| 休眠 | `push_alert_to_device` | `emotion=sleepy`，`silent=true` | 固件 sleep 动画内部调 `HeadDown`（`:2799`），呈现低头睡着；用 alert 而非 work_event 以免把「休眠」写进对话历史造成噪声 |

迎接走 `push_work_event`，其副作用是把问候写入对话历史，这是需要的——用户接着说「早」时
LLM 能看到自己刚打过招呼。

不设置 `restore_after`：迎接后设备应保持醒着的状态，而不是几秒后弹回基态。

**不做转向定位。** AGENTS.md 硬件边界写明单麦无法声源定位、机器人本体无摄像头，
「云台转向说话人」物理上不可行，转向只能按剧本编排。抬头由 happy 表情隐式完成。

## 5 配置

```yaml
presence_robot:
  enabled: false            # 默认关闭，不影响未配置的部署
  workstations: {}          # workstation_id -> device_id
  identity_wait_seconds: 5
  absent_grace_seconds: 90  # 心跳 15 秒一条，约 6 条心跳后休眠
  sleep_on_absent: true
  greeting_owner: "早上好，今天也一起把事情搞定吧。"
  greeting_generic: "你好，我在这儿。"
```

工位未出现在 `workstations` 映射中时静默跳过，不报错——同一 Server 可能同时接多个工位，
其中只有部分配了机器人。

## 6 失败与降级

| 情况 | 行为 |
|---|---|
| 编排器内部抛异常 | 捕获并记日志，`/xiaozhi/presence/report` 仍返回原有响应，不因编排失败变成 500 |
| 设备不在线 | 记日志跳过，不排队不重试，与 `/xiaozhi/event/push` 的既有语义一致 |
| 重复上报（相同 `event_id`） | 跳过，避免重复问候 |
| 无 `ws_server`（`presence_server.py` 轻量入口） | 编排器不装配，presence 接口行为不变 |
| 上报中断 | 编排器是推动式的，没有上报就没有回调；设备保持当前状态，不做超时兜底 |

并发上报可能交错，因此状态标记在 `await` 推送之前就置位，避免两条上报都判定为「该问好」。

## 7 测试策略

pytest，`cd server/main/xiaozhi-server && python -m pytest tests/`。
通过注入时钟与假的推送函数，全部用例离线可跑，不依赖真机与网络。

- 到岗：owner 触发一次带称呼的问候；同一到岗周期内后续上报不重复问候
- 收敛等待：identity 为 `starting` / `no_face` 时不问候；达到等待上限后发通用问候
- 陌生人：`unknown` / `not_enrolled` / `multiple_faces` 只发通用问候，不含姓名
- 离岗：`absent` 未达宽限期不休眠；达到后休眠一次并复位，再次到岗能重新问候
- 忽略：`starting` / `camera_error` 不产生任何指令
- 降级：未映射工位、设备离线、重复 `event_id` 均不产生指令
- 失败隔离：推送抛异常时 `/xiaozhi/presence/report` 仍返回 200

## 8 验收

对照演示流程一「功能需求分析」：

| 需求点 | 满足方式 |
|---|---|
| 触发条件：检测到人进入工位并确认是已登记用户 | `present` + identity `owner` 触发 |
| 识别结果：陌生人不能读取个人信息 | 非 owner 一律通用问候，不含姓名 |
| 机器人反馈：2 秒内完成亮屏、转向、笑脸和欢迎语 | 状态变化即时上报，单条 push 同时完成表情、抬头与播报 |
| 异常处理：识别不确定时只说通用欢迎语 | identity 未收敛超时后走通用问候分支 |

分镜 1-1 要求小智保持休眠，由离岗休眠提供；分镜 1-3 工程师回话走既有语音对话链路，不在本期改动范围。
