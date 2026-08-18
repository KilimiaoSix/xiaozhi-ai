import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { afterEach, describe, expect, it } from 'vitest';

import type { AgentEvent, AgentSource } from './contracts';
import type { AgentHookDetection, AgentHookInstallResult } from './install/types';
import { AgentHooksRuntime, type AgentHooksSpool } from './runtime';
import { AgentTaskTracker } from './taskTracker';

const directories: string[] = [];

const event = (
  eventName: string,
  overrides: Partial<AgentEvent> = {},
): AgentEvent => ({
  id: `${eventName}-${overrides.sessionId ?? 'session-1'}`,
  source: 'codex',
  sessionId: 'session-1',
  eventName,
  occurredAt: '2026-08-18T08:00:00.000Z',
  ...overrides,
});

const detection = (source: AgentSource, installed = false): AgentHookDetection => ({
  source,
  available: true,
  installed,
  configPath: `/home/.${source}/settings.json`,
  message: installed ? '监控 Hook 已安装' : '已发现，尚未启用监控',
});

class MemorySpool implements AgentHooksSpool {
  recovered: AgentEvent[] = [];
  liveConsumer?: (event: AgentEvent) => void | Promise<void>;
  closed = false;

  async consumePending(consumer: (event: AgentEvent) => void | Promise<void>) {
    for (const item of this.recovered) await consumer(item);
  }

  watch(consumer: (event: AgentEvent) => void | Promise<void>) {
    this.liveConsumer = consumer;
  }

  async close() { this.closed = true; }

  async emitLive(item: AgentEvent) { await this.liveConsumer?.(item); }
}

const createManager = () => {
  let installed = false;
  return {
    detect: async () => [
      detection('codex', installed),
      detection('claude-code'),
      detection('workbuddy'),
    ],
    install: async (source: AgentSource): Promise<AgentHookInstallResult> => {
      installed = source === 'codex';
      return {
        source,
        ok: true,
        installed: true,
        configPath: `/home/.${source}/settings.json`,
        message: '安装成功',
      };
    },
    uninstall: async (source: AgentSource): Promise<AgentHookInstallResult> => {
      if (source === 'codex') installed = false;
      return {
        source,
        ok: true,
        installed: false,
        configPath: `/home/.${source}/settings.json`,
        message: '卸载成功',
      };
    },
    installAll: async (): Promise<AgentHookInstallResult[]> => [],
  };
};

const setup = async () => {
  const directory = await mkdtemp(path.join(tmpdir(), 'launchcrush-runtime-'));
  directories.push(directory);
  const spool = new MemorySpool();
  const runtime = new AgentHooksRuntime({
    manager: createManager(),
    spool,
    tracker: new AgentTaskTracker(() => new Date('2026-08-18T08:00:10.000Z')),
    statePath: path.join(directory, 'state/tasks.json'),
    now: () => new Date('2026-08-18T08:00:10.000Z'),
  });
  return { directory, spool, runtime };
};

afterEach(async () => {
  await Promise.all(directories.splice(0).map((directory) =>
    rm(directory, { recursive: true, force: true })));
});

describe('AgentHooksRuntime', () => {
  it('恢复离线事件但不生成过期机器人动作', async () => {
    const { runtime, spool } = await setup();
    spool.recovered.push(event('UserPromptSubmit', {
      occurredAt: '2026-08-17T08:00:00.000Z',
      prompt: '离线期间提交的完整任务',
    }));

    await runtime.start();

    expect(runtime.getSnapshot()).toMatchObject({
      primaryTask: {
        status: 'running',
        prompt: '离线期间提交的完整任务',
      },
      actionIntents: [],
    });
  });

  it('实时事件发布快照、动作意图并持久化任务状态', async () => {
    const { runtime, spool, directory } = await setup();
    const snapshots: string[] = [];
    runtime.subscribe((snapshot) => {
      snapshots.push(snapshot.primaryTask?.status ?? 'none');
    });
    await runtime.start();

    await spool.emitLive(event('UserPromptSubmit', {
      prompt: '执行完整 Hook 集成',
      occurredAt: '2026-08-18T08:00:10.000Z',
    }));
    await spool.emitLive(event('PermissionRequest', {
      id: 'permission-1',
      toolName: 'Bash',
      occurredAt: '2026-08-18T08:00:10.000Z',
    }));

    expect(snapshots).toContain('running');
    expect(snapshots).toContain('needs_user');
    expect(runtime.getSnapshot()).toMatchObject({
      primaryTask: { status: 'needs_user', prompt: '执行完整 Hook 集成' },
      actionIntents: [expect.objectContaining({ action: 'needs_user' })],
    });
    const persisted = JSON.parse(await readFile(
      path.join(directory, 'state/tasks.json'),
      'utf8',
    ));
    expect(persisted.tasks[0]).toMatchObject({ status: 'needs_user' });
  });

  it('重启时恢复已持久化的任务快照', async () => {
    const { runtime, spool, directory } = await setup();
    await runtime.start();
    await spool.emitLive(event('UserPromptSubmit', {
      prompt: '跨重启恢复任务',
      occurredAt: '2026-08-18T08:00:10.000Z',
    }));
    await runtime.stop();

    const restarted = new AgentHooksRuntime({
      manager: createManager(),
      spool: new MemorySpool(),
      tracker: new AgentTaskTracker(() => new Date('2026-08-18T08:01:00.000Z')),
      statePath: path.join(directory, 'state/tasks.json'),
      now: () => new Date('2026-08-18T08:01:00.000Z'),
    });
    await restarted.start();

    expect(restarted.getSnapshot().primaryTask).toMatchObject({
      status: 'running',
      prompt: '跨重启恢复任务',
    });
  });

  it('安装与卸载后刷新检测状态并在 stop 时释放 Spool', async () => {
    const { runtime, spool } = await setup();
    await runtime.start();

    await runtime.install('codex');
    expect(runtime.getSnapshot().installations[0]).toMatchObject({ installed: true });
    await runtime.uninstall('codex');
    expect(runtime.getSnapshot().installations[0]).toMatchObject({ installed: false });

    await runtime.stop();
    expect(spool.closed).toBe(true);
  });
});
