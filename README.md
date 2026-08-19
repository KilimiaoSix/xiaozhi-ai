# 工伴·桌面精灵

AI Agent 驱动的打工人桌面宠物，由桌面应用、Server 和 ESP32-S3 双轴机器人组成。
产品定位与体验目标见 [AGENTS.md](AGENTS.md)。

## 通信架构

```text
Electron 桌面端 ── HTTP / 摄像头 WebSocket ──> Server ── WebSocket ──> ESP32-S3 机器人
```

- 桌面端不直接连接机器人。
- 桌面端通过 HTTP 发送工作事件，通过带背压的 WebSocket 持续发送摄像头 JPEG。
- Server 对同一帧执行人体在场和主人核验，再通过 WebSocket 与机器人通信。
- 桌面端已支持本机 Codex、Claude Code 和腾讯 WorkBuddy 的任务 Hook，也可通过飞书 CLI 读取当前用户的今日日程与未完成任务。

## 目录

| 目录 | 角色 | 说明 |
| --- | --- | --- |
| [`desktop/`](desktop/) | 桌面端 | Electron Forge + Vite + React + TypeScript。 |
| [`server/`](server/) | **上位机** | 服务端，基于 xiaozhi-esp32-server。负责事件接入、Agent 判断、设备通信与管理后台。 |
| [`firmware/`](firmware/) | **下位机** | ESP32-S3 双轴机器人固件，基于 ESP-IDF。负责执行预设动作、表情、灯光与声音。 |

## 运行桌面端

```bash
cd desktop
npm install
npm run dev
```

### 验证与打包

```bash
cd desktop
npm test
npm run typecheck
npm run package
```

macOS arm64 应用会生成到 `desktop/out/小飞桌面机器人-darwin-arm64/`。

## AI Agent 任务监控

桌面端启动后会自动发现本机的 Codex、Claude Code 和 WorkBuddy，但不会自行修改任何工具配置。用户需要在首页的“AI Agent 任务监控”区域点击“自动发现并接入”，或逐项点击“接入 Hook”。接入后，任意项目中的任务会统一显示为：

- 运行中
- 已完成
- 失败
- 需要用户输入、决策、确认或授权

任务卡会保留并显示 Hook 提供的完整提示词、工作目录、错误和等待原因。数据只在本机处理，不会由 Hook 上传到第三方；Hook 也不会替用户批准权限请求或直接控制机器人。

Codex 页面内的 Computer Use 应用授权不会触发 Hook。macOS 版小飞会在用户授予“辅助功能”权限后，只读匹配该授权卡片的标题与按钮标签，并临时显示“需要你”；卡片消失后恢复原任务状态。检测不会点击批准按钮，也不会读取代码、提示词或对话正文。

### 配置位置与撤销

| 工具 | 默认 Hook 配置 |
| --- | --- |
| Codex | `~/.codex/hooks.json` |
| Claude Code | `~/.claude/settings.json` |
| WorkBuddy | 优先使用已有的 `~/.workbuddy/settings.json` 或 `~/.codebuddy/settings.json` |

- 写入已有配置前，会在原文件旁生成带 UTC 时间戳的 `*.launchcrush.bak` 备份。
- LaunchCrush 只添加带 `launchcrush-agent-hook` 标记的 handler，保留原有字段和用户 Hook。
- 点击工具卡片上的“移除 Hook”只删除 LaunchCrush 自己的 handler，并在变更前再次备份。
- 配置 JSON 无法解析时会拒绝覆盖，并在桌面端显示错误。

### 本机数据与诊断

监控数据位于 Electron 的 `userData/agent-hooks/` 目录（macOS 通常在 `~/Library/Application Support/小飞桌面机器人/agent-hooks/`），其中：

| 路径 | 内容 |
| --- | --- |
| `inbox/` | Hook 写入、等待桌面端消费的原子事件文件 |
| `history/` | 按日保存的原始本地事件，保留 30 天，20 MiB 自动轮转 |
| `state/tasks.json` | 桌面任务快照，用于应用重启恢复 |
| `quarantine/` | 无法解析的事件文件 |
| `diagnostics/runner-errors.ndjson` | Hook runner 的非阻塞错误记录 |

桌面端离线期间产生的 inbox 事件会在下次启动时恢复，但不会补播已经过期的机器人动作。当前桌面端只生成 `quiet_companion`、`task_completed`、`task_failed`、`needs_user` 预设动作意图；通过 Server HTTP 发送到 ESP32-S3 的真实链路将在后续接入。

## 飞书 CLI 工作台

桌面端在 Electron 主进程中调用本机 `lark-cli`，渲染层只能通过只读 IPC 检查连接和刷新数据，不会执行创建、更新、完成或删除操作。当前工作台读取：

- 当前飞书用户身份与 CLI 版本；
- 当前用户主日历中的今日日程；
- 分配给当前用户的未完成任务、所属任务清单、截止时间和任务链接。

