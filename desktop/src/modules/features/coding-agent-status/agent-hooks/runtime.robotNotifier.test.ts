import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { afterAll, describe, expect, it, vi } from 'vitest';

import {
  AgentHooksRuntime,
  type AgentAttention,
  type AgentAttentionMonitor,
  type AgentHooksSpool,
} from './runtime';
import { AgentTaskTracker } from './taskTracker';
import type { AgentEvent, AgentSource, AgentTaskSnapshot, RobotActionIntent } from './contracts';
import type { AgentHookDetection, AgentHookInstallResult } from './install/types';

const directories: string[] = [];
afterAll(async () => {
  await Promise.all(directories.map((dir) => rm(dir, { recursive: true, force: true })));
});

class MemorySpool implements AgentHooksSpool {
  liveConsumer?: (event: AgentEvent) => void | Promise<void>;
  async consumePending() {}
  watch(consumer: (event: AgentEvent) => void | Promise<void>) { this.liveConsumer = consumer; }
  async close() {}
  async emitLive(item: AgentEvent) { await this.liveConsumer?.(item); }
}

const manager = {
  detect: async (): Promise<AgentHookDetection[]> => [],
  install: async (): Promise<AgentHookInstallResult> => ({
    source: 'claude-code' as AgentSource, ok: true, installed: true,
    configPath: '/home/.claude/settings.json', message: '',
  }),
  uninstall: async (): Promise<AgentHookInstallResult> => ({
    source: 'claude-code' as AgentSource, ok: true, installed: false,
    configPath: '/home/.claude/settings.json', message: '',
  }),
  installAll: async (): Promise<AgentHookInstallResult[]> => [],
};

const event = (eventName: string, over: Partial<AgentEvent> = {}): AgentEvent => ({
  id: `evt-${eventName}-${over.sessionId ?? 'sess-1'}`,
  source: 'claude-code',
  sessionId: 'sess-1',
  eventName,
  occurredAt: '2026-08-18T08:00:00.000Z',
  prompt: '补接口参数校验',
  ...over,
});

class MemoryAttentionMonitor implements AgentAttentionMonitor {
  private listener?: (attention: AgentAttention | null) => void | Promise<void>;

  start(listener: (attention: AgentAttention | null) => void | Promise<void>) {
    this.listener = listener;
  }

  async stop() { this.listener = undefined; }

  async emit(attention: AgentAttention | null) { await this.listener?.(attention); }
}

const setup = async (robotNotifier?: {
  notify: (intents: RobotActionIntent[], tasks: AgentTaskSnapshot[]) => void | Promise<void>;
}) => {
  const directory = await mkdtemp(path.join(tmpdir(), 'launchcrush-robot-'));
  directories.push(directory);
  const spool = new MemorySpool();
  const attentionMonitor = new MemoryAttentionMonitor();
  const runtime = new AgentHooksRuntime({
    manager,
    spool,
    tracker: new AgentTaskTracker(() => new Date('2026-08-18T08:00:10.000Z')),
    statePath: path.join(directory, 'state/tasks.json'),
    attentionMonitor,
    now: () => new Date('2026-08-18T08:00:10.000Z'),
    ...(robotNotifier ? { robotNotifier } : {}),
  });
  return { spool, attentionMonitor, runtime };
};

const approval = (over: Partial<AgentAttention> = {}): AgentAttention => ({
  source: 'codex',
  reason: 'Computer Use 需要用户确认',
  detectedAt: '2026-08-18T08:00:10.000Z',
  ...over,
});

