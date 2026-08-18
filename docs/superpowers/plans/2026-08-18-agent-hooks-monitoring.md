# AI Agent Hook Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 desktop 自动发现并配置 Codex、Claude Code、腾讯 WorkBuddy 的用户级 Hook，将任意项目中的任务事件统一为可持久恢复的四态任务流，并在桌面端展示和生成机器人预设动作意图。

**Architecture:** 三个来源 Adapter 只负责配置与 payload 规范化；公共 `agent-hooks` 模块负责事件 Spool、状态机、多会话仲裁和动作意图。Electron main 拥有文件系统与配置写入，preload 通过受控 IPC 向 React 暴露检测、安装、卸载、快照查询和实时订阅。

**Tech Stack:** Electron Forge、Vite、React 19、TypeScript、Node.js 内置模块、Vitest

**Spec:** `docs/superpowers/specs/2026-08-18-agent-hooks-monitoring-design.md`

## Global Constraints

- 修改用户级 Hook 配置只能由用户点击启用操作触发。
- 配置写入前创建 `<原文件名>.<UTC 时间戳>.launchcrush.bak`，卸载只移除 owned handler。
- 允许读取和本地保存完整提示词、最终回复、工具输入输出与 transcript；不得上传第三方。
- Hook 不得直接控制机器人或自动批准权限请求。
- desktop 不直连机器人；只生成预设动作意图并保留 `ServerGateway` seam。
- 历史保留 30 天，单日文件达到 20 MiB 后轮转。
- `quiet_companion` TTL 15 秒，完成/失败 TTL 30 秒，等待用户 TTL 10 分钟。
- 不新增运行时依赖；使用 Node/Electron 内置模块。

---

### Task 1: 公共 Hook 协议与三来源事件规范化

**Files:**
- Create: `desktop/src/modules/features/coding-agent-status/agent-hooks/contracts.ts`
- Create: `desktop/src/modules/features/coding-agent-status/agent-hooks/normalize.ts`
- Create: `desktop/src/modules/features/coding-agent-status/agent-hooks/normalize.test.ts`
- Modify: `desktop/src/modules/features/coding-agent-status/index.ts`

**Interfaces:**
- Produces: `AgentSource`, `AgentTaskStatus`, `RawAgentHookEvent`, `AgentEvent`, `AgentTaskSnapshot`, `RobotActionIntent`。
- Produces: `normalizeAgentEvent(raw: RawAgentHookEvent): AgentEvent`。

- [ ] **Step 1: 写规范化失败测试**

覆盖三种来源的 `UserPromptSubmit`、`PermissionRequest`、`StopFailure`、WorkBuddy `idle_prompt`，并断言完整 `prompt`、`tool_input`、`tool_response`、`transcript_path` 被保留。事件 ID 对同一 payload 必须稳定。

```ts
it('保留 Codex 的完整任务内容并生成稳定事件 ID', () => {
  const raw = createRaw('codex', {
    session_id: 'codex-1',
    turn_id: 'turn-1',
    hook_event_name: 'UserPromptSubmit',
    cwd: '/repo',
    transcript_path: '/tmp/codex.jsonl',
    prompt: '完整实现登录模块并运行全部测试',
  });

  const first = normalizeAgentEvent(raw);
  const second = normalizeAgentEvent(raw);
  expect(first).toMatchObject({
    source: 'codex',
    sessionId: 'codex-1',
    turnId: 'turn-1',
    eventName: 'UserPromptSubmit',
    prompt: '完整实现登录模块并运行全部测试',
  });
  expect(first.id).toBe(second.id);
});
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `cd desktop && npm test -- src/modules/features/coding-agent-status/agent-hooks/normalize.test.ts`

Expected: FAIL，因为 contracts 和 normalizer 尚不存在。

- [ ] **Step 3: 实现公共契约**

```ts
export type AgentSource = 'codex' | 'claude-code' | 'workbuddy';
export type AgentTaskStatus = 'running' | 'completed' | 'failed' | 'needs_user';

export interface RawAgentHookEvent {
  source: AgentSource;
  receivedAt: string;
  payload: Record<string, unknown>;
}

