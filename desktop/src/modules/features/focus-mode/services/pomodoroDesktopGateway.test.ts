/** @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from 'vitest';

import { PomodoroGatewayError, type PomodoroStatus } from '../types';
import { pomodoroDesktopGateway } from './pomodoroDesktopGateway';

const status: PomodoroStatus = {
  deviceId: 'esp32-01',
  connected: true,
  active: true,
  phase: 'focus',
  paused: false,
  remainingS: 1200,
  totalS: 1500,
  round: 1,
  totalRounds: 4,
};

const stubPomodoroApi = (api: Record<string, unknown>): void => {
  vi.stubGlobal('xiaofei', { pomodoro: api });
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('pomodoroDesktopGateway', () => {
  it('拆开 ok 信封后返回原始值', async () => {
    const sendCommand = vi.fn().mockResolvedValue({ ok: true, value: status });
    stubPomodoroApi({
      listDevices: async () => ({
        ok: true,
        value: { devices: [{ deviceId: 'esp32-01', connected: false, active: true }] },
      }),
      getStatus: async () => ({ ok: true, value: status }),
      sendCommand,
    });

    await expect(pomodoroDesktopGateway.listDevices()).resolves.toEqual({
      devices: [{ deviceId: 'esp32-01', connected: false, active: true }],
    });
    await expect(pomodoroDesktopGateway.getStatus('esp32-01')).resolves.toEqual(status);
    await expect(pomodoroDesktopGateway.sendCommand('esp32-01', 'start', 25)).resolves.toEqual(status);
    expect(sendCommand).toHaveBeenCalledWith({
      deviceId: 'esp32-01',
      command: 'start',
      focusMinutes: 25,
    });
  });

  it('把失败信封还原成带 code/status 的 PomodoroGatewayError', async () => {
    stubPomodoroApi({
      listDevices: async () => ({ ok: false, code: 'offline', message: '本地 Server 当前不可用' }),
      getStatus: vi.fn(),
      sendCommand: async () => ({
        ok: false,
        code: 'http-error',
        message: 'unknown command',
        status: 400,
      }),
    });

    await expect(pomodoroDesktopGateway.listDevices()).rejects.toMatchObject({
      name: 'PomodoroGatewayError',
      code: 'offline',
      message: '本地 Server 当前不可用',
    });

    const failure = await pomodoroDesktopGateway
      .sendCommand('esp32-01', 'pause')
      .catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(PomodoroGatewayError);
    expect(failure).toMatchObject({ code: 'http-error', message: 'unknown command', status: 400 });
  });
});
