# 桌面摄像头流式双识别设计

**实施计划：** [`../plans/2026-08-18-desktop-camera-stream-recognition.md`](../plans/2026-08-18-desktop-camera-stream-recognition.md)

**执行图：** [`../graphs/2026-08-18-desktop-camera-stream-recognition.json`](../graphs/2026-08-18-desktop-camera-stream-recognition.json)

## 1. 背景与现状

当前仓库已经存在两条尚未打通的摄像头链路：

- 桌面端 `b1883b4` 提供“主人录入”和“实时监测”界面。主人录入向不存在的
  `POST /api/identity/enroll` 上传单张 JPEG；实时监测用 `setInterval` 每秒向不存在的
  `POST /api/vision/frames` 上传单张 JPEG。
- Server 最新提交 `115c32c`、`194acbd` 提供人体在场和本地人脸核验。当前由独立
  `presence-agent` 独占摄像头，在本机完成 MediaPipe Pose、YuNet 和 SFace 推理，再把
  状态上报给 Server；Server 不接收画面。

因此桌面 UI、Server presence registry 和最新双识别能力虽然都已存在，但真实演示仍需
分别启动并争用摄像头，桌面端的注册及监测接口也无法成功。

## 2. 目标

1. 桌面端成为摄像头唯一所有者。用户打开监测开关后持续监测，跨页面导航、窗口最小化、
   Server 短暂断线均不得自动停止；只有用户明确关闭开关或应用进程退出才释放摄像头。
2. 用一条可背压的 WebSocket 二进制帧流替换每秒一次的 HTTP 定时拍照。
3. Server 对每个被接受的监测帧同时执行人体在场和人脸核验。
4. 保留现有 PC“主人录入”入口，但改为 3 到 5 秒多帧注册，生成与最新提交兼容的
   单主人 SFace 模板。
5. 实时监测同时展示人脸是否存在、稳定身份状态、匹配度、人体在场状态和传输指标。
6. 继续复用 `PresenceRegistry` 的查询、stale 和兼容语义。

## 3. 非目标

- 不支持多用户人脸库；本期只有一个可覆盖更新的“主人”模板。
- 不增加活体检测。匹配结果只能用于低风险提醒和个性化反馈，不能用于授权、考勤、
  门禁、支付或其他高风险决策。
- 不把视频或截图保存到磁盘，不把原始帧、完整人体关键点、人脸 embedding 写入日志。
- 不引入 WebRTC、远程 ICE/TURN 或音视频录制。
- 不改变 ESP32 机器人动作协议，也不由识别模块直接下发机器人动作。

## 4. 总体架构

```text
App-level CameraMonitoringProvider + getUserMedia
  -> requestVideoFrameCallback + Canvas JPEG
  -> 受控 IPC（start / frame / stop / event）
  -> Electron Main CameraStreamClient
  -> WebSocket /xiaozhi/presence/stream
  -> Server 最新帧槽（容量 1）
  -> 同一解码帧
       -> MediaPipe Pose -> PresenceTracker
       -> YuNet/SFace -> FaceVerifier 或 EnrollmentCollector
  -> PresenceRegistry + WebSocket result/progress
  -> Electron Main -> Renderer 状态界面
```

Electron 主进程负责网络连接，延续现有“渲染进程不直接访问 Server”的边界，并允许在
WebSocket 握手时使用 Server 的认证 header。WebSocket 使用成熟的 `ws` 客户端；Server
继续使用已有 `aiohttp`。

主进程从 `XIAOFEI_SERVER_URL` 读取 Server HTTP 基址，默认
`http://127.0.0.1:8003`，并转换成同 host/port 的 `ws` 或 `wss` 地址；认证 Token 只从主
进程环境变量 `XIAOFEI_SERVER_AUTH_TOKEN` 读取。两者都不通过 preload 暴露给 renderer，
日志也不得打印 Token。摄像头注册和监测不再保留旧的固定本地 HTTP client。

Server 在现有 Python 3.10 进程中直接复用 `presence-agent` 的 Pose、YuNet/SFace、模板和
状态算法，不启动额外 Python 进程。整个链路统一使用 `numpy==1.26.4`、
`mediapipe==0.10.35` 和 `opencv-contrib-python==4.11.0.86`。该组合已通过依赖解析、
`pip check`、现有 presence-agent 测试以及真实 YuNet/SFace 模型初始化验证；当前代码未使用
OpenCV 5 或 NumPy 2 专属 API。原有摄像头 CLI 和 PowerShell 入口继续使用同一套版本。
MediaPipe 1.0.1 的 macOS ARM wheel 在真实 PoseLandmarker 初始化时会进入不可捕获的 Metal
`abort(134)`；0.10.35 使用相同 Tasks API，并已通过同一模型的创建/关闭 smoke。

