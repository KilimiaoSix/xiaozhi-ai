import { describe, expect, it, vi } from 'vitest';

import { AgentRobotNotifier, RobotEventPushClient } from './agentRobotNotifier';
import type {
  AgentTaskSnapshot,
  RobotActionIntent,
} from '../../modules/features/coding-agent-status/agent-hooks/contracts';

const DEVICE = 'dc:da:0c:26:9a:60';
// 固定在意图仍然有效的时刻：过期意图会被丢弃，这里测的不是过期逻辑
const NOW = Date.parse('2026-08-18T13:00:10.000Z');
const TEST_OPTIONS = { minSpeakGapMs: 0, now: () => NOW };

const intent = (
  action: RobotActionIntent['action'],
  taskKey = 'claude-code:sess-1',
): RobotActionIntent => ({
  eventId: `evt-${action}`,
  taskKey,
  action,
  createdAt: '2026-08-18T13:00:00.000Z',
  expiresAt: '2026-08-18T13:00:30.000Z',
  ttlMs: 30_000,
});

const task = (over: Partial<AgentTaskSnapshot> = {}): AgentTaskSnapshot => ({
  key: 'claude-code:sess-1',
  source: 'claude-code',
  sessionId: 'sess-1',
  status: 'completed',
  title: '补接口参数校验',
  startedAt: '2026-08-18T12:59:00.000Z',
  updatedAt: '2026-08-18T13:00:00.000Z',
  ...over,
});

const okResponse = () =>
  new Response(JSON.stringify({ ok: true, delivered: true, spoke: true }), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });

describe('RobotEventPushClient', () => {
  it('POST 到 /xiaozhi/event/push 并带上 JSON 请求体', async () => {
    const fetcher = vi.fn().mockResolvedValue(okResponse());
    const client = new RobotEventPushClient(fetcher, 'http://127.0.0.1:8003');

    const delivered = await client.push({
      device_id: DEVICE,
      text: '完成了',
      emotion: 'happy',
      status: '任务完成',
      speak: true,
    });

    expect(delivered).toBe(true);
    const [url, init] = fetcher.mock.calls[0];
    expect(url).toBe('http://127.0.0.1:8003/xiaozhi/event/push');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toMatchObject({ device_id: DEVICE, emotion: 'happy' });
  });

  it('Server 不可用时吞掉异常并返回 false，不阻断调用方', async () => {
    const client = new RobotEventPushClient(
      vi.fn().mockRejectedValue(new Error('ECONNREFUSED')),
      'http://127.0.0.1:8003',
    );

    await expect(
      client.push({ device_id: DEVICE, text: 'x', emotion: 'happy', status: 's', speak: false }),
    ).resolves.toBe(false);
  });

  it('设备不在线返回 404 时同样只返回 false', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: false, message: 'device is not online' }), {
        status: 404,
        headers: { 'content-type': 'application/json' },
      }),
    );
    const client = new RobotEventPushClient(fetcher, 'http://127.0.0.1:8003');

    await expect(
      client.push({ device_id: DEVICE, text: 'x', emotion: 'happy', status: 's', speak: false }),
    ).resolves.toBe(false);
  });
});

describe('AgentRobotNotifier', () => {
  const makeClient = () => ({ push: vi.fn().mockResolvedValue(true) });

  it('把每条意图翻译成推送并发出去', async () => {
    const client = makeClient();
    const notifier = new AgentRobotNotifier(client, DEVICE, TEST_OPTIONS);

    await notifier.notify([intent('task_completed')], [task()]);
    await notifier.whenIdle();

    expect(client.push).toHaveBeenCalledTimes(1);
    expect(client.push.mock.calls[0][0]).toMatchObject({
      device_id: DEVICE,
      emotion: 'happy',
      speak: true,
    });
  });

  it('按 taskKey 找到对应的任务快照来组织文案', async () => {
    const client = makeClient();
    const notifier = new AgentRobotNotifier(client, DEVICE, TEST_OPTIONS);

    await notifier.notify(
      [intent('task_failed', 'codex:sess-9')],
      [task(), task({ key: 'codex:sess-9', title: '重构导入', error: '语法错误' })],
    );
    await notifier.whenIdle();

    expect(client.push.mock.calls[0][0].text).toContain('重构导入');
  });

  it('没有配置 device_id 时什么都不做', async () => {
    const client = makeClient();
    const notifier = new AgentRobotNotifier(client, '');

    await notifier.notify([intent('task_completed')], [task()]);

    expect(client.push).not.toHaveBeenCalled();
  });

  it('意图为空时不产生任何请求', async () => {
    const client = makeClient();
    const notifier = new AgentRobotNotifier(client, DEVICE);

    await notifier.notify([], [task()]);

    expect(client.push).not.toHaveBeenCalled();
  });

  it('单条推送抛错不影响其余意图，也不冒泡', async () => {
    const client = {
      push: vi.fn()
        .mockRejectedValueOnce(new Error('boom'))
        .mockResolvedValue(true),
    };
    const notifier = new AgentRobotNotifier(client, DEVICE, TEST_OPTIONS);

    await expect(
      notifier.notify(
        [intent('task_completed'), intent('needs_user', 'codex:sess-9')],
        [task(), task({ key: 'codex:sess-9' })],
      ),
    ).resolves.toBeUndefined();
    await notifier.whenIdle();

    expect(client.push).toHaveBeenCalledTimes(2);
  });
});
