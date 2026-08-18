# 桌面端功能模块占位设计

## 背景

当前 `desktop/src/shared/features.ts` 只有八项功能的展示元数据，`App.tsx` 点击按钮后直接写入一条 Mock 时间线事件。界面没有调用业务模块，因此后续接入身份识别、飞书、编码 Agent 或告警能力时缺少稳定扩展边界。

## 目标

- 为八项能力分别建立可注册、可执行、可测试的独立模块。
- UI 只通过模块注册中心执行能力，不再自行生成模块执行结果。
- 提供统一运行上下文和 Server HTTP 接口占位，便于后续替换 Mock 实现。
- 保持当前通信边界：Electron 通过 HTTP 访问 Server；Server 通过 WebSocket 连接机器人。
- 当前阶段不发送真实 HTTP 请求，不接入飞书、摄像头、Codex、Claude Code 或告警平台。

## 方案

### 模块契约

统一 `FeatureModule` 接口包含：

- `definition`：模块已有的标题、代码、状态、色彩和按钮文案。
- `execute(context)`：异步执行入口。
- 返回 `FeatureExecutionResult`：时间线标题、详情、色彩和 `mock` 数据来源标识。

`FeatureRuntimeContext` 向模块提供时钟与 `ServerGateway`，模块不直接依赖 React、浏览器全局对象或具体 HTTP 库。

### 八个独立模块

在 `desktop/src/modules/features/` 下建立八个模块目录，每个目录包含公开入口 `index.ts`、实现文件 `module.ts` 和就近测试 `module.test.ts`：

1. `identityWelcome`
2. `feishuBriefing`
3. `codingAgentStatus`
4. `gestureApproval`
5. `focusMode`
6. `awayMessages`
7. `returnSummary`
8. `incidentAssistant`

每个模块当前返回与自身场景对应的 Mock 结果，而不是通用的“已触发”文本。后续真实实现只替换对应模块内部逻辑或注入的适配器。

### 注册中心

`FeatureRegistry` 负责：

- 注册并列出模块。
- 校验模块 ID 唯一。
- 按 ID 查找并执行模块。
- 对未知 ID 给出明确错误。

应用使用一个默认注册中心聚合八个模块。功能卡片列表从注册中心导出，避免 UI 元数据和模块实现形成两份清单。

### Server HTTP 占位

`ServerGateway` 定义 Server 地址读写和请求契约。当前 `PlaceholderServerGateway` 只保存规范化后的地址，并在真实请求入口返回“尚未接入”的明确结果；Mock 功能模块不会发起网络请求。

后续可以用真实 `HttpServerGateway` 替换占位实现，而不修改模块注册中心和 UI 调用方式。

### UI 数据流

```text
功能卡片点击
  -> FeatureRegistry.execute(moduleId, runtimeContext)
  -> 对应 FeatureModule.execute(context)
  -> FeatureExecutionResult
  -> App 写入最近事件并更新当前模块
```

Server 地址保存也通过运行上下文中的 `ServerGateway` 完成，不再只保存在独立 UI 状态中。

## 错误处理

- 未知模块 ID：注册中心抛出带 ID 的错误。
- 重复模块 ID：注册中心创建时立即失败。
- 模块执行异常：UI 捕获后写入一条关注色时间线事件，不让应用崩溃。
- Server 地址为空：允许清空，表示关闭连接。
- 真实请求入口：占位阶段明确返回未实现错误，避免误发请求。

## 测试

- 注册中心恰好包含八个预期模块且 ID 唯一。
- 每个模块可通过注册中心执行，并返回标记为 `mock` 的场景化结果。
- 重复 ID 和未知 ID 有明确失败行为。
- Server 地址会被去除首尾空白，清空后保持关闭状态。
- 现有功能目录测试继续通过，并补充 TypeScript 类型检查、生产打包和 Electron 启动验证。

## 非目标

- 不实现真实业务集成或鉴权。
- 不建立 Electron IPC 模块执行链路。
- 不实现 Server 与机器人之间的 WebSocket 代码。
- 不新增持久化、重试、轮询、后台任务或状态数据库。

## 验收标准

- 八个功能各有独立模块目录、就近测试和统一执行入口。
- 点击任意功能卡片时，结果来自对应模块而不是 `App.tsx` 内联 Mock。
- UI 通过注册中心发现模块，不维护第二份功能列表。
- Server HTTP 适配器接口和安全占位实现可被后续真实实现替换。
- `npm test`、`npm run typecheck`、`npm run package` 全部通过，应用可在 macOS 启动。
