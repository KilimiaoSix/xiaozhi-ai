import { describe, expect, it } from 'vitest';

import { createFeatureTestContext } from '../testContext';
import { gestureApprovalModule } from './module';

describe('gestureApprovalModule', () => {
  it('提供手势审批模块入口和 Mock 结果', async () => {
    expect(gestureApprovalModule.definition.id).toBe('gesture-approval');
    await expect(gestureApprovalModule.execute(createFeatureTestContext()))
      .resolves.toMatchObject({ source: 'mock', tone: 'amber' });
  });
});
