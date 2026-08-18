import { describe, expect, it } from 'vitest';

import { featureCatalog } from './features';

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

  it('marks every scaffold capability as a placeholder', () => {
    expect(featureCatalog.every((feature) => feature.status === 'placeholder')).toBe(true);
  });
});
