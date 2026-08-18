import { describe, expect, it } from 'vitest';

import { createFeatureTestContext } from '../testContext';
import { incidentAssistantModule } from './module';

describe('incidentAssistantModule', () => {
  it('提供告警诊断模块入口和 Mock 结果', async () => {
    expect(incidentAssistantModule.definition.id).toBe('incident-assistant');
    await expect(incidentAssistantModule.execute(createFeatureTestContext()))
      .resolves.toMatchObject({ source: 'mock', tone: 'coral' });
  });
});
