import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { afterAll, describe, expect, it, vi } from 'vitest';

import { AgentHooksRuntime, type AgentHooksSpool } from './runtime';
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

const setup = async (robotNotifier?: {
  notify: (intents: RobotActionIntent[], tasks: AgentTaskSnapshot[]) => void | Promise<void>;
}) => {
  const directory = await mkdtemp(path.join(tmpdir(), 'launchcrush-robot-'));
  directories.push(directory);
  const spool = new MemorySpool();
  const runtime = new AgentHooksRuntime({
    manager,
    spool,
    tracker: new AgentTaskTracker(() => new Date('2026-08-18T08:00:10.000Z')),
    statePath: path.join(directory, 'state/tasks.json'),
    now: () => new Date('2026-08-18T08:00:10.000Z'),
    ...(robotNotifier ? { robotNotifier } : {}),
  });
  return { spool, runtime };
};

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
