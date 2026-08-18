# 工位在岗与本人识别 Agent

`presence-agent` 在一个摄像头采集循环中使用 MediaPipe 判断是否有人，并使用 YuNet/SFace 判断单张人脸是否为本机登记的本人。它只上报稳定状态、相似度和少量聚合指标，不会上传画面、截图、完整 landmark、人脸 embedding 或本人模板；除本地本人模板外也不会保存这些数据。

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
