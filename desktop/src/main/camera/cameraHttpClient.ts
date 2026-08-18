import {
  CameraGatewayError,
  type MonitoringFrameInput,
  type MonitoringFrameResult,
  type OwnerEnrollmentInput,
  type OwnerEnrollmentResult,
} from '../../modules/features/camera-capture/types';

type Fetcher = (
  input: string | URL | Request,
  init?: RequestInit,
) => Promise<Response>;

interface JsonRecord {
  [key: string]: unknown;
}

const asRecord = (value: unknown): JsonRecord => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new CameraGatewayError('invalid-response', 'Server 返回了无效数据');
  }
  return value as JsonRecord;
};

const requiredString = (record: JsonRecord, key: string): string => {
  const value = record[key];
  if (typeof value !== 'string' || !value) {
    throw new CameraGatewayError('invalid-response', `Server 响应缺少 ${key}`);
  }
  return value;
};

const requiredNumber = (record: JsonRecord, key: string): number => {
  const value = record[key];
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new CameraGatewayError('invalid-response', `Server 响应缺少 ${key}`);
  }
  return value;
};

export class CameraHttpClient {
  constructor(
    private readonly fetcher: Fetcher = fetch,
    private readonly baseUrl = 'http://127.0.0.1:8003',
  ) {}

  async enrollOwner(input: OwnerEnrollmentInput): Promise<OwnerEnrollmentResult> {
    const displayName = input.displayName.trim();
    if (!displayName) {
      throw new CameraGatewayError('invalid-response', '主人名称不能为空');
    }

    const form = new FormData();
    form.set('display_name', displayName);
    form.set('captured_at', input.capturedAt);
    form.set(
      'image',
      new Blob([input.jpeg], { type: 'image/jpeg' }),
      'owner.jpg',
    );
    const payload = await this.postJson('/api/identity/enroll', form);
    return {
      profileId: requiredString(payload, 'profile_id'),
      sampleId: requiredString(payload, 'sample_id'),
      storedAt: requiredString(payload, 'stored_at'),
    };
  }

  async uploadMonitoringFrame(
    input: MonitoringFrameInput,
  ): Promise<MonitoringFrameResult> {
    const form = new FormData();
    form.set('session_id', input.sessionId);
    form.set('sequence', String(input.sequence));
    form.set('captured_at', input.capturedAt);
    form.set(
      'image',
      new Blob([input.jpeg], { type: 'image/jpeg' }),
      `frame-${input.sequence}.jpg`,
    );
    const payload = await this.postJson('/api/vision/frames', form);
    return {
      accepted: payload.accepted === true,
      sessionId: requiredString(payload, 'session_id'),
      sequence: requiredNumber(payload, 'sequence'),
      receivedAt: requiredString(payload, 'received_at'),
    };
  }

  private async postJson(path: string, form: FormData): Promise<JsonRecord> {
    try {
      const response = await this.fetcher(`${this.baseUrl}${path}`, {
        method: 'POST',
        body: form,
        signal: AbortSignal.timeout(5000),
      });
      let payload: JsonRecord;
      try {
        payload = asRecord(await response.json());
      } catch (error) {
        if (error instanceof CameraGatewayError) throw error;
        throw new CameraGatewayError('invalid-response', 'Server 返回的不是 JSON');
      }
      if (!response.ok) {
        const message = typeof payload.message === 'string'
          ? payload.message
          : `Server 请求失败（${response.status}）`;
        throw new CameraGatewayError('http-error', message, response.status);
      }
      return payload;
    } catch (error) {
      if (error instanceof CameraGatewayError) throw error;
      if (error instanceof DOMException && error.name === 'TimeoutError') {
        throw new CameraGatewayError('timeout', '连接本地 Server 超时');
      }
      throw new CameraGatewayError('offline', '本地 Server 当前不可用');
    }
  }
}
