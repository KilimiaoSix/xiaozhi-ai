import { describe, expect, it } from 'vitest';

import { createFeatureTestContext } from '../testContext';
import { awayMessagesModule } from './module';

describe('awayMessagesModule', () => {
  it('提供离席留言模块入口和 Mock 结果', async () => {
    expect(awayMessagesModule.definition.id).toBe('away-messages');
    await expect(awayMessagesModule.execute(createFeatureTestContext()))
      .resolves.toMatchObject({ source: 'mock', tone: 'amber' });
  });
});
