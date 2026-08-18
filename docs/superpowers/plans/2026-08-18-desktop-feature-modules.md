# 桌面端独立功能模块实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `bounded-plan-execution` to implement this plan task-by-task in the current worktree. Do not create subagents, worktrees, or commits unless the user explicitly requests them.

**Goal:** 将八项 UI 功能元数据升级为可注册、可执行、可测试的独立模块，并让 UI 真正通过模块层执行 Mock 行为。

**Architecture:** 模块通过 `FeatureModule` 契约返回统一的 `FeatureExecutionResult`，`FeatureRegistry` 负责发现和执行。`FeatureRuntimeContext` 注入时钟与 `ServerGateway`，React 只消费注册中心和执行结果。

**Tech Stack:** Electron Forge、React 19、TypeScript、Vitest

---

### Task 1: 模块契约、注册中心与 Server Gateway

**Files:**
- Create: `desktop/src/modules/core/types.ts`
- Create: `desktop/src/modules/core/registry.ts`
- Create: `desktop/src/modules/core/registry.test.ts`
- Create: `desktop/src/services/server/serverGateway.ts`
- Create: `desktop/src/services/server/placeholderServerGateway.ts`
- Create: `desktop/src/services/server/placeholderServerGateway.test.ts`

- [x] **Step 1: 写注册中心失败测试**

覆盖三个行为：按 ID 执行模块；重复 ID 构造失败；未知 ID 执行失败。测试模块使用以下契约：

```ts
export interface FeatureExecutionResult {
  title: string;
  detail: string;
  tone: FeatureTone;
  source: 'mock';
}

export interface FeatureRuntimeContext {
  now: () => Date;
  server: ServerGateway;
}

export interface FeatureModule {
  definition: FeatureDefinition;
  execute: (context: FeatureRuntimeContext) => Promise<FeatureExecutionResult>;
}
```

- [x] **Step 2: 运行注册中心测试并确认 RED**

Run: `cd desktop && npm test -- src/modules/core/registry.test.ts`

Expected: FAIL，因为 `FeatureRegistry` 尚不存在。

- [x] **Step 3: 实现最小注册中心**

```ts
export class FeatureRegistry {
  constructor(modules: FeatureModule[]) { /* 校验并建立 Map */ }
  list(): FeatureModule[] { /* 返回注册顺序副本 */ }
  get(id: string): FeatureModule { /* 未知 ID 抛错 */ }
  execute(id: string, context: FeatureRuntimeContext): Promise<FeatureExecutionResult> {
    return this.get(id).execute(context);
  }
}
```

- [x] **Step 4: 写 Server Gateway 失败测试**

验证 `setBaseUrl('  http://192.168.1.2:8003  ')` 后得到无首尾空白地址，清空后返回空字符串，`request()` 明确拒绝真实请求。

- [x] **Step 5: 实现 Server Gateway 占位并跑 GREEN**

```ts
export interface ServerGateway {
  getBaseUrl(): string;
  setBaseUrl(value: string): string;
  request<T>(request: ServerRequest): Promise<T>;
}

export class PlaceholderServerGateway implements ServerGateway {
  // 只保存规范化地址；request 抛出“Server HTTP 尚未接入”错误。
}
```

Run: `cd desktop && npm test -- src/modules/core/registry.test.ts src/services/server/placeholderServerGateway.test.ts`

Expected: 相关测试全部 PASS。

### Task 2: 八个独立功能模块和默认注册中心

**Files:**
- Create: `desktop/src/modules/features/identity-welcome/{index,module,module.test}.ts`
- Create: `desktop/src/modules/features/feishu-briefing/{index,module,module.test}.ts`
- Create: `desktop/src/modules/features/coding-agent-status/{index,module,module.test}.ts`
- Create: `desktop/src/modules/features/gesture-approval/{index,module,module.test}.ts`
- Create: `desktop/src/modules/features/focus-mode/{index,module,module.test}.ts`
- Create: `desktop/src/modules/features/away-messages/{index,module,module.test}.ts`
- Create: `desktop/src/modules/features/return-summary/{index,module,module.test}.ts`
- Create: `desktop/src/modules/features/incident-assistant/{index,module,module.test}.ts`
- Create: `desktop/src/modules/features/index.ts`
- Create: `desktop/src/modules/features/features.test.ts`
- Modify: `desktop/src/shared/features.ts`
- Modify: `desktop/src/shared/features.test.ts`

