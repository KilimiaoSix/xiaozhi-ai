import { describe, expect, it } from 'vitest';

import { createFeatureTestContext } from '../testContext';
import { identityWelcomeModule } from './module';

describe('identityWelcomeModule', () => {
  it('提供身份欢迎模块入口和 Mock 结果', async () => {
    expect(identityWelcomeModule.definition.id).toBe('identity-welcome');
    await expect(identityWelcomeModule.execute(createFeatureTestContext()))
      .resolves.toMatchObject({ source: 'mock', tone: 'cyan' });
  });
});
