import {
  PomodoroGatewayError,
  type PomodoroCommandInput,
  type PomodoroIpcResult,
} from '../../modules/features/focus-mode/types';
import type { PomodoroHttpClient } from './pomodoroHttpClient';

export const POMODORO_CHANNELS = {
  listDevices: 'pomodoro:list-devices',
  getStatus: 'pomodoro:get-status',
  sendCommand: 'pomodoro:send-command',
} as const;

export interface PomodoroIpcMainLike {
  handle(
    channel: string,
    listener: (event: unknown, ...args: unknown[]) => unknown,
  ): void;
  removeHandler(channel: string): void;
}

interface RegisterPomodoroIpcOptions {
  ipcMain: PomodoroIpcMainLike;
  client: Pick<PomodoroHttpClient, 'listDevices' | 'getStatus' | 'sendCommand'>;
}

// 直接把异常抛回 ipcMain.handle 等于让 Electron 包一层 "Error invoking remote method ..."，
// 网关错误的 code/status 全丢。这里跟 agentHooks / feishu 一样用信封回传，
// 区别是番茄钟还要带上 code/status，renderer 侧才能还原成 PomodoroGatewayError。
const safe = async <T>(operation: () => Promise<T>): Promise<PomodoroIpcResult<T>> => {
  try {
    return { ok: true, value: await operation() };
  } catch (error) {
    if (error instanceof PomodoroGatewayError) {
      return { ok: false, code: error.code, message: error.message, status: error.status };
    }
    return {
      ok: false,
      code: 'unknown',
      message: error instanceof Error ? error.message : '番茄钟操作失败',
      status: undefined,
    };
  }
};

export const registerPomodoroIpc = (
  options: RegisterPomodoroIpcOptions,
): (() => void) => {
  options.ipcMain.handle(
    POMODORO_CHANNELS.listDevices,
    () => safe(() => options.client.listDevices()),
  );
  options.ipcMain.handle(
    POMODORO_CHANNELS.getStatus,
    (_event, deviceId) => safe(() => options.client.getStatus(deviceId as string)),
  );
  options.ipcMain.handle(
    POMODORO_CHANNELS.sendCommand,
    (_event, input) => safe(() => options.client.sendCommand(input as PomodoroCommandInput)),
  );

  return () => {
    for (const channel of Object.values(POMODORO_CHANNELS)) {
      options.ipcMain.removeHandler(channel);
    }
  };
};
