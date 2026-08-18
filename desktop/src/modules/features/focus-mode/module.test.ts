import { describe, expect, it, vi } from 'vitest';

import { createFeatureTestContext } from '../testContext';
import type { PomodoroDesktopGateway } from './services/pomodoroDesktopGateway';
import { createFocusModeModule, focusModeModule } from './module';

const context = createFeatureTestContext();

describe('focusModeModule', () => {
  it('模块 ID 固定且状态标记为已接入', () => {
    expect(focusModeModule.definition.id).toBe('focus-mode');
    expect(focusModeModule.definition.status).toBe('ready');
  });

  it('选中第一台在线设备并下发开始命令', async () => {
    const sendCommand = vi.fn().mockResolvedValue(undefined);
    const gateway: PomodoroDesktopGateway = {
      listDevices: () => Promise.resolve({
        devices: [
          { deviceId: 'esp32-offline', connected: false, active: false },
          { deviceId: 'esp32-01', connected: true, active: false },
        ],
      }),
      getStatus: vi.fn(),
      sendCommand,
    };
    const module = createFocusModeModule(gateway);

    await expect(module.execute(context)).resolves.toMatchObject({
      tone: 'violet',
      source: 'live',
    });
    expect(sendCommand).toHaveBeenCalledWith('esp32-01', 'start');
  });

  it('没有在线设备时返回 coral 失败结果', async () => {
    const gateway: PomodoroDesktopGateway = {
      listDevices: () => Promise.resolve({
        devices: [{ deviceId: 'esp32-01', connected: false, active: false }],
      }),
      getStatus: vi.fn(),
      sendCommand: vi.fn(),
    };
    const module = createFocusModeModule(gateway);

    await expect(module.execute(context)).resolves.toMatchObject({
      tone: 'coral',
      source: 'live',
    });
  });

  it('网关异常时返回失败结果而不抛出', async () => {
    const gateway: PomodoroDesktopGateway = {
      listDevices: () => Promise.reject(new Error('本地 Server 当前不可用')),
      getStatus: vi.fn(),
      sendCommand: vi.fn(),
    };
    const module = createFocusModeModule(gateway);

    await expect(module.execute(context)).resolves.toMatchObject({
      tone: 'coral',
      source: 'live',
      detail: '本地 Server 当前不可用',
    });
  });
});
