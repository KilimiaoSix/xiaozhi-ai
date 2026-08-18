import { describe, expect, it } from 'vitest';

import { createFeatureTestContext } from '../testContext';
import { codingAgentStatusModule } from './module';

describe('codingAgentStatusModule', () => {
  it('提供编码 Agent 状态模块入口和 Mock 结果', async () => {
    expect(codingAgentStatusModule.definition.id).toBe('coding-agent-status');
    await expect(codingAgentStatusModule.execute(createFeatureTestContext()))
      .resolves.toMatchObject({ source: 'mock', tone: 'cyan' });
  });
});