export interface AgentEvent {
  id: string;
  source: AgentSource;
  sessionId: string;
  turnId?: string;
  eventName: string;
  occurredAt: string;
  cwd?: string;
  transcriptPath?: string;
  prompt?: string;
  finalMessage?: string;
  toolName?: string;
  toolInput?: unknown;
  toolResponse?: unknown;
  notificationType?: string;
  error?: string;
  backgroundTaskCount?: number;
}
```

`normalizeAgentEvent` 使用 `node:crypto` 对来源、会话、turn、事件名、tool use ID 和原始 payload 的稳定序列化结果生成 SHA-256 ID。缺少 `session_id` 时使用 `source:cwd:receivedAt`。

- [ ] **Step 4: 运行测试并确认 GREEN**

Run: `cd desktop && npm test -- src/modules/features/coding-agent-status/agent-hooks/normalize.test.ts`

Expected: PASS。

- [ ] **Step 5: 提交公共协议**

```bash
git add desktop/src/modules/features/coding-agent-status
git commit -m "feat(desktop): normalize coding agent hook events"
```

---

### Task 2: 四态状态机、多会话仲裁和机器人动作意图

**Files:**
- Create: `desktop/src/modules/features/coding-agent-status/agent-hooks/taskTracker.ts`
- Create: `desktop/src/modules/features/coding-agent-status/agent-hooks/taskTracker.test.ts`

**Interfaces:**
- Consumes: `AgentEvent`, `AgentTaskSnapshot`, `RobotActionIntent`。
- Produces: `AgentTaskTracker.apply(event, options?)`、`list()`、`primary()`、`drainActionIntents()`。

- [ ] **Step 1: 写状态机失败测试**

逐个测试：

- `UserPromptSubmit → running`
- `PermissionRequest → needs_user`
- `Stop → completed`
- `Stop` 有 background tasks 时保持 running
- `Stop` 最终消息以问号或“请确认/请选择/需要你”结束时变 needs_user
- `StopFailure/PostToolUseFailure/error response → failed`
- failed 后的新工具事件恢复 running
- 多会话 `needs_user > failed > running > completed`
- 恢复事件会更新状态，但过期动作不进入 intent 队列

```ts
const tracker = new AgentTaskTracker(() => new Date('2026-08-18T08:00:00Z'));
tracker.apply(event('UserPromptSubmit', { prompt: '修复登录失败' }));
tracker.apply(event('PermissionRequest', { toolName: 'Bash' }));
expect(tracker.primary()).toMatchObject({ status: 'needs_user' });
expect(tracker.drainActionIntents()).toContainEqual(
  expect.objectContaining({ action: 'needs_user', ttlMs: 600_000 }),
);
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `cd desktop && npm test -- src/modules/features/coding-agent-status/agent-hooks/taskTracker.test.ts`

Expected: FAIL，因为 `AgentTaskTracker` 尚不存在。

- [ ] **Step 3: 实现最小状态机**

任务 key 使用 `${source}:${sessionId}`。`apply(event, { recovered?: boolean })` 返回最新 snapshot；相同事件 ID 直接忽略。标题使用完整 prompt 的第一行，`prompt` 字段保存全文。动作意图定义为：

```ts
export interface RobotActionIntent {
  eventId: string;
  taskKey: string;
  action: 'quiet_companion' | 'task_completed' | 'task_failed' | 'needs_user';
  createdAt: string;
  expiresAt: string;
  ttlMs: number;
}
```

- [ ] **Step 4: 运行测试并确认 GREEN**

Run: `cd desktop && npm test -- src/modules/features/coding-agent-status/agent-hooks/taskTracker.test.ts`

Expected: PASS。

- [ ] **Step 5: 提交状态机**

```bash
git add desktop/src/modules/features/coding-agent-status/agent-hooks
git commit -m "feat(desktop): track agent task lifecycle"
```

---

### Task 3: 三种工具的发现、Hook 配置合并与可逆卸载

