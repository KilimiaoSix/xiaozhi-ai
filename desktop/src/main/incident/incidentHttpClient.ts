import {
  IncidentGatewayError,
  type IncidentAckResult,
  type IncidentDiagnoseResult,
  type IncidentDiagnosis,
  type IncidentDiagnosisState,
  type IncidentListResult,
  type IncidentSeverity,
  type IncidentSource,
  type IncidentState,
  type IncidentSummary,
  type IncidentTimelineEntry,
} from '../../modules/features/incident-assistant/types';

type Fetcher = (
  input: string | URL | Request,
  init?: RequestInit,
) => Promise<Response>;

interface JsonRecord {
  [key: string]: unknown;
}

const asRecord = (value: unknown): JsonRecord => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new IncidentGatewayError('invalid-response', 'Server 返回了无效数据');
  }
  return value as JsonRecord;
};

const requiredString = (record: JsonRecord, key: string): string => {
  const value = record[key];
  if (typeof value !== 'string' || !value) {
    throw new IncidentGatewayError('invalid-response', `Server 响应缺少 ${key}`);
  }
  return value;
};

const requiredBoolean = (record: JsonRecord, key: string): boolean => {
  const value = record[key];
  if (typeof value !== 'boolean') {
    throw new IncidentGatewayError('invalid-response', `Server 响应缺少 ${key}`);
  }
  return value;
};

const requiredNumber = (record: JsonRecord, key: string): number => {
  const value = record[key];
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new IncidentGatewayError('invalid-response', `Server 响应缺少 ${key}`);
  }
  return value;
};

// 展示用的时间戳字段允许缺失/为 null（老落盘文件、中继坏时间戳），
// 但存在时必须是字符串——静默吞掉类型错误会把契约漂移埋起来。
const nullableString = (record: JsonRecord, key: string): string | null => {
  const value = record[key];
  if (value === null || value === undefined) return null;
  if (typeof value !== 'string') {
    throw new IncidentGatewayError('invalid-response', `Server 响应的 ${key} 类型不正确`);
  }
  return value;
};

const optionalString = (record: JsonRecord, key: string): string => {
  const value = record[key];
  return typeof value === 'string' ? value : '';
};

const enumString = <T extends string>(
  record: JsonRecord,
  key: string,
  allowed: readonly T[],
): T => {
  const value = record[key];
  if (typeof value !== 'string' || !allowed.includes(value as T)) {
    throw new IncidentGatewayError('invalid-response', `Server 响应的 ${key} 不是已知枚举`);
  }
  return value as T;
};

const SOURCES: IncidentSource[] = ['incident', 'alert_relay'];
const SEVERITIES: IncidentSeverity[] = ['P0', 'P1', 'P2', 'P3'];
const STATES: IncidentState[] = ['firing', 'observing', 'recovered'];
const DIAGNOSIS_STATES: IncidentDiagnosisState[] = ['idle', 'running', 'done', 'failed'];

const toDiagnosis = (value: unknown): IncidentDiagnosis | null => {
  if (value === null || value === undefined) return null;
  const record = asRecord(value);
  return {
    state: enumString(record, 'state', DIAGNOSIS_STATES),
    summary: optionalString(record, 'summary'),
    finishedAt: nullableString(record, 'finished_at'),
  };
};

const toTimeline = (value: unknown): IncidentTimelineEntry[] => {
  if (!Array.isArray(value)) {
    throw new IncidentGatewayError('invalid-response', 'Server 响应的 timeline 不是数组');
  }
  return value.map((entry) => {
    const record = asRecord(entry);
    return {
      at: nullableString(record, 'at') ?? '',
      event: optionalString(record, 'event'),
      detail: optionalString(record, 'detail'),
    };
  });
};

