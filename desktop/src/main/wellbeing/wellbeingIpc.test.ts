import { describe, expect, it, vi } from 'vitest';

import {
  WELLBEING_CHANNELS,
  registerWellbeingIpc,
  type WellbeingIpcMainLike,
} from './wellbeingIpc';

describe('registerWellbeingIpc', () => {
  it('exposes a typed wellbeing-test action and removes it during cleanup', async () => {
    const handlers = new Map<string, (...args: unknown[]) => unknown>();
    const removed: string[] = [];
    const ipcMain: WellbeingIpcMainLike = {
      handle: (channel, handler) => { handlers.set(channel, handler); },
      removeHandler: (channel) => { removed.push(channel); },
    };
    const sendTest = vi.fn(async () => ({ deviceId: 'robot-1' }));

    const cleanup = registerWellbeingIpc({
      ipcMain,
      service: { sendTest },
    });

    await expect(
      handlers.get(WELLBEING_CHANNELS.testEvent)!({}, 'overtime'),
    ).resolves.toEqual({ ok: true, value: { deviceId: 'robot-1' } });
    expect(sendTest).toHaveBeenCalledWith('overtime');

    cleanup();
    expect(removed).toEqual([WELLBEING_CHANNELS.testEvent]);
  });

  it('returns a renderer-safe error envelope when delivery fails', async () => {
    const handlers = new Map<string, (...args: unknown[]) => unknown>();
    const ipcMain: WellbeingIpcMainLike = {
      handle: (channel, handler) => { handlers.set(channel, handler); },
      removeHandler: () => undefined,
    };
    registerWellbeingIpc({
      ipcMain,
      service: {
        sendTest: async () => { throw new Error('机器人未接收测试提醒'); },
      },
    });

    await expect(
      handlers.get(WELLBEING_CHANNELS.testEvent)!({}, 'long_work'),
    ).resolves.toEqual({ ok: false, error: '机器人未接收测试提醒' });
  });

  it('rejects unknown test kinds before calling the service', async () => {
    const handlers = new Map<string, (...args: unknown[]) => unknown>();
    const ipcMain: WellbeingIpcMainLike = {
      handle: (channel, handler) => { handlers.set(channel, handler); },
      removeHandler: () => undefined,
    };
    const sendTest = vi.fn(async () => ({ deviceId: 'robot-1' }));
    registerWellbeingIpc({ ipcMain, service: { sendTest } });

    await expect(
      handlers.get(WELLBEING_CHANNELS.testEvent)!({}, 'unknown'),
    ).resolves.toEqual({ ok: false, error: '未知关怀测试类型：unknown' });
    expect(sendTest).not.toHaveBeenCalled();
  });
});