**Files:**
- Create: `desktop/src/modules/features/coding-agent-status/agent-hooks/install/types.ts`
- Create: `desktop/src/modules/features/coding-agent-status/agent-hooks/install/sourceDefinitions.ts`
- Create: `desktop/src/modules/features/coding-agent-status/agent-hooks/install/jsonHookConfig.ts`
- Create: `desktop/src/modules/features/coding-agent-status/agent-hooks/install/jsonHookConfig.test.ts`
- Create: `desktop/src/modules/features/coding-agent-status/agent-hooks/install/manager.ts`
- Create: `desktop/src/modules/features/coding-agent-status/agent-hooks/install/manager.test.ts`

**Interfaces:**
- Produces: `AgentHookManager.detect/install/uninstall/installAll`。
- Produces: `createOwnedHookCommand(options)`，owned marker 固定为 `launchcrush-agent-hook`。

```ts
export interface AgentHookDetection {
  source: AgentSource;
  available: boolean;
  installed: boolean;
  executablePath?: string;
  configPath: string;
  lastEventAt?: string;
  message: string;
}

export interface AgentHookInstallResult {
  source: AgentSource;
  ok: boolean;
  installed: boolean;
  configPath: string;
  backupPath?: string;
  message: string;
}
```

- [ ] **Step 1: 写 JSON 配置合并失败测试**

测试现有未知字段和用户 Hook 完整保留、所有目标事件只加入一个 owned handler、第二次 merge 深度相等、unmerge 只删除 owned handler 并清理空 matcher group。

```ts
const existing = {
  theme: 'dark',
  hooks: {
    Stop: [{ hooks: [{ type: 'command', command: '/user/notify.sh' }] }],
  },
};
const installed = mergeOwnedHooks(existing, codexDefinition, ownedCommand);
expect(installed.theme).toBe('dark');
expect(JSON.stringify(installed)).toContain('/user/notify.sh');
expect(mergeOwnedHooks(installed, codexDefinition, ownedCommand)).toEqual(installed);
```

- [ ] **Step 2: 运行配置测试并确认 RED**

Run: `cd desktop && npm test -- src/modules/features/coding-agent-status/agent-hooks/install/jsonHookConfig.test.ts`

Expected: FAIL，因为 merge 函数尚不存在。

- [ ] **Step 3: 实现来源定义与纯配置函数**

来源事件：

```ts
codex: ['SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PermissionRequest', 'PostToolUse', 'Stop', 'SessionEnd']
claude-code: ['SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PermissionRequest', 'PostToolUse', 'PostToolUseFailure', 'Notification', 'Stop', 'StopFailure', 'SessionEnd']
workbuddy: ['SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PermissionRequest', 'PostToolUse', 'Notification', 'Stop', 'SessionEnd']
```

工具事件 matcher 使用 `.*`，其他事件不写 matcher。owned handler 的 command 必须包含 `--owner launchcrush-agent-hook --source <source> --spool <absolute-path>`。

- [ ] **Step 4: 运行配置测试并确认 GREEN**

Run: `cd desktop && npm test -- src/modules/features/coding-agent-status/agent-hooks/install/jsonHookConfig.test.ts`

Expected: PASS。

- [ ] **Step 5: 写安装管理失败测试**

使用临时 home 和注入的 `fileSystem`/`resolveExecutable`：

- Codex/Claude 可执行文件存在时 detected。
- WorkBuddy 通过 `.workbuddy/settings.json`、`.codebuddy/settings.json` 或应用路径 detected。
- install 写备份和原子替换。
- malformed JSON 拒绝覆盖。
- uninstall 保留用户配置。
- 不存在的工具不创建目录。

- [ ] **Step 6: 运行管理测试并确认 RED**

Run: `cd desktop && npm test -- src/modules/features/coding-agent-status/agent-hooks/install/manager.test.ts`

Expected: FAIL，因为 manager 尚不存在。

- [ ] **Step 7: 实现安装管理**

默认路径：

```ts
codex: ~/.codex/hooks.json
claude-code: ~/.claude/settings.json
workbuddy: 优先现有 ~/.workbuddy/settings.json，其次 ~/.codebuddy/settings.json，否则 ~/.workbuddy/settings.json
```

原子写使用同目录临时文件和 `rename`。备份时间使用注入时钟生成不含冒号的 UTC 字符串。所有错误转换为 `AgentHookInstallResult`，不得让 renderer 收到不可序列化 Error。

