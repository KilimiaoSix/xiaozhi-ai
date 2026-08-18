# AGENTS.md

本文件是本仓库的**事实源**，描述系统实际长成什么样、各组件真实职责与接口契约。
所有编码 Agent（Codex、Claude Code、WorkBuddy）与人类协作者都以此为准。
Claude Code 的工作方式约定另见 [CLAUDE.md](CLAUDE.md)，那里不重复本文件的事实。

**当文档与代码冲突时，以代码为准，并顺手修正本文件。**

## 项目定位

项目名称：**工伴·桌面精灵**
产品类型：**AI Agent 驱动的打工人桌面宠物**
产品形态：**桌面应用 + ESP32-S3 双轴机器人**

工伴感知 Codex、Claude Code、WorkBuddy 等工具的工作状态，在合适的时机通过桌面界面和机器人动作提供提醒与情绪反馈。核心目标不是做一个普通通知工具，而是让它理解用户当前是在专注、等待、受阻还是刚刚完成工作，再决定如何回应。

## 核心体验

- **运行中：** 桌面端显示当前任务，机器人安静陪伴，不频繁打扰。
- **任务完成：** 桌面端显示绿色完成卡片和彩带；机器人抬头、点头并轻摆庆祝。
- **任务失败：** 桌面端显示橙色关注卡片和错误入口；机器人缓慢低头后回正，不擅自重试任务。
- **等待用户介入：** 桌面端显示"需要你"卡片，提供打开来源、延后和忽略操作；机器人抬头或歪头提醒。
- **设备断线：** 桌面端继续工作并显示断线状态；机器人重连后不补播过期动作。

用户可以调整提醒和庆祝强度，工伴应记住这些偏好，并在下一次行为中体现变化。

## 系统架构

```
Codex / Claude Code / WorkBuddy          摄像头
        │ hook 回调                        │
        ▼                                  ▼
   desktop/ ───────────────────────► WebSocket 摄像头流
   事件采集 + 看板 + 摄像头所有者          │
   （规则判定，无 LLM）                    ▼
        │                            server/  ◄── 主要大脑
        │  ……工作事件尚未接通          同帧人体与主人识别
        └──────────────────────────►       │
                                     LLM · 意图 · 记忆 · 工具
                                     ASR · TTS · VAD
                                          │ WebSocket + 裸 Opus
                                          ▼
                                     firmware/
                                     ESP32-S3 表演终端（无决策）
```

## 职责划分

### server/ — 主要大脑

系统里唯一同时面向外部事件源和机器人硬件的组件。四件事：

1. **完整的语音对话智能体。** 进程启动时按 `selected_module` 实例化 VAD / ASR / LLM / Intent / Memory 五类 provider 并在所有连接间共享（`core/websocket_server.py`）。每条设备连接持有一个 `ConnectionHandler`，其 `chat()`（`core/connection.py`）是流式主循环：查记忆 → 拼上下文 → 带 functions 调 LLM → 边流式解析边灌 TTS 队列 → 并发执行工具调用 → 结果写回历史后最多递归 5 层。
2. **ESP32-S3 的唯一通信对端。** `ws://host:8000/xiaozhi/v1/`，自定义 JSON 协议 + 裸 Opus 二进制帧。
3. **外部工作事件入口。** `POST /xiaozhi/event/push` 按 `device_id` 从 `DeviceRegistry` 查到活跃连接后直接下发。这条路径**不经过 LLM**，只把文本写进对话历史供后续追问；"该不该打扰、配什么表情动作"由调用方决定。
4. **在岗状态汇聚。** 接收 `POST /xiaozhi/presence/report` 并在内存维护每工位最新状态，超 30 秒无上报派生为 `stale`。
5. **桌面摄像头识别。** `GET /xiaozhi/presence/stream` 升级为 WebSocket，接收桌面 JPEG 流，在同一解码帧上运行 MediaPipe Pose 与 YuNet/SFace，并把稳定状态写入同一个 PresenceRegistry。

### desktop/ — 事件采集与看板

**不是大脑，不做 LLM 判断。** 三件事：

