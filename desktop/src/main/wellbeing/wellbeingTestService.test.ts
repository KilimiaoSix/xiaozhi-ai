import { describe, expect, it, vi } from 'vitest';

import { WellbeingTestService } from './wellbeingTestService';

describe('WellbeingTestService', () => {
  it('sends a labeled long-work test without speaking the test marker', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        ok: true,
        count: 1,
        devices: ['30:30:f9:37:f5:6c'],
      }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        ok: true,
        delivered: true,
        spoke: false,
      }), { status: 200 }));
    const service = new WellbeingTestService(fetcher, 'http://127.0.0.1:8003');

    await expect(service.sendTest('long_work')).resolves.toEqual({
      deviceId: '30:30:f9:37:f5:6c',
    });

    expect(fetcher).toHaveBeenNthCalledWith(
      1,
      'http://127.0.0.1:8003/xiaozhi/event/devices',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    const [url, init] = fetcher.mock.calls[1] as [string, RequestInit];
    expect(url).toBe('http://127.0.0.1:8003/xiaozhi/event/push');
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toEqual({
      device_id: '30:30:f9:37:f5:6c',
      text: '坐得有点久了，站起来伸伸腰，走两分钟再继续吧。',
      emotion: 'relaxed',
      status: '测试·休息一下',
      speak: true,
      silent: false,
      action: 'look_up',
      restore_after: 10,
      source: 'APP测试',
    });
  });

  it('rejects the test when there is not exactly one online robot', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      ok: true,
      count: 2,
      devices: ['robot-1', 'robot-2'],
    }), { status: 200 }));
    const service = new WellbeingTestService(fetcher, 'http://127.0.0.1:8003');

    await expect(service.sendTest('long_work')).rejects.toThrow(
      '在线机器人有 2 台，需要恰好一台才能发送测试提醒',
    );
    expect(fetcher).toHaveBeenCalledOnce();
  });

  it('sends the commute-safety care behavior with speech', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ devices: ['robot-1'] })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ delivered: true })));
    const service = new WellbeingTestService(fetcher, 'http://127.0.0.1:8003');

    await service.sendTest('commute_safety');

    const [, init] = fetcher.mock.calls[1] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toMatchObject({
      text: '快下班啦，回去路上慢一点，注意安全。',
      emotion: 'winking',
      status: '测试·下班平安',
      speak: true,
      silent: false,
      action: 'nod',
    });
  });

  it('sends the 21:00 overtime care behavior with speech', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ devices: ['robot-1'] })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ delivered: true })));
    const service = new WellbeingTestService(fetcher, 'http://127.0.0.1:8003');

    await service.sendTest('overtime');

    const [, init] = fetcher.mock.calls[1] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toMatchObject({
      text: '已经九点了，今天辛苦了。把手头这点收个尾，早点下班吧。',
      emotion: 'sleepy',
      status: '测试·早点下班',
      speak: true,
      action: 'look_down',
    });
  });

  it('sends the late-night strong reminder with speech and shake', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ devices: ['robot-1'] })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ delivered: true })));
    const service = new WellbeingTestService(fetcher, 'http://127.0.0.1:8003');

    await service.sendTest('frantic_overtime');

    const [, init] = fetcher.mock.calls[1] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toMatchObject({
      text: '已经很晚了，工作先停在这里。现在就收拾回家，明天再继续。',
      emotion: 'shocked',
      status: '测试·该下班了',
      speak: true,
      action: 'shake',
    });
  });

  it('sends the warm-encouragement behavior with speech', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ devices: ['robot-1'] })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ delivered: true })));
    const service = new WellbeingTestService(fetcher, 'http://127.0.0.1:8003');

    await service.sendTest('warm_encouragement');

    const [, init] = fetcher.mock.calls[1] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toMatchObject({
      text: '认真工作的你很棒，给你一颗小爱心。',
      emotion: 'loving',
      status: '测试·给你加油',
      speak: true,
      silent: false,
      action: 'nod',
    });
  });

  it('连不上 Server 时说的是连不上，而不是「需要恰好一台机器人」', async () => {
    // 两种失败都走 discoverRobotDeviceId 的 null 分支，旧实现一律报设备数问题，
    // 把「服务端没起」的排查方向引向机器人。
    const fetcher = vi.fn().mockRejectedValue(new TypeError('fetch failed'));
    const service = new WellbeingTestService(fetcher);

    await expect(service.sendTest('long_work')).rejects.toThrow(
      '本地 Server 当前不可用',
    );
  });

  it('从 DESKPET_SERVER 读取服务端地址', async () => {
    const previous = process.env.DESKPET_SERVER;
    process.env.DESKPET_SERVER = 'http://10.0.0.9:8003';
    try {
      const fetcher = vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ ok: true, devices: ['dc:da:0c:26:9a:60'] }), {
          headers: { 'content-type': 'application/json' },
        }),
      );
      const service = new WellbeingTestService(fetcher);
      await service.sendTest('long_work').catch(() => undefined);

      expect(String(fetcher.mock.calls[0][0])).toContain('10.0.0.9:8003');
    } finally {
      if (previous === undefined) delete process.env.DESKPET_SERVER;
      else process.env.DESKPET_SERVER = previous;
    }
  });
});