监测意图和摄像头生命周期由挂在应用根节点的 `CameraMonitoringProvider` 持有，不由
`CameraPage` 持有。Provider 使用常驻但不可见的 video/canvas 生产帧；摄像头页面只订阅
状态，并在可见时把同一个 MediaStream 附加到预览 video。页面卸载不会销毁 Provider。

## 5. 桌面端设计

### 5.1 帧生产

- 监测和注册均从同一个 `MediaStream` 与 `<video>` 读取画面。
- 使用 `requestVideoFrameCallback` 驱动采样，不再创建 `setInterval`。
- 目标采样速率为 5 FPS，编码上限为 640×360、JPEG quality 0.72。
- 同一时刻最多有一个 JPEG 编码或 IPC 提交在途；繁忙时跳过当前视频帧。
- 主进程 WebSocket 的 `bufferedAmount` 超过 1 MiB 时拒绝新帧并累计 dropped，不排队。
- 帧对象只包含 JPEG bytes；sequence 和接收时间由主进程会话维护。
- Provider 中的 `monitoringEnabled` 表示用户意图。一旦为 true，即使 WebSocket 暂时离线
  或摄像头轨道短暂中断也保持 true，并持续执行恢复流程，不能因一次错误把开关复位。

### 5.2 IPC 边界

预加载层只暴露以下受控能力：

- `camera.startRecognitionStream(options)`：创建 WebSocket 并发送 start 控制消息。
- `camera.sendRecognitionFrame(jpeg)`：尝试发送一帧，返回 `sent` 或 `dropped`。
- `camera.stopRecognitionStream()`：幂等关闭连接并清除退避计时器。
- `camera.onRecognitionEvent(listener)`：订阅连接、进度、结果和错误事件，并返回取消函数。

只有用户关闭监测开关或应用退出才对监测会话调用 stop。页面卸载、切回首页、窗口最小化
和普通连接错误均不得调用 stop。注册与监测互斥，同一窗口不允许同时开启两个识别会话；
监测开启时“主人录入”入口禁用，并提示先手动关闭监测开关，不能由切换页面隐式停止。

主进程同步保存本次运行期的 monitoring active 标志。监测中点击主窗口关闭按钮时窗口只
最小化而不销毁 renderer；用户可从任务栏或 Dock 恢复。只有明确退出应用或操作系统终止
进程时才允许销毁窗口。应用重启后监测默认关闭，不在未经用户再次操作时自动打开摄像头。

### 5.3 注册交互

现有“主人录入”入口保留，但不再先拍一张照片再上传：

1. 用户输入名称并点击“开始注册”。
2. 若监测开关仍开启，注册按钮不可用；用户必须先手动关闭监测。
3. 桌面启动 `mode=enrollment` 的帧流，并显示已接受样本数和质量提示。
4. Server 只接受恰好一张、尺寸足够且清晰的人脸；每 200 ms 最多接收一个样本。
5. 累计 20 个样本后剔除与初始中心最不一致的 2 个，保存 18 样本中心模板。
6. 成功后 Server 返回 profile/sample 标识和创建时间，桌面停止注册帧流与摄像头并显示成功。
7. 用户取消、切页或失败时不覆盖旧模板；重新注册成功时原子替换旧模板。

### 5.4 监测交互

监测卡片展示：

- 连接状态：连接中、监测中、重连中、已停止。
- 人体状态：`starting / present / absent / camera_error / stale` 的中文标签。
- 人脸状态：`not_enrolled / no_face / owner / unknown / multiple_faces / camera_error`。
- 人脸是否存在：`face_count > 0`。
- 单人脸匹配度：Server 返回的 cosine similarity，UI 以百分比显示但不改变原始阈值。
- `matched`：只有稳定状态为 `owner` 时为 true。
- 已发送、Server 已处理、客户端丢弃和 Server 丢弃帧数，以及最近结果时间。

打开开关后，Provider 立即启动摄像头并保持一条 WebSocket 会话。只要 Server 可用就持续
发送帧，即使人体和人脸状态长期不变也不降级为轮询或停止通信。用户离开摄像头页面后，
首页可以显示精简的监测中/离线状态；返回摄像头页面时复用原会话和累计指标。

## 6. WebSocket 协议

### 6.1 连接

- 路径：`GET /xiaozhi/presence/stream`，升级为 WebSocket。
- 认证：复用 `server.auth.enabled` 和 `server.auth_key`；开启时主进程在握手中发送
  `Authorization: Bearer <token>`。
