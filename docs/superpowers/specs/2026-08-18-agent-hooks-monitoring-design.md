# AI Agent Hook 监控设计

## 背景

`desktop/` 目前只有 `coding-agent-status` 的 Mock 模块，尚未接入 Codex、Claude Code 或腾讯 WorkBuddy 的真实任务事件。工伴需要在用户使用这些工具处理任意本地项目时，持续识别任务运行、完成、失败和等待用户介入等状态，并将状态提供给桌面界面和机器人反馈链路。

本设计使用三种工具提供的生命周期 Hook 作为主要事件来源：

- Codex Hooks：<https://learn.chatgpt.com/docs/hooks>
- Claude Code Hooks：<https://code.claude.com/docs/en/hooks>
- 腾讯 WorkBuddy Hooks：<https://cloud.tencent.com/document/product/1831/134517>

## 目标

- 自动发现本机已安装的 Codex、Claude Code 和腾讯 WorkBuddy。
- 在用户点击“一键启用监控”后，安全合并三种工具的用户级 Hook 配置。
- 将三种工具的原始事件转换成统一的 `AgentEvent`。
- 同时跟踪多个会话和任务，并判断 `running`、`completed`、`failed`、`needs_user` 四种任务状态。
- 在 desktop 未运行时继续收集本地事件，下次启动时恢复任务历史。
- 在桌面端展示当前任务、来源、状态、工作目录和时间。
- 将状态变化映射成机器人预设动作意图；桌面端仍只通过 Server HTTP 通信，不直接连接机器人。
- 支持检测、安装、重复安装、诊断和可逆卸载。

## 非目标

- 不由 Hook 直接控制机器人。
- 不让模型生成舵机角度。
- 不替用户自动批准 Codex、Claude Code 或 WorkBuddy 的权限请求。
- 不依赖三种工具未公开的数据库结构作为主要事件来源。
- 本阶段不定义 Server 与机器人之间的 WebSocket 实现。
- 本阶段不实现新的 Server HTTP endpoint；只产生可交给现有 `ServerGateway` 的动作意图。

## 用户授权与数据范围

- desktop 启动时可以只读检测工具和配置状态。
- 修改用户级配置必须由用户点击“一键启用监控”触发。
- 安装前为将被修改的配置创建 `<原文件名>.<UTC 时间戳>.launchcrush.bak` 备份。
- 卸载只删除 launchcrush 自己写入的 Hook handler，保留用户和其他工具的全部配置。
- 允许在本机读取和保存完整用户提示词、最终回复、工具输入输出以及 Hook 提供的 `transcript_path`。
- 所有采集数据仅保存在本机，不发送到第三方平台。
- Server 只接收机器人预设动作请求，不接收提示词、回复或 transcript 内容。

## 总体架构

```text
Codex hooks.json ─────────┐
Claude settings.json ────┼─> launchcrush-hook.cjs ─> 本地事件 Spool
WorkBuddy settings.json ─┘                              │
                                                       v
Electron main ─> 原始事件解析 ─> AgentEvent ─> 状态机/多会话仲裁
                                              │                │
                                              v                v
                                       Renderer 任务界面   机器人动作意图
                                                               │
                                                               v
                                                        ServerGateway HTTP
```

外部 Hook runner 必须是无第三方包依赖的 CommonJS 脚本。安装器将其复制到 Electron `userData` 下的稳定位置，三种工具的配置通过 `ELECTRON_RUN_AS_NODE=1` 和绝对路径调用当前 Electron 可执行文件，不要求用户另行安装 Node.js。Hook runner 从 stdin 读取 JSON，附加来源和接收时间后，把一个事件写成一个 JSON 文件，并始终快速、非阻塞地退出。

## 模块划分

### 1. `agent-hooks` 核心模块

负责公共协议、事件验证、持久队列和状态机，不了解任一工具的配置文件格式。

主要类型：

```ts
export type AgentSource = 'codex' | 'claude-code' | 'workbuddy';

export type AgentTaskStatus =
  | 'running'
  | 'completed'
  | 'failed'
  | 'needs_user';

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

export interface AgentTaskSnapshot {
  key: string;
  source: AgentSource;
  sessionId: string;
  status: AgentTaskStatus;
  title: string;
  prompt?: string;
  cwd?: string;
  startedAt: string;
  updatedAt: string;
  completedAt?: string;
  error?: string;
  needsUserReason?: string;
}
```