1. 往本机 Codex / Claude Code / WorkBuddy 的 JSON 配置注入带 `launchcrush-agent-hook` 标记的 hook，让它们在 SessionStart / PreToolUse / Stop 等时机回调一个落盘的 Node 脚本。
2. hook 脚本把事件原子落盘到 `userData/agent-hooks/inbox`，主进程 watch 消费并用**纯规则**（关键字、正则、事件名映射，见 `taskTracker.ts`）判定成 running / completed / failed / needs_user 四态，经 IPC 推给界面。
3. 应用根 Provider 独占摄像头，监测开启后以 5 FPS 生成最大 640×360 JPEG，经 IPC 交给主进程 WebSocket 客户端；切页、最小化、Server 断线和摄像头短暂中断都保留监测意图，只有手动关闭或退出应用才停止。

### firmware/ — 表演终端

**纯执行端，不做任何决策。** 开机 → 静默进表情模式（保证一直有张脸）→ 连 WiFi → 拉 OTA 拿服务器地址 → 与 Server 建 WebSocket **常连**并 30 秒一次 ping 保活（空闲也不断开，就是为了让 Server 能主动下发）。之后按下行消息表演，并把自身能力以 MCP 工具暴露给 Server 调用。设备没有 LLM、没有意图识别、没有记忆，也不认识桌面端——**唯一对端是 Server**。

### presence-agent/ — 在岗与本人识别

无桌面应用部署时的兼容 Python 命令行工具，**不是 desktop 的子模块，也不被 desktop 拉起**。它可独占一路摄像头，在本机同帧运行 MediaPipe Pose 与 YuNet/SFace，再把稳定状态 POST 给 Server。不能与桌面摄像头监测同时运行。默认产品链路使用 desktop → Server WebSocket，独立 Agent 只作为兼容入口。

## 关键接口契约

