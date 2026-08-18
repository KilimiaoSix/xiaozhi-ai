# 人脸身份识别集成设计

## 目标

在现有 `presence-agent` 的单路摄像头采集循环中加入 YuNet 人脸检测和 SFace 本人验证，并通过既有 presence 上报链路提供“是否在岗”和“是否本人”两个独立维度。实现必须兼容旧客户端、可一键安装运行，且不上传图片、特征向量或人脸模板。

## 范围

- 复用人脸 Demo 已验证的 YuNet、SFace、余弦相似度与稳态规则。
- 支持本地登记、删除模板和持续识别。
- 扩展 `POST /xiaozhi/presence/report` 与 `GET /xiaozhi/presence/{workstation_id}`。
- 更新一键启动脚本、README 和 API 文档。
- 不实现活体检测，不用身份结果执行门禁、考勤、支付或自动授权。

## 架构

`presence-agent` 是摄像头唯一所有者。每帧只读取一次，镜像后分别送给 MediaPipe Pose 和本地 FaceVerifier。Pose 继续产出顶层 presence 状态；FaceVerifier 产出独立 identity 状态。LatestSnapshot 在任一稳定状态变化时增加 revision，PresenceReporter 因此立即上报；未变化时沿用 15 秒心跳。

人脸模板位于 `presence-agent/.runtime/owner_template.npz`，模型位于 `presence-agent/models/`。模板被 `.gitignore` 排除，也不进入 HTTP payload。没有模板时 Agent 正常运行在岗判断并上报 `identity.state=not_enrolled`。

## 身份状态

- `starting`：已登记，等待人脸判断达到稳定条件。
- `not_enrolled`：本机没有可用模板。
- `owner`：单人脸与模板相似度达到阈值，连续 3 帧确认。
- `unknown`：单人脸未达到阈值，连续 3 帧确认。
- `multiple_faces`：检测到多张人脸，连续 3 帧确认，不猜测身份。
- `no_face`：连续 1 秒没有人脸。
- `camera_error`：摄像头无法打开或读取，立即确认。

`identity` 对象包含 `state`、`previous_state`、`changed`、`face_count`，单人脸识别时可包含 `similarity`。相似度仅是当前帧的聚合分数，不包含 embedding。

## API 兼容

协议仍使用 `schema_version=1.0`。请求顶层新增可选 `identity` 字段；旧 Agent 不发送该字段时继续合法。Server 严格校验新增对象并原样保存，查询响应新增可选 `identity`。顶层 `effective_state=stale` 表示整条观测已过期，消费者在 stale 时不得继续信任 identity。

`source` 保持 `camera_pose`，避免破坏既有消费者；它表示现有 presence 事件源，identity 是该事件的附加本地推理结果。

## 本地登记

`presence-agent/enroll-face.ps1` 独占摄像头采集 20 个合格样本，去掉一致性最低的 2 个，原子写入归一化模板。`run-presence-stack.ps1 -EnrollOwner` 先完成登记再启动服务。`-DeleteFaceTemplate` 仅删除本地模板并退出。

## 错误处理

- 模型缺失或模板损坏：启动返回配置错误，不静默降级为 owner/unknown。
- 模板不存在：正常进入 `not_enrolled`。
- 摄像头故障：presence 与 identity 都报告 `camera_error`，恢复后重新稳定判断。
- Server 拒绝或离线：继续使用现有幂等事件、最新值覆盖和指数退避。

## 测试与验收

- 单元测试覆盖身份稳态、模板校验、FaceVerifier 输出和 Server 协议校验。
- Agent 集成测试证明同一帧同时进入姿态与人脸检测，身份变化触发 revision。
- 包装测试校验模型、许可证、脚本参数、模板忽略规则和隐私边界。
- 全量 Python 测试、桌面端测试和 TypeScript 类型检查必须通过。
