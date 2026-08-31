import { describe, expect, it, vi } from 'vitest';

import { AwayGatewayError } from '../../modules/features/away-summary/types';
import { AwaySummaryHttpClient } from './awaySummaryHttpClient';

// 配置中心的默认地址；客户端自己不留任何硬编码兜底。
const BASE_URL = 'http://127.0.0.1:8003';

const jsonResponse = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });

const summaryBody = (overrides: Record<string, unknown> = {}) => ({
  ok: true,
  away: true,
  away_since: '2026-08-28T09:30:00',
  away_minutes: 42,
  count: 2,
  speech: '你离开的四十二分钟里，有个任务在等你确认。',
  items: [
    {
      kind: 'agent_needs_user',
      text: '补接口参数校验 需要你看一下',
      task_key: 'codex:abc',
      severity: 'normal',
      source: 'Codex',
      at: '2026-08-28T09:40:00',
      count: 1,
    },
    {
      kind: 'visitor_message',
      text: '有同事留言：下午三点碰一下',
      task_key: '',
      severity: 'normal',
      source: '',
      at: '2026-08-28T09:50:00',
      count: 1,
    },
  ],
  ...overrides,
});

describe('AwaySummaryHttpClient.getSummary', () => {
  it('只读拉取汇总并转换为驼峰字段', async () => {
    const fetcher = vi.fn().mockResolvedValue(jsonResponse(summaryBody()));
    const client = new AwaySummaryHttpClient(fetcher, () => BASE_URL, () => '');

    const result = await client.getSummary();

    expect(fetcher).toHaveBeenCalledTimes(1);
    const [url, init] = fetcher.mock.calls[0];
    expect(url).toBe(`${BASE_URL}/xiaozhi/away/summary`);
    // 只读不清账：这条链路只允许 GET，POST 会把主人还没听到的留言标成已播报
    expect(init.method).toBe('GET');
    expect(result).toEqual({
      away: true,
      awaySince: '2026-08-28T09:30:00',
      awayMinutes: 42,
      count: 2,
      speech: '你离开的四十二分钟里，有个任务在等你确认。',
      items: [
        {
          kind: 'agent_needs_user',
          text: '补接口参数校验 需要你看一下',
          taskKey: 'codex:abc',
          severity: 'normal',
          source: 'Codex',
          at: '2026-08-28T09:40:00',
          count: 1,
        },
        {
          kind: 'visitor_message',
          text: '有同事留言：下午三点碰一下',
          taskKey: '',
          severity: 'normal',
          source: '',
          at: '2026-08-28T09:50:00',
          count: 1,
        },
      ],
    });
  });

  it('地址与令牌按次向配置中心取，末尾斜杠归一', async () => {
    // 每次现造 Response：同一个实例的 body 只能读一次，复用会让第二次请求假报解析失败
    const fetcher = vi.fn().mockImplementation(async () => jsonResponse(summaryBody()));
    let baseUrl = 'http://127.0.0.1:8003/';
    let token = '';
    const client = new AwaySummaryHttpClient(
      fetcher,
      () => baseUrl,
      () => token,
    );

    await client.getSummary();
    expect(fetcher.mock.calls[0][0]).toBe('http://127.0.0.1:8003/xiaozhi/away/summary');
    expect(fetcher.mock.calls[0][1].headers).toEqual({});

    baseUrl = 'http://192.168.1.9:8003';
    token = 'demo-key';
    await client.getSummary();
    expect(fetcher.mock.calls[1][0]).toBe('http://192.168.1.9:8003/xiaozhi/away/summary');
    expect(fetcher.mock.calls[1][1].headers).toEqual({ Authorization: 'Bearer demo-key' });
  });

  it('空台账（away=false、无事项）照常返回', async () => {
    const fetcher = vi.fn().mockResolvedValue(jsonResponse({
      ok: true,
      away: false,
      away_since: null,
      away_minutes: 0,
      count: 0,
      speech: null,
      items: [],
    }));
    const client = new AwaySummaryHttpClient(fetcher, () => BASE_URL, () => '');

    await expect(client.getSummary()).resolves.toEqual({
      away: false,
      awaySince: null,
      awayMinutes: 0,
      count: 0,
      speech: null,
      items: [],
    });
  });

  it('服务端 ok:false 时按 message 报错', async () => {
    const fetcher = vi.fn().mockResolvedValue(jsonResponse(
      { ok: false, message: 'away summary unavailable: boom' },
      502,
    ));
    const client = new AwaySummaryHttpClient(fetcher, () => BASE_URL, () => '');

    await expect(client.getSummary()).rejects.toMatchObject({
      name: 'AwayGatewayError',
      code: 'http-error',
      message: 'away summary unavailable: boom',
      status: 502,
    });
  });

  it('200 但 ok:false 也算失败，不把空汇总当成真的没事', async () => {
    const fetcher = vi.fn().mockResolvedValue(jsonResponse({
      ok: false,
      message: 'unauthorized',
    }));
    const client = new AwaySummaryHttpClient(fetcher, () => BASE_URL, () => '');

    await expect(client.getSummary()).rejects.toBeInstanceOf(AwayGatewayError);
  });

  it('旧版服务端没有该路由时报状态码而不是解析错误', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response('404: Not Found', { status: 404, headers: { 'content-type': 'text/plain' } }),
    );
    const client = new AwaySummaryHttpClient(fetcher, () => BASE_URL, () => '');

    await expect(client.getSummary()).rejects.toMatchObject({
      code: 'http-error',
      message: 'Server 请求失败（404）',
      status: 404,
    });
  });

  it('items 不是数组时报无效响应', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      jsonResponse(summaryBody({ items: '两条' })),
    );
    const client = new AwaySummaryHttpClient(fetcher, () => BASE_URL, () => '');

    await expect(client.getSummary()).rejects.toMatchObject({ code: 'invalid-response' });
  });

  it('超时与断连各自归一成可上屏的中文原因', async () => {
    const timeout = new AwaySummaryHttpClient(
      vi.fn().mockRejectedValue(new DOMException('timeout', 'TimeoutError')),
      () => BASE_URL,
      () => '',
    );
    await expect(timeout.getSummary()).rejects.toMatchObject({
      code: 'timeout',
      message: '连接本地 Server 超时',
    });

    const offline = new AwaySummaryHttpClient(
      vi.fn().mockRejectedValue(new TypeError('fetch failed')),
      () => BASE_URL,
      () => '',
    );
    await expect(offline.getSummary()).rejects.toMatchObject({
      code: 'offline',
      message: '本地 Server 当前不可用',
    });
  });
});
