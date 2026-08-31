import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import path from 'node:path';

import type {
  AgentEvent,
  AgentSource,
  AgentTaskSnapshot,
  RobotActionIntent,
} from './contracts';
import type { AgentHookDetection, AgentHookInstallResult } from './install/types';
import type { AgentTaskTracker } from './taskTracker';

export interface AgentHooksSnapshot {
  installations: AgentHookDetection[];
  primaryTask: AgentTaskSnapshot | null;
  tasks: AgentTaskSnapshot[];
  actionIntents: RobotActionIntent[];
  updatedAt: string;
}

export interface AgentHooksSpool {
  consumePending(consumer: (event: AgentEvent) => void | Promise<void>): Promise<void>;
  watch(consumer: (event: AgentEvent) => void | Promise<void>): void;
  close(): Promise<void>;
}

export interface AgentAttention {
  source: AgentSource;
  reason: string;
  detectedAt: string;
}

export interface AgentAttentionMonitor {
  start(listener: (attention: AgentAttention | null) => void | Promise<void>): void;
  stop(): Promise<void>;
}

export interface AgentHooksManager {
  prepare?(): Promise<void>;
  detect(): Promise<AgentHookDetection[]>;
  install(source: AgentSource): Promise<AgentHookInstallResult>;
  uninstall(source: AgentSource): Promise<AgentHookInstallResult>;
  installAll(): Promise<AgentHookInstallResult[]>;
  markActive?(source: AgentSource): Promise<void>;
}

/**
 * 机器人意图的消费方。桌面端只负责把意图交出去，
 * 「配什么表情动作、该不该打扰」由实现方决定（见 AGENTS.md 职责划分）。
 */
export interface RobotIntentNotifier {
  notify(
    intents: RobotActionIntent[],
    tasks: AgentTaskSnapshot[],
  ): void | Promise<void>;
}

interface AgentHooksRuntimeOptions {
  manager: AgentHooksManager;
  spool: AgentHooksSpool;
  tracker: AgentTaskTracker;
  statePath: string;
  attentionMonitor?: AgentAttentionMonitor;
  robotNotifier?: RobotIntentNotifier;
  now?: () => Date;
}

type SnapshotListener = (snapshot: AgentHooksSnapshot) => void;

const cloneSnapshot = (snapshot: AgentHooksSnapshot): AgentHooksSnapshot => ({
  installations: snapshot.installations.map((item) => ({ ...item })),
  primaryTask: snapshot.primaryTask ? { ...snapshot.primaryTask } : null,
  tasks: snapshot.tasks.map((task) => ({ ...task })),
  actionIntents: snapshot.actionIntents.map((intent) => ({ ...intent })),
  updatedAt: snapshot.updatedAt,
});

export class AgentHooksRuntime {
  private readonly now: () => Date;
  private readonly listeners = new Set<SnapshotListener>();
  private installations: AgentHookDetection[] = [];
  private actionIntents: RobotActionIntent[] = [];
  private externalAttention: AgentAttention | null = null;
  private started = false;

  constructor(private readonly options: AgentHooksRuntimeOptions) {
    this.now = options.now ?? (() => new Date());
  }

  async start(): Promise<void> {
    if (this.started) return;
    await this.options.manager.prepare?.();
    await this.restoreState();
    await this.options.spool.consumePending(async (event) => {
      await this.acknowledgeHookEvent(event.source);
      this.options.tracker.apply(event, { recovered: true });
      await this.persistState();
    });
    this.installations = await this.options.manager.detect();
    this.actionIntents = [];
    this.started = true;
    this.options.spool.watch((event) => this.handleLiveEvent(event));
    this.options.attentionMonitor?.start((attention) =>
      this.handleExternalAttention(attention));
    await this.persistState();
    this.publish();
  }

  async stop(): Promise<void> {
    if (!this.started) return;
    await this.persistState();
    await this.options.attentionMonitor?.stop();
    await this.options.spool.close();
    this.externalAttention = null;
    this.started = false;
  }

  getSnapshot(): AgentHooksSnapshot {
    const tasks = this.tasksWithExternalAttention();
    return cloneSnapshot({
      installations: this.installations,
      primaryTask: this.primaryTask(tasks),
      tasks,
      actionIntents: this.actionIntents,
      updatedAt: this.now().toISOString(),
    });
  }

  subscribe(listener: SnapshotListener): () => void {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  }

  async detect(): Promise<AgentHookDetection[]> {
    this.installations = await this.options.manager.detect();
    this.publish();
    return this.installations.map((item) => ({ ...item }));
  }

  async install(source: AgentSource): Promise<AgentHookInstallResult> {
    const result = await this.options.manager.install(source);
    await this.detect();
    return result;
  }

  async installAll(): Promise<AgentHookInstallResult[]> {
    const results = await this.options.manager.installAll();
    await this.detect();
    return results;
  }

  async uninstall(source: AgentSource): Promise<AgentHookInstallResult> {
    const result = await this.options.manager.uninstall(source);
    await this.detect();
    return result;
  }

  private async handleLiveEvent(event: AgentEvent): Promise<void> {
    await this.acknowledgeHookEvent(event.source);
    this.options.tracker.apply(event);
    this.actionIntents = this.options.tracker.drainActionIntents();
    await this.persistState();
    this.publish();
    await this.notifyRobot();
  }

