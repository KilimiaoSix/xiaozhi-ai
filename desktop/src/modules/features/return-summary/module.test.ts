import { describe, expect, it } from 'vitest';

import { createFeatureTestContext } from '../testContext';
import { returnSummaryModule } from './module';

describe('returnSummaryModule', () => {
  it('提供返岗汇总模块入口和 Mock 结果', async () => {
    expect(returnSummaryModule.definition.id).toBe('return-summary');
    await expect(returnSummaryModule.execute(createFeatureTestContext()))
      .resolves.toMatchObject({ source: 'mock', tone: 'cyan' });
  });
});
