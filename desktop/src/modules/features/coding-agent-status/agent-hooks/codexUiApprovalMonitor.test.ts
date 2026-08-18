import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AgentAttention } from './runtime';
import { CodexUiApprovalMonitor } from './codexUiApprovalMonitor';

afterEach(() => {
  vi.useRealTimers();
});

describe('CodexUiApprovalMonitor', () => {
  it('只在审批卡片出现和消失时发布状态变化', async () => {
    vi.useFakeTimers();
    const probe = vi.fn()
      .mockResolvedValueOnce(false)
      .mockResolvedValueOnce(true)
      .mockResolvedValueOnce(true)
      .mockResolvedValueOnce(false);
    const updates: Array<AgentAttention | null> = [];
    const monitor = new CodexUiApprovalMonitor({
      probe,
      pollIntervalMs: 1_000,
      now: () => new Date('2026-08-18T08:00:10.000Z'),
    });

    monitor.start((attention) => { updates.push(attention); });
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(3_000);

    expect(updates).toEqual([
      {
        source: 'codex',
        reason: 'Computer Use 需要用户确认',
        detectedAt: '2026-08-18T08:00:10.000Z',
      },
      null,
    ]);

    await monitor.stop();
    await vi.advanceTimersByTimeAsync(2_000);
    expect(probe).toHaveBeenCalledTimes(4);
  });
});
