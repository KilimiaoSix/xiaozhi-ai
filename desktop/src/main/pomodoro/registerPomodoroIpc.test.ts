import { describe, expect, it, vi } from 'vitest';

import { PomodoroGatewayError } from '../../modules/features/focus-mode/types';
import {
  POMODORO_CHANNELS,
  registerPomodoroIpc,
  type PomodoroIpcMainLike,
} from './registerPomodoroIpc';

const makeHarness = (
  client: Parameters<typeof registerPomodoroIpc>[0]['client'],
) => {
  const handlers = new Map<string, (event: unknown, ...args: unknown[]) => unknown>();
  const removed: string[] = [];
  const ipcMain: PomodoroIpcMainLike = {
    handle: (channel, handler) => { handlers.set(channel, handler); },
    removeHandler: (channel) => { removed.push(channel); },
  };
  const cleanup = registerPomodoroIpc({ ipcMain, client });
  return { handlers, removed, cleanup };
};

const status = {
  deviceId: 'esp32-01',
  connected: true,
  active: true,
  phase: 'focus' as const,
  paused: false,
  remainingS: 1200,
  totalS: 1500,
  round: 1,
  totalRounds: 4,
};

describe('registerPomodoroIpc', () => {
  it('注册三个番茄钟 channel 并在成功时返回 ok 信封', async () => {
    const { handlers, removed, cleanup } = makeHarness({
      listDevices: async () => ({ devices: [{ deviceId: 'esp32-01', connected: true, active: true }] }),
      getStatus: async () => status,
      sendCommand: async () => status,
    });

    expect([...handlers.keys()].sort()).toEqual([
      POMODORO_CHANNELS.getStatus,
      POMODORO_CHANNELS.listDevices,
      POMODORO_CHANNELS.sendCommand,
    ].sort());

    await expect(handlers.get(POMODORO_CHANNELS.listDevices)!({})).resolves.toEqual({
      ok: true,
      value: { devices: [{ deviceId: 'esp32-01', connected: true, active: true }] },
    });
    await expect(handlers.get(POMODORO_CHANNELS.getStatus)!({}, 'esp32-01')).resolves.toEqual({
      ok: true,
      value: status,
    });

    cleanup();
    expect(removed.sort()).toEqual([...handlers.keys()].sort());
  });

  it('把 PomodoroGatewayError 的 code/status 摊进失败信封，而不是让它跨 IPC 被剥掉', async () => {
    const { handlers } = makeHarness({
      listDevices: vi.fn(),
      getStatus: vi.fn(),
      sendCommand: async () => {
        throw new PomodoroGatewayError('http-error', 'unknown command', 400);
      },
    });

    await expect(
      handlers.get(POMODORO_CHANNELS.sendCommand)!({}, { deviceId: 'esp32-01', command: 'pause' }),
    ).resolves.toEqual({
      ok: false,
      code: 'http-error',
      message: 'unknown command',
      status: 400,
    });
  });

  it('把非网关异常归到 unknown，保证 handler 永远不向 renderer 抛异常', async () => {
    const { handlers } = makeHarness({
      listDevices: async () => { throw new TypeError('boom'); },
      getStatus: vi.fn(),
      sendCommand: vi.fn(),
    });

    await expect(handlers.get(POMODORO_CHANNELS.listDevices)!({})).resolves.toEqual({
      ok: false,
      code: 'unknown',
      message: 'boom',
      status: undefined,
    });
  });

  it('透传设备 id 与命令入参', async () => {
    const getStatus = vi.fn().mockResolvedValue(status);
    const sendCommand = vi.fn().mockResolvedValue(status);
    const { handlers } = makeHarness({ listDevices: vi.fn(), getStatus, sendCommand });

    await handlers.get(POMODORO_CHANNELS.getStatus)!({}, 'esp32-02');
    await handlers.get(POMODORO_CHANNELS.sendCommand)!(
      {},
      { deviceId: 'esp32-02', command: 'start', focusMinutes: 25 },
    );

    expect(getStatus).toHaveBeenCalledWith('esp32-02');
    expect(sendCommand).toHaveBeenCalledWith({
      deviceId: 'esp32-02',
      command: 'start',
      focusMinutes: 25,
    });
  });
});
