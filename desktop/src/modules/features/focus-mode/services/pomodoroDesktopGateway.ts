import {
  PomodoroGatewayError,
  type PomodoroCommand,
  type PomodoroDeviceListResult,
  type PomodoroIpcResult,
  type PomodoroStatus,
} from '../types';

// main 侧的 PomodoroGatewayError 过 IPC 只剩一个普通对象，这里按 code/status 原样重建，
// 面板拿到的才是"连接本地 Server 超时"这种能直接上屏的中文原因。
const unwrap = <T>(result: PomodoroIpcResult<T>): T => {
  if (result.ok) return result.value;
  throw new PomodoroGatewayError(result.code, result.message, result.status);
};

export const pomodoroDesktopGateway = {
  listDevices: async (): Promise<PomodoroDeviceListResult> => (
    unwrap(await window.xiaofei.pomodoro.listDevices())
  ),
  getStatus: async (deviceId: string): Promise<PomodoroStatus> => (
    unwrap(await window.xiaofei.pomodoro.getStatus(deviceId))
  ),
  sendCommand: async (
    deviceId: string,
    command: PomodoroCommand,
    focusMinutes?: number,
  ): Promise<PomodoroStatus> => (
    unwrap(await window.xiaofei.pomodoro.sendCommand({ deviceId, command, focusMinutes }))
  ),
};

export type PomodoroDesktopGateway = typeof pomodoroDesktopGateway;