- [ ] **Step 8: 运行安装测试并确认 GREEN**

Run: `cd desktop && npm test -- src/modules/features/coding-agent-status/agent-hooks/install`

Expected: PASS。

- [ ] **Step 9: 提交安装模块**

```bash
git add desktop/src/modules/features/coding-agent-status/agent-hooks/install
git commit -m "feat(desktop): manage coding agent hook configs"
```

---

### Task 4: 外部 Hook runner、原子事件 Spool 与离线恢复

**Files:**
- Create: `desktop/src/modules/features/coding-agent-status/agent-hooks/spool/hookRunnerSource.ts`
- Create: `desktop/src/modules/features/coding-agent-status/agent-hooks/spool/hookRunnerSource.test.ts`
- Create: `desktop/src/modules/features/coding-agent-status/agent-hooks/spool/eventSpool.ts`
- Create: `desktop/src/modules/features/coding-agent-status/agent-hooks/spool/eventSpool.test.ts`

**Interfaces:**
- Produces: `HOOK_RUNNER_SOURCE`。
- Produces: `EventSpool.consumePending(onEvent)`、`watch(onEvent)`、`close()`。

- [ ] **Step 1: 写 Hook runner 集成失败测试**

测试把 `HOOK_RUNNER_SOURCE` 写到临时 `.cjs`，通过 `spawn(process.execPath, [...])` 输入完整 payload，断言 inbox 中只产生一个完整 JSON 文件且 stdout 为空；传入坏 JSON 时 runner 非阻塞退出并写诊断文件。

- [ ] **Step 2: 运行 runner 测试并确认 RED**

Run: `cd desktop && npm test -- src/modules/features/coding-agent-status/agent-hooks/spool/hookRunnerSource.test.ts`

Expected: FAIL，因为 runner source 尚不存在。

- [ ] **Step 3: 实现无依赖 runner source**

脚本仅使用 `node:fs`、`node:path`、`node:crypto`。参数解析只接受 `--owner`、`--source`、`--spool`。读取 stdin 后先写 `<uuid>.tmp`，再 rename 为 `<receivedAt>-<uuid>.json`。任何错误只写 `diagnostics/runner-errors.ndjson`，最终 `process.exitCode = 0`，不得阻塞 Agent。

- [ ] **Step 4: 运行 runner 测试并确认 GREEN**

Run: `cd desktop && npm test -- src/modules/features/coding-agent-status/agent-hooks/spool/hookRunnerSource.test.ts`

Expected: PASS。

- [ ] **Step 5: 写 EventSpool 失败测试**

测试：按文件名顺序消费、调用 `normalizeAgentEvent`、损坏文件移动到 quarantine、已处理事件追加每日历史、inbox 文件删除、重新消费不重复、30 天清理和 20 MiB 轮转函数。

- [ ] **Step 6: 运行 Spool 测试并确认 RED**

Run: `cd desktop && npm test -- src/modules/features/coding-agent-status/agent-hooks/spool/eventSpool.test.ts`

Expected: FAIL，因为 EventSpool 尚不存在。

- [ ] **Step 7: 实现 EventSpool**

`consumePending` 依次读取 `.json`；成功回调后才归档和删除。`watch` 使用 `fs.watch` 触发消费，并用串行 Promise 防止重入；额外每 2 秒扫描一次作为漏事件兜底。`close` 关闭 watcher 和 interval。

- [ ] **Step 8: 运行 Spool 测试并确认 GREEN**

Run: `cd desktop && npm test -- src/modules/features/coding-agent-status/agent-hooks/spool`

Expected: PASS。

- [ ] **Step 9: 提交 Spool 模块**

```bash
git add desktop/src/modules/features/coding-agent-status/agent-hooks/spool
git commit -m "feat(desktop): persist agent hook event spool"
```

---

### Task 5: Electron main runtime、IPC 与 preload 契约

