# CLAUDE.md

**先读 [AGENTS.md](AGENTS.md)** —— 系统架构、各组件职责、接口契约、硬件边界都在那里，本文件不重复。
这里只写在这个仓库里干活的方式：命令、验证、以及会真的把你绊倒的坑。

## 仓库形态

monorepo，**没有根 package.json、没有 Makefile、没有 CI**。所有命令都得 `cd` 进子目录跑。
`server/` 和 `firmware/` 是上游快照并入，改它们等于改 vendored 代码——**不要为了"顺手"去重构上游文件**，只改实现需求必需的部分，且在提交正文里说明改动原因。

## 常用命令

### desktop/（Electron + React + TypeScript）

```bash
cd desktop && npm install        # node_modules 不入库，新克隆必做
cd desktop && npm run dev        # 开发运行
cd desktop && npm test           # vitest，44 个测试文件
cd desktop && npm run typecheck
```

提交前的质量门是三件套：`npm test && npm run typecheck && npm run package`。
只跑单个文件：`npm test -- src/modules/features/coding-agent-status/agent-hooks/xxx.test.ts`

### server/（Python）

```bash
cd server/main/xiaozhi-server && pip install -r requirements.txt
cd server/main/xiaozhi-server && python app.py        # WebSocket 8000 + HTTP 8003
cd server/main/xiaozhi-server && python -m pytest tests/
```

只联调摄像头在岗、不想等 LLM/ASR/TTS 加载：`python presence_server.py`

手势审批与分心检测的视觉模型不入库，新机器各跑一次：
`python scripts/download_gesture_model.py --verify` 与 `python scripts/download_detection_model.py`

桌面端摄像头流的推理代码从 presence-agent 导入，新机器必须装进 server venv（**必须带 `--no-deps`**，
原因见 requirements-camera.txt 的注释——py3.10+torch 的 numpy 1.x 与 opencv 5 声明的 numpy>=2 无公共解）：
`pip install --no-deps -r requirements-camera.txt`，漏装的症状是桌面端报 "camera inference runtime is unavailable"。

### firmware/（ESP-IDF）

```bash
firmware/scripts/check_macos_env.sh                              # 先验环境
cd firmware && ./scripts/flash.sh /dev/cu.usbmodem101 project    # 编译+烧录，不擦 NVS
cd firmware && ./scripts/monitor.sh /dev/cu.usbmodem101          # 串口日志，Ctrl+] 退出
```

只编译不烧录（**不需要插板子**，改完先编译能省一轮往返）：

```bash
source $IDF_DIR/export.sh && idf.py -C firmware -B firmware/build-macos build
```

脚本里写死了作者本机路径，可用环境变量覆盖：`IDF_DIR` / `BUILD_DIR` / `FACTORY_BIN`。

### 联调

```bash
curl http://127.0.0.1:8003/xiaozhi/event/devices     # 设备真的常连上来了吗
```

推一条事件看机器人反应（字段见 AGENTS.md）：

```bash
curl -X POST http://127.0.0.1:8003/xiaozhi/event/push -H "Content-Type: application/json" -d '{"device_id":"dc:da:0c:26:9a:60","text":"任务完成","emotion":"happy","action":"nod"}'
```

## 坑

### 只有一块开发板

固件改动**要攒成批次再烧**。每轮编译加烧录几分钟，而且烧录期间没法联调。改完先只编译验证语法，攒够一批再烧。

### 密钥与大文件

- `server/main/xiaozhi-server/data/.config.yaml` 含 LLM API key，**已 gitignore，永远不要提交**。所有本地配置写这里，不要改 `config.yaml`（它与本文件递归合并，`data/` 优先）。
- SenseVoiceSmall 模型权重 936MB、presence-agent 的人脸底片，都已 gitignore。

### 服务端配置的隐形前提

- `enable_websocket_ping` 必须为 true，否则设备的 30 秒心跳收不到 pong，`close_connection_no_voice_time`（默认 120 秒）会把常连设备踢掉。
- `script_mode: true` 时 ASR 文本不进 LLM，只保留"设备在听"的画面——**拍摄时开，开发时务必关**，否则你会以为 LLM 坏了。
- 改 `selected_module` 后必须重启进程，provider 是启动时一次性实例化的。

### 真机验证要看对地方

- 舵机转动**不打日志**的那行在 `servo_controller.cc` 里是注释掉的；现在每段移动会打 `MoveTo (x,y) -> (x,y)`。别再拿"没有 ServoController 日志"断定舵机没动。
- 服务端进程可能不是你启的那个（多人多会话）。确认方法：`lsof -nP -iTCP:8003 -sTCP:LISTEN` 拿到 PID 后 `lsof -p <PID>` 看它的 stdout 指向哪个日志文件。项目自带的 `tmp/server.log` 总是活的。
- 设备每 10 秒的随机空闲动画默认已关。若看到机器人自己动，先确认是不是被 `idle_animation` 打开了，别误当成事件响应。

### 并发协作

多人同时改这个仓库。改共享文件（`app.py`、`core/http_server.py`）前先 `git fetch` 看有没有新提交，**尤其别整文件覆盖**——那两个文件里同时有事件推送和 presence 两套接线。

## 写代码的约定

- 提交信息用 Conventional Commits，scope 是 `desktop` / `server` / `firmware`。大改动的正文要写清**失败机理**和**验证证据**，不要只列改了什么。
- 涉及行为变更时先让测试红，再实现。desktop 侧用 vitest，Python 侧用 pytest。固件没有测试框架，验证方式是编译 + 真机串口日志，把观察到的现象写进提交正文。
- 文档默认中文，代码标识符与协议字段保留英文。
- 发现 AGENTS.md 与代码不符时**顺手修 AGENTS.md**，别绕过去。

## 与硬件相关的判断，先查 AGENTS.md 的「硬件边界」

那里列了这套 BOM **没有**什么。常见的踩坑是假设机器人有摄像头、有手势传感器、能听声辨位——都没有。设计方案前先看一眼，能省掉整轮返工。
