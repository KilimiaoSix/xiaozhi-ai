import { describe, expect, it, vi } from 'vitest';

import { CameraHttpClient } from './cameraHttpClient';

const jpeg = new Uint8Array([0xff, 0xd8, 0xff, 0xd9]).buffer;

describe('CameraHttpClient', () => {
  it('将主人 JPEG 上传到固定本地接口', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      ok: true,
      profile_id: 'owner',
      sample_id: 'sample-1',
      stored_at: '2026-08-18T04:00:00Z',
    }), { status: 200, headers: { 'content-type': 'application/json' } }));
    const client = new CameraHttpClient(fetcher);

    await expect(client.enrollOwner({
      displayName: ' 高新成 ',
      capturedAt: '2026-08-18T12:00:00+08:00',
      jpeg,
    })).resolves.toEqual({
      profileId: 'owner',
      sampleId: 'sample-1',
      storedAt: '2026-08-18T04:00:00Z',
    });

    const [url, init] = fetcher.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('http://127.0.0.1:8003/api/identity/enroll');
    expect(init.method).toBe('POST');
    const form = init.body as FormData;
    expect(form.get('display_name')).toBe('高新成');
    expect(form.get('captured_at')).toBe('2026-08-18T12:00:00+08:00');
    expect(form.get('image')).toBeInstanceOf(Blob);
  });

  it('上传监测帧并转换 Server 响应字段', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      ok: true,
      accepted: true,
      session_id: 'session-1',
      sequence: 7,
      received_at: '2026-08-18T04:00:00Z',
    }), { status: 200, headers: { 'content-type': 'application/json' } }));
    const client = new CameraHttpClient(fetcher);

    await expect(client.uploadMonitoringFrame({
      sessionId: 'session-1',
      sequence: 7,
      capturedAt: '2026-08-18T12:00:00+08:00',
      jpeg,
    })).resolves.toEqual({
      accepted: true,
      sessionId: 'session-1',
      sequence: 7,
      receivedAt: '2026-08-18T04:00:00Z',
    });

    expect(fetcher.mock.calls[0]?.[0]).toBe(
      'http://127.0.0.1:8003/api/vision/frames',
    );
  });

  it('把非 2xx 响应转换成稳定的 HTTP 错误', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      ok: false,
      message: 'image exceeds 2 MiB',
    }), { status: 413, headers: { 'content-type': 'application/json' } }));
    const client = new CameraHttpClient(fetcher);

    await expect(client.enrollOwner({
      displayName: '高新成',
      capturedAt: '2026-08-18T12:00:00+08:00',
      jpeg,
    })).rejects.toMatchObject({
      name: 'CameraGatewayError',
      code: 'http-error',
      status: 413,
      message: 'image exceeds 2 MiB',
    });
  });

  it('把网络异常转换成离线错误', async () => {
    const fetcher = vi.fn().mockRejectedValue(new TypeError('fetch failed'));
    const client = new CameraHttpClient(fetcher);

    await expect(client.uploadMonitoringFrame({
      sessionId: 'session-1',
      sequence: 1,
      capturedAt: '2026-08-18T12:00:00+08:00',
      jpeg,
    })).rejects.toMatchObject({
      name: 'CameraGatewayError',
      code: 'offline',
    });
  });
});