首次使用前需在终端完成飞书 CLI 配置和用户授权：

```bash
lark-cli config init
lark-cli auth login --scope "task:task:read calendar:calendar:readonly"
```

启动桌面端后，可在“飞书任务与会议”区域检查连接并刷新。日历或任务其中一项缺少权限时，另一项仍会正常展示，界面会列出缺失 scope 和最小权限授权命令。应用只读取结构化任务与日程字段，不保存 access token，也不采集完整飞书对话或文档内容。

## 飞书每日关注晨报

Server 提供三个只读接口，聚合时间窗内的消息、`@我` 消息和当天日程，生成最多三条待关注项：

```text
POST /xiaozhi/morning-brief/preview
GET  /xiaozhi/morning-brief/latest
GET  /xiaozhi/morning-brief/health
```

默认关闭（`morning_brief.enabled: false`）。接入方在自己的飞书租户用任意自建应用即可，需要开通四个消息权限，日历源另需一个：

```text
search:message
im:message:readonly
im:message.p2p_msg:get_as_user
im:message.group_msg:get_as_user
calendar:calendar:readonly       # 仅日历源需要，拿不到就设 calendar_enabled: false
```

从开通权限到本机联调的完整步骤、常见错误码和接口字段说明见
[`docs/api/飞书每日关注晨报接口.md`](docs/api/飞书每日关注晨报接口.md)，
设计取舍见 [`docs/技术方案-飞书每日关注晨报.md`](docs/技术方案-飞书每日关注晨报.md)。

用户令牌不要写进 `config.yaml`，通过环境变量或被 Git 忽略的 `data/.config.yaml` 提供。
配好后可用仓库内脚本一次性验证三个接口：

```bash
cd server/main/xiaozhi-server
FEISHU_USER_ACCESS_TOKEN=<token> FEISHU_SELF_OPEN_ID=<open_id> \
  python run_morning_brief_check.py
```

## 告警值班中继

线上告警进来后，**机器人抬头提醒人 + 飞书发卡片给值班人**；人在飞书回一句「帮我查」，
Server 就调起**本机的 Claude Code**（复用 `diagnose-sae-alert` skill）做只读根因诊断，
再把结论回帖成飞书卡片，同时让机器人点头播报一句话结论。

```text
POST /xiaozhi/alert/ingest             # SAE 告警接入
POST /xiaozhi/alert/feishu/callback    # 飞书事件与卡片回调（人的回复）
GET  /xiaozhi/alert/{alert_id}         # 查中继状态
GET  /xiaozhi/alert/health             # 依赖自检
```

三条边界：**人不点头就不开跑诊断**（状态机层面禁止）、**全程只读不自动修复**、
**查不出来就回失败卡片，绝不编根因**。诊断在本机子进程里跑，所以 Server 必须与
Claude Code 同机部署，且那台机器能连内网 SAE。

机器人应用需开通 `im:message:send_as_bot` 和 `im:message.reaction:write`，
密钥走 `FEISHU_BOT_APP_ID` / `FEISHU_BOT_APP_SECRET` 环境变量或 `data/.config.yaml`。
完整配置、状态机、硬件语汇和失败模式见
[`docs/api/告警值班中继接口.md`](docs/api/告警值班中继接口.md)。

诊断用的 skill 和跨平台日志脚本随仓库分发在
[`.claude/skills/diagnose-sae-alert/`](.claude/skills/diagnose-sae-alert/)，
克隆下来就有，不依赖任何个人机器上的配置。换机器只需要三样：装 Claude Code、
配 `SAE_AUTHORIZATION='Bearer <jwt>'`（或写进 `~/.sae/sae-token.env`）、
把被诊断服务的源码目录填进 `alert_relay.diagnosis.source_dirs`。缺哪样，
`GET /xiaozhi/alert/health` 和 `run_alert_relay_check.py` 的第 0 步都会直接列出来，
真实告警也会**秒级失败**并在卡片上写明，不会空转到超时。

改完代码可以先跑一遍全链路模拟（假飞书 + 假机器人，秒级）：

```bash
cd server/main/xiaozhi-server
python run_alert_relay_check.py              # 假 CLI，只验管道
python run_alert_relay_check.py --real-cli   # 用真的 Claude Code 跑诊断
```

## 运行上位机 server

- 入口：`server/main/xiaozhi-server/app.py`
- 依赖：`server/main/xiaozhi-server/requirements.txt`
- 部署方式、管理后台与各模块说明见 [`server/README.md`](server/README.md) 与 [`server/docs/`](server/docs/)。

```bash
cd server/main/xiaozhi-server && python app.py
```

摄像头识别统一运行在 Server 的 Python 3.10 进程中，不需要额外 Python worker。已有 Server 虚拟环境增加摄像头依赖：

