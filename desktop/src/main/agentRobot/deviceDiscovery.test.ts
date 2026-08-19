import { describe, expect, it, vi } from 'vitest';

import {
  DiscoveringRobotNotifier,
  discoverRobotDeviceId,
} from './deviceDiscovery';
import type {
  AgentTaskSnapshot,
  RobotActionIntent,
} from '../../modules/features/coding-agent-status/agent-hooks/contracts';

const devicesResponse = (devices: unknown, ok = true): Response =>
  ({
    ok,
    json: async () => ({ ok: true, count: Array.isArray(devices) ? devices.length : 0, devices }),
  }) as unknown as Response;

const intent = (taskKey: string): RobotActionIntent =>
  ({
    taskKey,
    action: 'task_completed',
    expiresAt: new Date(Date.now() + 60_000).toISOString(),
  }) as RobotActionIntent;

describe('discoverRobotDeviceId', () => {
  it('恰好一台在线设备时返回它', async () => {
    const fetcher = vi.fn().mockResolvedValue(devicesResponse(['aa:bb']));
    await expect(discoverRobotDeviceId(fetcher, 'http://s')).resolves.toBe('aa:bb');
  });

  it('零台或多台在线都返回 null，绝不猜设备', async () => {
    const none = vi.fn().mockResolvedValue(devicesResponse([]));
    const many = vi.fn().mockResolvedValue(devicesResponse(['a', 'b']));
    await expect(discoverRobotDeviceId(none, 'http://s')).resolves.toBeNull();
    await expect(discoverRobotDeviceId(many, 'http://s')).resolves.toBeNull();
  });

  it('网络失败或非 200 返回 null 而不是抛出', async () => {
    const boom = vi.fn().mockRejectedValue(new Error('refused'));
    const bad = vi.fn().mockResolvedValue(devicesResponse([], false));
    await expect(discoverRobotDeviceId(boom, 'http://s')).resolves.toBeNull();
    await expect(discoverRobotDeviceId(bad, 'http://s')).resolves.toBeNull();
  });
});

describe('DiscoveringRobotNotifier', () => {
  it('发现唯一设备后把意图转发给真正的通知器', async () => {
    const fetcher = vi.fn().mockResolvedValue(devicesResponse(['aa:bb']));
    const inner = { notify: vi.fn().mockResolvedValue(undefined) };
    const createNotifier = vi.fn().mockReturnValue(inner);
    const notifier = new DiscoveringRobotNotifier({
      fetcher,
      serverUrl: 'http://s',
      createNotifier,
      sleep: async () => {},
    });
    await notifier.settled;
    expect(createNotifier).toHaveBeenCalledWith('aa:bb');

    const tasks: AgentTaskSnapshot[] = [];
    await notifier.notify([intent('t1')], tasks);
    expect(inner.notify).toHaveBeenCalledTimes(1);
  });

  it('发现成功前的意图被丢弃而不是报错', async () => {
    // 永远发现不了：始终零台设备
    const fetcher = vi.fn().mockResolvedValue(devicesResponse([]));
    const notifier = new DiscoveringRobotNotifier({
      fetcher,
      serverUrl: 'http://s',
      maxAttempts: 2,
      sleep: async () => {},
    });
    await expect(notifier.notify([intent('t1')], [])).resolves.toBeUndefined();
    await notifier.settled;
  });

  it('前几轮失败后重试，直到唯一设备出现', async () => {
    const fetcher = vi
      .fn()
      .mockRejectedValueOnce(new Error('server not up'))
      .mockResolvedValueOnce(devicesResponse([]))
      .mockResolvedValue(devicesResponse(['aa:bb']));
    const inner = { notify: vi.fn() };
    const createNotifier = vi.fn().mockReturnValue(inner);
    const notifier = new DiscoveringRobotNotifier({
      fetcher,
      serverUrl: 'http://s',
      createNotifier,
      sleep: async () => {},
    });
    await notifier.settled;
    expect(createNotifier).toHaveBeenCalledWith('aa:bb');
    expect(fetcher).toHaveBeenCalledTimes(3);
  });

  it('stop 之后不再继续探测', async () => {
    let calls = 0;
    const fetcher = vi.fn().mockImplementation(async () => {
      calls += 1;
      return devicesResponse([]);
    });
    const notifier = new DiscoveringRobotNotifier({
      fetcher,
      serverUrl: 'http://s',
      maxAttempts: 100,
      sleep: async () => {
        notifier.stop();
      },
    });
    await notifier.settled;
    expect(calls).toBeLessThanOrEqual(2);
  });
});