**Files:**
- Create: `desktop/src/modules/features/coding-agent-status/agent-hooks/runtime.ts`
- Create: `desktop/src/modules/features/coding-agent-status/agent-hooks/runtime.test.ts`
- Create: `desktop/src/main/agentHooksIpc.ts`
- Create: `desktop/src/main/agentHooksIpc.test.ts`
- Modify: `desktop/src/main.ts`
- Modify: `desktop/src/shared/contracts.ts`
- Modify: `desktop/src/preload.ts`
- Modify: `desktop/src/global.d.ts`

**Interfaces:**
- Produces: `AgentHooksRuntime.start/stop/getSnapshot/detect/install/installAll/uninstall/subscribe`。
- Adds to `XiaofeiDesktopApi`: `agentHooks.detect/install/installAll/uninstall/getSnapshot/onSnapshot`。

- [ ] **Step 1: 写 runtime 失败测试**

使用临时 Spool、真实 normalizer/tracker 和 fake manager，断言启动恢复事件使用 `{ recovered: true }`、实时事件发布 snapshot 和未过期 intent、停止释放 watcher。

- [ ] **Step 2: 运行 runtime 测试并确认 RED**

Run: `cd desktop && npm test -- src/modules/features/coding-agent-status/agent-hooks/runtime.test.ts`

Expected: FAIL，因为 runtime 尚不存在。

- [ ] **Step 3: 实现 runtime**

Snapshot 结构：

```ts
export interface AgentHooksSnapshot {
  installations: AgentHookDetection[];
  primaryTask: AgentTaskSnapshot | null;
  tasks: AgentTaskSnapshot[];
  actionIntents: RobotActionIntent[];
  updatedAt: string;
}
```

恢复阶段完成后才打开实时 watcher；恢复事件不会发布动作。实时 intent 暂存在 snapshot，等待后续 `HttpServerGateway` 消费。

- [ ] **Step 4: 运行 runtime 测试并确认 GREEN**

Run: `cd desktop && npm test -- src/modules/features/coding-agent-status/agent-hooks/runtime.test.ts`

Expected: PASS。

- [ ] **Step 5: 写 IPC 注册失败测试**

通过注入 fake `ipcMain` 和 fake window sender，断言固定 channel、参数 source 校验、Error 序列化和取消订阅。

- [ ] **Step 6: 运行 IPC 测试并确认 RED**

Run: `cd desktop && npm test -- src/main/agentHooksIpc.test.ts`

Expected: FAIL，因为 IPC registrar 尚不存在。

- [ ] **Step 7: 实现 IPC、preload 和 main 启停**

固定 channel：

```ts
agent-hooks:detect
agent-hooks:install
agent-hooks:install-all
agent-hooks:uninstall
agent-hooks:snapshot
agent-hooks:snapshot-changed
```

`main.ts` 在 `app.whenReady()` 后创建 runtime 并注册 IPC；`before-quit` 调用 stop。preload 的 `onSnapshot` 返回取消订阅函数，并只接受已知 channel。

- [ ] **Step 8: 运行相关测试与类型检查**

Run: `cd desktop && npm test -- src/modules/features/coding-agent-status/agent-hooks/runtime.test.ts src/main/agentHooksIpc.test.ts && npm run typecheck`

Expected: PASS。

- [ ] **Step 9: 提交 Electron 接入**

```bash
git add desktop/src/main.ts desktop/src/main desktop/src/preload.ts desktop/src/global.d.ts desktop/src/shared/contracts.ts desktop/src/modules/features/coding-agent-status/agent-hooks/runtime.ts desktop/src/modules/features/coding-agent-status/agent-hooks/runtime.test.ts
git commit -m "feat(desktop): expose agent hook monitoring over IPC"
```

---

### Task 6: 将 coding-agent-status 从 Mock 升级为真实任务监控 UI

**Files:**
- Modify: `desktop/src/modules/core/types.ts`
- Modify: `desktop/src/modules/features/coding-agent-status/module.ts`
- Modify: `desktop/src/modules/features/coding-agent-status/module.test.ts`
- Modify: `desktop/src/shared/features.test.ts`
- Modify: `desktop/src/renderer/App.tsx`
- Modify: `desktop/src/renderer/styles.css`

**Interfaces:**
- Consumes: `window.xiaofei.agentHooks` 和 `AgentHooksSnapshot`。
- Produces: 工具安装面板、当前任务卡、并发任务列表、历史状态展示。