- Server 限制单个二进制消息不超过 1 MiB，超过时以协议错误关闭连接。
- 第一条客户端消息必须是 UTF-8 JSON `start`；之后客户端只能发送二进制 JPEG 或
  `stop` JSON。

监测 start 示例：

```json
{
  "type": "start",
  "schema_version": "1.0",
  "mode": "monitoring",
  "session_id": "uuid",
  "workstation_id": "desktop-local"
}
```

注册 start 额外包含合法的 `display_name`。`session_id` 必须为 UUID；`workstation_id`
沿用 `[A-Za-z0-9._-]{1,64}` 约束。

### 6.2 Server 事件

Server 文本事件统一包含 `type`、`session_id` 和 `sequence`：

- `ready`：模型和模板已就绪，可以发送帧。
- `enrollment_progress`：accepted、required、质量 reason。
- `enrollment_complete`：profile_id、sample_id、stored_at、sample_count。
- `recognition_result`：人体和人脸稳定结果、相似度及处理指标。
- `error`：稳定错误码、可显示 message、是否可重试。
- `stopped`：Server 已完成会话清理。

监测结果核心结构：

```json
{
  "type": "recognition_result",
  "session_id": "uuid",
  "sequence": 42,
  "processed_at": "2026-08-18T12:00:00.000Z",
  "presence": {
    "state": "present",
    "changed": false
  },
  "identity": {
    "state": "owner",
    "face_count": 1,
    "face_detected": true,
    "similarity": 0.731245,
    "threshold": 0.45,
    "matched": true
  },
  "metrics": {
    "client_dropped": 3,
    "server_dropped": 2,
    "processed_frames": 40
  }
}
```

`similarity` 只在恰好一张人脸且存在已注册模板时出现。`matched` 由稳定身份状态派生，
客户端不得自行用显示百分比重新判断。

## 7. Server 推理与状态

### 7.1 生命周期

- Server 延迟创建推理运行时；首次会话时校验三个模型文件和摄像头推理依赖。缺失时返回
  明确的 `MODEL_UNAVAILABLE`，不让整个 Server 因可选摄像头能力无法启动。
- 每个连接有容量为 1 的最新帧槽。新帧到达且槽已满时替换旧帧并增加 server_dropped。
- 同步的 JPEG 解码、MediaPipe 和 OpenCV 推理通过线程执行，不能阻塞 aiohttp event loop；
  每个会话只允许一个推理任务在途，因此模型对象不会被并发调用。
- 注册和监测使用同一组运行时工厂。会话关闭时取消帧消费任务并关闭 MediaPipe 资源；
  Server 退出不得遗留推理任务。
- 解码或推理异常转换为 `INFERENCE_ERROR`；监测开关仍开启时由桌面按既定退避重连并
  重新创建运行时。

### 7.2 人体状态

复用最新提交：

- 连续 3 个正样本确认 `present`。
- 2 秒没有正样本转为 `absent`。
- Server 内部生成兼容的 event/agent instance/sequence，并写入同一个
  `PresenceRegistry`。
- 连接停止后不伪造 absent；最后状态超过 30 秒由 Registry 派生为 `stale`。

### 7.3 人脸状态

复用最新提交的 YuNet/SFace 与阈值：

- 单主人 cosine threshold 默认 0.45。
- `owner`、`unknown`、`multiple_faces` 连续 3 帧确认。
- 1 秒没有人脸转为 `no_face`。
- 未注册时返回 `not_enrolled`，不尝试生成临时身份。
- 多人时返回 `multiple_faces`，不选择其中一张猜测身份。
- 每个稳定结果保留最近单人脸 similarity；非单人脸状态不返回旧 similarity。

### 7.4 模板存储

- 继续使用最新提交的 `OwnerTemplate` NPZ schema、SFace model SHA-256 校验和原子写入。
- 默认路径保持 `presence-agent/.runtime/owner_template.npz`，已被 Git 忽略。
- display name 和非敏感元数据保存在同目录 JSON 中；模板本身只保存归一化 embedding。
- 注册成功后增加模板 revision；已有监测会话在下一帧安全重建 verifier，后续结果立即使用
  新模板。

## 8. 错误、重连与安全

- 初次连接失败或异常断线按 1、2、4、8、16、30 秒退避。只要监测开关仍开启，达到
  30 秒上限后继续每 30 秒重连，不因失败次数停止；用户主动关闭开关后立即取消重连。
- 摄像头 track `ended` 或读取失败时，在监测意图仍为 true 的前提下重新请求同一设备；
  恢复前保持明确的 camera_error，不把开关复位。
