import { describe, expect, it } from 'vitest';

import { createFeatureTestContext } from '../testContext';
import { codingAgentStatusModule } from './module';

describe('codingAgentStatusModule', () => {
  it('提供已就绪的编码 Agent 实时监控入口', async () => {
    expect(codingAgentStatusModule.definition.id).toBe('coding-agent-status');
    expect(codingAgentStatusModule.definition.status).toBe('ready');
    await expect(codingAgentStatusModule.execute(createFeatureTestContext()))
      .resolves.toMatchObject({ source: 'live', tone: 'cyan' });
  });
});