- [ ] **Step 1: 写模块状态失败测试**

将 `FeatureExecutionResult.source` 扩为 `'mock' | 'live'`。测试 `codingAgentStatusModule.definition.status === 'ready'` 且 execute 返回 `source: 'live'`；其他七个模块仍为 placeholder/mock。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `cd desktop && npm test -- src/modules/features/coding-agent-status/module.test.ts src/shared/features.test.ts`

Expected: FAIL，因为 coding-agent-status 仍是 placeholder/mock。

- [ ] **Step 3: 修改模块契约与定义**

`codingAgentStatusModule` 的按钮文案改为“查看 Agent 任务”，状态改为 `ready`，execute 只负责将 UI 聚焦到监控区域，不伪造任务结果。

- [ ] **Step 4: 运行模块测试并确认 GREEN**

Run: `cd desktop && npm test -- src/modules/features/coding-agent-status/module.test.ts src/shared/features.test.ts`

Expected: PASS。

- [ ] **Step 5: 实现 React 监控界面**

App mount 时调用 `getSnapshot()` 和 `detect()`，并订阅 snapshot。增加：

- Codex/Claude Code/WorkBuddy 三个安装状态行。
- “一键启用监控”、逐项安装和卸载按钮；操作中禁用重复点击。
- primary task 四态卡，显示来源、完整 prompt、cwd、更新时间、错误或等待原因。
- 其他并发任务列表。
- 真实来源标识；现有演示模块仍显示 Mock。
- IPC 失败写入最近事件并展示可重试状态。

- [ ] **Step 6: 添加四态和安装状态样式**

完成使用绿色、失败使用橙色/珊瑚、等待使用琥珀色、运行使用青色。完整 prompt 使用 `white-space: pre-wrap` 和可滚动区域，避免长任务撑坏页面。

- [ ] **Step 7: 运行完整前端测试和类型检查**

Run: `cd desktop && npm test && npm run typecheck`

Expected: 全部 PASS。

- [ ] **Step 8: 提交 UI**

```bash
git add desktop/src/modules desktop/src/shared desktop/src/renderer
git commit -m "feat(desktop): show live coding agent tasks"
```

---

### Task 7: 全链路验收、打包与文档更新

**Files:**
- Modify: `README.md`
- Verify: `desktop/src/**`
- Verify: `desktop/out/**`

**Interfaces:**
- Consumes: 前六个任务的所有公开接口。
- Produces: 可重复的安装与事件注入验收说明。

- [ ] **Step 1: 添加全链路集成测试**

在 `runtime.test.ts` 增加真实 runner → inbox → normalizer → tracker → snapshot 测试，分别注入：

```text
Codex UserPromptSubmit -> running
Claude PermissionRequest -> needs_user
Claude StopFailure -> failed
WorkBuddy Stop -> completed
```

断言完整 prompt 被保留、来源正确、主任务仲裁正确、过期 recovered 事件没有 action intent。

- [ ] **Step 2: 运行集成测试**

Run: `cd desktop && npm test -- src/modules/features/coding-agent-status/agent-hooks/runtime.test.ts`

Expected: PASS。若失败，先保留失败用例，再只修复该用例暴露的断链并重新运行。

- [ ] **Step 3: 更新项目文档**

README 增加：工具发现、用户授权安装、配置备份、数据目录、卸载和诊断，并注明机器人真实 HTTP 发送仍是后续能力。实施前已经存在且未跟踪的 `docs/功能清单.md` 保持不变。

- [ ] **Step 4: 运行完整验证**

Run: `cd desktop && npm test`

Expected: 所有测试 PASS，无 warning/error。

Run: `cd desktop && npm run typecheck`

Expected: exit 0。

Run: `cd desktop && npm run package`

Expected: exit 0，生成可启动的 macOS 应用目录。

- [ ] **Step 5: 检查工作树和差异**

Run: `git status --short && git diff --check HEAD`

Expected: 只包含计划内文件；保留实施前已存在的用户修改，不把它们混入提交。

- [ ] **Step 6: 提交验收与文档**

```bash
git add README.md desktop/src
git commit -m "docs: document coding agent hook monitoring"
```