  /**
   * 把新产生的意图交给机器人通知器。放在 publish 之后，且失败只吞不抛——
   * 界面反馈不能因为 Server 没起或设备离线而被拖累。
   */
  private async notifyRobot(): Promise<void> {
    const notifier = this.options.robotNotifier;
    if (!notifier || this.actionIntents.length === 0) return;
    try {
      await notifier.notify(
        this.actionIntents.map((intent) => ({ ...intent })),
        this.tasksWithExternalAttention(),
      );
    } catch {
      // 通知机器人是尽力而为，失败不影响事件处理与快照发布
    }
  }

  private async acknowledgeHookEvent(source: AgentSource): Promise<void> {
    await this.options.manager.markActive?.(source);
    this.installations = this.installations.map((installation) =>
      installation.source === source && installation.installed
        ? {
            ...installation,
            requiresTrustReview: false,
            message: '监控 Hook 已生效',
          }
        : installation);
  }

  /**
   * 外部探针（Codex 桌面版的「等待批准」）报上来的关注态。
   *
   * 探针每秒轮询，同一个等待批准窗口会被报上来几十次；上面这道同源同因的闸门
   * 就是「只在跃迁沿走一次」的唯一保证——通知机器人必须在闸门之后，
   * 否则真机上等待批准会被反复播报。解除时 actionIntents 为空，
   * notifyRobot 自然静默：解除只是把界面上的「需要你」摘掉，不是一条新意图。
   */
  private async handleExternalAttention(attention: AgentAttention | null): Promise<void> {
    if (attention && this.externalAttention
      && attention.source === this.externalAttention.source
      && attention.reason === this.externalAttention.reason) {
      return;
    }

    this.externalAttention = attention;
    const taskKey = attention
      ? this.tasksWithExternalAttention().find((task) =>
          task.source === attention.source && task.status === 'needs_user')?.key
      : undefined;
    this.actionIntents = attention ? [this.attentionIntent(attention, taskKey)] : [];
    this.publish();
    await this.notifyRobot();
  }

  private tasksWithExternalAttention(): AgentTaskSnapshot[] {
    const tasks = this.options.tracker.list();
    if (!this.externalAttention) return tasks;

    const index = tasks.findIndex((task) =>
      task.source === this.externalAttention?.source
      && (task.status === 'running' || task.status === 'needs_user'));
    if (index < 0) {
      return [{
        key: `${this.externalAttention.source}:external-attention`,
        source: this.externalAttention.source,
        sessionId: 'external-attention',
        status: 'needs_user',
        title: `${this.externalAttention.source} 任务`,
        startedAt: this.externalAttention.detectedAt,
        updatedAt: this.externalAttention.detectedAt,
        needsUserReason: this.externalAttention.reason,
      }, ...tasks];
    }

    const current = tasks[index];
    if (!current) return tasks;
    const { completedAt: _completedAt, error: _error, needsUserReason: _reason, ...base } = current;
    tasks[index] = {
      ...base,
      status: 'needs_user',
      updatedAt: this.externalAttention.detectedAt,
      needsUserReason: this.externalAttention.reason,
    };
    return tasks;
  }

  private primaryTask(tasks: AgentTaskSnapshot[]): AgentTaskSnapshot | null {
    const priority: Record<AgentTaskSnapshot['status'], number> = {
      needs_user: 4,
      failed: 3,
      running: 2,
      completed: 1,
    };
    const task = [...tasks].sort((left, right) => {
      const statusPriority = priority[right.status] - priority[left.status];
      return statusPriority || Date.parse(right.updatedAt) - Date.parse(left.updatedAt);
    })[0];
    return task ? { ...task } : null;
  }

  private attentionIntent(
    attention: AgentAttention,
    taskKey = `${attention.source}:external-attention`,
  ): RobotActionIntent {
    const createdMs = Date.parse(attention.detectedAt);
    const safeCreatedMs = Number.isNaN(createdMs) ? this.now().getTime() : createdMs;
    const ttlMs = 600_000;
    return {
      eventId: `external-attention:${attention.source}:${safeCreatedMs}`,
      taskKey,
      action: 'needs_user',
      createdAt: new Date(safeCreatedMs).toISOString(),
      expiresAt: new Date(safeCreatedMs + ttlMs).toISOString(),
      ttlMs,
    };
  }

  private publish(): void {
    const snapshot = this.getSnapshot();
    for (const listener of this.listeners) listener(cloneSnapshot(snapshot));
  }

  private async restoreState(): Promise<void> {
    try {
      const raw: unknown = JSON.parse(await readFile(this.options.statePath, 'utf8'));
      if (raw && typeof raw === 'object' && Array.isArray((raw as { tasks?: unknown }).tasks)) {
        this.options.tracker.restore(
          (raw as { tasks: AgentTaskSnapshot[] }).tasks.filter((task) =>
            task && typeof task.key === 'string' && typeof task.status === 'string'),
        );
      }
    } catch {
      // First launch or damaged state falls back to the durable event inbox.
    }
  }

  private async persistState(): Promise<void> {
    await mkdir(path.dirname(this.options.statePath), { recursive: true });
    const temporaryPath = `${this.options.statePath}.${process.pid}.tmp`;
    await writeFile(temporaryPath, `${JSON.stringify({
      tasks: this.options.tracker.list(),
      updatedAt: this.now().toISOString(),
    }, null, 2)}\n`, 'utf8');
    await rename(temporaryPath, this.options.statePath);
  }
}
