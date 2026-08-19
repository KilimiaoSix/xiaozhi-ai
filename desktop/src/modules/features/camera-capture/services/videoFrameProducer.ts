import type { FrameSendResult } from '../types';

const FRAME_INTERVAL_MS = 200;
const MAX_WIDTH = 640;
const MAX_HEIGHT = 360;
const JPEG_QUALITY = 0.72;
const TRACK_WATCHDOG_MS = 2000;

export interface VideoFrameSource {
  readonly videoWidth: number;
  readonly videoHeight: number;
  requestVideoFrameCallback(callback: (now: number) => void): number;
  cancelVideoFrameCallback(handle: number): void;
}

export interface FrameProducerSnapshot {
  sent: number;
  dropped: number;
  inFlight: boolean;
}

type FrameEncoder = (
  video: VideoFrameSource,
  width: number,
  height: number,
  quality: number,
) => Promise<ArrayBuffer>;

interface VideoFrameProducerOptions {
  video: VideoFrameSource;
  /**
   * 摄像头视频轨。给了它且环境支持 MediaStreamTrackProcessor 时,走轨道直采:
   * 帧直接来自采集管线,与页面可见性无关——窗口最小化/被完全遮挡时,
   * Chromium 会挂起 <video> 元素的解码(readyState 停在 0),
   * requestVideoFrameCallback 一次都不会触发,元素路径整个静默断流。
   * 「切页、最小化都保留监测」的承诺只有轨道直采才真正兑现。
   */
  track?: MediaStreamTrack;
  sendFrame: (jpeg: ArrayBuffer) => Promise<FrameSendResult>;
  encode?: FrameEncoder;
  onSnapshot?: (snapshot: FrameProducerSnapshot) => void;
}

const targetDimensions = (width: number, height: number): [number, number] => {
  if (width <= 0 || height <= 0) return [0, 0];
  const scale = Math.min(1, MAX_WIDTH / width, MAX_HEIGHT / height);
  return [
    Math.max(1, Math.round(width * scale)),
    Math.max(1, Math.round(height * scale)),
  ];
};

const createCanvasEncoder = (): FrameEncoder => {
  let canvas: HTMLCanvasElement | null = null;
  return async (video, width, height, quality) => {
    canvas ??= document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext('2d', { alpha: false });
    if (!context) throw new Error('无法读取摄像头画面');
    context.drawImage(video as unknown as CanvasImageSource, 0, 0, width, height);
    const blob = await new Promise<Blob>((resolve, reject) => {
      canvas?.toBlob(
        (value) => value ? resolve(value) : reject(new Error('JPEG 编码失败')),
        'image/jpeg',
        quality,
      );
    });
    return blob.arrayBuffer();
  };
};

// MediaStreamTrackProcessor 尚未进 TS lib,自行声明最小面
interface TrackProcessorLike {
  readable: {
    getReader(): {
      read(): Promise<{ done: boolean; value?: VideoFrameLike }>;
      cancel(): Promise<void>;
    };
  };
}

interface VideoFrameLike {
  displayWidth: number;
  displayHeight: number;
  close(): void;
}

type TrackProcessorCtor = new (init: { track: MediaStreamTrack }) => TrackProcessorLike;

const trackProcessorCtor = (): TrackProcessorCtor | null => {
  const ctor = (globalThis as Record<string, unknown>).MediaStreamTrackProcessor;
  return typeof ctor === 'function' ? (ctor as TrackProcessorCtor) : null;
};

export class VideoFrameProducer {
  private readonly video: VideoFrameSource;
  private readonly track?: MediaStreamTrack;
  private readonly sendFrame: (jpeg: ArrayBuffer) => Promise<FrameSendResult>;
  private readonly encode: FrameEncoder;
  private readonly onSnapshot?: (snapshot: FrameProducerSnapshot) => void;
  private callbackHandle: number | null = null;
  private trackClone: MediaStreamTrack | null = null;
  private trackFrameSeen = false;
  private running = false;
  private inFlight = false;
  private lastCaptureAt = Number.NEGATIVE_INFINITY;
  private generation = 0;
  private sent = 0;
  private dropped = 0;

  constructor(options: VideoFrameProducerOptions) {
    this.video = options.video;
    this.track = options.track;
    this.sendFrame = options.sendFrame;
    this.encode = options.encode ?? createCanvasEncoder();
    this.onSnapshot = options.onSnapshot;
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    this.generation += 1;
    const Processor = trackProcessorCtor();
    if (this.track && Processor) {
      const generation = this.generation;
      void this.runTrackLoop(Processor, generation);
      // 看门狗:部分 Electron/Chromium 版本在窗口上下文里 MSTP 的 reader
      // 永远不出帧(规范已把它挪进 worker,窗口暴露是残留)。实测这种环境下
      // read() 静默饿死——不回退的话整条摄像头链路会无声卡死,
      // 比"退回旧行为"糟糕得多。
      setTimeout(() => {
        if (
          this.running
          && generation === this.generation
          && !this.trackFrameSeen
        ) {
          console.warn('轨道直采 2 秒无帧,回退到视频元素取帧路径');
          this.generation += 1;
          this.trackClone?.stop();
          this.trackClone = null;
          this.schedule();
        }
      }, TRACK_WATCHDOG_MS);
    } else {
      this.schedule();
    }
  }

