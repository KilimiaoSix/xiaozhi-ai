import { describe, expect, it, vi } from 'vitest';

import { IncidentGatewayError } from '../../modules/features/incident-assistant/types';
import {
  INCIDENT_CHANNELS,
  registerIncidentIpc,
  type IncidentIpcMainLike,
} from './registerIncidentIpc';

const makeHarness = (
  client: Parameters<typeof registerIncidentIpc>[0]['client'],
) => {
  const handlers = new Map<string, (event: unknown, ...args: unknown[]) => unknown>();
  const removed: string[] = [];
  const ipcMain: IncidentIpcMainLike = {
    handle: (channel, handler) => { handlers.set(channel, handler); },
    removeHandler: (channel) => { removed.push(channel); },
  };
  const cleanup = registerIncidentIpc({ ipcMain, client });
  return { handlers, removed, cleanup };
};

describe('registerIncidentIpc', () => {
  it('注册三个告警 channel 并在成功时返回 ok 信封', async () => {
    const list = { date: '2026-08-19', incidents: [] };
    const { handlers, removed, cleanup } = makeHarness({
      list: async () => list,
      ack: async () => ({ acknowledged: true }),
      diagnose: async () => ({ accepted: true }),
    });

    expect([...handlers.keys()].sort()).toEqual([
      INCIDENT_CHANNELS.ack,
      INCIDENT_CHANNELS.diagnose,
      INCIDENT_CHANNELS.list,
    ].sort());

    await expect(handlers.get(INCIDENT_CHANNELS.list)!({})).resolves.toEqual({
      ok: true,
      value: list,
    });
    await expect(handlers.get(INCIDENT_CHANNELS.ack)!({}, 'demo-1')).resolves.toEqual({
      ok: true,
      value: { acknowledged: true },
    });
    await expect(
      handlers.get(INCIDENT_CHANNELS.diagnose)!({}, 'demo-1'),
    ).resolves.toEqual({ ok: true, value: { accepted: true } });

    cleanup();
    expect(removed.sort()).toEqual([...handlers.keys()].sort());
  });

  it('把 IncidentGatewayError 的 code/status 摊进失败信封', async () => {
    const { handlers } = makeHarness({
      list: vi.fn(),
      ack: async () => {
        throw new IncidentGatewayError('http-error', '故障已恢复，无需标记', 409);
      },
      diagnose: vi.fn(),
    });

    await expect(handlers.get(INCIDENT_CHANNELS.ack)!({}, 'demo-1')).resolves.toEqual({
      ok: false,
      code: 'http-error',
      message: '故障已恢复，无需标记',
      status: 409,
    });
  });

  it('未知异常降级为 unknown 信封', async () => {
    const { handlers } = makeHarness({
      list: async () => {
        throw new Error('底层炸了');
      },
      ack: vi.fn(),
      diagnose: vi.fn(),
    });

    await expect(handlers.get(INCIDENT_CHANNELS.list)!({})).resolves.toEqual({
      ok: false,
      code: 'unknown',
      message: '底层炸了',
      status: undefined,
    });
  });
});
