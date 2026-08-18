# 工伴·桌面精灵

AI Agent 驱动的打工人桌面宠物，由桌面应用、Server 和 ESP32-S3 双轴机器人组成。
产品定位与体验目标见 [AGENTS.md](AGENTS.md)。

## 通信架构

```text
Electron 桌面端 ── HTTP ──> Server ── WebSocket ──> ESP32-S3 机器人
```

- 桌面端不直接连接机器人。
- Server 与机器人只使用 WebSocket 通信。
- 桌面端已支持本机 Codex、Claude Code 和腾讯 WorkBuddy 的任务 Hook；Server HTTP 与机器人反馈发送仍为 Mock/功能占位。

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

## 运行上位机 server

- 入口：`server/main/xiaozhi-server/app.py`
- 依赖：`server/main/xiaozhi-server/requirements.txt`
- 部署方式、管理后台与各模块说明见 [`server/README.md`](server/README.md) 与 [`server/docs/`](server/docs/)。

```bash
cd server/main/xiaozhi-server && python app.py
```

> 私有配置放在 `server/main/xiaozhi-server/data/.config.yaml`，该目录已被 `.gitignore` 排除，
> 不要把真实密钥提交进仓库。

## 运行工位在岗与本人识别

Windows 本地演示可从仓库根目录一键启动：

```powershell
.\run-presence-stack.ps1 -WorkstationId desk-tfzhang11
```

macOS / Linux 使用同名 `.sh` 入口，参数改为长选项：

```bash
./run-presence-stack.sh --workstation-id desk-tfzhang11
```

macOS 需要注意两点：

- 依赖固定在 `numpy==2.5.2`，要求 **Python 3.12 或更高**；系统自带的 `python3` 常常是 3.9/3.10，用
  `--python /opt/homebrew/bin/python3.14` 显式指定即可，`setup.sh` 会在建 venv 前检查版本。
- 首次运行会弹出摄像头授权，未授权时 OpenCV 只会报打开失败。允许当前终端后重试：
  系统设置 > 隐私与安全性 > 摄像头。

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

首次运行自动创建 `presence-agent/.venv` 并安装固定版本依赖。同一采集循环完成 MediaPipe Pose 与 YuNet/SFace 推理，避免两个进程争用摄像头。摄像头帧、完整人体关键点、人脸 embedding 和本人模板只在本机使用，不上传到 Server；本人模板保存在被 Git 忽略的 `presence-agent/.runtime/owner_template.npz`。设计与接口见：

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
