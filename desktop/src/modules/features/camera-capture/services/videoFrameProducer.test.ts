import { describe, expect, it, vi } from 'vitest';

import { VideoFrameProducer } from './videoFrameProducer';

class FakeVideo {
  videoWidth = 1280;
  videoHeight = 720;
  private nextId = 1;
  private callbacks = new Map<number, (now: number) => void>();

  requestVideoFrameCallback(callback: (now: number) => void): number {
    const id = this.nextId++;
    this.callbacks.set(id, callback);
    return id;
  }

  cancelVideoFrameCallback(id: number): void {
    this.callbacks.delete(id);
  }

  frame(now: number): void {
    const callbacks = [...this.callbacks.values()];
    this.callbacks.clear();
    callbacks.forEach((callback) => callback(now));
  }

  get pendingCallbacks(): number {
    return this.callbacks.size;
  }
}

const deferred = <T>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
};

describe('VideoFrameProducer', () => {
  it('encodes at 5 FPS with bounded dimensions and JPEG quality', async () => {
    const video = new FakeVideo();
    const sendFrame = vi.fn(async () => 'sent' as const);
    const encode = vi.fn(async () => new ArrayBuffer(12));
    const producer = new VideoFrameProducer({ video, sendFrame, encode });

    producer.start();
    video.frame(0);
    await vi.waitFor(() => expect(sendFrame).toHaveBeenCalledTimes(1));
    video.frame(100);
    video.frame(199);
    video.frame(200);
    await vi.waitFor(() => expect(sendFrame).toHaveBeenCalledTimes(2));

    expect(encode).toHaveBeenNthCalledWith(1, video, 640, 360, 0.72);
    expect(encode).toHaveBeenNthCalledWith(2, video, 640, 360, 0.72);
    expect(producer.snapshot()).toMatchObject({ sent: 2, dropped: 0 });
  });

  it('preserves portrait aspect ratio inside 640 by 360', async () => {
    const video = new FakeVideo();
    video.videoWidth = 720;
    video.videoHeight = 1280;
    const encode = vi.fn(async () => new ArrayBuffer(12));
    const producer = new VideoFrameProducer({
      video,
      encode,
      sendFrame: async () => 'sent',
    });

    producer.start();
    video.frame(0);
    await vi.waitFor(() => expect(encode).toHaveBeenCalledOnce());

    expect(encode).toHaveBeenCalledWith(video, 203, 360, 0.72);
  });

  it('allows only one encode and send operation in flight', async () => {
    const video = new FakeVideo();
    const pending = deferred<ArrayBuffer>();
    const sendFrame = vi.fn(async () => 'sent' as const);
    const producer = new VideoFrameProducer({
      video,
      sendFrame,
      encode: vi.fn(() => pending.promise),
    });

    producer.start();
    video.frame(0);
    video.frame(200);
    video.frame(400);

    expect(sendFrame).not.toHaveBeenCalled();
    expect(producer.snapshot().dropped).toBe(2);

    pending.resolve(new ArrayBuffer(12));
    await vi.waitFor(() => expect(sendFrame).toHaveBeenCalledOnce());
  });

  it('counts transport drops and keeps scheduling while not ready', async () => {
    const video = new FakeVideo();
    const sendFrame = vi.fn()
      .mockResolvedValueOnce('not-ready')
      .mockResolvedValueOnce('dropped')
      .mockResolvedValueOnce('sent');
    const producer = new VideoFrameProducer({
      video,
      sendFrame,
      encode: async () => new ArrayBuffer(12),
    });

    producer.start();
    for (const now of [0, 200, 400]) {
      video.frame(now);
      await vi.waitFor(() => expect(sendFrame).toHaveBeenCalledTimes(now / 200 + 1));
    }

    expect(producer.snapshot()).toMatchObject({ sent: 1, dropped: 2 });
    expect(video.pendingCallbacks).toBe(1);
  });

  it('cancels future callbacks and ignores an in-flight completion', async () => {
    const video = new FakeVideo();
    const pending = deferred<ArrayBuffer>();
    const sendFrame = vi.fn(async () => 'sent' as const);
    const producer = new VideoFrameProducer({
      video,
      sendFrame,
      encode: () => pending.promise,
    });

    producer.start();
    video.frame(0);
    producer.stop();
    pending.resolve(new ArrayBuffer(12));
    await Promise.resolve();
    await Promise.resolve();

    expect(video.pendingCallbacks).toBe(0);
    expect(sendFrame).not.toHaveBeenCalled();
  });

  it('reuses one canvas for the lifetime of the producer', async () => {
    const video = new FakeVideo();
    const drawImage = vi.fn();
    const canvas = {
      width: 0,
      height: 0,
      getContext: vi.fn(() => ({ drawImage })),
      toBlob: vi.fn((callback: (blob: Blob) => void) => {
        callback(new Blob([new Uint8Array([0xff, 0xd8, 0xff, 0xd9])], {
          type: 'image/jpeg',
        }));
      }),
    };
    const createElement = vi.fn(() => canvas);
    vi.stubGlobal('document', { createElement });
    const producer = new VideoFrameProducer({
      video,
      sendFrame: async () => 'sent',
    });

    producer.start();
    video.frame(0);
    await vi.waitFor(() => expect(producer.snapshot().sent).toBe(1));
    video.frame(200);
    await vi.waitFor(() => expect(producer.snapshot().sent).toBe(2));

    expect(createElement).toHaveBeenCalledOnce();
    vi.unstubAllGlobals();
  });

  describe('轨道直采路径(页面隐藏也要出帧)', () => {
    class FakeVideoFrame {
      closed = false;
      constructor(
        public displayWidth = 1280,
        public displayHeight = 720,
        private producedAt = 0,
      ) {}
      close(): void { this.closed = true; }
    }

    // 每次 read() 交付一帧;交付完挂起,由测试推帧
    const makeTrackWorld = () => {
      const frames: FakeVideoFrame[] = [];
      const waiters: Array<(f: FakeVideoFrame) => void> = [];
      let cancelled = false;
      const reader = {
        read: async () => {
          if (cancelled) return { done: true as const };
          const next = frames.shift();
          if (next) return { done: false as const, value: next };
          const value = await new Promise<FakeVideoFrame>((r) => waiters.push(r));
          return { done: false as const, value };
        },
        cancel: async () => { cancelled = true; },
      };
      const push = (frame: FakeVideoFrame) => {
        const waiter = waiters.shift();
        if (waiter) waiter(frame);
        else frames.push(frame);
      };
      const clone = { stop: vi.fn() } as unknown as MediaStreamTrack;
      const track = { clone: vi.fn(() => clone) } as unknown as MediaStreamTrack;
      class Processor {
        readable = { getReader: () => reader };
      }
      class FakeOffscreenCanvas {
        constructor(public width: number, public height: number) {}
        getContext() { return { drawImage: vi.fn() }; }
        async convertToBlob() {
          return { arrayBuffer: async () => new ArrayBuffer(9) } as Blob;
        }
      }
      vi.stubGlobal('MediaStreamTrackProcessor', Processor);
      vi.stubGlobal('OffscreenCanvas', FakeOffscreenCanvas);
      return { push, track, clone, cancelledRef: () => cancelled };
    };

    it('从轨道取帧编码发送,不依赖 video 元素回调', async () => {
      const world = makeTrackWorld();
      const perf = vi.spyOn(performance, 'now');
      perf.mockReturnValue(0);
      const video = new FakeVideo();
      const sendFrame = vi.fn(async () => 'sent' as const);
      const producer = new VideoFrameProducer({
        video, track: world.track, sendFrame,
      });

      producer.start();
      // 元素路径不该被武装:页面隐藏时它一次都不会触发
      expect(video.pendingCallbacks).toBe(0);
      world.push(new FakeVideoFrame());
      await vi.waitFor(() => expect(sendFrame).toHaveBeenCalledTimes(1));

      // 200ms 内的第二帧被节流丢弃且必须 close
      const throttled = new FakeVideoFrame();
      perf.mockReturnValue(100);
      world.push(throttled);
      // 第三帧到点后正常发送
      perf.mockReturnValue(250);
      const due = new FakeVideoFrame();
      world.push(due);
      await vi.waitFor(() => expect(sendFrame).toHaveBeenCalledTimes(2));
      expect(throttled.closed).toBe(true);
      expect(due.closed).toBe(true);
      expect(producer.snapshot()).toMatchObject({ sent: 2, dropped: 0 });

      producer.stop();
      expect((world.clone.stop as ReturnType<typeof vi.fn>)).toHaveBeenCalled();
      perf.mockRestore();
      vi.unstubAllGlobals();
    });

    it('克隆轨道失败时回退元素路径,行为与旧版一致', async () => {
      class Processor { readable = { getReader: () => ({ read: async () => ({ done: true as const }), cancel: async () => {} }) }; }
      vi.stubGlobal('MediaStreamTrackProcessor', Processor);
      const badTrack = {
        clone: vi.fn(() => { throw new Error('not supported'); }),
      } as unknown as MediaStreamTrack;
      const video = new FakeVideo();
      const sendFrame = vi.fn(async () => 'sent' as const);
      const encode = vi.fn(async () => new ArrayBuffer(3));
      const producer = new VideoFrameProducer({
        video, track: badTrack, sendFrame, encode,
      });

      producer.start();
      expect(video.pendingCallbacks).toBe(1);
      video.frame(0);
      await vi.waitFor(() => expect(sendFrame).toHaveBeenCalledTimes(1));
      producer.stop();
      vi.unstubAllGlobals();
    });

    it('环境没有 MediaStreamTrackProcessor 时直接走元素路径', async () => {
      const track = { clone: vi.fn() } as unknown as MediaStreamTrack;
      const video = new FakeVideo();
      const sendFrame = vi.fn(async () => 'sent' as const);
      const encode = vi.fn(async () => new ArrayBuffer(3));
      const producer = new VideoFrameProducer({ video, track, sendFrame, encode });

      producer.start();
      expect(video.pendingCallbacks).toBe(1);
      expect(track.clone).not.toHaveBeenCalled();
      producer.stop();
    });
  });
});
