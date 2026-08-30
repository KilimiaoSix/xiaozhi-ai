import {
  AwayGatewayError,
  type AwayEventItem,
  type AwaySummaryResult,
} from '../../modules/features/away-summary/types';

type Fetcher = (
  input: string | URL | Request,
  init?: RequestInit,
) => Promise<Response>;

interface JsonRecord {
  [key: string]: unknown;
}

const asRecord = (value: unknown): JsonRecord => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new AwayGatewayError('invalid-response', 'Server 返回了无效数据');
  }
  return value as JsonRecord;
};

const optionalString = (record: JsonRecord, key: string): string => {
  const value = record[key];
  return typeof value === 'string' ? value : '';
};

const nullableString = (record: JsonRecord, key: string): string | null => {
  const value = record[key];
  if (value === null || value === undefined) return null;
  if (typeof value !== 'string') {
    throw new AwayGatewayError('invalid-response', `Server 响应的 ${key} 类型不正确`);
  }
  return value;
};

const numberOr = (record: JsonRecord, key: string, fallback: number): number => {
  const value = record[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
};

const toItem = (value: unknown): AwayEventItem => {
  const record = asRecord(value);
  return {
    kind: optionalString(record, 'kind') || 'generic',
    text: optionalString(record, 'text'),
    taskKey: optionalString(record, 'task_key'),
    severity: optionalString(record, 'severity') || 'normal',
    source: optionalString(record, 'source'),
    at: optionalString(record, 'at'),
    count: numberOr(record, 'count', 1),
  };
};

/**
 * 返岗汇总的只读客户端。
 *
 * 这里刻意只有 GET：接口本身不清账，清账在机器人真的播报出去之后由服务端做，
 * 桌面端多一个「标记已读」按钮就等于替主人把没听到的留言划掉。
 *
 * 地址与令牌按次向配置中心取，构造期不缓存，改完设置无需重启。
 */
export class AwaySummaryHttpClient {
  constructor(
    private readonly fetcher: Fetcher,
    private readonly resolveBaseUrl: () => string,
    private readonly resolveAuthToken: () => string,
  ) {}

  async getSummary(): Promise<AwaySummaryResult> {
    const payload = await this.request('/xiaozhi/away/summary');
    const itemsValue = payload.items;
    if (!Array.isArray(itemsValue)) {
      throw new AwayGatewayError('invalid-response', 'Server 返回的离席事项格式不正确');
    }
    const items = itemsValue.map(toItem);
    return {
      away: payload.away === true,
      awaySince: nullableString(payload, 'away_since'),
      awayMinutes: numberOr(payload, 'away_minutes', 0),
      count: numberOr(payload, 'count', items.length),
      speech: nullableString(payload, 'speech'),
      items,
    };
  }

  private async request(path: string): Promise<JsonRecord> {
    // 末尾斜杠在这里归一：地址是用户在设置面板随手填的，
    // http://host:8003/ 拼出来的 //xiaozhi/... 会被 aiohttp 判成 404。
    const baseUrl = this.resolveBaseUrl().replace(/\/+$/, '');
    const authToken = this.resolveAuthToken();
    const headers: Record<string, string> = {};
    if (authToken) headers.Authorization = `Bearer ${authToken}`;

    try {
      const response = await this.fetcher(`${baseUrl}${path}`, {
        method: 'GET',
        headers,
        signal: AbortSignal.timeout(5000),
      });
      // 错误响应体不一定是 JSON：旧版服务端没有这条路由时 aiohttp 回 text/plain 404。
      // 必须按状态码优先报「请求失败（404）」——报「不是 JSON」会把排查引向解析问题，
      // 而真正的线索是服务端版本旧、该重启了。
      let payload: JsonRecord | null = null;
      try {
        payload = asRecord(await response.json());
      } catch (error) {
        if (response.ok) {
          if (error instanceof AwayGatewayError) throw error;
          throw new AwayGatewayError('invalid-response', 'Server 返回的不是 JSON');
        }
      }
      if (!response.ok || (payload !== null && payload.ok === false)) {
        // 200 + ok:false 同样算失败：把它当成空汇总会让主人以为「离席期间没事发生」
        const message = payload && typeof payload.message === 'string'
          ? payload.message
          : `Server 请求失败（${response.status}）`;
        throw new AwayGatewayError('http-error', message, response.status);
      }
      if (payload === null) {
        throw new AwayGatewayError('invalid-response', 'Server 返回的不是 JSON');
      }
      return payload;
    } catch (error) {
      if (error instanceof AwayGatewayError) throw error;
      if (error instanceof DOMException && error.name === 'TimeoutError') {
        throw new AwayGatewayError('timeout', '连接本地 Server 超时');
      }
      throw new AwayGatewayError('offline', '本地 Server 当前不可用');
    }
  }
}
