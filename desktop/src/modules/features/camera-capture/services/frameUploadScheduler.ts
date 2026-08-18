export type FrameSubmitResult = 'sent' | 'dropped' | 'backoff' | 'stopped';

export interface FrameUploadSnapshot {
  sent: number;
  dropped: number;
  inFlight: boolean;
  consecutiveFailures: number;
  lastError: string;
}

const BACKOFF_MS = [1000, 2000, 4000, 5000] as const;

export class FrameUploadScheduler {
  private sent = 0;
  private dropped = 0;
  private inFlight = false;
  private stopped = false;
  private consecutiveFailures = 0;
  private nextAllowedAt = 0;
  private lastError = '';

  constructor(
    private readonly upload: (jpeg: ArrayBuffer) => Promise<void>,
    private readonly now: () => number = Date.now,
  ) {}

  async submit(jpeg: ArrayBuffer): Promise<FrameSubmitResult> {
    if (this.stopped) return 'stopped';
    if (this.inFlight) {
      this.dropped += 1;
      return 'dropped';
    }
    if (this.now() < this.nextAllowedAt) return 'backoff';

    this.inFlight = true;
    try {
      await this.upload(jpeg);
      this.sent += 1;
      this.consecutiveFailures = 0;
      this.nextAllowedAt = 0;
      this.lastError = '';
      return 'sent';
    } catch (error) {
      this.consecutiveFailures += 1;
      const backoffIndex = Math.min(
        this.consecutiveFailures - 1,
        BACKOFF_MS.length - 1,
      );
      this.nextAllowedAt = this.now() + BACKOFF_MS[backoffIndex];
      this.lastError = error instanceof Error ? error.message : '监测帧上传失败';
      throw error;
    } finally {
      this.inFlight = false;
    }
  }

  stop(): void {
    this.stopped = true;
  }

  snapshot(): FrameUploadSnapshot {
    return {
      sent: this.sent,
      dropped: this.dropped,
      inFlight: this.inFlight,
      consecutiveFailures: this.consecutiveFailures,
      lastError: this.lastError,
    };
  }
}
