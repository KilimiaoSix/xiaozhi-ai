import { describe, expect, it } from 'vitest';

import { AWAY_BUCKET_LABELS, groupAwayEvents } from './awayBuckets';
import type { AwayEventItem } from './types';

const item = (overrides: Partial<AwayEventItem> = {}): AwayEventItem => ({
  kind: 'generic',
  text: '一条普通消息',
  taskKey: '',
  severity: 'normal',
  source: '',
  at: '2026-08-28T10:00:00',
  count: 1,
  ...overrides,
});

describe('groupAwayEvents', () => {
  it('按播报优先级分桶：严重告警 > 等待操作 > 已完成 > 留言 > 普通', () => {
    const groups = groupAwayEvents([
      item({ kind: 'generic', text: '普通' }),
      item({ kind: 'visitor_message', text: '有同事留言：下午找你' }),
      item({ kind: 'agent_completed', text: 'Codex 完成了任务' }),
      item({ kind: 'agent_needs_user', text: '等你确认' }),
      item({ kind: 'incident', severity: 'critical', text: '支付超时' }),
    ]);

    expect(groups.map((group) => group.id)).toEqual([
      'critical',
      'needs_user',
      'agent_result',
      'visitor',
      'generic',
    ]);
    expect(groups[0].label).toBe(AWAY_BUCKET_LABELS.critical);
    expect(groups.map((group) => group.items.length)).toEqual([1, 1, 1, 1, 1]);
  });

  it('空桶不出现在结果里，桶内保持服务端给的时间先后', () => {
    const groups = groupAwayEvents([
      item({ kind: 'agent_completed', text: '先完成的' }),
      item({ kind: 'agent_failed', text: '后失败的' }),
    ]);

    expect(groups).toHaveLength(1);
    expect(groups[0].id).toBe('agent_result');
    expect(groups[0].items.map((entry) => entry.text)).toEqual(['先完成的', '后失败的']);
  });

  it('非严重告警与未知 kind 一并落到普通桶，绝不丢', () => {
    const groups = groupAwayEvents([
      item({ kind: 'incident', severity: 'normal', text: '磁盘 70%' }),
      item({ kind: 'brand_new_kind', text: '服务端加的新类型' }),
    ]);

    expect(groups).toHaveLength(1);
    expect(groups[0].id).toBe('generic');
    expect(groups[0].items).toHaveLength(2);
  });

  it('没有事项时返回空数组', () => {
    expect(groupAwayEvents([])).toEqual([]);
  });
});