### 2. 三个来源 Adapter

分别建立 `codex-hook`、`claude-code-hook` 和 `workbuddy-hook` Adapter。每个 Adapter 提供：

- 可执行文件和用户配置目录发现。
- Hook 配置片段生成。
- 现有配置合并与 owned handler 识别。
- 原始 Hook payload 到 `AgentEvent` 的转换。
- 当前安装状态和能力诊断。

Adapter 不直接操作 React，也不直接发机器人动作。

### 3. Hook 安装管理模块

Electron 主进程拥有配置写入权限。安装管理模块通过 IPC 暴露以下能力：

```ts
interface AgentHookInstallationApi {
  detect(): Promise<AgentHookDetection[]>;
  install(source: AgentSource): Promise<AgentHookInstallResult>;
  uninstall(source: AgentSource): Promise<AgentHookInstallResult>;
  installAll(): Promise<AgentHookInstallResult[]>;
}
```

所有写入采用“读取 → 解析 → 合并 → 写临时文件 → 原子替换”。JSON 无法解析时拒绝覆盖并返回可操作错误。重复安装必须幂等。

### 4. 事件 Spool 与恢复模块

Hook runner 将事件写入 `userData/agent-hooks/inbox/`。每个事件先写入同目录唯一临时文件，再原子改名为 `.json`，避免三种工具并发 Hook 造成内容交错。Electron 主进程：

- 启动时按文件名顺序消费 inbox 中的遗留事件。
- 运行时监听 inbox 并增量消费。
- 将已处理事件追加到只由主进程写入的每日 NDJSON 历史文件，然后删除 inbox 文件。
- 隔离无法解析的事件文件并写入诊断记录，不中断后续事件。
- 用确定性事件 ID 去重。
- 保存最近任务状态和已处理事件 ID。
- 历史保留最近 30 天；单日文件达到 20 MiB 后按序号轮转。

desktop 关闭期间产生的事件在下次启动时恢复到任务历史。机器人动作意图包含 `expiresAt`，恢复旧事件时只更新状态，不发送已过期动作。

### 5. 状态机与多会话仲裁

基础映射：

| 事件 | 目标状态 |
| --- | --- |
| `UserPromptSubmit` | `running`，开始或更新当前任务 |
| `PreToolUse` / `PostToolUse` | `running` |
| `PermissionRequest` | `needs_user` |
| `Notification(permission_prompt)` | `needs_user` |
| `Notification(idle_prompt)` | `needs_user` |
| `Stop` | 默认 `completed` |
| `Stop` 且存在后台任务 | 保持 `running` |
| `Stop` 且最终回复明确要求用户输入 | `needs_user` |
| `StopFailure` / `PostToolUseFailure` | `failed` |
| 明确的错误工具结果 | `failed`，但后续继续运行事件可以恢复为 `running` |
| `SessionEnd` | 若任务未终止则转为 `completed` |

Codex 当前没有与 Claude Code 完全对应的 `StopFailure` 事件，因此 Codex Adapter 还会结合 `PostToolUse` 错误结果和 Stop 最终消息判断失败。判断规则是确定性的，不在 Hook 进程内调用模型。

同时存在多个任务时全部保留，桌面主任务按以下优先级选择；同优先级取更新时间最新者：

```text
needs_user > failed > running > completed
```

### 6. Desktop UI

`coding-agent-status` 从 Mock 模块升级为真实任务监控模块。界面增加：

- 三种工具的发现、Hook 安装和健康状态。
- “一键启用监控”和逐项启用/卸载操作。
- 当前主任务卡片：来源、完整提示词、状态、工作目录和更新时间。
- 活跃任务列表，支持多个工具和多个会话并行。
- 最近任务历史。
- `needs_user` 的“打开来源”入口；无法定位窗口时退化为展示来源和工作目录。
- 明确的模拟/真实数据标识。

Renderer 不直接访问用户配置和 transcript 文件，所有读取和写入通过 preload 暴露的受控 IPC 完成。

