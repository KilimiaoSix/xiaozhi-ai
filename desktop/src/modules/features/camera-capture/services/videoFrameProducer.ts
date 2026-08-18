import type { FrameSendResult } from '../types';

const FRAME_INTERVAL_MS = 200;
const MAX_WIDTH = 640;
const MAX_HEIGHT = 360;
const JPEG_QUALITY = 0.72;

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

export class VideoFrameProducer {
  private readonly video: VideoFrameSource;
  private readonly sendFrame: (jpeg: ArrayBuffer) => Promise<FrameSendResult>;
  private readonly encode: FrameEncoder;
  private readonly onSnapshot?: (snapshot: FrameProducerSnapshot) => void;
  private callbackHandle: number | null = null;
  private running = false;
  private inFlight = false;
  private lastCaptureAt = Number.NEGATIVE_INFINITY;
  private generation = 0;
  private sent = 0;
  private dropped = 0;

  constructor(options: VideoFrameProducerOptions) {
    this.video = options.video;
    this.sendFrame = options.sendFrame;
    this.encode = options.encode ?? createCanvasEncoder();
    this.onSnapshot = options.onSnapshot;
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    this.generation += 1;
    this.schedule();
  }

  stop(): void {
    this.running = false;
    this.generation += 1;
    if (this.callbackHandle !== null) {
      this.video.cancelVideoFrameCallback(this.callbackHandle);
      this.callbackHandle = null;
    }
  }

  snapshot(): FrameProducerSnapshot {
    return { sent: this.sent, dropped: this.dropped, inFlight: this.inFlight };
  }

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