const toIncident = (value: unknown): IncidentSummary => {
  const record = asRecord(value);
  return {
    id: requiredString(record, 'id'),
    source: enumString(record, 'source', SOURCES),
    service: optionalString(record, 'service'),
    severity: enumString(record, 'severity', SEVERITIES),
    title: requiredString(record, 'title'),
    message: optionalString(record, 'message'),
    state: enumString(record, 'state', STATES),
    repeatCount: requiredNumber(record, 'repeat_count'),
    firstSeenAt: nullableString(record, 'first_seen_at'),
    lastSeenAt: nullableString(record, 'last_seen_at'),
    recoveredAt: nullableString(record, 'recovered_at'),
    announced: requiredBoolean(record, 'announced'),
    acknowledged: requiredBoolean(record, 'acknowledged'),
    simulated: requiredBoolean(record, 'simulated'),
    diagnosis: toDiagnosis(record.diagnosis),
    timeline: toTimeline(record.timeline),
  };
};

export class IncidentHttpClient {
  constructor(
    private readonly fetcher: Fetcher = fetch,
    private readonly baseUrl = 'http://127.0.0.1:8003',
  ) {}

  async list(): Promise<IncidentListResult> {
    const payload = await this.request('/xiaozhi/incident/list', { method: 'GET' });
    const incidentsValue = payload.incidents;
    if (!Array.isArray(incidentsValue)) {
      throw new IncidentGatewayError('invalid-response', 'Server 返回的告警列表格式不正确');
    }
    return {
      date: requiredString(payload, 'date'),
      incidents: incidentsValue.map(toIncident),
    };
  }

  async ack(incidentId: string): Promise<IncidentAckResult> {
    await this.request(
      `/xiaozhi/incident/${encodeURIComponent(incidentId)}/ack`,
      { method: 'POST' },
    );
    return { acknowledged: true };
  }

  async diagnose(incidentId: string): Promise<IncidentDiagnoseResult> {
    // 409 = 服务端并发闸门：同一故障的诊断已在跑。这不是失败，桌面端
    // 同样进入「诊断中」态，所以在这里归一而不是抛给面板逐处判断。
    const payload = await this.request(
      `/xiaozhi/incident/${encodeURIComponent(incidentId)}/diagnose`,
      { method: 'POST' },
      { tolerateStatus: 409 },
    );
    return { accepted: payload.success === true };
  }

  private async request(
    path: string,
    init: RequestInit,
    options: { tolerateStatus?: number } = {},
  ): Promise<JsonRecord> {
    try {
      const response = await this.fetcher(`${this.baseUrl}${path}`, {
        ...init,
        signal: AbortSignal.timeout(5000),
      });
      // 错误响应的 body 可能不是 JSON：旧版服务端没有新路由时 aiohttp 回
      // text/plain 404。必须按状态码优先报「请求失败（404）」——报「不是 JSON」
      // 会把排查引向解析问题，而真正的线索是服务端版本旧、该重启了。
      let payload: JsonRecord | null = null;
      try {
        payload = asRecord(await response.json());
      } catch (error) {
        if (response.ok) {
          if (error instanceof IncidentGatewayError) throw error;
          throw new IncidentGatewayError('invalid-response', 'Server 返回的不是 JSON');
        }
      }
      if (!response.ok && response.status !== options.tolerateStatus) {
        const message = payload && typeof payload.message === 'string'
          ? payload.message
          : `Server 请求失败（${response.status}）`;
        throw new IncidentGatewayError('http-error', message, response.status);
      }
      if (payload === null) {
        // 被容忍的状态码（如 diagnose 的 409）body 仍必须是 JSON 契约
        throw new IncidentGatewayError('invalid-response', 'Server 返回的不是 JSON');
      }
      return payload;
    } catch (error) {
      if (error instanceof IncidentGatewayError) throw error;
      if (error instanceof DOMException && error.name === 'TimeoutError') {
        throw new IncidentGatewayError('timeout', '连接本地 Server 超时');
      }
      throw new IncidentGatewayError('offline', '本地 Server 当前不可用');
    }
  }
}