```bash
cd server/main/xiaozhi-server
python -m pip install -r requirements-camera.txt
python -m pip check
```

使用 pyenv 时，一个项目固定一个版本即可：

```bash
pyenv install -s 3.10.16
pyenv local 3.10.16
python -m venv .venv
source .venv/bin/activate
```

统一依赖基线为 Python 3.10、NumPy 1.26.4、OpenCV contrib 4.11.0.86 和 MediaPipe 0.10.35。

> 私有配置放在 `server/main/xiaozhi-server/data/.config.yaml`，该目录已被 `.gitignore` 排除，
> 不要把真实密钥提交进仓库。

## 桌面摄像头监测与主人录入

启动 Server 和桌面端后，在桌面应用“摄像头”页完成主人录入，再打开“实时监测”开关。桌面端独占摄像头，以 5 FPS、最大 640×360 的 JPEG 流发送给 Server。监测会跨页面切换、窗口最小化、Server 重启和摄像头短暂中断持续运行，直到手动关闭开关或退出应用。

Server 地址和认证只提供给 Electron 主进程：

```bash
export XIAOFEI_SERVER_URL=http://127.0.0.1:8003
export XIAOFEI_SERVER_AUTH_TOKEN='<server.auth_key>'
cd desktop
npm run dev
```

主人注册连续接受 20 个合格人脸样本，以其中 18 个样本生成模板。原始帧只在内存中流转，不写入磁盘。该识别没有活体检测，只能用于低风险提醒和个性化反馈。

### 独立 presence-agent（兼容工具）

没有桌面端的 Windows 部署仍可从仓库根目录启动独立兼容工具：

```powershell
.\run-presence-stack.ps1 -WorkstationId desk-tfzhang11
```

首次使用先登记本人，登记成功后脚本继续启动完整检测链路：

```powershell
.\run-presence-stack.ps1 -WorkstationId desk-tfzhang11 -EnrollOwner
```

替换模板时增加 `-ForceEnrollment`；删除本地模板使用 `-DeleteFaceTemplate`。未登记时在岗检测仍正常运行，接口返回 `identity.state=not_enrolled`。

脚本会优先复用 `http://127.0.0.1:8003` 上已运行且支持 presence API 的完整 Server。未找到时默认启动复用同一 Registry/Handler 的轻量 presence-only Server；若要由脚本启动完整 Server，显式传入其 Python 解释器：

```powershell
.\run-presence-stack.ps1 `
  -WorkstationId desk-tfzhang11 `
  -ServerPython C:\path\to\python.exe
```

远程或 Docker Server 场景只启动本机 sidecar：

```powershell
$env:PRESENCE_AUTH_TOKEN = "<server.auth_key>"
.\presence-agent\run.ps1 `
  -ServerUrl http://server-host:8003 `
  -WorkstationId desk-tfzhang11
```

不要同时运行桌面摄像头监测和独立 Agent，否则会争用摄像头。独立 Agent 与 Server 摄像头能力使用同一 Python 3.10 依赖基线；摄像头帧、完整人体关键点、人脸 embedding 和本人模板只在本机使用。模板保存在被 Git 忽略的 `presence-agent/.runtime/owner_template.npz`。设计与接口见：

- [`docs/superpowers/specs/2026-08-18-camera-presence-integration-design.md`](docs/superpowers/specs/2026-08-18-camera-presence-integration-design.md)
- [`docs/superpowers/specs/2026-08-18-face-verification-integration-design.md`](docs/superpowers/specs/2026-08-18-face-verification-integration-design.md)
- [`docs/api/camera-presence-api.md`](docs/api/camera-presence-api.md)

## 构建下位机 firmware

- 芯片：`esp32s3`（`CONFIG_IDF_TARGET="esp32s3"`）
- 框架：ESP-IDF v5.5.3
- 依赖：`firmware/managed_components/` 已随仓库提交，内网无法访问乐鑫组件仓库时也可直接构建；
  版本以 `firmware/dependencies.lock` 为准。

烧录脚本见 `firmware/scripts/flash.sh`，`--help` 查看完整用法：

```bash
cd firmware && ./scripts/flash.sh /dev/cu.wchusbserial110 project
```

> `scripts/flash.sh` 里的 `IDF_DIR`、`FACTORY_BIN` 默认值是原作者本机的绝对路径，
> 其他人使用时通过同名环境变量覆盖，例如 `IDF_DIR=~/esp/esp-idf ./scripts/flash.sh ...`。

## 上游来源

| 目录 | 上游仓库 |
| --- | --- |
| `firmware/` | https://gitee.com/pengjie0668/esp32s3-ai-deskbot-kit |
| `server/` | https://github.com/xinnan-tech/xiaozhi-esp32-server |

`firmware/` 与 `server/` 以快照方式并入，未保留上游提交历史；
编译产物（`build/`、`build-macos/`）不入库。