describe('AgentHooksRuntime 的机器人通知接线', () => {
  it('产生机器人意图时把意图与任务快照交给通知器', async () => {
    const notify = vi.fn();
    const { spool, runtime } = await setup({ notify });
    await runtime.start();

    await spool.emitLive(event('UserPromptSubmit'));

    expect(notify).toHaveBeenCalled();
    const [intents, tasks] = notify.mock.calls.at(-1)!;
    expect(intents.length).toBeGreaterThan(0);
    expect(tasks.some((task: AgentTaskSnapshot) => task.sessionId === 'sess-1')).toBe(true);
    await runtime.stop();
  });

  it('通知器抛错不影响事件处理与快照发布', async () => {
    const notify = vi.fn().mockRejectedValue(new Error('server down'));
    const { spool, runtime } = await setup({ notify });
    await runtime.start();

    await expect(spool.emitLive(event('UserPromptSubmit'))).resolves.toBeUndefined();

    expect(runtime.getSnapshot().tasks.length).toBeGreaterThan(0);
    await runtime.stop();
  });

  it('未注入通知器时行为与原先完全一致', async () => {
    const { spool, runtime } = await setup();
    await runtime.start();

    await expect(spool.emitLive(event('UserPromptSubmit'))).resolves.toBeUndefined();

    expect(runtime.getSnapshot().actionIntents.length).toBeGreaterThan(0);
    await runtime.stop();
  });
});

/**
 * 外部探针（Codex 桌面版的「等待批准」）走的是 attentionMonitor 而不是 hook 事件流，
 * 此前只 publish 到界面、不通知机器人——桌面上看得见、机器人一声不吭。
 * 探针每秒轮询，所以「只在跃迁沿通知一次」是这条链路的硬约束。
 */
describe('AgentHooksRuntime 的外部探针机器人通知', () => {
  it('进入等待批准时把 needs_user 意图交给机器人通知器', async () => {
    const notify = vi.fn();
    const { attentionMonitor, runtime } = await setup({ notify });
    await runtime.start();

    await attentionMonitor.emit(approval());

    expect(notify).toHaveBeenCalledTimes(1);
    const [intents, tasks] = notify.mock.calls.at(-1)!;
    expect(intents).toEqual([expect.objectContaining({
      action: 'needs_user',
      taskKey: 'codex:external-attention',
      ttlMs: 600_000,
      createdAt: '2026-08-18T08:00:10.000Z',
      expiresAt: '2026-08-18T08:10:10.000Z',
    })]);
    // 通知器要靠 taskKey 反查任务才能拼出播报文案，快照必须一起交出去
    expect(tasks).toEqual([expect.objectContaining({
      key: 'codex:external-attention',
      status: 'needs_user',
      needsUserReason: 'Computer Use 需要用户确认',
    })]);
    await runtime.stop();
  });

  it('连续多次同状态轮询只在跃迁沿通知一次', async () => {
    const notify = vi.fn();
    const { attentionMonitor, runtime } = await setup({ notify });
    await runtime.start();

    await attentionMonitor.emit(approval());
    await attentionMonitor.emit(approval({ detectedAt: '2026-08-18T08:00:11.000Z' }));
    await attentionMonitor.emit(approval({ detectedAt: '2026-08-18T08:00:12.000Z' }));

    expect(notify).toHaveBeenCalledTimes(1);
    await runtime.stop();
  });

  it('审批解除本身不通知机器人，解除后再次出现能再通知', async () => {
    const notify = vi.fn();
    const { attentionMonitor, runtime } = await setup({ notify });
    await runtime.start();

    await attentionMonitor.emit(approval());
    expect(notify).toHaveBeenCalledTimes(1);

    // 解除只是把界面上的「需要你」摘掉，没有新意图，不该再推一条给机器人
    await attentionMonitor.emit(null);
    expect(notify).toHaveBeenCalledTimes(1);

    await attentionMonitor.emit(approval({ detectedAt: '2026-08-18T08:05:00.000Z' }));
    expect(notify).toHaveBeenCalledTimes(2);
    expect(notify.mock.calls.at(-1)![0]).toEqual([expect.objectContaining({
      action: 'needs_user',
      createdAt: '2026-08-18T08:05:00.000Z',
    })]);
    await runtime.stop();
  });

  it('通知器抛错不影响外部审批态发布到界面', async () => {
    const notify = vi.fn().mockRejectedValue(new Error('server down'));
    const { attentionMonitor, runtime } = await setup({ notify });
    await runtime.start();

    await expect(attentionMonitor.emit(approval())).resolves.toBeUndefined();

    expect(runtime.getSnapshot().primaryTask).toMatchObject({
      key: 'codex:external-attention',
      status: 'needs_user',
    });
    await runtime.stop();
  });
});
