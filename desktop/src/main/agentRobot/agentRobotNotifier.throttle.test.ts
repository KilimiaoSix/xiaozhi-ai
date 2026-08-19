import { describe, expect, it, vi } from 'vitest';

import { AgentRobotNotifier } from './agentRobotNotifier';
import type { RobotPushBody } from './robotIntentMapper';
import type {
  AgentTaskSnapshot,
  RobotActionIntent,
} from '../../modules/features/coding-agent-status/agent-hooks/contracts';

const DEVICE = 'dc:da:0c:26:9a:60';
const T0 = Date.parse('2026-08-19T09:00:00.000Z');

const intent = (
  action: RobotActionIntent['action'],
  taskKey: string,
  { ttlMs = 30_000, createdAt = T0 }: { ttlMs?: number; createdAt?: number } = {},
): RobotActionIntent => ({
  eventId: `${action}-${taskKey}-${createdAt}`,
  taskKey,
  action,
  createdAt: new Date(createdAt).toISOString(),
  expiresAt: new Date(createdAt + ttlMs).toISOString(),
  ttlMs,
});

const task = (key: string, over: Partial<AgentTaskSnapshot> = {}): AgentTaskSnapshot => ({
  key,
  source: 'claude-code',
  sessionId: key,
  status: 'completed',
  title: `任务${key}`,
  startedAt: new Date(T0).toISOString(),
  updatedAt: new Date(T0).toISOString(),
  ...over,
});

/** 记录推送并可控地推进时间：sleep 不真等，直接把时钟往前拨。 */
const makeHarness = (options: { minSpeakGapMs?: number } = {}) => {
  const sent: RobotPushBody[] = [];
  let now = T0;
  const client = {
    push: vi.fn(async (body: RobotPushBody) => { sent.push(body); return true; }),
  };
  const notifier = new AgentRobotNotifier(client, DEVICE, {
    ...options,
    now: () => now,
    sleep: async (ms: number) => { now += ms; },
  });
  return { sent, client, notifier, advance: (ms: number) => { now += ms; } };
};

describe('AgentRobotNotifier 的节流与合并', () => {
  it('同一任务的多次状态变化只发最终态', async () => {
    const { sent, notifier } = makeHarness();

    await notifier.notify(
      [
        intent('quiet_companion', 'a'),
        intent('needs_user', 'a'),
        intent('task_completed', 'a'),
      ],
      [task('a')],
    );
    await notifier.whenIdle();

    expect(sent).toHaveLength(1);
    expect(sent[0].status).toBe('任务完成');
  });

  it('多个任务同时完成合并成一条播报，而不是连播三句', async () => {
    const { sent, notifier } = makeHarness();

    await notifier.notify(
      [
        intent('task_completed', 'a'),
        intent('task_completed', 'b'),
        intent('task_completed', 'c'),
      ],
      [task('a'), task('b'), task('c')],
    );
    await notifier.whenIdle();

    expect(sent).toHaveLength(1);
    expect(sent[0].text).toBe('Claude Code 的 3 个任务完成了');
    expect(sent[0].emotion).toBe('happy');
    expect(sent[0].speak).toBe(true);
  });

  it('不同 Agent 的完成任务合并后仍会说清来源', async () => {
    const { sent, notifier } = makeHarness();

    await notifier.notify(
      [intent('task_completed', 'a'), intent('task_completed', 'b')],
      [task('a', { source: 'codex' }), task('b', { source: 'workbuddy' })],
    );
    await notifier.whenIdle();

    expect(sent).toHaveLength(1);
    expect(sent[0].text).toBe('Codex 和 WorkBuddy 的 2 个任务完成了');
  });

  it('不同终态各自成条，不会被错误地合并到一起', async () => {
    const { sent, notifier } = makeHarness();

    await notifier.notify(
      [intent('task_completed', 'a'), intent('task_failed', 'b')],
      [task('a'), task('b', { status: 'failed', error: '超时' })],
    );
    await notifier.whenIdle();

    expect(sent).toHaveLength(2);
    expect(sent.map((push) => push.emotion).sort()).toEqual(['happy', 'sad']);
  });

  it('两条播报之间隔开最小间隔，避免撞上设备忙态被吞掉', async () => {
    const { sent, notifier } = makeHarness({ minSpeakGapMs: 12_000 });
    const gaps: number[] = [];
    let last = T0;

    await notifier.notify(
      [intent('task_completed', 'a'), intent('task_failed', 'b')],
      [task('a'), task('b', { status: 'failed' })],
    );
    await notifier.whenIdle();

    expect(sent).toHaveLength(2);
    // 第二条必须在最小间隔之后才发出去
    expect(notifier.lastSendGapMs).toBeGreaterThanOrEqual(12_000);
    void gaps; void last;
  });

  it('安静陪伴不占播报间隔，可以立刻发', async () => {
    const { sent, notifier } = makeHarness({ minSpeakGapMs: 12_000 });

    await notifier.notify([intent('quiet_companion', 'a')], [task('a', { status: 'running' })]);
    await notifier.notify([intent('quiet_companion', 'b')], [task('b', { status: 'running' })]);
    await notifier.whenIdle();

    expect(sent.every((push) => push.speak === false)).toBe(true);
    expect(notifier.lastSendGapMs).toBe(0);
  });

  it('过期的意图直接丢弃，不补播', async () => {
    const { sent, notifier, advance } = makeHarness();

    // TTL 15 秒的意图，排到队里之前已经过去 20 秒
    const stale = intent('task_completed', 'a', { ttlMs: 15_000 });
    advance(20_000);
    await notifier.notify([stale], [task('a')]);
    await notifier.whenIdle();

    expect(sent).toHaveLength(0);
  });

  it('等待介入不会被后到的完成事件合并掉，但同任务的完成会顶掉它', async () => {
    const { sent, notifier } = makeHarness();

    await notifier.notify(
      [intent('needs_user', 'a'), intent('task_completed', 'a')],
      [task('a')],
    );
    await notifier.whenIdle();

    expect(sent).toHaveLength(1);
    expect(sent[0].emotion).toBe('happy');
  });

  it('notify 不阻塞调用方：排队后立刻返回', async () => {
    const { notifier } = makeHarness({ minSpeakGapMs: 12_000 });

    const started = Date.now();
    await notifier.notify(
      [intent('task_completed', 'a'), intent('task_failed', 'b')],
      [task('a'), task('b', { status: 'failed' })],
    );
    // 真实时钟上应当几乎没有耗时（间隔是注入的假 sleep 消化的）
    expect(Date.now() - started).toBeLessThan(200);
    await notifier.whenIdle();
  });
});
