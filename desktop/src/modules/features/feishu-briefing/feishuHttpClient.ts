import type {
  FeishuBriefingSnapshot,
  FeishuConnectionStatus,
} from './contracts';

type Fetcher = (
  input: string | URL | Request,
  init?: RequestInit,
) => Promise<Response>;

type JsonRecord = Record<string, unknown>;

const asRecord = (value: unknown, message = 'Server 返回了无效数据'): JsonRecord => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(message);
  }
  return value as JsonRecord;
};

const stringValue = (record: JsonRecord, key: string): string | undefined => {
  const value = record[key];
  return typeof value === 'string' && value ? value : undefined;
};

const stringArray = (record: JsonRecord, key: string): string[] => {
  const value = record[key];
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : [];
};

const requiredString = (record: JsonRecord, key: string): string => {
  const value = stringValue(record, key);
  if (!value) throw new Error(`Server 响应缺少 ${key}`);
  return value;
};

const requiredBoolean = (record: JsonRecord, key: string): boolean => {
  const value = record[key];
  if (typeof value !== 'boolean') throw new Error(`Server 响应缺少 ${key}`);
  return value;
};

const parseStatus = (value: unknown): FeishuConnectionStatus => {
  const record = asRecord(value);
  const state = stringValue(record, 'state');
  if (state !== 'ready' && state !== 'configuration_required' && state !== 'error') {
    throw new Error('Server 返回了未知的飞书连接状态');
  }
  return {
    state,
    message: stringValue(record, 'message') ?? '飞书服务状态未知',
    openId: stringValue(record, 'open_id'),
    source: 'server_openapi',
    requiredScopes: stringArray(record, 'required_scopes'),
    missingScopes: stringArray(record, 'missing_scopes'),
  };
};

const parseBriefing = (value: unknown): FeishuBriefingSnapshot => {
  const record = asRecord(value);
  const meetingsValue = record.meetings;
  const tasksValue = record.tasks;
  if (!Array.isArray(meetingsValue) || !Array.isArray(tasksValue)) {
    throw new Error('Server 返回的飞书任务或日程格式不正确');
  }
  return {
    status: parseStatus(record.status),
    date: requiredString(record, 'date'),
    meetings: meetingsValue.map((value) => {
      const meeting = asRecord(value);
      return {
        id: requiredString(meeting, 'id'),
        title: requiredString(meeting, 'title'),
        start: requiredString(meeting, 'start'),
        end: stringValue(meeting, 'end'),
        allDay: requiredBoolean(meeting, 'all_day'),
        url: stringValue(meeting, 'url'),
      };
    }),
    tasks: tasksValue.map((value) => {
      const task = asRecord(value);
      return {
        id: requiredString(task, 'id'),
        title: requiredString(task, 'title'),
        due: stringValue(task, 'due'),
        allDay: requiredBoolean(task, 'all_day'),
        url: stringValue(task, 'url'),
        projectName: stringValue(task, 'project_name'),
      };
    }),
    warnings: stringArray(record, 'warnings'),
    fetchedAt: requiredString(record, 'fetched_at'),
  };
};

export class FeishuHttpClient {
  private readonly baseUrl: string;

  constructor(
    private readonly fetcher: Fetcher = fetch,
    baseUrl = 'http://127.0.0.1:8003',
    private readonly authToken = '',
  ) {
    this.baseUrl = baseUrl.replace(/\/+$/, '');
  }

  async getStatus(): Promise<FeishuConnectionStatus> {
    return parseStatus(await this.request('/xiaozhi/feishu/status'));
  }

  async getBriefing(): Promise<FeishuBriefingSnapshot> {
    return parseBriefing(await this.request('/xiaozhi/feishu/briefing'));
  }

  private async request(path: string): Promise<unknown> {
    const headers: Record<string, string> = {};
    if (this.authToken) headers.Authorization = `Bearer ${this.authToken}`;
    let response: Response;
    try {
      response = await this.fetcher(`${this.baseUrl}${path}`, {
        method: 'GET',
        headers,
        signal: AbortSignal.timeout(20_000),
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === 'TimeoutError') {
        throw new Error('连接本地 Server 超时');
      }
      throw new Error('本地 Server 当前不可用');
    }
    // 解析失败不能直接抛：错误响应体常是 aiohttp 的 text/plain（旧进程没有新
    // 路由就回 404 纯文本），先留住状态码再判断，否则用户只看到 JSON 解析报错。
    let envelope: JsonRecord | null = null;
    try {
      envelope = asRecord(await response.json(), 'Server 返回的不是 JSON');
    } catch {
      envelope = null;
    }
    if (!response.ok || (envelope !== null && envelope.code !== 'OK')) {
      throw new Error(
        (envelope && stringValue(envelope, 'message')) ??
          `Server 请求失败（${response.status}）`,
      );
    }
    if (envelope === null) throw new Error('Server 返回的不是 JSON');
    return envelope.data;
  }
}
