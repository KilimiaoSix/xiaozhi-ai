# 工位在岗检测 Agent

`presence-agent` 在本机使用摄像头和 MediaPipe 判断工位前是否有人，只向 launchcrush Server 上报状态、时间和少量聚合诊断指标。它不会上传或保存摄像头画面、截图、完整 landmark 或身份信息。

## 单独运行

首次运行会自动创建独立 `.venv` 并安装固定版本依赖：

```powershell
cd presence-agent
.\run.ps1 -ServerUrl http://127.0.0.1:8003 -WorkstationId desk-tfzhang11
```

连接远程或 Docker Server：

```powershell
$env:PRESENCE_AUTH_TOKEN = "<server.auth_key>"
.\run.ps1 -ServerUrl http://server-host:8003 -WorkstationId desk-tfzhang11
```

`PRESENCE_AUTH_TOKEN` 只通过进程环境传递，不要写入仓库。

## 验证摄像头

处理 30 个成功帧后自动退出：

```powershell
.\run.ps1 -ServerUrl http://127.0.0.1:8003 -WorkstationId desk-smoke -SmokeFrames 30
```

需要本地预览时添加 `-Preview`。长期运行默认无窗口。

## Python 参数

PowerShell 参数会转换为 Python CLI。常用参数为 `-ServerUrl`、`-WorkstationId`、`-Camera`、`-Width`、`-Height`、`-AbsentAfter`、`-HeartbeatSeconds`、`-CameraRetrySeconds`、`-Model`、`-Preview` 和 `-SmokeFrames`；对应的 Python CLI 为：

- `--server-url URL`
- `--workstation-id ID`
- `--camera INDEX`
- `--width PIXELS`
- `--height PIXELS`
- `--absent-after SECONDS`
- `--heartbeat-seconds SECONDS`
- `--camera-retry-seconds SECONDS`
- `--model PATH`
- `--preview`
- `--smoke-frames COUNT`

状态含义和完整协议见 `docs/api/camera-presence-api.md`。
