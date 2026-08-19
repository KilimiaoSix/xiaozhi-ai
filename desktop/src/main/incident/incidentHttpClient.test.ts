import { describe, expect, it, vi } from 'vitest';

import { IncidentGatewayError } from '../../modules/features/incident-assistant/types';
import { IncidentHttpClient } from './incidentHttpClient';

const jsonResponse = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });

const serverIncident = (overrides: Record<string, unknown> = {}) => ({
  id: 'demo-api-abc123',
  source: 'incident',
  service: 'demo-api',
  severity: 'P1',
  title: '接口错误率升高',
  message: '支付回调错误率 12%',
  state: 'firing',
  repeat_count: 3,
  first_seen_at: '2026-08-19T10:00:00',
  last_seen_at: '2026-08-19T10:05:00',
  recovered_at: null,
  announced: true,
  acknowledged: false,
  simulated: true,
  diagnosis: null,
  timeline: [
    { at: '2026-08-19T10:00:00', event: 'received', detail: '首次收到告警' },
  ],
  ...overrides,
});

const listBody = (incidents: unknown[]) => ({
  success: true,
  date: '2026-08-19',
  state: 'all',
  count: incidents.length,
  incidents,
});

describe('IncidentHttpClient.list', () => {
  it('拉取列表并转换为驼峰字段', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      jsonResponse(
        listBody([
          serverIncident(),
          serverIncident({
            id: 'relay-1',
            source: 'alert_relay',
            severity: 'P0',
            simulated: false,
            diagnosis: {
              state: 'done',
              summary: '内存泄漏导致容器反复重启',
              finished_at: '2026-08-19T10:20:00',
            },
          }),
        ]),
      ),
    );
    const client = new IncidentHttpClient(fetcher);

    const result = await client.list();

    expect(result.date).toBe('2026-08-19');
    expect(result.incidents).toHaveLength(2);
    expect(result.incidents[0]).toEqual({
      id: 'demo-api-abc123',
      source: 'incident',
      service: 'demo-api',
      severity: 'P1',
      title: '接口错误率升高',
      message: '支付回调错误率 12%',
      state: 'firing',
      repeatCount: 3,
      firstSeenAt: '2026-08-19T10:00:00',
      lastSeenAt: '2026-08-19T10:05:00',
      recoveredAt: null,
      announced: true,
      acknowledged: false,
      simulated: true,
      diagnosis: null,
      timeline: [
        { at: '2026-08-19T10:00:00', event: 'received', detail: '首次收到告警' },
      ],
    });
    expect(result.incidents[1].diagnosis).toEqual({
      state: 'done',
      summary: '内存泄漏导致容器反复重启',
      finishedAt: '2026-08-19T10:20:00',
    });

    const [url, init] = fetcher.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('http://127.0.0.1:8003/xiaozhi/incident/list');
    expect(init.method).toBe('GET');
  });

  it('severity 不是已知枚举时报 invalid-response', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      jsonResponse(listBody([serverIncident({ severity: 'P9' })])),
    );

    await expect(new IncidentHttpClient(fetcher).list()).rejects.toMatchObject({
      code: 'invalid-response',
    });
  });

  it('缺 id 时报 invalid-response', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      jsonResponse(listBody([serverIncident({ id: '' })])),
    );

    await expect(new IncidentHttpClient(fetcher).list()).rejects.toMatchObject({
      code: 'invalid-response',
    });
  });

  it('incidents 不是数组时报 invalid-response', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      jsonResponse({ success: true, date: '2026-08-19', incidents: '坏了' }),
    );

    await expect(new IncidentHttpClient(fetcher).list()).rejects.toMatchObject({
      code: 'invalid-response',
    });
  });

  it('HTTP 错误带回服务端 message 与 status', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      jsonResponse({ success: false, message: 'limit 必须是正整数' }, 400),
    );

    await expect(new IncidentHttpClient(fetcher).list()).rejects.toMatchObject({
      code: 'http-error',
      message: 'limit 必须是正整数',
      status: 400,
    });
  });

  it('超时映射为 timeout', async () => {
    const fetcher = vi.fn().mockRejectedValue(
      new DOMException('The operation timed out.', 'TimeoutError'),
    );

    await expect(new IncidentHttpClient(fetcher).list()).rejects.toMatchObject({
      code: 'timeout',
    });
  });

  it('连接失败映射为 offline', async () => {
    const fetcher = vi.fn().mockRejectedValue(new TypeError('fetch failed'));

    await expect(new IncidentHttpClient(fetcher).list()).rejects.toMatchObject({
      code: 'offline',
    });
  });
});

describe('IncidentHttpClient.ack', () => {
  it('成功时返回 acknowledged', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      jsonResponse({ success: true, incident_id: 'demo-api-abc123', acknowledged: true }),
    );
    const client = new IncidentHttpClient(fetcher);

    await expect(client.ack('demo-api-abc123')).resolves.toEqual({ acknowledged: true });

    const [url, init] = fetcher.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('http://127.0.0.1:8003/xiaozhi/incident/demo-api-abc123/ack');
    expect(init.method).toBe('POST');
  });

  it('409（已恢复）作为 http-error 抛出，面板据此提示', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      jsonResponse({ success: false, message: '故障已恢复，时间线已定稿，无需标记' }, 409),
    );

    await expect(new IncidentHttpClient(fetcher).ack('x')).rejects.toMatchObject({
      code: 'http-error',
      status: 409,
      message: '故障已恢复，时间线已定稿，无需标记',
    });
  });

  it('id 会被 URL 编码', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      jsonResponse({ success: true, acknowledged: true }),
    );

    await new IncidentHttpClient(fetcher).ack('demo api#1');

    const [url] = fetcher.mock.calls[0] as [string];
    expect(url).toBe('http://127.0.0.1:8003/xiaozhi/incident/demo%20api%231/ack');
  });
});

describe('IncidentHttpClient.diagnose', () => {
  it('受理时返回 accepted=true', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      jsonResponse({
        success: true,
        accepted: true,
        incident_id: 'demo-api-abc123',
        diagnosis: { state: 'running' },
      }),
    );
    const client = new IncidentHttpClient(fetcher);

    await expect(client.diagnose('demo-api-abc123')).resolves.toEqual({ accepted: true });

    const [url, init] = fetcher.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('http://127.0.0.1:8003/xiaozhi/incident/demo-api-abc123/diagnose');
    expect(init.method).toBe('POST');
  });

  it('409（诊断已在跑）不算错误，归一为 accepted=false', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      jsonResponse(
        { success: false, message: '这个故障的诊断还在跑', diagnosis: { state: 'running' } },
        409,
      ),
    );

    await expect(new IncidentHttpClient(fetcher).diagnose('x')).resolves.toEqual({
      accepted: false,
    });
  });

  it('400（alert_relay 来源）作为 http-error 抛出并带服务端说明', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      jsonResponse({ success: false, message: '该条目来自值班中继（alert_relay）……' }, 400),
    );

    const failure = new IncidentHttpClient(fetcher).diagnose('relay-1');

    await expect(failure).rejects.toBeInstanceOf(IncidentGatewayError);
    await expect(failure).rejects.toMatchObject({ code: 'http-error', status: 400 });
  });
});