- 注册过程中断线不自动继续采样，保留旧模板并要求用户重新开始。
- 监测重连创建新 session/agent instance，Registry 按现有重启语义接受新实例。
- 非 JPEG、无法解码、空帧、过大帧和非法控制消息返回稳定错误码；连续协议错误关闭连接。
- UI 区分 Server 离线、模型不可用、未注册、没有人脸和匹配失败，不把这些状态混为一类。
- 页面导航和窗口最小化不是停止信号；应用 `before-quit` 才是全局清理信号。
- Server 不记录帧 bytes、embedding、模板内容或认证 Token。
- 跨机器连接仍遵循 Server 认证；默认本地地址不等于绕过认证。

## 9. 测试策略

实现遵循 RED -> GREEN -> REFACTOR：

1. TypeScript 单元测试验证视频回调采样、单帧在途、WebSocket 背压、无限期封顶重连、
   主动停止、跨页面 Provider 生命周期、IPC 生命周期和事件字段转换。
2. React 测试验证注册进度、成功后停止摄像头、人脸存在与匹配度显示、未注册和离线状态。
3. Python 单元测试验证协议校验、最新帧替换、同一帧双推理、状态防抖、模板原子替换与热加载。
4. aiohttp WebSocket 集成测试使用 fake JPEG decoder/detectors，验证 start -> 多个 binary frame
   -> progress/result -> stop 的完整链路以及 Registry 查询结果。
5. 使用真实模型和固定测试图像验证 YuNet/SFace 与 Pose 适配器；测试图像不包含真实用户生物
   特征并只用于测试。
6. 最终运行桌面 `npm test`、`npm run typecheck`、`npm run package`，以及 Server/presence-agent
   全量相关 pytest。
7. 本机手工 smoke：PC 注册成功后进入实时监测，真人进入/离开画面时人体状态变化；主人、
   陌生人、无人脸分别得到 owner、unknown、no_face，并持续看到合理相似度和帧指标。
8. 导航回首页并最小化窗口至少 30 秒，Server 仍持续收到帧；断开并恢复 Server 后无需
   重新点击开关即可恢复。关闭开关后立即停止发送，退出应用后摄像头指示灯熄灭。

## 10. 风险与回滚

- MediaPipe/OpenCV 会增加 Server 环境体积和首次模型初始化时间；运行时延迟创建，摄像头
  能力失败不得影响语音、事件和机器人主链路。统一版本必须持续通过 `pip check` 和双侧测试。
- 5 FPS JPEG 会增加本机 CPU；固定分辨率、单帧槽和串行推理构成硬上限。
- 人脸阈值受光照、角度和摄像头影响；本期沿用已验证的 0.45，不在 UI 暴露任意阈值调节。
- 回滚时可移除新 WebSocket 路由、摄像头推理适配器和桌面流客户端；已有 presence
  report/query API、独立 presence-agent CLI 和模板格式保持兼容，不需要数据迁移。

## 11. 验收标准

- 仓库中不再存在监测模式对 `/api/vision/frames` 的调用，也不使用 `setInterval` 采集监测帧。
- 桌面只需连接 Server 即可完成多帧主人注册，旧模板只在新模板完整生成后被替换。
- 注册后开始监测，同一摄像头帧同时产生人体状态和人脸状态。
- 单人脸结果包含实时 similarity；UI 同时显示人脸存在、匹配度和是否为主人。
- 主人、陌生人、多人、无人脸和未注册都有互不混淆的稳定状态。
- 模型推理慢于采集时延迟保持有界，可观察到丢帧但不会持续积压内存。
- Server 和 presence-agent 统一使用 Python 3.10、NumPy 1.26 和 OpenCV 4.11；推理异常时
  Server 主进程和其他接口保持可用。
- 切页、最小化和 Server 断线不会改变监测开关或停止恢复；Server 可用时始终持续通信。
- 只有手动关闭监测开关或应用退出会终止重连并释放摄像头与 WebSocket。
- 原始帧、完整关键点、embedding 和模板内容不出现在日志或非预期磁盘文件中。
- PresenceRegistry 查询能观察到本次流产生的最新人体和 identity 状态，并保持 30 秒 stale
  语义。
- 新增及既有桌面测试、类型检查、打包、Server/presence-agent 测试全部通过。

## 12. 实施结果

实现采用本设计中的单 Server 进程方案：Python 3.10、NumPy 1.26.4、OpenCV contrib
4.11.0.86、MediaPipe 0.10.35。桌面监测生命周期已提升到应用根 Provider，旧 HTTP 定时
快照链路已删除；Server WebSocket、同帧双识别、20 帧注册、Registry 发布和桌面识别状态
均由自动化测试覆盖。物理摄像头的人体/身份效果和 30 秒最小化操作仍保留为本机人工 smoke。
