export type PomodoroPhase = 'focus' | 'short_break' | 'long_break' | 'idle';

export type PomodoroCommand = 'start' | 'pause' | 'resume' | 'toggle' | 'skip' | 'stop';

export interface PomodoroDeviceSummary {
  deviceId: string;
  connected: boolean;
  active: boolean;
}

export interface PomodoroDeviceListResult {
  devices: PomodoroDeviceSummary[];
}

export interface PomodoroStatus {
  deviceId: string;
  connected: boolean;
  active: boolean;
  phase: PomodoroPhase;
  paused: boolean;
  remainingS: number;
  totalS: number;
  round: number;
  totalRounds: number;
}

export interface PomodoroCommandInput {
  deviceId: string;
  command: PomodoroCommand;
  focusMinutes?: number;
}

export type PomodoroGatewayErrorCode =
  | 'http-error'
  | 'invalid-response'
  | 'offline'
  | 'timeout'
  // IPC 层兜底：main 侧捕到的不一定是 PomodoroGatewayError，也不能替它假装成某个已知原因
  | 'unknown';

export class PomodoroGatewayError extends Error {
  constructor(
    public readonly code: PomodoroGatewayErrorCode,
    message: string,
    public readonly status?: number,
  ) {
    super(message);
    this.name = 'PomodoroGatewayError';
  }
}

/**
 * 番茄钟三个 IPC channel 的返回信封。
 *
 * 失败必须以**数据**形式回到 renderer：Electron 跨 contextBridge 只会把 Error 的
 * message 复制过去，PomodoroGatewayError 上的 code/status 会被整个剥掉，renderer
 * 拿到的是 "Error invoking remote method ..." 的英文包装串，离线横幅就没法说人话。
 */
export type PomodoroIpcResult<T> =
  | { ok: true; value: T }
  | { ok: false; code: PomodoroGatewayErrorCode; message: string; status?: number };