- [x] **Step 1: 写八模块失败测试**

断言默认注册中心包含八个固定 ID，逐个执行后均返回 `source: 'mock'`、自身 tone 和非空的场景化标题/详情；结果标题不能全部相同。

- [x] **Step 2: 运行测试并确认 RED**

Run: `cd desktop && npm test -- src/modules/features/features.test.ts`

Expected: FAIL，因为默认模块集合尚不存在。

- [x] **Step 3: 分别实现八个模块**

每个文件导出一个 `FeatureModule`，结构一致但定义和 Mock 结果独立，例如：

```ts
export const identityWelcomeModule: FeatureModule = {
  definition: identityWelcomeDefinition,
  async execute() {
    return {
      title: '识别到熟悉用户 · Mock',
      detail: '已预留人脸识别和情绪欢迎适配入口。',
      tone: 'cyan',
      source: 'mock',
    };
  },
};
```

其他七个模块分别返回飞书简报、Agent 完成、手势审批、专注开始、进入离席、返岗汇总、告警诊断的场景化结果。

- [x] **Step 4: 聚合默认注册中心并跑 GREEN**

```ts
export const featureModules = [/* 八个模块，固定展示顺序 */];
export const featureRegistry = new FeatureRegistry(featureModules);
export const featureCatalog = featureRegistry.list().map(({ definition }) => definition);
```

Run: `cd desktop && npm test -- src/modules/features/features.test.ts src/shared/features.test.ts`

Expected: 模块和目录测试全部 PASS。

### Task 3: React UI 接入真实模块执行链

**Files:**
- Create: `desktop/src/modules/runtime.ts`
- Modify: `desktop/src/renderer/App.tsx`

- [x] **Step 1: 建立默认运行上下文**

```ts
export const serverGateway = new PlaceholderServerGateway();
export const featureRuntimeContext: FeatureRuntimeContext = {
  now: () => new Date(),
  server: serverGateway,
};
```

- [x] **Step 2: 将 UI 触发改为异步模块执行**

`triggerFeature` 接收模块 ID，通过 `featureRegistry.execute(id, featureRuntimeContext)` 获取结果，成功时将结果写入时间线；失败时写入 coral 关注事件。删除 `App.tsx` 中通用“Mock 已触发”结果生成逻辑。

- [x] **Step 3: 将 Server 地址保存接入 Gateway**

`saveServerUrl` 调用 `serverGateway.setBaseUrl(serverUrl)`，以返回的规范化地址更新界面和事件；保持空地址代表关闭连接。

- [x] **Step 4: 运行类型和模块测试**

Run: `cd desktop && npm test && npm run typecheck`

Expected: 全部测试 PASS，TypeScript 无错误。

### Task 4: 完整验证与验收审计

**Files:**
- Verify: `desktop/src/modules/**`
- Verify: `desktop/src/renderer/App.tsx`
- Verify: `desktop/out/`

- [x] **Step 1: 检查模块边界**

Run: `rg -n "Mock 已触发|setEvents.*feature" desktop/src/renderer/App.tsx`

Expected: 不再存在 UI 内联的通用模块 Mock 结果。

- [x] **Step 2: 运行完整验证**

Run: `cd desktop && npm test`

Run: `cd desktop && npm run typecheck`

Run: `cd desktop && npm run package`

Expected: 三个命令退出码均为 0。

- [x] **Step 3: 启动 Electron 验证运行时**

Run: `cd desktop && npm run dev`

Expected: Forge 报告 Electron app launched，Electron 主进程和渲染进程存活；验证后正常停止进程。

- [x] **Step 4: 对照设计验收**

确认八个独立模块文件、统一注册入口、Server Gateway、安全 Mock 标识、UI 调用链和错误处理均有源代码或测试证据，且没有真实外部请求。
