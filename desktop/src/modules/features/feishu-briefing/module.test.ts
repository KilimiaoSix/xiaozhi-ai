import { describe, expect, it } from 'vitest';

import { createFeatureTestContext } from '../testContext';
import { feishuBriefingModule } from './module';

describe('feishuBriefingModule', () => {
  it('提供飞书简报模块入口和 Mock 结果', async () => {
    expect(feishuBriefingModule.definition.id).toBe('feishu-briefing');
    await expect(feishuBriefingModule.execute(createFeatureTestContext()))
      .resolves.toMatchObject({ source: 'mock', tone: 'violet' });
  });
});
