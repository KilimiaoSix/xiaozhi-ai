/** @vitest-environment jsdom */

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AwaySummaryPanel } from './AwaySummaryPanel';
import type { AwayEventItem, AwaySummaryResult } from './types';

const item = (overrides: Partial<AwayEventItem> = {}): AwayEventItem => ({
  kind: 'generic',
  text: '一条普通消息',
  taskKey: '',
  severity: 'normal',
  source: '',
  at: '2026-08-28T09:40:00',
  count: 1,
  ...overrides,
});

const summary = (overrides: Partial<AwaySummaryResult> = {}): AwaySummaryResult => ({
  away: true,
  awaySince: '2026-08-28T09:30:00',
  awayMinutes: 42,
  count: 1,
  speech: '你离开的四十二分钟里，有个任务在等你确认。',
  items: [item()],
  ...overrides,
});

const gatewayMocks = vi.hoisted(() => ({
  getSummary: vi.fn(),
}));

vi.mock('./services/awaySummaryDesktopGateway', () => ({
  awaySummaryDesktopGateway: gatewayMocks,
}));

const flush = async (): Promise<void> => {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
};

const findButton = (container: HTMLElement, selector: string): HTMLButtonElement => {
  const button = container.querySelector<HTMLButtonElement>(selector);
  if (!button) throw new Error(`找不到按钮 ${selector}`);
  return button;
};

describe('AwaySummaryPanel', () => {
  let container: HTMLDivElement;
  let root: Root;

  const mount = async (): Promise<void> => {
    await act(async () => {
      root.render(<AwaySummaryPanel />);
      await flush();
    });
  };

  beforeEach(() => {
    gatewayMocks.getSummary.mockReset();
    vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
    container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('按播报优先级分桶展示，并给出每桶条数', async () => {
    gatewayMocks.getSummary.mockResolvedValue(summary({
      count: 4,
      items: [
        item({ kind: 'incident', severity: 'critical', text: '支付超时', count: 3 }),
        item({ kind: 'agent_needs_user', text: '补参数校验 需要你看一下', source: 'Codex' }),
        item({ kind: 'agent_completed', text: 'Codex 的任务完成了', source: 'Codex' }),
        item({ kind: 'visitor_message', text: '有同事留言：下午三点碰一下' }),
      ],
    }));

    await mount();

    const groups = [...container.querySelectorAll('.away-bucket')];
    expect(groups.map((node) => node.querySelector('.away-bucket-title')?.textContent))
      .toEqual(['严重告警', '等待你操作', 'Agent 任务结果', '同事留言']);
    expect(groups[0].querySelector('.away-bucket-count')?.textContent).toContain('1');
    expect(container.querySelectorAll('.away-event').length).toBe(4);
  });

  it('逐条显示来源与时间，重复次数出现在条目上', async () => {
    gatewayMocks.getSummary.mockResolvedValue(summary({
      items: [item({
        kind: 'incident',
        severity: 'critical',
        text: '支付超时',
        source: 'SAE',
        at: '2026-08-28T09:47:00',
        count: 3,
      })],
    }));

    await mount();

    const event = container.querySelector('.away-event');
    expect(event?.textContent).toContain('支付超时');
    expect(event?.querySelector('.away-event-source')?.textContent).toBe('SAE');
    expect(event?.querySelector('.away-event-at')?.textContent).toBe('09:47');
    expect(event?.textContent).toContain('×3');
  });

  it('顶部显示总条数、离席时长与服务端播报稿', async () => {
    gatewayMocks.getSummary.mockResolvedValue(summary({ count: 2, items: [item(), item()] }));

    await mount();

    expect(container.querySelector('.away-headline')?.textContent).toContain('2');
    expect(container.textContent).toContain('42');
    expect(container.querySelector('.away-speech')?.textContent)
      .toContain('你离开的四十二分钟里');
  });

  it('没有待汇总事项时显示空态且不假造内容', async () => {
    gatewayMocks.getSummary.mockResolvedValue(summary({
      away: false,
      awaySince: null,
      awayMinutes: 0,
      count: 0,
      speech: null,
      items: [],
    }));

    await mount();

    expect(container.querySelector('.away-empty')).not.toBeNull();
    expect(container.querySelectorAll('.away-event').length).toBe(0);
    expect(container.querySelector('.away-speech')).toBeNull();
  });

  it('Server 不可达时显示离线原因', async () => {
    gatewayMocks.getSummary.mockRejectedValue(new Error('连接本地 Server 超时'));

    await mount();

    expect(container.textContent).toContain('连接本地 Server 超时');
  });

  it('迟到的旧响应不允许覆盖新状态', async () => {
    let resolveFirst!: (value: AwaySummaryResult) => void;
    const first = new Promise<AwaySummaryResult>((resolve) => { resolveFirst = resolve; });
    gatewayMocks.getSummary
      .mockReturnValueOnce(first)
      .mockResolvedValueOnce(summary({ items: [item({ text: '新状态' })] }));

    await mount();
    await act(async () => {
      findButton(container, '.away-refresh').click();
      await flush();
    });
    expect(container.textContent).toContain('新状态');

    await act(async () => {
      resolveFirst(summary({ items: [item({ text: '旧状态' })] }));
      await flush();
    });

    expect(container.textContent).toContain('新状态');
    expect(container.textContent).not.toContain('旧状态');
  });

  it('挂载后按固定间隔轮询，卸载后停止', async () => {
    vi.useFakeTimers();
    gatewayMocks.getSummary.mockResolvedValue(summary({ count: 0, items: [] }));

    await act(async () => {
      root.render(<AwaySummaryPanel />);
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(gatewayMocks.getSummary).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(gatewayMocks.getSummary).toHaveBeenCalledTimes(2);

    await act(async () => root.unmount());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15000);
    });
    expect(gatewayMocks.getSummary).toHaveBeenCalledTimes(2);
  });
});