### Server 对外 HTTP（`server.http_port`，默认 8003）

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/xiaozhi/event/push` | 工作事件推送，最核心的对外入口 |
| GET | `/xiaozhi/event/devices` | 列出 WebSocket 在线设备，联调必用 |
| POST | `/xiaozhi/alert/ingest` | SAE 告警接入，触发机器人 + 飞书叫人 |
| POST | `/xiaozhi/alert/feishu/callback` | 飞书事件与卡片回调，人的回复由此进来 |
| GET | `/xiaozhi/alert/{alert_id}` | 查单条告警中继状态 |
| GET | `/xiaozhi/alert/health` | 告警中继依赖自检 |
| POST | `/xiaozhi/presence/report` | 在岗状态上报 |
| GET | `/xiaozhi/presence/{workstation_id}` | 查询工位最新状态 |
| WS | `/xiaozhi/presence/stream` | 桌面端主人注册与持续摄像头识别 |
| GET/POST | `/xiaozhi/ota/` | 简易 OTA，下发 WebSocket 地址 |
| GET/POST | `/mcp/vision/explain` | 视觉分析 |

`/xiaozhi/event/push` 请求体：

```jsonc
{
  "device_id": "dc:da:0c:26:9a:60",  // 必填
  "text": "Codex 任务已完成",          // 必填，上屏并写入对话历史
  "emotion": "happy",                 // 21 种表情之一，默认 neutral
  "status": "任务完成",                // 状态栏文字
  "speak": false,                     // 是否 TTS 播报；设备忙时自动降级为纯提示
  "silent": false,                    // 抑制提示音
  "action": "nod",                    // 机器人动作，见下表
  "restore_after": 6,                 // N 秒后恢复到设备基态
  "idle_animation": false             // 运行时开关随机空闲动画
}
```

### Server ↔ 机器人 WebSocket（`server.port`，默认 8000）

- 上行：`hello` / `ping` / `listen`(start·stop·detect) / `abort` / `mcp` / 二进制 Opus 帧
- 下行：`hello` / `pong` / `tts`(start·stop·sentence_start) / `stt` / `llm` / `mcp` / `alert` / `system`
- `tts.start` 是设备从 Idle 切到 Speaking 的**唯一触发**，而 `OnIncomingAudio` 只在 Speaking 态收包——**推送音频前必须先发它，否则真机上一片寂静**。

### 机器人 MCP 工具（由固件注册，Server 调用）

| 工具 | 参数 | 说明 |
|---|---|---|
| `self.robot.play_action` | `action` | `nod` `shake` `roll` / `look_*` 瞥一眼后回中 / `hold_*` 转过去保持不动 / `center` |
| `self.robot.set_emotion` | `emotion` | 21 种表情，会联动舵机 |
| `self.robot.set_idle_animation` | `enabled` | 随机空闲动画总开关，默认关 |

**服务端侧调用时工具名须用下划线版**（`self_robot_play_action`）——`MCPClient` 按 `sanitize_tool_name` 后的键存查，带点的原始名查不到。

## 硬件边界

实测 BOM：ESP32-S3 N16R8 / 0.96 寸 SSD1306 **128×64 单色** OLED / INMP441 **单颗**麦克风 / MAX98357A + 小喇叭 / 双轴 SG90 云台 / 板载 BOOT 与音量键。

**没有的东西**（设计时别假设它存在）：

- **没有摄像头**——机器人本体无任何视觉能力，视觉一律走桌面应用持有的 Mac 摄像头；无桌面部署才使用独立 presence-agent。
- **没有 PAJ7620U2 手势传感器**——`firmware/main/boards/esp32-s3n16r8-emoji/gesture_sensor.cc` 是上游带的死代码，开机探测失败后自动禁用。且该芯片原理上只能识别方向类动态手势，无法分类"竖大拇指"这种静态手型。
- **不能做声源定位**——单颗全向麦不含方位信息，"云台自动转向说话人"物理上不可行；转向只能按剧本编排。
- 屏幕画不出复合画面——16px 中文一行约 8 字、最多 4 行，无灰度。富信息一律落到电脑屏幕。

## 基本边界

- 只采集用户授权的必要工作状态，不默认保存完整代码、提示词或对话。
- 不让大模型直接生成舵机角度，机器人只执行经过测试的预设动作。
- 涉及授权或高风险操作时，只提醒用户，不代替用户确认。
- 优先保证三分钟核心演示稳定，不随意扩展与主线无关的功能。
- 模拟事件可以用于开发和演示，但必须明确标注为模拟或回放数据。
- 摄像头相关：原始帧只在桌面与配置的 Server 间以内存流传输，不落盘、不写日志；人脸 embedding 与模板内容不离开 Server 推理进程。

## 当前尚未接通的地方

这些是真实缺口，不是待办清单里的空话。动手前先确认是否已被别人补上：

1. **desktop 的事件到不了 Server。** 它算出的 `RobotActionIntent` 只在界面打印，没有任何代码把它发给 Server。"编码 Agent 事件 → 机器人反应"这条主链路中间是断的。
2. **在岗状态没有消费方。** desktop/presence-agent → Server 已跑通，但 Server 收下后只存进内存 Registry，没有 LLM 工具、推送编排或固件动作读过它。

## 项目约定

### 语言与文档

- 项目文档默认中文，代码标识符与协议字段保留英文。
- 文档流水线：先写 spec（`docs/superpowers/specs/YYYY-MM-DD-<slug>-design.md`），再写 plan（`docs/superpowers/plans/YYYY-MM-DD-<slug>.md`），实现完成后补 `docs/api/` 对接文档并回写 README。
- **plan 里的复选框状态不可信**——多数 plan 的任务框未勾选但代码早已合入。以代码为准。

### Git

- Conventional Commits：`type(scope): subject`，scope 用 `desktop` / `server` / `firmware`。
- 大改动的提交正文惯例：分段小标题 + 变更点 + **修复原因（说明失败机理）** + 验证证据（真机现象或单测数量）。

### 目录性质

`server/` 与 `firmware/` 是上游项目（xiaozhi-esp32-server、xiaozhi-esp32）的快照并入，未保留上游历史。它们自带的 README 与 docs 属于上游产物，不是本项目文档。
