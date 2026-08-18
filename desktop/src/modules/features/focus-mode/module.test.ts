import { describe, expect, it } from 'vitest';

import { createFeatureTestContext } from '../testContext';
import { focusModeModule } from './module';

describe('focusModeModule', () => {
  it('提供专注模式模块入口和 Mock 结果', async () => {
    expect(focusModeModule.definition.id).toBe('focus-mode');
    await expect(focusModeModule.execute(createFeatureTestContext()))
      .resolves.toMatchObject({ source: 'mock', tone: 'violet' });
  });
});
