import { describe, expect, it, vi } from 'vitest';

import { PomodoroHttpClient } from './pomodoroHttpClient';

const jsonResponse = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });

describe('PomodoroHttpClient', () => {
  it('拉取设备列表并转换为驼峰字段', async () => {
    const fetcher = vi.fn().mockResolvedValue(jsonResponse({
      success: true,
      devices: [
        { device_id: 'esp32-01', connected: true, active: false },
        { device_id: 'esp32-02', connected: false, active: false },
      ],
    }));
    const client = new PomodoroHttpClient(fetcher);

    await expect(client.listDevices()).resolves.toEqual({
      devices: [
        { deviceId: 'esp32-01', connected: true, active: false },
        { deviceId: 'esp32-02', connected: false, active: false },
      ],
    });

    const [url, init] = fetcher.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('http://127.0.0.1:8003/xiaozhi/pomodoro/devices');
    expect(init.method).toBe('GET');
  });

  it('拉取单设备状态快照', async () => {
    const fetcher = vi.fn().mockResolvedValue(jsonResponse({
      success: true,
      device_id: 'esp32-01',
      connected: true,
      active: true,
      phase: 'focus',
      paused: false,
      remaining_s: 1200,
      total_s: 1500,
      round: 2,
      total_rounds: 4,
    }));
    const client = new PomodoroHttpClient(fetcher);

    await expect(client.getStatus('esp32-01')).resolves.toEqual({
      deviceId: 'esp32-01',
      connected: true,
      active: true,
      phase: 'focus',
      paused: false,
      remainingS: 1200,
      totalS: 1500,
      round: 2,
      totalRounds: 4,
    });

    expect(fetcher.mock.calls[0]?.[0]).toBe(
      'http://127.0.0.1:8003/xiaozhi/pomodoro/esp32-01',
    );
  });

  it('发送命令时把请求体转换为 snake_case 并带上可选的 focus_minutes', async () => {
    const fetcher = vi.fn().mockResolvedValue(jsonResponse({
      success: true,
      device_id: 'esp32-01',
      connected: true,
      active: true,
      phase: 'focus',
      paused: false,
      remaining_s: 1500,
      total_s: 1500,
      round: 1,
      total_rounds: 4,
    }));
    const client = new PomodoroHttpClient(fetcher);

    await client.sendCommand({ deviceId: 'esp32-01', command: 'start', focusMinutes: 25 });

    const [url, init] = fetcher.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('http://127.0.0.1:8003/xiaozhi/pomodoro/esp32-01/command');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ command: 'start', focus_minutes: 25 });
  });

  it('把未知命令的 400 响应转换成稳定的 HTTP 错误', async () => {
    const fetcher = vi.fn().mockResolvedValue(jsonResponse({
      success: false,
      message: 'unknown command',
    }, 400));
    const client = new PomodoroHttpClient(fetcher);

    await expect(client.sendCommand({ deviceId: 'esp32-01', command: 'pause' }))
      .rejects.toMatchObject({
        name: 'PomodoroGatewayError',
        code: 'http-error',
        status: 400,
        message: 'unknown command',
      });
  });

  it('把网络异常转换成离线错误', async () => {
    const fetcher = vi.fn().mockRejectedValue(new TypeError('fetch failed'));
    const client = new PomodoroHttpClient(fetcher);

    await expect(client.listDevices()).rejects.toMatchObject({
      name: 'PomodoroGatewayError',
      code: 'offline',
    });
  });

  it('把超时异常转换成超时错误', async () => {
    const fetcher = vi.fn().mockRejectedValue(new DOMException('timeout', 'TimeoutError'));
    const client = new PomodoroHttpClient(fetcher);

    await expect(client.getStatus('esp32-01')).rejects.toMatchObject({
      name: 'PomodoroGatewayError',
      code: 'timeout',
    });
  });

  it('把缺少必需字段的响应转换成无效响应错误', async () => {
    const fetcher = vi.fn().mockResolvedValue(jsonResponse({
      success: true,
      device_id: 'esp32-01',
    }));
    const client = new PomodoroHttpClient(fetcher);

    await expect(client.getStatus('esp32-01')).rejects.toMatchObject({
      name: 'PomodoroGatewayError',
      code: 'invalid-response',
    });
  });
});
