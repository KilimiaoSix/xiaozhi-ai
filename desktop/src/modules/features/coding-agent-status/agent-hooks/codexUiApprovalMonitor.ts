import type { AgentAttention, AgentAttentionMonitor } from './runtime';

interface CodexUiApprovalMonitorOptions {
  probe: () => Promise<boolean | undefined>;
  pollIntervalMs?: number;
  now?: () => Date;
}

type AttentionListener = (attention: AgentAttention | null) => void | Promise<void>;

export class CodexUiApprovalMonitor implements AgentAttentionMonitor {
  private readonly pollIntervalMs: number;
  private readonly now: () => Date;
  private listener?: AttentionListener;
  private timer?: NodeJS.Timeout;
  private currentPoll?: Promise<void>;
  private approvalVisible = false;

  constructor(private readonly options: CodexUiApprovalMonitorOptions) {
    this.pollIntervalMs = options.pollIntervalMs ?? 1_000;
    this.now = options.now ?? (() => new Date());
  }

  start(listener: AttentionListener): void {
    if (this.timer) return;
    this.listener = listener;
    this.runPoll();
    this.timer = setInterval(() => { this.runPoll(); }, this.pollIntervalMs);
    this.timer.unref?.();
  }

  async stop(): Promise<void> {
    if (this.timer) clearInterval(this.timer);
    this.timer = undefined;
    await this.currentPoll;
    this.listener = undefined;
    this.approvalVisible = false;
  }

  private runPoll(): void {
    if (this.currentPoll) return;
    const poll = this.poll().catch(() => undefined);
    this.currentPoll = poll;
    void poll.finally(() => {
      if (this.currentPoll === poll) this.currentPoll = undefined;
    });
  }

  private async poll(): Promise<void> {
    let visible: boolean | undefined;
    try {
      visible = await this.options.probe();
    } catch {
      return;
    }
    if (visible === undefined) return;
    if (visible === this.approvalVisible) return;

    this.approvalVisible = visible;
    await this.listener?.(visible ? {
      source: 'codex',
      reason: 'Computer Use 需要用户确认',
      detectedAt: this.now().toISOString(),
    } : null);
  }
}