## 各来源事件配置

### Codex

用户级配置文件为 `~/.codex/hooks.json`。安装以下事件：

- `SessionStart`
- `UserPromptSubmit`
- `PreToolUse`
- `PermissionRequest`
- `PostToolUse`
- `Stop`
- `SessionEnd`

### Claude Code

用户级配置文件为 `~/.claude/settings.json`。安装以下事件：

- `SessionStart`
- `UserPromptSubmit`
- `PreToolUse`
- `PermissionRequest`
- `PostToolUse`
- `PostToolUseFailure`
- `Notification`
- `Stop`
- `StopFailure`
- `SessionEnd`

### 腾讯 WorkBuddy

优先检测实际 WorkBuddy 用户目录 `~/.workbuddy/settings.json`，同时兼容同源运行时使用的 `~/.codebuddy/settings.json`。安装配置与 Claude Code 兼容，包含：

- `SessionStart`
- `UserPromptSubmit`
- `PreToolUse`
- `PermissionRequest`
- `PostToolUse`
- `Notification`
- `Stop`
- `SessionEnd`

若当前 WorkBuddy 版本不触发某个事件，诊断界面显示“已配置但尚未观测”，其他事件继续工作。

## 机器人反馈意图

状态机只输出预设动作名称：

| 状态变化 | 动作意图 |
| --- | --- |
| 首次进入 `running` | `quiet_companion`，默认不频繁动作 |
| 进入 `completed` | `task_completed` |
| 进入 `failed` | `task_failed` |
| 进入 `needs_user` | `needs_user` |

动作意图包含来源事件 ID、创建时间和过期时间。`quiet_companion` 有效期 15 秒，`task_completed` 和 `task_failed` 有效期 30 秒，`needs_user` 有效期 10 分钟。重复事件不重复生成意图；从 inbox 或历史恢复事件时只要已超过有效期就不发送动作。真实 HTTP 发送由后续 `HttpServerGateway` Adapter 完成。

## 错误处理

- 工具未安装：显示“未发现”，不创建空配置目录。
- 配置缺失：在用户确认后创建。
- 配置语法错误：停止安装，保留原文件并展示文件路径和解析错误。
- Hook runner 不可执行或 Electron 可执行文件已移动：诊断失败并提示重新安装 Hook，但不影响 Agent 工具继续工作。
- Hook payload 缺字段：尽可能生成事件；没有 `session_id` 时使用来源、cwd 和接收时间生成临时关联键。
- Spool 事件文件损坏：隔离该文件并继续消费。
- transcript 无法读取：保留 Hook 直接提供的数据，不影响状态判断。
- Renderer 关闭或重载：主进程继续消费，恢复订阅后发送最新快照。

## 测试策略

### 单元测试

- 三种 Adapter 的发现、配置生成和事件转换。
- 配置合并不覆盖已有 Hook。
- 重复安装不产生重复 handler。
- 卸载只删除 owned handler。
- 四态状态机和 Stop 特殊情况。
- 多会话优先级、事件去重和动作过期。

### 集成测试

- 使用临时用户目录执行安装、备份和卸载。
- 运行真实 Hook runner，将 stdin JSON 原子写入临时事件 Spool。
- Electron 主进程消费 Spool 并通过 IPC 发布任务快照。
- desktop 关闭期间写入事件，重启后恢复状态但不补发过期动作。

### 完整验证

```bash
cd desktop
npm test
npm run typecheck
npm run package
```

手工验收分别在 Codex、Claude Code 和 WorkBuddy 中提交一个任务，确认 desktop 依次显示运行、等待介入、完成或失败，并确认机器人动作只产生预设名称。

## 验收标准

- desktop 能发现三种工具并展示独立状态。
- 用户可一键安装和可逆卸载 Hook，既有配置不丢失。
- 三种来源都进入统一任务列表，来源和状态准确可见。
- Codex 与 Claude Code 能观测运行、完成、失败和权限等待。
- WorkBuddy 能观测其当前版本实际触发的生命周期事件，并对未触发事件给出诊断。
- desktop 关闭期间的事件可恢复。
- 过期机器人动作不会在恢复或重连时补发。
- 测试、类型检查和打包全部通过。
