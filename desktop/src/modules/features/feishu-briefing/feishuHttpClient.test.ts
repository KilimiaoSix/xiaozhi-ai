import { describe, expect, it, vi } from 'vitest';

import { FeishuHttpClient } from './feishuHttpClient';

const jsonResponse = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });

describe('FeishuHttpClient', () => {
  it('通过 Server 读取飞书 OpenAPI 状态并携带 Bearer Token', async () => {
    const fetcher = vi.fn().mockResolvedValue(jsonResponse({
      code: 'OK',
      message: 'success',
      data: {
        state: 'ready',
        message: 'Server 已配置飞书用户凭据。',
        open_id: 'ou_me',
        source: 'server_openapi',
        required_scopes: ['calendar:calendar:readonly', 'task:task:read'],
        missing_scopes: [],
      },
    }));
    const client = new FeishuHttpClient(
      fetcher,
      'http://127.0.0.1:8003/',
      'server-secret',
    );

    await expect(client.getStatus()).resolves.toEqual({
      state: 'ready',
      message: 'Server 已配置飞书用户凭据。',
      openId: 'ou_me',
      source: 'server_openapi',
      requiredScopes: ['calendar:calendar:readonly', 'task:task:read'],
      missingScopes: [],
    });
    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:8003/xiaozhi/feishu/status',
      expect.objectContaining({
        method: 'GET',
        headers: { Authorization: 'Bearer server-secret' },
      }),
    );
  });

  it('把 Server 的任务与日程响应转换为桌面契约', async () => {
    const fetcher = vi.fn().mockResolvedValue(jsonResponse({
      code: 'OK',
      message: 'success',
      data: {
        status: {
          state: 'ready',
          message: 'Server 已配置飞书用户凭据。',
          open_id: 'ou_me',
          source: 'server_openapi',
          required_scopes: ['task:task:read'],
          missing_scopes: [],
        },
        date: '2026-08-19',
        meetings: [{
          id: 'event-1',
          title: '产品周会',
          start: '2026-08-19T01:00:00+00:00',
          end: '2026-08-19T02:00:00+00:00',
          all_day: false,
          url: 'https://example.com/event-1',
        }],
        tasks: [{
          id: 'task-1',
          title: '整理演示材料',
          due: '2026-08-19T08:00:00+00:00',
          all_day: false,
          url: 'https://example.com/task-1',
          project_name: '桌面精灵',
        }],
        warnings: [],
        fetched_at: '2026-08-19T08:01:00+08:00',
      },
    }));
    const client = new FeishuHttpClient(fetcher);

    await expect(client.getBriefing()).resolves.toEqual({
      status: {
        state: 'ready',
        message: 'Server 已配置飞书用户凭据。',
        openId: 'ou_me',
        source: 'server_openapi',
        requiredScopes: ['task:task:read'],
        missingScopes: [],
      },
      date: '2026-08-19',
      meetings: [{
        id: 'event-1',
        title: '产品周会',
        start: '2026-08-19T01:00:00+00:00',
        end: '2026-08-19T02:00:00+00:00',
        allDay: false,
        url: 'https://example.com/event-1',
      }],
      tasks: [{
        id: 'task-1',
        title: '整理演示材料',
        due: '2026-08-19T08:00:00+00:00',
        allDay: false,
        url: 'https://example.com/task-1',
        projectName: '桌面精灵',
      }],
      warnings: [],
      fetchedAt: '2026-08-19T08:01:00+08:00',
    });
    expect(fetcher.mock.calls[0]?.[0]).toBe(
      'http://127.0.0.1:8003/xiaozhi/feishu/briefing',
    );
  });

  it('把 Server 离线转换为可操作错误', async () => {
    const fetcher = vi.fn().mockRejectedValue(new TypeError('fetch failed'));
    const client = new FeishuHttpClient(fetcher);

    await expect(client.getStatus()).rejects.toThrow('本地 Server 当前不可用');
  });
});
