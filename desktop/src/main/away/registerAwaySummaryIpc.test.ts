import { describe, expect, it } from 'vitest';

import { AwayGatewayError } from '../../modules/features/away-summary/types';
import {
  AWAY_CHANNELS,
  registerAwaySummaryIpc,
  type AwayIpcMainLike,
} from './registerAwaySummaryIpc';

const makeHarness = (
  client: Parameters<typeof registerAwaySummaryIpc>[0]['client'],
) => {
  const handlers = new Map<string, (event: unknown, ...args: unknown[]) => unknown>();
  const removed: string[] = [];
  const ipcMain: AwayIpcMainLike = {
    handle: (channel, handler) => { handlers.set(channel, handler); },
    removeHandler: (channel) => { removed.push(channel); },
  };
  const cleanup = registerAwaySummaryIpc({ ipcMain, client });
  return { handlers, removed, cleanup };
};

const emptySummary = {
  away: false,
  awaySince: null,
  awayMinutes: 0,
  count: 0,
  speech: null,
  items: [],
};

describe('registerAwaySummaryIpc', () => {
  it('注册只读的 summary channel 并在成功时返回 ok 信封', async () => {
    const { handlers, removed, cleanup } = makeHarness({
      getSummary: async () => emptySummary,
    });

    expect([...handlers.keys()]).toEqual([AWAY_CHANNELS.summary]);
    await expect(handlers.get(AWAY_CHANNELS.summary)!({})).resolves.toEqual({
      ok: true,
      value: emptySummary,
    });

    cleanup();
    expect(removed).toEqual([AWAY_CHANNELS.summary]);
  });

  it('把 AwayGatewayError 的 code/status 摊进失败信封', async () => {
    const { handlers } = makeHarness({
      getSummary: async () => {
        throw new AwayGatewayError('http-error', 'away summary unavailable', 502);
      },
    });

    await expect(handlers.get(AWAY_CHANNELS.summary)!({})).resolves.toEqual({
      ok: false,
      code: 'http-error',
      message: 'away summary unavailable',
      status: 502,
    });
  });

  it('未知异常降级为 unknown 信封', async () => {
    const { handlers } = makeHarness({
      getSummary: async () => {
        throw new Error('底层炸了');
      },
    });

    await expect(handlers.get(AWAY_CHANNELS.summary)!({})).resolves.toEqual({
      ok: false,
      code: 'unknown',
      message: '底层炸了',
      status: undefined,
    });
  });
});
