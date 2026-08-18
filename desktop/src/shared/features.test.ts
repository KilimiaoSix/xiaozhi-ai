import { describe, expect, it } from 'vitest';

import { featureCatalog } from '../modules/features';

describe('featureCatalog', () => {
  it('provides all eight demo capabilities in priority order', () => {
    expect(featureCatalog.map((feature) => feature.id)).toEqual([
      'identity-welcome',
      'feishu-briefing',
      'coding-agent-status',
      'gesture-approval',
      'focus-mode',
      'away-messages',
      'return-summary',
      'incident-assistant',
    ]);
  });

  it('飞书 CLI、编码 Agent 监控和专注模式已就绪，其余仍是占位', () => {
    expect(featureCatalog.filter((feature) => feature.status === 'ready').map(({ id }) => id))
      .toEqual(['feishu-briefing', 'coding-agent-status', 'focus-mode']);
    expect(featureCatalog.filter((feature) => feature.status === 'placeholder')).toHaveLength(5);
  });
});
