import { describe, expect, it, vi } from 'vitest';

import { ConfiguredRobotNotifier } from './configuredRobotNotifier';
import type { RobotIntentNotifier } from '../../modules/features/coding-agent-status/agent-hooks/runtime';
import type { RobotActionIntent } from '../../modules/features/coding-agent-status/agent-hooks/contracts';

const intent = (taskKey = 't1'): RobotActionIntent =>
  ({
    taskKey,
    action: 'task_completed',
    expiresAt: new Date(Date.now() + 60_000).toISOString(),
  }) as RobotActionIntent;

const fakeNotifier = () => ({ notify: vi.fn().mockResolvedValue(undefined) });

describe('ConfiguredRobotNotifier', () => {
  it('配置了设备号就直接推给它，不走自动发现', async () => {
    const direct = fakeNotifier();
    const createDirect = vi.fn().mockReturnValue(direct);
    const createDiscovering = vi.fn();
    const notifier = new ConfiguredRobotNotifier({
      resolveDeviceId: () => 'dc:da:0c:26:9a:60',
      createDirect,
      createDiscovering: createDiscovering as unknown as () => RobotIntentNotifier,
    });

    await notifier.notify([intent()], []);

    expect(createDirect).toHaveBeenCalledWith('dc:da:0c:26:9a:60');
    expect(direct.notify).toHaveBeenCalledTimes(1);
    expect(createDiscovering).not.toHaveBeenCalled();
  });

  it('设备号是每条事件现取的：设置面板改完下一条就发到新设备', async () => {
    let deviceId = 'esp32-01';
    const created = new Map<string, ReturnType<typeof fakeNotifier>>();
    const notifier = new ConfiguredRobotNotifier({
      resolveDeviceId: () => deviceId,
      createDirect: (id) => {
        const target = fakeNotifier();
        created.set(id, target);
        return target;
      },
      createDiscovering: () => fakeNotifier(),
    });

    await notifier.notify([intent('t1')], []);
    deviceId = 'esp32-02';
    await notifier.notify([intent('t2')], []);

    expect([...created.keys()]).toEqual(['esp32-01', 'esp32-02']);
    expect(created.get('esp32-01')!.notify).toHaveBeenCalledTimes(1);
    expect(created.get('esp32-02')!.notify).toHaveBeenCalledTimes(1);
  });

  it('设备号没变时复用同一个通知器，播报间隔状态不会被重建冲掉', async () => {
    const createDirect = vi.fn().mockImplementation(() => fakeNotifier());
    const notifier = new ConfiguredRobotNotifier({
      resolveDeviceId: () => 'esp32-01',
      createDirect,
      createDiscovering: () => fakeNotifier(),
    });

    await notifier.notify([intent('t1')], []);
    await notifier.notify([intent('t2')], []);

    expect(createDirect).toHaveBeenCalledTimes(1);
  });

  it('没配设备号时交给自动发现，且只创建一次', async () => {
    const discovering = fakeNotifier();
    const createDiscovering = vi.fn().mockReturnValue(discovering);
    const notifier = new ConfiguredRobotNotifier({
      resolveDeviceId: () => '  ',
      createDirect: () => fakeNotifier(),
      createDiscovering,
    });

    await notifier.notify([intent('t1')], []);
    await notifier.notify([intent('t2')], []);

    expect(createDiscovering).toHaveBeenCalledTimes(1);
    expect(discovering.notify).toHaveBeenCalledTimes(2);
  });
});
