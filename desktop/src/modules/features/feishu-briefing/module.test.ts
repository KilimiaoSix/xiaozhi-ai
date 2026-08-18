import { describe, expect, it } from 'vitest';

import { createFeatureTestContext } from '../testContext';
import { feishuBriefingModule } from './module';

describe('feishuBriefingModule', () => {
  it('提供飞书 CLI 工作台入口和真实结果标记', async () => {
    expect(feishuBriefingModule.definition.id).toBe('feishu-briefing');
    await expect(feishuBriefingModule.execute(createFeatureTestContext()))
      .resolves.toMatchObject({ source: 'live', tone: 'violet' });
  });
});
