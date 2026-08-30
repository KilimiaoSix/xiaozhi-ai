# 操作链路顺滑化设计（ops-smoothness）

日期：2026-08-28。目标：消除"整体操作链路不流畅"的三个结构性根因——配置无单一事实源、无统一启动/健康入口、关键状态不落盘。设备侧允许以模拟设备代替真机验证。

## 1. 桌面端配置中心（desktop）

**现状**：摄像头流读 `XIAOFEI_SERVER_URL`，飞书/机器人/关怀读 `DESKPET_SERVER`，番茄钟/告警客户端硬编码 `127.0.0.1:8003`；UI 的"Server 地址"面板连着永远抛错的 PlaceholderServerGateway。打包应用经 `open` 启动时 shell 环境变量不可靠传入，实际全靠"都在本机"兜底。

**设计**：主进程新增配置存储（`userData` 下原子写 JSON），字段：`serverUrl`、`deviceId`、`authToken`。**取值优先级：环境变量 > 配置文件 > 默认值**（env 保留给脚本/演示的一次性覆盖；兼容全部现有变量名）。五条链路（camera / feishu / robot+发现 / pomodoro / incident / wellbeing）一律经配置中心按次解析，不再各自读 env、不再硬编码。设置面板做实：IPC 读写配置，展示每个字段的生效值与来源（env 覆盖时明示）；serverUrl 变更后摄像头客户端断开重连新地址。不引入新 npm 依赖。

## 2. 统一启动器（仓库根 `gongban`）

子命令：`up`（预检 venv/camera 依赖/配置/端口 → 自动带 SSL_CERT_FILE → server.command start → 健康等待 → open 打包版桌面应用 → 报设备数）、`down`、`status`（服务进程/HTTP 健康/WS 在线设备/在岗状态/桌面应用/最近错误一屏）、`doctor`（复用 server.command doctor）、`demo <场景>`（见 §5）、`mock-device`（见 §4）。不写死绝对路径，可在任意克隆位置运行。

## 3. 服务端关键状态落盘（server）

沿用 away_ledger / incident_manager 的 `.tmp`+rename 原子写模式：

- **设备基态**（pushHandle 模块级 dict）：落 `data/` JSON，设备重连时恢复，摄像头链路随后自然纠偏。
- **番茄钟会话**：每次相位变迁持久化（相位、墙钟截止时刻、暂停态、轮次、配置）；重启后仍在相位内→按墙钟重算 monotonic 截止并恢复计时与画面；已过期→丢会话并在设备回连后推 idle 收屏。generation 语义不变。
- **告警中继记录**（alert_relay 全内存）：状态变迁即持久化，启动时恢复非终态记录并重建超时巡检基线；重启后飞书回复不再 ALERT_NOT_FOUND。同时把 alert_relay 的 stop() 注册进 on_cleanup（补齐生命周期不对称）。

## 4. 模拟设备（tools/mock_device.py）

连接 `ws://…:8000/xiaozhi/v1/` 完成 hello 握手、30s ping、注册 4 个 MCP 工具（play_action / set_emotion / set_idle_animation / pomodoro.show），把收到的 tts/mcp/alert 下行打印成结构化日志。用途：无真机验证 `gongban up/status/demo` 全链路与三端联调。明确标注为模拟设备。

## 5. 交互断头路补全（desktop）

- Codex 桌面版探针的"等待批准"接通机器人（仅状态跃迁沿触发，复用 needs_user 映射，TTL 600s）。
- 返岗汇总桌面面板：只读 `GET /xiaozhi/away/summary`（不清账），沿 IncidentPanel 模式（requestSeq 防乱序），经配置中心取地址。
- 4 个 mock 占位模块从 UI 隐藏（代码保留），假"模拟事件流"入口一并收起。

## 6. dev 模式摄像头根因

`npm run dev`（electron-forge start）下摄像头调不起来、打包后正常。本次先做根因定位（打包配置/Info.plist/TCC 归属对比，附证据），能以配置修复则修；摄像头画面等主观项进人工验收清单。

## 边界（不做）

固件改动、把其余 3 个 mock 模块做实、CI、告警双链路合并、上游升级。
