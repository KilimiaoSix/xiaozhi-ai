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

  it('只把编码 Agent 监控标记为已就绪', () => {
    expect(featureCatalog.filter((feature) => feature.status === 'ready').map(({ id }) => id))
      .toEqual(['coding-agent-status']);
    expect(featureCatalog.filter((feature) => feature.status === 'placeholder')).toHaveLength(7);
  });
});
