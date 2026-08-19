# 工位在岗与本人识别 Agent

`presence-agent` 是无桌面应用部署时的兼容 CLI。默认产品链路由 Electron 独占摄像头并把 JPEG 流发送到 Server；不要同时开启桌面监测和本 CLI。CLI 在一个采集循环中使用 MediaPipe 判断是否有人，并使用 YuNet/SFace 核验本人，只上报稳定状态、相似度和少量聚合指标，不会上传画面、截图、完整 landmark、人脸 embedding 或本人模板。

## macOS / Linux 入口

Windows 用 `*.ps1`，macOS / Linux 用同名 `*.sh`，参数改成长选项：

| Windows | macOS / Linux |
| --- | --- |
| `.\run.ps1 -ServerUrl ... -WorkstationId ...` | `./run.sh --server-url ... --workstation-id ...` |
| `.\enroll-face.ps1 -Force` | `./enroll-face.sh --force` |
| `..\run-presence-stack.ps1 -EnrollOwner` | `../run-presence-stack.sh --enroll-owner` |

macOS 上有三个平台差异已经在代码里处理，但环境要求需要自己满足：

- **Python 版本**：`numpy==2.5.2` 要求 Python 3.12+，系统自带 `python3` 通常更老。
  用 `--python` 指定，例如 `./run.sh --python /opt/homebrew/bin/python3.14`；`setup.sh` 会在建 venv 前校验。
- **摄像头授权**：OpenCV 走 AVFoundation，未授权时只会返回打开失败并不断重试。
  首次运行看到 `not authorized to capture video` 时，去 系统设置 > 隐私与安全性 > 摄像头
  勾选运行脚本的终端，然后重新运行。
- **MediaPipe 走 Metal**：macOS 轮子把姿态检测编译成 Metal 路径，默认 CPU delegate 会直接 abort，
  因此 `PoseDetector` 在 macOS 上使用 GPU delegate 并改传 SRGBA 帧，其它平台保持原有 CPU + SRGB。

## 登记与删除本人模板

首次使用先登记本人：

```powershell
.\enroll-face.ps1
```

模板默认保存在 `.runtime\owner_template.npz`，已被 Git 忽略。重新登记使用 `-Force`。也可以从仓库根目录登记并直接启动：

```powershell
.\run-presence-stack.ps1 -WorkstationId desk-tfzhang11 -EnrollOwner
```

删除模板使用根脚本的 `-DeleteFaceTemplate`。没有模板时 Agent 仍运行在岗检测并报告 `not_enrolled`。

## 单独运行

首次运行会创建 `.venv`，要求 Python 3.12+（NumPy 2.5.2、OpenCV contrib 5.0.0.93、MediaPipe 1.0.1）。Server 有自己的 venv（Python 3.10，因 torch 依赖停在 NumPy 1.x），只共享 mediapipe/opencv 版本并以 `pip install -r requirements-camera.txt` 引入本包代码——两个 venv 是有意分开的，不要互相安装对方的 requirements：

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

需要本地预览时添加 `-Preview`，窗口同时显示 presence 和 identity 状态。长期运行默认无窗口。

## Python 参数

PowerShell 参数会转换为 Python CLI。除原有参数外，人脸识别可通过 `-FaceThreshold`、`-FaceHits`、`-NoFaceDelay`、`-FaceTemplate` 和 `-DisableFaceVerification` 调整；对应 Python 参数包括：

- `--server-url URL`
- `--workstation-id ID`
- `--camera INDEX`
- `--width PIXELS`
- `--height PIXELS`
- `--absent-after SECONDS`
- `--heartbeat-seconds SECONDS`
- `--camera-retry-seconds SECONDS`
- `--model PATH`
- `--face-detector-model PATH`
- `--face-recognizer-model PATH`
- `--face-template PATH`
- `--face-threshold SCORE`
- `--face-hits COUNT`
- `--no-face-delay SECONDS`
- `--disable-face-verification`
- `--preview`
- `--smoke-frames COUNT`

状态含义和完整协议见 `docs/api/camera-presence-api.md`。

人脸识别没有活体检测，照片、屏幕或视频可能骗过识别。`owner` 只能作为低风险交互提示，不能单独用于门禁、解锁、考勤、支付或其他高风险决策。
