import { describe, expect, it } from 'vitest';

import { FEISHU_CHANNELS, registerFeishuIpc, type FeishuIpcMainLike } from './feishuIpc';

describe('registerFeishuIpc', () => {
  it('注册只读飞书 channel 并序列化错误', async () => {
    const handlers = new Map<string, (...args: unknown[]) => unknown>();
    const removed: string[] = [];
    const ipcMain: FeishuIpcMainLike = {
      handle: (channel, handler) => { handlers.set(channel, handler); },
      removeHandler: (channel) => { removed.push(channel); },
    };
    const cleanup = registerFeishuIpc({
      ipcMain,
      client: {
        getStatus: async () => ({
          state: 'ready',
          message: '已连接',
          source: 'server_openapi',
          requiredScopes: [],
          missingScopes: [],
        }),
        getBriefing: async () => { throw new Error('日历未授权'); },
      },
    });

    expect([...handlers.keys()].sort()).toEqual([
      FEISHU_CHANNELS.briefing,
      FEISHU_CHANNELS.status,
    ].sort());
    await expect(handlers.get(FEISHU_CHANNELS.status)!({})).resolves.toMatchObject({ ok: true });
    await expect(handlers.get(FEISHU_CHANNELS.briefing)!({})).resolves.toEqual({
      ok: false,
      error: '日历未授权',
    });

    cleanup();
    expect(removed.sort()).toEqual([...handlers.keys()].sort());
  });
});
