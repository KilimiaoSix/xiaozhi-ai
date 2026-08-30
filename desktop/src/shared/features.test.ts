import { describe, expect, it } from 'vitest';

import { featureCatalog, visibleFeatureCatalog } from '../modules/features';

describe('featureCatalog', () => {
  it('保留全部九张卡片的定义（含四张已从 UI 收起的占位卡）', () => {
    expect(featureCatalog.map((feature) => feature.id)).toEqual([
      'identity-welcome',
      'feishu-briefing',
      'coding-agent-status',
      'gesture-approval',
      'focus-mode',
      'away-messages',
      'away-summary',
      'incident-assistant',
      'return-summary',
    ]);
  });

  it('五项能力已做实，四张占位卡仍是 placeholder 但不上屏', () => {
    expect(featureCatalog.filter((feature) => feature.status === 'ready').map(({ id }) => id))
      .toEqual([
        'feishu-briefing',
        'coding-agent-status',
        'focus-mode',
        'away-summary',
        'incident-assistant',
      ]);
    expect(featureCatalog.filter((feature) => feature.status === 'placeholder')).toHaveLength(4);
    expect(visibleFeatureCatalog.every((feature) => feature.status === 'ready')).toBe(true);
  });
});
