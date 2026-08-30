import {
  PomodoroGatewayError,
  type PomodoroCommandInput,
  type PomodoroDeviceListResult,
  type PomodoroDeviceSummary,
  type PomodoroPhase,
  type PomodoroStatus,
} from '../../modules/features/focus-mode/types';

type Fetcher = (
  input: string | URL | Request,
  init?: RequestInit,
) => Promise<Response>;

interface JsonRecord {
  [key: string]: unknown;
}

const asRecord = (value: unknown): JsonRecord => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new PomodoroGatewayError('invalid-response', 'Server 返回了无效数据');
  }
  return value as JsonRecord;
};

const requiredString = (record: JsonRecord, key: string): string => {
  const value = record[key];
  if (typeof value !== 'string' || !value) {
    throw new PomodoroGatewayError('invalid-response', `Server 响应缺少 ${key}`);
  }
  return value;
};

const requiredBoolean = (record: JsonRecord, key: string): boolean => {
  const value = record[key];
  if (typeof value !== 'boolean') {
    throw new PomodoroGatewayError('invalid-response', `Server 响应缺少 ${key}`);
  }
  return value;
};

const requiredNumber = (record: JsonRecord, key: string): number => {
  const value = record[key];
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new PomodoroGatewayError('invalid-response', `Server 响应缺少 ${key}`);
  }
  return value;
};

const PHASES: PomodoroPhase[] = ['focus', 'short_break', 'long_break', 'idle'];

const requiredPhase = (record: JsonRecord, key: string): PomodoroPhase => {
  const value = record[key];
  if (typeof value !== 'string' || !PHASES.includes(value as PomodoroPhase)) {
    throw new PomodoroGatewayError('invalid-response', `Server 响应的 ${key} 不是已知相位`);
  }
  return value as PomodoroPhase;
};

const toStatus = (payload: JsonRecord): PomodoroStatus => ({
  deviceId: requiredString(payload, 'device_id'),
  connected: requiredBoolean(payload, 'connected'),
  active: requiredBoolean(payload, 'active'),
  phase: requiredPhase(payload, 'phase'),
  paused: requiredBoolean(payload, 'paused'),
  remainingS: requiredNumber(payload, 'remaining_s'),
  totalS: requiredNumber(payload, 'total_s'),
  round: requiredNumber(payload, 'round'),
  totalRounds: requiredNumber(payload, 'total_rounds'),
});

const toDeviceSummary = (value: unknown): PomodoroDeviceSummary => {
  const record = asRecord(value);
  return {
    deviceId: requiredString(record, 'device_id'),
    connected: requiredBoolean(record, 'connected'),
    active: requiredBoolean(record, 'active'),
  };
};

export class PomodoroHttpClient {
  constructor(
    private readonly fetcher: Fetcher,
    /** 地址按次向配置中心取，构造期不缓存，改完设置无需重启。 */
    private readonly resolveBaseUrl: () => string,
  ) {}

  async listDevices(): Promise<PomodoroDeviceListResult> {
    const payload = await this.request('/xiaozhi/pomodoro/devices', { method: 'GET' });
    const devicesValue = payload.devices;
    if (!Array.isArray(devicesValue)) {
      throw new PomodoroGatewayError('invalid-response', 'Server 返回的设备列表格式不正确');
    }
    return { devices: devicesValue.map(toDeviceSummary) };
  }

  async getStatus(deviceId: string): Promise<PomodoroStatus> {
    const payload = await this.request(
      `/xiaozhi/pomodoro/${encodeURIComponent(deviceId)}`,
      { method: 'GET' },
    );
    return toStatus(payload);
  }

  async sendCommand(input: PomodoroCommandInput): Promise<PomodoroStatus> {
    const body: JsonRecord = { command: input.command };
    if (input.focusMinutes !== undefined) {
      body.focus_minutes = input.focusMinutes;
    }
    const payload = await this.request(
      `/xiaozhi/pomodoro/${encodeURIComponent(input.deviceId)}/command`,
      {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      },
    );
    return toStatus(payload);
  }

  private async request(path: string, init: RequestInit): Promise<JsonRecord> {
    try {
      const response = await this.fetcher(`${this.resolveBaseUrl()}${path}`, {
        ...init,
        signal: AbortSignal.timeout(5000),
      });
      let payload: JsonRecord;
      try {
        payload = asRecord(await response.json());
      } catch (error) {
        if (error instanceof PomodoroGatewayError) throw error;
        throw new PomodoroGatewayError('invalid-response', 'Server 返回的不是 JSON');
      }
      if (!response.ok) {
        const message = typeof payload.message === 'string'
          ? payload.message
          : `Server 请求失败（${response.status}）`;
        throw new PomodoroGatewayError('http-error', message, response.status);
      }
      return payload;
    } catch (error) {
      if (error instanceof PomodoroGatewayError) throw error;
      if (error instanceof DOMException && error.name === 'TimeoutError') {
        throw new PomodoroGatewayError('timeout', '连接本地 Server 超时');
      }
      throw new PomodoroGatewayError('offline', '本地 Server 当前不可用');
    }
  }
}
