import { describe, expect, it } from 'vitest';

import { createFeatureTestContext } from '../testContext';
import { createAwaySummaryModule } from './module';
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

const gatewayWith = (summary: Partial<AwaySummaryResult>) => ({
  getSummary: async (): Promise<AwaySummaryResult> => ({
    away: false,
    awaySince: null,
    awayMinutes: 0,
    count: summary.items?.length ?? 0,
    speech: null,
    items: [],
    ...summary,
  }),
});

describe('awaySummaryModule', () => {
  it('卡片已做实：status 为 ready，替代原来的返岗 Mock 入口', () => {
    const module = createAwaySummaryModule(gatewayWith({}));
    expect(module.definition.id).toBe('away-summary');
    expect(module.definition.status).toBe('ready');
    expect(module.definition.triggerLabel).not.toContain('模拟');
  });

  it('execute 返回真实条数与最高优先级桶', async () => {
    const module = createAwaySummaryModule(gatewayWith({
      away: true,
      awayMinutes: 42,
      count: 3,
      items: [
        item({ kind: 'incident', severity: 'critical', text: '支付超时' }),
        item({ kind: 'agent_needs_user', text: '等你确认' }),
        item({ kind: 'generic' }),
      ],
    }));

    const result = await module.execute(createFeatureTestContext());

    expect(result.source).toBe('live');
    expect(result.title).toContain('3');
    expect(result.detail).toContain('严重告警');
  });

  it('没有待汇总事项时给出安静文案', async () => {
    const module = createAwaySummaryModule(gatewayWith({ count: 0, items: [] }));

    const result = await module.execute(createFeatureTestContext());

    expect(result.source).toBe('live');
    expect(result.title).toContain('没有');
  });

  it('Server 不可用时降级为失败结果而不是抛异常', async () => {
    const module = createAwaySummaryModule({
      getSummary: async () => {
        throw new Error('本地 Server 当前不可用');
      },
    });

    const result = await module.execute(createFeatureTestContext());

    expect(result.source).toBe('live');
    expect(result.tone).toBe('coral');
    expect(result.detail).toContain('本地 Server 当前不可用');
  });
});
