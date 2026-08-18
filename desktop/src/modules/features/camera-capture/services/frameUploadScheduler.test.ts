import { describe, expect, it } from 'vitest';

import { FrameUploadScheduler } from './frameUploadScheduler';

describe('FrameUploadScheduler', () => {
  it('上传未结束时丢弃新帧', async () => {
    let resolveUpload!: () => void;
    const uploadPending = new Promise<void>((resolve) => {
      resolveUpload = resolve;
    });
    const scheduler = new FrameUploadScheduler(() => uploadPending);

    const first = scheduler.submit(new ArrayBuffer(4));
    await expect(scheduler.submit(new ArrayBuffer(4))).resolves.toBe('dropped');
    expect(scheduler.snapshot()).toMatchObject({
      sent: 0,
      dropped: 1,
      inFlight: true,
    });

    resolveUpload();
    await expect(first).resolves.toBe('sent');
    expect(scheduler.snapshot()).toMatchObject({
      sent: 1,
      dropped: 1,
      inFlight: false,
    });
  });

  it('失败后进入有限退避并在成功后恢复', async () => {
    let now = 1000;
    let shouldFail = true;
    const scheduler = new FrameUploadScheduler(
      async () => {
        if (shouldFail) throw new Error('offline');
      },
      () => now,
    );

    await expect(scheduler.submit(new ArrayBuffer(4))).rejects.toThrow('offline');
    await expect(scheduler.submit(new ArrayBuffer(4))).resolves.toBe('backoff');

    now = 2000;
    shouldFail = false;
    await expect(scheduler.submit(new ArrayBuffer(4))).resolves.toBe('sent');
    expect(scheduler.snapshot()).toMatchObject({
      sent: 1,
      consecutiveFailures: 0,
      lastError: '',
    });
  });

  it('停止后不再接受新帧', async () => {
    const scheduler = new FrameUploadScheduler(async () => undefined);

    scheduler.stop();

    await expect(scheduler.submit(new ArrayBuffer(4))).resolves.toBe('stopped');
  });
});
