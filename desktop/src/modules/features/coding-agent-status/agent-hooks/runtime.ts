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

export interface AgentHooksManager {
  detect(): Promise<AgentHookDetection[]>;
  install(source: AgentSource): Promise<AgentHookInstallResult>;
  uninstall(source: AgentSource): Promise<AgentHookInstallResult>;
  installAll(): Promise<AgentHookInstallResult[]>;
}

interface AgentHooksRuntimeOptions {
  manager: AgentHooksManager;
  spool: AgentHooksSpool;
  tracker: AgentTaskTracker;
  statePath: string;
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
  private started = false;

  constructor(private readonly options: AgentHooksRuntimeOptions) {
    this.now = options.now ?? (() => new Date());
  }

  async start(): Promise<void> {
    if (this.started) return;
    await this.restoreState();
    await this.options.spool.consumePending(async (event) => {
      this.options.tracker.apply(event, { recovered: true });
      await this.persistState();
    });
    this.installations = await this.options.manager.detect();
    this.actionIntents = [];
    this.started = true;
    this.options.spool.watch((event) => this.handleLiveEvent(event));
    await this.persistState();
    this.publish();
  }

  async stop(): Promise<void> {
    if (!this.started) return;
    await this.persistState();
    await this.options.spool.close();
    this.started = false;
  }

  getSnapshot(): AgentHooksSnapshot {
    const tasks = this.options.tracker.list();
    return cloneSnapshot({
      installations: this.installations,
      primaryTask: this.options.tracker.primary(),
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
    this.options.tracker.apply(event);
    this.actionIntents = this.options.tracker.drainActionIntents();
    await this.persistState();
    this.publish();
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