  stop(): void {
    this.running = false;
    this.generation += 1;
    if (this.callbackHandle !== null) {
      this.video.cancelVideoFrameCallback(this.callbackHandle);
      this.callbackHandle = null;
    }
    this.trackClone?.stop();
    this.trackClone = null;
  }

  snapshot(): FrameProducerSnapshot {
    return { sent: this.sent, dropped: this.dropped, inFlight: this.inFlight };
  }

  // ---------- 轨道直采路径(与页面可见性无关) ----------

  private async runTrackLoop(
    Processor: TrackProcessorCtor,
    generation: number,
  ): Promise<void> {
    // 处理器会独占接进来的轨道,必须喂克隆:原轨道还要继续驱动预览 <video>
    let reader: ReturnType<TrackProcessorLike['readable']['getReader']>;
    try {
      this.trackClone = this.track!.clone();
      reader = new Processor({ track: this.trackClone }).readable.getReader();
    } catch {
      // 克隆或建处理器失败(老内核/特殊轨道)→ 回退元素路径,行为与旧版一致
      this.trackClone?.stop();
      this.trackClone = null;
      this.schedule();
      return;
    }

    while (this.running && generation === this.generation) {
      let frame: VideoFrameLike | undefined;
      try {
        const result = await reader.read();
        if (result.done) break;
        frame = result.value;
      } catch {
        break;
      }
      if (!frame) continue;
      this.trackFrameSeen = true;
      // 每帧都要及时 close,否则采集管线的帧池被占满后整路卡死;
      // 节流丢帧不计入 dropped——那是常态限频,不是异常
      const now = performance.now();
      if (
        !this.running
        || generation !== this.generation
        || now - this.lastCaptureAt < FRAME_INTERVAL_MS
        || this.inFlight
      ) {
        frame.close();
        continue;
      }
      this.lastCaptureAt = now;
      await this.encodeAndSendFrame(frame, generation);
    }
    await reader.cancel().catch(() => undefined);
  }

  private async encodeAndSendFrame(
    frame: VideoFrameLike,
    generation: number,
  ): Promise<void> {
    const [width, height] = targetDimensions(
      frame.displayWidth,
      frame.displayHeight,
    );
    if (!width || !height) {
      frame.close();
      return;
    }
    this.inFlight = true;
    try {
      const canvas = new OffscreenCanvas(width, height);
      const context = canvas.getContext('2d', { alpha: false });
      if (!context) throw new Error('无法读取摄像头画面');
      context.drawImage(frame as unknown as CanvasImageSource, 0, 0, width, height);
      frame.close();
      const blob = await canvas.convertToBlob({
        type: 'image/jpeg',
        quality: JPEG_QUALITY,
      });
      const jpeg = await blob.arrayBuffer();
      if (!this.running || generation !== this.generation) return;
      const result = await this.sendFrame(jpeg);
      if (!this.running || generation !== this.generation) return;
      if (result === 'sent') this.sent += 1;
      else this.dropped += 1;
    } catch {
      frame.close();
      if (this.running && generation === this.generation) this.dropped += 1;
    } finally {
      this.inFlight = false;
      if (this.running && generation === this.generation) this.notify();
    }
  }

  // ---------- 元素回退路径(rVFC,页面可见时才出帧) ----------

  private schedule(): void {
    if (!this.running || this.callbackHandle !== null) return;
    this.callbackHandle = this.video.requestVideoFrameCallback((now) => {
      this.callbackHandle = null;
      this.schedule();
      this.onVideoFrame(now);
    });
  }

  private onVideoFrame(now: number): void {
    if (!this.running || now - this.lastCaptureAt < FRAME_INTERVAL_MS) return;
    this.lastCaptureAt = now;
    if (this.inFlight) {
      this.dropped += 1;
      this.notify();
      return;
    }
    const [width, height] = targetDimensions(
      this.video.videoWidth,
      this.video.videoHeight,
    );
    if (!width || !height) return;

    this.inFlight = true;
    const generation = this.generation;
    void this.encode(this.video, width, height, JPEG_QUALITY)
      .then(async (jpeg) => {
        if (!this.running || generation !== this.generation) return;
        const result = await this.sendFrame(jpeg);
        if (!this.running || generation !== this.generation) return;
        if (result === 'sent') this.sent += 1;
        else this.dropped += 1;
      })
      .catch(() => {
        if (this.running && generation === this.generation) this.dropped += 1;
      })
      .finally(() => {
        this.inFlight = false;
        if (this.running && generation === this.generation) this.notify();
      });
  }

  private notify(): void {
    this.onSnapshot?.(this.snapshot());
  }
}
